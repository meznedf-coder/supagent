"""Notes (0.7): any user writes one in a few seconds, for the team or for themselves; the team reads the team's,
nobody else a personal one (not even an admin); its author changes or deletes it, an admin a team one (pin, make
a catalog entry of it); "/note ..." and "/mynote ..." save one without the LLM; the list pages and searches; the
agent finds a note with its author and day, marked not verified, and never takes it as the source of a query's
condition."""

from __future__ import annotations

import pytest

from conftest import login

API = "/supagent/api/notes"


@pytest.fixture()
def clean(app):
    yield
    with app.app_context():
        from superset.extensions import db

        from supagent.models import Chunk, Entry, Note

        db.session.query(Note).delete()
        db.session.query(Chunk).filter(Chunk.ref.like("note:%")).delete(synchronize_session=False)
        db.session.query(Entry).filter(Entry.category == "Team notes").delete(synchronize_session=False)
        db.session.commit()
        db.session.remove()


def _client(app, user):
    c = app.test_client()
    login(c, user)
    return c


def test_a_team_note_for_everyone_a_personal_one_for_its_author(app, clean):
    alice, bob, admin = _client(app, "alice"), _client(app, "bob"), _client(app, "admin")
    r = alice.post(API, json={"text": "Pricing weekly\nThe FX surface is rebuilt at 07:00 from now on.",
                              "tags": "pricing, Meeting", "meeting_on": "2026-09-30"}).get_json()
    team = r["note"]
    assert team["title"] == "Pricing weekly" and team["tags"] == ["pricing", "meeting"] and team["scope"] == "team"
    assert team["day"] == "2026-09-30" and team["author"] == "alice Test" and team["mine"] and team["can_change"]
    mine = alice.post(API, json={"text": "Call the ledger team about the late export", "scope": "user"}).get_json()["note"]
    seen = {n["id"]: n for n in bob.get(API).get_json()["notes"]}
    assert team["id"] in seen and mine["id"] not in seen                  # the team's, never someone's own
    assert not seen[team["id"]]["can_change"] and not seen[team["id"]]["mine"]
    assert mine["id"] not in {n["id"] for n in admin.get(API).get_json()["notes"]}     # not even an admin's
    assert {n["id"] for n in alice.get(API + "?mine=1").get_json()["notes"]} == {team["id"], mine["id"]}
    nobody = app.test_client()
    login(nobody, "nobody")                                              # no AI Agent role
    assert nobody.get(API).status_code in (401, 403)
    assert nobody.post(API, json={"text": "x"}).status_code in (401, 403)


def test_its_author_changes_it_an_admin_a_team_one(app, clean):
    alice, bob, admin = _client(app, "alice"), _client(app, "bob"), _client(app, "admin")
    team = alice.post(API, json={"text": "Batch freeze on Friday 17:00"}).get_json()["note"]
    mine = alice.post(API, json={"text": "My own reminder", "scope": "user"}).get_json()["note"]
    assert bob.post(f"{API}/{team['id']}", json={"text": "changed by bob"}).status_code == 404
    assert bob.delete(f"{API}/{team['id']}").status_code == 404
    assert admin.post(f"{API}/{mine['id']}", json={"text": "an admin's change"}).status_code == 404
    r = admin.post(f"{API}/{team['id']}", json={"text": "Batch freeze on Friday 18:00", "pinned": True}).get_json()
    assert r["note"]["text"] == "Batch freeze on Friday 18:00" and r["note"]["pinned"] and r["note"]["updated_by"] == "admin"
    r = alice.post(f"{API}/{team['id']}", json={"pinned": False, "title": "Freeze"}).get_json()
    assert r["note"]["pinned"] and r["note"]["title"] == "Freeze"        # pinned: an admin's (her title kept)
    first = bob.get(API).get_json()["notes"][0]
    assert first["id"] == team["id"]                                     # pinned first
    assert alice.delete(f"{API}/{mine['id']}").get_json() == {"deleted": mine["id"]}
    assert alice.post(f"{API}/{team['id']}", json={"text": "  "}).status_code == 400


