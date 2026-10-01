"""Where the data of the questions was (0.7): one row per table the answers read, with the words of the questions that
led there (a word that also led to other tables, and other databases, said so), the answers and the Helpful among
them; searched by a word or a table, by database, paged; only the databases the user may query; an admin's Wrong
takes a word away from a table."""

from __future__ import annotations

import pytest

from conftest import login

API = "/supagent/dictionary/api/where_data"


@pytest.fixture()
def learned(app):
    with app.app_context():
        from superset.extensions import db
        from superset.models.core import Database

        from supagent.models import Association, Conversation, Message

        main = Database(database_name="where main", sqlalchemy_uri="sqlite://")
        replica = Database(database_name="where replica", sqlalchemy_uri="sqlite://")
        db.session.add_all([main, replica])
        db.session.flush()
        c = Conversation(user_id=1, title="t")
        db.session.add(c)
        db.session.flush()
        msgs = []
        for fb in (1, None, None):
            m = Message(conversation_id=c.id, role="assistant", content="a", status="done", feedback=fb)
            db.session.add(m)
            db.session.flush()
            msgs.append(m.id)
        rows = [("fail", main.id, "batch-jobs", 5, msgs[:2]), ("job", main.id, "batch-jobs", 7, msgs),
                ("job", replica.id, "batch-jobs", 2, msgs[2:]), ("replica", replica.id, "batch-jobs", 2, msgs[2:]),
                ("cpu", main.id, "node_cpu_seconds_total", 1, msgs[1:2])]
        for word, dbid, name, uses, ids in rows:
            db.session.add(Association(word=word, database_id=dbid, kind="index" if "jobs" in name else "metric",
                                       parent="", name=name, uses=uses, messages=ids))
        db.session.commit()
        ids = {"main": main.id, "replica": replica.id, "conv": c.id}
        db.session.remove()
    yield ids
    with app.app_context():
        from superset.extensions import db
        from superset.models.core import Database

        from supagent.models import Association, Conversation, Message

        db.session.query(Association).filter(Association.database_id.in_([ids["main"], ids["replica"]])).delete(
            synchronize_session=False)
        db.session.query(Message).filter(Message.conversation_id == ids["conv"]).delete()
        db.session.query(Conversation).filter(Conversation.id == ids["conv"]).delete()
        db.session.query(Database).filter(Database.id.in_([ids["main"], ids["replica"]])).delete(synchronize_session=False)
        db.session.commit()
        db.session.remove()


def test_one_row_per_table_with_the_words_that_led_there(app, learned):
    c = app.test_client()
    login(c, "admin")
    d = c.get(API).get_json()
    mine = [t for t in d["tables"] if t["database_id"] in (learned["main"], learned["replica"])]
    jobs = next(t for t in mine if t["name"] == "batch-jobs" and t["database_id"] == learned["main"])
    assert jobs["uses"] == 12 and jobs["answers"] == 3 and jobs["helpful"] == 1 and jobs["database"] == "where main"
    words = {w["word"]: w for w in jobs["words"]}
    assert [w["word"] for w in jobs["words"]] == ["job", "fail"]                     # the most used first
    assert words["job"]["elsewhere"] == 1 and words["job"]["other_databases"] == ["where replica"]
    assert words["fail"]["elsewhere"] == 0 and words["fail"]["other_databases"] == []   # led only there
    replica = next(t for t in mine if t["database_id"] == learned["replica"])
    assert {w["word"] for w in replica["words"]} == {"job", "replica"} and d["is_admin"]
    assert {"id": learned["replica"], "name": "where replica"} in d["databases"]


def test_searched_filtered_and_paged(app, learned):
    c = app.test_client()
    login(c, "admin")
    found = c.get(API + "?q=failed").get_json()["tables"]                          # the word's stem
    assert [(t["name"], t["database_id"]) for t in found if t["database_id"] == learned["main"]] == \
        [("batch-jobs", learned["main"])]
    assert [t["name"] for t in c.get(API + "?q=node_cpu").get_json()["tables"]] == ["node_cpu_seconds_total"]
    only = c.get(API + f"?database={learned['replica']}").get_json()
    assert only["total"] == 1 and only["tables"][0]["database"] == "where replica"
    page = c.get(API + f"?database={learned['main']}&limit=1&offset=1").get_json()
    assert page["total"] == 2 and [t["name"] for t in page["tables"]] == ["node_cpu_seconds_total"]
    assert c.get(API + "?limit=x").status_code == 400


def test_only_the_users_databases_and_wrong_for_admins(app, learned):
    alice = app.test_client()
    login(alice, "alice")                                       # neither database: nothing of them
    d = alice.get(API).get_json()
    assert not [t for t in d["tables"] if t["database_id"] in (learned["main"], learned["replica"])]
    assert alice.post(API + "/forget", json={"word": "job", "database_id": learned["main"], "kind": "index",
                                             "name": "batch-jobs"}).status_code in (403, 404)   # an admin's
    admin = app.test_client()
    login(admin, "admin")
    r = admin.post(API + "/forget", json={"word": "job", "database_id": learned["main"], "kind": "index",
                                          "parent": "", "name": "batch-jobs"}).get_json()
    assert r == {"forgotten": 1}
    jobs = next(t for t in admin.get(API + f"?database={learned['main']}").get_json()["tables"]
                if t["name"] == "batch-jobs")
    assert [w["word"] for w in jobs["words"]] == ["fail"] and jobs["uses"] == 5
