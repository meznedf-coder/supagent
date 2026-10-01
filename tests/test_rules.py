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
    """Three indices with a BOOK field (PnL, risk, trades); only the risk index's values say VaR. And a shop's
    orders, whose STATUS has CANCELLED like the trades'."""
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
                                       "ERROR_CODE": ["INVALID_TRADE", "TIMEOUT"]}),
                          ("shop-orders", {"CHANNEL": ["WEB", "TEST"],
                                           "STATUS": ["PAID", "CANCELLED", "PAYMENT_FAILED"]})):
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


TEST_RULE = ("Orders of the channel TEST (CHANNEL = 'TEST') are made by the QA team: never count them in sales, "
             "revenue, order counts or rates (CHANNEL <> 'TEST'), unless the question asks for the test orders "
             "themselves.")
CANCELLED_RULE = ("Exclude cancelled trades (STATUS = 'CANCELLED') from trade counts and notionals unless the "
                  "question asks about cancellations.")


def test_the_way_a_rule_writes_its_condition():
    """A rule that first says which values it is about, then leaves them out (the retail lab's: the governed
    pipeline counted only the test orders, CHANNEL = 'TEST'), and the other ways rules write it."""
    from supagent.knowledge.rulecheck import rule_conditions

    assert rule_conditions(TEST_RULE) == [("CHANNEL", "TEST", True)]
    assert rule_conditions(RULE) == [("ENVIRONMENT_TYPE", "UAT", True)]
    assert rule_conditions("Exclude the UAT environment (ENVIRONMENT_TYPE = 'UAT', label env = 'UAT').") == [
        ("ENVIRONMENT_TYPE", "UAT", True), ("env", "UAT", True)]
    assert rule_conditions(VAR_RULE) == [("BOOK", "ALL", False)]
    assert rule_conditions("Only count PROD jobs (ENVIRONMENT_TYPE = 'PROD'), never DEV.") == [
        ("ENVIRONMENT_TYPE", "PROD", False)]
    assert rule_conditions("Cancelled orders (STATUS = 'CANCELLED') are never counted as sales.") == [
        ("STATUS", "CANCELLED", True)]
    assert rule_conditions("The PnL of a COB is the official one (PNL_STATUS = 'OFFICIAL') unless the question "
                           "asks for the flash PnL.") == [("PNL_STATUS", "OFFICIAL", False)]
    assert rule_conditions("Never add up the books (VaR is not additive). The VaR of a desk is its record with "
                           "BOOK = 'ALL'.") == [("BOOK", "ALL", False)]          # the sentence before is another
    assert rule_conditions("Use BOOK = 'ALL' for the VaR of a desk, not the sum of the books.") == [
        ("BOOK", "ALL", False)]
    assert rule_conditions("Live trades have STATUS NOT IN ('CANCELLED', 'REJECTED').")[0] == (
        "STATUS", "CANCELLED", True)


def test_a_rule_that_names_its_data_is_about_that_data_only(desks):
    """"Exclude cancelled trades (STATUS = 'CANCELLED')" is about the trades, not the shop's orders whose STATUS
    has CANCELLED too (the retail lab's governed answers took the trades' rule to the orders); a rule that
    leaves a value out is added that way, and not where the question says that field for what it counts."""
    from supagent.governed.decider import Pack, TableInfo
    from supagent.governed.plan import Cond, Measure, Step
    from supagent.governed.validate import Checked, _apply_rules
    from supagent.knowledge import rulecheck
    from supagent.knowledge.catalog import save_entry

    save_entry({"title": "Cancelled trades", "classification": "rule", "content": CANCELLED_RULE}, by="admin")
    save_entry({"title": "Test orders", "classification": "rule", "content": TEST_RULE}, by="admin")
    rulecheck._CONCERNS.clear()
    assert rulecheck.concerns({"text": CANCELLED_RULE}, {"STATUS"}) == {"trades"}
    orders = TableInfo(subject="data:1:shop-orders", ref="T1", kind="index", database_id=1, database="lab",
                       backend="osagg", name="shop-orders",
                       columns={"CHANNEL": {"values": ["WEB", "TEST"]},
                                "STATUS": {"values": ["PAID", "CANCELLED", "PAYMENT_FAILED"]}})
    pack = Pack(tables=[], knowledge=[], ambiguous=[], missing=[], kind="data", confidence="high")
    step = Step(id="q1", table="T1")
    out = Checked(plan=None)
    _apply_rules(step, orders, "How many orders failed at payment on 23 September?", pack, out)
    assert [(c.field, c.op, c.value) for c in step.where] == [("CHANNEL", "!=", "TEST")]
    web = Step(id="q1", table="T1", measures=[Measure(label="web orders", fn="count", where=[
        Cond(field="CHANNEL", op="=", value="WEB", source="question: web orders")])])
    _apply_rules(web, orders, "How many web orders did we sell on 23 September?", pack, Checked(plan=None))
    assert web.where == []


