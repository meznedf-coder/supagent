"""The governed agent end to end (a scripted model, a fake execute_sql): the decider's choice, a plan checked
(a condition nobody gave sent back, the team's rule added by code), the query built by code, the answer written
from the results with how it was counted; a question back, a chart request that goes to the classic agent, a
query that fails and is planned again; the same with LangGraph and with the plain driver."""

from __future__ import annotations

import json
import re

import pytest

from test_agent_loop import ScriptedLLM, call, say
from test_decider import lab  # noqa: F401  (the fixture)

Q = "How many BILLING jobs failed on 23 September?"
DAY = {"start": "2026-09-23 00:00", "end": "2026-09-24 00:00", "source": "question: on 23 September"}


def governed(monkeypatch, replies, results=None):
    from supagent.agent import Agent, ChartGuard
    from supagent.governed.pipeline import GovernedAgent

    a = object.__new__(GovernedAgent)
    a.username, a.on_step, a.should_stop, a.rich, a.max_steps = "admin", None, None, True, 10
    a.llm = ScriptedLLM(replies)
    a.names = {"execute_sql", "generate_chart", "list_charts"}
    a.local, a.scope, a.places, a.follow_up, a.support = {}, None, {}, False, None
    a.open_question, a.people_words, a.asks_new = False, "", False
    a.superset = type("S", (), {"schema": staticmethod(lambda name: {}), "available": True, "error": None})()
    a.guard = ChartGuard(a)
    ran: list[tuple[str, dict]] = []

    def fake_call(self, name, args):
        ran.append((name, args))
        out = results(name, args) if results else {"success": True, "columns": [{"name": "failed jobs"}],
                                                   "rows": [{"failed jobs": 42}], "row_count": 1}
        return name, json.dumps(out)

    monkeypatch.setattr(Agent, "_system", lambda self, q, shown=None: "SYSTEM")
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q, shown=None: "")
    monkeypatch.setattr(Agent, "_specs_for", lambda self, q: [])
    monkeypatch.setattr(Agent, "_call", fake_call)
    return a, ran


def choose(kind="data", table='"batch-jobs" in database'):
    def reply(messages):
        text = messages[-1]["content"]
        ref = next((line.split(":")[0] for line in text.splitlines() if table in line), "T1")
        rule = next((line.split(":")[0] for line in text.splitlines() if "Decider rule" in line), None)
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "choose_knowledge", "arguments": json.dumps({
                "kind": kind, "needs": [{"what": "jobs", "tables": [ref]}], "knowledge": [rule] if rule else [],
                "confidence": "high"})}}]}
    return reply


def plan(steps, kind="answer", **kw):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "p", "type": "function", "function": {
        "name": "submit_plan", "arguments": json.dumps({"kind": kind, "steps": steps, **kw})}}]}


def jobs_step(where, **kw):
    return {"id": "q1", "table": "T1", "measures": [{"label": "failed jobs", "fn": "count"}], "where": where,
            "period": DAY, **kw}


GOOD = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"},
        {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}]


class _Replies(ScriptedLLM):
    """A ScriptedLLM whose replies may be functions of the messages."""

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        self.last_usage = {"calls": 1, "seconds": 1.0, "prompt_tokens": 100, "completion_tokens": 10}
        return reply(messages) if callable(reply) else reply


@pytest.fixture(params=["langgraph", "plain"])
def driver(request, monkeypatch):
    from supagent.governed import graph

    if request.param == "plain":
        monkeypatch.setattr(graph, "langgraph_available", lambda: False)
    else:
        pytest.importorskip("langgraph")
    return request.param


