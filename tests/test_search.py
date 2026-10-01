"""Knowledge search: pieces that follow their origin, words and vectors, what each user may find,
memories (personal / team, approval), documents and sites."""

from __future__ import annotations

import json

import numpy as np
import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.fixture()
def clean_knowledge(world):
    from superset.extensions import db

    from supagent.knowledge.catalog import invalidate
    from supagent.models import Chunk, Doc, Entry, EntryVersion, Memory, Recipe

    for model in (Chunk, Doc, Memory, Recipe, EntryVersion, Entry):
        db.session.query(model).delete()
    db.session.commit()
    invalidate()
    yield world


def test_pieces_follow_their_origin(clean_knowledge):
    from superset.extensions import db

    from supagent.knowledge.catalog import delete_entry, save_entry
    from supagent.knowledge.index import sync
    from supagent.models import Chunk

    rule = save_entry({"title": "No UAT", "classification": "rule", "content": "Exclude UAT unless asked."}, by="t")
    first = sync()
    assert first["added"] >= 3                                   # the jobs index, the cpu metric, the rule
    refs = {c.ref: c for c in db.session.query(Chunk)}
    assert any(r.startswith("object:") for r in refs) and f"entry:{rule.id}#0" in refs
    assert sync() == {"added": 0, "changed": 0, "removed": 0, "unchanged": len(refs)}
    save_entry({"title": "No UAT", "classification": "rule", "content": "Exclude UAT and DEV."}, by="t",
               entry_id=rule.id)
    assert sync()["changed"] == 1
    delete_entry(rule.id, by="t")
    assert sync()["removed"] == 1


def test_words_find_only_what_the_user_may_see(clean_knowledge):
    from superset.extensions import security_manager as sm

    from supagent.knowledge.index import sync
    from supagent.knowledge.memory import add
    from supagent.knowledge.search import search
    from supagent.security import acting_as

    bob = sm.find_user(username="bob").id
    add(bob, "Bob wants the CPU numbers per server in percent", scope="user")
    add(bob, "Servers named srv-* belong to the compute grid", scope="team", approved_by="admin")
    add(bob, "Unapproved team rule about servers", scope="team")          # proposed: not used yet
    sync()
    with acting_as("admin"):
        kinds = {r["kind"] for r in search("cpu seconds server", k=10)}
    assert "metric" in kinds
    with acting_as("alice"):
        found = search("cpu seconds server jobs", k=10)
    texts = " ".join(f["title"] + f["text"] for f in found)
    assert "node_cpu_seconds_total" not in texts                 # alice may not query the metrics database
    assert "Bob wants" not in texts                               # bob's personal memory
    assert "compute grid" in texts and "Unapproved" not in texts      # the team's approved memory only
    with acting_as("bob"):
        assert "Bob wants" in " ".join(f["text"] for f in search("cpu server percent", k=10))


def test_vectors_rank_by_meaning(clean_knowledge, monkeypatch):
    from supagent import settings
    from supagent.knowledge import embeddings as E
    from supagent.knowledge.index import embed_pending, sync
    from supagent.knowledge.search import search
    from supagent.models import Chunk
    from supagent.security import acting_as
    from superset.extensions import db

    vocab = ["cpu", "processor", "server", "jobs", "failed", "temperature"]

    def fake_embed(texts):                       # a meaning: processor ~ cpu
        out = []
        for t in texts:
            low = t.lower()
            v = np.array([low.count(w) + (low.count("processor") if w == "cpu" else 0) for w in vocab], dtype=np.float32)
            out.append(v / (np.linalg.norm(v) or 1.0))
        return out

    monkeypatch.setattr(E, "embed", fake_embed)
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k: "fake-model" if k == "embed.model" else real_get(k))
    sync()
    done = embed_pending()
    assert done["embedded"] >= 2 and done["left"] == 0
    c = db.session.query(Chunk).filter(Chunk.vector.isnot(None)).first()
    assert len(c.vector) == 2 * len(vocab)                       # float16
    with acting_as("admin"):
        found = search("processor load", k=3)                     # no word in common with "cpu"
    assert found and found[0]["title"].startswith("metric node_cpu")
    with acting_as("alice"):
        assert all(not f["title"].startswith("metric") for f in search("processor load", k=5))


