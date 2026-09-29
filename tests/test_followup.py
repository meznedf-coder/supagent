"""A question about an earlier answer ("create the chart in Superset of that finding"): the agent
sees the queries that gave that answer's rows, with their database and time window (a query only
written in the answer's text did not run), and looks for the data of the question it refers to. A
finding that is a calculation is saved as a virtual dataset, then charted; the tool checks the
user's right to create datasets, and gives back the same dataset when asked again."""

from __future__ import annotations

import sqlite3

import pytest

from test_agent_loop import agent_with, say
from test_stop import FakeAgent

PROMQL = '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{instance="srv-amer-002:9100",mode="idle"}[5m])))'
FINDING = {"tool": "promql_query", "sql": PROMQL, "database": "Prometheus", "database_id": 3,
           "start": "2026-09-24 18:00", "end": "2026-09-25 06:00", "step": "5m",
           "columns": ["ts", "instance", "value"], "rows": [["2026-09-24 18:00", "srv-amer-002:9100", 12.1]],
           "row_count": 144, "truncated": False}
PROBE = {"tool": "promql_query", "sql": PROMQL.replace(":9100", ""), "database": "Prometheus", "database_id": 3,
         "columns": ["ts"], "rows": [], "row_count": 0, "truncated": False}
JOBS = {"tool": "execute_sql", "sql": "SELECT APP, COUNT(*) n FROM jobs GROUP BY 1", "database": "OpenSearch",
        "database_id": 1, "columns": ["APP", "n"], "rows": [["A", 3]], "row_count": 1, "truncated": False}
FOLLOW_UP = "According to that finding, please create the chart in Superset."


def test_the_queries_of_an_answer_are_the_ones_that_gave_its_rows(ctx):
    from supagent.runner import queries_of

    out = queries_of([FINDING, PROBE])
    assert out == [{"tool": "promql_query", "database": "Prometheus", "database_id": 3, "start": "2026-09-24 18:00",
                    "end": "2026-09-25 06:00", "step": "5m", "query": PROMQL, "columns": ["ts", "instance", "value"],
                    "rows": 144}]                                   # the empty probe is not the finding
    assert len(queries_of([JOBS, FINDING, JOBS])) == 2               # the last two
    assert len(queries_of([dict(JOBS, sql="SELECT " + "x, " * 400 + "1")])[0]["query"]) == 700


def test_the_last_answers_go_with_their_queries(app, monkeypatch):
    from superset.extensions import db, security_manager as sm

    from supagent import runner
    from supagent.knowledge import experience, generic
    from supagent.models import Conversation, Message

    seen: dict = {}

    class Capture(FakeAgent):
        stop = False

        def ask(self, question, history=None):
            seen["history"], seen["question"] = history, question
            return super().ask(question, history)

    with app.app_context():
        conv = Conversation(user_id=sm.find_user(username="alice").id, title="CPU")
        db.session.add(conv)
        db.session.flush()
        for role, content, results in (("user", "How many jobs per application?", None),
                                       ("assistant", "A: 3.", [JOBS]),
                                       ("user", "Show me the CPU usage of host srv-amer-002 over the last 12 hours.", None),
                                       ("assistant", "CPU climbed from 12% to 42%.", [PROBE, FINDING]),
                                       ("user", "Thanks!", None),
                                       ("assistant", "You are welcome.", None),
                                       ("user", FOLLOW_UP, None)):
            db.session.add(Message(conversation_id=conv.id, role=role, content=content, status="done", results=results))
        answer = Message(conversation_id=conv.id, role="assistant", status="pending", steps=[])
        db.session.add(answer)
        db.session.commit()
        monkeypatch.setattr("supagent.agent.Agent", type("Agent", (Capture,), {"message_id": answer.id}))
        monkeypatch.setattr(generic, "generalize", lambda *a, **k: {})
        monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: None)
        runner.run_answer(answer.id)
    history = seen["history"]
    assert seen["question"] == FOLLOW_UP and [h["role"] for h in history] == ["user", "assistant"] * 3
    assert [bool(h.get("queries")) for h in history] == [False, True, False, True, False, False]
    assert history[3]["queries"][0]["query"] == PROMQL and history[3]["queries"][0]["start"] == "2026-09-24 18:00"


