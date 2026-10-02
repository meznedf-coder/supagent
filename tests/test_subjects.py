"""The subjects of a chat (0.8): a follow-up keeps its subject and gets it from its start, a question about other
data starts a new subject without the earlier messages, a question about an earlier subject's data goes back to it;
when the words cannot tell, the LLM decides; not sure: the same subject."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_stop import FakeAgent

ORDERS = {"tool": "execute_sql", "sql": "SELECT CHANNEL, COUNT(*) FROM \"shop-orders\" WHERE DAY = '2026-09-22' GROUP BY 1",
          "database": "OpenSearch", "database_id": 1, "columns": ["CHANNEL", "n"], "rows": [["WEB", 3]], "row_count": 1}
TICKETS = {"tool": "execute_sql", "sql": "SELECT PRIORITY, COUNT(*) FROM \"shop-tickets\" GROUP BY 1",
           "database": "OpenSearch", "database_id": 1, "columns": ["PRIORITY", "n"], "rows": [["P1", 4]], "row_count": 1}


def msg(i, role, content, topic=None, results=None, status="done"):
    return SimpleNamespace(id=i, role=role, content=content, topic=topic, results=results, steps=[], status=status)


def chat(*pairs):
    """[(question, answer, topic, results)] -> messages."""
    out, i = [], 1
    for q, a, t, res in pairs:
        out.append(msg(i, "user", q, t))
        out.append(msg(i + 1, "assistant", a, t, res))
        i += 2
    return out


@pytest.fixture()
def tables(monkeypatch):
    """The resolver as the lab's: the words of a question -> the tables it needs."""
    from supagent.knowledge import topics

    words = {"order": "shop-orders", "orders": "shop-orders", "channel": "shop-orders", "sold": "shop-orders",
             "ticket": "shop-tickets", "tickets": "shop-tickets", "sla": "shop-tickets",
             "cpu": "node_cpu_seconds_total"}

    def fake(question):
        q = question.lower()
        return {t for w, t in words.items() if w in q.replace("?", " ").split()}

    monkeypatch.setattr(topics, "_question_tables", fake)
    return fake


def decide(question, messages, llm=None):
    from supagent.knowledge import topics

    subs, last = topics.subjects(messages)
    return topics.decide(question, subs, last, llm)


def test_follow_ups_stay_in_their_subject(ctx, tables):
    orders = chat(("How many orders did we sell on 22 September?", "1,204 orders.", 1, [ORDERS]))
    assert decide("How many orders did we sell?", []).topic is None                       # the first question
    for q in ("And on 23 September?", "Which channel had the most?", "Show it per hour.", "In euros please.",
              "What about the refunds?", "Only the web ones.", "How many of them were paid by card?"):
        d = decide(q, orders)
        assert d.topic == 1 and not d.back, (q, d)
    asked_back = chat(("How many failed?", "Do you mean the failed payments or the failed deliveries?", 1, None))
    assert decide("The payments", asked_back).how == "reply to the question back"


def test_other_data_starts_a_new_subject_and_an_earlier_subject_comes_back(ctx, tables):
    two = chat(("How many orders did we sell on 22 September?", "1,204 orders.", 1, [ORDERS]),
               ("Which channel had the most?", "WEB: 640.", 1, [ORDERS]),
               ("What is the SLA of a P1 ticket and how many P1 tickets breached it?", "4 hours; 12 breached.", 2,
                [TICKETS]))
    d = decide("How many CPU cores does the busiest server use?", two)
    assert d.topic is None, d                                   # other data: a new subject
    d = decide("How many web orders were sold on 23 September?", two)
    assert d.topic == 1 and d.back, d                           # the orders again: back to them
    d = decide("Back to the orders: and on the 24th?", two)
    assert d.topic == 1 and d.back, d
    d = decide("Another question: how many orders did we sell in total?", two)
    assert d.topic is None, d                                   # said so


