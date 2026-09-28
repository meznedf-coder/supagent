"""Learned answers and chats are named by a short generic question, not by the user's words
(which can hold the data of one case): written by the LLM after the answer, plain without it."""

from __future__ import annotations

import json

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


class FakeLLM:
    """Answers with `reply`, or with the replies of a list one after the other."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls.append(messages[-1]["content"])
        reply = self.reply.pop(0) if isinstance(self.reply, list) else self.reply
        if isinstance(reply, Exception):
            raise reply
        return {"content": "<think>...</think>" + json.dumps(reply)}


def test_plain_questions_lose_the_values_of_one_case(app):
    from supagent.knowledge.generic import plain, plain_title

    assert plain("can you give me why this job is failing run_id=202609253742945492") == \
        "Why this job is failing run_id=<id>"
    assert plain('give me number of today error in batch_jobs, the field of error is STATUS_INFO="KO"') == \
        "Number of today error in batch_jobs, the field of error is STATUS_INFO=<value>"
    assert plain("failed jobs on 2026-09-24 14:00 for 20260924") == "Failed jobs on <date> for <date>"
    assert plain_title("show me the failed jobs of the application BILLING yesterday please") == \
        "The failed jobs of the application…"


def test_the_llm_writes_a_standalone_generic_question(app):
    from supagent.knowledge.generic import generalize

    llm = FakeLLM({"question": "Why did a given job (RUN_ID) fail?", "title": "Failed job details",
                   "reusable": True})
    g = generalize("the @timestamp_date is a timestamp", ["why is job 202609253742945492 failing?"],
                   "SELECT \"ERROR_DESCRIPTION\" FROM jobs WHERE \"RUN_ID\" = '202609253742945492'",
                   "execute_sql", llm=llm)
    assert g == {"question": "Why did a given job (RUN_ID) fail?", "title": "Failed job details",
                 "reusable": True, "generic": True, "same_as": None}
    assert "Earlier user message: why is job" in llm.calls[0] and "Final query (execute_sql)" in llm.calls[0]
    down = generalize("why is job 202609253742945492 failing?", [], None, llm=FakeLLM(RuntimeError("503")))
    assert down["generic"] is False and "<id>" in down["question"]      # plain, rewritten later
    hello = generalize("hello", [], None, llm=FakeLLM({"question": "", "title": "Greeting", "reusable": False}))
    assert (hello["title"], hello["reusable"], hello["generic"]) == ("Greeting", False, True)   # named, not learned


def test_the_values_of_one_case_never_stay(app):
    from supagent.knowledge.generic import case_values, generalize

    question = "number of failed jobs of application BILLING on 23 September for run 20260923"
    sql = "SELECT COUNT(*) FROM jobs WHERE APP = 'BILLING' AND STATUS_INFO = 'KO' AND day = '2026-09-23'"
    hard, soft = case_values([question], sql)
    assert hard == {"23 September", "20260923"} and soft == {"BILLING"}     # KO was not written by the user
    llm = FakeLLM([{"question": "Number of failed jobs of BILLING on 23 September", "title": "BILLING failures"},
                   {"question": "Number of failed jobs of a given application on a given day",
                    "title": "Failed jobs per application"}])
    g = generalize(question, [], sql, "execute_sql", llm=llm)
    assert g["question"] == "Number of failed jobs of a given application on a given day"
    assert "BILLING" in llm.calls[1] and "23 September" in llm.calls[1]           # sent back once
    stubborn = FakeLLM({"question": "Failed jobs of BILLING on 23 September (run 20260923)", "title": "BILLING"})
    g = generalize(question, [], sql, "execute_sql", llm=stubborn)
    assert len(stubborn.calls) == 2
    assert "23 September" not in g["question"] and "20260923" not in g["question"]   # replaced here
    assert "BILLING" in g["question"]           # a name the LLM keeps twice stays (it may be a code)
    code = generalize('errors today in batch_jobs, the field of error is STATUS_INFO="KO"', [],
                      "SELECT COUNT(*) FROM batch_jobs WHERE STATUS_INFO = 'KO'", "execute_sql",
                      llm=FakeLLM({"question": "Number of errors (STATUS_INFO = KO) in batch_jobs on a given day",
                                   "title": "Errors per day"}))
    assert "KO" in code["question"]              # the code that defines an error is kept


def test_the_llm_says_which_kept_question_is_the_same(app):
    from supagent.knowledge.generic import generalize

    kept = [(12, "Number of failed jobs of a given application on a given day"), (15, "Failed jobs per server")]
    llm = FakeLLM({"question": "Failed jobs count of a given application on a given day", "title": "Failed jobs",
                   "same_as": 12})
    g = generalize("and for RATES?", ["failed jobs of BILLING yesterday"], "SELECT 1", "execute_sql", llm=llm,
                   kept=kept)
    assert g["same_as"] == 12 and "12: Number of failed jobs" in llm.calls[0]
    wrong = FakeLLM({"question": "Failed jobs", "title": "Failed jobs", "same_as": 99})
    assert generalize("failed jobs", [], "SELECT 1", llm=wrong, kept=kept)["same_as"] is None


def _trace(sql, database_id):
    full = json.dumps({"success": True, "rows": [{"n": 1}], "row_count": 1})
    return [{"tool": "execute_sql", "called": "execute_sql", "status": "done", "seconds": 0.5,
             "args": {"request": {"database_id": database_id, "sql": sql}}, "full": full, "result": full}]


def test_learned_answers_keep_the_generic_question_and_merge_similar_ones(world):
    from superset.extensions import db

    from supagent.knowledge.experience import record_recipe
    from supagent.models import Recipe

    db.session.query(Recipe).delete()
    db.session.commit()
    jobs = world["jobs"].id
    sql = "SELECT \"ERROR_DESCRIPTION\" FROM jobs WHERE \"RUN_ID\" = '{}'"
    g = {"question": "Why did a given job fail?", "title": "Failed job", "reusable": True, "generic": True}
    r = record_recipe(301, 1, "why is job 202609253742945492 failing?", _trace(sql.format("202609253742945492"), jobs), g)
    assert r.question == "Why did a given job fail?" and r.generic and "202609253742945492" not in r.question
    again = record_recipe(302, 1, "and job 99999 ?", _trace(sql.format("99999"), jobs),
                          {**g, "question": "Why did a given job fail?"})
    assert again.id == r.id and again.uses == 2                          # the same one, used twice
    assert record_recipe(303, 1, "thanks!", _trace(sql.format("1"), jobs), {**g, "reusable": False}) is None
    assert db.session.query(Recipe).count() == 1
    # the LLM says it is the same question, answered with another query: one learned answer, the newest way
    other = "SELECT \"ERROR_DESCRIPTION\", \"STATUS\" FROM jobs WHERE \"RUN_ID\" IN ('{}')"
    same = record_recipe(304, 1, "and these two?", _trace(other.format("1"), jobs),
                         {**g, "question": "Error of given jobs", "same_as": r.id})
    assert same.id == r.id and same.uses == 3 and "IN" in same.query and db.session.query(Recipe).count() == 1
    same.status = "confirmed"
    db.session.commit()
    kept_way = same.query
    apart = record_recipe(305, 1, "why?", _trace(sql.format("7"), jobs), {**g, "same_as": r.id})
    assert apart.id != r.id and db.session.get(Recipe, r.id).query == kept_way   # a confirmed way stays


def test_the_first_answer_names_the_chat(world, monkeypatch):
    from superset.extensions import db

    from supagent import runner
    from supagent.knowledge import generic
    from supagent.models import Conversation, Message

    monkeypatch.setattr(generic, "generalize", lambda q, earlier, query, tool=None, llm=None, kept=None: {
        "question": "Why did a given job fail?", "title": "Failed job details", "reusable": True, "generic": True,
        "same_as": None})
    question = "can you give me why this job is failing id=202609253742945492"
    conv = Conversation(user_id=1, title=question[:120])
    db.session.add(conv)
    db.session.flush()
    user = Message(conversation_id=conv.id, role="user", content=question)
    db.session.add(user)
    db.session.commit()
    assert runner._name_chat(conv.id, question, [user], _trace("SELECT 1 FROM jobs", world["jobs"].id)) == \
        "Failed job details"
    assert db.session.get(Conversation, conv.id).title == "Failed job details"
    later = Message(conversation_id=conv.id, role="assistant", content="...", status="done")
    db.session.add(later)
    db.session.commit()
    monkeypatch.setattr(generic, "generalize", lambda *a, **k: {"question": "Other", "title": "Other title",
                                                                "reusable": True, "generic": True})
    assert runner._name_chat(conv.id, "and the next one?", [user, later],
                             _trace("SELECT 2 FROM jobs", world["jobs"].id)) is None
    assert db.session.get(Conversation, conv.id).title == "Failed job details"   # named once


def test_the_daily_learning_rewrites_older_answers_and_chats(world):
    from superset.extensions import db

    from supagent.knowledge.generic import tidy_learned
    from supagent.models import Conversation, Message, Recipe

    db.session.query(Recipe).delete()
    db.session.commit()
    jobs = world["jobs"].id
    for i, q in enumerate(("why is job 202609253742945492 failing", "why is job 11111111 failing now")):
        db.session.add(Recipe(question=q, words=q, tool="execute_sql", database_id=jobs, query="SELECT 1",
                              signature="same-shape", status="confirmed" if i else "helpful", uses=1,
                              confirmations=[400 + i] if i else None, generic=False))
    conv = Conversation(user_id=1, title="can you give me why this job is failing id=42")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="can you give me why this job is failing id=42"))
    db.session.commit()
    llm = FakeLLM({"question": "Why did a given job fail?", "title": "Failed job details", "reusable": True})
    out = tidy_learned(llm=llm)
    assert (out["answers"], out["merged"]) == (2, 1) and out["chats"] >= 1  # same query shape, similar question
    (r,) = db.session.query(Recipe).all()
    assert (r.question, r.status, r.uses, r.generic) == ("Why did a given job fail?", "confirmed", 2, True)
    assert db.session.get(Conversation, conv.id).title == "Failed job details"
    assert tidy_learned(llm=llm) == {"answers": 0, "chats": 0, "merged": 0}


def test_the_daily_learning_joins_what_the_llm_finds_already_kept(world):
    from superset.extensions import db

    from supagent.knowledge.experience import words
    from supagent.knowledge.generic import tidy_learned
    from supagent.models import Recipe

    db.session.query(Recipe).delete()
    db.session.commit()
    jobs = world["jobs"].id
    kept_q = "Number of failed jobs of a given application on a given day"
    old = Recipe(question=kept_q, words=" ".join(sorted(words(kept_q))),
                 tool="execute_sql", database_id=jobs, query="SELECT COUNT(*) FROM jobs WHERE APP = 'X'",
                 signature="shape-1", status="confirmed", uses=3, confirmations=[500], generic=True)
    raw_q = "failed jobs of application BILLING and RATES on 23 September"
    raw = Recipe(question=raw_q, words=" ".join(sorted(words(raw_q))), tool="execute_sql", database_id=jobs,
                 query="SELECT COUNT(*) FROM jobs WHERE APP IN ('BILLING', 'RATES')", signature="shape-2",
                 status="helpful", uses=1, generic=False)
    db.session.add_all([old, raw])
    db.session.commit()
    llm = FakeLLM([{"question": "Number of failed jobs of given applications on a given day",
                    "title": "Failed jobs", "same_as": old.id}])
    assert tidy_learned(llm=llm) == {"answers": 1, "chats": 0, "merged": 1}
    (r,) = db.session.query(Recipe).all()
    assert (r.id, r.status, r.uses, r.query) == (old.id, "confirmed", 4, "SELECT COUNT(*) FROM jobs WHERE APP = 'X'")
    assert f"{old.id}: Number of failed jobs" in llm.calls[0]


def test_the_daily_tidying_stops_at_the_time_limit(world):
    """The learning run's LLM steps stop at its time limit (a run once looked blocked at the end
    rewriting old answers one LLM call after another); the next run goes on."""
    from superset.extensions import db

    from supagent.knowledge.generic import tidy_learned
    from supagent.models import Recipe

    db.session.query(Recipe).delete()
    db.session.commit()
    for i in range(3):
        db.session.add(Recipe(question=f"failed jobs of app {i} on the 23rd", words="fail job", tool="execute_sql",
                              database_id=world["jobs"].id, query=f"SELECT {i}", signature=f"s{i}",
                              status="helpful", uses=1, generic=False))
    db.session.commit()
    llm = FakeLLM({"question": "Failed jobs of a given application on a given day", "title": "Failed jobs",
                   "reusable": True})
    assert tidy_learned(llm=llm, deadline=0.0) == {"answers": 0, "chats": 0, "merged": 0, "stopped": "time limit"}
    assert not llm.calls
