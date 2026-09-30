"""The period of the question is the period of the queries: a date or a period in the question needs a filter
on the time of the data (the index's time field, ts for metrics), not on business dates unless asked; one
whole day needs the whole of that day. Sent back once. A reply to the agent's question back comes with the
question it answers; a chart asked for the top N that shows more is said NOT DONE; "n1-n5" of given names
is not a name of the model's own."""

from __future__ import annotations

import datetime as dt
import json
import types

from test_knowledge import world  # noqa: F401  (the fixture)

TODAY = dt.date(2026, 9, 24)


def test_the_days_of_a_question():
    from supagent.knowledge.period import days_named, has_period, whole_day

    d23 = dt.date(2026, 9, 23)
    assert days_named("How many jobs failed on 23 September?", TODAY) == [d23]
    assert days_named("Failures on September 23rd and on 2026-09-16", TODAY) == [d23, dt.date(2026, 9, 16)]
    assert days_named("Combien de jobs ont échoué le 23 septembre ?", TODAY) == [d23]
    assert days_named("How many jobs failed yesterday?", TODAY) == [d23]
    assert whole_day("How many batch jobs failed on 23 September?", TODAY) == d23
    assert whole_day("How many jobs failed during the night batch window on 23 September?", TODAY) is None
    assert whole_day("Failed jobs on 23 September between 02:00 and 06:00", TODAY) is None
    assert whole_day("Failed jobs on 23 September compared with the previous weeks", TODAY) is None
    assert whole_day("Failed jobs on 23 September vs 16 September", TODAY) is None
    assert has_period("What happened last week?", TODAY) and has_period("Is it normal right now?", TODAY)
    assert not has_period("What does the application BILLING do, and on which servers do its jobs run?", TODAY)


def _sql(sql: str) -> dict:
    return {"request": {"database_id": 1, "sql": sql}}


def test_a_query_that_misses_the_period_is_sent_back(world):
    from supagent.knowledge.period import refusal

    q = "How many jobs failed on 23 September?"
    day = "\"ts\" >= '2026-09-23 00:00' AND \"ts\" < '2026-09-24 00:00'"
    assert refusal(q, "execute_sql", _sql(f'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND {day}'),
                   TODAY) is None
    no_time = refusal(q, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\''), TODAY)
    assert no_time.startswith("tool error (not run: period)") and 'time field "ts" of jobs' in no_time
    business = refusal(q, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE "POSITION_LABEL" = \'D-1\''), TODAY)
    assert "position (business) date" in business
    assert refusal("How many jobs of the position date D-1 failed?", "execute_sql",
                   _sql('SELECT COUNT(*) FROM "jobs" WHERE "POSITION_LABEL" = \'D-1\''), TODAY) is None
    night = refusal(q, "create_virtual_dataset", _sql(
        "SELECT COUNT(*) FROM \"jobs\" WHERE \"ts\" >= '2026-09-23 00:00' AND \"ts\" < '2026-09-23 06:00'"), TODAY)
    assert "covers only 00:00 to 06:00" in night
    other = refusal(q, "execute_sql", _sql(
        "SELECT COUNT(*) FROM \"jobs\" WHERE \"ts\" >= '2026-09-24 00:00' AND \"ts\" < '2026-09-25 00:00'"), TODAY)
    assert "this query's dates are 2026-09-24, 2026-09-25" in other
    assert refusal(q, "execute_sql", _sql(
        "SELECT COUNT(*) FROM \"jobs\" WHERE \"ts\" BETWEEN '2026-09-23 00:00:00' AND '2026-09-23 23:59:59'"),
        TODAY) is None
    assert refusal(q, "execute_sql", _sql('SELECT DISTINCT "NODE" FROM "jobs"'), TODAY) is None     # values listed
    assert refusal(q, "execute_sql", _sql('SELECT * FROM "jobs" LIMIT 10'), TODAY) is None           # a look
    assert refusal(q, "execute_sql", _sql('SELECT * FROM "jobs" WHERE "STATUS" = \'FAILED\' LIMIT 10'), TODAY)
    assert refusal("Which servers run the jobs?", "execute_sql", _sql('SELECT COUNT(*) FROM "jobs"'), TODAY) is None
    cpu = refusal("CPU busy % yesterday", "execute_sql", _sql('SELECT AVG(value) FROM "node_cpu_seconds_total"'),
                  TODAY)
    assert 'time field "ts" of node_cpu_seconds_total' in cpu
    assert refusal(q, "list_charts", {}, TODAY) is None


