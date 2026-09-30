"""Several databases with the same index or metric: the question, its charts and dashboards decide which
one is queried. A database the question names (its own words next to a word for a database, or a tenant
only it has) and the database of a chart or dashboard it is about are evidence: a query on the same table
in another database is sent back once (sent again unchanged, it runs). Otherwise "Where the data is"
states the database used and why (the admins' list, the catalog's metrics database, the team's charts)."""

from __future__ import annotations

import json

import pytest

from test_knowledge import _database, world  # noqa: F401  (the fixture)


@pytest.fixture()
def several(world):
    """world + a replica of the jobs database (same index) with a dashboard on it, and a federated metrics
    database (same metric, one more tenant)."""
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from supagent.knowledge.store import source_for, upsert

    replica = _database("jobs DR replica", "osagg://127.0.0.2:9200/?timezone=Europe/Paris")
    federated = _database("metrics federated", "promagg://127.0.0.1:9009/prometheus?tenant=ops%7Cfed-a")
    token = _database("metrics via gateway: token", "promagg://127.0.0.1:9444/prometheus")
    run = world["run"]
    s_rep, s_fed = source_for(replica), source_for(federated)
    upsert(run, s_rep, "index", "", "jobs", {"stats": {"docs": 1000, "time_field": "ts"}})
    upsert(run, s_rep, "field", "jobs", "STATUS", {"data_type": "keyword",
                                                    "stats": {"cardinality": 2, "values": ["FAILED", "SUCCESS"]}})
    upsert(run, world["s_prom"], "label", "node_cpu_seconds_total", "__tenant_id__",
           {"data_type": "string", "stats": {"cardinality": 1, "values": ["ops"]}})
    upsert(run, s_fed, "metric", "", "node_cpu_seconds_total", {"metric_type": "counter", "stats": {"series": 64}})
    upsert(run, s_fed, "label", "node_cpu_seconds_total", "__tenant_id__",
           {"data_type": "string", "stats": {"cardinality": 2, "values": ["ops", "fed-a"]}})
    ds = SqlaTable(table_name="jobs", database_id=replica.id)
    db.session.add(ds)
    db.session.flush()
    chart = Slice(slice_name="Replica failed jobs by node", viz_type="table", datasource_id=ds.id,
                  datasource_type="table", params="{}")
    board = Dashboard(dashboard_title="Replica night health", slices=[chart])
    db.session.add_all([chart, board])
    db.session.commit()
    ids = {"replica": replica.id, "federated": federated.id, "token": token.id, "dataset": ds.id, "chart": chart.id,
           "board": board.id, "jobs": world["jobs"].id, "metrics": world["metrics"].id}
    from supagent.knowledge import resolve

    resolve._CACHE.clear()
    yield {**world, **ids}
    db.session.rollback()
    for model, ident in ((Dashboard, ids["board"]), (Slice, ids["chart"]), (SqlaTable, ids["dataset"])):
        obj = db.session.get(model, ident)
        if obj is not None:
            db.session.delete(obj)
    db.session.commit()
    resolve._CACHE.clear()


def _dbs():
    from supagent.knowledge.resolve import _databases

    return _databases()


def test_a_database_the_question_names(several):
    from supagent.knowledge.scope import named_databases, tenant_databases
    from supagent.security import acting_as

    with acting_as("admin"):
        dbs = _dbs()
        assert list(named_databases("How many jobs failed on the DR replica cluster yesterday?", dbs)) == [
            several["replica"]]
        assert list(named_databases("CPU of srv-1 in the federated metrics database", dbs)) == [several["federated"]]
        assert named_databases("How many jobs failed yesterday?", dbs) == {}
        assert named_databases("How many license tokens are in use?", dbs) == {}     # "token": no word for a database
        assert list(tenant_databases("CPU busy % of the fed-a tenant yesterday", dbs)) == [several["federated"]]
        assert tenant_databases("CPU busy % of the ops tenant", dbs) == {}              # ops: in both


