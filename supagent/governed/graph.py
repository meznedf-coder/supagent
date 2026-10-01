"""The governed pipeline as a graph: its nodes are the governed agent's steps, its routes the decisions between
them. LangGraph runs it when it is installed (pip install "supagent[graph]"): the graph is where a new kind of
source (another MCP server) becomes one more route. Without it, the same nodes and routes run in order (run()).

  decide --(action, status, other; decider failed)--> classic
  decide --(the knowledge answers it)--> explain
  decide --> plan --(no valid plan)--> classic
             plan --(clarify | cannot | explain)--> that answer
             plan --> run --(a step failed, first time)--> replan --> run --(failed again)--> classic
                      run --> answer
"""

from __future__ import annotations

from typing import Any, TypedDict

TERMINAL = ("answer", "clarify", "cannot", "explain", "classic")
NODES = ("decide", "plan", "run", "replan") + TERMINAL


class State(TypedDict, total=False):
    question: str
    history: list
    prev_q: str
    prev_a: str
    user_id: Any
    trace: list
    decision: Any
    plan: Any
    checked: Any
    results: dict
    failed: list
    replanned: bool
    why: str
    say: bool
    answer: str


def after_decide(st: dict[str, Any]) -> str:
    if st.get("why") or st.get("decision") is None:
        return "classic"
    return "explain" if st.get("plan") is not None and st["plan"].kind == "explain" else "plan"


def after_plan(st: dict[str, Any]) -> str:
    plan = st.get("plan")
    if plan is None:
        return "classic"
    return plan.kind if plan.kind in ("clarify", "cannot", "explain") else "run"


def after_run(st: dict[str, Any]) -> str:
    if not st.get("failed"):
        return "answer"
    return "classic" if st.get("replanned") else "replan"


def after_replan(st: dict[str, Any]) -> str:
    return "classic" if st.get("plan") is None else "run"


ROUTES = {"decide": after_decide, "plan": after_plan, "run": after_run, "replan": after_replan}


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except Exception:  # pylint: disable=broad-except
        return False
    return True


def build(agent: Any) -> Any:
    """The compiled LangGraph of this agent's nodes."""
    from langgraph.graph import END, StateGraph

    g = StateGraph(State)
    for name in NODES:
        g.add_node(name, getattr(agent, f"_n_{name}"))
    g.set_entry_point("decide")
    for name, route in ROUTES.items():
        g.add_conditional_edges(name, route, {n: n for n in NODES})
    for name in TERMINAL:
        g.add_edge(name, END)
    return g.compile()


def run(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    """The pipeline for one question: with LangGraph when it is installed, else the same nodes in order."""
    if langgraph_available():
        out = build(agent).invoke(state, {"recursion_limit": 25})
        return {**state, **out}
    node = "decide"
    for _ in range(25):
        state.update(getattr(agent, f"_n_{node}")(state) or {})
        if node in TERMINAL:
            return state
        node = ROUTES[node](state)
    state["answer"] = state.get("answer") or ""
    return state
