"""The people waiting for an answer come first: the LLM calls of background work (the daily
learning's descriptions, learned answers and memory from a Helpful) wait while answers are being
computed (supagent.llm.background), at most WAIT_S seconds per call. Only answers being computed
count (not the queued ones: with every worker busy they cannot start anyway), and not those whose
progress stopped long ago (a worker that died)."""

from __future__ import annotations

import datetime as dt
import time

WAIT_S = 120.0            # longest wait of one background LLM call
POLL_S = 2.0
STALE_S = 600             # an answer without progress for this long is not waited for


def answers_running() -> int:
    from superset import db

    from supagent.models import Message

    since = dt.datetime.utcnow() - dt.timedelta(seconds=STALE_S)
    try:
        n = (db.session.query(Message.id)
             .filter(Message.status == "running", Message.updated_at >= since).limit(50).count())
        db.session.commit()                  # no connection of Superset's pool held while waiting
        return n
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return 0


def wait_for_answers(max_wait: float = WAIT_S, poll: float = POLL_S) -> float:
    """Wait while answers are being computed; returns the seconds waited. A learning run that an
    admin stops ends its wait at once (LearningStopped)."""
    from supagent.knowledge.stopping import check

    t0 = time.time()
    while time.time() - t0 < max_wait and answers_running():
        check()
        time.sleep(poll)
    return round(time.time() - t0, 1)
