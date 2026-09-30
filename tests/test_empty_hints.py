"""An empty result says why, from the data dictionary only (no query runs): a filter value in the
wrong case or not a value of the field or label (closest values), a time window before the data
starts or after it stopped; nothing when the dictionary does not know."""

from __future__ import annotations

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.mark.parametrize("sql, expected", [
    ('SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'failed\'', "'failed' is written 'FAILED'"),
    ('SELECT COUNT(*) FROM "jobs" WHERE "NODE" IN (\'srv-7\') AND "STATUS" = \'FAILED\'',
     "'srv-7' is not a value (closest: 'srv-"),
    ('SELECT COUNT(*) FROM "jobs" WHERE ts >= \'2026-08-01\' AND ts < \'2026-08-15 00:00\'',
     "jobs has data from 2026-09-01 00:00 only"),
    ('SELECT "NODE", COUNT(*) FROM "jobs" WHERE ts BETWEEN \'2026-07-01\' AND \'2026-07-02\' GROUP BY 1',
     "the time window is before it"),
])
def test_why_an_opensearch_query_found_nothing(world, sql, expected):
    from supagent.knowledge.empty import why_empty

    hint = why_empty(world["jobs"], sql)
    assert hint.startswith("No rows. From the data dictionary:") and expected in hint


@pytest.mark.parametrize("sql", [
    'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' AND ts >= \'2026-09-10\'',   # all fine: no idea
    'SELECT COUNT(*) FROM "other-index" WHERE "STATUS" = \'nope\'',                       # not learned
    'SELECT COUNT(*) FROM "jobs" WHERE ts >= \'2026-10-01\'',       # after the data, but it may still grow
    'this is not SQL',
])
def test_nothing_is_said_when_the_dictionary_does_not_know(world, sql):
    from supagent.knowledge.empty import why_empty

    assert why_empty(world["jobs"], sql) == ""


def test_data_that_stopped_says_so(world):
    from superset.extensions import db

    from supagent.knowledge.empty import why_empty
    from supagent.models import KObject

    idx = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
    idx.stats = {**idx.stats, "profiled_at": "2026-09-28 02:00 UTC"}          # learned 4 days after the end
    db.session.commit()
    hint = why_empty(world["jobs"], 'SELECT COUNT(*) FROM "jobs" WHERE ts >= \'2026-09-26\'')
    assert "jobs has no data after 2026-09-24 23:00" in hint


@pytest.mark.parametrize("sql, expected", [
    ("SELECT DATE_TRUNC('hour', ts), SUM(rate) FROM node_cpu_seconds_total WHERE node = 'srv-4' GROUP BY 1",
     "\"node\": 'srv-4' is not a value (closest: 'srv-"),
    ("SELECT SUM(rate) FROM all_metrics WHERE metric_name = 'node_cpu_seconds_total' AND mode = 'IDLE'",
     "\"mode\": 'IDLE' is written 'idle'"),
])
def test_why_a_metrics_query_found_nothing(world, sql, expected):
    from supagent.knowledge.empty import why_empty

    assert expected in why_empty(world["metrics"], sql)


def test_why_a_promql_expression_found_nothing(world):
    from supagent.knowledge.empty import why_empty_promql

    hint = why_empty_promql(world["metrics"], 'sum by (node) (rate(node_cpu_seconds_total{mode="IDLE", '
                                              'node="srv-1"}[5m]))')
    assert "node_cpu_seconds_total \"mode\": 'IDLE' is written 'idle'" in hint and "srv-1" not in hint.split(";")[0][:40]
    assert why_empty_promql(world["metrics"], 'up{job="x"}') == ""


def test_execute_sql_gives_the_hint_with_an_empty_result(world, monkeypatch):
    from supagent import tools_superset
    from supagent.security import acting_as

    monkeypatch.setattr(tools_superset, "_run", lambda database, sql, limit, extract: (["n"], [], False))
    with acting_as("admin"):
        out = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(
            database_id=world["jobs"].id, sql='SELECT "NODE" FROM "jobs" WHERE "STATUS" = \'Failed\''))
        full = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(
            database_id=world["jobs"].id, sql='SELECT "NODE" FROM "jobs" WHERE "STATUS" = \'FAILED\''))
    assert out["success"] and out["row_count"] == 0 and "'Failed' is written 'FAILED'" in out["hint"]
    assert "hint" not in full
    monkeypatch.setattr(tools_superset, "_run", lambda database, sql, limit, extract: (["avg"], [(None,)], False))
    with acting_as("admin"):
        nulls = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(      # AVG over no rows: one NULL row
            database_id=world["jobs"].id, sql='SELECT AVG("D") FROM "jobs" WHERE "STATUS" = \'failed\''))
    assert nulls["row_count"] == 1 and "'failed' is written 'FAILED'" in nulls["hint"]


def test_the_same_value_written_otherwise_is_recognised():
    from supagent.knowledge.empty import _value_hint

    stats = {"cardinality": 3, "values": ["srv-amer-001", "srv-amer-002", "srv-emea-010"]}
    assert "'srv-amer-2' is written 'srv-amer-002'" in _value_hint("node", ["srv-amer-2"], stats, "promagg")
    assert "'SRV_EMEA_10' is written 'srv-emea-010'" in _value_hint("node", ["SRV_EMEA_10"], stats, "promagg")
    assert _value_hint("node", ["srv-amer-001"], stats, "promagg") is None


def test_a_result_the_sqls_own_limit_cut_says_it_is_not_all(world, monkeypatch):
    from supagent import tools_superset
    from supagent.security import acting_as

    rows = [(f"E{i}", 1) for i in range(100)]
    monkeypatch.setattr(tools_superset, "_run", lambda database, sql, limit, extract: (["ERROR", "n"], rows, False))
    with acting_as("admin"):
        cut = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(
            database_id=world["jobs"].id, sql='SELECT "ERROR", COUNT(*) FROM "jobs" GROUP BY 1 LIMIT 100'))
        whole = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(
            database_id=world["jobs"].id, sql='SELECT "ERROR", COUNT(*) FROM "jobs" GROUP BY 1'))
    assert "The SQL's own LIMIT 100 was reached" in cut["note"] and "note" not in whole
