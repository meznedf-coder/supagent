"""Long lists in pages, like Browse: the query timings, the changes, the relations of the Data
dictionary, and the runs of the settings page (which also says whether a run is running, for
the Stop button, whatever the page shown)."""

from __future__ import annotations

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


def _get(app, user: str, path: str) -> dict:
    with app.app_context(), app.test_client() as c:
        login(c, user)
        return c.get(path).get_json()


def test_the_dictionary_lists_come_in_pages(world, app):
    from superset.extensions import db

    from supagent.models import Change, KObject, QueryStat, Relation, Run

    run = Run(kind="learn", reason="test", status="done")
    db.session.add(run)
    db.session.flush()
    stats_before = db.session.query(QueryStat).filter(QueryStat.database_id == world["jobs"].id).count()
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    rows = [Change(run_id=run.id, object_id=node.id, change="cardinality", detail={"n": i}) for i in range(60)]
    rows += [QueryStat(target=f"jobs{i}", database_id=world["jobs"].id, pattern=f"SELECT {i}", calls=1,
                       total_seconds=float(i), max_seconds=float(i)) for i in range(12)]
    from supagent.knowledge.store import upsert

    extra = [upsert(None, world["s_jobs"], "field", "jobs", f"EXTRA_{i}", {"data_type": "keyword", "stats": {}})
             for i in range(7)]
    db.session.flush()
    rows += [Relation(a_id=node.id, b_id=f.id, relation="same_values", origin="learned", confidence=0.5 + i / 100)
             for i, f in enumerate(extra)]
    db.session.add_all(rows)
    db.session.commit()
    try:
        first = _get(app, "alice", "/supagent/dictionary/api/changes?days=7&page=0&size=50")
        last = _get(app, "alice", "/supagent/dictionary/api/changes?days=7&page=1&size=50")
        assert (len(first["changes"]), first["total"]) == (50, 60) and len(last["changes"]) == 10
        assert first["changes"][0]["detail"] == {"n": 59}                  # the newest first
        t = _get(app, "alice", "/supagent/dictionary/api/timings?page=0&size=5")
        assert t["total"] == stats_before + 12 and len(t["timings"]) == 5
        assert [x["max_seconds"] for x in t["timings"]] == sorted((x["max_seconds"] for x in t["timings"]), reverse=True)
        last = (t["total"] - 1) // 5
        tail = _get(app, "alice", f"/supagent/dictionary/api/timings?page={last}&size=5")
        assert len(tail["timings"]) == t["total"] - 5 * last                  # the last page: what is left
        r = _get(app, "alice", "/supagent/dictionary/api/relations?page=1&size=5")
        assert r["total"] >= 7 and r["page"] == 1 and len(r["relations"]) == r["total"] - 5
    finally:
        for x in rows + extra:
            db.session.delete(db.session.merge(x))
        db.session.query(Change).filter(Change.run_id == run.id).delete()
        db.session.delete(db.session.merge(run))
        db.session.commit()


def test_the_runs_come_in_pages_and_say_what_is_running(app):
    from superset.extensions import db

    from supagent.models import Run

    with app.app_context():
        db.session.query(Run).delete()
        runs = [Run(kind="learn", reason="schedule", status="done") for _ in range(19)]
        runs.append(Run(kind="context", reason="manual", status="running"))      # the newest
        db.session.add_all(runs)
        db.session.commit()
        running_id = runs[-1].id
    try:
        page2 = _get(app, "admin", "/supagent/admin/api/runs?page=1&size=15")
        assert (len(page2["runs"]), page2["total"]) == (5, 20)
        assert page2["running"] == running_id                                 # not on this page, still known
        page1 = _get(app, "admin", "/supagent/admin/api/runs?page=0&size=15")
        assert page1["runs"][0]["kind"] == "context"
    finally:
        with app.app_context():
            db.session.query(Run).delete()
            db.session.commit()
