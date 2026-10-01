"""The Data dictionary's review and fast saves: a save answers before the agent's search is updated (that runs in
the background of the process that saved, its state in the database for every server), the review lists what
waits for an admin with its real counts, every action takes an item out of it, and only admins reach these APIs."""

from __future__ import annotations

import pytest

from conftest import login

ENDPOINTS = [("GET", "/supagent/admin/api/review"), ("GET", "/supagent/admin/api/facets"),
             ("POST", "/supagent/admin/api/facets"),
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
def sure_used_at_once(monkeypatch):
    """categories.review_all off (on by default since 0.7): what the LLM is sure of is used at once, as in 0.6."""
    from supagent import settings

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: False if key == "categories.review_all" else real(key))



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


def test_every_action_takes_an_item_out_of_the_review(app, queue, sure_used_at_once):
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


def test_a_value_merged_into_another_moves_its_items(app, queue, sure_used_at_once):
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
        # into an approved value: the item the LLM was sure of (0.9) is used at once, so the value's count moves
        # at once; the unsure one (0.6) waits in the review
        statuses = sorted((t.confidence, t.status) for t in db.session.query(Tag).filter(Tag.facet_id == queue["near"]))
        assert statuses == [(0.6, "proposed"), (0.9, "approved")]
        listed = c.get("/supagent/admin/api/facets?facet=subject").get_json()["facets"]
        assert [(f["value"], f["status"], f["items"]) for f in listed] == [("Settlement", "approved", 1)]
        assert c.get("/supagent/admin/api/review").get_json()["counts"]["tags"] >= 1


def test_an_admin_adds_a_value_by_hand(app, queue):
    """A value added by hand is used at once; one that exists already in that category is said so, a retired
    one is used again; the aspect (functional, technical) takes none."""
    from superset.extensions import db

    from supagent.models import Facet

    with app.app_context():
        c = _client(app, "admin")
        r = c.post("/supagent/admin/api/facets", json={"facet": "application", "value": "  Risk   engine ",
                                                        "description": "the pricing service", "synonyms": "RE, pricer"})
        assert r.status_code == 200
        f = db.session.get(Facet, r.get_json()["id"])
        assert (f.facet, f.value, f.status, f.source, f.description, f.synonyms) == (
            "application", "Risk engine", "approved", "admin", "the pricing service", ["RE", "pricer"])
        dup = c.post("/supagent/admin/api/facets", json={"facet": "application", "value": "risk ENGINE"})
        assert dup.status_code == 409 and dup.get_json()["id"] == f.id
        assert c.post("/supagent/admin/api/facets", json={"facet": "aspect", "value": "legal"}).status_code == 400
        assert c.post("/supagent/admin/api/facets", json={"facet": "subject", "value": " "}).status_code == 400
        old = db.session.get(Facet, queue["new"])
        old.status = "rejected"
        db.session.commit()
        again = c.post("/supagent/admin/api/facets", json={"facet": "subject", "value": "settlements"})
        assert again.status_code == 200 and again.get_json()["id"] == queue["new"]
        db.session.refresh(old)
        assert (old.status, old.value) == ("approved", "settlements")


def test_a_value_moves_to_another_category(app, queue):
    """Editing a value changes its category too; where the other category has that value already, the two are
    one (the items together); the aspect stays fixed."""
    from superset.extensions import db

    from supagent.models import Facet, Tag

    with app.app_context():
        c = _client(app, "admin")
        r = c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"facet": "component", "value": "Settlements",
                                                                       "description": "the settlement batch"})
        assert r.get_json()["facet"] == "component"
        moved = db.session.get(Facet, queue["new"])
        db.session.refresh(moved)
        assert (moved.facet, moved.description) == ("component", "the settlement batch")
        assert db.session.query(Tag).filter(Tag.facet_id == queue["new"]).count() == 2     # its items with it
        # into a category that has the value already: merged there
        r = c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"facet": "application", "value": "ledger",
                                                                       "synonyms": "GL"})
        assert r.get_json()["merged_into"] == queue["app"]
        assert db.session.get(Facet, queue["new"]) is None
        ledger = db.session.get(Facet, queue["app"])
        db.session.refresh(ledger)
        assert set(ledger.synonyms) == {"GL", "Settlements"}
        refs = [t.ref for t in db.session.query(Tag).filter(Tag.facet_id == queue["app"])]
        assert sorted(refs) == sorted(set(refs)) and len(refs) == 2     # each item once (one had both values)
        # renamed in its own category to a value that exists: merged too (it was a unique-key error)
        r = c.post(f"/supagent/admin/api/facets/{queue['near']}", json={"value": "Settlement"})
        assert r.status_code == 200 and r.get_json()["value"] == "Settlement"
        aspect = Facet(facet="aspect", value="technical", status="approved", source="seed")
        db.session.add(aspect)
        db.session.commit()
        assert c.post(f"/supagent/admin/api/facets/{aspect.id}", json={"facet": "subject"}).status_code == 400
        assert c.post(f"/supagent/admin/api/facets/{queue['near']}", json={"facet": "aspect"}).status_code == 400


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


