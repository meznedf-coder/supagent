"""The Data dictionary's review and fast saves: a save answers before the agent's search is updated (that runs in
the background of the process that saved, its state in the database for every server), the review lists what
waits for an admin with its real counts, every action takes an item out of it, and only admins reach these APIs."""

from __future__ import annotations

import pytest

from conftest import login

ENDPOINTS = [("GET", "/supagent/admin/api/review"), ("GET", "/supagent/admin/api/facets"),
             ("POST", "/supagent/admin/api/facets/1"), ("POST", "/supagent/admin/api/tags/1"),
             ("POST", "/supagent/admin/api/links/1"), ("POST", "/supagent/admin/api/routes/1"),
             ("GET", "/supagent/admin/api/apply")]


def _client(app, user):
    c = app.test_client()
    login(c, user)
    return c


def test_only_admins_reach_the_review(app):
    c = _client(app, "alice")                   # no app context held: each request has its own g (its own user)
    for method, url in ENDPOINTS:
        r = c.open(url, method=method, json={} if method == "POST" else None)
        assert r.status_code in (401, 403), (url, r.status_code)
    c = _client(app, "admin")
    for method, url in ENDPOINTS:
        r = c.open(url, method=method, json={} if method == "POST" else None)
        assert r.status_code in (200, 400, 404), (url, r.status_code)     # allowed (404: no such item)


@pytest.fixture()
def queue(ctx):
    from superset.extensions import db

    from supagent.models import Facet, Link, Memory, Route, Tag

    def clean():
        for model in (Tag, Link, Facet, Route):
            db.session.query(model).delete()
        db.session.query(Memory).filter(Memory.text.like("%(review test)%")).delete(synchronize_session=False)
        db.session.commit()

    clean()
    mems = [Memory(scope="team", kind="rule", text=f"Amounts are in EUR, rule {i} (review test)", status="proposed",
                   source="chat") for i in range(3)]
    new = Facet(facet="subject", value="Settlements", status="proposed", source="llm")
    near = Facet(facet="subject", value="Settlement", status="approved", source="seed")
    app_ = Facet(facet="application", value="LEDGER", status="approved", source="data")
    db.session.add_all(mems + [new, near, app_])
    db.session.flush()
    ref = f"memory:{mems[0].id}"
    tags = [Tag(ref=ref, facet_id=new.id, confidence=0.9, source="llm", status="proposed"),
            Tag(ref=f"memory:{mems[1].id}", facet_id=new.id, confidence=0.6, source="llm", status="proposed"),
            Tag(ref=ref, facet_id=app_.id, confidence=0.6, source="llm", status="proposed")]
    link = Link(a_ref=ref, b_ref="data:1:ledger", kind="about", confidence=0.8, source="llm", status="proposed")
    routes = [Route(question=q, terms=q, shown=[], chosen=[], used=[], moa=moa, moa_by="llm", moa_followed=True,
                    signal="helpful") for q, moa in (("why was the ledger late", "incident"),
                                                      ("how does the ledger work", "functional"),
                                                      ("ledger totals per desk", "functional"))]
    db.session.add_all(tags + [link] + routes)
    db.session.commit()
    yield {"memories": [m.id for m in mems], "new": new.id, "near": near.id, "app": app_.id,
           "tags": [t.id for t in tags], "link": link.id, "routes": [r.id for r in routes], "ref": ref}
    db.session.rollback()
    clean()


def test_the_review_lists_what_waits_with_real_counts_and_titles(app, queue):
    with app.app_context():
        c = _client(app, "admin")
        d = c.get("/supagent/admin/api/review?limit=1").get_json()
        assert d["counts"]["memory"] == 3 and len(d["memory"]) == 1          # the count is not the page's size
        assert d["counts"]["values"] == 1 and d["values"][0]["items"] == 2
        assert d["counts"]["tags"] == 1                                       # on approved values only
        assert d["tags"][0]["title"].startswith("Amounts are in EUR")         # the item, in words
        assert d["links"][0]["b_title"] == "ledger" and d["counts"]["routes"] == 3
        assert d["waiting"] == sum(d["counts"][k] for k in ("memory", "recipes", "values", "tags", "links"))


