"""Learning from answers: learned answers (from Helpful only; helpful, confirmed by an admin,
rejected; shared by database access), query timings, compact results for the LLM, the guard
against huge raw reads."""

from __future__ import annotations

import json

from test_knowledge import world  # noqa: F401  (the fixture)


def _trace(sql: str, database_id: int, seconds: float = 0.8, rows: int = 10, status: str = "done") -> list[dict]:
    full = json.dumps({"success": True, "rows": [{"a": i} for i in range(rows)], "row_count": rows})
    return [{"tool": "describe_data", "status": "done", "args": {}, "seconds": 0.1},
            {"tool": "execute_sql", "called": "execute_sql", "status": status, "seconds": seconds,
             "args": {"request": {"database_id": database_id, "sql": sql}}, "full": full, "result": full[:200]}]


def test_learned_answers_come_from_helpful_and_are_shared_by_access(world):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.experience import feedback, learn_from_answer, recipes_for, record_recipe
    from supagent.models import Recipe
    from supagent.security import acting_as

    db.session.query(Recipe).delete()
    db.session.commit()
    jobs, metrics = world["jobs"].id, world["metrics"].id
    admin = sm.find_user(username="admin").id
    q = "How many jobs failed per application yesterday?"
    first = _trace("SELECT APPLICATION, COUNT(*) FROM jobs WHERE STATUS = 'FAILED' AND day = '2026-09-23' GROUP BY 1",
                   jobs)
    assert learn_from_answer(101, admin, q, first)["timings"] == 1
    assert db.session.query(Recipe).count() == 0             # an answer alone teaches nothing: Helpful does
    rec = record_recipe(101, admin, q, first)                # marked Helpful
    assert (rec.status, rec.confirmations) == ("helpful", [101])
    with acting_as("alice"):
        found = recipes_for("failed jobs per application last week")
    assert found and found[0]["status"] == "helpful" and "GROUP BY" in found[0]["query"]
    record_recipe(102, admin, "failed jobs per application on 2026-09-20", _trace(
        "SELECT APPLICATION, COUNT(*) FROM jobs WHERE STATUS = 'FAILED' AND day = '2026-09-20' GROUP BY 1", jobs))
    rec = db.session.query(Recipe).one()
    assert rec.uses == 2 and rec.confirmations == [101, 102]   # the same query shape: one, Helpful twice
    feedback(102, -1)                                        # the second Helpful taken back
    assert db.session.get(Recipe, rec.id).confirmations == [101]
    feedback(101, 0)                                         # the first too: nothing left, it goes
    assert db.session.query(Recipe).count() == 0
    rec = record_recipe(103, admin, "cpu busy per node yesterday", _trace(
        "SELECT node, AVG(value) FROM node_cpu_seconds_total GROUP BY node", metrics))
    rec.status = "confirmed"                                 # an admin confirms it
    db.session.commit()
    feedback(103, -1)
    assert db.session.get(Recipe, rec.id).status == "confirmed"   # an admin's decision stays
    with acting_as("alice"):                                 # alice may not query the metrics database
        assert recipes_for("cpu busy per node today") == []
    with acting_as("admin"):
        assert recipes_for("cpu busy per node today")[0]["status"] == "confirmed"
    for status in ("rejected", "auto"):                     # rejected, or saved by itself by 0.2.1: never
        db.session.get(Recipe, rec.id).status = status
        db.session.commit()
        with acting_as("admin"):
            assert recipes_for("cpu busy per node today") == []


