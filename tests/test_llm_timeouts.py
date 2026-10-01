"""An LLM that does not answer in time is not asked the same question again (the lab's governed plan waited 2 x 900
s: half an hour for one answer); a quick step (the router, the plan, the decider's choice) has its own, shorter
bound, after which the normal way answers."""

from __future__ import annotations

import pytest
import requests


def test_a_read_timeout_is_not_retried_and_a_quick_step_is_bounded(ctx, monkeypatch):
    from supagent import settings
    from supagent.llm import LLM, LLMError, bounded

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: {"llm.base_url": "http://llm.invalid/v1", "llm.model": "m",
                                                      "llm.timeout": 900, "llm.auth": "none"}.get(key, real(key)))
    llm = LLM()
    calls = []

    def stuck(method, url, timeout=None, **kw):
        calls.append(timeout)
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(llm.session, "request", stuck)
    with pytest.raises(LLMError, match="no answer within"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert calls == [900]                                      # once: not 900 s twice
    calls.clear()
    with bounded(llm, 60), pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert calls == [60.0] and llm.cfg.timeout == 900           # the bound, then the setting again


def test_a_bound_on_a_test_double_changes_nothing():
    from supagent.llm import bounded

    class Double:
        def chat(self, messages, tools=None, max_tokens=None):
            return {"content": "ok"}

    d = Double()
    with bounded(d, 5) as same:
        assert same is d and same.chat([]) == {"content": "ok"}
