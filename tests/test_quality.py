"""Measured on what exists: does "Where the data is" find the data of the Helpful answers (hit@k,
MRR), what the agent did not answer well (gaps), where the time of the answers goes (stats); and
the commands that print them."""

from __future__ import annotations

import json

from test_resolve import live  # noqa: F401  (the fixture)
from test_knowledge import world  # noqa: F401  (the fixture)


def _recipe(question: str, database_id: int, sql: str, status: str = "helpful"):
    from superset.extensions import db

    from supagent.models import Recipe

    r = Recipe(question=question, words="x", tool="execute_sql", database_id=database_id, query=sql,
               signature=f"sig-{question}", status=status, uses=1, generic=True)
    db.session.add(r)
    db.session.commit()
    return r


def test_the_resolver_is_measured_on_the_helpful_answers(live):
    from superset.extensions import db

    from supagent.knowledge.quality import evaluate_resolver
    from supagent.models import Association, Recipe
    from supagent.security import acting_as

    db.session.query(Recipe).delete()
    db.session.query(Association).delete()
    db.session.commit()
    mid = live["metrics"].id
    _recipe("CPU of the Elasticsearch nodes", mid, 'SELECT AVG(value) FROM "elasticsearch_os_cpu_percent"')
    _recipe("Backlog of the grid", mid, "SELECT AVG(value) FROM all_metrics WHERE metric_name = 'app_queue_pending_tasks'")
    _recipe("Rejected one", mid, 'SELECT 1 FROM "http_requests_total"', status="rejected")
    with acting_as("admin"):
        out = evaluate_resolver()
    assert out["answers"] == 2 and out["hit_at_1"] == 0.5 and out["hit_at_3"] == 0.5 and out["mrr"] == 0.5
    assert out["not_found"] == ["Backlog of the grid -> app_queue_pending_tasks"]


def _chat(question: str, **answer) -> int:
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, Message

    conv = Conversation(user_id=sm.find_user(username="alice").id, title=question)
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content=question, status="done"))
    m = Message(conversation_id=conv.id, role="assistant", **{"status": "done", "content": "ok", **answer})
    db.session.add(m)
    db.session.commit()
    return m.id


def test_gaps_list_what_was_not_answered_well(live):
    from superset.extensions import db

    from supagent.knowledge import resolve
    from supagent.knowledge.quality import gaps
    from supagent.models import KObject, Message, Recipe
    from supagent.security import acting_as

    db.session.query(Message).delete()
    db.session.query(Recipe).delete()
    db.session.commit()
    _chat("How many jobs failed on the grid?", feedback=-1)
    _chat("CPU of the elasticsearch nodes?", status="error", content="The agent could not answer: timeout")
    looked = [{"tool": "execute_sql", "status": "done", "seconds": 0.1}]
    _chat("What is the weather in Lisbon?", steps=looked)
    _chat("CPU of the elasticsearch cluster now", steps=looked)
    _chat("hello there, who are you?")                                  # no data needed: not a gap
    _chat("and the day before?", steps=looked)                          # a follow-up: too few words
    _recipe("Seconds of CPU", live["metrics"].id, 'SELECT SUM(rate) FROM "node_cpu_seconds_total"')
    db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one().gone_at = db.func.now()
    db.session.commit()
    resolve._CACHE.clear()
    with acting_as("admin"):
        out = gaps(days=30)
    assert [x.split(" ", 1)[1] for x in out["not_helpful"]] == ["How many jobs failed on the grid?"]
    assert out["failed"][0].endswith("-> The agent could not answer: timeout")
    assert any("weather in Lisbon" in x and "weather" in x.split("words:")[1] for x in out["no_data_found"])
    assert not any("elasticsearch cluster now" in x for x in out["no_data_found"])
    assert len(out["no_data_found"]) == 1                              # not "hello", not the follow-up
    assert out["learned_on_gone_data"] and out["learned_on_gone_data"][0].endswith("Seconds of CPU")


def test_where_the_time_of_the_answers_goes(live):
    from superset.extensions import db

    from supagent.knowledge.quality import usage_stats
    from supagent.models import Usage

    db.session.query(Usage).delete()
    mid = _chat("Jobs per application")
    db.session.add_all([
        Usage(message_id=mid, seconds=100.0, llm_calls=2, llm_seconds=90.0, prompt_tokens=10000, completion_tokens=200,
              cached_tokens=6000, tool_calls=1, tool_seconds=5.0, failed_calls=0),
        Usage(message_id=mid + 100000, seconds=40.0, llm_calls=2, llm_seconds=30.0, prompt_tokens=10000,
              completion_tokens=100, cached_tokens=2000, tool_calls=3, tool_seconds=8.0, failed_calls=1,
              nudges=1)])
    db.session.commit()
    out = usage_stats(days=7)
    assert (out["answers"], out["llm_calls_per_answer"], out["tool_calls_per_answer"]) == (2, 2.0, 2.0)
    assert out["llm_share"] == round(120 / 140, 3) and out["prompt_tokens_per_call"] == 5000
    assert out["prompt_cache_share"] == 0.4 and out["seconds_median"] in (40.0, 100.0)
    assert out["sent_back_per_answer"] == 0.5 and out["failed_calls_per_answer"] == 0.5
    assert out["slowest"][0].startswith(f"#{mid} 100 s (LLM 90 s in 2 calls") and "Jobs per application" in out["slowest"][0]


def test_the_commands_print_them(live, app):
    from superset.extensions import db

    from supagent.models import Usage

    db.session.query(Usage).delete()
    db.session.commit()
    from supagent.cli import supagent

    runner = app.test_cli_runner()
    stats = runner.invoke(supagent, ["stats", "--days", "1"])
    assert stats.exit_code == 0 and json.loads(stats.output) == {"answers": 0}
    evaluate = runner.invoke(supagent, ["evaluate", "--user", "admin"])
    assert evaluate.exit_code == 0 and "answers" in json.loads(evaluate.output)
    found = runner.invoke(supagent, ["gaps", "--user", "admin"])
    assert found.exit_code == 0 and "not helpful (" in found.output and "learned on gone data (" in found.output