def test_memory_signals_approval_and_prompt(clean_knowledge):
    from superset.extensions import security_manager as sm

    from supagent.knowledge.memory import add, learn_from_message, prompt_block, worth_learning
    from supagent.models import Conversation, Memory, Message
    from superset.extensions import db

    assert worth_learning("From now on show durations in minutes") and worth_learning("Retiens que D-1 = hier")
    assert not worth_learning("How many jobs failed yesterday?")
    alice = sm.find_user(username="alice").id
    assert add(alice, "Show durations in minutes") is not None
    assert add(alice, "show   durations in MINUTES") is None           # the same thing
    conv = Conversation(user_id=alice, title="t")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="Always exclude UAT, for everyone"))
    answer = Message(conversation_id=conv.id, role="assistant", content="Done: UAT excluded.", status="done")
    db.session.add(answer)
    db.session.commit()

    class FakeLLM:
        def chat(self, messages, tools=None, max_tokens=None):
            return {"content": json.dumps([{"text": "Exclude the UAT environment", "scope": "team", "kind": "rule",
                                            "category": "environments"}])}

    made = learn_from_message(answer.id, llm=FakeLLM())
    m = db.session.get(Memory, made[0])
    assert (m.scope, m.status, m.kind) == ("team", "proposed", "rule")      # waits for an admin
    block = prompt_block(alice)
    assert "Show durations in minutes" in block and "UAT" not in block
    m.status = "active"
    db.session.commit()
    assert "Exclude the UAT environment" in prompt_block(sm.find_user(username="bob").id)
    assert "durations" not in prompt_block(sm.find_user(username="bob").id)


def test_memories_given_with_every_question_have_a_budget(clean_knowledge):
    """Statements are learned at once for their author: the block stays within memory.prompt_chars,
    rules and preferences before facts (facts left out are still found by the search)."""
    from superset.extensions import security_manager as sm

    from supagent import settings
    from supagent.knowledge.memory import add, prompt_block

    alice = sm.find_user(username="alice").id
    for i in range(12):
        assert add(alice, f"Fact {i}: " + " ".join(f"word{i}x{j}" for j in range(40)), kind="fact") is not None
    add(alice, "Always give durations in minutes", kind="rule")
    add(alice, "I prefer tables sorted by the largest value", kind="preference")
    block = prompt_block(alice)
    lines = block.strip().splitlines()[1:]
    assert sum(len(x) for x in lines) <= settings.get("memory.prompt_chars") == 2000
    assert "durations in minutes" in lines[0] and "sorted by the largest" in lines[1]    # rules, preferences first
    assert 2 < len(lines) < 14                                                          # some facts, not all
    settings.set_value("memory.prompt_chars", 0)                                        # 0: no budget
    try:
        assert len(prompt_block(alice).strip().splitlines()) == 15
    finally:
        settings.set_value("memory.prompt_chars", 2000)


def test_documents_are_read_safely(clean_knowledge, monkeypatch):
    from supagent import settings
    from supagent.knowledge import docs as D
    from supagent.knowledge.index import split_text, sync
    from supagent.models import Chunk, Doc
    from superset.extensions import db

    title, text, links = D.html_to_text("<html><head><title>Runbook</title><style>x{}</style></head><body>"
                                        "<h1>Failed jobs</h1><p>Check the <a href='/pool'>pool</a> first.</p>"
                                        "<script>alert(1)</script></body></html>")
    assert title == "Runbook" and "Failed jobs\nCheck the pool first." in text and "alert" not in text
    assert links == ["/pool"]
    with pytest.raises(D.DocError, match="private"):
        D.check_url("http://127.0.0.1:8088/secret")
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k: ["wiki.example.com"] if k == "docs.allowed_domains" else real_get(k))
    D.check_url("https://wiki.example.com/ops/runbook")          # an allowed domain, even if private
    with pytest.raises(D.DocError, match="not in docs.allowed_domains"):
        D.check_url("https://evil.example.org/")
    pages = {"https://wiki.example.com/ops/a": ("A", "Page A. " * 400, ["https://wiki.example.com/ops/b",
                                                                        "https://other.example.com/x"]),
             "https://wiki.example.com/ops/b": ("B", "Page B about POOL_07 saturation.", [])}
    monkeypatch.setattr(D, "fetch", lambda url: pages[url])
    doc = Doc(kind="url", url="https://wiki.example.com/ops/a", max_pages=5)
    db.session.add(doc)
    db.session.commit()
    out = D.refresh(doc)
    assert out["status"] == "ok" and out["pages"] == 2 and "POOL_07" in doc.content    # same site, not the other
    assert len(split_text(doc.content)) >= 2
    sync()
    assert db.session.query(Chunk).filter(Chunk.kind == "doc").count() >= 2


