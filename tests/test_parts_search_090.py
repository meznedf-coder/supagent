"""0.9: the choices of what a value is part of are searched on the server (a category read from the data has
thousands of values): the words typed are looked up in the names and the other names, the names that start with
them first, the wider categories first; the page asks for the first ones and is told how many there are."""

from __future__ import annotations

from test_dictionary_080 import _client


def test_the_part_of_choices_are_searched(app):
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Facet

    with app.app_context():
        settings.set_value("categories.custom", ["server"])
        rows = [Facet(facet="server", value=f"srv-{i:03d}", status="approved", source="data") for i in range(150)]
        rows += [Facet(facet="application", value="Server farm manager", status="approved", source="admin", synonyms=["SFM"]),
                 Facet(facet="subject", value="Capacity", status="approved", source="admin"),
                 Facet(facet="application", value="Waiting", status="proposed", source="llm"),
                 Facet(facet="server", value="srv-retired", status="rejected", source="data")]
        db.session.add_all(rows)
        db.session.commit()
    api = "/supagent/admin/api/facets?status=approved&brief=1"
    try:
        with _client(app, "admin") as c:
            first = c.get(api + "&limit=60").get_json()
            assert len(first["facets"]) == 60 and first["total"] >= 152 and first["capped"] is True
            assert first["facets"][0]["facet"] == "subject"                         # the wider categories first
            found = c.get(api + "&limit=60&q=srv-01").get_json()
            assert [x["value"] for x in found["facets"]] == [f"srv-01{i}" for i in range(10)] and found["total"] == 10
            assert not found["capped"]
            words = c.get(api + "&limit=60&q=SERV").get_json()["facets"]
            assert [x["value"] for x in words] == ["Server farm manager"]           # in the name, whatever the case
            assert [x["value"] for x in c.get(api + "&limit=60&q=sfm").get_json()["facets"]] == ["Server farm manager"]   # another name
            cap = [x["value"] for x in c.get(api + "&limit=5&q=cap").get_json()["facets"]]
            assert cap[0] == "Capacity"                                             # a name that starts with the words: first
            assert c.get(api + "&q=waiting").get_json()["facets"] == []            # proposed: not a choice
            assert c.get(api + "&q=srv-retired").get_json()["facets"] == []        # retired: not a choice
            everything = c.get(api).get_json()                                      # as before: the whole list, capped at 5,000
            assert len(everything["facets"]) == everything["total"] and not everything["capped"]
    finally:
        with app.app_context():
            db.session.query(Facet).filter(Facet.value.like("srv-%")).delete(synchronize_session=False)
            db.session.query(Facet).filter(Facet.value.in_(("Server farm manager", "Capacity", "Waiting"))).delete(synchronize_session=False)
            db.session.commit()
            settings.set_value("categories.custom", None)
