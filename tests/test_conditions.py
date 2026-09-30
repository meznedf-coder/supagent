"""Every condition of a query comes from what was said: the question and the chat, the team's words, the
documents found, the formulas of the instructions, the rows an earlier query of the answer gave. A
condition the model adds by itself ("late" jobs counted among the successful ones only) is sent back before
the query runs; sent again unchanged, the query runs and the answer says the condition nobody asked for."""

from __future__ import annotations

import json

from supagent.knowledge.conditions import build, call_conditions, note, refusal, sql_conditions, unsaid

QUESTION = "According to our SLA note, how many BILLING jobs were late on 23 September?"
BLOCKS = ("What this user and the team asked to remember:\n- (team, fact) The critical applications are BILLING and "
          "PAYROLL (field APPLICATION).\n\nWhere the data is:\n- field \"STATUS\" (keyword; values: FAILED, KILLED, "
          "RUNNING, SUCCESS) of index \"jobs\" in database 1\n\nBackground:\n- [index] index jobs: fields: STATUS: "
          "FAILED, SUCCESS\n- [doc] SLA note: a job is late when it runs more than 45 minutes, that is DURATION_S > "
          "2700.\n\n(Now: Thursday 2026-09-24 23:30.)\n" + QUESTION)
SYSTEM = "Rules... CPU usage = busy %: 100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate)."


def _support(question=QUESTION, blocks=BLOCKS, history=()):
    messages = [{"role": "system", "content": SYSTEM}, *history, {"role": "user", "content": blocks}]
    return build(messages, [question, "The critical applications are BILLING and PAYROLL."])


def _sql(sql):
    return {"request": {"database_id": 1, "sql": sql}}


LATE = ('SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" = \'BILLING\' AND "DURATION_S" > 2700 AND '
        '"ts" >= \'2026-09-23 00:00\' AND "ts" < \'2026-09-24 00:00\'')


def test_a_condition_nobody_asked_for_is_found(ctx):
    s = _support()
    assert refusal(s, "execute_sql", _sql(LATE)) is None
    added = refusal(s, "execute_sql", _sql(LATE + ' AND "STATUS" = \'SUCCESS\''))
    assert added.startswith("tool error (not run: a condition nobody asked for)") and "STATUS = 'SUCCESS'" in added
    # the dictionary's list of every status is not a source; "45 minutes" is 2,700 seconds
    longer = QUESTION.replace("late", "longer than 45 minutes")
    assert refusal(_support(longer, longer), "execute_sql",
                   _sql('SELECT COUNT(*) FROM "jobs" WHERE "DURATION_S" > 2700')) is None
    assert unsaid(sql_conditions('SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" IN (\'BILLING\', \'ORDERS\')'),
                  s)[0].values == ["BILLING", "ORDERS"]                    # ORDERS: nobody said it


def test_what_was_said_in_other_words_is_found(ctx):
    def ok(question, sql, blocks=None, tool="execute_sql"):
        s = _support(question, blocks if blocks is not None else question)
        return refusal(s, tool, _sql(sql) if tool != "promql_query" else {"expr": sql}) is None

    assert ok("How many jobs failed yesterday?", 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\'')
    assert ok("Show me the errors of BILLING", 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND '
                                               '"APPLICATION" = \'BILLING\'')
    assert ok("Failed jobs in production", 'SELECT COUNT(*) FROM "jobs" WHERE "ENV" = \'PROD\'')
    assert ok("How many jobs completed?", 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'SUCCESS\'')
    assert ok("HTTP 5xx errors per app", "SELECT app, SUM(increase) FROM http_requests_total WHERE code LIKE '5%' "
                                         "GROUP BY app")
    assert ok("Availability (not 5xx) per app", "SELECT app, SUM(increase) FILTER (WHERE code NOT LIKE '5%') FROM "
                                                "http_requests_total GROUP BY app")
    assert ok("Peak JVM heap per application", "SELECT app, MAX(value) FROM jvm_used WHERE area = 'heap' GROUP BY app")
    assert ok("CPU busy of srv-a", "SELECT 100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate) FROM cpu "
                                   "WHERE node = 'srv-a'", blocks="")        # the formula of the instructions
    assert ok("Relaunched jobs yesterday", 'SELECT COUNT(*) FROM "jobs" WHERE "RELAUNCHED" = true')
    assert ok("Jobs of the critical applications", 'SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" IN '
                                                   "('BILLING', 'PAYROLL')")   # the memory
    assert ok("CPU of fed-a", 'avg(temp{__tenant_id__="fed-a"})', tool="promql_query")
    assert ok("Errors of BILLING on 23 September", 'SELECT COUNT(*) FROM "jobs" WHERE "POSITION_DATE" = 20260923 '
                                                   "AND \"STATUS\" = 'FAILED'")      # a date: the period check
    assert not ok("How many jobs ran longer than 45 minutes?", "SELECT COUNT(*) FROM dur_bucket WHERE le = '3600'")
    assert not ok("How many jobs ran yesterday?", 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'SUCCESS\'')
    assert not ok("How many jobs ran yesterday?", 'SELECT COUNT(*) FROM "jobs" WHERE "ENV" = \'PROD\'')
    assert not ok("Jobs per server", 'SELECT "NODE", COUNT(*) FROM "jobs" GROUP BY 1 HAVING COUNT(*) > 100')


