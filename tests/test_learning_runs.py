"""A learning run: every osagg and promagg database is learned (the ones left out are listed, with
the reason), each database's AI descriptions come right after it (with a fair share of the time),
the relations between them all at the end; an admin can stop a run, which keeps what it learned,
and start another one."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import login
from test_knowledge import _database, world  # noqa: F401  (the fixture)


def _no_runs() -> None:
    from superset.extensions import db

    from supagent.models import Change, Run

    db.session.query(Change).delete()
    db.session.query(Run).delete()
    db.session.commit()


@pytest.fixture()
def four(world):
    """Two more OpenSearch databases next to the world's: jobs, metrics, jobs-eu, logs; no run."""
    _database("jobs-eu", "osagg://127.0.0.1:9200/?timezone=Europe/Paris&tables=jobs-eu-*")
    _database("logs", "osagg://127.0.0.1:9200/?timezone=Europe/Paris&tables=logs-*")
    _no_runs()
    return world


def _learnable():
    from superset.extensions import db
    from superset.models.core import Database

    return [d.database_name for d in db.session.query(Database).order_by(Database.id) if d.backend in ("osagg", "promagg")]


def test_every_osagg_and_promagg_database_is_learned_and_the_others_say_why(four):
    from supagent import settings
    from supagent.knowledge.learner import databases_to_learn
    from supagent.security import acting_as

    with acting_as("admin"):
        assert [d.database_name for d in databases_to_learn()] == _learnable()   # all of them, several osagg
    settings.set_value("learn.databases", ["jobs", "metrics", "gone-db"])
    try:
        why: dict[str, str] = {}
        with acting_as("admin"):
            names = [d.database_name for d in databases_to_learn(why=why)]
        assert names == ["jobs", "metrics"]
        assert why["jobs-eu"].startswith("not in learn.databases") and why["logs"].startswith("not in learn.databases")
        assert "no osagg or promagg database has this name" in why["gone-db"]
    finally:
        settings.set_value("learn.databases", [])
    why = {}
    with acting_as("alice"):                        # a learning user who may read the jobs database only
        names = [d.database_name for d in databases_to_learn(why=why)]
    assert "jobs" in names and "metrics" not in names and "alice (learn.user) may not read it" in why["metrics"]


def _fake_steps(monkeypatch, calls: list, stop_at: str | None = None):
    """The learning steps replaced by recorders (the order of a run is what is checked); the
    describer thread's calls are marked "during"."""
    import threading

    from supagent.knowledge import (autocatalog, curated, describer, enrich, generic, index, learn_indices,
                                    learn_metrics, quality, relations)

    lock = threading.Lock()

    def note(*item):
        with lock:
            calls.append(item)

    def learn(run, source, database, deadline):
        note("learn", database.database_name)
        if database.database_name == stop_at:      # an admin presses Stop while this database is learned
            from supagent.knowledge.stopping import check, request_stop

            request_stop()
            check(force=True)                      # the next request to the database
        return {"complete": True}

    def describe(run, deadline, llm=None, source_id=None, pause=None):
        during = threading.current_thread().name.startswith("supagent-describe")
        note("describe", "during" if during else "end")
        return {"written": 1, "requests": 1, "by_source": {}} if not during else {"written": 0, "requests": 0}

    monkeypatch.setattr(describer, "IDLE_S", 0.01)
    monkeypatch.setattr(learn_metrics, "learn_metrics", learn)
    monkeypatch.setattr(learn_indices, "learn_indices", learn)
    monkeypatch.setattr(enrich, "enrich", describe)
    monkeypatch.setattr(enrich, "infer_categories", lambda: 0)
    monkeypatch.setattr(enrich, "left_to_describe", lambda source_id=None: 0)
    monkeypatch.setattr(relations, "learn_relations", lambda: note("relations") or {"same_values": 0})
    monkeypatch.setattr(curated, "apply_catalog", lambda: {"curated": 0})
    monkeypatch.setattr(autocatalog, "run", lambda llm_docs=True: {})
    monkeypatch.setattr(generic, "tidy_learned", lambda limit=50, deadline=None: {})
    monkeypatch.setattr(index, "index_knowledge", lambda: {})
    monkeypatch.setattr(index, "sync", lambda prefixes=None: {})
    monkeypatch.setattr(quality, "evaluate_resolver", lambda limit=100, seconds=60: {})


