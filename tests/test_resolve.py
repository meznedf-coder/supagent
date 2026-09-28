"""Where the data is, found before the LLM starts: metric names from the live list (the dictionary
may be empty), words of the dictionary, built-in synonyms, and what earlier answers used."""

from __future__ import annotations

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


class Conn:
    all_metrics = "all_metrics"
    names = ["elasticsearch_os_cpu_percent", "elasticsearch_jvm_memory_used_bytes", "node_cpu_seconds_total",
             "http_requests_total", "app_queue_pending_tasks"] + [f"other_metric_{i:04d}" for i in range(3000)]

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
