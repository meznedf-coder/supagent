"""The numbers of an answer come from what the LLM was given: a value of a result as written or
rounded as shown, a share as a percentage, seconds in minutes, bytes in GB, a total of a column or
of its first rows, a row count, a rate within a row; the question's, the chat's, the knowledge's
numbers. Dates, times, links, code, names with digits are not numbers to check. Another number is
the model's own: the agent is asked once to take it from a query, then it is marked."""

from __future__ import annotations

import json

import pytest

from supagent.grounding import answer_numbers, ungrounded

RESULT = {"columns": ["APPLICATION", "TOTAL_JOBS", "FAILED_JOBS", "FAILURE_RATE"],
          "rows": [["BILLING", 1200, 41, 0.034166], ["PAYROLL", 800, 12, 0.015], ["LEDGER", 350, 7, 0.02]]}


def given(question: str = "How many jobs failed per application on 23 September?", result=RESULT) -> list[dict]:
    return [{"role": "system", "content": "You answer with 100 and 0.95 in examples."},
            {"role": "user", "content": question},
            {"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "execute_sql", "arguments": json.dumps({"sql": "SELECT ... LIMIT 50"})}}]},
            {"role": "tool", "content": json.dumps(result)}]


@pytest.mark.parametrize("answer", [
    "BILLING had 41 failed jobs out of 1,200 (3.4%), PAYROLL 12 of 800 (1.5%).",
    "BILLING: 41 failures, a failure rate of 3.42 %.",                     # rounded as shown, a share in %
    "In all, 60 jobs failed out of 2,350 (2.55%).",                      # totals and their rate
    "The top 2 account for 53 failures.",                                 # the first rows' total
    "3 applications had failures; BILLING had 29 more than PAYROLL.",          # a row count, a difference
    "BILLING: 1 200 jobs, 41 en erreur (3,4 %).",                           # French writing
    "The average is 20 failed jobs per application.",                     # the average of a column
    "Chart saved: /superset/explore/?slice_id=4521 on 2026-09-23 from 02:00 to 06:00, BILLING, W-1.",
    "```sql\nSELECT 12345 FROM t\n```\nDone: 41 failed.",
])
def test_what_comes_from_the_results_is_not_flagged(answer):
    assert ungrounded(answer, given()) == []


@pytest.mark.parametrize("answer, wrong", [
    ("BILLING had 43 failed jobs out of 1,200.", ["43"]),
    ("The failure rate of BILLING is 4.1%.", ["4.1%"]),
    ("PAYROLL ran 815 jobs and LEDGER 350.", ["815"]),
    ("That is 12,345 jobs in all.", ["12,345"]),
])
def test_a_number_of_the_models_own_is_flagged(answer, wrong):
    assert ungrounded(answer, given()) == wrong


def test_conversions_and_the_question_and_the_knowledge():
    metrics = {"rows": [{"host": "srv-1", "seconds": 5400, "bytes": 274877906944, "busy": 0.4667}]}
    msgs = given("Memory and CPU of srv-1 over the last 12 hours?", metrics)
    assert ungrounded("It ran 90 minutes (1.5 hours), 256 GB of memory, CPU busy 46.7% over 12 hours "
                      "(720 minutes).", msgs) == []
    msgs.insert(2, {"role": "user", "content": "The team's words: SLA: the night batch ends before 06:00, "
                                                 "at most 250 failures a night."})
    assert ungrounded("The SLA allows 250 failures a night.", msgs) == []


def test_a_unit_constant_grounds_only_itself():
    """3600 (seconds in an hour) said as such is fine; it does not make 59 to 61 of anything "found" in minutes."""
    prev = [{"role": "assistant", "content": "The two most-traded products were IRS and SWAPTION, 19 trades each."}]
    assert ungrounded("The desk had 61 trades booked by voice.", prev) == ["61"]
    assert ungrounded("An hour is 3,600 seconds and a GiB 1,073,741,824 bytes.", []) == []


def test_small_counts_and_years_are_not_checked():
    assert answer_numbers("The 5 busiest servers in 2026, rank 3.") == []
    assert [t for t, _r in answer_numbers("12.5% and 1,234")] == ["12.5%", "1,234"]


def _result(name, args):
    return json.dumps({"success": True, **RESULT})


def test_the_agent_asks_once_then_marks(ctx, monkeypatch):
    from test_agent_loop import agent_with, call, say

    a, _ran = agent_with(monkeypatch, [
        call("execute_sql", {"request": {"database_id": 1, "sql": "SELECT 1"}}),
        say("BILLING had 43 failed jobs."),                                  # not in the result: asked again
        say("BILLING had 41 failed jobs."),
    ], results=_result)
    answer, _trace = a.ask("How many jobs failed for BILLING?")
    assert answer == "BILLING had 41 failed jobs." and a.usage["nudges"] == 1
    sent = a.llm.seen[-1][-1]["content"]
    assert "these numbers or names of your answer are in none of the results above: 43" in sent

    b, _ran = agent_with(monkeypatch, [
        call("execute_sql", {"request": {"database_id": 1, "sql": "SELECT 1"}}),
        say("BILLING had 43 failed jobs."),
        say("BILLING had 43 failed jobs, I am sure."),
    ], results=_result)
    answer, _trace = b.ask("How many jobs failed for BILLING?")
    assert answer.endswith("(Check: these numbers or names do not come from the results of this answer's queries: 43.)")


def test_the_check_can_be_switched_off(ctx, monkeypatch):
    from supagent import settings
    from test_agent_loop import agent_with, call, say

    settings.set_value("agent.check_numbers", False)
    try:
        a, _ran = agent_with(monkeypatch, [
            call("execute_sql", {"request": {"database_id": 1, "sql": "SELECT 1"}}),
            say("BILLING had 43 failed jobs."),
        ], results=_result)
        answer, _trace = a.ask("How many jobs failed for BILLING?")
    finally:
        settings.set_value("agent.check_numbers", True)
    assert answer == "BILLING had 43 failed jobs."


def test_durations_the_answer_computes_from_times_and_units():
    msgs = given("How long was srv-3 under memory pressure?", {"breaches": [
        {"check": "memory_pressure", "from": "2026-09-24 02:30", "to": "2026-09-24 05:30", "worst": 2.0}],
        "avg_duration_s": 293.4})
    assert ungrounded("Memory pressure from 02:30 to 05:30 (180 minutes, 3 h), worst 2% free.", msgs) == []
    assert ungrounded("It lasted 180 minutes.", msgs) == []                       # the result's from / to
    assert ungrounded("Average duration: 4.89 minutes (about 4 minutes and 53 seconds).", msgs) == []
    assert ungrounded("Average duration: 6 minutes and 10 seconds.", msgs) == ["6 minutes and 10 seconds"]
    assert ungrounded("*First 10 of 13 rows: every row is in the result below.*", msgs) == []


def test_a_duration_in_two_units_matches_a_value_in_minutes():
    msgs = given("Average duration of the failed jobs?", {"rows": [{"avg_minutes": 4.89}]})
    assert ungrounded("4.89 minutes on average (4 minutes and 53 seconds).", msgs) == []
    assert ungrounded("From 02:30 to 03:00 (30 minutes) then 04:10 to 04:50.", msgs) == []


def test_a_dashboard_said_made_needs_the_tool_that_makes_it():
    from supagent.agent import claims_check

    done = [{"tool": "generate_dashboard", "called": "generate_dashboard", "status": "done", "result": "{}"}]
    failed = [{"tool": "generate_dashboard", "called": "generate_dashboard", "status": "error", "result": "{}"}]
    text = "The dashboard 'Night health' has been created with three charts."
    assert claims_check(text, done) == ""
    assert "no Superset dashboard was created or changed" in claims_check(text, failed)
    assert "no Superset dashboard" in claims_check("I added the chart to the dashboard Ops.", [])
    assert claims_check("Le tableau de bord a été créé.", done) == ""
    assert claims_check("Here are the failed jobs per application.", []) == ""


def test_a_name_the_results_did_not_give_is_flagged():
    msgs = given("On which servers do the BILLING jobs run?", {"rows": [{"NODE": "srv-amer-000"}, {"NODE": "srv-amer-001"},
                                                                      {"NODE": "srv-emea-004"}]})
    assert ungrounded("They run on srv-amer-000, srv-amer-001 and srv-emea-004.", msgs) == []
    assert ungrounded("From srv-amer-000 through srv-amer-199.", msgs) == ["srv-amer-199"]   # a series continued
    assert ungrounded("See https://h/srv-q-2, positions W-1 and D-1.", msgs) == []
    # a name in `code` is a claim too: the model wrote the end of a list it never saw
    assert ungrounded("They run on `srv-amer-000`, `srv-amer-001` ... up to `srv-amer-200`.", msgs) == ["srv-amer-200"]
    assert ungrounded("```sql\nSELECT * FROM t WHERE node = 'srv-x-9'\n```", msgs) == []       # a code block: no


def test_a_big_result_says_how_many_rows_are_empty():
    from supagent.knowledge.experience import compact_for_llm

    rows = [{"NODE": f"srv-amer-{i:03d}"} for i in range(200)] + [{"NODE": None}]
    out = json.loads(compact_for_llm(json.dumps({"success": True, "columns": ["NODE"], "rows": rows}),
                                     "On which servers do the BILLING jobs run?"))
    assert out["row_count"] == 201 and out["empty_values"] == {"NODE": 1}
    assert "not a name" in out["note"] and "never guess the names" in out["note"]
    small = json.loads(compact_for_llm(json.dumps({"success": True, "columns": ["NODE"], "rows": rows[:3]}), "q"))
    assert "empty_values" not in small


def test_out_of_calls_the_answer_says_what_was_done(ctx, monkeypatch):
    from test_agent_loop import agent_with, call, say

    calls = [call("execute_sql", {"request": {"database_id": 1, "sql": f"SELECT {i}"}}) for i in range(10)]
    a, ran = agent_with(monkeypatch, calls + [say("Two charts were made; the dashboard is still to do.")],
                        results=_result)
    answer, _trace = a.ask("How many jobs failed?")
    assert len(ran) == 10 and answer.startswith("Two charts were made; the dashboard is still to do.")
    assert answer.endswith("(Stopped: the tool calls of one answer were used up.)")
    assert "No tool call is left for this answer" in a.llm.seen[-1][-1]["content"]


def test_metric_and_field_names_are_not_values_to_check():
    msgs = given("Load of the servers?", {"rows": [{"node": "srv-amer-000", "v": 1.5}]})
    assert ungrounded("node_load1 and node_load15 of srv-amer-000.", msgs) == []


def test_an_answer_written_again_never_speaks_of_the_check():
    from supagent.agent import NUMBERS_NUDGE, RULES_NUDGE, without_apology

    assert "never mention it" in NUMBERS_NUDGE and "never mention it" in RULES_NUDGE
    text = ("You're right, I included fabricated numbers in the truncated portion. Let me provide the correct answer "
            "based only on the actual query results.\n\n**Summary:** 358 failed jobs.")
    assert without_apology(text) == "**Summary:** 358 failed jobs."
    assert without_apology("Sorry about that.\n358 failed jobs.") == "358 failed jobs."
    assert without_apology("358 failed jobs. You're right to ask.") == "358 failed jobs. You're right to ask."


def test_the_rest_of_a_share_shown_in_the_answer_is_not_made_up():
    msgs = given("Availability of each HTTP service?", {"rows": [{"app": "A", "error_pct": 1.0}]})
    assert ungrounded("| A | 1.00 | 99.00 |\n\nA: 1.00% of errors, 99.00% available.", msgs) == []
    assert ungrounded("A: 99.00% available.", msgs) == ["99.00%"]                  # its base is not shown
    counts = given("Jobs and servers?", {"rows": [{"jobs": 12}]})
    assert ungrounded("12 jobs on 88 servers.", counts) == ["88"]                 # not a share


def test_converted_values_rounded_twice_and_unit_constants():
    msgs = given("Peak heap of each application?", {"rows": [{"app": "A", "bytes": 20610467840}]})
    assert ungrounded("A: 19.20 GiB (1,073,741,824 bytes per GiB).", msgs) == []     # 19.1949 GiB, 2^30
    assert ungrounded("A: 19.30 GiB.", msgs) == ["19.30"]


def test_a_number_said_as_rounded_is_read_as_rounded():
    msgs = given("Jobs per server?", {"rows": [{"node": "a", "n": 12305}, {"node": "b", "n": 12864}]})
    assert ungrounded("Roughly 12,300–12,900 jobs per server.", msgs) == []
    assert ungrounded("12,300 jobs on server a.", msgs) == ["12,300"]          # said as exact: exact
    assert ungrounded("About 13,500 jobs per server.", msgs) == ["13,500"]      # too far


def test_a_result_cut_by_the_sqls_own_limit_says_so():
    from supagent.knowledge.experience import compact_for_llm

    rows = [{"ERROR": f"E{i}", "n": 1} for i in range(100)]
    res = {"success": True, "columns": ["ERROR", "n"], "rows": rows, "note": "The SQL's own LIMIT 100 was reached"}
    out = json.loads(compact_for_llm(json.dumps(res), "Show me the errors"))
    assert "The SQL's own LIMIT 100 was reached" in out["note"]


def test_the_total_of_the_rows_a_sentence_names_is_from_the_results():
    rows = {"rows": [{"APPLICATION": "BILLING", "failed": 616}, {"APPLICATION": "ORDERS", "failed": 285},
                     {"APPLICATION": "PAYROLL", "failed": 255}, {"APPLICATION": "LEDGER", "failed": 100}]}
    msgs = given("Failed jobs per application?", rows)
    assert ungrounded("BILLING and PAYROLL, the critical ones, account for 871 of them.", msgs) == []   # rows 1 + 3
    assert ungrounded("BILLING and PAYROLL account for 950 of them.", msgs) == ["950"]
    assert ungrounded("The critical ones account for 871 of them.", msgs) == ["871"]     # which rows: not said
