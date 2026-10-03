"""0.9: the system around a question (knowledge.brief): the parts a question names, what they consist of, depend
on, run on and call, one step further, where each kind of part is in the data, the joins and usual values of the
catalog; and the tool system_links. Read from the categories and the interactions of the system map only."""

from __future__ import annotations

import pytest
from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.fixture()
def system(world, app):  # noqa: F811
    """Billing = two applications; they run on a pool of two servers; one waits for the other, which calls a
    service. Servers are read from the fields named NODE / node."""
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import catalog, sysmap
    from supagent.models import Entry, Facet, Link, Tag

    db.session.query(Link).delete()
    db.session.query(Tag).delete()
    db.session.query(Facet).delete()
    db.session.query(Entry).delete()
    db.session.commit()
    settings.set_value("categories.custom", ["server", "pool", "service"])
    settings.set_value("categories.fields", {"server": "^(NODE|node)$"})
    f = {}
    for cat, name, about in (("subject", "Billing", "Invoices the customers. Second sentence."),
                             ("subject", "jobs", ""), ("application", "Invoicing", "Writes the invoices."),
                             ("application", "Payments", ""), ("pool", "grid-a", "The compute pool."),
                             ("server", "srv-1", ""), ("server", "srv-2", ""), ("service", "ledger db", "The ledger.")):
        f[name] = Facet(facet=cat, value=name, status="approved", source="admin", description=about or None)
        db.session.add(f[name])
    db.session.flush()
    f["Invoicing"].parents = f["Payments"].parents = [f["Billing"].id]
    f["srv-1"].parents = f["srv-2"].parents = [f["grid-a"].id]
    f["ledger db"].synonyms = ["LEDGERDB"]
    db.session.commit()
    ids = {k: v.id for k, v in f.items()}
    sysmap.save_interaction(ids["Invoicing"], ids["Payments"], "depends_on", "waits for the payments of the day", "admin")
    sysmap.save_interaction(ids["Invoicing"], ids["grid-a"], "runs_on", "", "admin")
    sysmap.save_interaction(ids["Payments"], ids["grid-a"], "runs_on", "", "admin")
    sysmap.save_interaction(ids["Payments"], ids["ledger db"], "calls", "one connection per run", "admin")
    catalog.save_entry({"title": "Index jobs", "classification": "index", "content": (
        "jobs:\n  time_field: ts\n  relationships:\n    - to: steps\n      keys: {ID: ID}\n      description: its steps\n"
        "  fields:\n    USUAL_S: {description: usual duration, usual_of: DURATION_S}\n")}, "admin")
    from supagent.knowledge.freshness import touch

    touch()
    db.session.commit()
    yield ids
    db.session.query(Link).delete()
    db.session.query(Facet).delete()
    db.session.query(Entry).delete()
    db.session.commit()
    settings.set_value("categories.custom", None)
    settings.set_value("categories.fields", None)


def test_the_values_a_question_names(system):
    from supagent.knowledge.brief import _graph, named

    g = _graph()
    name = lambda ids: [g["values"][i]["name"] for i in ids]   # noqa: E731
    assert name(named("Why are the Billing applications late today?")) == ["Billing"]
    assert name(named("is ledger db slow, and grid-a?")) == ["ledger db", "grid-a"]       # two words, a hyphen
    assert name(named("the LEDGERDB again")) == ["ledger db"]                             # another name of it
    assert name(named("payments on srv-2.")) == ["Payments", "srv-2"]
    assert named("nothing of the system here") == []


def test_the_picture_of_an_investigation(system, app):
    from supagent.knowledge.brief import brief_block, build
    from supagent.security import acting_as

    with acting_as("admin"):                               # the data places: of the databases the user may query
        b = build("why are the jobs of Billing slow?")
    assert b["seeds"] == ["Billing"]                       # "jobs" is a value that says nothing: a word, not a part
    text = "\n".join(b["lines"])
    assert "- Billing (subject): Invoices the customers. Its parts: applications Invoicing, Payments." in text
    assert "- They depend on: Payments (application: only Invoicing, waits for the payments of the day)." in text
    assert "- They run on: grid-a (pool)." in text
    assert "- They call: ledger db (service: only Payments, one connection per run)." in text
    assert "- One step further: grid-a has 2 server(s): srv-1, srv-2." in text
    assert 'servers: field "NODE" of jobs' in text and "label node of 1 metric(s) such as node_cpu_seconds_total" in text
    assert 'jobs -> steps on "ID" = "ID" (its steps)' in text
    assert ('"USUAL_S" of jobs is the usual value of "DURATION_S" in the same document (compare_groups measure: '
            'ratio(DURATION_S, USUAL_S))') in text
    block = brief_block("why are the jobs of Billing slow?")
    assert block.startswith("\n\nThe system around this question") and "grid-a" in block
    assert brief_block("how many rows are there?") == ""
    with acting_as("alice"):                               # alice may not query the metrics database
        text = "\n".join(build("why are the jobs of Billing slow?")["lines"])
    assert 'servers: field "NODE" of jobs' in text and "node_cpu_seconds_total" not in text


