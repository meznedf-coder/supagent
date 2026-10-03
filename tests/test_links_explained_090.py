"""0.9: every interaction of the System map explained, twice: a short explanation (a few words, shown when the link
is clicked on the map: never drawn on it) and a long one (what to do when an investigation follows it), given to
the agent with the system around a question and by system_links; the LLM writes them for the interactions it
reads in a text and for the others (an admin's, an older one), never over what an admin wrote. And the order of the
night: the classification runs right after the Context is built, on what it wrote."""

from __future__ import annotations

import json

import pytest
from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, grid-a, ledger db)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401


class Explainer:
    """An LLM that explains every interaction it is given, numbered as given."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def chat(self, messages, tools=None, max_tokens=None):
        asked = messages[1]["content"]
        self.seen.append(asked)
        n = sum(1 for line in asked.splitlines() if line[:1].isdigit())
        found = [{"n": k, "short": f"short {k}", "long": f"what to do {k}"} for k in range(1, n + 1)]
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "e", "function": {"name": "explanations", "arguments": json.dumps({"explanations": found})}}]}


def test_the_interactions_without_explanations_are_explained_never_over_an_admin(system, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import interactions as I
    from supagent.knowledge import sysmap
    from supagent.models import ContextPage, Link

    ids = system
    db.session.add(ContextPage(section="functional", slug="billing", title="Billing", kind="summary", version=1,
                               content="Invoicing waits for Payments: an invoice needs the day's payments. Other text."))
    db.session.commit()
    # drawn by an admin before 0.9: its note is theirs (no author recorded then)
    old = db.session.query(Link).filter(Link.kind == "runs_on", Link.a_ref == f"facet:{ids['Payments']}").one()
    old.note, old.explained_by, old.reviewed_by = "its night runs", None, "carol"
    db.session.commit()
    # an admin wrote both explanations of one: kept as they are
    sysmap.save_interaction(ids["Payments"], ids["ledger db"], "calls", "one connection per run", "boss",
                            detail="Check the ledger db's connections.")
    llm = Explainer()
    out = I.explain(llm)
    links = {(x.a_ref, x.kind, x.b_ref): x for x in db.session.query(Link)}
    dep = links[(f"facet:{ids['Invoicing']}", "depends_on", f"facet:{ids['Payments']}")]
    calls = links[(f"facet:{ids['Payments']}", "calls", f"facet:{ids['ledger db']}")]
    runs = links[(f"facet:{ids['Invoicing']}", "runs_on", f"facet:{ids['grid-a']}")]
    assert out["explained"] == 3 and out["calls"] == 1, (out, llm.seen)     # three of four lacked something
    assert dep.note == "waits for the payments of the day" and dep.detail.startswith("what to do")   # its short kept
    assert dep.explained_by == "admin and the AI"
    assert runs.note.startswith("short") and runs.detail.startswith("what to do") and runs.explained_by == "llm"
    assert (calls.note, calls.detail, calls.explained_by) == ("one connection per run", "Check the ledger db's connections.", "boss")
    assert (old.note, old.explained_by) == ("its night runs", "carol and the AI") and old.detail.startswith("what to do")
    asked = llm.seen[0]
    assert "Invoicing (application: Writes the invoices.) depends on Payments (application)" in asked
    assert "The admin's note: waits for the payments of the day" in asked
    assert "The Context: Invoicing waits for Payments: an invoice needs the day's payments. (Billing)" in asked
    assert "calls ledger db" not in asked                                     # (both written by the admin)
    assert I.explain(Explainer()) == {"explained": 0, "calls": 0}             # nothing left to explain
    db.session.query(ContextPage).delete()
    db.session.commit()


def test_an_older_link_keeps_its_sentence_as_evidence(system, app):  # noqa: F811
    from superset.extensions import db

    from supagent.models import Link, _link_notes_as_evidence

    ids = system
    quote = '"Payments writes its results to the ledger db every night after the close." (Billing at night)'
    old = Link(a_ref=f"facet:{ids['Payments']}", b_ref=f"facet:{ids['ledger db']}", kind="sends_to", source="llm",
               status="approved", note=quote + " and more words to be long enough for a sentence")
    db.session.add(old)
    db.session.commit()
    _link_notes_as_evidence()
    db.session.commit()
    assert old.note is None and old.evidence.startswith('"Payments writes its results')
    mine = db.session.query(Link).filter(Link.kind == "depends_on").one()
    assert mine.note == "waits for the payments of the day" and mine.evidence is None      # an admin's: kept


def test_the_map_the_review_and_the_agent_get_the_explanations(system, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge.brief import build, links_of
    from supagent.models import Link

    ids = system
    with _client(app, "admin") as c:
        r = c.post("/supagent/dictionary/api/map", json={"interaction": {
            "a": ids["Invoicing"], "b": ids["ledger db"], "kind": "reads_from", "note": "reads the customers",
            "detail": "When Invoicing is slow at its start, check the ledger db's response time in that window."}})
        made = r.get_json()["interaction"]
        assert (made["note"], made["explained_by"]) == ("reads the customers", "admin")
        m = c.get("/supagent/dictionary/api/map").get_json()
        x = next(k for k in m["links"] if k["id"] == made["id"])
        assert x["detail"].startswith("When Invoicing is slow") and x["explained_by"] == "admin"
        # a proposed one: an admin corrects its explanations when approving it
        p = Link(a_ref=f"facet:{ids['Payments']}", b_ref=f"facet:{ids['Invoicing']}", kind="triggers", source="llm",
                 status="proposed", note="starts it", detail="Check it started.", evidence='"..." (doc)', explained_by="llm")
        db.session.add(p)
        db.session.commit()
        card = next(k for k in c.get("/supagent/admin/api/review").get_json()["links"] if k["id"] == p.id)
        assert (card["note"], card["detail"], card["evidence"]) == ("starts it", "Check it started.", '"..." (doc)')
        done = c.post(f"/supagent/admin/api/links/{p.id}", json={"status": "approved", "note": "releases it",
                                                                 "detail": "Check its release time."}).get_json()
        assert done == {"id": p.id, "status": "approved", "note": "releases it", "detail": "Check its release time."}
        assert db.session.get(Link, p.id).explained_by == "admin"
    lines = "\n".join(build("Why is Billing late today?")["lines"])
    assert "- They read from: ledger db (service: only Invoicing, reads the customers)." in lines
    assert ("Invoicing reads from ledger db: When Invoicing is slow at its start, check the ledger db's response time "
            "in that window.") in lines
    said = links_of(["Invoicing"])["parts"][0]
    assert any(t.startswith("Invoicing reads from ledger db: When Invoicing is slow") for t in said["what_to_do_when_following"])
    assert any(t.startswith("Payments triggers Invoicing: Check its release time.") for t in said["what_to_do_when_following"])


def test_the_classification_runs_right_after_the_context(world, app, monkeypatch):  # noqa: F811
    from superset.extensions import db

    from supagent import settings, tasks
    from supagent.knowledge import context, learner
    from supagent.models import Run

    asked: list = []
    monkeypatch.setattr(context, "build_context", lambda reason="manual", llm=True, force=False:
                        asked.append(("context", reason)) or {"run": 1, "status": "done"})
    monkeypatch.setattr(learner, "run_classification", lambda reason="manual", minutes=None, limit=None:
                        asked.append(("classify", reason)) or {"status": "done"})
    out = context.build_then_classify("manual")
    assert asked == [("context", "manual"), ("classify", "after the Context")] and out["classification"]["status"] == "done"
    asked.clear()
    monkeypatch.setattr(context, "build_context", lambda reason="manual", llm=True, force=False:
                        asked.append(("context", reason)) or {"run": None, "status": "skipped"})
    context.build_then_classify("manual")
    assert asked == [("context", "manual")]                                # another run was going on: nothing more
    asked.clear()
    settings.set_value("context.classify_after", False)
    monkeypatch.setattr(context, "build_context", lambda reason="manual", llm=True, force=False:
                        asked.append(("context", reason)) or {"run": 1, "status": "done"})
    context.build_then_classify("manual")
    assert asked == [("context", "manual")] and not context.classify_follows()
    settings.set_value("context.classify_after", None)
    # the page's button: the build, then the classification
    asked.clear()
    monkeypatch.setattr(tasks, "_in_thread", lambda fn: fn())
    monkeypatch.setattr(tasks, "workers_alive", lambda: False)
    tasks.dispatch_context("manual")
    assert asked == [("context", "manual"), ("classify", "after the Context")]
    # the nightly learning leaves the categories to the Context's classification while none is built today
    db.session.query(Run).filter(Run.kind == "context").delete()
    db.session.commit()
    assert context.classify_follows()
    db.session.add(Run(kind="context", reason="schedule", status="done"))
    db.session.commit()
    assert not context.classify_follows()                                   # built today: the learning does them
    db.session.query(Run).filter(Run.kind == "context").delete()
    db.session.commit()


def test_the_nightly_learning_leaves_the_categories_to_the_context(world, app, monkeypatch):  # noqa: F811
    from test_learning_runs import _fake_steps

    from supagent.knowledge import learner
    from supagent.llm import LLM

    from superset.extensions import db

    world["run"].status = "done"                                           # (the fixture's run)
    db.session.commit()
    calls: list = []
    _fake_steps(monkeypatch, calls)
    monkeypatch.setattr(learner, "_categories", lambda run_id, steps, seconds, limit: calls.append(("categories",)) or {})
    monkeypatch.setattr(LLM, "chat", lambda self, *a, **k: {"role": "assistant", "content": ""})
    night = learner.run_learning(reason="schedule")
    assert ("categories",) not in calls and night.get("categories") == "after tonight's Context build", night
    calls.clear()
    learner.run_learning(reason="manual")                                  # asked by an admin: as before
    assert ("categories",) in calls
