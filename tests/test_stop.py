"""Stop: the answer is stopped at once for the user (the chat takes the next question right
away), and the run that was in the middle of a step never writes over it nor learns from it."""

from __future__ import annotations

import json

import pytest

from conftest import login


def _running(user: str = "alice"):
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, Message

    conv = Conversation(user_id=sm.find_user(username=user).id, title="How many jobs failed?")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="How many jobs failed?", status="done"))
    answer = Message(conversation_id=conv.id, role="assistant", status="pending", steps=[])
    db.session.add(answer)
    db.session.commit()
    return conv.id, answer.id


def _press_stop(message_id: int) -> None:
    """What the Stop button does, committed while the agent is in the middle of a step."""
    import datetime as dt

    from superset.extensions import db

    from supagent.models import Message

    (db.session.query(Message).filter(Message.id == message_id)
     .update({"status": "cancelled", "content": "(stopped)", "finished_at": dt.datetime.utcnow()},
             synchronize_session=False))
    db.session.commit()


class FakeAgent:
    """Runs one query, then the user presses Stop during the next step (an LLM call)."""
    checks_again = False
    stop = True

    def __init__(self, username, on_step=None, rich_results=False, should_stop=None, **_kw):
        self.on_step, self.should_stop = on_step, should_stop

    def ask(self, question, history=None):
        from supagent.agent import Cancelled

        full = json.dumps({"success": True, "columns": [{"name": "n"}], "rows": [{"n": 7}], "row_count": 1})
        trace = [{"tool": "execute_sql", "called": "execute_sql", "status": "done", "seconds": 0.2, "full": full,
                  "args": {"request": {"database_id": 1, "sql": "SELECT COUNT(*) AS n FROM jobs"}}, "result": full}]
        self.on_step(trace)                                   # progress is saved while it runs
        if self.stop:
            _press_stop(self.message_id)
        if self.checks_again and self.should_stop():          # the next step starts: it stops here
            raise Cancelled("stopped by the user")
        return "7 jobs failed.", trace                        # or the step that was running ends

    def close(self):
        pass


@pytest.mark.parametrize("checks_again", [False, True])
def test_a_stopped_answer_is_never_written_over(app, monkeypatch, checks_again):
    from superset.extensions import db

    from supagent import runner
    from supagent.knowledge import experience, generic
    from supagent.models import Conversation, Message, Recipe

    with app.app_context():
        cid, mid = _running()
        agent = type("Agent", (FakeAgent,), {"message_id": mid, "checks_again": checks_again})
        monkeypatch.setattr("supagent.agent.Agent", agent)
        learned = []
        monkeypatch.setattr(generic, "generalize", lambda *a, **k: learned.append("chat name") or {})
        monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: learned.append("timings"))
        recipes = db.session.query(Recipe).count()
        runner.run_answer(mid)
        m = db.session.get(Message, mid)
        assert (m.status, m.content) == ("cancelled", "(stopped)")
        assert m.steps and m.steps[0]["tool"] == "execute_sql"          # what it did before Stop stays shown
        assert not m.results and not m.files and learned == []
        assert db.session.query(Recipe).count() == recipes
        assert db.session.get(Conversation, cid).title == "How many jobs failed?"


def test_an_answer_that_is_not_stopped_is_saved_and_learned(app, monkeypatch):
    from superset.extensions import db

    from supagent import runner
    from supagent.knowledge import experience, generic
    from supagent.models import Conversation, Message

    with app.app_context():
        _cid, mid = _running()
        monkeypatch.setattr("supagent.agent.Agent", type("Agent", (FakeAgent,), {"message_id": mid, "stop": False}))
        learned = []
        monkeypatch.setattr(generic, "generalize", lambda *a, **k: learned.append("chat name") or {
            "question": "How many jobs failed on a given day?", "title": "Failed jobs", "reusable": True,
            "generic": True, "same_as": None})
        monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: learned.append("timings"))
        runner.run_answer(mid)
        m = db.session.get(Message, mid)
        assert (m.status, m.content) == ("done", "7 jobs failed.") and m.results[0]["rows"] == [[7]]
        assert learned == ["timings", "chat name"]            # no learned answer without Helpful
        assert db.session.get(Conversation, m.conversation_id).title == "Failed jobs"


def test_an_answer_stopped_before_it_starts_does_not_run(app, monkeypatch):
    from supagent import runner

    with app.app_context():
        _cid, mid = _running()
        _press_stop(mid)
        monkeypatch.setattr("supagent.agent.Agent", lambda *a, **k: pytest.fail("the agent must not start"))
        runner.run_answer(mid)


def test_stop_frees_the_chat_at_once(app, monkeypatch):
    from superset.extensions import db

    from supagent import tasks
    from supagent.models import Message

    started = []
    monkeypatch.setattr(tasks, "dispatch_answer", lambda message_id: started.append(message_id) or "thread")
    with app.app_context():
        cid, mid = _running()
        db.session.query(Message).filter(Message.id == mid).update({"status": "running"})
        db.session.commit()
    with app.test_client() as c:
        login(c, "alice")
        assert c.post("/supagent/api/ask", json={"question": "and yesterday?", "conversation_id": cid}).status_code == 409
        assert c.post(f"/supagent/api/messages/{mid}/cancel").get_json()["status"] == "cancelled"
        r = c.post("/supagent/api/ask", json={"question": "and yesterday?", "conversation_id": cid})
        assert r.status_code == 200 and started == [r.get_json()["message_id"]]
        m = c.get(f"/supagent/api/messages/{mid}").get_json()
        assert (m["status"], m["content"]) == ("cancelled", "(stopped)")


def test_stop_on_a_finished_answer_keeps_it(app):
    from superset.extensions import db

    from supagent.models import Message

    with app.app_context():
        _cid, mid = _running()
        db.session.query(Message).filter(Message.id == mid).update({"status": "done", "content": "7 jobs failed."})
        db.session.commit()
    with app.test_client() as c:
        login(c, "alice")
        assert c.post(f"/supagent/api/messages/{mid}/cancel").get_json()["status"] == "done"
        assert c.get(f"/supagent/api/messages/{mid}").get_json()["content"] == "7 jobs failed."
