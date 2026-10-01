"""An answer is saved whatever its results hold: a sum over no row is NaN (OpenSearch), which PostgreSQL's JSON
refuses with the whole row (the lab lost an answer so: "The agent could not answer: ... UPDATE supagent_message").
NaN and infinities are stored as null."""

from __future__ import annotations

import math


def test_nan_in_results_is_saved_as_null(ctx):
    from superset.extensions import db

    from supagent.models import Conversation, Message
    from supagent.runner import _save, json_safe

    assert json_safe({"rows": [["A", float("nan")], ["B", 1.5]], "x": (float("inf"), 2)}) == \
        {"rows": [["A", None], ["B", 1.5]], "x": [None, 2]}
    c = Conversation(user_id=1, title="nan test")
    db.session.add(c)
    db.session.commit()
    m = Message(conversation_id=c.id, role="assistant", status="running", steps=[])
    db.session.add(m)
    db.session.commit()
    try:
        assert _save(m.id, content="done", status="done", results=[{"rows": [["EQ", float("nan")]]}],
                     steps=[{"tool": "execute_sql", "seconds": float("nan")}])
        db.session.refresh(m)
        assert m.results == [{"rows": [["EQ", None]]}] and m.steps[0]["seconds"] is None
        assert not any(isinstance(v, float) and math.isnan(v) for v in m.results[0]["rows"][0])
    finally:
        db.session.delete(m)
        db.session.delete(c)
        db.session.commit()