def test_the_llm_decides_only_when_the_words_cannot(ctx, tables):
    one = chat(("How many orders did we sell on 22 September?", "1,204 orders.", 1, [ORDERS]))
    asked = []

    class LLM:
        def __init__(self, say):
            self.say = say

        def chat(self, messages, max_tokens=None, **_kw):
            asked.append(messages[-1]["content"])
            return {"content": self.say}

    q = "Which marketing campaign brought the most new customers this quarter?"
    assert decide(q, one, LLM("0")).topic is None and len(asked) == 1
    assert "Subject 1: How many orders did we sell on 22 September?" in asked[0]
    assert decide(q, one, LLM("1")).topic == 1
    assert decide(q, one, LLM("I am not sure")).topic is None   # no number: the words decide (nothing in common)
    asked.clear()
    assert decide("And on the 23rd?", one, LLM("0")).topic == 1 and not asked     # clear: no LLM call


def test_the_history_of_a_subject_from_its_start(ctx):
    from supagent.knowledge import topics

    long_answer = "Result line. " * 200
    msgs = chat(*[(f"question {i} about the orders", long_answer if i else "the first answer", 1, None)
                  for i in range(8)])
    subs, _last = topics.subjects(msgs)
    h = topics.history(subs[1])
    qs = [x["content"] for x in h if x["role"] == "user"]
    assert qs[0] == "question 0 about the orders" and qs[-3:] == [f"question {i} about the orders" for i in (5, 6, 7)]
    assert len(qs) == 8                                          # every question of the subject
    middle = [x["content"] for x in h if x["role"] == "assistant"][1:-3]
    assert all(len(a) <= topics.MIDDLE_CHARS + 2 and a.endswith("…") for a in middle)
    full = [x["content"] for x in h if x["role"] == "assistant"][-1]
    assert len(full) == min(len(long_answer), topics.HISTORY_CHARS)
    huge = chat(*[(f"q{i}", "x" * 3000, 1, None) for i in range(30)])
    subs, _last = topics.subjects(huge)
    h = topics.history(subs[1])
    assert sum(len(x["content"]) for x in h) <= topics.MAX_HISTORY_CHARS
    assert h[0]["content"] == "q0" and h[-2]["content"] == "q29"  # the first and the last kept
    legacy = chat(*[(f"old {i}", f"answer {i}", None, None) for i in range(5)])
    subs, last = topics.subjects(legacy)
    assert last == 0 and [x["content"] for x in topics.history(subs[0])][-2:] == ["old 4", "answer 4"]
    assert len(topics.history(subs[0])) == topics.LEGACY_MESSAGES          # as before 0.8


def _run(app, monkeypatch, rows, question):
    from superset.extensions import db, security_manager as sm

    from supagent import runner
    from supagent.knowledge import experience, generic, topics
    from supagent.models import Conversation, Message

    seen: dict = {}

    class Capture(FakeAgent):
        stop = False

        def ask(self, q, history=None):
            seen["history"] = history
            return super().ask(q, history)

    words = {"order": "shop-orders", "orders": "shop-orders", "ticket": "shop-tickets", "tickets": "shop-tickets",
             "cpu": "node_cpu_seconds_total"}
    monkeypatch.setattr(topics, "_question_tables",
                        lambda q: {t for w, t in words.items() if w in q.lower().replace("?", " ").split()})
    with app.app_context():
        conv = Conversation(user_id=sm.find_user(username="alice").id, title="Shop")
        db.session.add(conv)
        db.session.flush()
        for role, content, topic, results in rows:
            db.session.add(Message(conversation_id=conv.id, role=role, content=content, status="done", topic=topic,
                                   results=results))
        q = Message(conversation_id=conv.id, role="user", content=question, status="done")
        db.session.add(q)
        answer = Message(conversation_id=conv.id, role="assistant", status="pending", steps=[])
        db.session.add(answer)
        db.session.commit()
        ids = (q.id, answer.id)
        monkeypatch.setattr("supagent.agent.Agent", type("Agent", (Capture,), {"message_id": answer.id}))
        monkeypatch.setattr(generic, "generalize", lambda *a, **k: {})
        monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: None)
        runner.run_answer(answer.id)
        db.session.expire_all()
        topics_of = [db.session.get(Message, i).topic for i in ids]
    return seen["history"], topics_of