def test_memory_and_documents_api(app):
    from conftest import login

    with app.test_client() as c:
        login(c, "alice")
        r = c.post("/supagent/api/memory", json={"text": "Numbers with two decimals", "scope": "user"})
        assert r.status_code == 200
        mid = r.get_json()["memory"]["id"]
        data = c.get("/supagent/api/memory").get_json()
        assert any(m["id"] == mid for m in data["mine"])
        assert c.get("/supagent/admin/api/docs").status_code in (401, 403)
        assert c.get("/supagent/dictionary/api/search?q=numbers").status_code == 200
    with app.test_client() as c:
        login(c, "bob")
        assert c.delete(f"/supagent/api/memory/{mid}").status_code == 404        # not bob's
    with app.test_client() as c:
        login(c, "admin")
        r = c.post("/supagent/admin/api/docs", json={"name": "runbook.md", "content": "# Runbook\nRestart pool 7."})
        assert r.status_code == 200 and r.get_json()["doc"]["status"] == "ok"
        from superset.extensions import db

        from supagent.models import Doc

        up = db.session.get(Doc, r.get_json()["doc"]["id"])
        assert up.content_hash and up.fetched_at                       # read for definitions like a site
        bad = c.post("/supagent/admin/api/docs", json={"url": "http://127.0.0.1/secret"})
        assert bad.status_code == 400 and "private" in bad.get_json()["error"]
        assert c.get("/supagent/admin/api/memory").status_code == 200


def test_learned_answers_and_memories_about_vanished_data_are_not_given(clean_knowledge):
    """A metric the learning saw disappear: the learned answers and memories that name it are not
    given to the agent any more (they would send it to data that is not there)."""
    import datetime as dt

    from superset.extensions import db, security_manager as sm

    from supagent.knowledge import resolve
    from supagent.knowledge.experience import recipes_for
    from supagent.knowledge.memory import add, prompt_block
    from supagent.models import KObject, Recipe
    from supagent.security import acting_as

    world = clean_knowledge
    alice = sm.find_user(username="alice").id
    for q, sql in (("cpu of the servers", 'SELECT AVG(rate) FROM "node_cpu_seconds_total"'),
                   ("cpu load of the servers", 'SELECT COUNT(*) FROM "jobs"')):
        db.session.add(Recipe(question=q, words=" ".join(sorted({"cpu", "server", "load"})), tool="execute_sql",
                              database_id=world["jobs"].id, query=sql, signature=f"sig-{q}", status="confirmed",
                              uses=1, generic=True))
    db.session.commit()
    add(alice, "node_cpu_seconds_total is the CPU of the grid servers", kind="fact")
    add(alice, "Always show CPU in percent", kind="rule")
    resolve._CACHE.clear()
    with acting_as("admin"):
        assert len(recipes_for("cpu load of the servers")) == 2
    assert "node_cpu_seconds_total" in prompt_block(alice)
    db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one().gone_at = dt.datetime.utcnow()
    db.session.commit()
    resolve._CACHE.clear()
    with acting_as("admin"):
        assert [r["query"] for r in recipes_for("cpu load of the servers")] == ['SELECT COUNT(*) FROM "jobs"']
    block = prompt_block(alice)
    assert "node_cpu_seconds_total" not in block and "CPU in percent" in block
    assert resolve.mentions_gone("see node_cpu_seconds_total") and not resolve.mentions_gone("see node_cpu")


def test_weak_neighbours_found_by_meaning_only_are_left_out(clean_knowledge, monkeypatch):
    """A piece found by meaning only must be close enough (cosine floor, and not far behind the
    closest one); each result says which search found it."""
    from supagent import settings
    from supagent.knowledge import embeddings as E
    from supagent.knowledge import search as S
    from supagent.knowledge.index import sync
    from supagent.models import Chunk
    from supagent.security import acting_as
    from superset.extensions import db

    sync()
    chunks = {c.title: c.id for c in db.session.query(Chunk)}
    cpu = next(i for t, i in chunks.items() if t.startswith("metric node_cpu"))
    others = [i for t, i in chunks.items() if i != cpu]
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k: "fake-model" if k == "embed.model" else real_get(k))
    monkeypatch.setattr(E, "embed", lambda texts: [np.ones(4, dtype=np.float32)] * len(texts))
    monkeypatch.setattr(E, "nearest", lambda qv, allowed, k: [(cpu, 0.82)] + [(i, 0.30) for i in others])
    with acting_as("admin"):
        found = S.search("processor load", k=8)
        meaning_only = [f["title"] for f in found if f["via"] == "meaning"]
        assert len(meaning_only) == 1 and meaning_only[0].startswith("metric node_cpu")     # 0.30: left out
        assert all(f["via"] in ("words", "meaning", "words and meaning") for f in found)
        monkeypatch.setattr(E, "nearest", lambda qv, allowed, k: [(i, 0.30) for i in [cpu] + others])
        assert all(f["via"] != "meaning" for f in S.search("processor load", k=8))     # nothing close enough


