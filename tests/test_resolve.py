"""Where the data is, found before the LLM starts: metric names from the live list (the dictionary
may be empty), words of the dictionary, built-in synonyms, and what earlier answers used."""

from __future__ import annotations

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


class Conn:
    all_metrics = "all_metrics"
    names = ["elasticsearch_os_cpu_percent", "elasticsearch_jvm_memory_used_bytes", "node_cpu_seconds_total",
             "http_requests_total", "app_queue_pending_tasks", "container_cpu_throttling_seconds_total"] + \
        [f"node_{part}_{i}_total" for i in range(20) for part in ("cpu_guest", "disk_io", "network_rx")] + \
        [f"other_metric_{i:04d}" for i in range(3000)]

    def list_tables(self):
        return self.names

    def table_meta(self, name):
        from promagg.schema import MetricMeta

        return MetricMeta(name, "counter" if name.endswith("_total") else "gauge", ["cluster", "node"],
                          "a test metric").build()

    def close(self):
        pass


@pytest.fixture()
def live(world, monkeypatch):
    from supagent import tools
    from supagent.knowledge import resolve

    monkeypatch.setattr(tools, "_promagg_connection", lambda database: Conn())
    resolve._CACHE.clear()
    return world


def test_names_are_words(app):
    from supagent.knowledge.resolve import name_tokens, terms

    assert name_tokens("elasticsearch_os_cpu_percent") == ["elasticsearch", "os", "cpu", "percent"]
    assert name_tokens("jvmMemoryUsed:rate5m") == ["jvm", "memory", "used", "rate5m"]
    assert terms("need to see the chart of CPU usage of the Elasticsearch cluster during last 12 hours") == \
        ["cpu", "usage", "elasticsearch", "cluster"]                     # what to do with the data: not searched
    assert terms("mémoire des serveurs") == ["memoire", "serveur"]


def test_a_metric_is_found_by_its_words_without_the_dictionary(live):
    import time

    from supagent.knowledge.resolve import resolve, where_block
    from supagent.security import acting_as

    with acting_as("admin"):
        t0 = time.time()
        found = resolve("need to see the chart of cpu usage of elasticsearch cluster during last 12 hours")
        assert time.time() - t0 < 2
        assert found[0]["name"] == "elasticsearch_os_cpu_percent" and found[0]["database"].id == live["metrics"].id
        block = where_block("chart of cpu usage of elasticsearch cluster")
    assert 'metric "elasticsearch_os_cpu_percent" (gauge' in block and f"database {live['metrics'].id}" in block
    assert "AVG(value)" in block and 'FROM "elasticsearch_os_cpu_percent"' in block
    with acting_as("admin"):
        mem = resolve("mémoire jvm de elasticsearch")                    # French word, synonym mem -> memory
        assert mem[0]["name"] == "elasticsearch_jvm_memory_used_bytes"
        rate = where_block("http requests per second")
    assert 'metric "http_requests_total" (counter' in rate and "SUM(rate)" in rate


def test_only_databases_the_user_may_query(live):
    from supagent.knowledge.resolve import resolve
    from supagent.security import acting_as

    with acting_as("alice"):                                  # alice may not query the metrics database
        assert all(c["database"].id != live["metrics"].id for c in resolve("elasticsearch cpu"))


def test_answers_teach_where_the_data_is(live):
    import json

    from superset.extensions import db

    from supagent.knowledge.experience import forget_associations, record_associations
    from supagent.knowledge.resolve import resolve, where_block
    from supagent.models import Association
    from supagent.security import acting_as

    db.session.query(Association).delete()
    db.session.commit()
    full = json.dumps({"success": True, "rows": [{"n": 1}], "row_count": 1})
    trace = [{"tool": "execute_sql", "called": "execute_sql", "status": "done", "full": full, "result": full,
              "args": {"request": {"database_id": live["metrics"].id,
                                   "sql": "SELECT AVG(value) FROM all_metrics WHERE metric_name = 'app_queue_pending_tasks'"}}}]
    assert record_associations(900, "backlog of the grid", trace) == 2      # backlog, grid -> the metric
    with acting_as("admin"):
        found = resolve("what is the backlog now")
        assert found[0]["name"] == "app_queue_pending_tasks" and "used before for: backlog" in where_block("backlog now")
    forget_associations(900)                                               # Not helpful: it goes
    assert db.session.query(Association).count() == 0


def test_statements_are_knowledge_questions_are_not():
    from supagent.knowledge.memory import is_statement, worth_learning

    assert is_statement("the field STATUS_INFO = KO means the job failed")
    assert is_statement("elasticsearch_os_cpu_percent est le CPU des noeuds du cluster")
    assert not is_statement("what is the cpu of elasticsearch?")
    assert not is_statement("need to see the chart of cpu usage of elasticsearch cluster")
    assert not is_statement("donne moi les jobs en erreur")
    assert worth_learning("KO means failed")