def test_the_list_pages_and_finds_every_word(app, clean):
    alice = _client(app, "alice")
    for i in range(25):
        alice.post(API, json={"text": f"Daily stand-up {i}: " + ("ledger export late" if i % 5 == 0 else "nothing new")})
    d = alice.get(API + "?limit=10&offset=10").get_json()
    assert d["total"] == 25 and len(d["notes"]) == 10 and d["offset"] == 10 and "catalog" not in d
    d = alice.get(API + "?q=ledger%20late").get_json()
    assert d["total"] == 5 and all("ledger export late" in n["text"] for n in d["notes"])
    assert alice.get(API + "?q=ledger%20payroll").get_json()["total"] == 0          # every word
    assert alice.get(API + "?limit=x").status_code == 400


def test_the_same_note_twice_is_one_and_limits_are_said(app, clean):
    alice = _client(app, "alice")
    a = alice.post(API, json={"text": "Q3 close moved to 3 October"}).get_json()["note"]
    b = alice.post(API, json={"text": "Q3 close moved to 3 October"}).get_json()["note"]
    assert a["id"] == b["id"]                                            # a double click
    assert "empty" in alice.post(API, json={"text": "   "}).get_json()["error"]
    assert "at most" in alice.post(API, json={"text": "x" * 50_001}).get_json()["error"]
    assert alice.post(API, json={"text": "x", "scope": "everyone"}).status_code == 400
    assert alice.post(API, json={"text": "x", "meeting_on": "30/09"}).status_code == 400


def test_the_commands_of_the_question_box():
    from supagent.knowledge.notes import command

    assert command("/note The FX desk moves to floor 3") == {"scope": "team", "text": "The FX desk moves to floor 3"}
    assert command("  /MyNote: call Anna") == {"scope": "user", "text": "call Anna"}
    assert command("/note") == {"scope": "team", "text": ""}
    assert command("/notes of yesterday") is None and command("What is a /note?") is None


def test_an_admin_makes_a_catalog_entry_of_a_team_note(app, clean):
    alice, bob, admin = _client(app, "alice"), _client(app, "bob"), _client(app, "admin")
    team = alice.post(API, json={"text": "Unexplained PnL above 5% is investigated the same day.",
                                 "meeting_on": "2026-09-29"}).get_json()["note"]
    mine = alice.post(API, json={"text": "Personal", "scope": "user"}).get_json()["note"]
    assert bob.post(f"{API}/{team['id']}/promote").status_code == 404          # an admin's (404: nothing said)
    r = admin.post(f"{API}/{team['id']}/promote").get_json()
    assert r["note"]["entry_id"] == r["entry_id"]
    assert admin.post(f"{API}/{mine['id']}/promote").status_code == 404           # a personal note: never
    with app.app_context():
        from superset.extensions import db

        from supagent.models import Entry

        e = db.session.get(Entry, r["entry_id"])
        assert e.classification == "guide" and e.category == "Team notes" and e.updated_by == "admin"
        assert e.content.endswith("(From the note of alice Test of 2026-09-29.)")
        db.session.remove()
    d = bob.get(API + "?q=unexplained").get_json()
    assert [c["id"] for c in d["catalog"]] == [r["entry_id"]]            # read with the team's notes, verified


def test_the_agent_finds_a_note_with_its_author_and_day_and_never_as_a_condition(app, clean):
    alice = _client(app, "alice")
    team = alice.post(API, json={"text": "Desk ZQXDESK is closed: count its trades apart.",
                                 "meeting_on": "2026-09-30"}).get_json()["note"]
    alice.post(API, json={"text": "My draft about desk ZQXDESK", "scope": "user"})
    with app.app_context():
        from superset.extensions import db

        from supagent.knowledge import conditions as C
        from supagent.knowledge.index import sync
        from supagent.knowledge.search import knowledge_block, search
        from supagent.models import Chunk
        from supagent.security import acting_as

        sync(("note:",))
        chunks = {c.ref: c for c in db.session.query(Chunk).filter(Chunk.ref.like("note:%"))}
        piece = chunks[f"note:{team['id']}#0"]
        assert piece.kind == "teamnote" and piece.scope == "team"
        assert piece.title.startswith("Team note by alice Test of 2026-09-30 (not verified): Desk ZQXDESK")
        with acting_as("bob"):
            found = [f["ref"] for f in search("ZQXDESK closed desk", 10)]
            assert f"note:{team['id']}#0" in found and not any(r != f"note:{team['id']}#0" for r in found
                                                               if r.startswith("note:"))   # not alice's own
            block = knowledge_block("How many trades did desk ZQXDESK book?")
            from supagent.tools import search_knowledge                # kind "note": the users' notes too (the
            refs = [r["ref"] for r in search_knowledge("ZQXDESK closed desk", kind="note")["results"]]   # retail lab)
            assert f"note:{team['id']}#0" in refs and not any(r.startswith("note:") and r != f"note:{team['id']}#0"
                                                              for r in refs)
        assert "- [teamnote] Team note by alice Test" in block
        with acting_as("alice"):
            assert len([f for f in search("ZQXDESK", 10) if f["ref"].startswith("note:")]) == 2
        support = C.build([{"role": "user", "content": "How many trades did the desks book?" + block}], [])
        assert "zqxdesk" not in support.text                             # a note never says a condition
        assert C.unsaid(C.sql_conditions("SELECT COUNT(*) FROM t WHERE DESK = 'ZQXDESK'"), support)
        db.session.remove()