def test_the_dictionary_search_lists_every_piece_with_its_link(app):
    """The Data dictionary's search: every piece found (not the 12 best), page by page in the page, each name a
    link to where it is read or edited; a piece is opened only by a user who may search it."""
    from conftest import login
    from superset.extensions import db

    from supagent.knowledge.index import sync
    from supagent.knowledge.search import link_of
    from supagent.models import Memory

    assert link_of("superset:chart:42") == {"href": "/explore/?slice_id=42", "where": "superset"}
    assert link_of("superset:dashboard:7:3") == {"href": "/superset/dashboard/7/", "where": "superset"}
    assert link_of("object:12") == {"href": "#open/object%3A12", "where": "dictionary"}
    assert link_of("entry:5#settlement-date") == {"href": "#open/entry%3A5", "where": "dictionary"}
    assert link_of("note:9#2")["href"] == "#open/note%3A9" and link_of("data:1:jobs") is None
    with app.app_context():
        mems = [Memory(scope="team", kind="rule", status="active", source="manual",
                       text=f"Settlement batch rule number {i}: amounts in EUR (paging test)") for i in range(40)]
        db.session.add_all(mems)
        db.session.commit()
        sync()
        ids = [m.id for m in mems]
    try:
        with app.test_client() as c:
            login(c, "alice")
            d = c.get("/supagent/dictionary/api/search?all=1&q=settlement+batch+rule").get_json()
            refs = [r["ref"] for r in d["results"]]
            assert d["total"] == len(refs) and len(set(refs) & {f"memory:{i}" for i in ids}) == 40
            hit = next(r for r in d["results"] if r["ref"] == f"memory:{ids[0]}")
            assert hit["link"] == {"href": f"#open/memory%3A{ids[0]}", "where": "dictionary"}
            assert len(c.get("/supagent/dictionary/api/search?q=settlement+batch+rule").get_json()["results"]) == 12
            got = c.get(f"/supagent/dictionary/api/item?ref=memory:{ids[0]}").get_json()
            assert "rule number 0" in got["text"] and got["kind"] == "memory"
            assert c.get("/supagent/dictionary/api/item?ref=memory:999999").status_code == 404
            assert c.get("/supagent/dictionary/api/item?ref=memory%25").status_code == 404
    finally:
        with app.app_context():
            db.session.query(Memory).filter(Memory.text.like("%(paging test)%")).delete(synchronize_session=False)
            db.session.commit()
            sync()


def test_a_private_note_or_memory_is_never_listed_nor_opened_for_another_user(app):
    """The dictionary's search lists everything found and opens any item by its address: a user's personal note and
    personal memory stay theirs (the search's own filter, before ranking and in api/item)."""
    from conftest import login
    from superset.extensions import db, security_manager

    from supagent.knowledge.index import sync
    from supagent.models import Memory, Note

    with app.app_context():
        alice = security_manager.find_user("alice").id
        note = Note(scope="user", user_id=alice, title="Quokka meeting", text="Quokka budget is 42k (privacy test)")
        mem = Memory(scope="user", user_id=alice, kind="preference", status="active", source="chat",
                     text="Quokka figures in thousands (privacy test)")
        team = Memory(scope="team", kind="rule", status="active", source="manual",
                      text="Quokka reports go to the team (privacy test)")
        db.session.add_all([note, mem, team])
        db.session.commit()
        sync()
        refs = {"note": f"note:{note.id}", "memory": f"memory:{mem.id}", "team": f"memory:{team.id}"}
    try:
        for user, sees in (("alice", True), ("bob", False)):
            with app.test_client() as c:
                login(c, user)
                found = {r["ref"].split("#")[0] for r in
                         c.get("/supagent/dictionary/api/search?all=1&q=quokka").get_json()["results"]}
                assert refs["team"] in found
                for k in ("note", "memory"):
                    assert (refs[k] in found) is sees, (user, k)
                    r = c.get(f"/supagent/dictionary/api/item?ref={refs[k]}")
                    assert r.status_code == (200 if sees else 404), (user, k)
                    if sees:
                        assert "Quokka" in r.get_json()["text"]
                assert c.get(f"/supagent/dictionary/api/item?ref={refs['team']}").status_code == 200
    finally:
        with app.app_context():
            db.session.query(Note).filter(Note.text.like("%(privacy test)%")).delete(synchronize_session=False)
            db.session.query(Memory).filter(Memory.text.like("%(privacy test)%")).delete(synchronize_session=False)
            db.session.commit()
            sync()
