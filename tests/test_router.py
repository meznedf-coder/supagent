"""The router (MOA): the LLM's route used when it is confident enough, the normal way otherwise; confirmed
examples override the LLM only when several close ones agree or an admin set one; only the routes the execution
kept to are examples; the classic agent gets the route's tools and instruction; the governed pipeline sends the
routes only the classic agent's tools answer to it and records the route on the answer's row."""

from __future__ import annotations

import json

import pytest

from test_agent_loop import ScriptedLLM


def route_call(route, confidence="high", second="", scores=None):
    args = {"route": route, "confidence": confidence, "second": second, "why": "test"}
    if scores is not None:
        args = {"route": route, "scores": scores, "why": "test"}
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {
        "name": "route_question", "arguments": json.dumps(args)}}]}


@pytest.fixture()
def on(ctx, monkeypatch):
    from supagent import settings

    real = settings.get
    conf = {"agent.router": True, "router.min_confidence": "medium"}
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    yield conf


def test_the_llm_route_when_sure_else_the_normal_way(on):
    from supagent import router

    d = router.decide("Why was the report late yesterday?", llm=ScriptedLLM([route_call("incident")]), shown=[],
                      found=[])
    assert (d.route, d.by, d.active) == ("incident", "llm", True)
    d = router.decide("Tell me about it", llm=ScriptedLLM([route_call("functional", "low")]), shown=[], found=[])
    assert (d.route, d.by, d.active) == ("other", "fallback", False)
    on["router.min_confidence"] = "low"
    d = router.decide("Tell me about it", llm=ScriptedLLM([route_call("functional", "low")]), shown=[], found=[])
    assert d.route == "functional"
    garbled = {"role": "assistant", "content": "I think this is about servers"}
    assert router.decide("Tell me", llm=ScriptedLLM([garbled]), shown=[], found=[]).route == "other"
    on["agent.router"] = False
    assert router.decide("Why was the report late?", llm=ScriptedLLM([route_call("incident")])).by == "off"


def test_confirmed_examples_override_only_when_several_agree_or_an_admin_set_one(on):
    from supagent import router

    one = [{"question": "why did the batch fail", "route": "incident", "similarity": 0.93}]
    d = router.decide("why did the batch fail last night", llm=ScriptedLLM([route_call("technical")]), shown=one,
                      found=[])
    assert (d.route, d.by) == ("technical", "llm")                       # one example never flips the LLM
    two = one + [{"question": "what made the batch fail", "route": "incident", "similarity": 0.9}]
    d = router.decide("why did the batch fail last night", llm=ScriptedLLM([route_call("technical")]), shown=two,
                      found=[])
    assert (d.route, d.by) == ("incident", "examples")
    split = two + [{"question": "how does the batch work", "route": "technical", "similarity": 0.85}]
    d = router.decide("why did the batch fail last night", llm=ScriptedLLM([route_call("technical")]), shown=split,
                      found=[])
    assert d.by == "llm"                                                  # the close examples disagree
    admin = [{"question": "batch failures", "route": "observability", "similarity": 0.95, "admin": True}]
    d = router.decide("batch failures?", llm=ScriptedLLM([route_call("incident")]), shown=admin, found=[])
    assert (d.route, d.by) == ("observability", "admin example")
    far = [{"question": "x", "route": "incident", "similarity": 0.5}] * 3
    assert router.decide("q", llm=ScriptedLLM([route_call("charts")]), shown=far, found=[]).route == "charts"
    messages = ScriptedLLM([route_call("incident")])
    similar = [{"question": "what made the payroll run late", "route": "incident", "similarity": 0.7}]
    router.decide("why did the batch fail", llm=messages, shown=two + similar,
                  found=[{"kind": "guide", "title": "Batch chain", "facets": "aspect: technical"}])
    prompt = messages.seen[0][-1]["content"]
    assert "incident: what made the payroll run late" in prompt and "guide: Batch chain [aspect: technical]" in prompt
    assert "why did the batch fail\n" not in prompt.split("Question:")[0]       # the same question only votes


def test_an_admin_decides_alone_only_by_setting_the_route_itself(on):
    """An admin confirming an answer judged the answer, not its route: that example votes like the others; a
    route an admin kept or corrected (To review) decides alone for the same question."""
    from superset.extensions import db

    from supagent import router
    from supagent.models import Route

    from supagent.knowledge.resolve import terms

    db.session.query(Route).delete()
    q = "why did the payroll batch fail last night"
    r = Route(question=q, terms=" ".join(terms(q)), shown=[], chosen=[], used=[], moa="charts", moa_by="llm",
              moa_followed=True, signal="confirmed")               # an admin confirmed the answer
    db.session.add(r)
    db.session.commit()
    try:
        shown = router._examples_by_words(q, 6)
        assert shown and not shown[0]["admin"]
        d = router.decide(q, llm=ScriptedLLM([route_call("incident")]), shown=shown, found=[])
        assert (d.route, d.by) == ("incident", "llm")
        r.moa_by = "admin"                                         # an admin set the route itself
        db.session.commit()
        shown = router._examples_by_words(q, 6)
        assert shown[0]["admin"]
        d = router.decide(q, llm=ScriptedLLM([route_call("incident")]), shown=shown, found=[])
        assert (d.route, d.by) == ("charts", "admin example")
    finally:
        db.session.query(Route).delete()
        db.session.commit()


