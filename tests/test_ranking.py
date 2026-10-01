"""What the agent learned, ranked by the discussions and the data requests: after an answer, what its prompt was
given, the tables its queries read and the learned queries it ran again (worked or failed) are recorded; each
learned item's usefulness comes from what people said of those answers; the learned answers proposed follow it
(one people keep saying is wrong is no longer proposed, unless an admin confirmed it); the Learned tab is sorted
by it, with its reasons; the prompt's knowledge says what it gave."""

from __future__ import annotations

import pytest

from test_experience import _trace
from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.fixture()
def learned(world):  # noqa: F811
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.experience import record_recipe
    from supagent.models import Conversation, ItemUse, Message, Recipe

    db.session.query(Recipe).delete()
    db.session.query(ItemUse).delete()
    db.session.commit()
    admin = sm.find_user(username="admin").id
    jobs = world["jobs"].id
    good = record_recipe(201, admin, "failed jobs per application yesterday", _trace(
        "SELECT APPLICATION, COUNT(*) FROM jobs WHERE STATUS = 'FAILED' GROUP BY 1", jobs))
    bad = record_recipe(202, admin, "failed jobs per application and queue yesterday", _trace(
        "SELECT APPLICATION, QUEUE, COUNT(*) FROM jobs WHERE STATUS = 'KILLED' GROUP BY 1, 2", jobs))
    conv = Conversation(user_id=admin, title="ranking test")
    db.session.add(conv)
    db.session.commit()
    ids = {"good": good.id, "bad": bad.id, "admin": admin, "jobs": jobs, "conv": conv.id}
    yield ids
    db.session.rollback()
    db.session.query(ItemUse).delete()
    db.session.query(Recipe).delete()
    db.session.query(Message).filter(Message.conversation_id == ids["conv"]).delete()
    db.session.query(Conversation).filter(Conversation.id == ids["conv"]).delete()
    db.session.commit()


def _answer(ctx, feedback, given, sql=None, status="done"):
    """An answer given these items, rated so (+1, -1, None), whose query is sql."""
    from superset.extensions import db

    from supagent.knowledge.ranking import record
    from supagent.models import Message

    m = Message(conversation_id=ctx["conv"], role="assistant", content="answer", status="done", feedback=feedback)
    db.session.add(m)
    db.session.commit()
    trace = _trace(sql, ctx["jobs"], status=status) if sql else []
    return record(m.id, ctx["admin"], set(given), trace)


def test_the_uses_of_an_answer_are_recorded(learned):
    from superset.extensions import db

    from supagent.models import ItemUse

    n = _answer(learned, 1, {f"recipe:{learned['good']}", "entry:7#2", "object:3", "chat:9"},
                sql="SELECT APPLICATION, COUNT(*) FROM jobs WHERE STATUS = 'FAILED' GROUP BY 1")
    rows = {(u.ref, u.how) for u in db.session.query(ItemUse)}
    assert (f"recipe:{learned['good']}", "given") in rows and ("entry:7", "given") in rows   # one per item
    assert not any(ref.startswith(("object:", "chat:")) for ref, _h in rows)       # only the learned kinds
    assert (f"recipe:{learned['good']}", "reused") in rows                           # its query ran again
    assert any(ref == f"data:{learned['jobs']}:jobs" and how == "used" for ref, how in rows)
    assert n == len(rows)


def test_usefulness_from_what_people_said_and_the_proposals_follow_it(learned):
    from superset.extensions import db

    from supagent.knowledge.experience import recipes_for
    from supagent.knowledge.ranking import adjust, demoted, reasons, usefulness
    from supagent.models import Recipe
    from supagent.security import acting_as

    good, bad = f"recipe:{learned['good']}", f"recipe:{learned['bad']}"
    for fb in (1, 1, 1, None):
        _answer(learned, fb, {good})
    for fb in (-1, -1, -1, -1, -1):
        _answer(learned, fb, {bad}, sql="SELECT APPLICATION, QUEUE, COUNT(*) FROM jobs WHERE STATUS = 'KILLED' "
                                        "GROUP BY 1, 2", status="error")
    use = usefulness([good, bad])
    assert (use[good]["given"], use[good]["helpful"], use[good]["not_helpful"]) == (4, 3, 0)
    assert use[good]["score"] == 0.8 and use[good]["weight"] == 0.6 and adjust(use[good]) > 0.3
    assert use[bad]["failed"] == 5 and demoted(use[bad]) and adjust(use[bad]) < -0.9
    assert "no longer proposed" in reasons(use[bad]) and "3 helpful, 0 not helpful" in reasons(use[good])
    with acting_as("admin"):
        found = [r["id"] for r in recipes_for("failed jobs per application and queue yesterday", limit=5)]
    assert found == [learned["good"]]                        # the one people keep saying is wrong: not proposed
    db.session.get(Recipe, learned["bad"]).status = "confirmed"   # an admin confirmed it: an admin decides
    db.session.commit()
    with acting_as("admin"):
        found = [r["id"] for r in recipes_for("failed jobs per application and queue yesterday", limit=5)]
    assert learned["bad"] in found


def test_the_learned_tab_is_sorted_by_usefulness(app, learned):
    from conftest import login

    good, bad = f"recipe:{learned['good']}", f"recipe:{learned['bad']}"
    for fb in (1, 1):
        _answer(learned, fb, {good})
    for fb in (-1, -1, -1, -1, -1):
        _answer(learned, fb, {bad})
    c = app.test_client()
    login(c, "admin")
    d = c.get("/supagent/dictionary/api/recipes").get_json()
    ids = [r["id"] for r in d["recipes"]]
    assert ids.index(learned["good"]) < ids.index(learned["bad"])
    row = next(r for r in d["recipes"] if r["id"] == learned["bad"])
    assert row["demoted"] and "not helpful" in row["why"] and row["use"]["given"] == 5


def test_the_prompt_says_what_it_gave(world):  # noqa: F811
    from supagent.knowledge.index import sync
    from supagent.knowledge.search import knowledge_block
    from supagent.security import acting_as

    sync()
    shown: set[str] = set()
    with acting_as("admin"):
        text = knowledge_block("failed jobs of the night batch", shown)
    assert text and shown and all(":" in ref for ref in shown)