def test_every_action_takes_an_item_out_of_the_review(app, queue):
    from superset.extensions import db

    from supagent.models import Facet, Link, Route, Tag

    with app.app_context():
        c = _client(app, "admin")
        a, b, c3 = queue["memories"]
        assert c.post(f"/supagent/admin/api/memory/{a}", json={"status": "active"}).status_code == 200
        assert c.post(f"/supagent/admin/api/memory/{b}", json={"status": "disabled"}).status_code == 200
        # a proposed value approved: its confident items with it; the others stay to review
        assert c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"status": "approved"}).status_code == 200
        t_sure, t_unsure, t_app = (db.session.get(Tag, i) for i in queue["tags"])
        db.session.refresh(t_sure)
        db.session.refresh(t_unsure)
        assert (t_sure.status, t_unsure.status) == ("approved", "proposed")
        assert c.post(f"/supagent/admin/api/tags/{t_unsure.id}", json={"status": "rejected"}).status_code == 200
        assert c.post(f"/supagent/admin/api/tags/{t_app.id}", json={"status": "approved"}).status_code == 200
        assert c.post(f"/supagent/admin/api/links/{queue['link']}", json={"status": "approved"}).status_code == 200
        r_keep, r_change, r_remove = queue["routes"]
        assert c.post(f"/supagent/admin/api/routes/{r_keep}", json={"route": "incident"}).status_code == 200
        assert c.post(f"/supagent/admin/api/routes/{r_change}", json={"route": "technical"}).status_code == 200
        assert c.post(f"/supagent/admin/api/routes/{r_remove}", json={"remove": True}).status_code == 200
        assert c.post(f"/supagent/admin/api/routes/{r_remove}", json={"route": "nonsense"}).status_code == 400
        d = c.get("/supagent/admin/api/review").get_json()
        assert (d["counts"]["memory"], d["counts"]["values"], d["counts"]["tags"], d["counts"]["links"],
                d["counts"]["routes"]) == (1, 0, 0, 0, 0)
        assert [m["id"] for m in d["memory"]] == [c3]
        changed = db.session.get(Route, r_change)
        db.session.refresh(changed)
        assert (changed.moa, changed.moa_by, changed.signal) == ("technical", "admin", "helpful")
        kept = db.session.get(Route, r_keep)
        db.session.refresh(kept)
        assert (kept.moa, kept.moa_by, kept.signal) == ("incident", "admin", "helpful")   # an admin's example now
        assert db.session.get(Link, queue["link"]).status == "approved"


def test_a_value_merged_into_another_moves_its_items(app, queue):
    from superset.extensions import db

    from supagent.models import Facet, Tag

    with app.app_context():
        c = _client(app, "admin")
        bad = c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"merge_into": queue["app"]})
        assert bad.status_code == 400                                         # another category: refused
        r = c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"merge_into": queue["near"]})
        assert r.get_json() == {"merged_into": queue["near"]}
        assert db.session.get(Facet, queue["new"]) is None
        near = db.session.get(Facet, queue["near"])
        db.session.refresh(near)
        assert near.synonyms == ["Settlements"]
        assert db.session.query(Tag).filter(Tag.facet_id == queue["near"]).count() == 2
        listed = c.get("/supagent/admin/api/facets?facet=subject").get_json()["facets"]
        assert [(f["value"], f["status"]) for f in listed] == [("Settlement", "approved")]


def test_a_save_answers_at_once_and_the_search_follows_in_the_background(app, monkeypatch):
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import apply
    from supagent.models import Chunk, Memory

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: True if key == "knowledge.apply_background" else real(key))
    ran = []
    real_run = apply.run
    monkeypatch.setattr(apply, "run", lambda jobs: ran.append(jobs) or real_run(jobs))
    with app.app_context():
        c = _client(app, "admin")
        try:
            r = c.post("/supagent/api/memory", json={"text": "Desk books close at 18:00 (background test)",
                                                          "scope": "team", "kind": "fact"})
            assert r.status_code == 200
            c.post("/supagent/api/memory", json={"text": "Desk limits reset monthly (background test)",
                                                      "scope": "team", "kind": "fact"})
            t = apply._WORKER["thread"]
            assert t is not None
            t.join(timeout=30)
            assert not t.is_alive()
            assert len(ran) == 1 and ran[0]["prefixes"] == {"memory:"}        # two saves in a row: one run
            texts = [x.text for x in db.session.query(Chunk).filter(Chunk.ref.like("memory:%"))]
            assert any("Desk books close at 18:00" in x for x in texts)
            assert any("Desk limits reset monthly" in x for x in texts)
            st = c.get("/supagent/admin/api/apply").get_json()
            assert st["pending"] is False and st["done_at"] and not st["error"] and not st.get("stale")
        finally:
            t = apply._WORKER["thread"]
            if t is not None:
                t.join(timeout=30)
            db.session.query(Memory).filter(Memory.text.like("%(background test)%")).delete(synchronize_session=False)
            db.session.commit()
            real_run({"prefixes": {"memory:"}})


def test_a_pending_state_nobody_works_on_is_shown_stale(ctx):
    import datetime as dt
    import json

    from superset.extensions import db

    from supagent.knowledge import apply
    from supagent.models import Meta

    old = (dt.datetime.utcnow() - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
    row = db.session.get(Meta, apply.KEY)
    if row is None:
        row = Meta(key=apply.KEY)
        db.session.add(row)
    row.value = json.dumps({"pending": True, "queued_at": old})
    db.session.commit()
    assert apply.status()["stale"] is True
    row.value = json.dumps({"pending": False, "done_at": old})
    db.session.commit()
    assert "stale" not in apply.status()