def test_a_plain_question_gets_what_the_named_value_consists_of_only(system):
    from supagent.knowledge.brief import brief_block

    block = brief_block("how many jobs of Billing failed yesterday?", full=False)
    assert block.startswith("\n\nWhat the question names") and "applications Invoicing, Payments" in block
    assert "run on" not in block and "In the data" not in block
    assert brief_block("how many jobs of Payments failed?", full=False) == ""            # nothing it consists of


def test_one_part_alone_and_the_way_back(system):
    from supagent.knowledge.brief import build

    text = "\n".join(build("what is wrong with grid-a")["lines"])
    assert "- grid-a (pool): The compute pool. Its parts: servers srv-1, srv-2." in text
    text = "\n".join(build("Payments is failing")["lines"])
    assert "- Payments calls: ledger db (service: one connection per run)." in text
    assert "- Payments runs on: grid-a (pool)." in text
    assert "- Needed by: Invoicing (application: waits for the payments of the day)." in text


def test_the_tool_system_links(system, app):
    from supagent.security import acting_as
    from supagent.tools import system_links

    with acting_as("admin"):
        r = system_links(["grid-a", "ledger db", "no such thing"])
    pool = r["parts"][0]
    assert pool["name"] == "grid-a" and pool["parts"] == ["srv-1 (server)", "srv-2 (server)"]
    assert pool["others"] == {"what runs on them": ["Invoicing (application)", "Payments (application)"]}
    assert r["parts"][1]["others"] == {"called by": ["Payments (application): one connection per run"]}
    assert r["not_in_the_map"] == ["no such thing"]
    with acting_as("admin"):
        assert "none of these names" in system_links(["nope"])["note"]


def test_the_prompt_gets_the_picture_the_steps_and_the_tools_of_an_investigation(system, app):
    """The question's blocks, the instructions and the tools offered: an investigation gets the whole picture, the
    five steps and its tools; a plain question about a family only what the family consists of."""
    from test_prompt import _agent

    from supagent.security import acting_as

    a = _agent()
    why = "Why are the Billing applications so late today?"
    with acting_as("admin"):
        blocks, instructions = a._question_blocks(why), a._system(why)
        plain = a._question_blocks("How many runs of the Billing applications failed yesterday?")
        plain_instructions = a._system("How many runs of the Billing applications failed yesterday?")
    assert "The system around this question" in blocks and "They run on: grid-a (pool)." in blocks
    assert "Investigations (" in instructions and "4) Check a cause before naming it" in instructions
    offered = {s["function"]["name"] for s in a._specs_for(why)}
    assert {"compare_groups", "system_links", "compare_to_usual", "check_health"} <= offered
    assert "What the question names" in plain and "applications Invoicing, Payments" in plain
    assert "run on" not in plain and "Investigations (" not in plain_instructions
    assert not {"compare_groups", "system_links"} & {s["function"]["name"] for s in a._specs_for("How many runs failed?")}
    assert a._investigating(why) and not a._investigating("How many runs failed yesterday?")


def test_a_question_routed_to_the_servers_or_the_usual_gets_only_what_it_names(system, app):
    """0.9 (the e2e regression): an infrastructure, observability or "is it usual" question that is no
    investigation gets what it names, not the whole picture (a follow-up on a server's load needs no map of the
    platform: given the whole picture, the model answered from it without querying); a question of how the
    system works (the technical route) and an investigation get the whole picture."""
    import types

    from test_prompt import _agent

    from supagent.security import acting_as

    a = _agent()
    q = "What was the highest load of the Billing applications' servers yesterday?"
    with acting_as("admin"):
        for route, whole in (("infrastructure", False), ("observability", False), ("technical", True), ("incident", True)):
            a.moa = types.SimpleNamespace(route=route, active=True)
            blocks = a._question_blocks(q)
            assert ("The system around this question" in blocks) is whole, route
            assert ("What the question names" in blocks) is not whole, route
        a.moa = None
