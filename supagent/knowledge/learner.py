"""One learning run: every osagg and promagg database (or the ones of learn.databases) is read
as the learning user, what changed since the last run is recorded, the relations are measured
again, the catalog is applied, and the LLM describes what nobody described.

Runs every day at learn.hour (Celery beat, task supagent.learn_tick), from the admin page
("Learn now") or with `superset supagent learn`. A run stops after learn.max_minutes; the
next one starts with what was not learned (new objects first, then the oldest profiles)."""

from __future__ import annotations

import datetime as dt
import logging
import time
import traceback
from typing import Any

from superset import db

from supagent import settings
from supagent.models import Run

log = logging.getLogger(__name__)
BACKENDS = ("osagg", "promagg")


def learning_username() -> str:
    """learn.user, else the first active Admin."""
    from superset.extensions import security_manager

    name = settings.get("learn.user")
    if name:
        return name
    admin = security_manager.find_role("Admin")
    users = sorted((u for u in (admin.user if admin is not None else []) if getattr(u, "active", True)),
                   key=lambda u: u.id)
    if not users:
        raise RuntimeError("no active Admin user to learn as: set learn.user")
    return users[0].username


def databases_to_learn(only: list[str] | None = None) -> list[Any]:
    from superset.extensions import security_manager
    from superset.models.core import Database

    wanted = [str(x) for x in (only or settings.get("learn.databases") or [])]
    out = []
    for d in db.session.query(Database).order_by(Database.id):
        if d.backend not in BACKENDS:
            continue
        if wanted and str(d.id) not in wanted and d.database_name not in wanted:
            continue
        if security_manager.can_access_database(d):
            out.append(d)
    return out


SCHEDULED_TRIES = 3          # a scheduled run stopped by a restart or an error is tried again, 3 times a day


def running_run() -> Run | None:
    """A run started less than twice learn.max_minutes ago and not finished. An older one never
    finished (its process was stopped: a restart, a deployment) and is marked interrupted."""
    limit = dt.datetime.utcnow() - dt.timedelta(minutes=2 * int(settings.get("learn.max_minutes")) + 5)
    stale = db.session.query(Run).filter(Run.kind == "learn", Run.status == "running", Run.started_at <= limit).all()
    for r in stale:
        r.status, r.finished_at = "interrupted", dt.datetime.utcnow()
        r.error = r.error or "the process stopped before the end of the run (a restart?)"
    if stale:
        db.session.commit()
    return (db.session.query(Run).filter(Run.kind == "learn", Run.status == "running", Run.started_at > limit)
            .order_by(Run.id.desc()).first())


def run_learning(reason: str = "manual", databases: list[str] | None = None, llm: bool = True,
                 max_minutes: int | None = None) -> dict[str, Any]:
    """Learn now; returns the run's summary (also stored in supagent_run)."""
    from supagent.knowledge.curated import apply_catalog
    from supagent.knowledge.enrich import enrich, infer_categories
    from supagent.knowledge.learn_indices import learn_indices
    from supagent.knowledge.learn_metrics import learn_metrics
    from supagent.knowledge.relations import learn_relations
    from supagent.knowledge.store import source_for
    from supagent.security import acting_as

    busy = running_run()
    if busy is not None:
        return {"run": busy.id, "status": "skipped", "reason": f"run {busy.id} is still running"}
    run = Run(kind="learn", reason=reason, status="running", stats={})
    db.session.add(run)
    db.session.commit()
    run_id = run.id
    minutes = int(max_minutes or settings.get("learn.max_minutes"))
    deadline = time.time() + 60 * minutes
    stats: dict[str, Any] = {"databases": {}}
    status, error = "done", None
    t0 = time.time()
    try:
        username = learning_username()
        stats["user"] = username
        with acting_as(username):
            run = db.session.get(Run, run_id)
            targets = databases_to_learn(databases)
            if not targets:
                stats["note"] = "no osagg or promagg database to learn (or none this user may read)"
            for database in targets:
                source = source_for(database)
                db.session.commit()
                t1 = time.time()
                try:
                    if database.backend == "promagg":
                        res = learn_metrics(run, source, database, deadline)
                    else:
                        res = learn_indices(run, source, database, deadline)
                except Exception as ex:  # pylint: disable=broad-except
                    db.session.rollback()
                    log.exception("supagent learn: database %s", database.database_name)
                    res = {"error": f"{type(ex).__name__}: {str(ex)[:500]}", "complete": False}
                res["seconds"] = round(time.time() - t1, 1)
                source = db.session.merge(source)
                source.stats = res
                source.last_learned_at = dt.datetime.utcnow()
                db.session.commit()
                stats["databases"][database.database_name] = res
                if not res.get("complete", True) or res.get("error") or res.get("errors"):
                    status = "partial"
            stats["relations"] = learn_relations()
            stats["catalog"] = apply_catalog()
            stats["categories"] = infer_categories()
            if llm and settings.get("learn.llm_descriptions"):
                stats["llm"] = enrich(run, deadline + 600)          # descriptions may take ten more minutes
                if stats["llm"].get("error"):
                    status = "partial"
            from supagent.knowledge.autocatalog import run as agent_catalog
            from supagent.knowledge.index import index_knowledge

            stats["agent_catalog"] = agent_catalog(llm_docs=llm)   # entries the agent is certain of
            stats["index"] = index_knowledge()
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent learn: run %s failed", run_id)
        status, error = "error", "".join(traceback.format_exception_only(type(ex), ex))[-2000:]
    stats["seconds"] = round(time.time() - t0, 1)
    run = db.session.get(Run, run_id)
    run.status = status
    run.error = error
    run.stats = stats
    run.finished_at = dt.datetime.utcnow()
    db.session.commit()
    return {"run": run_id, "status": status, "error": error, **stats}


