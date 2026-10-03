"""0.9: investigation paths (knowledge.paths): the path of an investigation marked Helpful is written by the LLM
(generic: the names of tools, tables and fields stay, the values of the case do not), waits for an admin, and
once confirmed is given with the next investigation; a learned answer of its own kind (not a query to run)."""

from __future__ import annotations

import json

import pytest
from test_knowledge import world  # noqa: F401  (the fixture)

PATH = {"problem": "The night batch of a family of applications is late",
        "cause": "An upstream feed arrived late",
        "steps": ["compare_groups on the runs table, counting the runs done in that window: fewer than usual",
                  "compare_groups grouped by ASSET: the missing runs are of one asset class only",
                  "execute_sql on the feeds table for that position date: the feed of that asset class is LATE"],
        "confirm": "Every waiting run reads that feed and the runs of the other asset classes are on time.",
        "ruled_out": "A memory alert on a server those runs did not wait for.", "reusable": True}
TRACE = [
    {"tool": "compare_groups", "status": "done", "args": {"table": "jobs", "measure": "count", "group_by": ["ASSET"]},
     "result": json.dumps({"conclusion": 'The change is concentrated on "ASSET": NORTH (0 against 200 usually)'})},
    {"tool": "execute_sql", "status": "error", "args": {"request": {"database_id": 1, "sql": "SELECT x FROM nothing"}},
     "result": '{"success": false}'},
    {"tool": "execute_sql", "status": "done",
     "args": {"request": {"database_id": 1, "sql": "SELECT FEED, STATUS FROM feeds WHERE DAY = '20260921'"}},
     "result": '{"success": true, "rows": [{"FEED": "MD_NORTH", "STATUS": "LATE"}]}'},
    {"tool": "check_health", "status": "done", "args": {"start": "2026-09-22 00:00", "end": "2026-09-22 05:00"},
     "result": '{"breaches": []}'},
]


class OneReply:
    def __init__(self, reply) -> None:
        self.reply, self.seen = reply, []

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append(messages)
        return {"role": "assistant", "content": self.reply if isinstance(self.reply, str) else json.dumps(self.reply)}


def _clean():
    from superset.extensions import db

    from supagent.models import Recipe

    db.session.query(Recipe).delete()
    db.session.commit()


def test_the_path_is_written_generic_and_only_when_a_cause_was_found(ctx):
    from supagent.knowledge import paths

    llm = OneReply(PATH)
    p = paths.write("Why is the batch of BILLING late on 2026-09-22, job 202609220017?", "The feed was late.", TRACE, llm)
    assert p["problem"] == PATH["problem"] and len(p["steps"]) == 3
    asked = llm.seen[0][1]["content"]
    assert "1. compare_groups:" in asked and "-> The change is concentrated" in asked       # what a comparison concluded
    assert "SELECT x FROM nothing" not in asked                                               # a failed step led nowhere
    text = paths.text_of(p)
    assert text.startswith("Problem: The night batch") and "\n3. execute_sql on the feeds table" in text
    assert "What confirms it: Every waiting run" in text and "Ruled out: A memory alert" in text
    leaky = dict(PATH, cause="The feed of 2026-09-22 arrived late for job 202609220017")
    p = paths.write("Why is the batch late on 2026-09-22, job 202609220017?", "...", TRACE, OneReply(leaky))
    assert "2026-09-22" not in p["cause"] and "202609220017" not in p["cause"]              # the case's values never stay
    assert paths.write("why?", "No cause found.", TRACE, OneReply(dict(PATH, reusable=False))) is None
    assert paths.write("why?", "x", TRACE[:2], OneReply(PATH)) is None                        # too little was done
    assert paths.write("why?", "x", TRACE, OneReply("not json")) is None


