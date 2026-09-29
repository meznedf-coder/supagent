"""Superset's Celery workers as supagent sees them, with any broker: Redis, RabbitMQ, or a queue
in Superset's own database (broker_url = "sqla+postgresql://...", no extra service).

Every worker writes a heartbeat row in supagent_meta (worker:<node name>) every HEARTBEAT_S
seconds, from a thread of its main process, and removes it when it stops. The web servers read
these rows (every CACHE_S seconds at most): in agent.executor = auto, a question goes to the
workers while one of them is alive and reads the answers' queue, else the web server answers it.
A database queue cannot carry Celery's ping (a broadcast); the heartbeat works with every broker.
A row counts only for the web servers that send to the same broker, and only if the worker reads
the answers' queue (a worker started with -Q other_queue does not answer).

The heartbeat opens and closes its own connection for each write (no pool): the worker's main
process forks its pool processes, and Superset closes the inherited pool in each of them.

purge() runs with the hourly tick: the delivered messages of a database queue older than
agent.queue_keep_days (Celery marks a delivered message and never deletes it: about 1 KB each,
1,500 a day with Superset's reports scheduler), and the heartbeats of workers gone for a day."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import socket
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

HEARTBEAT_S = 30.0          # a worker writes its heartbeat this often
ALIVE_S = 90.0              # a worker not seen for this long is gone (killed, host down)
GONE_S = 86400.0            # heartbeats older than this are removed by purge()
CACHE_S = 10.0              # the web servers read the heartbeats at most this often
PURGE_BATCH = 5000          # delivered messages deleted per statement
PREFIX = "worker:"
_APP: dict[str, Any] = {}
_BEAT: dict[str, Any] = {}
_SEEN: dict[str, Any] = {"at": 0.0, "rows": []}
_BROADCAST: dict[str, bool] = {}


def _meta() -> Any:
    import sqlalchemy as sa

    return sa.table("supagent_meta", sa.column("key"), sa.column("value"))


def _key(node: str) -> str:
    key = PREFIX + node
    if len(key) > 64:                                   # supagent_meta.key is 64 characters
        key = PREFIX + node[:44] + "-" + hashlib.sha1(node.encode("utf-8")).hexdigest()[:12]
    return key


def broker_id() -> str:
    """A short fingerprint of the Celery broker (its URL without the password)."""
    from superset.extensions import celery_app

    try:
        conn = celery_app.connection_for_write()
        uri = conn.as_uri()                                 # the password masked
        conn.release()
    except Exception:  # pylint: disable=broad-except
        uri = str(celery_app.conf.broker_url or "")
    return hashlib.sha1(uri.encode("utf-8", "replace")).hexdigest()[:12]


def answers_queue() -> str:
    """The Celery queue the questions are sent to."""
    from superset.extensions import celery_app

    from supagent import settings

    queue = (settings.get("agent.celery_queue") or "").strip()
    return queue or str(celery_app.conf.task_default_queue or "celery")


def can_broadcast() -> bool:
    """Whether the broker carries Celery's broadcasts (ping): Redis, RabbitMQ yes, a database no."""
    if "ok" not in _BROADCAST:
        from superset.extensions import celery_app

        try:
            conn = celery_app.connection_for_write()
            _BROADCAST["ok"] = "fanout" in conn.transport.implements.exchange_type
            conn.release()
        except Exception:  # pylint: disable=broad-except
            _BROADCAST["ok"] = False
    return _BROADCAST["ok"]


# --------------------------------------------------------------------------- #
# the worker side
# --------------------------------------------------------------------------- #
def register(app: Any) -> None:
    """Connect the worker signals (they fire only in a `celery worker` main process)."""
    from celery.signals import worker_ready, worker_shutdown, worker_shutting_down

    _APP["app"] = app
    worker_ready.connect(_on_worker_ready, weak=False, dispatch_uid="supagent-worker-ready")
    worker_shutting_down.connect(_on_shutting_down, weak=False, dispatch_uid="supagent-worker-shutting-down")
    worker_shutdown.connect(_on_shutdown, weak=False, dispatch_uid="supagent-worker-shutdown")


def _engine() -> Any:
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    config = _APP["app"].config
    args = (config.get("SQLALCHEMY_ENGINE_OPTIONS") or {}).get("connect_args") or {}
    return sa.create_engine(config["SQLALCHEMY_DATABASE_URI"], poolclass=NullPool, connect_args=args)


def _write(engine: Any, key: str, value: str) -> None:
    from sqlalchemy.exc import IntegrityError

    t = _meta()
    with engine.begin() as conn:
        if conn.execute(t.update().where(t.c.key == key).values(value=value)).rowcount:
            return
    try:
        with engine.begin() as conn:
            conn.execute(t.insert().values(key=key, value=value))
    except IntegrityError:                              # written meanwhile (a restart of the same node)
        with engine.begin() as conn:
            conn.execute(t.update().where(t.c.key == key).values(value=value))


def _delete(engine: Any, key: str) -> None:
    t = _meta()
    with engine.begin() as conn:
        conn.execute(t.delete().where(t.c.key == key))