def test_a_value_the_question_names_and_no_query_uses_is_sent_back(ruled, monkeypatch):
    """"How many web orders did we sell on 23 September?" counted over every channel (the retail lab): a value the
    question names in the data the queries read (as the data writes it, code-like, or before the table's subject)
    that no query writes nor groups by is sent back once, then marked."""
    from test_agent_loop import agent_with, call, say

    from supagent.knowledge.named import unused

    failed = 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\''
    assert unused("How many billing jobs failed?", [failed]) == [("BILLING", "APPLICATION", "jobs")]
    assert unused("How many PAYROLL jobs failed?", [failed]) == [("PAYROLL", "APPLICATION", "jobs")]
    assert unused("How many billing jobs failed?", [failed + " AND \"APPLICATION\" = 'BILLING'"]) == []
    assert unused("How many jobs failed per application?", [failed]) == []
    assert unused("Failed jobs by application, billing first?", [failed + ' GROUP BY "APPLICATION"']) == []
    assert unused("How many jobs failed on the billing side?", [failed]) == []          # an ordinary word
    assert unused("Billing application: how many jobs failed?", [failed]) == [("BILLING", "APPLICATION", "jobs")]
    assert unused("How many jobs failed, UAT included?", [failed]) == []                 # with the others
    assert unused("How many jobs failed including UAT?", [failed]) == []
    ruled_sql = failed + " AND \"ENVIRONMENT_TYPE\" <> 'UAT'"                       # the fixture's rule applied
    plain = {"request": {"database_id": 1, "sql": ruled_sql}}
    billing = {"request": {"database_id": 1, "sql": ruled_sql + " AND \"APPLICATION\" = 'BILLING'"}}
    a, ran = agent_with(monkeypatch, [call("execute_sql", plain), say("12 jobs failed."),
                                      call("execute_sql", billing), say("3 BILLING jobs failed.")], results=ROWS)
    answer, _trace = a.ask("How many billing jobs failed?")
    assert [r[1] for r in ran] == [plain, billing] and answer == "3 BILLING jobs failed."
    b, ran = agent_with(monkeypatch, [call("execute_sql", plain), say("12 jobs failed."), say("12 jobs failed.")],
                        results=ROWS)
    answer, _trace = b.ask("How many billing jobs failed?")
    assert answer.endswith("(Check: the question names BILLING (APPLICATION); no query of this answer filters on it.)")


def test_a_follow_up_keeps_the_previous_questions_conditions(ruled, monkeypatch):
    """"when I say billing you should know the filter is APPLICATION=BILLING ... show me now only billing", after
    an answer counted with a label: the new queries added the application and dropped the label. The previous
    answer's conditions on the same table are sent back once, then said under the answer."""
    from test_agent_loop import agent_with, call, say

    from supagent.knowledge.carry import dropped

    base = 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND "ENVIRONMENT_TYPE" <> \'UAT\''
    prev = base + " AND \"LABEL\" = 'D'"
    history = [{"role": "user", "content": "How many jobs failed, label D?"},
               {"role": "assistant", "content": "12 jobs failed.", "queries": [{"query": prev}]}]
    q = "when I say billing you should know the filter is APPLICATION=BILLING, show me only billing"
    billing = base + " AND \"APPLICATION\" = 'BILLING'"
    assert [c.said() for c in dropped(q, [prev], [billing])] == ["LABEL = 'D'"]
    assert dropped(q, [prev], [billing + " AND \"LABEL\" = 'D'"]) == []
    assert dropped("Now all labels, only billing", [prev], [billing]) == []           # removed on purpose
    assert dropped("Only billing, label D-1 instead", [prev], [billing]) == []        # the message changes it
    assert dropped(q, [prev], ['SELECT COUNT(*) FROM "other" WHERE "APPLICATION" = \'BILLING\'']) == []

    plain = {"request": {"database_id": 1, "sql": billing}}
    kept = {"request": {"database_id": 1, "sql": billing + " AND \"LABEL\" = 'D'"}}
    a, ran = agent_with(monkeypatch, [call("execute_sql", plain), say("3 BILLING jobs failed."),
                                      call("execute_sql", kept), say("2 BILLING jobs failed with label D.")],
                        results=ROWS)
    answer, _trace = a.ask(q, history)
    assert [r[1] for r in ran] == [plain, kept] and answer == "2 BILLING jobs failed with label D."
    b, ran = agent_with(monkeypatch, [call("execute_sql", plain), say("3 BILLING jobs failed."),
                                      say("3 BILLING jobs failed.")], results=ROWS)
    answer, _trace = b.ask(q, history)
    assert answer.endswith("(Check: the previous question counted with LABEL = 'D'; this answer does not.)")


def test_a_memory_said_in_other_words_is_learned():
    from supagent.knowledge.memory import SIGNALS

    for said in ("when I say BILLING you should know the filter is APPLICATION=BILLING, make it in your memory",
                 "Keep this in your memory: PROD only", "Mémorise que PROD veut dire production"):
        assert SIGNALS.search(said), said
