"""0.8.2 on the server side: a proposed value edited whole before it is approved (its category, its name, what it is
part of, together); the review without the AI-written descriptions; a category of one's own renamed and removed with
everything that names it; the map's categories with no value yet and what waits; a memory in the catalog names its
entry."""

from __future__ import annotations

from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401  (the fixture)


def _clean():
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Entry, Facet, Link, Memory, Meta, Tag

    db.session.query(Link).filter(Link.a_ref.like("facet:%")).delete(synchronize_session=False)
    db.session.query(Tag).delete()
    db.session.query(Facet).delete()
    db.session.query(Memory).delete()
    db.session.query(Entry).filter(Entry.origin.like("memory:%")).delete(synchronize_session=False)
    row = db.session.get(Meta, "system_map")
    if row is not None:
        db.session.delete(row)
    db.session.commit()
    settings.set_value("categories.custom", None)
    settings.set_value("categories.fields", None)


def test_a_proposed_value_is_edited_whole_then_approved(world, app):
    from superset.extensions import db

    from supagent.models import Facet, KObject, Tag

    with app.app_context():
        orders = Facet(facet="subject", value="Orders", status="approved", source="admin")
        pay = Facet(facet="application", value="Payments", status="approved", source="admin")
        gw = Facet(facet="component", value="Payment gateway", status="approved", source="admin")
        new = Facet(facet="component", value="billing svc", status="proposed", source="llm", description="Bills.")
        twin = Facet(facet="subject", value="PG", status="proposed", source="llm")
        wait = Facet(facet="application", value="Ledger", status="proposed", source="llm")
        again = Facet(facet="component", value="ledger app", status="proposed", source="llm")
        db.session.add_all([orders, pay, gw, new, twin, wait, again])
        db.session.flush()
        gw.parents = [pay.id]
        jobs = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
        db.session.add(Tag(ref=f"object:{jobs.id}", facet_id=twin.id, status="proposed", confidence=0.9))
        db.session.commit()
        ids = {f.value: f.id for f in (orders, pay, gw, new, twin, wait, again)}
    try:
        with _client(app, "admin") as c:
            d = c.get("/supagent/admin/api/review").get_json()
            assert "descriptions" not in d and "descriptions" not in d["counts"]       # not listed any more
            assert d["counts"]["values"] == 4
            assert d["waiting"] == sum(n for k, n in d["counts"].items() if k != "routes")
            card = next(v for v in d["values"] if v["value"] == "billing svc")
            assert card["synonyms"] == [] and card["description"] == "Bills."
            # its category, its name, what it covers, its other names and what it is part of, then approved: one save
            r = c.post(f"/supagent/admin/api/facets/{ids['billing svc']}", json={
                "facet": "application", "value": "Billing", "description": "Bills the customers.",
                "synonyms": "billing svc, BIL", "parents": [ids["Orders"]], "status": "approved"}).get_json()
            assert (r["facet"], r["value"], r["status"], r["parents"]) == ("application", "Billing", "approved", [ids["Orders"]])
            # edited and kept for later: still proposed, with what was changed
            r = c.post(f"/supagent/admin/api/facets/{ids['ledger app']}", json={
                "facet": "component", "value": "Ledger writer", "parents": [ids["Payments"]]}).get_json()
            assert (r["value"], r["status"], r["parents"]) == ("Ledger writer", "proposed", [ids["Payments"]])
            # a name that exists in the category it moves to: one value, with the parts the admin chose and approved
            r = c.post(f"/supagent/admin/api/facets/{ids['PG']}", json={
                "facet": "component", "value": "payment gateway", "parents": [ids["Orders"]], "status": "approved"}).get_json()
            assert r["merged_into"] == ids["Payment gateway"] and r["status"] == "approved"
            # ... and onto a value that was itself waiting: approved with it
            r = c.post(f"/supagent/admin/api/facets/{ids['ledger app']}", json={
                "facet": "application", "value": "ledger", "status": "approved"}).get_json()
            assert r["merged_into"] == ids["Ledger"] and r["status"] == "approved"
            assert c.get("/supagent/admin/api/review").get_json()["counts"]["values"] == 0
        with app.app_context():
            gw = db.session.get(Facet, ids["Payment gateway"])
            assert set(gw.parents) == {ids["Payments"], ids["Orders"]}
            assert db.session.get(Facet, ids["PG"]) is None
            assert db.session.query(Tag).filter_by(facet_id=gw.id).count() == 1          # its item moved along
            led = db.session.get(Facet, ids["Ledger"])
            assert led.status == "approved" and led.parents == [ids["Payments"]]
            b = db.session.get(Facet, ids["billing svc"])
            assert b.synonyms == ["billing svc", "BIL"] and b.description == "Bills the customers."
    finally:
        with app.app_context():
            _clean()


