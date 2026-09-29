"""A learning run: every osagg and promagg database is learned (the ones left out are listed, with
the reason), each database's AI descriptions come right after it (with a fair share of the time),
the relations between them all at the end; an admin can stop a run, which keeps what it learned,
and start another one."""

from __future__ import annotations

import datetime as dt

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
    """The learning steps replaced by recorders (the order of a run is what is checked)."""
    from supagent.knowledge import (autocatalog, curated, enrich, generic, index, learn_indices, learn_metrics,
                                    quality, relations)

    def learn(run, source, database, deadline):
        calls.append(("learn", database.database_name))
        if database.database_name == stop_at:      # an admin presses Stop while this database is learned
            from supagent.knowledge.stopping import check, request_stop

            request_stop()
            check(force=True)                      # the next request to the database
        return {"complete": True}

    monkeypatch.setattr(learn_metrics, "learn_metrics", learn)
    monkeypatch.setattr(learn_indices, "learn_indices", learn)
    monkeypatch.setattr(enrich, "enrich", lambda run, deadline, llm=None, source_id=None:
                        calls.append(("describe", source_id)) or {"written": 1, "requests": 1})
    monkeypatch.setattr(enrich, "infer_categories", lambda: 0)
    monkeypatch.setattr(enrich, "left_to_describe", lambda source_id=None: 0)
    monkeypatch.setattr(relations, "learn_relations", lambda: calls.append(("relations",)) or {"same_values": 0})
    monkeypatch.setattr(curated, "apply_catalog", lambda: {"curated": 0})
    monkeypatch.setattr(autocatalog, "run", lambda llm_docs=True: {})
    monkeypatch.setattr(generic, "tidy_learned", lambda limit=50, deadline=None: {})
    monkeypatch.setattr(index, "index_knowledge", lambda: {})
    monkeypatch.setattr(index, "sync", lambda prefixes=None: {})
    monkeypatch.setattr(quality, "evaluate_resolver", lambda limit=100, seconds=60: {})


def test_each_database_is_described_right_after_it_and_the_relations_come_last(four, monkeypatch):
    from supagent.knowledge.learner import run_learning
    from supagent.knowledge.store import source_for
    from superset.extensions import db
    from superset.models.core import Database

    calls: list = []
    _fake_steps(monkeypatch, calls)
    out = run_learning(reason="test")
    names = _learnable()
    sources = {d.database_name: source_for(d).id for d in db.session.query(Database) if d.database_name in names}
    expected = []
    for name in names:
        expected += [("learn", name), ("describe", sources[name])]
    assert calls[:len(expected)] == expected and calls[len(expected)] == ("relations",)
    assert calls[len(expected) + 1:] == [("describe", None)]         # the time left: what a share left
    assert out["status"] == "done" and out["llm"]["written"] == len(names) + 1


def test_an_admin_stops_a_run_it_keeps_what_it_learned_and_another_can_start(four, monkeypatch):
    from supagent.knowledge.learner import run_learning, running_run
    from supagent.models import Run
    from superset.extensions import db

    names = _learnable()
    calls: list = []
    _fake_steps(monkeypatch, calls, stop_at=names[1])
    out = run_learning(reason="test")
    assert out["status"] == "stopped" and out["error"] == "stopped by an admin"
    assert calls == [("learn", names[0]), ("describe", calls[1][1]), ("learn", names[1])]   # no more after the stop
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
