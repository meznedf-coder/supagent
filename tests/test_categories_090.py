"""0.9: nothing the learning finds changes the categories before an admin approves it. The values it reads in the
data's category fields and the categories it gave to the data's objects wait in To review like the LLM's
proposals (a category's values found in the data are approved together in one click); what people wrote (a
category on a catalog entry or a document, in the catalog for a metric) is used at once, as before."""

from __future__ import annotations

from test_decider import lab  # noqa: F401
from test_dictionary_080 import _client
from test_facets import world  # noqa: F401  (the fixture: jobs index with APPLICATION values, metrics, an entry)


def test_what_the_learning_finds_waits_and_what_people_wrote_is_used(world, monkeypatch):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import catalog, facets as F
    from supagent.models import Facet, KObject

    o = db.session.query(KObject).filter(KObject.kind == "metric", KObject.gone_at.is_(None)).first()
    was = o.category
    o.category = "Queue depth"                                   # written by the LLM with the metric's description
    db.session.commit()
    monkeypatch.setattr(catalog, "load_catalog", lambda: {"metrics": {"tables": {"some_metric": {"category": "Capacity"}}}})
    kept = Facet(facet="application", value="LEGACY", status="approved", source="admin", description="by hand")
    db.session.add(kept)
    db.session.commit()
    try:
        n = F.seed()
        by = {(f.facet, f.value): f for f in db.session.query(Facet)}
        assert by[("subject", "Payments")].status == "approved"            # the category of a catalog entry
        assert by[("subject", "Capacity")].status == "approved"            # the catalog's category of a metric
        learned = by[("subject", "Queue depth")]
        assert (learned.status, learned.source) == ("proposed", "llm") and "the learning gave" in learned.origins[0]
        billing = by[("application", "BILLING")]
        assert (billing.status, billing.source) == ("proposed", "data") and billing.origins[0].startswith("field ")
        assert n["proposed"] >= 2
        assert (by[("application", "LEGACY")].status, by[("application", "LEGACY")].description) == ("approved", "by hand")
    finally:
        o.category = was
        db.session.commit()


def test_the_values_found_in_the_data_are_approved_together(world, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet

    F.seed()
    db.session.add(Facet(facet="application", value="Proposed by the LLM", status="proposed", source="llm"))
    db.session.commit()
    waiting = db.session.query(Facet).filter(Facet.status == "proposed", Facet.source == "data",
                                             Facet.facet == "application").count()
    assert waiting >= 1
    with _client(app, "admin") as c:
        d = c.get("/supagent/admin/api/review?limit=500").get_json()
        assert d["found"]["application"] == waiting
        card = next(v for v in d["values"] if v["value"] == "BILLING")
        assert card["source"] == "data" and card["origins"][0].startswith("field ")
        assert next(v for v in d["values"] if v["value"] == "Proposed by the LLM")["source"] == "llm"
        assert c.post("/supagent/admin/api/facets/approve-found", json={"facet": "nothing"}).status_code == 400
        r = c.post("/supagent/admin/api/facets/approve-found", json={"facet": "application"}).get_json()
        assert r == {"approved": waiting, "facet": "application"}
        d = c.get("/supagent/admin/api/review?limit=500").get_json()
        assert "application" not in d["found"]
    by = {f.value: f for f in db.session.query(Facet).filter(Facet.facet == "application")}
    assert by["BILLING"].status == "approved" and by["BILLING"].reviewed_by == "admin"
    assert by["Proposed by the LLM"].status == "proposed"                  # the LLM's own proposals: one by one
    with _client(app, "alice") as c:
        assert c.post("/supagent/admin/api/facets/approve-found", json={"facet": "application"}).status_code in (302, 401, 403)