def test_a_category_of_ones_own_is_renamed_and_removed_with_what_names_it(world, app):
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import sysmap
    from supagent.knowledge.facets import _approved, editable
    from supagent.knowledge.freshness import touch
    from supagent.models import Facet, KObject, Link, Tag

    api = "/supagent/admin/api/facets/categories"
    try:
        with _client(app, "admin") as c:
            r = c.post(api, json={"name": "Server", "fields": "^(host|node)$"}).get_json()
            server = next(x for x in r["categories"] if x["name"] == "server")
            assert server == {"name": "server", "builtin": False, "fields": "^(host|node)$", "about": "", "values": 0,
                              "tags": 0, "parts": 0, "interactions": 0}
            pay = c.post("/supagent/admin/api/facets", json={"facet": "application", "value": "Payments"}).get_json()
            s1 = c.post("/supagent/admin/api/facets", json={"facet": "server", "value": "srv-01", "parents": [pay["id"]]}).get_json()
            s2 = c.post("/supagent/admin/api/facets", json={"facet": "server", "value": "srv-02"}).get_json()
            agent = c.post("/supagent/admin/api/facets", json={"facet": "component", "value": "Agent", "parents": [s1["id"], pay["id"]]}).get_json()
            c.post("/supagent/dictionary/api/map", json={"interaction": {"a": pay["id"], "b": s1["id"], "kind": "runs_on"}})
            c.post("/supagent/dictionary/api/map", json={"layout": {"positions": {str(s1["id"]): [5, 6], str(pay["id"]): [1, 2]},
                                                                    "hidden": [s2["id"]], "folded": {"server": False}}})
        with app.app_context():
            jobs = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
            ref = f"object:{jobs.id}"
            db.session.add(Tag(ref=ref, facet_id=s1["id"], status="approved"))
            wait = db.session.get(Facet, agent["id"])
            wait.suggested = {"parents": [s2["id"]], "from": {str(s2["id"]): "field NODE of jobs"}}
            db.session.commit()
            touch()
            db.session.commit()
            assert set(_approved()[ref]) == {"server: srv-01", "application: Payments"}    # what the agent reads
        with _client(app, "admin") as c:
            server = next(x for x in c.get(api).get_json()["categories"] if x["name"] == "server")
            assert (server["values"], server["tags"], server["parts"], server["interactions"]) == (2, 1, 2, 1)
            # built in: neither renamed nor removed; a name that exists: refused
            assert c.post(api, json={"name": "component", "rename": "module"}).status_code == 400
            assert c.post(api, json={"name": "component", "remove": True}).status_code == 400
            assert c.post(api, json={"name": "server", "rename": "application"}).status_code == 400
            assert c.post(api, json={"name": "nothing", "remove": True}).status_code == 400
            # renamed: its values follow, its field names, its fold on the map
            r = c.post(api, json={"name": "server", "rename": "Host", "fields": "^(host|node)$"})
            assert r.status_code == 200 and [x["name"] for x in r.get_json()["categories"]][-1] == "host"
        with app.app_context():
            assert set(_approved()[ref]) == {"host: srv-01", "application: Payments"}      # under its new name
            assert editable()[-1] == "host" and "server" not in editable()
            assert settings.get("categories.fields")["host"] == "^(host|node)$" and "server" not in settings.get("categories.fields")
            assert {f.value for f in db.session.query(Facet).filter_by(facet="host")} == {"srv-01", "srv-02"}
            assert sysmap.layout()["folded"] == {"host": False}
        with _client(app, "alice") as c:
            assert c.post(api, json={"name": "host", "remove": True}).status_code == 403
        with _client(app, "admin") as c:
            r = c.post(api, json={"name": "host", "remove": True}).get_json()
            assert r["removed"] == {"values": 2, "tags": 1, "parts": 2, "interactions": 1}
            assert "host" not in [x["name"] for x in r["categories"]]
        with app.app_context():
            assert ref not in _approved()                        # the agent no longer reads the removed values
            assert editable() == ("subject", "application", "component")
            assert "host" not in (settings.get("categories.fields") or {})
            assert db.session.query(Facet).filter(Facet.facet.in_(("host", "server"))).count() == 0
            assert db.session.query(Tag).filter(Tag.facet_id.in_([s1["id"], s2["id"]])).count() == 0
            assert db.session.query(Link).filter(Link.b_ref == f"facet:{s1['id']}").count() == 0
            left = db.session.get(Facet, agent["id"])
            assert left.parents == [pay["id"]] and not left.suggested           # the server it was part of is gone
            lay = sysmap.layout()
            assert lay["positions"] == {str(pay["id"]): [1.0, 2.0]} and lay["hidden"] == [] and lay["folded"] == {}
    finally:
        with app.app_context():
            _clean()