def _loop(engine: Any, key: str, value: dict[str, Any], stop: threading.Event) -> None:
    failures = 0
    while True:
        try:
            _write(engine, key, json.dumps({**value, "at": round(time.time(), 1)}))
            failures = 0
        except Exception as ex:  # pylint: disable=broad-except   (the database away: next time)
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                log.warning("supagent: worker heartbeat not written (%s)", str(ex)[:300])
        if stop.wait(HEARTBEAT_S):
            break
    try:
        _delete(engine, key)                            # stopped: the web servers stop sending at once
    except Exception:  # pylint: disable=broad-except
        pass
    engine.dispose()


def _queues_of(consumer: Any) -> list[str]:
    try:
        return sorted({q.name for q in consumer.task_consumer.queues})
    except Exception:  # pylint: disable=broad-except
        try:
            return sorted(consumer.app.amqp.queues.consume_from)
        except Exception:  # pylint: disable=broad-except
            return []


def _on_worker_ready(sender: Any = None, **_kw: Any) -> None:
    if "app" not in _APP or _BEAT.get("thread") is not None:
        return
    from supagent import __version__

    node = str(getattr(sender, "hostname", "") or f"celery@{socket.gethostname()}")
    procs = getattr(getattr(sender, "controller", None), "concurrency", None)
    value = {"node": node, "broker": broker_id(), "queues": _queues_of(sender), "procs": procs,
             "pid": os.getpid(), "version": __version__}
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(_engine(), _key(node), value, stop),
                              name="supagent-heartbeat", daemon=True)
    _BEAT.update(stop=stop, thread=thread)
    thread.start()
    log.info("supagent: worker %s seen by the web servers (queues %s)", node, ", ".join(value["queues"]) or "?")


def _on_shutting_down(**_kw: Any) -> None:
    stop = _BEAT.get("stop")                            # inside a signal handler: only tell the thread
    if stop is not None:
        stop.set()


def _on_shutdown(**_kw: Any) -> None:
    _on_shutting_down()
    thread = _BEAT.get("thread")
    if thread is not None:
        thread.join(5.0)


# --------------------------------------------------------------------------- #
# the web side
# --------------------------------------------------------------------------- #
def _rows() -> list[dict[str, Any]]:
    import sqlalchemy as sa
    from superset import db

    t = _meta()
    try:
        with db.engine.connect() as conn:
            values = conn.execute(sa.select(t.c.value).where(t.c.key.like(PREFIX + "%"))).scalars().all()
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        return []
    out = []
    for v in values:
        try:
            row = json.loads(v or "")
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def live_workers(fresh: bool = False) -> list[dict[str, Any]]:
    """The workers alive that read the answers' queue on this web server's broker."""
    now = time.time()
    if fresh or now - _SEEN["at"] >= CACHE_S:
        _SEEN.update(at=now, rows=_rows())
    broker, queue = broker_id(), answers_queue()
    return [r for r in _SEEN["rows"]
            if now - float(r.get("at") or 0) <= ALIVE_S and r.get("broker") == broker
            and (not r.get("queues") or queue in r["queues"])]


# --------------------------------------------------------------------------- #
# the hourly clean-up
# --------------------------------------------------------------------------- #
def _purge_queue(days: int, now: dt.datetime | None = None) -> int:
    """Delivered messages of a database queue older than `days` (the Celery broker's tables)."""
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool
    from superset.extensions import celery_app

    url = celery_app.conf.broker_url
    if days <= 0 or not isinstance(url, str) or not url.startswith("sqla+"):
        return 0
    opts = celery_app.conf.broker_transport_options or {}
    t = sa.table(opts.get("message_tablename", "kombu_message"),
                 sa.column("id"), sa.column("visible"), sa.column("timestamp"))
    cutoff = (now or dt.datetime.now()) - dt.timedelta(days=days)     # the broker writes local times
    engine = sa.create_engine(url[len("sqla+"):], poolclass=NullPool)
    deleted = 0
    try:
        while True:
            with engine.begin() as conn:                # one short transaction per batch
                ids = conn.execute(sa.select(t.c.id).where(t.c.visible == sa.false(), t.c.timestamp < cutoff)
                                   .order_by(t.c.id).limit(PURGE_BATCH)).scalars().all()
                if ids:
                    conn.execute(t.delete().where(t.c.id.in_(ids)))
            deleted += len(ids)
            if len(ids) < PURGE_BATCH:
                break
    except Exception as ex:  # pylint: disable=broad-except   (no worker ever started: no table)
        log.info("supagent: queue clean-up skipped (%s)", str(ex)[:200])
    finally:
        engine.dispose()
    return deleted


def _purge_heartbeats() -> int:
    import sqlalchemy as sa
    from superset import db

    t = _meta()
    gone = []
    with db.engine.begin() as conn:
        for key, value in conn.execute(sa.select(t.c.key, t.c.value).where(t.c.key.like(PREFIX + "%"))).all():
            try:
                at = float(json.loads(value or "{}").get("at") or 0)
            except (ValueError, AttributeError):
                at = 0.0
            if time.time() - at > GONE_S:
                gone.append(key)
        if gone:
            conn.execute(t.delete().where(t.c.key.in_(gone)))
    return len(gone)


def purge(now: dt.datetime | None = None) -> dict[str, int]:
    from supagent import settings

    out = {"queue_messages": _purge_queue(int(settings.get("agent.queue_keep_days") or 0), now)}
    try:
        out["worker_heartbeats"] = _purge_heartbeats()
    except Exception:  # pylint: disable=broad-except
        out["worker_heartbeats"] = 0
    return out
