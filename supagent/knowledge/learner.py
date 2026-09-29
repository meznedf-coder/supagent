"""One learning run: every osagg and promagg database (or the ones of learn.databases) is read
as the learning user, one after the other, and what changed since the last run is recorded. While
they are read, the LLM describes what nobody described yet (supagent.knowledge.describer: after
the catalog, as the objects are learned); once every database is read, the relations between them
all are measured, the catalog is applied, and the time left describes what is still missing. The
databases left out, and why, are in the run's statistics. An admin can stop a run (the Stop
button): it ends as "stopped", keeping what it learned.

Runs every day at learn.hour (Celery beat, task supagent.learn_tick), from the admin page
("Learn now") or with `superset supagent learn`. A run stops after learn.max_minutes; the
next one starts with what was not learned (new objects first, then the oldest profiles).

The databases never learned come first (one just added is learned the same day), and each
database gets a fair share of the time left (the time left divided by the databases left), so
that a very big one (thousands of metrics) cannot keep the others waiting for days: it goes on
at the next run. The settings page shows, while the run goes, the database being learned, the
ones still to come, and the ones left out with the reason.

One run at a time for all the web servers and workers: starting a run holds a row lock
(supagent_meta 'learn:start') while it checks that none is running."""

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
START_LOCK = "learn:start"
FINISHING = "relations, catalog and AI descriptions"


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


def databases_to_learn(only: list[str] | None = None, why: dict[str, str] | None = None) -> list[Any]:
    """The osagg and promagg databases to learn: all of them, or those of learn.databases (or of
    `only`), that the current (learning) user may read. `why` receives the ones left out, with
    the reason, and the names of learn.databases that match no such database."""
    from flask import g
    from superset.extensions import security_manager
    from superset.models.core import Database

    wanted = [str(x) for x in (only or settings.get("learn.databases") or [])]
    source = "the databases asked for" if only else "learn.databases"
    out, matched = [], set()
    for d in db.session.query(Database).order_by(Database.id):
        if d.backend not in BACKENDS:
            continue
        if wanted and str(d.id) not in wanted and d.database_name not in wanted:
            if why is not None:
                why[d.database_name] = f"not in {source} ({', '.join(wanted)})"
            continue
        matched |= {str(d.id), d.database_name}
        if security_manager.can_access_database(d):
            out.append(d)
        elif why is not None:
            user = getattr(getattr(g, "user", None), "username", None) or "the learning user"
            why[d.database_name] = f"{user} (learn.user) may not read it: give that user access to it"
    if why is not None:
        for w in wanted:
            if w not in matched:
                why[w] = f"named in {source}, but no osagg or promagg database has this name or id"
    return out


def in_order(targets: list[Any]) -> list[Any]:
    """The databases never learned first (by id), then the others (by id)."""
    from supagent.models import Source

    learned = {i for (i,) in db.session.query(Source.database_id).filter(Source.last_learned_at.isnot(None))}
    return sorted(targets, key=lambda d: (d.id in learned, d.id))


def _publish(run_id: int, stats: dict[str, Any]) -> None:
    """What the run did so far, for the settings page (a run can take hours)."""
    import copy

    try:
        (db.session.query(Run).filter(Run.id == run_id)
         .update({"stats": copy.deepcopy(stats)}, synchronize_session=False))
        db.session.commit()
    except Exception:  # pylint: disable=broad-except   (the page shows it at the end)
        db.session.rollback()


SCHEDULED_TRIES = 3          # a scheduled run stopped by a restart or an error is tried again, 3 times a day


def running_run() -> Run | None:
    """A run started less than twice learn.max_minutes ago and not finished, or asked to stop less
    than STOP_GRACE seconds ago (it ends at its next step). An older one never finished (its
    process was stopped: a restart, a deployment) and is marked interrupted; one asked to stop
    longer ago is marked stopped."""
    from supagent.knowledge.stopping import STOP_GRACE, asked_at

    now = dt.datetime.utcnow()
    limit = now - dt.timedelta(minutes=2 * int(settings.get("learn.max_minutes")) + 5)
    changed = False
    for r in db.session.query(Run).filter(Run.kind == "learn", Run.status == "running", Run.started_at <= limit):
        r.status, r.finished_at = "interrupted", now
        r.error = r.error or "the process stopped before the end of the run (a restart?)"
        changed = True
    stopping = None
    for r in db.session.query(Run).filter(Run.kind == "learn", Run.status == "stopping").order_by(Run.id.desc()):
        asked = asked_at(r)
        if asked is not None and (now - asked).total_seconds() < STOP_GRACE:
            stopping = stopping or r
            continue
        r.status, r.finished_at = "stopped", r.finished_at or now
        r.error = "stopped by an admin"
        changed = True
    if changed:
        db.session.commit()
    running = (db.session.query(Run).filter(Run.kind == "learn", Run.status == "running", Run.started_at > limit)
               .order_by(Run.id.desc()).first())
    return running or stopping


