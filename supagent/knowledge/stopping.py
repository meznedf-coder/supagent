"""Stopping a learning run: the Stop button of the settings page (or `superset supagent learn
--stop`) marks the running run "stopped" at once, so that a new run can start right away (also
when the run's process died: a restart during a run). The run looks at its status before each
request to a database, each LLM request, each step and while it waits (every few seconds at
most), and ends, keeping what it learned; the request or LLM call in progress at that moment
finishes in the background. Runs marked "stopping" by an older version are still honoured.

LearningStopped is not an Exception (like KeyboardInterrupt): the learning code catches
Exception to go on after a failed request, and must never record a stop as such a failure."""

from __future__ import annotations

import datetime as dt
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

CHECK_S = 3.0                    # the run's status is read at most this often
STOP_GRACE = 90                  # seconds a stopping run keeps a new one waiting (a dead process: no longer)
ASKED = "stop asked at "
_LOCAL = threading.local()


class LearningStopped(BaseException):
    """An admin stopped the learning run."""


class _Control:
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.at = 0.0
        self.stopped = False


@contextmanager
def watching(run_id: int) -> Iterator[None]:
    """While this run learns, check() raises LearningStopped once it was asked to stop."""
    before = getattr(_LOCAL, "control", None)
    _LOCAL.control = _Control(run_id)
    try:
        yield
    finally:
        _LOCAL.control = before


def _status(run_id: int) -> str | None:
    from sqlalchemy import text
    from superset import db

    with db.engine.connect() as conn:            # not the run's own session (its work in progress)
        return conn.execute(text("SELECT status FROM supagent_run WHERE id = :i"), {"i": run_id}).scalar()


def check(force: bool = False) -> None:
    control = getattr(_LOCAL, "control", None)
    if control is None:
        return
    if control.stopped:
        raise LearningStopped("stopped by an admin")
    now = time.time()
    if not force and now - control.at < CHECK_S:
        return
    control.at = now
    try:
        status = _status(control.run_id)
    except Exception:  # pylint: disable=broad-except   (the database busy: look again later)
        return
    if status not in (None, "running"):        # stopping, or already marked stopped after STOP_GRACE
        control.stopped = True
        raise LearningStopped("stopped by an admin")


def request_stop() -> int | None:
    """Mark the running learning run as stopped, at once; its id, or None when no run is running."""
    from superset import db

    from supagent.models import Run

    from supagent.knowledge.learner import KINDS

    run = (db.session.query(Run).filter(Run.kind.in_(KINDS), Run.status.in_(("running", "stopping")))
           .order_by(Run.id.desc()).first())
    if run is None:
        return None
    now = dt.datetime.utcnow()
    n = (db.session.query(Run).filter(Run.id == run.id, Run.status.in_(("running", "stopping")))
         .update({"status": "stopped", "finished_at": now,
                  "error": f"stopped by an admin at {now:%Y-%m-%d %H:%M:%S} UTC"}, synchronize_session=False))
    db.session.commit()
    return run.id if n else None


def asked_at(run: Any) -> dt.datetime | None:
    text = str(getattr(run, "error", "") or "")
    if not text.startswith(ASKED):
        return None
    try:
        return dt.datetime.strptime(text[len(ASKED):len(ASKED) + 19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