def test_what_an_earlier_query_of_the_answer_gave_is_said_too(ctx):
    s = _support("CPU busy % of the 2 servers with the most failed jobs", "")
    cpu = "SELECT node, AVG(busy) FROM cpu WHERE node IN ('srv-a', 'srv-b') GROUP BY node"
    assert refusal(s, "execute_sql", _sql(cpu))                             # not yet found
    s.add_result(json.dumps({"rows": [{"NODE": "srv-a", "failed": 43}, {"NODE": "srv-b", "failed": 42}]}))
    assert unsaid(call_conditions("execute_sql", _sql(cpu)), s) == []
    listed = _support("How many jobs ran?", "")
    listed.add_result(json.dumps({"rows": [{"STATUS": "SUCCESS"}, {"STATUS": "FAILED"}]}))   # a list of values
    assert refusal(listed, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'SUCCESS\''))


def test_lists_charts_and_the_note(ctx):
    s = _support()
    assert call_conditions("execute_sql", _sql('SELECT DISTINCT "STATUS" FROM "jobs"')) == []   # looking, not counting
    chart = {"request": {"dataset_id": 1, "config": {"filters": [{"column": "STATUS", "op": "=", "value": "SUCCESS"}]}}}
    assert refusal(s, "generate_chart", chart)
    trace = [{"tool": "execute_sql", "status": "done", "args": _sql(LATE + ' AND "STATUS" = \'SUCCESS\'')}]
    assert note(s, trace).startswith("\n\n(Check: this answer counts only STATUS = 'SUCCESS'")
    assert note(s, [{"tool": "execute_sql", "status": "done", "args": _sql(LATE)}]) == ""


def test_the_agent_sends_it_back_then_marks_it(ctx, monkeypatch):
    from test_agent_loop import agent_with, call, say

    ASKED = "How many BILLING jobs ran longer than 45 minutes on 23 September?"     # noqa: N806
    rows = lambda n, args: json.dumps({"success": True, "columns": ["n"], "rows": [{"n": 337}]})  # noqa: E731
    added = _sql(LATE + ' AND "STATUS" = \'SUCCESS\'')
    a, ran = agent_with(monkeypatch, [call("execute_sql", added), call("execute_sql", _sql(LATE)),
                                      say("337 BILLING jobs were late on 23 September.")], results=rows)
    answer, trace = a.ask(ASKED)
    assert ran == [("execute_sql", _sql(LATE))] and answer == "337 BILLING jobs were late on 23 September."
    assert "a condition nobody asked for" in trace[0]["result"]
    b, ran = agent_with(monkeypatch, [call("execute_sql", added), call("execute_sql", added),
                                      say("337 BILLING jobs were late on 23 September.")], results=rows)
    answer, _trace = b.ask(ASKED)
    assert ran == [("execute_sql", added)] and "(Check: this answer counts only STATUS = 'SUCCESS'" in answer
    c, ran = agent_with(monkeypatch, [call("execute_sql", added), say("Nothing unusual.")], results=rows)
    c.ask("Is everything normal with the BILLING jobs right now?")        # an open question: its own choices
    assert ran == [("execute_sql", added)]