def _start_run(reason: str) -> int | None:
    """Create the run, unless another process (a worker, a web server, cron) started one
    meanwhile: the check and the insert hold a row lock, so only one of them starts it
    (PostgreSQL, MySQL; SQLite writes one at a time anyway). Called after running_run(), whose
    clean-up commits (a commit would end the lock)."""
    from sqlalchemy.exc import IntegrityError

    from supagent.models import Meta

    if db.session.get(Meta, START_LOCK) is None:
        try:
            db.session.add(Meta(key=START_LOCK, value=""))
            db.session.commit()
        except IntegrityError:                         # created by another process meanwhile
            db.session.rollback()
    db.session.query(Meta).filter(Meta.key == START_LOCK).with_for_update().one()
    limit = dt.datetime.utcnow() - dt.timedelta(minutes=2 * int(settings.get("learn.max_minutes")) + 5)
    busy = (db.session.query(Run.id).filter(Run.kind == "learn", Run.status.in_(("running", "stopping")),
                                            Run.started_at > limit).first())
    if busy is not None:
        db.session.rollback()                          # the lock ends
        return None
    run = Run(kind="learn", reason=reason, status="running", stats={})
    db.session.add(run)
    db.session.commit()                                # the lock ends
    return run.id


def run_learning(reason: str = "manual", databases: list[str] | None = None, llm: bool = True,
                 max_minutes: int | None = None) -> dict[str, Any]:
    """Learn now; returns the run's summary (also stored in supagent_run). Its LLM calls wait
    while answers are being computed (supagent.priority)."""
    from supagent.llm import background

    with background():
        return _run_learning(reason, databases, llm, max_minutes)