def test_helpful_makes_a_learned_answer_in_the_background(world, monkeypatch):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge import generic
    from supagent.knowledge.experience import learn_from_helpful
    from supagent.models import Conversation, Message, Recipe

    db.session.query(Recipe).delete()
    conv = Conversation(user_id=sm.find_user(username="alice").id, title="failed jobs of BILLING yesterday")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="failed jobs of BILLING yesterday",
                           status="done"))
    sql = "SELECT COUNT(*) AS n FROM jobs WHERE APP = 'BILLING' AND day = '2026-09-23'"
    m = Message(conversation_id=conv.id, role="assistant", status="done", content="7 jobs failed.", feedback=1,
                steps=[{"tool": "execute_sql", "status": "done", "seconds": 0.4, "result": "{...",
                        "args": {"request": {"database_id": world["jobs"].id, "sql": sql}}}],
                results=[{"tool": "execute_sql", "sql": sql, "columns": ["n"], "rows": [[7]], "row_count": 1}])
    db.session.add(m)
    db.session.commit()
    asked = []
    monkeypatch.setattr(generic, "generalize", lambda q, earlier, query, tool=None, llm=None, kept=None: asked.append(
        (q, query)) or {"question": "Failed jobs of a given application on a given day", "title": "Failed jobs",
                        "reusable": True, "generic": True, "same_as": None})
    mid = m.id
    r = db.session.get(Recipe, learn_from_helpful(mid))
    assert (r.question, r.status, r.rows, r.confirmations) == (
        "Failed jobs of a given application on a given day", "helpful", 1, [mid])
    assert asked == [("failed jobs of BILLING yesterday", sql)]
    assert db.session.get(Recipe, learn_from_helpful(mid)).uses == 1    # the same Helpful counts once
    db.session.query(Message).filter(Message.id == mid).update({"feedback": -1})
    db.session.commit()
    db.session.query(Recipe).delete()
    db.session.commit()
    assert learn_from_helpful(mid) is None                    # taken back before the job ran


def test_query_timings_and_compact_results(world):
    from superset.extensions import db

    from supagent.knowledge.experience import compact_for_llm, record_timings, timing_hints
    from supagent.models import QueryStat

    db.session.query(QueryStat).delete()
    db.session.commit()
    jobs = world["jobs"].id
    record_timings(_trace("SELECT * FROM jobs WHERE day = '2026-09-01' LIMIT 10", jobs, seconds=42))
    record_timings(_trace("SELECT * FROM jobs WHERE day = '2026-09-02' LIMIT 10", jobs, seconds=40))
    stat = db.session.query(QueryStat).one()                 # literals removed: one kind of query
    assert stat.calls == 2 and stat.max_seconds == 42 and "?" in stat.pattern
    hint = timing_hints(["jobs"], jobs)[0]
    assert "2 queries seen" in hint and "slowest 42 s" in hint
    big = json.dumps({"success": True, "database": "jobs", "columns": [{"name": "app"}, {"name": "n"}],
                      "rows": [{"app": f"A{i % 7}", "n": i} for i in range(300)], "row_count": 300})
    small = compact_for_llm(big, "failed jobs by application")
    data = json.loads(small)
    assert data["row_count"] == 300 and len(data["first_rows"]) == 25 and data["numeric_columns"]["n"]["max"] == 299
    assert data["top_values"]["app"][0][1] > 40 and len(small) < len(big) / 4
    every = json.loads(compact_for_llm(big, "give me the result as JSON"))     # asked: every row, and the sums
    assert every["rows"] == json.loads(big)["rows"] and every["column_sums"] == {"n": sum(range(300))}


def test_huge_raw_reads_are_refused_before_running(world):
    from superset.extensions import db

    from supagent.knowledge.experience import guard_sql
    from supagent.models import KObject

    cpu = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    cpu.stats = {**(cpu.stats or {}), "series": 3_200_000}
    db.session.commit()
    metrics = world["metrics"]
    refused = guard_sql(metrics, "SELECT * FROM node_cpu_seconds_total WHERE ts > TIMESTAMP '2026-09-01'")
    assert refused and "3,200,000 series" in refused
    assert guard_sql(metrics, "SELECT node, SUM(rate) FROM node_cpu_seconds_total GROUP BY node") is None
    assert guard_sql(world["jobs"], "SELECT * FROM jobs") is None       # OpenSearch: LIMIT and paging protect it