def _in_learning_order():
    """The databases never learned first, then the others (by id in each group)."""
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.models import Source

    learned = {i for (i,) in db.session.query(Source.database_id).filter(Source.last_learned_at.isnot(None))}
    dbs = [d for d in db.session.query(Database).order_by(Database.id) if d.backend in ("osagg", "promagg")]
    return [d.database_name for d in sorted(dbs, key=lambda d: (d.id in learned, d.id))]


def test_descriptions_run_during_the_learning_and_relations_come_last(four, monkeypatch):
    from supagent.knowledge.learner import run_learning

    calls: list = []
    _fake_steps(monkeypatch, calls)
    names = _in_learning_order()
    out = run_learning(reason="test")
    learned = [c for c in calls if c[0] == "learn"]
    assert learned == [("learn", n) for n in names]                       # every database, the new ones first
    last = max(i for i, c in enumerate(calls) if c[0] == "learn")
    rel = calls.index(("relations",))
    assert rel > last and ("describe", "during") in calls[:rel]           # the thread worked during the reading
    assert all(c != ("describe", "during") for c in calls[rel:])          # and was over before the relations
    assert calls[rel + 1:] == [("describe", "end")]                       # then what is still missing
    assert out["status"] == "done" and out["llm"]["written"] == 1


def test_descriptions_are_written_while_a_database_is_still_being_read(four, monkeypatch):
    """The real describe step next to a slow database: its first metrics are described (by a
    fake LLM) before it is fully read."""
    import json
    import threading
    import time as _time

    from superset.extensions import db

    from supagent import llm as L
    from supagent.knowledge import (autocatalog, describer, generic, index, learn_indices, learn_metrics, quality,
                                    relations)
    from supagent.knowledge.learner import run_learning
    from supagent.knowledge.store import upsert
    from supagent.models import KObject

    events: list = []

    class FakeLLM:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, tools=None, max_tokens=None):
            items = json.loads(messages[1]["content"])
            events.append(("llm", threading.current_thread().name, _time.time(), [i["name"] for i in items]))
            return {"content": json.dumps([{"id": i["id"], "description": f"About {i['name']}"} for i in items])}

    def slow_metrics(run, source, database, deadline):                   # 3 batches, a pause after each
        for b in range(3):
            for k in range(4):
                upsert(run, source, "metric", "", f"slow_{b}_{k}", {"metric_type": "gauge", "stats": {"series": 1}})
            db.session.commit()
            events.append(("batch", b, _time.time()))
            _time.sleep(0.6)
        events.append(("read", database.database_name, _time.time()))
        return {"complete": True}

    monkeypatch.setattr(L, "LLM", FakeLLM)
    monkeypatch.setattr(describer, "IDLE_S", 0.05)
    monkeypatch.setattr(learn_metrics, "learn_metrics", slow_metrics)
    monkeypatch.setattr(learn_indices, "learn_indices", lambda run, source, database, deadline: {"complete": True})
    monkeypatch.setattr(relations, "learn_relations", lambda: events.append(("relations", _time.time())) or {})
    monkeypatch.setattr(autocatalog, "run", lambda llm_docs=True: {})
    monkeypatch.setattr(generic, "tidy_learned", lambda limit=50, deadline=None: {})
    monkeypatch.setattr(index, "index_knowledge", lambda: {})
    monkeypatch.setattr(quality, "evaluate_resolver", lambda limit=100, seconds=60: {})
    out = run_learning(reason="test")
    read_at = next(e[2] for e in events if e[0] == "read")
    during = [e for e in events if e[0] == "llm" and e[1].startswith("supagent-describe")]
    assert during and during[0][2] < read_at                              # described before the database was read
    assert any(n.startswith("slow_0_") for e in during for n in e[3])      # its first metrics among them
    rel_at = next(e[1] for e in events if e[0] == "relations")
    assert all(e[2] < rel_at for e in during)
    left = db.session.query(KObject).filter(KObject.name.like("slow_%"), KObject.description.is_(None)).count()
    assert out["status"] == "done" and left == 0 and out["llm"]["written"] >= 12


def test_an_admin_stops_a_run_it_keeps_what_it_learned_and_another_can_start(four, monkeypatch):
    from supagent.knowledge.learner import run_learning, running_run
    from supagent.models import Run
    from superset.extensions import db

    names = _in_learning_order()
    calls: list = []
    _fake_steps(monkeypatch, calls, stop_at=names[1])
    out = run_learning(reason="test")
    assert out["status"] == "stopped" and out["error"] == "stopped by an admin"
    assert [c for c in calls if c[0] != "describe"] == [("learn", names[0]), ("learn", names[1])]   # nothing after
    assert names[0] in out["databases"] and ("relations",) not in calls
    run = db.session.get(Run, out["run"])
    assert run.status == "stopped" and run.finished_at is not None
    assert running_run() is None
    calls.clear()
    _fake_steps(monkeypatch, calls)
    assert run_learning(reason="test")["status"] == "done"          # a new run right after


