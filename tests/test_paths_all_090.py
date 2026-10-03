"""0.9: every answer marked Helpful keeps its path, small request or big one (a count, an extract, a check): the
data it used (tables with their fields, metrics with their labels) and its steps, read from the answer without the
LLM, with no value of the case. An answer that ran no query to keep (a health check, a comparison with usual) is
learned as its path. The path is given with the learned answer to the next similar question, shown in Learned by
the agent, and corrected and confirmed by an admin."""

from __future__ import annotations

import json

import pytest
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401  (the fixture)
from test_paths import OneReply, _clean

EXTRACT = [
    {"tool": "describe_data", "status": "done", "args": {"index": "jobs"}, "result": "..."},
    {"tool": "execute_sql", "status": "done", "args": {"request": {"database_id": 1, "sql":
        "SELECT \"APP\", COUNT(*) AS n FROM \"jobs\" WHERE \"STATUS\" = 'FAILED' AND \"DAY\" = '20260921' GROUP BY \"APP\""}},
     "result": '{"success": true, "row_count": 3}'},
    {"tool": "execute_sql", "status": "error", "args": {"request": {"database_id": 1, "sql": "SELECT nope FROM nothing"}},
     "result": '{"success": false}'},
    {"tool": "execute_sql", "status": "done", "args": {"request": {"database_id": 1, "sql":
        "SELECT j.\"ID\", s.\"STEP\", s.\"SECONDS\" FROM \"jobs\" j JOIN \"steps\" s ON j.\"ID\" = s.\"ID\" WHERE j.\"APP\" = 'BILLING'"}},
     "result": '{"success": true, "row_count": 40}'},
    {"tool": "export_excel", "status": "done", "args": {"sql": "SELECT \"ID\", \"APP\", \"STATUS\" FROM \"jobs\" WHERE \"DAY\" = '20260921'",
                                                        "database_id": 1}, "result": '{"rows": 812}'},
]
CHECK = [
    {"tool": "system_links", "status": "done", "args": {"names": ["grid-a"]}, "result": "{}"},
    {"tool": "check_health", "status": "done", "args": {"start": "2026-09-22 00:00", "end": "2026-09-22 05:00",
                                                         "entities": ["srv-1", "srv-2"], "checks": ["memory_low", "cpu_saturated"]},
     "result": '{"breaches": []}'},
    {"tool": "compare_to_usual", "status": "done", "args": {"promql": 'avg by (node) (node_load1{node=~"srv-.*"})',
                                                             "start": "2026-09-22 00:00", "end": "2026-09-22 05:00"},
     "result": '{"unusual": 0}'},
]


def test_what_an_answer_used_is_read_from_its_steps(app):
    from supagent.knowledge import paths

    p = paths.used(EXTRACT)
    assert p["tables"] == {"jobs": ["APP", "STATUS", "DAY", "ID"], "steps": ["STEP", "SECONDS", "ID"]}
    assert p["steps"][0] == "describe_data: jobs"
    assert p["steps"][1].startswith('execute_sql on jobs: SELECT "APP", COUNT(*) AS n FROM "jobs" WHERE "STATUS" = ?')
    assert "FAILED" not in " ".join(p["steps"]) and "20260921" not in " ".join(p["steps"]) and "BILLING" not in " ".join(p["steps"])
    assert p["steps"][2].startswith("execute_sql on jobs, steps:") and p["steps"][3].startswith("export_excel on jobs:")
    assert len(p["steps"]) == 4 and not any("nothing" in s for s in p["steps"])          # a failed step led nowhere
    text = paths.path_text(p)
    assert text.startswith("Data: jobs (fields APP, STATUS, DAY, ID); steps (fields STEP, SECONDS, ID)\nSteps:\n1. describe_data: jobs")
    assert paths.path_brief(p).endswith("steps: describe_data -> execute_sql -> execute_sql -> export_excel")
    c = paths.used(CHECK)
    assert c["metrics"] == {"node_load1": ["node"]} and c["tables"] == {}
    assert c["steps"] == ["system_links for the parts the question names",
                          "check_health (memory_low, cpu_saturated) for the parts the question names",
                          'compare_to_usual: avg by (node) (node_load1{node=~"?"})']
    twice = paths.used([EXTRACT[1], EXTRACT[1]])
    assert len(twice["steps"]) == 1 and twice["steps"][0].endswith(" ×2")


def _answer(app, question, trace, world_db):
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, Message

    alice = sm.find_user(username="alice")
    conv = Conversation(user_id=alice.id, title="t")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content=question, status="done"))
    steps = json.loads(json.dumps(trace).replace('"database_id": 1', f'"database_id": {world_db}'))
    answer = Message(conversation_id=conv.id, role="assistant", status="done", content="Here it is.", feedback=1, steps=steps)
    db.session.add(answer)
    db.session.commit()
    return conv.id, answer.id


@pytest.fixture()
def clean(world, app):  # noqa: F811
    _clean()
    yield world
    from superset.extensions import db

    from supagent.models import Conversation, Message

    db.session.rollback()
    db.session.query(Message).delete()
    db.session.query(Conversation).delete()
    db.session.commit()
    _clean()