def test_the_agent_sees_what_that_finding_was(ctx, monkeypatch):
    from supagent.agent import HISTORY_CHARS, Agent
    from supagent.runner import queries_of

    subjects: list[str] = []
    a, _ran = agent_with(monkeypatch, [say("Saved."), say("Saved.")])
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q: subjects.append(q) or "")
    earlier = "Show me the CPU usage of host srv-amer-002 over the last 12 hours."
    answer = "CPU climbed from 12% to 42%.\n\n```sql\nSELECT cpu_busy_pct FROM x\n```"
    history = [{"role": "user", "content": earlier},
               {"role": "assistant", "content": answer, "queries": queries_of([PROBE, FINDING])}]
    a.ask(FOLLOW_UP, history)
    sent = a.llm.seen[0]
    assert next(m["content"] for m in sent if m["role"] == "assistant") == answer   # the answer as it was
    asked = sent[-1]["content"]                                       # with the new question: what it ran
    assert asked.endswith(FOLLOW_UP) and "only written in an answer's text did not run" in asked
    assert f'- the answer to "{earlier}":' in asked
    assert ("promql_query on database 'Prometheus' (id 3), from 2026-09-24 18:00 to 2026-09-25 06:00, step 5m: "
            + PROMQL + " -> columns ts, instance, value") in asked
    assert subjects == [f"{earlier}\n{FOLLOW_UP}"]                   # where the data is: the finding's question

    subjects.clear()
    b, _ = agent_with(monkeypatch, [say("12 jobs."), say("12 jobs.")])
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q: subjects.append(q) or "")
    b.ask("How many jobs failed on 23 September?", history)          # a new question: its own data
    assert subjects == ["How many jobs failed on 23 September?"]

    c, _ = agent_with(monkeypatch, [say("ok"), say("ok")])
    c.ask(FOLLOW_UP, [{"role": "user", "content": earlier},
                      {"role": "assistant", "content": "x" * 5000, "queries": queries_of([FINDING])}])
    assert len(next(m["content"] for m in c.llm.seen[0] if m["role"] == "assistant")) == HISTORY_CHARS
    assert PROMQL in c.llm.seen[0][-1]["content"]
    d, _ = agent_with(monkeypatch, [say("ok"), say("ok")])
    d.ask("How many jobs failed?", [])                                # a first question: nothing added
    assert d.llm.seen[0][-1]["content"].endswith("How many jobs failed?") and "computed from" not in d.llm.seen[0][-1]["content"]


def test_follow_up_questions_are_recognised():
    from supagent.agent import refers_back

    for q in (FOLLOW_UP, "Save it as a chart in Superset", "Do the same for srv-emea-001",
              "Crée ce graphique dans Superset", "Crée le graphique de ce résultat dans Superset"):
        assert refers_back(q), q
    for q in ("How many jobs failed this week?", "Show the CPU usage of srv-amer-002 over the last 12 hours",
              "Combien de jobs ont échoué cette semaine ?", "How many jobs failed on 23 September?"):
        assert not refers_back(q), q


# ------------------------------------------------------------------------------------------------ #
@pytest.fixture()
def local_db(app, tmp_path):
    """A SQLite database the agent may use, with a small table of samples."""
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.core import Database

    from supagent import settings

    path = tmp_path / "samples.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE samples (ts TEXT, node TEXT, value REAL)")
    con.executemany("INSERT INTO samples VALUES (?, ?, ?)",
                    [("2026-09-24 18:00:00", "srv-amer-002", 12.5), ("2026-09-24 19:00:00", "srv-amer-002", 20.0)])
    con.commit()
    con.close()
    with app.app_context():
        d = Database(database_name="samples-test", sqlalchemy_uri=f"sqlite:///{path}")
        db.session.add(d)
        db.session.commit()
        database_id = d.id
        settings.set_value("agent.databases", ["samples-test"])
    yield database_id
    with app.app_context():
        settings.set_value("agent.databases", [])
        for ds in db.session.query(SqlaTable).filter(SqlaTable.database_id == database_id):
            db.session.delete(ds)
        db.session.delete(db.session.get(Database, database_id))
        db.session.commit()