def test_a_value_is_part_of_several_others(app, queue):
    """A component of two applications (the user's example): its items count as about both; the list says what it
    is part of; values that cannot be parents (itself, the aspect, a retired one) are left out; a merge keeps it."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet, Tag

    with app.app_context():
        c = _client(app, "admin")
        ids = {}
        for facet, value in (("application", "LEDGER2"), ("application", "PAYMENTS2"), ("component", "posting engine")):
            ids[value] = c.post("/supagent/admin/api/facets", json={"facet": facet, "value": value}).get_json()["id"]
        aspect = Facet(facet="aspect", value="functional", status="approved", source="seed")
        db.session.add(aspect)
        db.session.commit()
        comp = ids["posting engine"]
        r = c.post(f"/supagent/admin/api/facets/{comp}",
                   json={"parents": [ids["LEDGER2"], ids["PAYMENTS2"], comp, aspect.id, 999999]})
        assert r.get_json()["parents"] == [ids["LEDGER2"], ids["PAYMENTS2"]]
        listed = {f["value"]: f for f in c.get("/supagent/admin/api/facets?facet=component").get_json()["facets"]}
        assert [p["value"] for p in listed["posting engine"]["parents"]] == ["LEDGER2", "PAYMENTS2"]
        db.session.add(Tag(ref="memory:424242", facet_id=comp, confidence=0.9, source="admin", status="approved"))
        db.session.commit()
        F._CACHE["stamp"] = None
        got = F.facets_of(["memory:424242"])["memory:424242"]
        assert {"component: posting engine", "application: LEDGER2", "application: PAYMENTS2"} <= set(got)
        # an application merged into another: what was part of it is part of the other
        r = c.post(f"/supagent/admin/api/facets/{ids['PAYMENTS2']}", json={"merge_into": ids["LEDGER2"]})
        assert r.status_code == 200
        moved = db.session.get(Facet, comp)
        db.session.refresh(moved)
        assert moved.parents == [ids["LEDGER2"]]
        db.session.query(Tag).filter(Tag.ref == "memory:424242").delete(synchronize_session=False)
        db.session.commit()


def test_the_review_shows_what_the_learning_says_values_are_part_of(app, queue):
    from superset.extensions import db

    from supagent.models import Facet

    with app.app_context():
        c = _client(app, "admin")
        ids = {v: c.post("/supagent/admin/api/facets", json={"facet": f, "value": v}).get_json()["id"]
               for f, v in (("application", "LEDGER3"), ("component", "engine3"))}
        engine = db.session.get(Facet, ids["engine3"])
        engine.suggested = {"parents": [ids["LEDGER3"]]}
        new = db.session.get(Facet, queue["new"])                       # proposed "Settlements"
        new.parents = [ids["LEDGER3"]]
        new.suggested = {"same_as": queue["near"]}
        db.session.commit()
        d = c.get("/supagent/admin/api/review").get_json()
        rel = next(x for x in d["relations"] if x["id"] == ids["engine3"])
        assert [p["value"] for p in rel["suggested"]] == ["LEDGER3"] and d["counts"]["relations"] >= 1
        val = next(x for x in d["values"] if x["id"] == queue["new"])
        assert val["same_as"]["value"] == "Settlement" and [p["value"] for p in val["parents"]] == ["LEDGER3"]
        assert c.post(f"/supagent/admin/api/facets/{ids['engine3']}", json={"accept_parents": True}).get_json()["parents"] \
            == [ids["LEDGER3"]]
        db.session.refresh(engine)
        assert engine.parents == [ids["LEDGER3"]] and not engine.suggested
        assert not [x for x in c.get("/supagent/admin/api/review").get_json()["relations"] if x["id"] == ids["engine3"]]
        m = c.get("/supagent/admin/api/facets/map").get_json()["values"]
        assert next(x for x in m if x["value"] == "engine3")["parents"] == [ids["LEDGER3"]]


def test_an_admin_adds_a_category_of_their_own(app, queue):
    from supagent import settings

    with app.app_context():
        c = _client(app, "admin")
        try:
            r = c.post("/supagent/admin/api/facets/categories", json={"name": "Server", "fields": "^(host|node)$"})
            cats = {x["name"]: x for x in r.get_json()["categories"]}
            assert cats["server"]["fields"] == "^(host|node)$" and not cats["server"]["builtin"]
            assert c.post("/supagent/admin/api/facets", json={"facet": "server", "value": "srv-9"}).status_code == 200
            assert c.post("/supagent/admin/api/facets/categories", json={"name": "x"}).status_code == 400
            assert c.post("/supagent/admin/api/facets/categories", json={"name": "env", "fields": "(("}).status_code == 400
        finally:
            settings.set_value("categories.custom", None)
            settings.set_value("categories.fields", None)
    alice = _client(app, "alice")                       # (outside the app context: her own g)
    assert alice.post("/supagent/admin/api/facets/categories", json={"name": "team"}).status_code in (401, 403)


def test_a_suggestion_is_changed_before_it_is_approved(app, queue):
    """The user: not only approve or reject: change. Part of only some of the values proposed (the others are not
    proposed again), and an item's category moved to another value."""
    from superset.extensions import db

    from supagent.models import Facet, Tag

    with app.app_context():
        c = _client(app, "admin")
        ids = {v: c.post("/supagent/admin/api/facets", json={"facet": f, "value": v}).get_json()["id"]
               for f, v in (("application", "APP-A4"), ("application", "APP-B4"), ("component", "engine4"))}
        engine = db.session.get(Facet, ids["engine4"])
        engine.suggested = {"parents": [ids["APP-A4"], ids["APP-B4"]]}
        db.session.commit()
        r = c.post(f"/supagent/admin/api/facets/{ids['engine4']}", json={"accept_parents": [ids["APP-B4"]]})
        assert r.get_json()["parents"] == [ids["APP-B4"]]
        db.session.refresh(engine)
        assert engine.suggested == {"declined": [ids["APP-A4"]]}
        tag = db.session.query(Tag).filter(Tag.facet_id == queue["app"]).first()     # an item tagged LEDGER
        r = c.post(f"/supagent/admin/api/tags/{tag.id}", json={"facet_id": ids["APP-B4"]})
        assert r.get_json()["facet_id"] == ids["APP-B4"] and r.get_json()["status"] == "approved"
        assert c.post(f"/supagent/admin/api/tags/{tag.id}", json={"facet_id": 987654}).status_code == 400


def test_review_all_keeps_everything_the_llm_finds_waiting(app, queue, monkeypatch):
    """categories.review_all: even the categories and links the LLM is sure of wait for an admin."""
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import facets as F
    from supagent.models import Facet, Tag

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: True if key == "categories.review_all" else real(key))
    with app.app_context():
        assert F.review_all()
        sure = db.session.query(Tag).filter(Tag.confidence >= 0.9, Tag.status == "proposed").first()
        F.settle()
        db.session.refresh(sure)
        assert sure.status == "proposed"                                   # settle() no longer approves it
        c = _client(app, "admin")
        c.post(f"/supagent/admin/api/facets/{queue['new']}", json={"status": "approved"})
        assert db.session.get(Facet, queue["new"]).status == "approved"
        db.session.refresh(sure)
        assert sure.status == "proposed"                                   # nor the approval of its value


def test_review_all_is_on_by_default(app):
    """Since 0.7 nothing the LLM finds is used before an admin approves it, unless categories.review_all is off."""
    from supagent import settings
    from supagent.knowledge import facets as F

    with app.app_context():
        assert settings.BY_KEY["categories.review_all"].default is True and F.review_all()