def test_team_notes_are_classified_personal_ones_not(app, clean):
    alice = _client(app, "alice")
    team = alice.post(API, json={"text": "Risk weekly: VaR limits reviewed"}).get_json()["note"]
    mine = alice.post(API, json={"text": "Risk: my questions", "scope": "user"}).get_json()["note"]
    with app.app_context():
        from supagent.knowledge.facets import items

        refs = {i["ref"]: i for i in items()}
        assert f"note:{team['id']}" in refs and refs[f"note:{team['id']}"]["kind"] == "team note"
        assert f"note:{mine['id']}" not in refs


def test_the_notes_tool_finds_words_and_days_for_its_user(app, clean):
    alice = _client(app, "alice")
    alice.post(API, json={"text": "Pricing weekly: the HESTON timeouts are checked by Anna", "meeting_on": "2026-09-28"})
    alice.post(API, json={"text": "Pricing weekly: the vol surface is rebuilt at 07:00", "meeting_on": "2026-09-21"})
    alice.post(API, json={"text": "My pricing draft", "scope": "user"})
    with app.app_context():
        from supagent import tools
        from supagent.agent import TOOLS_OF, intents
        from supagent.security import acting_as

        with acting_as("bob"):
            r = tools.search_notes(words="pricing weekly", since="2026-09-25")
            assert r["total"] == 1 and r["notes"][0]["day"] == "2026-09-28" and r["notes"][0]["author"] == "alice Test"
            assert "not verified" in r["about"]
            assert tools.search_notes(words="pricing", until="2026-09-22")["notes"][0]["day"] == "2026-09-21"
            assert tools.search_notes(words="draft")["total"] == 0                     # alice's own: not bob's
            assert "error" in tools.search_notes(since="last week")
        with acting_as("alice"):
            assert tools.search_notes(words="draft")["total"] == 1
        assert "notes" in intents("What did we decide in the pricing meeting on Monday?")
        assert "search_notes" in TOOLS_OF["notes"] and "notes" not in intents("How many jobs failed yesterday?")


def test_the_agent_reads_writes_and_changes_notes_as_its_user(app, clean):
    """The notes assistant: the agent's tools read a note in full, write one (the team's or the user's own),
    add to one or replace its text (its earlier version kept: undo puts it back), as the user who asks: a
    personal note is its author's only, a team note its author's or an admin's to change."""
    alice = _client(app, "alice")
    team = alice.post(API, json={"text": "Batch freeze on Friday 17:00"}).get_json()["note"]
    with app.app_context():
        from supagent import tools as T
        from supagent.security import acting_as

        with acting_as("alice"):
            saved = T.add_note("Ops meeting: QUICKSHIP strike, express orders to FASTPOST", personal=True,
                               day="2026-09-18", tags="ops, carriers")["saved"]
            assert saved["for"] == "its author only" and saved["day"] == "2026-09-18" and saved["can_change"]
            assert [n["id"] for n in T.search_notes(words="quickship", mine=True)["notes"]] == [saved["id"]]
            changed = T.change_note(saved["id"], add_text="QUICKSHIP back on 21 September.")["after"]
            assert changed["text"] == ("Ops meeting: QUICKSHIP strike, express orders to FASTPOST\n\n"
                                       "QUICKSHIP back on 21 September.") and changed["earlier_versions"] == 1
            back = T.change_note(saved["id"], undo=True)["after"]
            assert back["text"] == "Ops meeting: QUICKSHIP strike, express orders to FASTPOST"
            assert back["earlier_versions"] == 0
            assert "error" in T.change_note(saved["id"], undo=True)               # nothing earlier
            assert "error" in T.change_note(saved["id"])                          # nothing to change
            assert T.read_note(team["id"])["note"]["can_change"] is True          # her team note
        with acting_as("bob"):
            assert "error" in T.read_note(saved["id"])                            # alice's own
            assert "error" in T.change_note(team["id"], add_text="bob was here")  # not his, not an admin
            assert T.read_note(team["id"])["note"]["can_change"] is False
        with acting_as("admin"):
            assert T.change_note(team["id"], title="Freeze")["after"]["title"] == "Freeze"   # a team note: an admin's
            assert "error" in T.change_note(team["id"], personal=True)            # only its author
            assert "error" in T.read_note(saved["id"])                            # a personal one: not an admin's
        from superset.extensions import db

        db.session.remove()


