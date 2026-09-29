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


def test_an_answer_that_skipped_the_rule_is_sent_back_once_then_marked(ruled, monkeypatch):
    from test_agent_loop import agent_with, call, say

    rows = lambda name, args: json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 12}]})  # noqa
    a, ran = agent_with(monkeypatch, [
        call("execute_sql", {"request": {"database_id": 1, "sql": 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = '
                                                                  "'FAILED'"}}),
        say("12 jobs failed."),
        call("execute_sql", {"request": {"database_id": 1, "sql": 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = '
                                                                  "'FAILED' AND \"ENVIRONMENT_TYPE\" <> 'UAT'"}}),
        say("Without UAT, 12 jobs failed."),
    ], results=rows)
    answer, _trace = a.ask("How many jobs failed yesterday?")
    assert answer == "Without UAT, 12 jobs failed." and len(ran) == 2
    assert any("the team's rule" in (m["content"] or "") for m in a.llm.seen[-1] if m["role"] == "user")

    b, _ran = agent_with(monkeypatch, [
        call("execute_sql", {"request": {"database_id": 1, "sql": 'SELECT COUNT(*) FROM "jobs"'}}),
        say("12 jobs."),
        say("12 jobs, all environments."),
    ], results=rows)
    answer, _trace = b.ask("How many jobs ran yesterday?")
    assert answer.endswith(f'(Check: the team\'s rule "{RULE}" was not applied in this answer\'s queries.)')


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