def due_today(now: dt.datetime | None = None) -> bool:
    """The daily run is due: enabled, the hour has come, no run finished today (a scheduled or
    a manual one) and none running; a scheduled run stopped by a restart or an error is tried
    again at the next tick, SCHEDULED_TRIES times a day at most."""
    if not settings.get("learn.enabled"):
        return False
    now = now or dt.datetime.now()
    days = [str(d).strip().lower()[:3] for d in settings.get("learn.days") or []]
    if days and now.strftime("%a").lower()[:3] not in days:
        return False
    if now.hour < int(settings.get("learn.hour")):
        return False
    if running_run() is not None:
        return False
    start_of_day_utc = dt.datetime.utcnow() - dt.timedelta(hours=now.hour, minutes=now.minute)
    today = db.session.query(Run).filter(Run.kind == "learn", Run.started_at >= start_of_day_utc).all()
    if any(r.status in ("done", "partial") for r in today):
        return False
    return sum(1 for r in today if r.reason == "schedule") < SCHEDULED_TRIES


def plan_learning(databases: list[str] | None = None) -> list[dict[str, Any]]:
    """What a run would do today, per database, without reading any data: objects listed (the
    list itself is one or two cheap requests), due today, estimated requests and minutes at
    learn.max_requests_per_minute (one request at a time)."""
    import math

    from supagent.knowledge.learn_indices import group_indices
    from supagent.knowledge.learn_metrics import HISTORY_REFRESH_DAYS, due
    from supagent.knowledge.store import matches, source_for
    from supagent.models import KObject
    from supagent.tools import _connection, _promagg_connection

    per_minute = max(1, int(settings.get("learn.max_requests_per_minute")))
    every = int(settings.get("learn.profile_every_days"))
    fields_per_request = max(5, int(settings.get("learn.fields_per_request")))
    today = dt.date.today()
    out = []
    for database in databases_to_learn(databases):
        source = source_for(database)
        known = {o.name: o for o in db.session.query(KObject).filter(
            KObject.source_id == source.id, KObject.kind.in_(("metric", "index")))}
        row: dict[str, Any] = {"database": database.database_name, "backend": database.backend}
        try:
            if database.backend == "promagg":
                conn = _promagg_connection(database)
                try:
                    names = [n for n in conn.list_tables()
                             if matches(n, settings.get("learn.metrics"), settings.get("learn.metrics_exclude"))]
                finally:
                    conn.close()
                new = [n for n in names if n not in known]
                again = [n for n in names if n in known and due(n, known[n].stats, every, today)]
                now_ms = time.time() * 1000
                # a known metric: count, statistics, series sample (3); the depth of its history once a
                # month (about 7 index lookups); 2 more when its data had stopped (nothing newer?)
                requests = 2 + 10 * len(new)
                for n in again:
                    st = known[n].stats or {}
                    checked = st.get("history_checked_on")
                    history = not checked or (today - dt.date.fromisoformat(checked)).days >= HISTORY_REFRESH_DAYS
                    stopped = not st.get("data_to_ms") or now_ms - float(st["data_to_ms"]) > 2 * 3_600_000
                    requests += 3 + (7 if history else 0) + (2 if stopped else 0)
                row.update(objects=len(names), new=len(new), due=len(new) + len(again))
            else:
                conn = _connection(database, extract=False)
                try:
                    names = [n for n in conn.list_tables() if not any(c in n for c in "*?[")
                             and matches(n, settings.get("learn.indices"), settings.get("learn.indices_exclude"))]
                finally:
                    conn.close()
                objects, families = group_indices(names) if settings.get("learn.group_rollover") else \
                    ({n: n for n in names}, {})
                due_names = [n for n in objects if n not in known or due(n, known[n].stats, every, today)]
                requests = 1
                for n in due_names:
                    fields = db.session.query(KObject.id).filter_by(source_id=source.id, kind="field", parent=n).count()
                    batches = math.ceil(max(fields, 40) / fields_per_request)
                    requests += 2 * batches + 2          # statistics, values of small keywords, documents, mapping
                row.update(objects=len(objects), indices=len(names), families=len(families),
                           new=len([n for n in objects if n not in known]), due=len(due_names))
            row.update(requests=requests, minutes=round(requests / per_minute, 1),
                       limit_minutes=int(settings.get("learn.max_minutes")))
        except Exception as ex:  # pylint: disable=broad-except
            row["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
        out.append(row)
    db.session.rollback()                               # a plan changes nothing
    return out
