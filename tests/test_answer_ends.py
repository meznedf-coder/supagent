"""Every way an answer ends gets its checks: an answer that goes round in circles (a model writing its
working notes again and again until its token limit) is sent back once, then cut and said; out of calls
or with no text, the answer still says a condition nobody asked for, a rule not applied, numbers nothing
gave; and one LLM answer has a token limit."""

from __future__ import annotations

import json

from test_agent_loop import agent_with, call, say

from supagent.agent import LOOP_NOTE, LOOP_NUDGE, repeating

LOOP = ("I will now write the response for the user with the numbers of both weeks.\n"
        "One final check: the application with the most failures is BILLING in both weeks.\n"
        "The summary is ready and the numbers are consistent with the results above.\n\n")
LATE = ('SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" = \'BILLING\' AND "DURATION_S" > 2700 AND '
        '"ts" >= \'2026-09-23 00:00\' AND "ts" < \'2026-09-24 00:00\'')
ASKED = "How many BILLING jobs ran longer than 45 minutes on 23 September?"


def _sql(sql):
    return {"request": {"database_id": 1, "sql": sql}}


def _rows(n, args):
    return json.dumps({"success": True, "columns": ["n"], "rows": [{"n": 337}]})


def test_a_text_going_round_in_circles():
    text = "Week 1: 512,709 jobs.\n\n" + LOOP * 6
    at = repeating(text)
    assert at is not None and text[:at].count("One final check") == 1
    table = "| hour | busy % |\n|---|---|\n" + "".join(f"| {h:02d}:00 | 97.00% of the CPU of srv-a, as the hour before |\n"
                                                    for h in range(24))
    assert repeating(table) is None                                        # rows of a table differ
    assert repeating("- BILLING: 12 failed jobs, all of them on the night batch.\n" * 3) is None    # one line
    assert repeating(LOOP * 2) is None                                     # twice is not yet a circle


def test_an_answer_going_round_in_circles_is_sent_back_then_cut(ctx, monkeypatch):
    a, _ran = agent_with(monkeypatch, [call("execute_sql", _sql(LATE)), say("337 late jobs.\n\n" + LOOP * 8),
                                       say("337 BILLING jobs were late on 23 September.")], results=_rows)
    answer, _trace = a.ask(ASKED)
    assert answer == "337 BILLING jobs were late on 23 September."
    last = a.llm.seen[-1]
    assert last[-1]["content"] == LOOP_NUDGE and last[-2]["content"].count("One final check") == 1   # cut in the context
    assert a.llm.caps[0] == 8192                                           # llm.max_answer_tokens
    b, _ran = agent_with(monkeypatch, [call("execute_sql", _sql(LATE)), say("337 late jobs.\n\n" + LOOP * 8),
                                       say("337 late jobs.\n\n" + LOOP * 5)], results=_rows)
    answer, _trace = b.ask(ASKED)
    assert answer.count("One final check") == 1 and answer.endswith(LOOP_NOTE)


def test_out_of_calls_or_of_text_the_condition_is_still_said(ctx, monkeypatch):
    from supagent.llm import EmptyAnswer

    added, variant = _sql(LATE + ' AND "STATUS" = \'SUCCESS\''), _sql(LATE + ' AND "STATUS" IN (\'SUCCESS\')')
    a, ran = agent_with(monkeypatch, [call("execute_sql", added), call("execute_sql", variant),
                                      say("337 BILLING jobs were late.")], results=_rows)
    a.max_steps = 2                                                        # the calls are used up after the variant
    answer, _trace = a.ask(ASKED)
    assert ran == [("execute_sql", variant)]
    assert "(Check: this answer counts only STATUS IN 'SUCCESS'" in answer and answer.endswith("were used up.)")
    b, _ran = agent_with(monkeypatch, [call("execute_sql", added), call("execute_sql", variant), EmptyAnswer("empty")],
                         results=_rows, rich=False)
    answer, _trace = b.ask(ASKED)
    assert "| 337 |" in answer and "(Check: this answer counts only STATUS IN 'SUCCESS'" in answer
    c, _ran = agent_with(monkeypatch, [call("execute_sql", _sql(LATE)), say("337 BILLING jobs, 412 of them in the night.")],
                         results=_rows)
    c.max_steps = 1
    answer, _trace = c.ask(ASKED)
    assert "(Check: these numbers or names do not come from the results of this answer's queries: 412" in answer


def test_the_words_about_writing_the_answer_go():
    from supagent.agent import without_preamble

    body = "**Batch summary: week of 14-20 Sep**\n\n- Total jobs: 512,709 then 511,550.\n- Failure rate: 8.01% then 8.04%."
    assert without_preamble("Now I have all the numbers from the queries. Let me write the summary:\n\n---\n\n" + body) == body
    assert without_preamble("Good, the numbers are confirmed. Now let me write the clean summary.\n\n***\n\n" + body) == body
    assert without_preamble("Let me write it for 23 September:\n\n" + body).startswith("Let me write")   # a figure
    kept = "The 3 servers with the most failures:\n\n" + body
    assert without_preamble(kept) == kept
    assert without_preamble("Let me write the summary:\n\nShort.") == "Let me write the summary:\n\nShort."


def test_the_rows_a_limit_let_through_are_not_a_count(ctx, monkeypatch):
    from supagent.agent import LIMIT_NUDGE, limit_as_count

    grouped = _sql('SELECT "ERROR_CATEGORY", "ERROR_EXCEPTION", COUNT(*) AS n FROM "jobs" WHERE "APPLICATION" = '
                   "'BILLING' GROUP BY 1, 2 ORDER BY n DESC LIMIT 100")
    rows = [{"ERROR_CATEGORY": "DATA", "ERROR_EXCEPTION": f"OutOfMemoryError (code {i})", "n": 3 + i % 7}
            for i in range(100)]
    big = json.dumps({"success": True, "row_count": 100, "rows": rows, "note": "The SQL's own LIMIT 100 was reached."})
    trace = [{"tool": "execute_sql", "status": "done", "args": grouped, "result": big}]
    assert limit_as_count("BILLING had **100 failed job runs** on 23 September.", trace) == 100
    assert limit_as_count("61 of the 100 failures were memory errors.", trace) == 100
    assert limit_as_count("Here are the first 100 rows.", trace) is None
    assert limit_as_count("At least 100 kinds of errors.", trace) is None
    assert limit_as_count("The 100 most frequent errors:", trace, "Show the 100 most frequent errors") is None

    def results(name, args):
        return big if "LIMIT 100" in args["request"]["sql"] else json.dumps({"success": True, "row_count": 1,
                                                                             "rows": [{"n": 358}]})
    total = _sql('SELECT COUNT(*) AS n FROM "jobs" WHERE "APPLICATION" = \'BILLING\'')
    a, ran = agent_with(monkeypatch, [call("execute_sql", grouped), say("BILLING had 100 failed job runs."),
                                      call("execute_sql", total), say("BILLING had 358 failed job runs.")],
                        results=results)
    answer, _trace = a.ask("Show me the errors of BILLING")
    assert answer == "BILLING had 358 failed job runs." and a.llm.seen[2][-1]["content"] == LIMIT_NUDGE.format(n=100)
    b, _ran = agent_with(monkeypatch, [call("execute_sql", grouped), say("BILLING had 100 failed job runs."),
                                       say("BILLING had 100 failed job runs.")], results=results)
    answer, _trace = b.ask("Show me the errors of BILLING")
    assert answer.endswith("(Check: 100 is where the LIMIT of a query cut its rows, not a count: more rows match.)")