ROWS = [("user", "How many orders did we sell on 22 September?", 1, None),
        ("assistant", "1,204 orders.", 1, [ORDERS]),
        ("user", "And per channel?", 1, None),
        ("assistant", "WEB 640, SHOP 564.", 1, [ORDERS]),
        ("user", "How many P1 tickets breached their SLA this week?", 2, None),
        ("assistant", "12 P1 tickets breached it.", 2, [TICKETS])]


def test_an_answer_is_given_its_subject_only(app, monkeypatch):
    history, topics_of = _run(app, monkeypatch, ROWS, "How many web orders were sold on 23 September?")
    assert topics_of == [1, 1]                                   # back to the orders
    assert [h["content"] for h in history] == ["How many orders did we sell on 22 September?", "1,204 orders.",
                                               "And per channel?", "WEB 640, SHOP 564."]
    assert history[-1].get("queries") and "shop-orders" in history[-1]["queries"][0]["query"]
    history, topics_of = _run(app, monkeypatch, ROWS, "Which server had the highest CPU yesterday?")
    assert topics_of == [3, 3] and history == []                 # a new subject: none of the earlier messages
    history, topics_of = _run(app, monkeypatch, ROWS, "And how many P2 ones?")
    assert topics_of == [2, 2] and [h["content"] for h in history][0].startswith("How many P1 tickets")


def test_off_gives_the_last_exchanges_as_before(app, monkeypatch):
    from supagent import settings

    monkeypatch.setattr(settings, "get", (lambda real: (lambda key, *a: False if key == "agent.subjects"
                                                         else real(key, *a)))(settings.get))
    history, _t = _run(app, monkeypatch, ROWS, "Which server had the highest CPU yesterday?")
    assert [h["content"] for h in history][0] == "How many orders did we sell on 22 September?" and len(history) == 6


def test_the_words_of_a_subject_are_not_taken_for_time_words(ctx):
    from supagent.knowledge.topics import _subject_terms

    t = _subject_terms("Which events were declined in the main market on 23 September yesterday morning, per hour?")
    assert {"event", "market", "main"} <= t                     # "even", "mar", "mai" are no prefixes of them
    assert not {w for w in t if w.startswith(("septemb", "yesterday", "morn", "hour"))}


def test_another_day_of_the_previous_question_stays_in_its_subject(ctx, tables, monkeypatch):
    from supagent.knowledge import topics

    monkeypatch.setattr(topics, "_question_tables", lambda q: {"http_requests_total"})    # whatever it guesses
    errors = chat(("How many requests ended with a 503 error on 24 September?", "42 requests.", 1, None))
    d = decide("Compare with the day before.", errors)
    assert d.topic == 1 and d.how == "another day of the previous question" and d.how in topics.CONTINUES_LAST
    ranking = chat(("Which application failed most?", "The billing one.", 1, None))      # no day to be relative to
    assert decide("Compare with the day before.", ranking).how != "another day of the previous question"


def test_a_short_question_about_the_answer_stays_whatever_tables_its_words_guess(ctx, tables, monkeypatch):
    from supagent.knowledge import topics

    monkeypatch.setattr(topics, "_question_tables", lambda q: {"batch-jobs"})          # "failure rate": the jobs
    pricing = chat(("What was the maximum latency of the pricing service on 22 September?", "5 seconds.", 1, None))

    class NoLLM:
        def chat(self, *a, **k):
            raise AssertionError("the LLM is not asked")

    d = decide("What was its failure rate?", pricing, NoLLM())
    assert d.topic == 1 and d.how == "refers to the previous answer"
    assert decide("How many of them failed at BILLING?", pricing).how != "refers to the previous answer"   # a value