def test_a_note_is_deleted_only_after_the_users_yes(app, clean, monkeypatch):
    """The agent shows the note and asks; it deletes only in its answer to the user's yes, and a call with
    confirmed=true in the turn the deletion was asked is never run (not even sent again)."""
    from test_agent_loop import agent_with, call, say

    with app.app_context():
        from supagent import tools as T
        from supagent.knowledge.notes import delete_refusal
        from supagent.security import acting_as

        with acting_as("alice"):
            n = T.add_note("Carriers review: keep EUROPARCEL for the islands", personal=True)["saved"]
            shown = T.delete_note(n["id"])
            assert shown["to_confirm"]["id"] == n["id"] and T.read_note(n["id"])["note"]   # not deleted
        asked = [{"role": "user", "content": "Delete my note about the carriers review"},
                 {"role": "assistant", "content": f"Delete your note \"{n['title']}\" of {n['day']}?"}]
        args = {"note_id": n["id"], "confirmed": True}
        assert delete_refusal("Delete my note about the carriers review", [], args)       # no yes yet
        assert delete_refusal("yes", asked, args) is None
        assert delete_refusal("Oui, vas-y", asked, args) is None
        assert delete_refusal("no, keep it", asked, args)
        assert delete_refusal("yes", asked, {"note_id": n["id"] + 1000, "confirmed": True})   # not that note
        assert delete_refusal("whatever", [], {"note_id": n["id"]}) is None                  # only shown

        a, ran = agent_with(monkeypatch, [call("delete_note", args), call("delete_note", args), say("Asked.")])
        a.names = a.names | {"delete_note"}
        _answer, trace = a.ask("Delete my note about the carriers review")
        assert ran == [] and all("not run: confirmation" in t["result"] for t in trace)
        b, ran = agent_with(monkeypatch, [call("delete_note", args), say("Deleted.")])
        b.names = b.names | {"delete_note"}
        b.ask("yes", asked)
        assert ran == [("delete_note", args)]
        c, ran = agent_with(monkeypatch, [call("delete_note", {"note_id": n["id"]}), say("Deleted.")])
        c.names = c.names | {"delete_note"}
        c.ask("yes", asked)                                     # the yes given: the call is the confirmed one
        assert ran == [("delete_note", {"note_id": n["id"], "confirmed": True})]
        d, ran = agent_with(monkeypatch, [call("delete_note", {"note_id": n["id"]}), say("Shown.")])
        d.names = d.names | {"delete_note"}
        d.ask("Delete my note about the carriers review")       # no yes yet: only shown
        assert ran == [("delete_note", {"note_id": n["id"]})]
        with acting_as("bob"):
            assert "error" in T.delete_note(n["id"], confirmed=True)                       # not his
        with acting_as("alice"):
            assert T.delete_note(n["id"], confirmed=True)["deleted"]["id"] == n["id"]
            assert "error" in T.read_note(n["id"])
        from superset.extensions import db

        db.session.remove()


