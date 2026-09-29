"""Many workers and web servers: a worker is seen through its heartbeat with any broker (also a
queue in Superset's own database, which cannot carry Celery's ping), a question is answered once
whatever the number of processes, the web server answers a question no worker started in time,
delivered queue messages are removed after agent.queue_keep_days, and a file of an earlier answer
is found on any host."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import types

import pytest

from conftest import login
from test_stop import FakeAgent, _running


def _wait(cond, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _status(message_id: int) -> str:
    from superset.extensions import db

    from supagent.models import Message

    s = db.session.query(Message.status).filter(Message.id == message_id).scalar()
    db.session.commit()
    return s


def _heartbeats() -> dict:
    import sqlalchemy as sa
    from superset.extensions import db

    with db.engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT key, value FROM supagent_meta WHERE key LIKE 'worker:%'")).all()
    return {k: json.loads(v) for k, v in rows}


def _put_heartbeat(node: str, **values) -> None:
    from superset.extensions import db

    from supagent import workers
    from supagent.models import Meta

    row = {"node": node, "at": time.time(), "broker": workers.broker_id(), "queues": ["celery"], **values}
    db.session.merge(Meta(key=workers._key(node), value=json.dumps(row)))
    db.session.commit()
    workers._SEEN["at"] = 0.0                       # read again


@pytest.fixture()
def no_workers(app):
    from superset.extensions import db

    from supagent import workers
    from supagent.models import Meta

    def clear():
        with app.app_context():
            db.session.query(Meta).filter(Meta.key.like("worker:%")).delete(synchronize_session=False)
            db.session.commit()
        workers._SEEN["at"] = 0.0

    clear()
    yield
    clear()


def _consumer(node: str = "celery@host-a", queues=("celery",), procs: int = 4):
    return types.SimpleNamespace(hostname=node, controller=types.SimpleNamespace(concurrency=procs),
                                 task_consumer=types.SimpleNamespace(queues=[types.SimpleNamespace(name=q) for q in queues]))


def test_a_worker_is_seen_while_it_runs_and_gone_as_soon_as_it_stops(app, no_workers, monkeypatch):
    """The database queue carries no ping: the heartbeat says a worker is there."""
    from supagent import tasks, workers

    monkeypatch.setattr(workers, "_BEAT", {})
    with app.app_context():
        assert not workers.can_broadcast()          # the tests' broker: Superset's default, a database queue
        assert tasks.workers_alive() is False
        workers._on_worker_ready(sender=_consumer())
        try:
            assert _wait(lambda: "worker:celery@host-a" in _heartbeats())
            row = _heartbeats()["worker:celery@host-a"]
            assert (row["queues"], row["procs"], row["broker"]) == (["celery"], 4, workers.broker_id())
            workers._SEEN["at"] = 0.0
            assert [w["node"] for w in workers.live_workers()] == ["celery@host-a"]
            assert tasks.workers_alive() is True
        finally:
            workers._on_shutdown()                  # a stop (Ctrl+C, systemctl stop): the row goes at once
        assert "worker:celery@host-a" not in _heartbeats()
        assert workers.live_workers(fresh=True) == []


def test_only_live_workers_of_this_broker_reading_the_answers_queue_count(app, no_workers):
    from supagent import settings, workers

    with app.app_context():
        _put_heartbeat("celery@old", at=time.time() - workers.ALIVE_S - 5)       # killed: no news since
        _put_heartbeat("celery@redis", broker="0123456789ab")                       # another broker
        _put_heartbeat("celery@reports", queues=["reports"])                        # -Q reports only
        assert workers.live_workers(fresh=True) == []
        _put_heartbeat("celery@host-b")
        assert [w["node"] for w in workers.live_workers(fresh=True)] == ["celery@host-b"]
        settings.set_value("agent.celery_queue", "reports")                         # the answers' queue changes
        try:
            assert [w["node"] for w in workers.live_workers(fresh=True)] == ["celery@reports"]
        finally:
            settings.set_value("agent.celery_queue", "")
        long_name = "celery@" + "x" * 80                                          # supagent_meta keys: 64 characters
        assert len(workers._key(long_name)) <= 64 and workers._key(long_name) != workers._key(long_name + "y")


def test_the_web_server_answers_a_question_no_worker_started_in_time(app, no_workers, monkeypatch):
    """auto, a worker alive but busy (or stopped since its heartbeat): the web server that received
    the question answers it after TAKEOVER_S; when a worker gets it afterwards, it leaves it."""
    from supagent import runner, tasks
    from supagent.knowledge import experience, generic

    real_task = tasks.answer_task
    sent: list = []
    monkeypatch.setattr(tasks, "answer_task", types.SimpleNamespace(
        apply_async=lambda args, **kw: sent.append(args) or types.SimpleNamespace(id="task-1")))
    monkeypatch.setattr(tasks, "workers_alive", lambda: True)
    monkeypatch.setattr(tasks, "TAKEOVER_S", 0.2)
    monkeypatch.setattr(generic, "generalize", lambda *a, **k: {})
    monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: None)
    with app.app_context():
        _cid, mid = _running()
        monkeypatch.setattr("supagent.agent.Agent", type("Agent", (FakeAgent,), {"message_id": mid, "stop": False}))
        assert tasks.dispatch_answer(mid) == "celery" and sent == [[mid]]
        assert _wait(lambda: _status(mid) == "done")
        from superset.extensions import db

        from supagent.models import Message

        m = db.session.get(Message, mid)
        assert (m.content, m.task_id) == ("7 jobs failed.", "task-1")
        assert real_task.run(mid) is None and runner.run_answer(mid) is False    # the worker, later: nothing
        db.session.expire_all()
        assert db.session.get(Message, mid).content == "7 jobs failed."


def test_a_question_a_worker_started_is_not_taken_over(app, no_workers, monkeypatch):
    from supagent import runner, tasks

    started: list = []
    monkeypatch.setattr(tasks, "answer_task", types.SimpleNamespace(
        apply_async=lambda args, **kw: types.SimpleNamespace(id="task-2")))
    monkeypatch.setattr(tasks, "workers_alive", lambda: True)
    monkeypatch.setattr(tasks, "TAKEOVER_S", 0.2)
    monkeypatch.setattr(runner, "run_answer", lambda mid, taking_over=False: started.append(taking_over) or False)
    with app.app_context():
        _cid, mid = _running()
        tasks.dispatch_answer(mid)
        assert _wait(lambda: started == [True], 5)           # the web server only tries: the claim decides
    from supagent import runner as real_runner

    monkeypatch.undo()
    with app.app_context():
        from superset.extensions import db

        from supagent.models import Message

        db.session.query(Message).filter(Message.id == mid).update({"status": "running"})   # a worker has it
        db.session.commit()
        assert real_runner.run_answer(mid, taking_over=True) is False
        assert _status(mid) == "running"


def test_many_processes_starting_the_same_question_answer_it_once(app, monkeypatch):
    from supagent import runner
    from supagent.knowledge import experience, generic

    class Slow(FakeAgent):
        stop = False
        calls = 0

        def ask(self, question, history=None):
            type(self).calls += 1
            time.sleep(0.3)
            return super().ask(question, history)

    monkeypatch.setattr(generic, "generalize", lambda *a, **k: {})
    monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: None)
    with app.app_context():
        _cid, mid = _running()
    agent = type("Agent", (Slow,), {"message_id": mid})
    monkeypatch.setattr("supagent.agent.Agent", agent)
    results: list = []
    gate = threading.Barrier(4)

    def process():
        with app.app_context():
            gate.wait()
            results.append(runner.run_answer(mid))

    threads = [threading.Thread(target=process) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == [False, False, False, True] and agent.calls == 1
    with app.app_context():
        assert _status(mid) == "done"


def test_without_a_worker_auto_answers_in_the_web_server_at_once(app, no_workers, monkeypatch):
    from supagent import tasks

    ran: list = []
    monkeypatch.setattr(tasks, "answer_task", types.SimpleNamespace(
        apply_async=lambda *a, **k: pytest.fail("no worker: nothing is sent to Celery")))
    monkeypatch.setattr(tasks, "_in_thread", lambda fn, *args, **kw: ran.append((fn.__name__, args, kw)))
    with app.app_context():
        _cid, mid = _running()
        assert tasks.dispatch_answer(mid) == "thread" and ran == [("run_answer", (mid,), {})]


def _broker_db(tmp_path):
    import sqlalchemy as sa

    url = f"sqlite:///{tmp_path}/broker.db"
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    table = sa.Table("kombu_message", meta, sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("visible", sa.Boolean), sa.Column("timestamp", sa.DateTime),
                     sa.Column("payload", sa.Text), sa.Column("version", sa.SmallInteger), sa.Column("queue_id", sa.Integer))
    meta.create_all(engine)
    return url, engine, table


def test_delivered_queue_messages_are_kept_queue_keep_days(app, monkeypatch, tmp_path):
    import sqlalchemy as sa
    from superset.extensions import celery_app

    from supagent import settings, workers

    url, engine, km = _broker_db(tmp_path)
    now = dt.datetime.now()
    with engine.begin() as conn:
        conn.execute(km.insert(), [
            {"id": 1, "visible": False, "timestamp": now - dt.timedelta(days=100), "payload": "{}", "version": 1, "queue_id": 1},
            {"id": 2, "visible": False, "timestamp": now - dt.timedelta(days=10), "payload": "{}", "version": 1, "queue_id": 1},
            {"id": 3, "visible": True, "timestamp": None, "payload": "{}", "version": 1, "queue_id": 1},   # waiting
            {"id": 4, "visible": False, "timestamp": now - dt.timedelta(days=91), "payload": "{}", "version": 1, "queue_id": 1},
        ])
    monkeypatch.setattr(celery_app.conf, "broker_url", "sqla+" + url)
    monkeypatch.setattr(workers, "PURGE_BATCH", 1)                  # several statements
    with app.app_context():
        assert settings.get("agent.queue_keep_days") == 90            # three months by default
        settings.set_value("agent.queue_keep_days", 0)
        try:
            assert workers.purge()["queue_messages"] == 0             # 0: never
        finally:
            settings.set_value("agent.queue_keep_days", 90)
        assert workers.purge()["queue_messages"] == 2
    with engine.connect() as conn:
        assert sorted(conn.execute(sa.select(km.c.id)).scalars()) == [2, 3]


def test_the_queue_clean_up_leaves_redis_alone_and_old_heartbeats_go(app, no_workers, monkeypatch):
    from superset.extensions import celery_app

    from supagent import workers

    monkeypatch.setattr(celery_app.conf, "broker_url", "redis://127.0.0.1:1/0")
    with app.app_context():
        _put_heartbeat("celery@gone", at=time.time() - 2 * 86400)
        _put_heartbeat("celery@here")
        assert workers.purge() == {"queue_messages": 0, "worker_heartbeats": 1}
        assert list(_heartbeats()) == ["worker:celery@here"]


def test_the_hourly_tick_cleans_the_queue(app, monkeypatch):
    from supagent import runner, tasks, workers
    from supagent.knowledge import learner

    done: list = []
    monkeypatch.setattr(workers, "purge", lambda: done.append("queue") or {})
    monkeypatch.setattr(runner, "purge_old_files", lambda: 0)
    monkeypatch.setattr(runner, "purge_old_chats", lambda: 0)
    monkeypatch.setattr(learner, "due_today", lambda: False)
    monkeypatch.setattr("supagent.knowledge.docs.refresh_due", lambda: 0)
    monkeypatch.setattr("supagent.knowledge.index.index_knowledge", lambda: {})
    with app.app_context():
        tasks.learn_tick.run()
    assert done == ["queue"]


def test_the_settings_page_counts_the_workers(app, no_workers):
    with app.app_context():
        _put_heartbeat("celery@host-a")
        _put_heartbeat("celery@host-b")
    with app.test_client() as c:
        login(c, "admin")
        s = c.get("/supagent/admin/api/status").get_json()
    assert (s["celery_workers"], s["workers"], s["executor"]) == (True, 2, "auto")


def test_a_file_of_an_earlier_answer_is_found_on_another_worker(app, monkeypatch, tmp_path):
    """An Excel file made by an answer on worker A, mailed by the next answer on worker B."""
    import os

    from superset.extensions import db

    from supagent import tools
    from supagent.models import File
    from supagent.security import acting_as

    monkeypatch.setattr(tools, "_export_dir", lambda: str(tmp_path / "exports"))   # nothing on this host
    with app.app_context():
        _cid, mid = _running("alice")
        db.session.add(File(message_id=mid, name="failed_jobs-a1b2c3.xlsx", mime="application/vnd.ms-excel",
                            size=4, data=b"PK\x03\x04"))
        db.session.commit()
        with acting_as("bob"):                                 # not bob's answer
            with pytest.raises(tools.ToolError):
                tools._export_file("failed_jobs-a1b2c3.xlsx", (".xlsx",))
        with acting_as("alice"):
            path = tools._export_file("failed_jobs-a1b2c3.xlsx", (".xlsx",))
            with open(path, "rb") as fh:
                assert fh.read() == b"PK\x03\x04"
            os.remove(path)
            assert tools._export_file("a1b2c3", (".xlsx",)).endswith("failed_jobs-a1b2c3.xlsx")   # by its id
            with pytest.raises(tools.ToolError):
                tools._export_file("a1b2c3", (".png",))            # not a picture
