"""The cross-check (0.7, test): a classic answer's headline figures looked for in a second, independent computation
(the governed pipeline, no classic agent): agree, disagree (the answer says so with the other figures), undecided
(no figure, no second answer, or a question the governed side hands to the classic agent). Dates, years, times, codes
and ids are not figures; a figure rounded or in thousands is the same."""

from __future__ import annotations

import pytest


def test_the_figures_of_an_answer():
    from supagent.crosscheck import figures, headline

    text = ("On **23 September 2026**, **5,939** jobs failed out of 73,769 (TR017, run 2026-09-23 14:00, id 42).\n\n"
            "Per application: BILLING 1,807.")
    assert headline(text) == [5939.0]                                   # the bold figure, not the bold date
    assert 73769.0 in figures(text) and 2026.0 not in figures(text) and 17.0 not in figures(text)
    assert headline("We had 1,950 failed jobs in 2026, then 12 retries.") == [1950.0, 12.0]   # a count, not a year
    assert headline("Nothing to count.") == []
    assert figures("12,5 % en moyenne, 5 939 travaux, 1,234.5 EUR") == [12.5, 5939.0, 1234.5]


def test_agree_disagree_undecided():
    from supagent.crosscheck import close, verdict

    assert close(5939, 5939.0) and close(391035.18, 391.0) and close(8.4, 8.38) and not close(5939, 6100)
    assert verdict("**5,939** failed jobs", "5,939 jobs failed", []) == ("agree", [])
    assert verdict("**5,939** failed jobs", "the plan found", [5939.0, 12.0]) == ("agree", [])      # in its rows
    assert verdict("**5,939** failed jobs", "6,100 jobs failed", [6100.0]) == ("disagree", [5939.0])
    assert verdict("No figure here.", "6,100", [])[0] == "undecided"
    assert verdict("**5,939** failed jobs", "", [])[0] == "undecided"


def test_the_answer_says_when_a_second_computation_disagrees(monkeypatch):
    from supagent import crosscheck as X, settings

    real = settings.get
    on = {"agent.cross_check": True}
    monkeypatch.setattr(settings, "get", lambda k: on[k] if k in on else real(k))
    trace = [{"tool": "execute_sql", "called": "execute_sql", "status": "done", "result": "{\"rows\": [[5939]]}"}]
    monkeypatch.setattr(X, "second_opinion", lambda *a, **k: {"text": "**6,100** jobs failed", "rows": [6100.0],
                                                              "way": "governed"})
    answer, found = X.check("admin", "How many jobs failed?", [], "**5,939** jobs failed.", trace)
    assert found["verdict"] == "disagree" and found["missing"] == [5939.0]
    assert answer.startswith("**5,939** jobs failed.") and "(Check: a second, independent way" in answer
    assert "did not find 5,939; it found 6,100" in answer
    monkeypatch.setattr(X, "second_opinion", lambda *a, **k: {"text": "5,939 failed", "rows": [], "way": "governed"})
    assert X.check("admin", "q", [], "**5,939** jobs failed.", trace) == ("**5,939** jobs failed.",
                                                                          {"verdict": "agree", "missing": [],
                                                                           "headline": [5939.0], "other": "5,939 failed"})
    monkeypatch.setattr(X, "second_opinion", lambda *a, **k: {"text": "", "rows": [], "way": "classic: charts"})
    assert X.check("admin", "q", [], "**5,939** jobs failed.", trace)[1]["verdict"] == "undecided"
    assert X.check("admin", "q", [], "**5,939** jobs failed.", [])[1] is None          # no query: no second opinion
    on["agent.cross_check"] = False
    assert X.check("admin", "q", [], "**5,939** jobs failed.", trace) == ("**5,939** jobs failed.", None)


def test_a_second_opinion_never_runs_the_classic_agent(app, monkeypatch):
    from supagent import agent as classic
    from supagent.governed.pipeline import GovernedAgent

    called = []
    monkeypatch.setattr(classic.Agent, "ask", lambda self, q, h=None: called.append(q) or ("classic answer", []))
    with app.app_context():
        from supagent.security import acting_as

        class LLM:
            def chat(self, *a, **k):
                raise AssertionError("no LLM call expected")
        with acting_as("admin"):
            g = GovernedAgent("admin", llm=LLM())
            g.second_opinion = True
            out = g._n_classic({"question": "Create a chart", "history": [], "trace": [], "why": "charts"})
            g.close()
    assert out == {"answer": ""} and called == [] and g.way == "classic: charts"