def test_a_request_about_notes_gets_the_notes_tools_only():
    """"Note for the team: ..." was answered with an average order value (the retail lab: the data tools were
    offered and used): a request about the notes themselves is offered the notes tools only."""
    from supagent.agent import TOOLS_OF, Agent, notes_request

    for asked in ("Note for the team: the ops meeting kept FASTPOST.", "Make a personal note for me: call LYON.",
                  "Add to that note: Lea follows up on Friday.", "Undo that last change to the note.",
                  "Delete my personal note about the LYON warehouse.", "Delete the team note about FASTPOST.",
                  "Show me my notes", "Can you add a note: the freeze moves to 18:00", "Supprime ma note sur LYON"):
        assert notes_request(asked), asked
    for asked in ("What did we decide about FASTPOST?", "How many notes of credit were issued?",
                  "Why were so many deliveries late?", "Which notes mention the strike?"):
        assert not notes_request(asked), asked
    a = object.__new__(Agent)
    a.rich, a.wants_saved_chart, a.intent_text = True, False, None
    a.specs = [{"function": {"name": n}} for n in ("execute_sql", "describe_data", "search_notes", "read_note",
                                                     "add_note", "change_note", "delete_note")]
    a._route_intents = lambda: set()
    assert {s["function"]["name"] for s in a._specs_for("Delete my note about the carriers review")} == \
        TOOLS_OF["notes"]
    assert {s["function"]["name"] for s in a._specs_for("How many orders failed?")} == {"execute_sql", "describe_data"}


def test_an_answer_that_says_a_note_was_changed_without_the_call_is_sent_back(ctx, monkeypatch):
    """The lab's model wrote "The note ... has been deleted." with no tool call: sent back once, then marked; a
    note the tool did change is not."""
    import json

    from test_agent_loop import agent_with, call, say

    from supagent.agent import claims_check, note_claim

    a, ran = agent_with(monkeypatch, [say("The personal note \"Call LYON\" (note_id 11) has been deleted."),
                                      say("The personal note \"Call LYON\" has been deleted.")])
    answer, _trace = a.ask("Delete my personal note about the LYON warehouse.")
    assert ran == [] and a.usage["nudges"] == 1
    assert answer.startswith("I have not deleted any note: the deletion did not run.")     # not shown as done
    done = [{"tool": "change_note", "status": "done", "result": json.dumps({"before": {}, "after": {}})}]
    assert note_claim("The note has been updated with the new line.", done) is None
    assert note_claim("I deleted the note about LYON.", done) == ("deleted", "delete_note")
    assert note_claim("The note was written by Admin Lab on 18 September.", []) is None      # a description
    assert note_claim("I have added to the note: \"Lea follows up on Friday.\"", done) is None   # an append
    assert note_claim("The note has been saved.", done) is None                              # a change saves it
    assert "(Check: no note was deleted" in claims_check("The note has been deleted.", [])
    b, ran = agent_with(monkeypatch, [call("add_note", {"text": "Call LYON on Monday"}),
                                      say("The note has been saved for you.")],
                        results=lambda name, args: json.dumps({"saved": {"id": 3, "title": "Call LYON on Monday"}}))
    b.names = b.names | {"add_note"}
    answer, _trace = b.ask("Please keep a personal note that I call LYON on Monday")      # no colon: the LLM
    assert answer.startswith("The note has been saved for you.") and "(Check:" not in answer


def test_a_note_asked_in_words_is_saved_without_the_llm(app, clean, monkeypatch):
    """"Note for the team: ..." was taken for a question by the lab's LLM (16 searches, no note): a message that
    asks in words to write a note, its text after a colon, is saved at once like "/note"; "Note: ..." alone is
    not one (context for a question)."""
    from test_agent_loop import agent_with

    from supagent.knowledge.notes import asked_to_write

    assert asked_to_write("Note for the team: the freeze moves to 18:00.") == {
        "scope": "team", "text": "the freeze moves to 18:00."}
    assert asked_to_write("Make a personal note for me: call LYON on Monday.")["scope"] == "user"
    assert asked_to_write("Add a note: QUICKSHIP is back.")["scope"] == "team"
    assert asked_to_write("Note pour moi : appeler LYON lundi.")["scope"] == "user"
    for not_one in ("Note: the data ends on 24 September. How many orders failed?", "What did the note say?",
                    "Make a note of the failed jobs on 23 September: which ones?"):
        assert asked_to_write(not_one) is None, not_one
    with app.app_context():
        from supagent.security import acting_as

        a, _ran = agent_with(monkeypatch, [])                     # no LLM reply: it is never asked
        with acting_as("alice"):
            answer, trace = a.ask("Note for the team: the batch freeze moves to Friday 18:00.")
            assert answer.startswith("Saved for the team: “") and trace[0]["tool"] == "add_note"
            from supagent import tools as T

            found = T.search_notes(words="freeze")["notes"]
            assert [n["text"] for n in found] == ["the batch freeze moves to Friday 18:00."]
        from superset.extensions import db

        db.session.remove()
