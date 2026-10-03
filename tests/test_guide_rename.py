"""0.8: the catalog's "note" classification is "guide" (the users' quick notes are the notes): the upgrade renames
the entries, their history and their search pieces; older names and files still work."""

from __future__ import annotations


def test_the_upgrade_renames_the_catalog_notes(ctx):
    from superset.extensions import db

    from supagent.models import Chunk, Entry, EntryVersion, Meta, create_or_upgrade, store_kinds

    e = Entry(title="Runbook: restart the batch", classification="note", category="Ops", content="Restart it.",
              enabled=True, version=1, created_by="admin", updated_by="admin")
    db.session.add(e)
    db.session.flush()
    db.session.add(EntryVersion(entry_id=e.id, version=1, title=e.title, classification="note", content="Restart it."))
    db.session.add(Chunk(ref=f"entry:{e.id}#0", kind="note", title=e.title, text="Restart it."))
    db.session.add(Chunk(ref="note:999#0", kind="teamnote", title="A user's note", text="kept"))
    db.session.get(Meta, "schema_version").value = "13"
    db.session.commit()
    try:
        before, after = create_or_upgrade()
        assert (before, after) == (13, 15)
        db.session.expire_all()
        assert db.session.get(Entry, e.id).classification == "guide"
        assert db.session.query(EntryVersion).filter_by(entry_id=e.id).one().classification == "guide"
        assert db.session.query(Chunk).filter_by(ref=f"entry:{e.id}#0").one().kind == "guide"
        assert db.session.query(Chunk).filter_by(ref="note:999#0").one().kind == "teamnote"   # the users' notes stay
        assert store_kinds() == 0                       # no knowledge store on SQLite: nothing to follow
        assert create_or_upgrade() == (15, 15)          # once
    finally:
        db.session.query(Chunk).filter(Chunk.ref.in_([f"entry:{e.id}#0", "note:999#0"])).delete(
            synchronize_session=False)
        db.session.query(EntryVersion).filter_by(entry_id=e.id).delete(synchronize_session=False)
        db.session.delete(db.session.get(Entry, e.id))
        db.session.commit()


def test_the_old_name_is_still_understood(ctx):
    from superset.extensions import db

    from supagent.knowledge.catalog import CLASSIFICATIONS, delete_entry, guides, save_entry, split_catalog

    assert "guide" in CLASSIFICATIONS and "note" not in CLASSIFICATIONS
    e = save_entry({"title": "How the nightly batch works", "classification": "note", "content": "It runs at 2."},
                   by="admin")
    try:
        assert e.classification == "guide"
        assert any(g["title"] == "How the nightly batch works" for g in guides())
    finally:
        delete_entry(e.id, by="admin")
        db.session.commit()
    parts = split_catalog({"glossary": {"SLA": "the agreed time"}, "owners": {"batch": "ops team"}})
    assert [p["classification"] for p in parts if p["title"] == "Other catalog keys"] == ["guide"]


def test_searching_notes_finds_the_guides_and_the_users_notes(ctx, monkeypatch):
    from supagent import tools

    seen = []

    def fake_search(query, k=8, kinds=None, **_kw):
        seen.append(kinds)
        return []

    import supagent.knowledge.search as S

    monkeypatch.setattr(S, "search", fake_search)
    monkeypatch.setattr(tools, "_as_user", lambda: __import__("contextlib").nullcontext())
    tools.search_knowledge("restart", kind="note")
    tools.search_knowledge("restart", kind="guide")
    tools.search_knowledge("restart")
    assert seen == [("guide", "teamnote"), ("guide",), None]
