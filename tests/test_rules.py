"""The team's rules are applied: given again next to the question (in full when short), a learned
answer yields to them, and a rule that filters on a field ("ENVIRONMENT_TYPE = 'UAT'") must be used
by every query on data that has that field, unless the question asks for the rule's value: the
answer is sent back once, then marked. A count of 0 over a value that does not exist says so."""

from __future__ import annotations

import json
import types

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)

RULE = "Exclude the UAT environment (ENVIRONMENT_TYPE = 'UAT') unless the question asks for UAT."


@pytest.fixture()
def ruled(world):
    from superset.extensions import db

    from supagent.knowledge.catalog import save_entry
    from supagent.knowledge.store import upsert
    from supagent.models import Entry, EntryVersion, KObject

    upsert(world["run"], world["s_jobs"], "field", "jobs", "ENVIRONMENT_TYPE",
           {"data_type": "keyword", "stats": {"cardinality": 3, "values": ["DEV", "PROD", "UAT"]}})
    upsert(world["run"], world["s_jobs"], "field", "jobs", "APPLICATION",
           {"data_type": "keyword", "stats": {"cardinality": 2, "values": ["BILLING", "PAYROLL"]}})
    db.session.commit()
    save_entry({"title": "No UAT", "classification": "rule", "content": RULE}, by="admin")
    yield world
    db.session.query(EntryVersion).delete()
    db.session.query(Entry).delete()
    db.session.query(KObject).filter(KObject.name.in_(["ENVIRONMENT_TYPE", "APPLICATION"])).delete()
    db.session.commit()


def _step(sql: str, tool: str = "execute_sql") -> dict:
    return {"tool": tool, "called": tool, "status": "done", "args": {"request": {"database_id": 1, "sql": sql}}}


def test_a_filtering_rule_is_checked_on_the_queries(ruled):
    from supagent.knowledge.rulecheck import unapplied

    q = "How many jobs failed yesterday?"
    without = _step('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\'')
    applied = _step('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND "ENVIRONMENT_TYPE" <> \'UAT\'')
    prod = _step('SELECT COUNT(*) FROM "jobs" WHERE "ENVIRONMENT_TYPE" = \'PROD\'')
    other = _step('SELECT COUNT(*) FROM "servers" WHERE "STATUS" = \'DOWN\'')            # no such field there
    assert unapplied(q, [without]) == [RULE]
    assert unapplied(q, [applied]) == [] and unapplied(q, [prod]) == [] and unapplied(q, [other]) == []
    assert unapplied("How many jobs failed in UAT yesterday?", [without]) == []           # the question asks
    failed = dict(without, status="error")
    assert unapplied(q, [failed]) == []                                                    # it did not run


def test_the_rules_come_again_next_to_the_question(ruled):
    from supagent.agent import Agent, ChartGuard
    from supagent.security import acting_as

    a = object.__new__(Agent)
    a.username, a.rich, a.wants_saved_chart = "admin", True, False
    a.superset = types.SimpleNamespace(available=True, error=None)
    a.guard = ChartGuard(a)
    with acting_as("admin"):
        blocks = a._question_blocks("How many jobs failed yesterday?", set())
    assert "The team's rules, for every query of this answer (they win over the learned answers" in blocks
    assert RULE in blocks


def ROWS(name, args):  # noqa: N802
    return json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 12}]})
WITHOUT = {"request": {"database_id": 1, "sql": 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\''}}
WITH = {"request": {"database_id": 1, "sql": 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND '
                                             '"ENVIRONMENT_TYPE" <> \'UAT\''}}


def test_a_query_without_the_rule_is_sent_back_before_it_runs(ruled, monkeypatch):
    from test_agent_loop import agent_with, call, say

    a, ran = agent_with(monkeypatch, [call("execute_sql", WITHOUT), call("execute_sql", WITH),
                                      say("Without UAT, 12 jobs failed.")], results=ROWS)
    answer, trace = a.ask("How many jobs failed?")
    assert answer == "Without UAT, 12 jobs failed." and ran == [("execute_sql", WITH)]      # the first never ran
    assert trace[0]["status"] == "error" and "not run: team rule" in trace[0]["result"]
    assert "\"ENVIRONMENT_TYPE\" <> 'UAT' in the WHERE" in trace[0]["result"]            # what to add
    assert a.usage["nudges"] == 1


