"""0.4.8: batched Mimir requests with dotted metric names are valid PromQL (a failed batch is
counted with its first error), a label name already described is copied to its new labels
without an LLM call, the search text of a metric does not change with its series count, a run
shows its steps (also the one it was doing when it stopped), and what the team said not to use
(a person's description: "not used") is never proposed nor queried."""

from __future__ import annotations

import datetime as dt
import json
import types

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


# ---------------------------------------------------------------------------------------------- #
# PromQL of the batched requests
# ---------------------------------------------------------------------------------------------- #
def test_metric_names_with_dots_make_a_valid_promql_regex(ctx):
    from supagent.knowledge.learn_metrics import name_regex

    rx = name_regex(["node_cpu_seconds_total", "http.server.duration", "a+b", 'odd"name'])
    assert rx == 'node_cpu_seconds_total|http\\\\.server\\\\.duration|a\\\\+b|odd\\"name'
    # inside the PromQL string "...", Go unquoting gives the RE2 regex node_cpu...|http\.server\.duration|a\+b|odd"name
    assert json.loads(f'"{rx}"') == 'node_cpu_seconds_total|http\\.server\\.duration|a\\+b|odd"name'


def test_a_refused_batch_is_counted_with_its_first_error(ctx, monkeypatch):
    from supagent.knowledge import learn_metrics as LM

    sent: list[str] = []

    class Client:
        def query(self, q, t_ms, timeout=None):
            sent.append(q)
            raise ValueError("invalid parameter \"query\": parse error")

        def label_values(self, name, match, a, b):
            sent.append(match[0])
            raise ValueError("refused")

    conn = types.SimpleNamespace(client=Client())
    monkeypatch.setattr(LM.settings, "get", lambda key: 30)
    failed: dict = {}
    assert LM.batch_counts(conn, ["a.b", "c"], 0, failed) is None
    assert LM.batch_counts(conn, ["d"], 0, failed) is None
    assert LM.batch_history(conn, ["a.b"], 10 ** 12, failed) is None
    assert sent[0] == 'count by (__name__) ({__name__=~"a\\\\.b|c"})'
    assert failed["series counts"]["batches"] == 2 and "parse error" in failed["series counts"]["first_error"]
    assert failed["history"]["batches"] == 1