def test_the_map_shows_an_admin_every_category_and_what_waits(world, app):
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Facet, KObject, Tag

    with app.app_context():
        settings.set_value("categories.custom", ["team"])
        pay = Facet(facet="application", value="Payments", status="approved", source="admin")
        db.session.add_all([pay, Facet(facet="component", value="Fraud scoring", status="proposed", source="llm")])
        db.session.flush()
        jobs = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
        db.session.add(Tag(ref=f"object:{jobs.id}", facet_id=pay.id, status="approved"))
        db.session.commit()
    try:
        with _client(app, "admin") as c:
            d = c.get("/supagent/dictionary/api/map").get_json()
            assert [(x["name"], x["count"]) for x in d["categories"]] == [("subject", 0), ("application", 1),
                                                                          ("component", 0), ("team", 0)]
            assert d["proposed"] == 1 and [v["value"] for v in d["values"]] == ["Payments"]    # drawn once approved
        with _client(app, "alice") as c:
            d = c.get("/supagent/dictionary/api/map").get_json()
            assert [(x["name"], x["count"]) for x in d["categories"]] == [("application", 1)] and d["proposed"] == 0
    finally:
        with app.app_context():
            _clean()


def test_a_memory_in_the_catalog_names_its_entry(world, app):
    from superset.extensions import db, security_manager

    from supagent.models import Entry, Memory

    with app.app_context():
        admin = security_manager.find_user(username="admin")
        moved = Memory(scope="team", user_id=admin.id, kind="rule", text="Test orders are never counted.", status="catalog")
        orphan = Memory(scope="team", user_id=admin.id, kind="fact", text="The batch ends before 06:00.", status="catalog")
        plain = Memory(scope="team", user_id=admin.id, kind="rule", text="Amounts are in euros.", status="active")
        db.session.add_all([moved, orphan, plain])
        db.session.flush()
        entry = Entry(title="Test orders are never counted", classification="rule", fmt="text",
                      content="Test orders are never counted.", origin=f"memory:{moved.id}")
        import datetime as dt

        deleted = Entry(title="The batch ends before 06:00", classification="guide", fmt="text", content="x",
                        origin=f"memory:{orphan.id}", deleted_at=dt.datetime.utcnow())
        db.session.add_all([entry, deleted])
        db.session.commit()
        ids = (moved.id, orphan.id, plain.id, entry.id)
    try:
        with _client(app, "admin") as c:
            rows = {m["id"]: m for m in c.get("/supagent/admin/api/memory").get_json()["memory"]}
            assert rows[ids[0]]["entry_id"] == ids[3]            # "Edit it in the catalog" opens it
            assert rows[ids[1]]["entry_id"] is None              # its entry was deleted: edited as a memory
            assert rows[ids[2]]["entry_id"] is None
    finally:
        with app.app_context():
            _clean()


def test_the_part_of_list_has_every_category_whatever_the_number_of_values(world, app):
    """A category read from the data with more than a thousand values came first in the list of values (by the
    categories' names), which stops at 1,000: the subjects were not in it, and an edit saved from that list took a
    value's subjects away. The brief list has every category, the wider ones first."""
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Facet

    with app.app_context():
        settings.set_value("categories.custom", ["server"])
        db.session.add(Facet(facet="subject", value="Orders", status="approved", source="admin"))
        db.session.bulk_insert_mappings(Facet, [{"facet": "server", "value": f"srv-{i:04d}", "status": "approved",
                                                 "source": "data"} for i in range(1100)])
        db.session.commit()
    try:
        with _client(app, "admin") as c:
            full = c.get("/supagent/admin/api/facets?status=approved").get_json()["facets"]
            assert len(full) == 1000 and not any(f["facet"] == "subject" for f in full)      # why the brief list
            d = c.get("/supagent/admin/api/facets?status=approved&brief=1").get_json()
            assert len(d["facets"]) == 1101 and not d["capped"]
            assert (d["facets"][0]["facet"], d["facets"][0]["value"]) == ("subject", "Orders")
            assert set(d["facets"][0]) == {"id", "facet", "value", "status"}
    finally:
        with app.app_context():
            _clean()
