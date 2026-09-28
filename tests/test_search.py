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