def test_the_checks_before_a_call_say_every_reason_at_once(world, monkeypatch):
    from test_agent_loop import agent_with, call, say

    from supagent.knowledge import period

    monkeypatch.setattr(period, "_now", lambda: dt.datetime(2026, 9, 24, 23, 30))
    bad = _sql('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\'')
    good = _sql("SELECT COUNT(*) FROM \"jobs\" WHERE \"STATUS\" = 'FAILED' AND \"ts\" >= '2026-09-23 00:00' AND "
                "\"ts\" < '2026-09-24 00:00'")
    a, ran = agent_with(monkeypatch, [call("execute_sql", bad), call("execute_sql", good), say("12 jobs failed.")],
                        results=lambda n, args: json.dumps({"success": True, "columns": ["n"], "rows": [{"n": 12}]}))
    answer, trace = a.ask("How many jobs failed on 23 September?")
    assert answer == "12 jobs failed." and ran == [("execute_sql", good)]
    assert "not run: period" in trace[0]["result"]


def test_a_reply_to_a_question_back_comes_with_the_question(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    a, _ran = agent_with(monkeypatch, [say("BILLING_API had 358 failed jobs.")])
    history = [{"role": "user", "content": "Show me the errors of BILLING_API on 23 September."},
               {"role": "assistant", "content": "Do you mean the failed jobs, or the HTTP 5xx errors of its service?"}]
    msgs = a.prompt("The failed jobs.", history)
    last = msgs[-1]["content"]
    assert "Show me the errors of BILLING_API on 23 September." in last
    assert 'You asked: "Do you mean the failed jobs, or the HTTP 5xx errors of its service?" My reply: The failed ' \
           "jobs." in last
    assert a.intent_text.startswith("Show me the errors of BILLING_API") and "23 September" in a.question
    plain = a.prompt("How many jobs failed on 23 September?", [
        {"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hello! What do you want to know?"}])
    assert plain[-1]["content"].endswith("How many jobs failed on 23 September?")


def test_a_chart_asked_for_the_top_n_that_shows_more_is_not_done(ctx):
    from supagent.agent import ChartGuard, categories, top_n, wanted_top

    z = "In the dashboard, change the bar chart to show only the 5 applications with the most failures"
    y = "a line chart of the CPU busy % per hour of the 5 busiest servers and a bar chart per application"
    assert wanted_top("APPLICATION", top_n(z)) == 5 and wanted_top("node", top_n(y)) == 5
    assert wanted_top("APPLICATION", top_n(y)) is None and wanted_top("APPLICATION", top_n("Failed jobs")) is None
    rows = [{"APPLICATION": f"APP{i}", "SUM(failed)": 10 - i} for i in range(10)]
    assert categories(rows) == ("APPLICATION", 10)
    assert categories([{"__timestamp": "2026-09-23T00:00:00", "srv-1": 3}]) == (None, 0)
    agent = types.SimpleNamespace(question=z, superset=types.SimpleNamespace(
        available=True, call=lambda name, args: json.dumps({"data": rows, "row_count": 10})))
    note = ChartGuard(agent).shows(141)
    assert "NOT DONE: the question asks for 5 and this chart shows 10 APPLICATION values" in note
    agent.question = "Change the bar chart title"
    assert "NOT DONE" not in ChartGuard(agent).shows(141)


def test_a_range_of_given_names_is_not_a_name_of_its_own():
    from supagent.grounding import ungrounded

    msgs = [{"role": "user", "content": "Which nodes?"},
            {"role": "tool", "content": json.dumps({"rows": [{"node": f"n{i}"} for i in range(1, 6)]})}]
    assert ungrounded("The nodes are n1-n5.", msgs) == []
    assert ungrounded("The nodes are n1-n9.", msgs) == ["n1-n9"]


def test_how_many_answered_with_shares_only_is_sent_back_once(ctx, monkeypatch):
    from test_agent_loop import agent_with, call, say

    from supagent.agent import COUNT_NUDGE, missing_count

    q = "What was the availability of each HTTP service, and how many 5xx errors did each one have?"
    assert missing_count(q, "A: 1.00% of errors, 99.00% available.")
    assert not missing_count(q, "A: 1.00% of errors (51,986 errors), 99.00% available.")
    assert not missing_count("How many jobs failed on 23 September?", "None failed on 23 September.")
    assert not missing_count("What was the availability?", "A: 99.00% available.")
    assert missing_count("How many jobs failed on 23 September?", "On 2026-09-23 at 02:00, see srv-amer-002.")
    rows = lambda n, args: json.dumps({"success": True, "columns": ["app", "pct", "n"],  # noqa: E731
                                       "rows": [{"app": "A", "pct": 1.0, "n": 51986}]})
    a, _ran = agent_with(monkeypatch, [call("execute_sql", {"request": {"database_id": 1, "sql": "SELECT 1"}}),
                                       say("A: 1.00% of errors."), say("A: 51,986 errors, 1.00% of the requests.")],
                         results=rows)
    answer, _trace = a.ask(q)
    assert answer.startswith("A: 51,986 errors") and COUNT_NUDGE in [m["content"] for m in a.llm.seen[-1]]


def test_a_list_continued_past_the_results_is_cut_after_the_check(ctx, monkeypatch):
    from test_agent_loop import agent_with, call, say

    from supagent.agent import without_extrapolation

    text = ("BILLING runs on 200 servers:\n- `srv-amer-000`\n- `srv-amer-001`\n- ... continuing up to `srv-amer-199`\n"
            "Some jobs have no server.")
    cut, left = without_extrapolation(text, ["srv-amer-199"])
    assert "srv-amer-199" not in cut and "srv-amer-001" in cut and left == []
    assert without_extrapolation("It runs on srv-x-9.", ["srv-x-9"]) == ("It runs on srv-x-9.", ["srv-x-9"])  # no range
    rows = lambda n, args: json.dumps({"success": True, "columns": ["NODE"],  # noqa: E731
                                       "rows": [{"NODE": "srv-amer-000"}, {"NODE": "srv-amer-001"}]})
    a, _ran = agent_with(monkeypatch, [call("execute_sql", {"request": {"database_id": 1, "sql": "SELECT 1"}}),
                                       say(text), say(text)], results=rows)
    answer, _trace = a.ask("On which servers do the BILLING jobs run?")
    assert "srv-amer-199" not in answer and "(Check:" not in answer and answer.startswith("BILLING runs on 200")


def test_a_short_reply_after_an_answer_completes_the_question(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    a, _ran = agent_with(monkeypatch, [say("ok")])
    history = [{"role": "user", "content": "Show me the errors of BILLING_API on 23 September."},
               {"role": "assistant", "content": "BILLING_API had 358 errors. (Not read for this answer: ...)"}]
    last = a.prompt("The failed jobs.", history)[-1]["content"]
    assert last.endswith("Show me the errors of BILLING_API on 23 September.\n(About your answer above: The failed "
                         "jobs.) Answer my question again with this.")
    fresh = a.prompt("Show me the dashboards.", history)[-1]["content"]
    assert fresh.endswith("Show me the dashboards.")


def test_a_span_of_days_is_queried_exactly(world):
    from supagent.knowledge.period import ranges_named, refusal

    q = ("Weekly review for the week of 14 to 20 September, compared with the week of 7 to 13 September: jobs per "
         "business line.")
    assert ranges_named(q, TODAY) == [(dt.datetime(2026, 9, 14), dt.datetime(2026, 9, 21)),
                                      (dt.datetime(2026, 9, 7), dt.datetime(2026, 9, 14))]
    week = lambda a, b: _sql(f"SELECT COUNT(*) FROM \"jobs\" WHERE \"ts\" >= '{a}' AND \"ts\" < '{b}'")  # noqa: E731
    assert refusal(q, "execute_sql", week("2026-09-14 00:00", "2026-09-21 00:00"), TODAY) is None
    assert refusal(q, "execute_sql", week("2026-09-07 00:00", "2026-09-14 00:00"), TODAY) is None
    late = refusal(q, "execute_sql", week("2026-09-14 00:00", "2026-09-21 06:00"), TODAY)
    assert "covers 2026-09-14 00:00 to 2026-09-21 06:00" in late
    assert refusal(q + " And the previous 4 weeks?", "execute_sql", week("2026-08-17 00:00", "2026-09-14 00:00"),
                   TODAY) is None                                                               # another span
    assert refusal("Jobs from 14 to 20 September between 02:00 and 06:00", "execute_sql",
                   week("2026-09-14 02:00", "2026-09-20 06:00"), TODAY) is None               # hours: not whole days


def test_a_follow_up_restating_the_chats_results_needs_no_new_query(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    history = [{"role": "user", "content": "Show me the errors of BILLING_API on 23 September."},
               {"role": "assistant", "content": "BILLING_API had 358 failed jobs on 23 September. (Not read for this "
                                                "answer: BILLING_API is also a value of the label app of 2 metrics.)"}]
    a, _ran = agent_with(monkeypatch, [say("BILLING_API had 358 failed jobs on 23 September.")])
    answer, _trace = a.ask("The failed jobs.", history)
    assert answer == "BILLING_API had 358 failed jobs on 23 September." and not a.usage.get("nudges")
    b, _ran = agent_with(monkeypatch, [say("BILLING_API had 412 failed jobs.")] * 3)
    answer, _trace = b.ask("The failed jobs.", history)                  # a number of its own: sent back, marked
    assert b.usage.get("nudges") and "(Check:" in answer