def test_more_of_what_was_found_or_said(ctx):
    s = _support("CPU busy % per hour of the 2 busiest servers", "")
    cpu = _sql("SELECT node, AVG(busy) FROM cpu WHERE node IN ('srv-a', 'srv-b') GROUP BY node")
    s.add_result(json.dumps({"series": [{"labels": {"node": "srv-a"}, "max": 91.0, "values": [["t", 91.0]]},
                                        {"labels": {"node": "srv-b"}, "max": 88.0, "values": [["t", 88.0]]}]}))
    assert refusal(s, "execute_sql", cpu) is None                          # the servers of a PromQL result
    cut = _support("Load of the servers with the most failed jobs", "")
    cut.add_result('{"rows": [{"NODE": "srv-a", "failed": 50}, {"NODE": "srv-b", "failed": 4')   # cut at 4000
    assert refusal(cut, "execute_sql", cpu) is None
    apps = _support("Excel extract of the failed jobs of today with the team of each application", "")
    apps.add_result(json.dumps({"rows": [{"APPLICATION": "BILLING"}, {"APPLICATION": "ORDERS"}]}),
                    "SELECT DISTINCT \"APPLICATION\" FROM \"jobs\" WHERE \"STATUS\" = 'FAILED'")   # a filtered list
    assert refusal(apps, "execute_sql", _sql("SELECT \"APPLICATION\", \"TEAM\" FROM \"apps\" WHERE \"APPLICATION\" "
                                             "IN ('BILLING', 'ORDERS')")) is None
    assert refusal(_support("How many jobs succeeded on 23 September?", ""), "execute_sql",
                   _sql('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'SUCCESS\'')) is None
    assert call_conditions("execute_sql", _sql("SELECT COUNT(*) FROM information_schema.tables WHERE "
                                               "table_schema = 'default'")) == []


def test_a_value_with_an_underscore_found_earlier(ctx):
    s = _support("Excel extract of the failed jobs with the team of each application", "")
    s.add_result(json.dumps({"rows": [{"APPLICATION": "BILLING_API"}]}),
                 "SELECT DISTINCT \"APPLICATION\" FROM \"jobs\" WHERE \"STATUS\" = 'FAILED'")
    assert refusal(s, "execute_sql", _sql("SELECT \"TEAM\" FROM \"apps\" WHERE \"APPLICATION\" = 'BILLING_API'")) is None


def test_each_condition_is_sent_back_once(ctx, monkeypatch):
    s = _support("How many jobs ran longer than 45 minutes on 23 September?", "")
    both = refusal(s, "execute_sql", _sql("SELECT SUM(increase) FROM dur_bucket WHERE le IN ('1800', '3600')"))
    assert "le IN '1800', '3600'" in both and "histogram bucket (le)" in both
    assert refusal(s, "execute_sql", _sql("SELECT SUM(increase) FROM dur_bucket WHERE le = '3600'")) is None
    assert refusal(s, "execute_sql", _sql("SELECT SUM(increase) FROM dur_bucket WHERE le <> '3600'"))  # the other side
    assert refusal(s, "execute_sql", _sql('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'SUCCESS\''))
    from test_agent_loop import agent_with, call, say

    rows = lambda n, args: json.dumps({"success": True, "columns": ["n"], "rows": [{"n": 337}]})  # noqa: E731
    first, variant = _sql(LATE + ' AND "STATUS" = \'SUCCESS\''), _sql(LATE + ' AND "STATUS" IN (\'SUCCESS\')')
    a, ran = agent_with(monkeypatch, [call("execute_sql", first), call("execute_sql", variant),
                                      say("337 BILLING jobs were late on 23 September.")], results=rows)
    answer, _trace = a.ask("How many BILLING jobs ran longer than 45 minutes on 23 September?")
    assert ran == [("execute_sql", variant)] and "(Check: this answer counts only STATUS IN 'SUCCESS'" in answer