# ---------------------------------------------------------------------------------------------- #
# labels described once per name
# ---------------------------------------------------------------------------------------------- #
def test_new_labels_of_a_described_name_are_copied_without_the_llm(world, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge.enrich import enrich, inherit_label_descriptions
    from supagent.knowledge.store import upsert
    from supagent.models import KObject

    s_prom, run = world["s_prom"], world["run"]
    node = db.session.query(KObject).filter_by(source_id=s_prom.id, kind="label", name="node").first()
    node.description, node.description_source, node.verified = "The server of the sample", "curated", True
    mode = db.session.query(KObject).filter_by(source_id=s_prom.id, kind="label", name="mode").first()
    mode.description, mode.description_source, mode.verified = "The CPU state", "llm", False
    for metric in ("node_load1", "node_load5"):                  # profiled later: new labels, same names
        upsert(run, s_prom, "metric", "", metric, {"metric_type": "gauge", "stats": {}})
        upsert(run, s_prom, "label", metric, "node", {"data_type": "string", "stats": {}})
        upsert(run, s_prom, "label", metric, "mode", {"data_type": "string", "stats": {}})
    db.session.commit()
    assert inherit_label_descriptions(s_prom.id) == 4
    copies = db.session.query(KObject).filter(KObject.source_id == s_prom.id, KObject.kind == "label",
                                              KObject.parent.in_(["node_load1", "node_load5"])).all()
    got = {(o.name, o.description, o.description_source, o.verified) for o in copies}
    assert got == {("node", "The server of the sample", "curated", True), ("mode", "The CPU state", "llm", False)}

    asked: list = []

    class LLM:
        last_usage = {"prompt_tokens": 100, "completion_tokens": 20}

        def chat(self, messages, tools=None, max_tokens=None):
            items = json.loads(messages[1]["content"])
            asked.extend(i["name"] for i in items)
            return {"content": json.dumps([{"id": i["id"], "description": "About " + i["name"]} for i in items])}

    upsert(run, s_prom, "label", "node_load15", "node", {"data_type": "string", "stats": {}})
    db.session.commit()
    out = enrich(None, 10 ** 12, llm=LLM(), source_id=s_prom.id)
    assert out["copied"] == 1 and "node" not in asked                # never asked again
    assert out["tokens"] == 120 * out["requests"]


# ---------------------------------------------------------------------------------------------- #
# the search text of a metric
# ---------------------------------------------------------------------------------------------- #
def test_the_search_text_does_not_change_with_the_series_count(world):
    from superset.extensions import db

    from supagent.knowledge.index import _object_pieces
    from supagent.models import KObject

    def text() -> str:
        return next(p["text"] for p in _object_pieces() if p["title"] == "metric node_cpu_seconds_total")

    m = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    label = db.session.query(KObject).filter_by(kind="label", parent="node_cpu_seconds_total", name="node").one()
    label.stats = {"values": ["srv-1", "srv-3"], "partial": True}         # a sample of the values
    db.session.commit()
    before = text()
    m.stats = {**(m.stats or {}), "series": 123456}                       # the next run: other counts,
    label.stats = {"values": ["srv-2", "srv-9"], "partial": True}         # another sample
    db.session.commit()
    assert text() == before and "123456" not in before and "srv-" not in before   # the same text: no new embedding


# ---------------------------------------------------------------------------------------------- #
# the steps of a run
# ---------------------------------------------------------------------------------------------- #
def test_a_run_shows_its_steps_and_the_one_it_was_doing_when_stopped(app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge.learner import Steps, _start_run
    from supagent.models import Run

    with app.app_context():
        db.session.query(Run).delete()
        db.session.commit()
        run_id = _start_run("test")
        stats: dict = {"databases": {}}
        steps = Steps(run_id, stats)
        steps.begin("read the database", "metrics")
        steps.update(phase="profiles", listed=10090, due=4904, profiled=0, requests=3)
        steps.update(profiled=1200, requests=1300, fallbacks={"history": {"batches": 2, "first_error": "x"}})
        steps.end(profiled=4904, changes={"new": 12})
        steps.begin("relations")
        steps.interrupted("stopped by an admin")
        saved = db.session.get(Run, run_id)
        db.session.refresh(saved)
        read, rel = saved.stats["steps"]
        assert (read["step"], read["database"], read["listed"], read["profiled"], read["changes"]) == (
            "read the database", "metrics", 10090, 4904, {"new": 12})
        assert "phase" not in read and read["seconds"] >= 0 and read["fallbacks"]["history"]["batches"] == 2
        assert rel["interrupted"] == "stopped by an admin" and "seconds" in rel
        db.session.query(Run).delete()
        db.session.commit()


# ---------------------------------------------------------------------------------------------- #
# what the team said not to use
# ---------------------------------------------------------------------------------------------- #
def _say(obj_filter: dict, text: str, source: str = "curated"):
    from superset.extensions import db

    from supagent.knowledge import excluded
    from supagent.models import KObject

    o = db.session.query(KObject).filter_by(**obj_filter).one()
    o.description, o.description_source = text, source
    db.session.commit()
    excluded._CACHE["at"] = 0.0                                      # read again
    return o


def test_the_teams_not_used_is_understood_in_english_and_french(ctx):
    from supagent.knowledge.excluded import NOT_USED

    for text in ("does not used", "Not used anymore", "DEPRECATED: use NODE_NAME", "Obsolète", "ne pas utiliser",
                 "Ce champ n'est plus utilisé", "unused since 2025", "do not use", "don't use it", "non utilisé"):
        assert NOT_USED.search(text), text
    for text in ("The server that ran the job", "Number of used licences", "used by the billing team"):
        assert not NOT_USED.search(text), text


def test_what_the_team_said_not_to_use_is_refused_in_queries(world):
    from supagent.knowledge.excluded import violations

    _say({"kind": "field", "parent": "jobs", "name": "NODE"}, "does not used: take HOST instead")
    _say({"kind": "label", "parent": "node_cpu_seconds_total", "name": "mode"}, "Obsolete label")
    _say({"kind": "field", "parent": "jobs", "name": "STATUS"}, "not used", source="llm")   # the LLM's: no
    sql = 'SELECT "NODE", COUNT(*) FROM "jobs" WHERE "STATUS" = \'FAILED\' GROUP BY 1'
    assert violations([sql]) == ['field "NODE" of "jobs": the team wrote "does not used: take HOST instead"']
    assert violations(['SELECT j."NODE" FROM "jobs" j']) != []
    assert violations(['SELECT "HOST", COUNT(*) FROM "jobs" GROUP BY 1']) == []        # another field
    assert violations(['SELECT "NODE" FROM "other_index"']) == []                        # not this index
    promql = 'sum by (mode) (rate(node_cpu_seconds_total{node="srv-1"}[5m]))'
    assert violations([promql]) == ['label "mode" of "node_cpu_seconds_total": the team wrote "Obsolete label"']


def test_the_agent_never_runs_a_query_that_uses_it(world):
    from supagent.agent import Agent

    _say({"kind": "field", "parent": "jobs", "name": "NODE"}, "not used")
    a = object.__new__(Agent)
    ran: list = []
    a.local, a.names = {"execute_sql": True}, {"execute_sql"}
    a.registry = types.SimpleNamespace(call_text=lambda name, args: ran.append(name) or "{}")
    a.wants_saved_chart, a.redirected_chart = False, False
    called, content = a._call("execute_sql", {"request": {"database_id": 1, "sql": 'SELECT "NODE" FROM "jobs"'}})
    assert content.startswith("tool error: this uses what the team marked as not to be used") and not ran
    called, content = a._call("execute_sql", {"request": {"database_id": 1, "sql": 'SELECT "HOST" FROM "jobs"'}})
    assert ran == ["execute_sql"]


def test_where_the_data_is_and_describe_say_so(world, monkeypatch):
    from supagent.knowledge import resolve
    from supagent.knowledge.describe import describe, marker

    node = _say({"kind": "field", "parent": "jobs", "name": "NODE"}, "not used anymore")
    assert marker(node) == " (DO NOT USE: the team's description)"
    jobs = world["jobs"]
    monkeypatch.setattr(resolve, "resolve", lambda q: [
        {"kind": "field", "database": jobs, "name": "NODE", "parent": "jobs"},
        {"kind": "index", "database": jobs, "name": "jobs", "parent": ""}])
    block = resolve.where_block("failed jobs by node")
    assert 'field "NODE"' not in block and 'index "jobs"' in block
    assert 'DO NOT USE (the team): "NODE" (not used anymore)' in block
    from supagent.security import acting_as

    with acting_as("admin"):
        text = describe(None, "jobs") or ""
    assert '"NODE"' in text and "DO NOT USE" in text