def test_a_query_sent_again_unchanged_runs_and_the_answer_is_marked(ruled, monkeypatch):
    from test_agent_loop import agent_with, call, say

    b, ran = agent_with(monkeypatch, [
        call("execute_sql", WITHOUT),
        call("execute_sql", WITHOUT),          # sent again unchanged: the question may ask for it
        say("12 jobs failed."),
        say("12 jobs failed, all environments."),
    ], results=ROWS)
    answer, _trace = b.ask("How many jobs failed?")
    assert ran == [("execute_sql", WITHOUT)]
    assert answer.endswith(f'(Check: the team\'s rule "{RULE}" was not applied in this answer\'s queries.)')


def test_a_question_that_asks_for_the_rules_value_is_not_sent_back(ruled, monkeypatch):
    from test_agent_loop import agent_with, call, say

    c, ran = agent_with(monkeypatch, [call("execute_sql", WITHOUT), say("12 jobs failed in all, UAT included.")],
                        results=ROWS)
    answer, trace = c.ask("How many jobs failed, UAT included?")
    assert ran == [("execute_sql", WITHOUT)] and trace[0]["status"] == "done" and "(Check:" not in answer


def test_the_example_follows_the_rule_and_the_tool(ruled):
    from supagent.knowledge.rulecheck import _detail, _suggestion, call_refusal

    d = _detail({"text": RULE}, {"ENVIRONMENT_TYPE"}, ["UAT"], {"jobs"})
    assert (d["field"], d["value"], d["exclude"]) == ("ENVIRONMENT_TYPE", "UAT", True)
    assert _suggestion(d, "promql_query") == '{ENVIRONMENT_TYPE!="UAT"} in the selector'
    assert json.loads(_suggestion(d, "generate_chart").split(" in the filters")[0]) == {
        "column": "ENVIRONMENT_TYPE", "op": "!=", "value": "UAT"}
    only = _detail({"text": "Only count PROD jobs (ENVIRONMENT_TYPE = 'PROD'), never DEV."}, {"ENVIRONMENT_TYPE"},
                   ["PROD"], {"jobs"})
    assert (only["value"], only["exclude"]) == ("PROD", False)
    assert call_refusal("How many jobs failed?", "execute_sql", WITH) is None
    assert call_refusal("How many jobs failed?", "list_charts", {}) is None


def test_a_chart_on_the_table_needs_the_rules_filter(ruled):
    from supagent.knowledge.rulecheck import chart_refusal

    table = types.SimpleNamespace(table_name="jobs", sql=None)
    query = types.SimpleNamespace(table_name="failed", sql='SELECT * FROM "jobs"')
    bar = {"chart_type": "xy", "x": {"name": "APPLICATION"}, "y": [{"name": "count", "saved_metric": True}]}
    refused = chart_refusal("Chart of the failed jobs per application", table, bar)
    assert refused and '{"column": "ENVIRONMENT_TYPE", "op": "!=", "value": "UAT"}' in refused
    filtered = dict(bar, filters=[{"column": "ENVIRONMENT_TYPE", "op": "!=", "value": "UAT"}])
    per_env = dict(bar, group_by=[{"name": "ENVIRONMENT_TYPE"}])
    assert chart_refusal("Chart of the failed jobs per application", table, filtered) is None
    assert chart_refusal("Chart of the failed jobs per environment", table, per_env) is None     # each one shown
    assert chart_refusal("Chart of the failed jobs per application", query, bar) is None         # its SQL decides
    assert chart_refusal("Chart of the failed UAT jobs per application", table, bar) is None     # asked for UAT


def test_a_count_of_zero_over_a_value_that_does_not_exist_says_so(ruled):
    from supagent.knowledge.empty import why_empty

    sql = 'SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" = \'ZEPHYR\''
    hint = why_empty(ruled["jobs"], sql, counted=True)
    assert hint.startswith("Nothing matched (0).") and "'ZEPHYR' is not a value" in hint
    assert "rather than a count of 0" in hint
    assert why_empty(ruled["jobs"], 'SELECT COUNT(*) FROM "jobs" WHERE "APPLICATION" = \'BILLING\'', counted=True) == ""