def test_where_the_data_is_says_which_database_and_why(several):
    from supagent import settings
    from supagent.knowledge.resolve import where_block
    from supagent.security import acting_as

    with acting_as("admin"):
        plain = where_block("How many jobs failed yesterday?")
        named = where_block("How many jobs failed yesterday on the DR replica cluster?")
    line = next(x for x in plain.splitlines() if x.startswith('- index "jobs"'))
    assert f'in database {several["replica"]} "jobs DR replica"' in line      # the team's chart is on the replica
    assert f'(also in database {several["jobs"]} "jobs": use database {several["replica"]} unless the question' in line
    assert "the team's charts use it (1)" in line
    line = next(x for x in named.splitlines() if x.startswith('- index "jobs"'))
    assert f'database {several["replica"]} "jobs DR replica"' in line and "the question names database" in line
    settings.set_value("agent.preferred_databases", ["jobs"])
    try:
        from supagent.knowledge import resolve

        resolve._CACHE.clear()
        with acting_as("admin"):
            preferred = where_block("How many jobs failed yesterday?")
    finally:
        settings.set_value("agent.preferred_databases", [])
    line = next(x for x in preferred.splitlines() if x.startswith('- index "jobs"'))
    assert f'in database {several["jobs"]} "jobs"' in line and "the admins' preferred database" in line


def test_a_dashboard_of_the_question_binds_its_tables(several):
    from supagent.knowledge.scope import refusal, scope_for
    from supagent.security import acting_as

    on = lambda i: {"request": {"database_id": i, "sql": 'SELECT COUNT(*) FROM "jobs"'}}  # noqa: E731
    with acting_as("admin"):
        scope = scope_for("Is everything normal on the dashboard 'Replica night health'?")
        assert scope.bound == {"jobs": {several["replica"]: f'dashboard {several["board"]} "Replica night health"'}}
        assert "its charts read jobs in database" in scope.block()
        sent_back = refusal(scope, "execute_sql", on(several["jobs"]))
        assert sent_back.startswith("tool error (not run: another database)")
        assert f'database {several["replica"]} "jobs DR replica"' in sent_back and "database_id=" in sent_back
        assert refusal(scope, "execute_sql", on(several["replica"])) is None
        other = {"request": {"database_id": several["metrics"], "sql": 'SELECT * FROM "node_cpu_seconds_total"'}}
        assert refusal(scope, "execute_sql", other) is None                    # not a table of the dashboard
        by_id = scope_for(f"What does chart {several['chart']} show?")
        assert list(by_id.bound["jobs"]) == [several["replica"]]
        assert scope_for("How many jobs failed yesterday?").bound == {}


def test_a_named_database_sends_back_a_query_on_another_one(several):
    from supagent.knowledge.scope import refusal, scope_for
    from supagent.security import acting_as

    sql = 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\''
    with acting_as("admin"):
        scope = scope_for("How many jobs failed on the DR replica cluster?")
        assert refusal(scope, "execute_sql", {"request": {"database_id": several["jobs"], "sql": sql}})
        assert refusal(scope, "execute_sql", {"request": {"database_id": several["replica"], "sql": sql}}) is None
        # a metric only the other databases have: not the named one's, not sent back
        cpu = {"request": {"database_id": several["metrics"], "sql": 'SELECT * FROM "node_cpu_seconds_total"'}}
        assert refusal(scope, "execute_sql", cpu) is None
        fed = scope_for("CPU busy % of the fed-a tenant yesterday")
        assert refusal(fed, "promql_query", {"expr": "sum(rate(node_cpu_seconds_total[5m]))",
                                             "database": str(several["metrics"])})
        assert refusal(fed, "promql_query", {"expr": "sum(rate(node_cpu_seconds_total[5m]))",
                                             "database": str(several["federated"])}) is None
        # a metric inside a function, no matcher, no database given (the tools' default: another one)
        assert refusal(fed, "compare_to_usual", {"promql": "avg(node_cpu_seconds_total)", "start": "2026-09-23 00:00",
                                                 "end": "2026-09-23 06:00", "database": str(several["metrics"])})


