"""The admins' LLM usage page: every LLM call is recorded with what it was for (an answer for a person, the
learning, the Context, the memory...) and its size, time and failure; the report adds them up per period,
person, task and model; the page and its data are for admins only; old calls are removed."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import login


class Server:
    """What the LLM server answers (llama.cpp-like: usage and timings)."""

    def __init__(self, status: int = 200):
        self.status = status

    def request(self, method, url, headers=None, verify=None, timeout=None, data=None):
        class R:
            status_code = self.status
            text = "error" if self.status >= 400 else ""

            @staticmethod
            def json():
                return {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 1200, "completion_tokens": 30}, "timings": {"prompt_n": 200}}
        return R()


@pytest.fixture()
def calls(ctx, monkeypatch):
    from superset.extensions import db

    from supagent import llm as L
    from supagent.models import LLMCall

    db.session.query(LLMCall).delete()
    db.session.commit()
    monkeypatch.setattr(L, "RETRY_DELAYS", ())
    yield L
    db.session.query(LLMCall).delete()
    db.session.commit()


def _client(L, status=200):
    c = object.__new__(L.LLM)
    c.cfg = L.LLMConfig(base_url="http://llm", model="m1")
    c.base, c._model, c.session, c.last_usage = "http://llm", "m1", Server(status), None
    return c


def test_every_call_is_recorded_with_what_it_was_for(calls):
    from superset.extensions import db, security_manager as sm

    from supagent.models import LLMCall

    L = calls
    alice = sm.find_user(username="alice").id
    with L.llm_task("answer", user_id=alice, message_id=42):
        _client(L).chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}] * 3)
    with L.llm_task("context", run_id=7):
        _client(L).chat([{"role": "user", "content": "page"}])
    _client(L).chat([{"role": "user", "content": "x"}])
    with L.llm_task("learn"), pytest.raises(L.LLMError):
        _client(L, status=500).chat([{"role": "user", "content": "y"}])
    rows = {r.task: r for r in db.session.query(LLMCall)}
    a = rows["answer"]
    assert (a.user_id, a.message_id, a.model, a.prompt_tokens, a.completion_tokens, a.cached_tokens, a.tools, a.ok) == (
        alice, 42, "m1", 1200, 30, 1000, 3, True)
    assert rows["context"].run_id == 7 and rows["other"].user_id is None
    assert rows["learn"].ok is False and "HTTP 500" in rows["learn"].error


def test_the_report_adds_them_up(calls, app):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.usage import purge, report
    from supagent.models import LLMCall

    alice = sm.find_user(username="alice").id
    now = dt.datetime.utcnow().replace(second=0, microsecond=0) - dt.timedelta(minutes=2)     # always in the past
    db.session.add_all([
        LLMCall(at=now, task="answer", user_id=alice, message_id=1, model="m1", prompt_tokens=5000,
                completion_tokens=100, cached_tokens=4000, seconds=2.0, tools=12, ok=True),
        LLMCall(at=now, task="answer", user_id=alice, message_id=1, model="m1", prompt_tokens=7000,
                completion_tokens=200, cached_tokens=5000, seconds=4.0, tools=12, ok=True),
        LLMCall(at=now - dt.timedelta(hours=3), task="learn", model="m1", prompt_tokens=3000, completion_tokens=900,
                cached_tokens=0, seconds=10.0, ok=False, error="timeout"),
        LLMCall(at=now - dt.timedelta(days=200), task="learn", model="m1", prompt_tokens=1, completion_tokens=1)])
    db.session.commit()
    out = report(now - dt.timedelta(days=1), now + dt.timedelta(hours=1), "hour")
    t = out["totals"]
    assert (t["calls"], t["tokens"], t["errors"], t["max_prompt"], t["people"]) == (3, 16200, 1, 7000, 1)
    assert t["cache_share"] == round(100 * 9000 / 15000, 1)
    users = {u["user"]: u for u in out["by_user"]}
    assert users["alice"]["answers"] == 1 and users["alice"]["tokens"] == 12300
    assert "(nightly and background work)" in users
    tasks = {x["task"]: x for x in out["by_task"]}
    assert tasks["answer"]["avg_prompt"] == 6000 and tasks["learn"]["errors"] == 1
    assert out["series"]["columns"] == ["time", "task", "tokens", "calls"] and len(out["series"]["rows"]) == 2
    assert out["biggest_contexts"][0]["prompt_tokens"] == 7000
    assert purge(90) == 1                                                  # the 200-day-old one

    with app.app_context(), app.test_client() as c:
        login(c, "admin")
        page = c.get("/supagent/admin/usage")
        assert page.status_code == 200 and b"LLM usage" in page.data
        start, end = (now - dt.timedelta(days=1)).isoformat(), (now + dt.timedelta(minutes=1)).isoformat()
        d = c.get(f"/supagent/admin/api/usage?start={start}&end={end}&grain=hour").get_json()
        assert d["totals"]["calls"] == 3
    with app.app_context(), app.test_client() as c:
        login(c, "alice")                                                  # the AI Agent role: not admin
        assert c.get("/supagent/admin/api/usage").status_code in (401, 403)
        assert c.get("/supagent/admin/usage").status_code in (302, 401, 403)