def test_tools_and_rules_follow_what_the_question_asks():
    from supagent.agent import INTENT_TOOLS, intents

    assert intents("need to see the chart of cpu usage during the last 12 hours") == set()
    assert "charts" in intents("save this as a chart in the Operations dashboard")
    assert "files" in intents("send me the failed jobs by e-mail every morning")
    assert "investigation" in intents("why did the jobs fail yesterday?")
    assert {"export_excel", "generate_chart", "chart_from_sql"} <= INTENT_TOOLS


def test_one_entry_per_metric_and_labels_count(live, monkeypatch):
    """The same metric in several metrics databases is given once; a word naming a label of the
    metric ("per application") ranks it first."""
    from supagent.knowledge.resolve import _score, name_tokens, terms

    q = terms("http request rate per application")
    with_label = _score(q, name_tokens("http_requests_total"), set(), {"application", "code"})
    other = _score(q, name_tokens("fed_requests_total"), set(), {"job", "node"})
    assert with_label > other


def _sql_step(database_id: int, sql: str, rows: int) -> dict:
    import json

    full = json.dumps({"success": True, "rows": [{"n": 1}] * rows, "row_count": rows})
    return {"tool": "execute_sql", "called": "execute_sql", "status": "done", "full": full, "result": full,
            "args": {"request": {"database_id": database_id, "sql": sql}}}


def test_an_association_that_misses_loses_its_use(live):
    """The data was not where an association said (no rows there, the answer came from another
    metric): that association loses a use and goes at none; the right one is learned."""
    from superset.extensions import db

    from supagent.knowledge.experience import record_associations
    from supagent.models import Association

    db.session.query(Association).delete()
    db.session.commit()
    mid = live["metrics"].id
    record_associations(901, "backlog of the grid", [_sql_step(mid, 'SELECT AVG(value) FROM "http_requests_total"', 1)])
    assert {a.name for a in db.session.query(Association)} == {"http_requests_total"}
    trace = [_sql_step(mid, 'SELECT AVG(value) FROM "http_requests_total"', 0),
             _sql_step(mid, 'SELECT AVG(value) FROM "app_queue_pending_tasks"', 3)]
    record_associations(902, "backlog of the grid", trace)
    assert {(a.word, a.name) for a in db.session.query(Association)} == \
        {("backlog", "app_queue_pending_tasks"), ("grid", "app_queue_pending_tasks")}


def test_old_or_vanished_associations_are_not_used(live):
    import datetime as dt

    from superset.extensions import db

    from supagent.knowledge import resolve as R
    from supagent.models import Association, KObject
    from supagent.security import acting_as

    db.session.query(Association).delete()
    mid, jid = live["metrics"].id, live["jobs"].id
    old = dt.datetime.utcnow() - dt.timedelta(days=R.ASSOCIATION_DAYS + 5)
    db.session.add_all([
        Association(word="backlog", database_id=mid, kind="metric", parent="", name="app_queue_pending_tasks", uses=3,
                    messages=[1], updated_at=old),
        Association(word="grid", database_id=mid, kind="metric", parent="", name="metric_that_was_removed", uses=3,
                    messages=[2]),
        Association(word="grid", database_id=jid, kind="index", parent="", name="jobs", uses=3, messages=[3])])
    db.session.query(KObject).filter_by(kind="index", name="jobs").one().gone_at = dt.datetime.utcnow()
    db.session.commit()
    R._CACHE.clear()
    with acting_as("admin"):
        names = [c["name"] for c in R.resolve("backlog of the grid")]
    assert "metric_that_was_removed" not in names and "jobs" not in names
    assert "app_queue_pending_tasks" not in names[:1] or R._score(R.terms("backlog of the grid"),
                                                                  R.name_tokens("app_queue_pending_tasks"), set()) >= 2


def test_words_meet_across_their_forms(live):
    from supagent.knowledge.describe import stem
    from supagent.knowledge.resolve import resolve
    from supagent.security import acting_as

    assert {stem(w) for w in ("failed", "failure", "failures", "failing", "fails")} == {"fail"}
    assert stem("alerting") == "alert" and stem("throttled") == stem("throttling") == "throttl"
    assert (stem("rated"), stem("duration"), stem("used")) == ("rated", "duration", "used")   # never below 4 letters
    assert (stem("servers"), stem("queries"), stem("processes")) == ("server", "query", "process")
    with acting_as("admin"):
        assert resolve("were the containers throttled yesterday")[0]["name"] == "container_cpu_throttling_seconds_total"


def test_a_rare_word_counts_more_than_a_common_one(live):
    """"CPU of the Elasticsearch nodes": node is in 60 names, elasticsearch in two; the
    Elasticsearch metric comes first, not a node_* one."""
    from supagent.knowledge.resolve import Rarity, resolve
    from supagent.security import acting_as

    r = Rarity([["node", "cpu"], ["node", "disk"], ["node", "net"], ["elasticsearch", "cpu"]])
    assert r("elasticsearch") == 1.0 and r("zzz") == 1.0 and 0.7 <= r("node") < r("cpu") < 1.0
    with acting_as("admin"):
        assert resolve("CPU of the Elasticsearch nodes")[0]["name"] == "elasticsearch_os_cpu_percent"