def test_a_stopping_run_whose_process_died_does_not_block_the_next_one(app):
    from superset.extensions import db

    from supagent.knowledge.learner import running_run
    from supagent.knowledge.stopping import ASKED
    from supagent.models import Run

    with app.app_context():
        _no_runs()
        now = dt.datetime.utcnow()
        fresh = Run(kind="learn", reason="test", status="stopping", started_at=now,
                    error=f"{ASKED}{now:%Y-%m-%d %H:%M:%S} UTC")
        old = Run(kind="learn", reason="test", status="stopping", started_at=now - dt.timedelta(minutes=10),
                  error=f"{ASKED}{now - dt.timedelta(minutes=5):%Y-%m-%d %H:%M:%S} UTC")
        db.session.add_all([fresh, old])
        db.session.commit()
        assert running_run().id == fresh.id                          # it ends at its next step: a moment
        assert db.session.get(Run, old.id).status == "stopped"       # asked long ago: its process died
        db.session.get(Run, fresh.id).status = "stopped"
        db.session.commit()
        assert running_run() is None


def test_every_request_to_a_database_checks_the_stop(app):
    from superset.extensions import db

    from supagent.knowledge.stopping import LearningStopped, request_stop, watching
    from supagent.knowledge.throttle import Throttle
    from supagent.models import Run

    with app.app_context():
        _no_runs()
        run = Run(kind="learn", reason="test", status="running")
        db.session.add(run)
        db.session.commit()
        throttle = Throttle(6000, 5, "db")
        sent = []
        with watching(run.id):
            throttle.call(sent.append, 1)
            assert request_stop() == run.id
            with pytest.raises(LearningStopped):
                for _ in range(1000):                               # checked every few seconds at most
                    throttle.call(sent.append, 2)
                    __import__("supagent.knowledge.stopping", fromlist=["x"])._LOCAL.control.at = 0
        assert sent[0] == 1 and len(sent) <= 2
        assert not isinstance(LearningStopped(), Exception)           # never taken for a failed request
        run.status = "stopped"
        db.session.commit()


def test_the_stop_button_and_command(app):
    from superset.extensions import db

    from supagent.cli import supagent
    from supagent.models import Run

    with app.app_context():
        _no_runs()
    with app.test_client() as c:
        login(c, "admin")
        assert c.post("/supagent/admin/api/learn/stop", json={}).status_code == 409   # nothing runs
        with app.app_context():
            run = Run(kind="learn", reason="test", status="running")
            db.session.add(run)
            db.session.commit()
            run_id = run.id
        assert c.post("/supagent/admin/api/learn/stop", json={}).get_json() == {"stopping": run_id}
        busy = c.post("/supagent/admin/api/learn", json={})
        assert busy.status_code == 409 and "is stopping" in busy.get_json()["error"]
    with app.test_client() as c:
        login(c, "alice")                                            # not an admin
        assert c.post("/supagent/admin/api/learn/stop", json={}).status_code in (401, 403)
    with app.app_context():
        db.session.get(Run, run_id).status = "stopped"
        db.session.commit()
        run = Run(kind="learn", reason="test", status="running")
        db.session.add(run)
        db.session.commit()
        out = app.test_cli_runner().invoke(supagent, ["learn", "--stop"])
        assert out.exit_code == 0 and f"stopping learning run {run.id}" in out.output
        db.session.get(Run, run.id).status = "stopped"
        db.session.commit()


def test_a_run_in_progress_shows_what_it_did_so_far(four, app):
    from superset.extensions import db

    from supagent.knowledge.store import upsert
    from supagent.models import KObject, Run

    run = Run(kind="learn", reason="test", status="running", started_at=dt.datetime.utcnow() - dt.timedelta(seconds=5))
    db.session.add(run)
    db.session.commit()
    for k in range(3):
        upsert(run, four["s_prom"], "metric", "", f"progress_{k}", {"metric_type": "gauge", "stats": {"series": 1}})
    db.session.flush()
    obj = db.session.query(KObject).filter_by(name="progress_0").one()
    obj.description, obj.description_source = "About it", "llm"
    db.session.commit()
    with app.test_client() as c:
        login(c, "admin")
        runs = c.get("/supagent/admin/api/runs").get_json()["runs"]
    mine = next(r for r in runs if r["id"] == run.id)
    assert mine["progress"]["objects"] >= 3 and mine["progress"]["ai_descriptions"] >= 1
    run.status = "done"
    db.session.commit()


