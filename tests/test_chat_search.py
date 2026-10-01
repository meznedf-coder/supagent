"""The search box of the chat: a user finds their own earlier chats (never someone else's), each conversation once
with the message found, the words typed taken as words (no wildcard), nothing for less than two characters."""

from __future__ import annotations

import pytest

from conftest import login


@pytest.fixture()
def chats(app):
    with app.app_context():
        from superset.extensions import db, security_manager as sm

        from supagent.models import Conversation, Message

        alice, bob = sm.find_user(username="alice").id, sm.find_user(username="bob").id
        made = []
        for uid, title, turns in (
                (alice, "Payroll batch", [("user", "Why was the payroll batch late on Monday?"),
                                          ("assistant", "The payroll batch waited for the ledger export (40 min).")]),
                (alice, "Ledger", [("user", "Ledger totals per desk yesterday"),
                                   ("assistant", "Totals per desk: 12 rows. The payroll feed was on time.")]),
                (bob, "Bob's payroll", [("user", "payroll costs of my team"), ("assistant", "Bob's payroll: 3 rows")])):
            c = Conversation(user_id=uid, title=title)
            db.session.add(c)
            db.session.flush()
            ids = []
            for role, content in turns:
                m = Message(conversation_id=c.id, role=role, content=content, status="done")
                db.session.add(m)
                db.session.flush()
                ids.append(m.id)
            made.append((c.id, ids))
        db.session.commit()
        db.session.remove()
    yield made                                   # no app context held: each request has its own g (its own user)
    with app.app_context():
        from superset.extensions import db

        from supagent.models import Conversation, Message

        for cid, _ids in made:
            db.session.query(Message).filter(Message.conversation_id == cid).delete()
            db.session.query(Conversation).filter(Conversation.id == cid).delete()
        db.session.commit()


def test_a_user_finds_their_own_chats_only(app, chats):
    (payroll, (q1, a1)), (ledger, (q2, a2)), (bobs, _ids) = chats
    c = app.test_client()
    login(c, "alice")
    d = c.get("/supagent/api/chats/search?q=payroll").get_json()
    got = [(r["conversation_id"], r["message_id"]) for r in d["results"]]
    assert d["by"] == "words"                                            # SQLite: no knowledge store
    assert {cid for cid, _m in got} == {payroll, ledger}                 # hers, each once; never Bob's
    assert (ledger, a2) in got and [r["title"] for r in d["results"]].count("Payroll batch") == 1
    d = c.get("/supagent/api/chats/search?q=payroll%20late").get_json()   # every word
    assert [r["conversation_id"] for r in d["results"]] == [payroll]
    assert "late" in d["results"][0]["snippet"]
    assert c.get("/supagent/api/chats/search?q=%25").get_json()["results"] == []      # "%" is not a wildcard
    assert c.get("/supagent/api/chats/search?q=p").get_json()["results"] == []        # too short
    c = app.test_client()
    login(c, "bob")
    d = c.get("/supagent/api/chats/search?q=payroll").get_json()
    assert [r["conversation_id"] for r in d["results"]] == [bobs]
    c = app.test_client()
    login(c, "nobody")                                                    # no chat role
    assert c.get("/supagent/api/chats/search?q=payroll").status_code in (401, 403)