def test_summing_a_counter_value_is_refused_with_the_right_way(world):
    """Lab acceptance G: SUM(value) of node_cpu_seconds_total (cumulative) gave a wrong ranking."""
    from superset.extensions import db

    from supagent.knowledge.experience import guard_sql
    from supagent.knowledge.store import upsert
    from supagent.models import Run

    metrics = world["metrics"]
    wrong = ("WITH c AS (SELECT node, SUM(CASE WHEN mode <> 'idle' THEN value ELSE 0 END) AS busy, SUM(value) AS total "
             "FROM node_cpu_seconds_total GROUP BY node) SELECT node, 100.0 * busy / total AS pct FROM c")
    refused = guard_sql(metrics, wrong)
    assert refused and "counter" in refused and "rate" in refused
    assert guard_sql(metrics, "SELECT node, 100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate) "
                              "FROM node_cpu_seconds_total GROUP BY node") is None
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    upsert(run, world["s_prom"], "metric", "", "node_load1", {"metric_type": "gauge", "stats": {"series": 4}})
    db.session.commit()
    assert guard_sql(metrics, "SELECT node, AVG(value) FROM node_load1 GROUP BY node") is None   # a gauge


def test_the_learned_answers_api(app):
    from conftest import login

    with app.test_client() as c:
        login(c, "alice")
        data = c.get("/supagent/dictionary/api/recipes").get_json()
        assert data["is_admin"] is False
        assert all(r["database_id"] is None or r["tool"] for r in data["recipes"])
        if data["recipes"]:
            assert c.post(f"/supagent/dictionary/api/recipes/{data['recipes'][0]['id']}",
                          json={"status": "confirmed"}).status_code in (401, 403)
        assert "timings" in c.get("/supagent/dictionary/api/timings").get_json()


def test_every_learned_answer_knows_its_database(world):
    """PromQL, extracts and saved charts too: a recipe is shared only with the users who may
    query its database, and one whose database is unknown with nobody."""
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.experience import database_of, recipes_for, record_recipe
    from supagent.knowledge.index import sync
    from supagent.knowledge.search import search
    from supagent.models import Chunk, Recipe
    from supagent.security import acting_as

    db.session.query(Recipe).delete()
    db.session.commit()
    metrics = world["metrics"].id
    with acting_as("admin"):
        assert database_of("promql_query", {"expr": "up", "database": "metrics"}) == metrics
        assert database_of("promql_query", {"expr": "up", "database": "no such database"}) == 0
        assert database_of("generate_chart", {"dataset_id": 987654}) == 0
        trace = [{"tool": "promql_query", "status": "done", "seconds": 1.0, "result": "{}",
                  "args": {"expr": "sum by (node) (rate(node_cpu_seconds_total{mode='user'}[5m]))",
                           "database": "metrics"}}]
        admin = sm.find_user(username="admin").id
        record_recipe(201, admin, "cpu user rate per node", trace)          # marked Helpful
        record_recipe(202, admin, "cpu user rate per node today", trace)
    rec = db.session.query(Recipe).one()
    assert (rec.tool, rec.database_id, rec.uses) == ("promql_query", metrics, 2)
    unknown = Recipe(question="cpu user rate per node", words="cpu node per rate user", tool="promql_query",
                     query="up", signature="unknown-db", status="confirmed", uses=5, database_id=None)
    db.session.add(unknown)
    db.session.commit()
    sync()
    assert db.session.query(Chunk).filter_by(ref=f"recipe:{unknown.id}").one().database_id == 0
    with acting_as("alice"):
        assert recipes_for("cpu user rate per node") == []
        assert not [f for f in search("cpu user rate node", k=20) if f["kind"] == "recipe"]
    with acting_as("admin"):
        found = recipes_for("cpu user rate per node")
        assert [f["database_id"] for f in found] == [metrics]              # never the unknown one


def test_small_results_come_with_their_column_sums(app):
    """Lab: the model wrote '6,011 jobs failed' under a table adding up to 7,011."""
    from supagent.knowledge.experience import compact_for_llm

    rows = [{"APPLICATION": a, "FAILED_JOBS": n, "rate": r} for a, n, r in
            (("BILLING", 2161, 1.5), ("PAYROLL", 944, 2.25), ("ORDERS", 3906, 0.5))]
    out = json.loads(compact_for_llm(json.dumps({"success": True, "columns": [{"name": "APPLICATION"},
                                                                                  {"name": "FAILED_JOBS"}, {"name": "rate"}],
                                                   "rows": rows}), "failed jobs by application"))
    assert out["column_sums"] == {"FAILED_JOBS": 7011, "rate": 4.25} and out["rows"] == rows
    one = json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 5}]})
    assert compact_for_llm(one, "how many") == one                     # one row: nothing to add
