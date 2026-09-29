"""When a question can mean two things that give different numbers, the agent asks one short
question back (rule 6) instead of guessing: that answer is not sent back for having run no tool nor
checked for numbers. The user's answer to it, and the reason of a Not helpful, are what the memory
learns from (a team point waits for an admin); the admins see the reasons (gaps)."""

from __future__ import annotations

import json

import pytest

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)

QUESTION_BACK = ("Do you count the KILLED jobs as failed too, or only STATUS_INFO = 'FAILED'? I would take only "
                 "'FAILED'. Which one do you mean?")


def test_a_question_back_is_an_answer(ctx, monkeypatch):
    from supagent.agent import CORE, asks_back
    from test_agent_loop import agent_with, say

    assert "answer only with ONE short question naming the readings you see" in CORE
    assert asks_back(QUESTION_BACK)
    assert not asks_back("BILLING had 41 failed jobs.") and not asks_back("| a | 1 |\n| b | 2 |\nWhich one?")
    a, _ran = agent_with(monkeypatch, [say(QUESTION_BACK)])
    answer, trace = a.ask("How many jobs failed yesterday?")
    assert answer == QUESTION_BACK and trace == [] and not a.usage.get("nudges")      # asked once, no tool


@pytest.fixture()
def chat(world):
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, Memory, Message

    alice = sm.find_user(username="alice").id
    conv = Conversation(user_id=alice, title="failures")
    db.session.add(conv)
    db.session.flush()
    rows = [Message(conversation_id=conv.id, role="user", content="How many jobs failed yesterday?"),
            Message(conversation_id=conv.id, role="assistant", content=QUESTION_BACK, status="done"),
            Message(conversation_id=conv.id, role="user", content="The killed ones too"),
            Message(conversation_id=conv.id, role="assistant", content="1,234 jobs failed or were killed.",
                    status="done")]
    for r in rows:
        db.session.add(r)
        db.session.flush()
    db.session.commit()
    yield {"conv": conv, "messages": rows, "alice": alice}
    db.session.query(Memory).delete()
    db.session.query(Message).filter(Message.conversation_id == conv.id).delete()
    db.session.delete(conv)
    db.session.commit()


class ExchangeLLM:
    def __init__(self, items):
        self.items, self.seen = items, []

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append(messages[1]["content"])
        return {"content": json.dumps(self.items)}


def test_the_answer_to_a_question_back_is_learned_as_a_team_definition(chat):
    from superset.extensions import db

    from supagent.knowledge.memory import asked_back, learn_from_message
    from supagent.models import Memory

    question, answer = chat["messages"][2], chat["messages"][3]
    assert asked_back(question)[1].content == QUESTION_BACK
    llm = ExchangeLLM([{"text": "Failed jobs include KILLED ones (STATUS_INFO FAILED or KILLED)", "scope": "team",
                        "kind": "fact", "category": "jobs"}])
    made = learn_from_message(answer.id, llm=llm)
    assert "ASSISTANT (asking what the user meant): Do you count the KILLED jobs" in llm.seen[0]
    assert llm.seen[0].index("How many jobs failed yesterday?") < llm.seen[0].index("The killed ones too")
    m = db.session.get(Memory, made[0])
    assert (m.scope, m.status) == ("team", "proposed")                       # waits for an admin


def test_the_reason_of_a_not_helpful_is_kept_shown_and_learned(chat, app, monkeypatch):
    from superset.extensions import db

    from supagent import tasks
    from supagent.knowledge.memory import learn_from_message
    from supagent.knowledge.quality import gaps
    from supagent.models import Message

    sent: list[int] = []
    monkeypatch.setattr(tasks, "dispatch_memory", lambda mid: sent.append(mid) or "thread")
    answer = chat["messages"][3]
    with app.app_context(), app.test_client() as c:
        login(c, "alice")
        r = c.post(f"/supagent/api/messages/{answer.id}/feedback", json={"value": -1}).get_json()
        assert r["feedback"] == -1 and r["feedback_reason"] is None and sent == []
        r = c.post(f"/supagent/api/messages/{answer.id}/feedback",
                   json={"value": -1, "reason": "WARNING jobs are not failures"}).get_json()
        assert r["feedback_reason"] == "WARNING jobs are not failures" and sent == [answer.id]
        shown = c.get(f"/supagent/api/messages/{answer.id}").get_json()
        assert shown["feedback_reason"] == "WARNING jobs are not failures"
    db.session.expire_all()
    llm = ExchangeLLM([])
    learn_from_message(answer.id, llm=llm)
    assert "USER (marked this answer Not helpful): WARNING jobs are not failures" in llm.seen[0]
    from supagent.security import acting_as

    with acting_as("admin"):
        lines = gaps(30)["not_helpful"]
    assert any("-- why: WARNING jobs are not failures" in ln for ln in lines)
    with app.app_context(), app.test_client() as c:
        login(c, "alice")
        r = c.post(f"/supagent/api/messages/{answer.id}/feedback", json={"value": 0}).get_json()
    assert r["feedback"] is None and r["feedback_reason"] is None                # taken back with it
    assert db.session.get(Message, answer.id) is not None


def test_results_with_an_offer_at_the_end_are_still_checked(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    offer = "On 23 September 6,996 jobs failed. Want the breakdown by application?"
    a, _ran = agent_with(monkeypatch, [say(offer), say(offer), say(offer)])
    answer, _trace = a.ask("How many jobs failed on 23 September?")
    assert a.usage.get("nudges", 0) == 2                                   # no tool, then a number from nowhere
    assert "(Check:" in answer and "6,996" in answer.split("(Check:")[-1]  # then marked


def test_ordinary_data_words_do_not_make_a_status_question():
    from supagent.agent import intents

    for q in ("How many jobs per status yesterday?", "Send me the health report of the servers as an e-mail",
              "Failed jobs by STATUS_INFO for PAYROLL"):
        assert "status" not in intents(q), q
    for q in ("What is the status of the gateway?", "Is the billing cluster healthy?", "What's happening now?",
              "Any issue on the servers right now?"):
        assert "status" in intents(q), q
