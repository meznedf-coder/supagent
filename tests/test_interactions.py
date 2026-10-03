"""0.9: the interactions the team's texts state are proposed for the System map (knowledge.interactions): only
between known parts the text names, only with a sentence that is in the text word for word, never drawn nor used
before an admin approves them, never proposed again once rejected; a text is read once."""

from __future__ import annotations

import json

import pytest
from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, grid-a, ledger db)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401

TEXT = """# Billing at night

The Invoicing application waits for Payments every night. Payments writes its results to the ledger db.

Both run on grid-a. srv-1 is a server of grid-a."""
FOUND = [
    {"from": "Invoicing", "kind": "depends_on", "to": "Payments",
     "quote": "The Invoicing application waits for Payments every night."},            # drawn already
    {"from": "Payments", "kind": "sends_to", "to": "ledger db", "quote": "Payments writes its results to the ledger db.",
     "short": "writes its results there", "long": "When Payments is slow at its end, check the ledger db in that window."},
    {"from": "Payments", "kind": "runs_on", "to": "grid-z", "quote": "Both run on grid-a."},         # no such part
    {"from": "Invoicing", "kind": "calls", "to": "ledger db", "quote": "Invoicing calls the ledger db directly."},  # not said
    {"from": "srv-1", "kind": "runs_on", "to": "grid-a", "quote": "srv-1 is a server of grid-a."},   # part of it
    {"from": "Payments", "kind": "owns", "to": "ledger db", "quote": "Payments writes its results to the ledger db."},
]


class Reader:
    def __init__(self, found) -> None:
        self.found, self.seen = found, []

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append(messages[1]["content"])
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "interactions", "arguments": json.dumps({"interactions": self.found})}}]}


@pytest.fixture()
def doc(system):  # noqa: F811
    from superset.extensions import db

    from supagent.models import Classified, Doc

    d = Doc(kind="upload", title="Billing at night", content=TEXT, enabled=True, status="ok")
    db.session.add(d)
    db.session.commit()
    yield d.id
    db.session.query(Doc).delete()
    db.session.query(Classified).delete()
    db.session.commit()


def test_what_a_document_states_is_proposed_with_its_sentence(doc, app):
    from superset.extensions import db

    from supagent.knowledge import interactions as I
    from supagent.knowledge import sysmap
    from supagent.knowledge.brief import build
    from supagent.models import Link

    llm = Reader(FOUND)
    out = I.run(llm)
    assert out == {"texts": 1, "calls": 1, "proposed": 1}
    assert "- Payments (application)" in llm.seen[0] and "- ledger db (service)" in llm.seen[0]
    assert "- srv-2" not in llm.seen[0]                                   # only the parts the text names
    x = db.session.query(Link).filter(Link.status == "proposed").one()
    pay = db.session.query(Link).filter(Link.kind == "calls").one().a_ref  # Payments, from the fixture
    assert (x.a_ref, x.kind, x.source) == (pay, "sends_to", "llm")
    # 0.9: the short explanation is what the map shows on a click, the long one what to do; the sentence, where
    assert (x.note, x.detail, x.explained_by) == ("writes its results there",
                                                 "When Payments is slow at its end, check the ledger db in that window.", "llm")
    assert x.evidence == '"Payments writes its results to the ledger db." (Billing at night)'
    # waiting: neither drawn nor followed
    assert all(i["kind"] != "sends_to" for i in sysmap.interactions())
    assert "send data to" not in "\n".join(build("why is Billing late?")["lines"])
    assert I.run(Reader(FOUND)) == {"texts": 0, "calls": 0, "proposed": 0}            # read once
    with _client(app, "admin") as c:
        d = c.get("/supagent/admin/api/review").get_json()
        card = next(k for k in d["links"] if k["id"] == x.id)
        assert (card["a_title"], card["b_title"], card["parts"]) == ("Payments (application)", "ledger db (service)", True)
        assert card["note"] == "writes its results there" and card["evidence"].startswith('"Payments writes its results')
        assert card["detail"].startswith("When Payments is slow")
        assert c.get("/supagent/dictionary/api/map").get_json()["proposed_interactions"] == 1
        assert c.post(f"/supagent/admin/api/links/{x.id}", json={"status": "approved"}).get_json()["status"] == "approved"
        m = c.get("/supagent/dictionary/api/map").get_json()
        assert m["proposed_interactions"] == 0 and any(k["kind"] == "sends_to" for k in m["links"])
    lines = "\n".join(build("why is Billing late?")["lines"])
    assert "- They send data to: ledger db (service: only Payments, writes its results there)." in lines
    assert ("- What to do when following them (the map's explanations): Payments sends data to ledger db: When "
            "Payments is slow at its end, check the ledger db in that window.") in lines


def test_a_rejected_interaction_is_never_proposed_again_and_a_changed_text_is_read_again(doc, app):
    from superset.extensions import db

    from supagent.knowledge import interactions as I
    from supagent.models import Doc, Link

    I.run(Reader(FOUND))
    x = db.session.query(Link).filter(Link.status == "proposed").one()
    x.status = "rejected"
    db.session.commit()
    assert I.run(Reader(FOUND), again=True)["proposed"] == 0
    d = db.session.get(Doc, doc)
    d.content = TEXT + "\n\nInvoicing calls the ledger db directly."
    db.session.commit()
    out = I.run(Reader(FOUND))                                             # the text changed: read again
    assert out["texts"] == 1 and out["proposed"] == 1
    new = db.session.query(Link).filter(Link.status == "proposed").one()
    assert new.kind == "calls" and "Invoicing calls the ledger db directly." in new.evidence and new.note is None


def test_a_text_that_names_one_part_costs_no_call_and_long_texts_are_cut_between_paragraphs(system):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import interactions as I
    from supagent.models import Classified, Doc

    db.session.add(Doc(kind="upload", title="Alone", content="Payments is the application that takes the money. " * 4,
                       enabled=True, status="ok"))
    db.session.commit()
    try:
        llm = Reader(FOUND)
        assert I.run(llm) == {"texts": 1, "calls": 0, "proposed": 0} and llm.seen == []
        pieces = I.windows("\n\n".join(f"Paragraph {i}. " + "word " * 300 for i in range(12)))
        assert len(pieces) >= 3 and all(len(p) <= 2 * I.WINDOW for p in pieces)
        assert all(p.startswith("Paragraph") for p in pieces)
    finally:
        db.session.query(Doc).delete()
        db.session.query(Classified).delete()
        db.session.commit()
