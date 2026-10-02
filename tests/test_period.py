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


def test_a_follow_up_is_grounded_in_the_previous_answer_only(ctx, monkeypatch):
    """A follow-up answered without a query passes when it restates the previous answer; a number found only in an
    older answer of the subject (a limit, another book's figure) is no answer: sent back for a query."""
    from test_agent_loop import agent_with, say

    history = [{"role": "user", "content": "What is the VaR limit of the desk?"},
               {"role": "assistant", "content": "The VaR limit of the desk is 10,000 EUR."},
               {"role": "user", "content": "Which book lost the most on 22 September?"},
               {"role": "assistant", "content": "EQ_BOOK_A lost the most on 22 September: -248,707 EUR."}]
    a, _ran = agent_with(monkeypatch, [say("The VaR of that book was 10,000 EUR.")] * 3)
    a.ask("What was that book's VaR?", history)
    assert a.usage.get("nudges")                                         # a figure of an older answer: a query
    b, _ran = agent_with(monkeypatch, [say("EQ_BOOK_A lost 248,707 EUR on 22 September.")])
    b.ask("How much did it lose?", history)
    assert not b.usage.get("nudges")                                     # the previous answer, restated


def test_a_no_such_field_follow_up_is_sent_back_even_with_the_previous_figures(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    history = [{"role": "user", "content": "How many trades did the desk do on 23 September?"},
               {"role": "assistant", "content": "The desk did 93 trades on 23 September."}]
    a, _ran = agent_with(monkeypatch, [say("The data does not contain a voice field for the 93 trades. Which field "
                                           "do you mean?")] * 3)
    a.ask("How many of them were booked by voice?", history)
    assert a.usage.get("nudges")                                         # looked up first, not waved through


def test_a_follow_up_with_another_day_is_not_answered_from_the_chat(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    history = [{"role": "user", "content": "How many jobs failed on 23 September?"},
               {"role": "assistant", "content": "5,939 jobs failed on 23 September."}]
    a, _ran = agent_with(monkeypatch, [say("5,939 jobs failed on 22 September.")] * 3)
    answer, _trace = a.ask("And on 22 September?", history)
    assert a.usage.get("nudges") and "(Check:" in answer                 # another day: its own query
    b, _ran = agent_with(monkeypatch, [say("ok")])
    last = b.prompt("The CPU of srv-amer-002 yesterday.", history)[-1]["content"]
    assert last.endswith("The CPU of srv-amer-002 yesterday.")          # a new question, not a completion


def test_a_date_alone_on_a_field_with_times_is_sent_back(world):
    """A date field whose values carry a time (stored at 00:00 UTC, read at 02:00 here; or real times) finds
    nothing with = '2026-09-23': sent back once with the day's range; a field of dates alone passes."""
    from superset.extensions import db

    from supagent.knowledge.period import refusal
    from supagent.models import KObject

    src = db.session.query(KObject).filter(KObject.kind == "index", KObject.name == "jobs").one().source_id
    timed = KObject(source_id=src, kind="field", parent="jobs", name="RUN_DATE", data_type="date",
                    stats={"min": "2026-07-01 02:00", "max": "2026-09-25 02:00"})
    plain = KObject(source_id=src, kind="field", parent="jobs", name="COB", data_type="date",
                    stats={"min": "2026-07-01", "max": "2026-09-25 00:00"})
    db.session.add_all([timed, plain])
    db.session.commit()
    try:
        q = "How many jobs ran on 23 September?"
        back = refusal(q, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE "RUN_DATE" = \'2026-09-23\''), TODAY)
        assert back.startswith("tool error (not run: date)") and "\"RUN_DATE\" < '2026-09-24'" in back
        assert "e.g. 2026-09-25 02:00" in back
        inlist = refusal(q, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE RUN_DATE IN (\'2026-09-23\')'), TODAY)
        assert inlist and "not run: date" in inlist
        for typed in ("DATE '2026-09-23'", "TIMESTAMP '2026-09-23 00:00:00'"):   # the retail lab: = DATE '...'
            back = refusal(q, "execute_sql", _sql(f'SELECT COUNT(*) FROM "jobs" WHERE "RUN_DATE" = {typed}'), TODAY)
            assert back and "not run: date" in back, typed
        instant = refusal(q, "execute_sql", _sql("SELECT COUNT(*) FROM \"jobs\" WHERE \"RUN_DATE\" = TIMESTAMP "
                                                 "'2026-09-23 02:00:00' AND \"ts\" >= '2026-09-23 00:00' AND \"ts\" < "
                                                 "'2026-09-24 00:00'"), TODAY)
        assert "not run: date" not in (instant or "")                      # an instant: meant
        ranged = refusal(q, "execute_sql", _sql(
            "SELECT COUNT(*) FROM \"jobs\" WHERE \"RUN_DATE\" >= '2026-09-23' AND \"RUN_DATE\" < '2026-09-24' "
            "AND \"ts\" >= '2026-09-23 00:00' AND \"ts\" < '2026-09-24 00:00'"), TODAY)
        assert ranged is None
        assert "not run: date" not in (refusal(q, "execute_sql", _sql(
            "SELECT COUNT(*) FROM \"jobs\" WHERE \"COB\" = '2026-09-23' AND \"ts\" >= '2026-09-23 00:00' "
            "AND \"ts\" < '2026-09-24 00:00'"), TODAY) or "")                   # dates alone: equality works
    finally:
        db.session.delete(timed)
        db.session.delete(plain)
        db.session.commit()


def _shipments(world):
    """A shipments index with four dates; its learned time field is DELIVERED_TIME (the first in name order)."""
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.knowledge.store import source_for, upsert
    from supagent.models import Run

    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    jobs = db.session.query(Database).filter(Database.database_name == "jobs").one()
    s = source_for(jobs)
    upsert(run, s, "index", "", "shipments", {"stats": {"docs": 1000, "time_field": "DELIVERED_TIME"}})
    for name in ("DELIVERED_TIME", "SHIPPED_TIME", "ORDER_DATE", "PROMISED_DATE"):
        upsert(run, s, "field", "shipments", name, {"data_type": "date", "stats": {"filled_pct": 100.0}})
    upsert(run, s, "field", "shipments", "CARRIER", {"data_type": "keyword", "stats": {"values": ["FASTPOST"]}})
    db.session.commit()


def test_the_time_the_question_names_is_the_time_of_its_period(world):
    from supagent.knowledge.period import named_time_fields, ranges_named, refusal, whole_day

    _shipments(world)
    q = "Among the parcels shipped on 17 and 18 September, which carrier had the most late deliveries, and how many?"
    assert whole_day(q, TODAY) is None                                  # two days, not the 18th
    assert ranges_named(q, TODAY) == [(dt.datetime(2026, 9, 17), dt.datetime(2026, 9, 19))]
    assert named_time_fields(q, {"shipments"}) == {"shipments": {"SHIPPED_TIME"}}
    period = "'2026-09-17 00:00' AND \"{f}\" < '2026-09-19 00:00'"
    by = 'SELECT "CARRIER", COUNT(*) FROM "shipments" WHERE "LATE" = true AND "{f}" >= ' + period + ' GROUP BY "CARRIER"'
    wrong = refusal(q, "execute_sql", _sql(by.format(f="DELIVERED_TIME")), TODAY)
    assert wrong.startswith("tool error (not run: period)") and '"SHIPPED_TIME" of shipments' in wrong
    assert "puts the period on DELIVERED_TIME" in wrong
    assert refusal(q, "execute_sql", _sql(by.format(f="SHIPPED_TIME")), TODAY) is None
    both = ("How many of the LYON warehouse's orders of 22 September were shipped only on 24 September or later?")
    assert named_time_fields(both, {"shipments"}) == {"shipments": {"ORDER_DATE", "SHIPPED_TIME"}}
    assert refusal(both, "execute_sql", _sql("SELECT COUNT(*) FROM \"shipments\" WHERE \"ORDER_DATE\" >= '2026-09-22 "
                                             "00:00' AND \"ORDER_DATE\" < '2026-09-23 00:00' AND \"SHIPPED_TIME\" >= "
                                             "'2026-09-24 00:00'"), TODAY) is None
    plain = "What is the average delivery time of FASTPOST parcels in September, in days?"
    assert named_time_fields(plain, {"shipments"}) == {}                # "delivery time" names the measure here
    assert refusal(plain, "execute_sql", _sql("SELECT AVG(\"DELIVERY_DAYS\") FROM \"shipments\" WHERE \"CARRIER\" = "
                                              "'FASTPOST' AND \"DELIVERED_TIME\" >= '2026-09-01 00:00' AND "
                                              "\"DELIVERED_TIME\" < '2026-10-01 00:00'"), TODAY) is None
    assert named_time_fields("How many parcels were delivered on 23 September?", {"shipments"}) == \
        {"shipments": {"DELIVERED_TIME"}}


def test_each_day_the_question_names_has_its_field():
    """The retail lab's D4: two dates, each with its event: the period of 22 September is the orders' day."""
    import datetime as dt

    from supagent.knowledge.period import named_days

    fields = ["ORDER_DATE", "SHIPPED_TIME", "DELIVERED_TIME"]
    q = "How many of the LYON warehouse's orders of 22 September were shipped only on 24 September or later?"
    assert named_days(q, fields, TODAY) == {dt.date(2026, 9, 22): {"ORDER_DATE"}, dt.date(2026, 9, 24): {"SHIPPED_TIME"}}
    assert named_days("Parcels shipped 17-18 September and delivered late?", fields, TODAY) == {
        dt.date(2026, 9, 17): {"SHIPPED_TIME"}, dt.date(2026, 9, 18): {"SHIPPED_TIME"}}
    assert named_days("How many parcels on 23 September?", fields, TODAY) == {}


def test_an_index_takes_its_datasets_main_time_column(world):
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.knowledge.learn_indices import prefer_dataset_time

    jobs = db.session.query(Database).filter(Database.database_name == "jobs").one()
    fstats = {"DELIVERED_TIME": {"type": "date", "min": "2026-08-28", "max": "2026-09-24"},
              "SHIPPED_TIME": {"type": "date", "min": "2026-08-27", "max": "2026-09-25"}, "CARRIER": {"type": "keyword"}}
    info = {"time_field": "DELIVERED_TIME", "time_range": ["2026-08-28", "2026-09-24"]}
    prefer_dataset_time(info, fstats, jobs.id, ("shipments",))
    assert info["time_field"] == "DELIVERED_TIME"                       # no dataset: the rule's choice
    ds = SqlaTable(table_name="shipments", database_id=jobs.id, main_dttm_col="SHIPPED_TIME")
    db.session.add(ds)
    db.session.commit()
    try:
        prefer_dataset_time(info, fstats, jobs.id, ("shipments",))
        assert info == {"time_field": "SHIPPED_TIME", "time_range": ["2026-08-27", "2026-09-25"],
                        "time_field_from": "dataset"}
        from supagent.knowledge.learn_indices import dataset_time_now   # learned before: at once, not at the
        from supagent.knowledge.store import source_for, upsert            # next profile
        from supagent.models import Run

        src = source_for(jobs)
        run = db.session.query(Run).first()
        idx = upsert(run, src, "index", "", "shipments", {"stats": {"time_field": "DELIVERED_TIME",
                                                                     "time_range": ["2026-08-28", "2026-09-24"]}})
        made = [idx] + [upsert(run, src, "field", "shipments", n, {"data_type": st["type"], "stats": {
            k: v for k, v in st.items() if k != "type"}}) for n, st in fstats.items()]
        db.session.commit()
        assert dataset_time_now(idx, src, jobs.id, ("shipments",))
        assert (idx.stats["time_field"], idx.stats["time_range"]) == ("SHIPPED_TIME", ["2026-08-27", "2026-09-25"])
        assert not dataset_time_now(idx, src, jobs.id, ("shipments",))
        for o in made:
            db.session.delete(o)
        db.session.commit()
        ds.main_dttm_col = "CARRIER"                                     # not a date of the index: not taken
        db.session.commit()
        other = {"time_field": "DELIVERED_TIME"}
        prefer_dataset_time(other, fstats, jobs.id, ("shipments",))
        assert other == {"time_field": "DELIVERED_TIME"}
    finally:
        db.session.delete(ds)
        db.session.commit()


def test_a_month_is_a_months_name():
    from supagent.knowledge.period import days_named, has_period

    def d(m, n):
        return dt.date(2026, m, n)

    assert days_named("Which are the top 5 markets by sales?", TODAY) == []          # not 5 March
    assert days_named("We made 3 decisions and 10 junior hires; 2 augmented, 5 novices, 7 decimals", TODAY) == []
    assert not has_period("Which are the top 5 markets by sales?", TODAY)
    assert days_named("le 3 juin, le 4 sept., le 12 févr. et le 10 mars 2026", TODAY) == [d(6, 3), d(9, 4), d(2, 12),
                                                                                         d(3, 10)]
    assert days_named("on the 23rd of September", TODAY) == [d(9, 23)]
    assert days_named("Sept 23 and Dec. 1", TODAY) == [d(9, 23), d(12, 1)]
    assert days_named("le 1er août et le 15 août", TODAY) == [d(8, 1), d(8, 15)]


def test_a_day_said_relative_to_another_needs_one():
    from supagent.knowledge.period import anchor_of, days_named

    d22, d23 = dt.date(2026, 9, 22), dt.date(2026, 9, 23)
    assert days_named("And on the 23rd?", TODAY, anchor=d22) == [d23]
    assert days_named("Et le 23 ?", TODAY, anchor=d22) == [d23]
    assert days_named("And the day before?", TODAY, anchor=d23) == [d22]
    assert days_named("Et la veille ?", TODAY, anchor=d23) == [d22]
    assert days_named("And the next day?", TODAY, anchor=d22) == [d23]
    assert days_named("And on the 23rd?", TODAY) == []                               # no day to be relative to
    assert days_named("And the 2nd?", TODAY) == []
    assert days_named("What is the 3rd largest order of 22 September?", TODAY) == [d22]      # a rank, not a day
    assert days_named("How many failed the day before yesterday?", TODAY) == [d22]           # today - 2
    assert days_named("Failed jobs on 23 September compared with the day before", TODAY) == [d23, d22]
    assert days_named("the day before 23 September", TODAY) == [d22, d23]
    assert anchor_of(["How many orders on 22 September?", "And on the 23rd?"], TODAY) == d23
    assert anchor_of(["Which application failed most?"], TODAY) is None


def test_a_follow_up_puts_its_own_day_in_the_earlier_question():
    from supagent.knowledge.period import follow_up_text, named_among, whole_day

    night = ["How many parcels were shipped on 22 September during the night?"]
    t = follow_up_text("And on the 23rd?", night, TODAY)
    assert t.startswith("How many parcels were shipped on 23 September 2026 during the night?")
    assert whole_day(t, TODAY) is None                                  # the night stays: not the whole day
    t = follow_up_text("And on the 23rd?", ["How many parcels were shipped on 22 September?"], TODAY)
    assert whole_day(t, TODAY) == dt.date(2026, 9, 23)
    assert named_among(t, ["ORDER_DATE", "SHIPPED_TIME"]) == {"SHIPPED_TIME"}        # the event next to the day
    assert follow_up_text("And per carrier?", night, TODAY) is None                  # no day of its own
    assert follow_up_text("And the day before?", night + ["And on the 23rd?"], TODAY).startswith(
        "How many parcels were shipped on 22 September 2026 during the night?")
    assert follow_up_text("And last week?", ["How many orders on 22 September?"], TODAY) == \
        "How many orders on last week?\nAnd last week?"
    assert follow_up_text("And on the 24th?", ["Orders from 22 to 23 September?"], TODAY) is None     # a span
    assert follow_up_text("And the 2nd?", ["Which application failed most?"], TODAY) is None


def test_a_follow_up_for_another_day_is_checked_on_that_day(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    history = [{"role": "user", "content": "How many parcels were shipped on 22 September?"},
               {"role": "assistant", "content": "412 parcels were shipped on 22 September."}]
    a, _ran = agent_with(monkeypatch, [say("412 parcels were shipped on 22 September.")] * 3)
    answer, _trace = a.ask("And on the 23rd?", history)
    assert a.asks_new and not a.follow_up and a.usage.get("nudges") and "(Check:" in answer   # its own query
    assert a.intent_text.startswith("How many parcels were shipped on 22 September?")        # what it is about
    assert a.period_text.startswith("How many parcels were shipped on 23 September 2026?")   # the day it asks
    b, _ran = agent_with(monkeypatch, [say("ok")])
    b.prompt("And per carrier?", history)
    assert not b.asks_new and b.period_text == b.intent_text                          # the earlier day stays


def test_that_day_is_the_day_named_before():
    from supagent.knowledge.period import days_named, follow_up_text, named_among

    d22 = dt.date(2026, 9, 22)
    assert days_named("I meant the ones delivered that day.", TODAY, anchor=d22) == [d22]
    assert days_named("Et ce jour-là ?", TODAY, anchor=d22) == [d22]
    assert days_named("How many orders on the same day last week?", TODAY, anchor=d22) == []   # another day
    t = follow_up_text("I meant the ones delivered that day.", ["How many late shipments were there on 22 September?"],
                       TODAY)
    assert t.endswith("I meant the ones delivered 22 September 2026.")
    assert named_among(t, ["SHIPPED_TIME", "DELIVERED_TIME"]) == {"DELIVERED_TIME"}      # the follow-up's event


def test_a_question_that_goes_on_from_the_last_exchange_is_a_follow_up(ctx, monkeypatch):
    from test_agent_loop import agent_with, say

    from supagent.agent import refers_back
    from supagent.knowledge.topics import Decision

    assert refers_back("What was its failure rate?") and refers_back("Quel est son taux d'échec ?")
    history = [{"role": "user", "content": "How many pricing requests of pricer-eq failed on 22 September?"},
               {"role": "assistant", "content": "37 requests failed."}]
    a, _ran = agent_with(monkeypatch, [say("ok")])
    a.subject = Decision(1, "no subject of its own")
    a.prompt("Which error code came up most often?", history)
    assert a.intent_text.startswith("How many pricing requests of pricer-eq failed")     # its context
    b, _ran = agent_with(monkeypatch, [say("ok")])
    b.subject = Decision(1, "the same data")                                            # a new question
    b.prompt("Which error code came up most often?", history)
    assert b.intent_text == "Which error code came up most often?"
