"""The prompt: the instructions are the same for every user and every simple question (the LLM
server keeps them and the tools in its prompt cache), what is found for the question goes with
it; every tool the instructions name is offered with them; the words people use (English and
French) reach the right sections and tools."""

from __future__ import annotations

import re
import types

import pytest

from test_resolve import live  # noqa: F401  (the fixture)
from test_knowledge import world  # noqa: F401  (the fixture)

SUPERSET_TOOLS = {"generate_chart", "update_chart", "update_chart_preview", "get_chart_type_schema", "list_charts",
                  "get_chart_info", "generate_dashboard", "add_chart_to_existing_dashboard", "list_dashboards",
                  "get_dashboard_info", "list_datasets", "get_dataset_info"}


def _agent(rich: bool = True):
    from supagent import tools, tools_superset  # noqa: F401  (registers the tools)
    from supagent.agent import SHOW_CHART_SPEC, Agent

    a = object.__new__(Agent)
    a.rich, a.wants_saved_chart = rich, False
    a.superset = types.SimpleNamespace(available=True, error=None)
    names = set(tools.mcp.tools) | SUPERSET_TOOLS
    a.specs = [{"type": "function", "function": {"name": n}} for n in sorted(names)]
    if not rich:
        a.specs.append(SHOW_CHART_SPEC)
    a.names = {s["function"]["name"] for s in a.specs}
    return a


def test_the_instructions_are_the_same_for_every_simple_question_and_user(live):
    from supagent.knowledge.memory import add
    from supagent.security import acting_as
    from superset.extensions import security_manager as sm

    add(sm.find_user(username="admin").id, "Always give durations in minutes", kind="rule")
    a = _agent()
    with acting_as("admin"):
        first, blocks = a._system("cpu usage of elasticsearch"), a._question_blocks("cpu usage of elasticsearch")
    with acting_as("alice"):
        second = a._system("how many jobs failed yesterday")
    assert first == second
    header = "Where the data is (found for this question"
    assert header not in first and "durations in minutes" not in first
    assert header in blocks and "durations in minutes" in blocks


def test_the_question_comes_last_after_what_was_found(ctx, monkeypatch):
    from supagent.agent import Agent

    a = _agent()
    seen = []

    class LLM:
        last_usage = None

        def chat(self, messages, tools=None, max_tokens=None):
            seen.append([dict(m) for m in messages])
            return {"role": "assistant", "content": "Done."}

    a.llm, a.max_steps, a.should_stop, a.on_step, a.local = LLM(), 3, None, None, {}
    from supagent.agent import ChartGuard

    a.guard = ChartGuard(a)
    monkeypatch.setattr(Agent, "_system", lambda self, q: "RULES")
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q: "Where the data is: metric x")
    a.ask("How many?", history=[{"role": "user", "content": "Before"}, {"role": "assistant", "content": "Earlier."}])
    roles = [m["role"] for m in seen[0]]
    assert roles == ["system", "user", "assistant", "user"] and seen[0][0]["content"] == "RULES"
    last = seen[0][-1]["content"]
    assert last.startswith("Where the data is: metric x\n\n(Now: ") and last.endswith("\nHow many?")


QUESTIONS = [
    "How many jobs failed yesterday?",
    "Save a line chart of the CPU per server in the Operations dashboard",
    "Why did the jobs fail between 02:00 and 06:00?",
    "Send me the failed jobs as an Excel file by e-mail every morning",
    "Show me a screenshot of the dashboard Tenants",
    "Is the memory of the grid unusual today?",
    "Pourquoi les jobs ont-ils échoué cette nuit ?",
    "Envoie-moi un fichier Excel des jobs en erreur",
]


@pytest.mark.parametrize("rich", [True, False])
@pytest.mark.parametrize("question", QUESTIONS)
def test_every_tool_the_instructions_name_is_offered(ctx, question, rich):
    a = _agent(rich)
    a.wants_saved_chart = False
    text = a._system(question)
    offered = {s["function"]["name"] for s in a._specs_for(question)}
    named = {n for n in a.names if re.search(rf"\b{re.escape(n)}\b", text)}
    assert named <= offered, sorted(named - offered)


@pytest.mark.parametrize("question, expected", [
    ("How many jobs failed yesterday?", set()),
    ("combien de jobs ont échoué hier par application", set()),
    ("chart of the cpu usage of the elasticsearch cluster during the last 12 hours", set()),
    ("why did the jobs fail last night", {"investigation"}),
    ("pourquoi le serveur était lent ce matin", {"investigation"}),
    ("create a chart named CPU per server in superset", {"charts"}),
    ("crée un graphique dans le tableau de bord Ops", {"charts"}),
    ("export the failed jobs to excel", {"files"}),
    ("envoie un rapport chaque lundi", {"files"}),
    ("show me a screenshot of the dashboard", {"charts", "images"}),
    ("is the latency higher than usual", {"usual"}),
    ("la mémoire est anormale ?", {"usual"}),
])
def test_the_words_people_use_reach_the_right_sections(question, expected):
    from supagent.agent import intents

    assert intents(question) == expected