def test_a_dashboard_read_by_a_tool_binds_too(several):
    from supagent.knowledge.scope import Scope, learn_from_call, refusal
    from supagent.knowledge.resolve import _databases
    from supagent.security import acting_as

    with acting_as("admin"):
        scope = Scope(names={d.id: d.database_name for d in _databases()})
        learn_from_call(scope, "get_dashboard_info", {"request": {"identifier": several["board"]}})
        assert list(scope.bound["jobs"]) == [several["replica"]]
        assert refusal(scope, "execute_sql", {"request": {"database_id": several["jobs"], "sql": 'SELECT 1 FROM "jobs"'}})


def test_the_agent_sends_the_call_back_once(several, monkeypatch):
    from test_agent_loop import agent_with, call, say

    wrong = {"request": {"database_id": several["jobs"], "sql": 'SELECT COUNT(*) FROM "jobs"'}}
    right = {"request": {"database_id": several["replica"], "sql": 'SELECT COUNT(*) FROM "jobs"'}}
    rows = lambda name, args: json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 7}]})  # noqa
    a, ran = agent_with(monkeypatch, [call("execute_sql", wrong), call("execute_sql", right), say("7 jobs.")],
                        results=rows)
    from supagent.security import acting_as

    with acting_as("admin"):
        answer, trace = a.ask("How many jobs are on the dashboard 'Replica night health'?")
    assert answer == "7 jobs." and ran == [("execute_sql", right)]
    assert trace[0]["status"] == "error" and "another database" in trace[0]["result"]
    b, ran = agent_with(monkeypatch, [call("execute_sql", wrong), call("execute_sql", wrong), call("execute_sql", right),
                                      say("7 jobs.")], results=rows)
    with acting_as("admin"):
        answer, trace = b.ask("How many jobs are on the dashboard 'Replica night health'?")
    # sent again unchanged: still not run (a database the question did not name is never read)
    assert ran == [("execute_sql", right)] and [t["status"] for t in trace] == ["error", "error", "done"]
    with acting_as("admin"):
        from supagent.knowledge.scope import named_databases

        assert list(named_databases(f"How many jobs are in database {several['jobs']}?", _dbs())) == [several["jobs"]]


def test_a_named_database_not_learned_knows_its_indices_live(several, monkeypatch):
    from supagent.knowledge import scope as S
    from supagent.security import acting_as

    archive = _database("jobs archive cluster", "osagg://127.0.0.3:9200/?timezone=Europe/Paris")
    monkeypatch.setattr(S, "_index_names", lambda d: {"jobs"} if d.id == archive.id else set())
    sql = 'SELECT COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\''
    with acting_as("admin"):
        scope = S.scope_for("How many jobs failed in the archive cluster?")
        assert list(scope.named) == [archive.id]
        sent_back = S.refusal(scope, "execute_sql", {"request": {"database_id": several["jobs"], "sql": sql}})
        assert sent_back and f'database {archive.id} "jobs archive cluster"' in sent_back
        assert S.refusal(scope, "execute_sql", {"request": {"database_id": archive.id, "sql": sql}}) is None


def test_a_value_in_two_kinds_of_data_is_shown_where_it_is(world):
    from supagent.knowledge.resolve import values_lines
    from supagent.security import acting_as

    with acting_as("admin"):
        lines = values_lines("What happened on srv-1 on 23 September?")
        assert len(lines) == 1 and lines[0].startswith('- "srv-1" is a value of: field NODE of index jobs')
        assert "label node of 1 metric(s): node_cpu_seconds_total" in lines[0] and "name the other one" in lines[0]
        assert values_lines("How many jobs failed on srv-1?") == []            # the question says which data
        assert values_lines("What happened on srv-4?") == []                   # only in the jobs index
    with acting_as("alice"):                                                   # the jobs database only
        assert values_lines("What happened on srv-1 on 23 September?") == []


