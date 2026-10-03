"""0.9: what the agent knows of the system and what is missing for an investigation (knowledge.readiness): the
categories, the interactions, the catalog's joins and usual values, the health checks, the paths; in words what
is not there yet; for a question, what it is given and the names it writes that nothing knows. No LLM."""

from __future__ import annotations

import pytest
from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, grid-a, ledger db)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401


@pytest.fixture()
def gaps(system):  # noqa: F811
    """What an admin has not finished: an application nobody tied to anything, an interaction waiting, a field
    that looks like a usual value, a path waiting."""
    from superset.extensions import db

    from supagent.knowledge import catalog
    from supagent.knowledge.freshness import touch
    from supagent.models import Facet, Link, Recipe

    db.session.add(Facet(facet="application", value="Refunds", status="approved", source="admin"))
    db.session.add(Link(a_ref=f"facet:{system['Payments']}", b_ref=f"facet:{system['ledger db']}", kind="sends_to",
                        confidence=0.7, source="llm", status="proposed", note="Payments writes to the ledger db. (doc)"))
    db.session.add(Recipe(question="A batch is late", tool="investigation", query="Problem: ...", signature="p1",
                          status="helpful", generic=True))
    catalog.save_entry({"title": "Index steps", "classification": "index", "content": (
        "steps:\n  fields:\n    AVG_WAIT_S: {description: the usual wait of the step}\n")}, "admin")
    touch()
    db.session.commit()
    yield system
    db.session.query(Recipe).delete()
    db.session.commit()


def test_what_is_known_and_what_is_missing(gaps, app):
    from supagent.knowledge.readiness import report, text
    from supagent.security import acting_as

    with acting_as("admin"):
        out = report()
    cats = {c["category"]: c for c in out["categories"]}
    assert (cats["application"]["values"], cats["application"]["with_interactions"]) == (3, 2)
    assert cats["server"]["part_of_or_parts"] == 2 and any('field "NODE"' in p for p in cats["server"]["in_the_data"])
    assert out["interactions"] == {"approved": {"depends_on": 1, "runs_on": 2, "calls": 1}, "proposed": 1}
    tables = {t["table"]: t for t in out["tables"]}
    assert tables["jobs"]["time_field"] == "ts" and tables["jobs"]["usual_values"] == {"USUAL_S": "DURATION_S"}
    assert tables["jobs"]["joins"] == 1 and tables["steps"]["usual_values_not_said"] == ["AVG_WAIT_S"]
    assert out["paths"] == {"confirmed": 0, "waiting": 1}
    todo = "\n".join(out["to_do"])
    assert 'category "application": 1 of 3 value(s) have no interaction and are part of nothing (Refunds)' in todo
    assert 'category "pool": its values interact on the System map, and none is found in a field' in todo
    assert 'category "server"' not in todo                 # read from the fields NODE / node; no interaction asked of it
    assert 'category "subject"' not in todo                # topics: nothing to tie
    assert "1 interaction(s) proposed from the documents wait" in todo
    assert 'table steps: "AVG_WAIT_S" look(s) like the usual value of a measure' in todo
    assert "no health check in the catalog" in todo and "1 investigation path(s) wait" in todo
    said = text(out)
    assert "Interactions (System map): calls 1, depends_on 1, runs_on 2; 1 proposed, waiting" in said
    assert "  jobs: time field ts, 1 join(s); USUAL_S = usual DURATION_S" in said and "To do:\n- " in said


def test_what_a_question_is_given_and_the_names_nothing_knows(gaps, app):
    from supagent.knowledge.readiness import report, text
    from supagent.security import acting_as

    with acting_as("admin"):
        out = report("Why are the jobs of Billing slow, and what about GHOST_APP on NODE srv-1?")
        nothing = report("Why is everything slow?")
    q = out["question"]
    assert q["names"] == ["Billing (subject)", "srv-1 (server)"] and q["says_nothing"] == ["jobs (subject)"]
    assert q["not_known"] == ["GHOST_APP"]                 # NODE is a field the agent learned
    assert q["given"].startswith("The system around this question") and "They run on: grid-a (pool: only Invoicing, Payments)." in q["given"]
    assert out["to_do"][0].startswith("the question writes GHOST_APP as names")
    assert "the question names jobs (subject), of which nothing is said" in out["to_do"][1]
    assert "The question names: Billing (subject), srv-1 (server)" in text(out)
    assert nothing["question"]["names"] == [] and nothing["to_do"][0].startswith("the question names no part")
    assert "(no picture of the system is given for it)" in text(nothing)


def test_the_map_page_lists_what_is_missing_to_admins_only(gaps, app):
    with _client(app, "admin") as c:
        missing = c.get("/supagent/dictionary/api/map").get_json()["missing"]
    assert any("Refunds" in x for x in missing) and not any("wait in Data dictionary" in x for x in missing)
    with _client(app, "alice") as c:
        assert c.get("/supagent/dictionary/api/map").get_json()["missing"] == []


def test_nothing_drawn_yet(world, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge.freshness import touch
    from supagent.knowledge.readiness import report
    from supagent.models import Facet, Link
    from supagent.security import acting_as

    db.session.query(Link).delete()
    db.session.query(Facet).delete()
    touch()
    db.session.commit()
    with acting_as("admin"):
        out = report("why is it slow?")
    assert out["interactions"] == {"approved": {}, "proposed": 0}
    assert any(x.startswith("no interaction is drawn between the parts") for x in out["to_do"])
    assert out["question"]["given"] == ""