def _learned_before(names_learned: set[str]) -> None:
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.knowledge.store import source_for

    for d in db.session.query(Database).filter(Database.database_name.in_(["jobs", "metrics", "jobs-eu", "logs"])):
        source_for(d).last_learned_at = dt.datetime.utcnow() if d.database_name in names_learned else None
    db.session.commit()


def test_a_new_database_comes_first_and_each_gets_a_fair_share_of_the_time(four, monkeypatch):
    """A database of 5,000 metrics must not keep the others waiting: each one gets the time left
    divided by the databases left (a quick one leaves its time to the next)."""
    import time as _time

    from supagent.knowledge import learn_indices, learn_metrics
    from supagent.knowledge.learner import plan_learning, run_learning

    calls: list = []
    _fake_steps(monkeypatch, calls)
    _learned_before({"jobs", "metrics"})
    shares: dict[str, float] = {}

    def learn(run, source, database, deadline):
        shares[database.database_name] = deadline - _time.time()
        return {"complete": True}

    monkeypatch.setattr(learn_metrics, "learn_metrics", learn)
    monkeypatch.setattr(learn_indices, "learn_indices", learn)
    monkeypatch.setattr(learn_metrics, "_promagg_connection", lambda d: pytest.fail("no data read"), raising=False)
    out = run_learning(reason="test", max_minutes=40)
    order = list(shares)
    assert order[:2] == ["jobs-eu", "logs"] and set(order[2:]) == {"jobs", "metrics"}   # never learned: first
    assert [round(shares[n] / 60) for n in order] == [10, 13, 20, 40]     # 40/4, then what is left / 3, / 2, / 1
    assert out["plan"] == order and "now" not in out


def test_the_page_shows_the_database_being_learned_and_the_next_ones(four, monkeypatch):
    import sqlalchemy as sa
    from superset.extensions import db

    from supagent.knowledge import learn_indices, learn_metrics
    from supagent.knowledge.learner import FINISHING, run_learning

    calls: list = []
    _fake_steps(monkeypatch, calls)
    _learned_before(set())
    seen: list[dict] = []

    def read_stats(run_id):
        with db.engine.connect() as conn:                     # what the settings page reads meanwhile
            value = conn.execute(sa.text("SELECT stats FROM supagent_run WHERE id = :i"), {"i": run_id}).scalar()
        return json.loads(value) if isinstance(value, str) else value

    def learn(run, source, database, deadline):
        seen.append(read_stats(run.id))
        return {"complete": True, "fields": 3}

    monkeypatch.setattr(learn_metrics, "learn_metrics", learn)
    monkeypatch.setattr(learn_indices, "learn_indices", learn)
    from supagent.knowledge import relations

    monkeypatch.setattr(relations, "learn_relations", lambda: seen.append(read_stats(_last_run_id())) or {})
    out = run_learning(reason="test")
    plan = out["plan"]
    assert [s["now"] for s in seen[:4]] == plan and all(s["plan"] == plan for s in seen)
    assert list(seen[1]["databases"]) == plan[:1] and list(seen[3]["databases"]) == plan[:3]   # the ones done
    assert seen[4]["now"] == FINISHING and len(seen[4]["databases"]) == 4
    final = read_stats(out["run"])
    assert "now" not in final and final["plan"] == plan


def _last_run_id():
    from superset.extensions import db

    from supagent.models import Run

    return db.session.query(Run.id).order_by(Run.id.desc()).limit(1).scalar()


def test_only_one_run_starts_at_a_time_for_all_the_processes(app):
    from superset.extensions import db

    from supagent.knowledge.learner import _start_run, run_learning
    from supagent.models import Run

    with app.app_context():
        _no_runs()
        first = _start_run("schedule")
        assert first and _start_run("schedule") is None          # another worker, the same minute: no second run
        out = run_learning(reason="manual")
        assert out["status"] == "skipped" and out["run"] == first
        db.session.query(Run).filter(Run.id == first).update({"status": "done"})
        db.session.commit()
        second = _start_run("manual")
        assert second and second != first
        _no_runs()