def test_the_last_query_on_the_data_decides(ruled):
    from supagent.knowledge.rulecheck import unapplied

    first = _step('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\'')
    again = _step('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND "ENVIRONMENT_TYPE" <> \'UAT\'')
    assert unapplied("How many jobs failed?", [first, again]) == []
    assert unapplied("How many jobs failed?", [again, first]) == [RULE]


def test_a_rule_that_defines_a_value_never_sends_an_answer_back(ruled):
    from supagent.knowledge.catalog import save_entry
    from supagent.knowledge.rulecheck import unapplied

    save_entry({"title": "Failed means", "classification": "rule",
                "content": "ENVIRONMENT_TYPE = 'PROD' is the production; STATUS = 'KO' means the job failed."}, by="admin")
    q = "How many jobs failed yesterday?"
    applied = _step('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND "ENVIRONMENT_TYPE" <> \'UAT\'')
    assert unapplied(q, [applied]) == []                              # the definition is not a filter to apply


def test_the_metrics_of_a_promql_expression():
    from supagent.knowledge.rulecheck import promql_metrics

    assert promql_metrics("avg(fed_temperature)") == {"fed_temperature"}
    assert promql_metrics('sum by (application) (rate(http_requests_total{code=~"5.."}[1d])) / '
                          "sum by (application) (rate(http_requests_total[1d])) * 100") == {"http_requests_total"}
    assert promql_metrics("up == 0 or on(instance) node_load1 > 4") == {"up", "node_load1"}
    assert promql_metrics("histogram_quantile(0.95, sum by (le) (rate(job_seconds_bucket[5m])))") == {
        "job_seconds_bucket"}


def test_a_count_over_a_counter_is_sent_back(world):
    from supagent.knowledge.experience import count_of_counter

    metrics = world["metrics"]
    sql = ("SELECT node, COUNT(*) FILTER (WHERE mode = 'idle') AS idle FROM \"node_cpu_seconds_total\" "
           "WHERE ts >= TIMESTAMP '2026-09-23 00:00' GROUP BY node")
    text = count_of_counter(metrics, sql)
    assert text and text.startswith("tool error (not run: counter)") and "SUM(increase)" in text
    assert count_of_counter(metrics, "SELECT node, SUM(increase) FROM \"node_cpu_seconds_total\" GROUP BY node") is None
    assert count_of_counter(world["jobs"], sql) is None                                 # not a metrics database


VAR_RULE = ("The VaR of a desk is its record with BOOK = 'ALL'. Never add up the VaR of the books of a desk "
            "(VaR is not additive).")


@pytest.fixture()
def desks(world):
    """Three indices with a BOOK field (PnL, risk, trades); only the risk index's values say VaR."""
    from superset.extensions import db

    from supagent.knowledge import rulecheck
    from supagent.knowledge.catalog import save_entry
    from supagent.knowledge.store import upsert
    from supagent.models import Entry, EntryVersion, KObject

    made = []
    for index, fields in (("pnl", {"BOOK": ["EQ_VANILLA_EU", "FX_SPOT_G10"], "PNL_STATUS": ["FLASH", "OFFICIAL"],
                                   "VALIDATION": ["NOT_VALIDATED", "VALIDATED"]}),       # "not" is in the rule too
                          ("risk", {"BOOK": ["ALL", "EQ_VANILLA_EU"], "RISK_MEASURE": ["VAR_1D_99", "VEGA", "DELTA"]}),
                          ("trades", {"BOOK": ["EQ_VANILLA_EU"], "STATUS": ["NEW", "CANCELLED"]}),
                          ("pricing", {"BOOK": ["FX_SPOT_G10"], "STATUS": ["OK", "ERROR"],
                                       "ERROR_CODE": ["INVALID_TRADE", "TIMEOUT"]})):
        made.append(upsert(world["run"], world["s_jobs"], "index", "", index, {"stats": {"time_field": "COB_DATE"}}))
        for name, values in fields.items():
            made.append(upsert(world["run"], world["s_jobs"], "field", index, name,
                               {"data_type": "keyword", "stats": {"values": values, "cardinality": len(values)}}))
    db.session.commit()
    save_entry({"title": "VaR of a desk", "classification": "rule", "content": VAR_RULE}, by="admin")
    rulecheck._CONCERNS.clear()
    yield world
    db.session.query(EntryVersion).delete()
    db.session.query(Entry).delete()
    for o in made:
        db.session.delete(o)
    db.session.commit()
    rulecheck._CONCERNS.clear()