def _run_learning(reason: str, databases: list[str] | None, llm: bool, max_minutes: int | None) -> dict[str, Any]:
    from supagent.knowledge.curated import apply_catalog
    from supagent.knowledge.enrich import enrich, infer_categories
    from supagent.knowledge.learn_indices import learn_indices
    from supagent.knowledge.learn_metrics import learn_metrics
    from supagent.knowledge.relations import learn_relations
    from supagent.knowledge.store import source_for
    from supagent.security import acting_as

    from supagent.knowledge.index import sync
    from supagent.knowledge.stopping import LearningStopped, check, watching

    busy = running_run()                          # marks the runs whose process died first
    run_id = _start_run(reason) if busy is None else None
    if run_id is None:
        busy = busy or running_run()
        return {"run": busy.id if busy else None, "status": "skipped",
                "reason": f"run {busy.id} is still {busy.status}" if busy else "another run started at the same time"}
    minutes = int(max_minutes or settings.get("learn.max_minutes"))
    deadline = time.time() + 60 * minutes
    llm_deadline = deadline + 600                 # descriptions may take ten more minutes
    describe = llm and settings.get("learn.llm_descriptions")
    stats: dict[str, Any] = {"databases": {}}
    status, error = "done", None
    t0 = time.time()

    def index_now() -> None:                      # the search finds what was learned now, not at the end
        try:
            sync(("object:",))
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()

    describer = None
    if describe:                                  # the descriptions while the databases are read
        from flask import current_app

        from supagent.knowledge.describer import Describer

        describer = Describer(current_app._get_current_object(), run_id, deadline)
        describer.start()

    def descriptions_so_far() -> dict[str, Any]:
        if describer is None:
            return {}
        return describer.finish(timeout=float(settings.get("llm.timeout") or 900) + 60)

    try:
        with watching(run_id):
            username = learning_username()
            stats["user"] = username
            with acting_as(username):
                run = db.session.get(Run, run_id)
                skipped: dict[str, str] = {}
                targets = in_order(databases_to_learn(databases, why=skipped))
                if skipped:
                    stats["skipped"] = skipped
                if not targets:
                    stats["note"] = "no osagg or promagg database to learn (or none this user may read)"
                stats["plan"] = [d.database_name for d in targets]
                for i, database in enumerate(targets):
                    check(force=True)
                    stats["now"] = database.database_name
                    _publish(run_id, stats)                    # the page: this one now, the next ones
                    source = source_for(database)
                    db.session.commit()
                    t1 = time.time()
                    share = t1 + max(0.0, deadline - t1) / (len(targets) - i)   # a fair share of the time left
                    try:
                        if database.backend == "promagg":
                            res = learn_metrics(run, source, database, share)
                        else:
                            res = learn_indices(run, source, database, share)
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
                    index_now()
                stats["now"] = FINISHING
                _publish(run_id, stats)
                during = descriptions_so_far()                  # every database read: the thread ends
                check(force=True)
                stats["relations"] = learn_relations()          # between all the databases: at the end
                stats["catalog"] = apply_catalog()
                stats["categories"] = infer_categories()
                if describe:
                    llm_out = {"written": during.get("written", 0), "requests": during.get("requests", 0)}
                    by_source = dict(during.get("by_source") or {})
                    if during.get("error"):
                        llm_out["error"] = during["error"]
                    if time.time() < llm_deadline and not during.get("still_running") and not during.get("error"):
                        more = enrich(run, llm_deadline)        # what is still missing, with the time left
                        llm_out["written"] += more.get("written", 0)
                        llm_out["requests"] += more.get("requests", 0)
                        llm_out.update({k: more[k] for k in ("left", "stopped", "error") if k in more})
                        for sid, n in (more.get("by_source") or {}).items():
                            by_source[sid] = by_source.get(sid, 0) + n
                    else:
                        from supagent.knowledge.enrich import left_to_describe

                        llm_out["left"] = left_to_describe()
                    for d in targets:                           # each database's share, for the runs list
                        x = stats["databases"].get(d.database_name)
                        if x is not None:
                            x["llm"] = {"written": by_source.get(source_for(d).id, 0)}
                    stats["llm"] = llm_out
                    if llm_out.get("error"):
                        status = "partial"
                from supagent.knowledge.autocatalog import run as agent_catalog
                from supagent.knowledge.index import index_knowledge

                check(force=True)
                stats["agent_catalog"] = agent_catalog(llm_docs=llm)   # entries the agent is certain of
                if llm:
                    from supagent.knowledge.generic import tidy_learned

                    try:
                        stats["generic_questions"] = tidy_learned(limit=50, deadline=llm_deadline)   # older ones
                    except Exception as ex:  # pylint: disable=broad-except
                        db.session.rollback()
                        stats["generic_questions"] = {"error": str(ex)[:300]}
                check(force=True)
                stats["index"] = index_knowledge()
                try:                                  # does "Where the data is" find the Helpful answers' data?
                    from supagent.knowledge.quality import evaluate_resolver

                    stats["resolver"] = evaluate_resolver(limit=100, seconds=60)
                except Exception as ex:  # pylint: disable=broad-except
                    db.session.rollback()
                    stats["resolver"] = {"error": str(ex)[:300]}
    except LearningStopped:
        db.session.rollback()
        status, error = "stopped", "stopped by an admin"
        index_now()                               # what was learned before the stop is searchable
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent learn: run %s failed", run_id)
        status, error = "error", "".join(traceback.format_exception_only(type(ex), ex))[-2000:]
    finally:
        if describer is not None and describer.is_alive():
            describer.finish(timeout=60)          # a stop or a failure: the thread ends too
    if describer is not None and "llm" not in stats and describer.out.get("written"):
        stats["llm"] = {"written": describer.out["written"], "requests": describer.out["requests"]}
    stats["seconds"] = round(time.time() - t0, 1)
    stats.pop("now", None)
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
    from supagent.knowledge.learn_metrics import BATCH, HISTORY_REFRESH_DAYS, SMALL_SERIES, due
    from supagent.knowledge.store import matches, source_for
    from supagent.models import KObject
    from supagent.tools import _connection, _promagg_connection

    per_minute = max(1, int(settings.get("learn.max_requests_per_minute")))
    every = int(settings.get("learn.profile_every_days"))
    fields_per_request = max(5, int(settings.get("learn.fields_per_request")))
    today = dt.date.today()
    out = []
    skipped: dict[str, str] = {}
    targets = in_order(databases_to_learn(databases, why=skipped))
    out += [{"database": name, "skipped": reason} for name, reason in skipped.items()]
    for database in targets:
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
                # the list and the metadata (2); the live series counts of BATCH metrics in one request;
                # per metric the statistics (1) and the labels (one request of its own, or a share of one
                # for the metrics with a few series); 2 more when its data had stopped. The depth of the
                # history comes after, with the time left (8 index lookups per BATCH live metrics).
                requests = 2 + math.ceil((len(new) + len(again)) / BATCH) + 1.5 * len(new)
                history_metrics = len(new)
                for n in again:
                    st = known[n].stats or {}
                    checked = st.get("history_checked_on")
                    if not checked or (today - dt.date.fromisoformat(checked)).days >= HISTORY_REFRESH_DAYS:
                        history_metrics += 1
                    stopped = not st.get("data_to_ms") or now_ms - float(st["data_to_ms"]) > 2 * 3_600_000
                    requests += 1 + (0.05 if 0 < (st.get("series") or 0) <= SMALL_SERIES else 1) + (2 if stopped else 0)
                requests = math.ceil(requests)
                row.update(objects=len(names), new=len(new), due=len(new) + len(again),
                           history_requests=8 * math.ceil(history_metrics / BATCH))
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
