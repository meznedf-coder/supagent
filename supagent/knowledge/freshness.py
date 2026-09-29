"""One stamp for "the knowledge changed" (supagent_meta), so that every process (web servers,
Celery workers) drops its caches of the dictionary, the catalog and what the team said not to
use at its next question, instead of after their time to live: a description a person just
wrote is used by the next answer, on any server.

Written in the transaction of the change or after it (never before: a process would read the
new stamp with the old data); read at most every READ_S seconds per process."""

from __future__ import annotations

import time

from superset import db

KEY = "knowledge_changed"
READ_S = 2.0
_SEEN: dict[str, object] = {"at": 0.0, "value": ""}


def touch() -> None:
    """The knowledge changed (the caller commits)."""
    from supagent.models import Meta

    value = f"{time.time():.6f}"
    row = db.session.query(Meta).filter(Meta.key == KEY).first()
    if row is None:
        db.session.add(Meta(key=KEY, value=value))
    else:
        row.value = value
    _SEEN.update(at=0.0)                                  # this process: at once


def stamp() -> str:
    """The last change ("" before any), as the database has it."""
    now = time.time()
    if now - float(_SEEN["at"]) < READ_S:                 # type: ignore[arg-type]
        return str(_SEEN["value"])
    from supagent.models import Meta

    try:
        value = db.session.query(Meta.value).filter(Meta.key == KEY).scalar() or ""
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        db.session.rollback()
        value = ""
    _SEEN.update(at=now, value=value)
    return value
