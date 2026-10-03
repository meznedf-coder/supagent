"""0.9: what an investigation blames is tied to the question's parts (knowledge.leads): by the system map (two
steps), or by a result of the answer's own queries on those parts; else it is sent back once, then marked."""

from __future__ import annotations

import json

import pytest
from test_agent_loop import agent_with, call, say
from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, on grid-a = srv-1, srv-2)
from test_knowledge import world  # noqa: F401


@pytest.fixture()
def elsewhere(system):  # noqa: F811
    """Another pool with its servers, which nothing links to Billing."""
    from superset.extensions import db

    from supagent.knowledge.freshness import touch
    from supagent.models import Facet

    pool = Facet(facet="pool", value="grid-b", status="approved", source="admin")
    db.session.add(pool)
    db.session.flush()
    for n in ("srv-8", "srv-9", "srv-10"):
        db.session.add(Facet(facet="server", value=n, status="approved", source="admin", parents=[pool.id]))
    touch()
    db.session.commit()
    return system


def test_the_lines_that_say_what_the_cause_is():
    from supagent.knowledge.leads import _expanded, blaming_lines

    answer = """The batch is late because srv-9 ran out of memory. The release of the evening is unrelated to it.

**Root cause**
- memory pressure on srv-8
- a full disk

## What was checked
- srv-1: normal
The delay is not due to srv-2."""
    lines = blaming_lines(answer)
    assert lines == ["The batch is late because srv-9 ran out of memory.", "- memory pressure on srv-8", "- a full disk"]
    assert _expanded("servers srv-901-904 and srv101 to srv103") == \
        "servers srv-901, srv-902, srv-903, srv-904 and srv101, srv102, srv103"
    assert _expanded("from 10 to 2000 rows") == "from 10 to 2000 rows"                 # no list of names


def test_a_part_the_system_map_links_is_tied_and_another_is_not(elsewhere, app):
    from supagent.knowledge.leads import untied

    q = "Why are the Billing applications slow today?"
    assert untied(q, "It is caused by memory pressure on srv-1, a server of grid-a.", []) == ([], "")
    assert untied(q, "It is caused by memory pressure on srv-9.", []) == (["srv-9"], "Billing")
    assert untied(q, "Caused by: srv-8-10 out of memory.", [])[0] == ["srv-8", "srv-9", "srv-10"]
    assert untied(q, "srv-9 had an alert, unrelated to these runs. Nothing else was found.", []) == ([], "")
    assert untied("Why is it slow today?", "It is caused by srv-9.", []) == ([], "")    # the question names no part
    # a bare word of the map (a category of the catalog kept as a subject: no description, part of nothing, no
    # interaction) is no part of the system: neither a part the question names nor a part an answer blames
    from superset.extensions import db

    from supagent.knowledge.freshness import touch
    from supagent.models import Facet

    for word in ("memory", "Batch"):
        db.session.add(Facet(facet="subject", value=word, status="approved", source="seed"))
    touch()
    db.session.commit()
    assert untied(q, "It is caused by memory pressure on srv-1, a server of grid-a.", []) == ([], "")
    assert untied("Why is the Batch slow today?", "It is caused by srv-9.", []) == ([], "")   # the question names no part
    assert untied("Why is the Billing Batch slow?", "It is caused by memory pressure on srv-9.", []) == (["srv-9"], "Billing")
    # the data says more than the map: a query on the question's parts shows that server
    shown = [{"tool": "execute_sql", "status": "done", "args": {"request": {"sql": "SELECT NODE FROM jobs WHERE APP IN "
              "('Invoicing', 'Payments')"}}, "result": json.dumps({"rows": [{"NODE": "srv-9", "n": 12}]})}]
    assert untied(q, "It is caused by memory pressure on srv-9.", shown) == ([], "")
    other = [{"tool": "execute_sql", "status": "done", "args": {"request": {"sql": "SELECT NODE FROM alerts"}},
              "result": json.dumps({"rows": [{"NODE": "srv-9"}]})}]
    assert untied(q, "It is caused by memory pressure on srv-9.", other)[0] == ["srv-9"]   # not a query on those parts


def test_the_agent_sends_an_untied_cause_back_once_then_marks_it(elsewhere, ctx, monkeypatch):
    from supagent.agent import TIE_NUDGE

    sql = {"request": {"database_id": 1, "sql": "SELECT NODE, POOL FROM alerts"}}     # not a query on Billing's parts
    rows = json.dumps({"success": True, "columns": [{"name": "NODE"}, {"name": "POOL"}], "row_count": 2,
                       "rows": [{"NODE": "srv-9", "POOL": "grid-b"}, {"NODE": "srv-1", "POOL": "grid-a"}]})

    def results(name, args):
        return rows

    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), say("The slowdown is caused by srv-9 (memory)."),
                                       say("The slowdown is caused by srv-9 (memory).")], results)
    answer, _trace = a.ask("Why are the Billing applications slow today?")
    asked = [m["content"] for m in a.llm.seen[-1] if m["role"] == "user"]
    assert asked[-1] == TIE_NUDGE.format(names="srv-9", scope="Billing", it="it")
    assert answer.endswith("(Check: srv-9 named as a cause, but neither the system map nor this answer's results tie it "
                           "to Billing.)")
    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), say("The slowdown is caused by srv-9 (memory)."),
                                       say("srv-1 of grid-a is short of memory: that is the cause.")], results)
    answer, _trace = a.ask("Why are the Billing applications slow today?")
    assert answer == "srv-1 of grid-a is short of memory: that is the cause."         # corrected: nothing marked
    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), say("srv-9 is slow because of its memory.")], results)
    answer, _trace = a.ask("Which Billing applications ran on srv-9 today?")          # no investigation: not checked
    assert "Check:" not in answer