def test_a_checked_plan_answers_with_how_it_counted(lab, monkeypatch, driver):
    from supagent.security import acting_as

    a, ran = governed(monkeypatch, [])
    a.llm = _Replies([choose(), plan([jobs_step(GOOD)]), say("42 BILLING jobs failed on 23 September.")])
    with acting_as("admin"):
        answer, trace = a.ask(Q)
    sql = ran[0][1]["request"]["sql"]
    assert ran[0][0] == "execute_sql" and "\"STATUS\" = 'FAILED'" in sql and "\"ENV\" <> 'UAT'" in sql   # rule by code
    assert answer.startswith("42 BILLING jobs failed on 23 September.")
    assert "How this was counted" in answer and 'STATUS = FAILED (you said "failed")' in answer
    assert "ENV ≠ UAT (the team's rule \"Decider rule\")" in answer and "23 Sep 2026" in answer
    assert [t["tool"] for t in trace] == ["decide", "plan", "execute_sql", "answer"] and a.way == "governed"
    assert json.loads(trace[2]["full"])["rows"] == [{"failed jobs": 42}]          # the page's result view


def test_a_condition_nobody_gave_is_planned_again(lab, monkeypatch, driver):
    from supagent.security import acting_as

    a, ran = governed(monkeypatch, [])
    invented = GOOD + [{"field": "STATUS", "op": "=", "value": "SUCCESS", "source": "question: jobs"}]
    a.llm = _Replies([choose(), plan([jobs_step(invented)]), plan([jobs_step(GOOD)]), say("42 jobs.")])
    with acting_as("admin"):
        answer, trace = a.ask(Q)
    assert len(ran) == 1 and "SUCCESS" not in ran[0][1]["request"]["sql"]
    assert [t["status"] for t in trace if t["tool"] == "plan"] == ["error", "done"]
    assert "do not say 'SUCCESS'" in a.llm.seen[2][-1]["content"]


def test_a_question_back_and_a_chart_request(lab, monkeypatch, driver):
    from supagent.security import acting_as

    a, ran = governed(monkeypatch, [])
    a.llm = _Replies([choose(), plan([], kind="clarify", question_back="Job failures or HTTP errors?",
                                     options=["failed jobs", "HTTP 5xx errors"])])
    with acting_as("admin"):
        answer, _trace = a.ask("How many errors did BILLING have on 23 September?")
    assert answer == "- failed jobs\n- HTTP 5xx errors\n\nJob failures or HTTP errors?" and ran == []   # ends asking
    b, ran = governed(monkeypatch, [])
    sql = {"request": {"database_id": 1, "sql": "SELECT 1"}}
    b.llm = _Replies([choose(kind="action"), call("execute_sql", sql), say("The chart is saved.")])
    with acting_as("admin"):
        answer, trace = b.ask("Save a bar chart of the failed jobs per application on 23 September.")
    assert b.way == "classic: a action question" and answer.startswith("The chart is saved.")
    assert [t["tool"] for t in trace][:2] == ["decide", "classic"]


def test_a_failed_query_is_planned_again(lab, monkeypatch, driver):
    from supagent.security import acting_as

    calls = {"n": 0}

    def results(name, args):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "error": "PushdownError: too many documents"}
        return {"success": True, "columns": [{"name": "failed jobs"}], "rows": [{"failed jobs": 7}], "row_count": 1}

    a, ran = governed(monkeypatch, [], results)
    a.llm = _Replies([choose(), plan([jobs_step(GOOD)]), plan([jobs_step(GOOD)]), say("7 jobs failed.")])
    with acting_as("admin"):
        answer, trace = a.ask(Q)
    assert len(ran) == 2 and answer.startswith("7 jobs failed.") and "too many documents" in a.llm.seen[2][-1]["content"]


def test_numbers_not_in_the_results_are_sent_back_then_marked(lab, monkeypatch, driver):
    from supagent.security import acting_as

    a, _ran = governed(monkeypatch, [])
    a.llm = _Replies([choose(), plan([jobs_step(GOOD)]), say("45 jobs failed."), say("45 jobs failed.")])
    with acting_as("admin"):
        answer, _trace = a.ask(Q)
    assert "(Check: these numbers or names do not come from the results" in answer and "45" in answer