def test_a_finding_is_saved_as_a_virtual_dataset_once(app, local_db, monkeypatch):
    from supagent.security import acting_as
    from supagent.tools_superset import VirtualDatasetRequest, create_virtual_dataset

    sql = "SELECT ts, node, value FROM samples WHERE node = 'srv-amer-002'"
    ask = lambda q, name="CPU busy % srv-amer-002": create_virtual_dataset(   # noqa: E731
        VirtualDatasetRequest(database_id=local_db, sql=q, name=name))
    with app.app_context():
        with acting_as("admin"):
            first = ask(sql + ";")
            assert not first.get("error"), first
            assert [c["name"] for c in first["columns"]] == ["ts", "node", "value"]
            assert first["time_column"] == "ts" and first["reused"] is False   # charts filter on it
            again = ask(sql)                                              # a retry: the same dataset
            assert again["dataset_id"] == first["dataset_id"] and again["reused"] is True
            other = ask("SELECT ts, value FROM samples")
            assert other["name"] == "CPU busy % srv-amer-002 (2)" and other["dataset_id"] != first["dataset_id"]
            from superset.extensions import security_manager

            monkeypatch.setattr(security_manager, "can_access_database", lambda database: False)
            refused = ask("SELECT ts, value FROM promql('up')")
            assert "promql() reads the whole database" in refused["error"]
        with acting_as("alice"):                                           # Gamma + AI Agent: no dataset writes
            assert "Dataset write permission" in ask(sql)["error"]


def test_the_dataset_tool_is_offered_for_charts_and_suggested_when_a_field_is_missing(ctx, monkeypatch):
    import json

    from supagent.agent import Agent

    a = object.__new__(Agent)
    a.rich, a.wants_saved_chart = True, False
    a.specs = [{"function": {"name": n}} for n in ("execute_sql", "create_virtual_dataset", "generate_chart")]
    names = lambda q: [s["function"]["name"] for s in a._specs_for(q)]   # noqa: E731
    assert names("How many jobs failed yesterday?") == ["execute_sql"]
    a.wants_saved_chart = True
    assert "create_virtual_dataset" in names(FOLLOW_UP)

    b, _ran = agent_with(monkeypatch, [say("ok")])
    b.names |= {"create_virtual_dataset"}
    monkeypatch.setattr(b.guard, "_dataset", lambda ident: ({"ts", "node_cpu", "value"}, set()))
    _name, _args, content = b.guard.before("generate_chart", {"request": {
        "dataset_id": 6, "save_chart": True, "chart_name": "CPU",
        "config": {"chart_type": "xy", "kind": "line", "x": {"name": "ts"},
                   "y": [{"name": "cpu_busy_pct", "aggregate": "AVG"}]}}})
    details = json.loads(content)["details"]
    assert any("unknown column 'cpu_busy_pct'" in d for d in details)
    assert any("create_virtual_dataset" in d for d in details)


def test_the_cpu_usage_hint_is_the_busy_share_not_the_cores(ctx):
    from supagent.knowledge.resolve import _sql

    cpu = _sql({"name": "node_cpu_seconds_total", "metric_type": "counter", "labels": ["cpu", "mode", "node"]})
    assert "100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate) AS busy_pct" in cpu
    assert "SUM(rate) AS per_second" in _sql({"name": "http_requests_total", "metric_type": "counter",
                                               "labels": ["code", "node"]})


def test_a_promql_dataset_needs_the_findings_window(app, local_db):
    from supagent.security import acting_as
    from supagent.tools_superset import VirtualDatasetRequest, create_virtual_dataset

    with app.app_context(), acting_as("admin"):
        out = create_virtual_dataset(VirtualDatasetRequest(
            database_id=local_db, sql="SELECT ts, node, value FROM promql('100 * avg by (node) (up)')", name="up"))
        assert "needs the finding's time window" in out["error"]      # a chart grouped by node would fail


def test_the_datasets_of_tries_that_did_not_make_the_chart_are_dropped(app, local_db):
    import json

    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db

    from supagent.security import acting_as
    from supagent.tools_superset import VirtualDatasetRequest, create_virtual_dataset, drop_unused_datasets

    def made(sql, name):
        res = create_virtual_dataset(VirtualDatasetRequest(database_id=local_db, sql=sql, name=name))
        return {"tool": "create_virtual_dataset", "status": "done", "result": json.dumps(res)}, res["dataset_id"]

    with app.app_context(), acting_as("admin"):
        (try1, first), (try2, second) = made("SELECT ts FROM samples", "try"), made("SELECT ts, value FROM samples", "try")
        failed = {"tool": "generate_chart", "status": "error", "args": {"request": {"dataset_id": first}}}
        chart = {"tool": "generate_chart", "status": "done",
                 "args": {"request": {"dataset_id": second, "save_chart": True}}}
        assert drop_unused_datasets([try1, failed, try2]) == []         # no chart saved: nothing deleted
        assert drop_unused_datasets([try1, failed, try2, chart]) == [first]
        assert db.session.get(SqlaTable, first) is None and db.session.get(SqlaTable, second) is not None