def test_one_wrong_example_of_the_same_question_never_decides(on):
    """The LLM never sees a close example (it would follow it); alone, it does not vote either."""
    from supagent import router

    wrong = [{"question": "why was the report late yesterday", "route": "charts", "similarity": 0.97}]
    llm = ScriptedLLM([route_call("incident")])
    d = router.decide("why was the report late yesterday", llm=llm, shown=wrong, found=[])
    assert (d.route, d.by) == ("incident", "llm")
    assert "charts: why was the report late" not in llm.seen[0][-1]["content"]


def test_the_confidence_is_how_far_the_best_score_is_ahead(on):
    from supagent import router

    def decide(route, scores):
        return router.decide("q", llm=ScriptedLLM([route_call(route, scores=scores)]), shown=[], found=[])

    d = decide("incident", {"incident": 90, "observability": 40, "functional": 10})
    assert (d.route, d.confidence, d.second, d.by) == ("incident", "high", "", "llm")
    assert d.scores["incident"] == 90
    d = decide("incident", {"incident": 80, "infrastructure": 60})
    assert (d.route, d.confidence, d.second) == ("incident", "medium", "infrastructure")
    assert decide("incident", {"incident": 70, "infrastructure": 65}).route == "other"      # too close: normal way
    assert decide("charts", {"charts": 40}).route == "other"                                 # nothing fits well
    assert decide("charts", {"functional": 90, "charts": 50}).route == "other"               # it hesitates
    assert decide("functional", {"functional": "95", "technical": "x"}).route == "functional"   # tolerant


def test_only_confirmed_routes_the_execution_kept_to_teach(on):
    from superset.extensions import db

    from supagent import router
    from supagent.governed.gate import confirm
    from supagent.models import Route

    db.session.query(Route).delete()
    db.session.commit()
    kept = router.record(None, "why did the payroll batch fail on monday",
                         router.Decision(route="incident", llm="incident", confidence="high", by="llm"))
    dropped = router.record(None, "why did the payroll batch fail on tuesday",
                            router.Decision(route="incident", llm="incident", confidence="high", by="llm"))
    router.left(dropped)                                                   # the execution left the route
    for rid, mid in ((kept, 9001), (dropped, 9002)):
        r = db.session.get(Route, rid)
        r.message_id = mid
    db.session.commit()
    confirm(9001, "helpful")
    confirm(9002, "helpful")
    found = router._examples_by_words("why did the payroll batch fail", 6)
    assert [e["route"] for e in found] == ["incident"] and "monday" in found[0]["question"]
    confirm(9001, "not_helpful")
    assert router._examples_by_words("why did the payroll batch fail", 6) == []
    db.session.query(Route).delete()
    db.session.commit()


def test_the_classic_agent_gets_the_routes_tools_and_instruction(on, monkeypatch):
    from supagent.agent import TOOLS_OF, Agent
    from supagent.router import ROUTE_NOTES

    a = object.__new__(Agent)
    a.username, a.rich = "admin", True
    a.llm = ScriptedLLM([route_call("charts")])
    names = sorted(TOOLS_OF["charts"] | {"execute_sql"})
    a.specs = [{"type": "function", "function": {"name": n}} for n in names]
    monkeypatch.setattr(Agent, "_system", lambda self, q, shown=None: "SYSTEM")
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q, shown=None: "")
    messages = a.prompt("Show me what the jobs look like", [])
    assert a.moa.route == "charts" and ROUTE_NOTES["charts"] in messages[-1]["content"]
    offered = {s["function"]["name"] for s in a._specs_for("Show me what the jobs look like")}
    assert "generate_chart" in offered                  # a chart tool, though the words name no chart
    assert a.prompt("Show me what the jobs look like", []) and len(a.llm.seen) == 1     # routed once
    from superset.extensions import db

    from supagent.models import Route

    a.after_saved(4242)
    r = db.session.get(Route, a.route_id)
    assert (r.moa, r.moa_followed, r.message_id) == ("charts", True, 4242)
    db.session.delete(r)
    db.session.commit()


def test_the_governed_pipeline_sends_chart_and_incident_questions_to_the_classic_agent(on, monkeypatch):
    from test_governed_pipeline import governed

    from supagent.agent import Agent

    said = {"role": "assistant", "content": "The batch failed on srv-a-1 at 03:05."}
    a, _ran = governed(monkeypatch, [route_call("incident"), said, said, said])       # the classic checks may ask again
    monkeypatch.setattr(Agent, "route", Agent.route)
    answer, trace = a.ask("Why was the report late on 23 September?", [])
    assert [s["tool"] for s in trace][:1] == ["route"] and "srv-a-1" in answer
    assert a.way.startswith("classic: the router: incident")