def test_knowledge_questions_and_periods_outside_the_data(lab, monkeypatch, driver):
    from supagent.security import acting_as

    def explain(messages):
        text = messages[-1]["content"]
        words = next(line.split(":")[0] for line in text.splitlines() if "night batch window" in line)
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "choose_knowledge", "arguments": json.dumps({"kind": "explain", "needs": [], "knowledge": [words],
                                                                 "confidence": "high"})}}]}

    a, ran = governed(monkeypatch, [])
    a.llm = _Replies([explain, say("The night batch window runs from 00:00 to 06:00 (the glossary).")])
    with acting_as("admin"):
        answer, trace = a.ask("What is the night batch window?")
    assert answer.startswith("The night batch window runs from 00:00 to 06:00") and ran == []
    assert [t["tool"] for t in trace] == ["decide", "explain"]                  # no plan needed
    b, ran = governed(monkeypatch, [], lambda n, args: {"success": True, "columns": [{"name": "failed jobs"}],
                                                        "rows": [], "row_count": 0})
    old = dict(DAY, start="2025-01-01 00:00", end="2025-01-02 00:00", source="question: on 1 January 2025")
    b.llm = _Replies([choose(), plan([jobs_step(GOOD, period=old)]), say("No BILLING jobs failed then.")])
    with acting_as("admin"):
        answer, _trace = b.ask("How many BILLING jobs failed on 1 January 2025?")
    assert "There is no data for that period: batch-jobs holds data from 01 Jun 2026 to 25 Sep 2026" in answer


def test_the_reply_to_a_question_back_teaches_the_gate(lab, monkeypatch, driver):
    from superset.extensions import db

    from supagent.models import Route
    from supagent.security import acting_as

    a, _ran = governed(monkeypatch, [])
    a.llm = _Replies([choose(), plan([jobs_step(GOOD)]), say("42 BILLING jobs failed on 23 September.")])
    history = [{"role": "user", "content": "How many errors did BILLING have on 23 September?"},
               {"role": "assistant", "content": "Job failures or HTTP errors?"}]
    with acting_as("admin"):
        a.ask("The failed jobs, on 23 September.", history)
    a.after_saved(4321)
    r = db.session.get(Route, a.route_id)
    assert r.message_id == 4321 and r.signal == "clarified" and r.used == [f"data:{lab['main']}:batch-jobs"]


def test_a_step_that_fails_hands_the_question_to_the_classic_agent(lab, monkeypatch, driver):
    """A governed step raising (the lab: a learned count ">=200" read as a number) never ends the answer in an
    error: the classic agent answers, and says it answered without a checked plan."""
    from supagent.governed.pipeline import GovernedAgent
    from supagent.security import acting_as

    a, ran = governed(monkeypatch, [])
    said = say("42 BILLING jobs failed on 23 September.")
    a.llm = _Replies([choose(), said, said, said])                 # the classic checks may ask again

    def broken(self, st, feedback=""):
        raise ValueError("invalid literal for int() with base 10: '>=200'")

    monkeypatch.setattr(GovernedAgent, "_n_plan", broken)
    with acting_as("admin"):
        answer, trace = a.ask(Q)
    assert answer.startswith("42 BILLING jobs failed") and "without a checked plan: a step failed (ValueError)" in answer
    assert a.way.startswith("classic: a step failed") and any(t["tool"] == "classic" for t in trace)


def test_a_learned_count_that_is_a_lower_bound_is_read_as_one():
    from supagent.governed.decider import exact_count

    assert exact_count(12) == 12 and exact_count("12") == 12 and exact_count(12.0) == 12
    assert exact_count(">=200") is None and exact_count(None) is None and exact_count(True) is None


def test_a_question_back_with_options_ends_with_the_question():
    """The user's reply is read as the answer to a question back only when the message ends with the question:
    the options come first (the lab's question back ended with its list, so a reply would have been a new
    question)."""
    from supagent.agent import asks_back
    from supagent.governed.compose import clarify
    from supagent.governed.plan import parse_plan

    plan, err = parse_plan({"kind": "clarify", "question_back": "Which options book do you mean?",
                            "options": ["FX_OPT_EM", "FX_OPT_G10", "all the books of the FX_OPTIONS desk"]})
    assert plan is not None, err
    text = clarify(plan)
    assert text.splitlines()[-1] == "Which options book do you mean?" and "- FX_OPT_G10" in text
    assert asks_back(text)