def test_a_rule_applies_to_the_data_it_is_about_and_never_to_a_list_per_its_field(desks):
    """A rule on BOOK that is about VaR is not added to PnL or trades queries (the lab's governed pipeline found
    nothing: BOOK = 'ALL' rows exist only in risk), and not to a query per BOOK."""
    from supagent.knowledge.rulecheck import concerns, unapplied

    assert concerns({"text": VAR_RULE}, {"BOOK"}) == {"risk"}           # not pnl for its NOT_VALIDATED
    q = "What was the VaR of the EQUITY desk on 23 September?"
    risk = _step('SELECT SUM("VALUE") FROM "risk" WHERE "RISK_MEASURE" = \'VAR\'')
    assert unapplied(q, [risk]) == [VAR_RULE]
    trades = _step('SELECT COUNT(*) FROM "trades" WHERE "STATUS" = \'NEW\'')
    assert unapplied("How many trades did the desk book?", [trades]) == []
    per_book = _step('SELECT "BOOK", SUM("VALUE") FROM "risk" WHERE "RISK_MEASURE" = \'VAR\' GROUP BY "BOOK"')
    assert unapplied("VaR per book on 23 September?", [per_book]) == []


def test_the_governed_plan_gets_a_rule_only_where_it_belongs(desks):
    from supagent.governed.decider import Pack, TableInfo
    from supagent.governed.plan import Step
    from supagent.governed.validate import Checked, _apply_rules

    def table(name):
        return TableInfo(subject=f"data:1:{name}", ref="T1", kind="index", database_id=1, database="lab",
                         backend="osagg", name=name, columns={"BOOK": {}, "DESK": {}})

    pack = Pack(tables=[], knowledge=[], ambiguous=[], missing=[], kind="data", confidence="high")

    def added(name, by=()):
        step = Step(id="q1", table="T1", by=list(by))
        out = Checked(plan=None)
        _apply_rules(step, table(name), "What was the desk's total?", pack, out)
        return [(c.field, c.value) for c in step.where]

    assert added("trades") == [] and added("pnl") == []                    # not about their data
    assert added("risk") == [("BOOK", "ALL")]                               # the VaR of a desk
    assert added("risk", by=["BOOK"]) == []                                 # per book: every book


def test_a_rule_whose_value_cannot_be_in_the_data_is_not_applied_there(desks):
    """The excluding rule on STATUS = 'CANCELLED' concerns trades (its statuses have CANCELLED), not the pricing
    index, whose statuses are all known and have none (the lab's answers said "STATUS = CANCELLED: no such value")."""
    from supagent.knowledge import rulecheck
    from supagent.knowledge.catalog import save_entry

    save_entry({"title": "Cancelled trades", "classification": "rule", "content":
                "Exclude cancelled trades (STATUS = 'CANCELLED') from trade counts unless the question asks."}, by="admin")
    rulecheck._CONCERNS.clear()
    assert rulecheck.value_possible("pricing", "STATUS", "CANCELLED") is False
    assert rulecheck.value_possible("trades", "STATUS", "cancelled") is True
    assert rulecheck.possible_in({"values": ["A", "B"], "cardinality": ">=200"}, "C") is None     # not all known
    q = "How many pricing requests failed?"
    assert rulecheck.unapplied(q, [_step('SELECT COUNT(*) FROM "pricing" WHERE "STATUS" = \'ERROR\'')]) == []
    trades = rulecheck.unapplied("How many trades?", [_step('SELECT COUNT(*) FROM "trades"')])
    assert any("CANCELLED" in r for r in trades)