def test_an_extract_keeps_its_query_and_its_path_and_an_admin_corrects_it(clean, app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import experience, paths
    from supagent.models import Recipe
    from supagent.security import acting_as

    jobs = clean["jobs"].id
    monkeypatch.setattr(experience, "final_database", lambda trace: jobs)
    _cid, aid = _answer(app, "Extract the failed jobs of 2026-09-21 with their application", EXTRACT, jobs)
    generic = {"question": "Extract the failed jobs of a day with their application", "generic": True, "reusable": True}
    rid = experience.learn_from_helpful(aid, llm=OneReply(generic))
    r = db.session.get(Recipe, rid)
    assert r.tool == "export_excel" and r.query.startswith('SELECT "ID", "APP", "STATUS" FROM "jobs"')
    path = paths.of_recipe(r)
    assert path["tables"]["jobs"] == ["APP", "STATUS", "DAY", "ID"] and len(path["steps"]) == 4
    with acting_as("alice"):
        given = experience.recipes_for("extract the failed jobs of yesterday with their application")
    assert given and given[0]["path"].startswith("jobs (fields APP, STATUS, DAY, ID); steps (fields STEP, SECONDS, ID); steps: describe_data")
    with _client(app, "admin") as c:
        listed = next(x for x in c.get("/supagent/dictionary/api/recipes").get_json()["recipes"] if x["id"] == rid)
        assert listed["path"].startswith("Data: jobs (fields APP, STATUS, DAY, ID)")
        waiting = next(x for x in c.get("/supagent/admin/api/review").get_json()["recipes"] if x["id"] == rid)
        assert waiting["path"] == listed["path"]
        mine = "Data: jobs (STATUS, APP, DAY), steps by ID.\nSteps: count the failed per application, then export every failed job."
        out = c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"path": mine, "status": "confirmed"}).get_json()
        assert out["status"] == "confirmed"
        again = next(x for x in c.get("/supagent/dictionary/api/recipes").get_json()["recipes"] if x["id"] == rid)
        assert again["path"] == mine and again["edited_by"] == "admin" and again["query"] == listed["query"]
    with acting_as("alice"):
        assert experience.recipes_for("extract the failed jobs of yesterday with their application")[0]["path"] == " ".join(mine.split())
    with _client(app, "admin") as c:                          # emptied: back to what the answer did
        c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"path": ""})
        assert next(x for x in c.get("/supagent/dictionary/api/recipes").get_json()["recipes"] if x["id"] == rid)["path"] == listed["path"]
    with _client(app, "alice") as c:
        assert c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"path": "x"}).status_code == 403


def test_a_check_with_no_query_is_learned_as_its_path(clean, app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import experience, paths
    from supagent.models import Recipe
    from supagent.security import acting_as

    metrics = clean["metrics"].id
    monkeypatch.setattr(paths, "_database", lambda trace: metrics)
    monkeypatch.setattr(experience, "final_database", lambda trace: 0)
    generic = {"question": "Check that the servers of a pool are healthy over a night", "generic": True, "reusable": True}
    _c, a1 = _answer(app, "Can you confirm the servers of grid-a were fine last night?", CHECK, metrics)
    rid = experience.learn_from_helpful(a1, llm=OneReply(generic))
    r = db.session.get(Recipe, rid)
    assert (r.tool, r.status, r.uses, r.database_id) == ("path", "helpful", 1, metrics) and r.target == "node_load1"
    assert r.query.startswith("Data: metric node_load1 (labels node)\nSteps:\n1. system_links for the parts the question names")
    assert "srv-1" not in r.query and "2026-09-22" not in r.query                   # no value of that one case
    _c, a2 = _answer(app, "Confirm the servers of grid-b were fine this night", CHECK, metrics)
    assert experience.learn_from_helpful(a2, llm=OneReply(generic)) == rid and db.session.get(Recipe, rid).uses == 2
    looked = [CHECK[0], {"tool": "search_knowledge", "status": "done", "args": {"query": "grid"}, "result": "[]"}]
    _c, a3 = _answer(app, "What is grid-a?", looked, metrics)
    assert experience.learn_from_helpful(a3, llm=OneReply(generic)) is None          # it only looked things up
    with _client(app, "admin") as c:
        assert c.post(f"/supagent/dictionary/api/recipes/{rid}/check", json={}).get_json()["note"] == "a path: nothing to run"
        text = r.query + "\n4. say which server breached, or that none did"
        assert c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"query": text, "status": "confirmed"}).get_json()["status"] == "confirmed"
    with acting_as("admin"):
        given = experience.recipes_for("check that the servers of the pool were healthy over the night")
    assert given[0]["tool"] == "path" and "4. say which server breached" in given[0]["query"]
    with acting_as("alice"):                                  # she may not query the metrics: not her path
        assert experience.recipes_for("check that the servers of the pool were healthy over the night") == []


def test_the_agent_is_given_the_path_with_the_learned_answer(clean, app, monkeypatch):
    from test_prompt import _agent

    from supagent.knowledge import experience
    from supagent.security import acting_as

    jobs = clean["jobs"].id
    monkeypatch.setattr(experience, "final_database", lambda trace: jobs)
    _cid, aid = _answer(app, "Extract the failed jobs of 2026-09-21 with their application", EXTRACT, jobs)
    experience.learn_from_helpful(aid, llm=OneReply({"question": "Extract the failed jobs of a day with their application",
                                                     "generic": True, "reusable": True}))
    a = _agent()
    with acting_as("alice"):
        blocks = a._question_blocks("Extract the failed jobs of yesterday with their application")
    assert "Ways that answered similar questions before" in blocks
    assert "its path: jobs (fields APP, STATUS, DAY, ID); steps (fields STEP, SECONDS, ID); steps: describe_data -> execute_sql" in blocks
