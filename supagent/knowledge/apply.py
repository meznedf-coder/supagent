"""Saving knowledge fast (0.6). A save answers as soon as the item is in the database; what follows (the catalog
applied to the dictionary and taken back where it no longer says something, the searchable pieces written again,
their vectors, the knowledge store) runs in the background of the process that saved, merged when saves come in a
row. Its state is in Superset's database (every web server shows it): pending, done at, the last error. A process
that stops meanwhile loses nothing for long: the hourly indexing does the same work.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

KEY = "knowledge_apply"
_LOCK = threading.Lock()
_JOBS: dict[str, Any] = {"catalog": None, "prefixes": set(), "objects": set()}
_WORKER: dict[str, Any] = {"thread": None}
DELAY = 0.3                 # seconds a worker waits for the saves that follow (merged into one run)


def later(kind: str, before: dict | None = None, prefix: str | None = None, ids: list[int] | None = None) -> None:
    """Queue the work that follows a save: kind catalog (with the catalog's texts before it), chunks (a kind of
    refs: memory:, doc:, context:, entry:), objects (dictionary objects whose pieces change)."""
    from flask import current_app

    with _LOCK:
        if kind == "catalog":
            if _JOBS["catalog"] is None:              # the oldest "before": what the catalog said before this run
                _JOBS["catalog"] = before or {}
        elif kind == "chunks" and prefix:
            _JOBS["prefixes"].add(prefix)
        elif kind == "objects" and ids:
            _JOBS["objects"].update(ids)
        _state(pending=True, queued_at=_now())
        t = _WORKER["thread"]
        if t is None or not t.is_alive():
            app = current_app._get_current_object()       # type: ignore[attr-defined]
            t = threading.Thread(target=_work, args=(app,), name="supagent-apply", daemon=True)
            _WORKER["thread"] = t
            t.start()


def _take() -> dict[str, Any] | None:
    with _LOCK:
        if _JOBS["catalog"] is None and not _JOBS["prefixes"] and not _JOBS["objects"]:
            _WORKER["thread"] = None
            return None
        jobs = {"catalog": _JOBS["catalog"], "prefixes": set(_JOBS["prefixes"]), "objects": set(_JOBS["objects"])}
        _JOBS.update(catalog=None, prefixes=set(), objects=set())
        return jobs


def _work(app: Any) -> None:
    with app.app_context():
        from superset import db

        while True:
            time.sleep(DELAY)
            jobs = _take()
            if jobs is None:
                return
            t0 = time.time()
            error = None
            _state(started_at=_now())
            try:
                run(jobs)
            except Exception as ex:  # pylint: disable=broad-except   (the hourly indexing catches up)
                db.session.rollback()
                error = f"{type(ex).__name__}: {str(ex)[:300]}"
                log.warning("supagent: applying the knowledge: %s", error, exc_info=True)
            with _LOCK:
                more = bool(_JOBS["catalog"] is not None or _JOBS["prefixes"] or _JOBS["objects"])
            _state(pending=more, done_at=_now(), seconds=round(time.time() - t0, 2), error=error)
            db.session.remove()


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def run(jobs: dict[str, Any]) -> dict[str, Any]:
    """The work itself (also what a save does at once when there is no app to run it later)."""
    from supagent.knowledge.index import embed_few, sync, sync_objects

    out: dict[str, Any] = {}
    if jobs.get("catalog") is not None:
        from supagent.knowledge.curated import after_change

        done = after_change(jobs["catalog"])
        embed_few(done.pop("pieces", {}) or {})
        out["catalog"] = done
    for prefix in sorted(jobs.get("prefixes") or ()):
        embed_few(sync((prefix,)))
    if jobs.get("objects"):
        embed_few(sync_objects(sorted(jobs["objects"])))
    return out


def _state(**values: Any) -> None:
    """The apply state in Superset's database (read by every web server)."""
    from superset import db

    from supagent.models import Meta

    try:
        row = db.session.get(Meta, KEY)
        cur = json.loads(row.value) if row is not None and row.value else {}
        cur.update(values)
        if row is None:
            db.session.add(Meta(key=KEY, value=json.dumps(cur)))
        else:
            row.value = json.dumps(cur)
        db.session.commit()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()


STALE_S = 300              # pending with no run started or ended this long: the process that saved stopped


def status() -> dict[str, Any]:
    """{pending, queued_at, started_at, done_at, seconds, error, stale}: stale when the saves wait for a process
    that stopped (the hourly indexing does the work then)."""
    from superset import db

    from supagent.models import Meta

    try:
        row = db.session.get(Meta, KEY)
        out = json.loads(row.value) if row is not None and row.value else {"pending": False}
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return {"pending": False}
    if out.get("pending"):
        last = max(x for x in (out.get("queued_at"), out.get("started_at"), out.get("done_at"), "") if x is not None)
        try:
            age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(last)).total_seconds() if last else 0
        except ValueError:
            age = 0
        out["stale"] = age > STALE_S
    return out