def test_a_path_waits_for_an_admin_then_is_given(world, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import paths
    from supagent.knowledge.experience import edit_recipe, check_recipe_query, recipes_for
    from supagent.models import Recipe
    from supagent.security import acting_as

    _clean()
    try:
        with acting_as("admin"):
            r = paths.record(11, 1, "why is the batch late?", "The feed was late.", TRACE, OneReply(PATH))
            assert (r.tool, r.status, r.question, r.uses) == ("investigation", "helpful", PATH["problem"], 1)
            assert r.query.startswith("Problem: ") and r.generic is True
            assert paths.paths_for("why is the night batch late?") == []                     # not validated yet
            assert paths.paths_block("why is the night batch late?") == ""
            again = paths.record(12, 1, "why is it late again?", "The feed.", TRACE, OneReply(PATH))
            assert again.id == r.id and again.uses == 2 and again.confirmations == [11, 12]  # the same path: counted
            assert db.session.query(Recipe).count() == 1
            assert recipes_for("why is the night batch of a family late") == []              # never as a query to run
            assert check_recipe_query(r, r.query)["note"] == "an investigation path: nothing to run"
            edit_recipe(r, "The night batch is late", r.query + "\n(checked by the team)", "admin")
            r.status = "confirmed"
            db.session.commit()
            shown: set[str] = set()
            block = paths.paths_block("why is the night batch late?", shown)
            assert block.startswith("\n\nInvestigation paths the team validated")
            assert "1. (found 2 times) Problem: The night batch of a family" in block and "(checked by the team)" in block
            assert shown == {f"recipe:{r.id}"}
            # another problem's words: with few paths kept, the validated ones are still at hand
            assert [p["id"] for p in paths.paths_for("is the ledger slow?")] == [r.id]
    finally:
        _clean()


def test_one_problem_has_several_known_causes_and_they_come_together(world, app):  # noqa: F811
    """The same symptom, different causes: each is its own path; a question of that kind gets them all, the
    closest and the most often found with their steps, the others in a line; another kind of problem stays out."""
    from superset.extensions import db

    from supagent.knowledge import paths
    from supagent.models import Recipe
    from supagent.security import acting_as

    causes = ["An upstream feed arrived late", "Servers of the pool ran out of memory",
              "A new release made one step slower", "The licence tokens of a service were used up",
              "A test campaign ran on the production pool"]
    _clean()
    try:
        with acting_as("admin"):
            ids = []
            for i, cause in enumerate(causes):
                r = paths.record(20 + i, 1, "why is the batch late?", "x", TRACE,
                                 OneReply(dict(PATH, cause=cause, confirm=f"What shows cause {i}.")))
                ids.append(r.id)
            assert len(set(ids)) == 5                                  # the same problem, another cause: another path
            again = paths.record(30, 1, "late again", "x", TRACE, OneReply(dict(PATH, cause=causes[1])))
            assert again.id == ids[1] and again.uses == 2
            other = paths.record(31, 1, "errors", "x", TRACE, OneReply(dict(
                PATH, problem="A web service answers with many errors", cause="Its database refused connections")))
            db.session.query(Recipe).update({"status": "confirmed"})
            db.session.commit()
            found = paths.paths_for("Why is the night batch of the billing applications late today?")
            assert [p["id"] for p in found] == [ids[1], ids[0], ids[2], ids[3], ids[4]]       # the most found first
            assert other.id not in [p["id"] for p in found]
            shown: set[str] = set()
            block = paths.paths_block("Why is the night batch of the billing applications late today?", shown)
            assert "1. (found 2 times) Problem: The night batch of a family of applications is late" in block
            assert "\n   Cause found: Servers of the pool ran out of memory" in block
            assert "\n3. Problem:" in block and "\n4. " not in block
            assert ("Other causes found for such a problem:\n- The licence tokens of a service were used up (what "
                    "confirms it: What shows cause 3.)\n- A test campaign ran on the production pool") in block
            assert shown == {f"recipe:{i}" for i in ids}
            assert [p["id"] for p in paths.paths_for("many errors on the web service since noon")] == [other.id]
    finally:
        _clean()


def test_helpful_on_an_investigation_keeps_its_path_not_its_last_query(world, app, monkeypatch):  # noqa: F811
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge import experience, paths
    from supagent.models import Conversation, Message, Recipe

    _clean()
    alice = sm.find_user(username="alice")
    conv = Conversation(user_id=alice.id, title="t")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="Why is the night batch late?", status="done"))
    answer = Message(conversation_id=conv.id, role="assistant", status="done", content="The feed was late.", feedback=1,
                     steps=[dict(t) for t in TRACE])
    db.session.add(answer)
    db.session.commit()
    cid, aid, uid, jobs_id = conv.id, answer.id, alice.id, world["jobs"].id
    monkeypatch.setattr(experience, "final_database", lambda trace: jobs_id)
    try:
        rid = experience.learn_from_helpful(aid, llm=OneReply(PATH))
        r = db.session.get(Recipe, rid)
        assert r.tool == "investigation" and r.message_id == aid and r.user_id == uid
        assert paths.is_investigation("Why is the night batch late?") and not paths.is_investigation("How many jobs?")
        experience.feedback(aid, -1)                                                          # Helpful taken back
        assert db.session.get(Recipe, rid) is None
    finally:
        db.session.rollback()
        db.session.query(Message).filter_by(conversation_id=cid).delete()
        db.session.query(Conversation).filter_by(id=cid).delete()
        db.session.commit()
        _clean()