def test_words_of_data_do_not_name_a_database(several):
    from supagent.knowledge.scope import named_databases
    from supagent.security import acting_as

    with acting_as("admin"):
        dbs = _dbs()
        assert named_databases("How many license tokens were used per server yesterday?", dbs) == {}
        assert named_databases("Token usage per tenant and per server", dbs) == {}
        assert list(named_databases("the token gateway database", dbs)) == [several["token"]]


def test_the_other_reading_is_named_under_the_answer(world, monkeypatch):
    from test_agent_loop import agent_with, call, say

    from supagent.knowledge.resolve import other_reading
    from supagent.security import acting_as

    places = {"srv-1": [
        {"database_id": 1, "database": "jobs", "kind": "field", "name": "NODE", "tables": ["jobs"]},
        {"database_id": 2, "database": "metrics", "kind": "label", "name": "node", "tables": ["node_cpu_seconds_total"]}]}
    note = other_reading("srv-1 had 3 failed jobs.", {"jobs"}, places)
    assert note == ('\n\n(Not read for this answer: "srv-1" is also a value of the label node of 1 metric(s): '
                    'node_cpu_seconds_total (database 2 "metrics"). Ask if you meant that data.)')
    assert other_reading("srv-1 had 3 failed jobs; its node_cpu_seconds_total was normal.", {"jobs"}, places) == ""
    assert other_reading("Both.", {"jobs", "node_cpu_seconds_total"}, places) == ""
    assert other_reading("Nothing ran.", set(), places) == ""

    sql = {"request": {"database_id": world["jobs"].id, "sql": "SELECT COUNT(*) FROM \"jobs\" WHERE \"NODE\" = "
                       "'srv-1' AND \"ts\" >= '2026-09-23 00:00' AND \"ts\" < '2026-09-24 00:00'"}}
    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), say("srv-1 had 3 failed jobs.")],
                         results=lambda n, args: json.dumps({"success": True, "columns": ["n"], "rows": [{"n": 3}]}))
    with acting_as("admin"):
        answer, _trace = a.ask("What happened on srv-1 on 23 September?")
    assert answer.startswith("srv-1 had 3 failed jobs.") and "is also a value of the label node" in answer


def test_a_database_of_the_chat_can_be_overridden(several, monkeypatch):
    from test_agent_loop import agent_with, call, say

    from supagent.knowledge.scope import refusal, scope_for
    from supagent.security import acting_as

    wrong = {"request": {"database_id": several["jobs"], "sql": 'SELECT COUNT(*) FROM "jobs"'}}
    with acting_as("admin"):
        chat = scope_for("And yesterday?", "Is the dashboard 'Replica night health' normal?", "It is normal.")
        text = refusal(chat, "execute_sql", wrong)
    assert text.startswith("tool error (not run: check the database)") and "send this same call again" in text
    rows = lambda name, args: json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 7}]})  # noqa
    a, ran = agent_with(monkeypatch, [call("execute_sql", wrong), call("execute_sql", wrong), say("7 jobs.")],
                        results=rows)
    history = [{"role": "user", "content": "Is the dashboard 'Replica night health' normal?"},
               {"role": "assistant", "content": "It is normal."}]
    with acting_as("admin"):
        answer, trace = a.ask("And the same for that one yesterday?", history)
    assert ran == [("execute_sql", wrong)] and [t["status"] for t in trace] == ["error", "done"]


def test_that_dashboard_of_the_chat_is_the_questions_own(several):
    from supagent.knowledge.scope import refusal, scope_for
    from supagent.security import acting_as

    wrong = {"request": {"database_id": several["jobs"], "sql": 'SELECT COUNT(*) FROM "jobs"'}}
    with acting_as("admin"):
        chat = scope_for("Is everything normal on that dashboard right now?", "Which dashboards about replicas?",
                         "One: 'Replica night health'.")
        assert refusal(chat, "execute_sql", wrong).startswith("tool error (not run: another database)")
