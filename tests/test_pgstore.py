"""The knowledge store (pgstore) on a real PostgreSQL with pgvector, pg_trgm and pg_textsearch (SUPAGENT_TEST_PG,
e.g. postgresql://user@host:5432/db; skipped without it): built from the pieces with their vectors, searched by
words (BM25), near spellings and meaning with the user's permissions, each user's chats found by their owner
only, confirmed routes voting for their tables, kept in step with the pieces, rebuilt for another embedding
model, wiped (the search of 0.5 answers again), and its connections never left in a failed transaction."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re

import pytest

from test_decider import lab  # noqa: F401  (the fixture: two jobs databases, a metrics database, rule, glossary)

URI = os.environ.get("SUPAGENT_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="SUPAGENT_TEST_PG (a PostgreSQL with the extensions) is not set")


def _fake_embed(dims):
    """Words hashed into `dims` numbers: texts sharing words are close (a stand-in for the model)."""
    import numpy as np

    def embed(texts):
        out = []
        for t in texts:
            v = np.zeros(dims, dtype=np.float32)
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dims] += 1.0
            out.append(v / (float(np.linalg.norm(v)) or 1.0))
        return out
    return embed


@pytest.fixture()
def store(lab, monkeypatch):  # noqa: F811
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import embeddings as E, pgstore
    from supagent.knowledge.index import embed_pending

    conf = {"search.store_uri": URI, "search.store_schema": "supagent_store_test", "embed.model": "fake-64",
            "search.store": "auto", "search.chats": True}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    monkeypatch.setattr(E, "embed", _fake_embed(64))
    pgstore.forget_state()
    pgstore.wipe()
    embed_pending()                                   # the pieces' vectors (supagent_chunk), before the store
    built = pgstore.rebuild()
    yield {**lab, "conf": conf, "built": built}
    db.session.rollback()
    pgstore.wipe()
    pgstore.forget_state()


def test_built_with_the_vectors_and_searched_three_ways(store):
    from supagent.knowledge import pgstore
    from supagent.security import acting_as

    b = store["built"]
    assert b["bm25"].startswith("pg_textsearch") and b["dims"] == 64 and b["docs"] > 5 and b["names"] > 20
    st = pgstore.status()
    assert st["rows"]["metric"] == 5 and st["without_vector"] == 0
    old = pgstore._version(st["extensions"]["pg_textsearch"]) < pgstore.SAFE_TEXTSEARCH
    assert any("pre-release" in w for w in st["warnings"]) == old     # 0.5.x warned about, 1.x not
    with acting_as("admin"):
        top = pgstore.search("Seconds the CPUs spent in each mode", 3)
        assert top[0]["title"] == "metric node_cpu_seconds_total" and "words" in top[0]["via"]
        assert "meaning" in top[0]["via"] and top[0]["cos"] > 0.5
        typo = pgstore.search("rate of node_cpu_secnds_total", 5)
        hit = next(f for f in typo if f["title"] == "metric node_cpu_seconds_total")
        assert "spelling" in hit["via"] and hit["spelled"][0]["name"] == "node_cpu_seconds_total"
        value = pgstore.search("jobs of BILLNG", 8)          # a value typed with a typo
        assert any(x["name"] == "BILLING" for f in value for x in f["spelled"])


def test_an_ordinary_database_user_builds_and_searches_it(store):
    """Superset's database user is seldom a superuser: shared_preload_libraries is not shown to it (SHOW fails;
    the store was then never built). It is built and searched all the same; the preload is said unknown."""
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url

    from supagent.knowledge import pgstore
    from supagent.security import acting_as

    admin = sa.create_engine(URI, isolation_level="AUTOCOMMIT")
    with admin.connect() as con:
        if not con.execute(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar():
            admin.dispose()
            pytest.skip("SUPAGENT_TEST_PG's user may not create a role")
        name = con.execute(sa.text("SELECT current_database()")).scalar()
        con.execute(sa.text("DROP SCHEMA IF EXISTS supagent_store_plain CASCADE"))
        if not con.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'supagent_plain'")).scalar():
            con.execute(sa.text("CREATE ROLE supagent_plain LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"))
        con.execute(sa.text(f'GRANT CREATE ON DATABASE "{name}" TO supagent_plain'))
    plain = make_url(URI).set(username="supagent_plain", password=None).render_as_string(hide_password=False)
    store["conf"].update({"search.store_uri": plain, "search.store_schema": "supagent_store_plain"})
    pgstore.forget_state()
    try:
        with pgstore.engine().connect() as con:
            assert con.execute(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar() is False
        out = pgstore.rebuild()
        assert out["docs"] > 5 and out["names"] > 20 and out["dims"] == 64 and out["bm25"].startswith("pg_textsearch")
        st = pgstore.status()
        assert "error" not in st and st["extensions"]["pg_textsearch_preloaded"] is None and st["rows"]["metric"] == 5
        old = pgstore._version(st["extensions"]["pg_textsearch"]) < pgstore.SAFE_TEXTSEARCH
        assert any("not shown to this database user" in w for w in st["warnings"]) == old
        with acting_as("admin"):
            assert pgstore.search("Seconds the CPUs spent in each mode", 3)[0]["title"] == "metric node_cpu_seconds_total"
        assert pgstore.maintain()["sync"]["added"] == 0                       # kept in step by that user too
    finally:
        pgstore.wipe()
        store["conf"].update({"search.store_uri": URI, "search.store_schema": "supagent_store_test"})
        pgstore.forget_state()
        with admin.connect() as con:
            con.execute(sa.text("DROP SCHEMA IF EXISTS supagent_store_plain CASCADE"))
            con.execute(sa.text(f'REVOKE CREATE ON DATABASE "{name}" FROM supagent_plain'))
            con.execute(sa.text("DROP ROLE IF EXISTS supagent_plain"))
        admin.dispose()

def test_what_a_user_may_not_query_is_never_found(store):
    from superset.connectors.sqla.models import SqlaTable  # noqa: F401  (models loaded)
    from superset.extensions import db, security_manager as sm
    from superset.models.core import Database

    from supagent.knowledge import pgstore
    from supagent.security import acting_as

    role = sm.find_role("store readers") or sm.add_role("store readers")
    main = db.session.get(Database, store["main"])
    pvm = sm.find_permission_view_menu("database_access", main.perm) or \
        sm.add_permission_view_menu("database_access", main.perm)
    sm.add_permission_role(role, pvm)
    alice = sm.find_user(username="alice")
    alice.roles.append(role)
    db.session.commit()
    try:
        with acting_as("alice"):
            found = pgstore.search("Seconds the CPUs spent in each mode, batch job executions", 20)
            titles = [f["title"] for f in found]
            assert "index batch-jobs" in titles and not any(t.startswith("metric ") for t in titles)
            assert not pgstore.search("node_cpu_secnds_total", 5) or all(
                not f["title"].startswith("metric ") for f in pgstore.search("node_cpu_secnds_total", 5))
        with acting_as("admin"):
            assert any(f["title"].startswith("metric ") for f in pgstore.search("CPU seconds", 5))
    finally:
        alice = sm.find_user(username="alice")
        alice.roles = [r for r in alice.roles if r.name != "store readers"]
        db.session.commit()


def _chat(user, question, answer, when=None, feedback=None):
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, Message

    c = Conversation(user_id=sm.find_user(username=user).id, title=question[:40])
    db.session.add(c)
    db.session.flush()
    at = when or dt.datetime.utcnow()
    db.session.add(Message(conversation_id=c.id, role="user", content=question, created_at=at))
    m = Message(conversation_id=c.id, role="assistant", content=answer, status="done", created_at=at,
                feedback=feedback)
    db.session.add(m)
    db.session.commit()
    return c.id, m.id


def test_chats_are_found_by_their_owner_only(store):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge import pgstore
    from supagent.models import Conversation

    mine = _chat("alice", "How many BILLING jobs failed on 23 September?", "42 BILLING jobs failed on 23 September.")
    his = _chat("bob", "How many BILLING jobs failed on 22 September?", "7 BILLING jobs failed on 22 September.")
    try:
        out = pgstore.maintain()
        assert out["sync"]["added"] >= 2 and out["sync"]["embedded"]["embedded"] >= 2
        alice, bob = sm.find_user(username="alice").id, sm.find_user(username="bob").id
        found = pgstore.chats("failed BILLING jobs 23 September", alice)
        assert found[0]["conversation_id"] == mine[0] and found[0]["answer"].startswith("42 BILLING jobs")
        assert his[0] not in {c["conversation_id"] for c in found}
        assert mine[0] not in {c["conversation_id"] for c in pgstore.chats("failed BILLING jobs", bob)}
        from supagent.security import acting_as

        with acting_as("admin"):                       # never in the knowledge search, not even for an admin
            assert not any(f["kind"] == "chat" for f in pgstore.search("BILLING jobs failed", 20))
        again = pgstore.maintain()["sync"]
        assert again["added"] == 0 and again["changed"] == 0            # in step: nothing written again
        from supagent.models import Message

        m = db.session.get(Message, mine[1])
        m.feedback = -1                                                 # Not helpful: the row follows
        db.session.commit()
        assert pgstore.maintain()["sync"]["changed"] == 1
        assert pgstore.chats("failed BILLING jobs 23 September", alice)[0]["feedback"] == -1
        # the search box: every word typed, or close in meaning; never the nearest of anything
        def said(cid, typed):                                           # every word typed is in that chat
            text_ = " ".join(x.content or "" for x in db.session.query(Message).filter(Message.conversation_id == cid))
            return set(pgstore.words(typed)) <= set(pgstore.words(text_))

        box = pgstore.chats("BILLING failed", alice, k=10, min_cos=0.99)
        ids = [c["conversation_id"] for c in box]
        assert mine[0] in ids and his[0] not in ids and all(c["message_id"] for c in box), box
        # alice's other chats (other tests' in a full run) come only with every word typed; never another user's
        assert {c.user_id for c in db.session.query(Conversation).filter(Conversation.id.in_(ids))} == {alice}
        assert all(said(i, "BILLING failed") for i in ids), box
        other = pgstore.chats("BILLING payroll", alice, k=10, min_cos=0.99)
        assert mine[0] not in {c["conversation_id"] for c in other}                     # one word is not in mine
        assert all(said(c["conversation_id"], "BILLING payroll") for c in other), other
        assert pgstore.chats("zzqx vvkw", alice, k=10, min_cos=0.99) == []
    finally:
        db.session.query(Conversation).filter(Conversation.title.like("How many BILLING jobs failed%")).delete(
            synchronize_session=False)
        db.session.commit()


def test_confirmed_routes_vote_for_their_tables(store):
    from superset.extensions import db

    from supagent.governed.decider import gather
    from supagent.knowledge import pgstore
    from supagent.models import Route
    from supagent.security import acting_as

    jobs = f"data:{store['main']}:batch-jobs"
    db.session.add(Route(question="how many jobs of the night batch ended in error", terms="", used=[jobs],
                         chosen=[jobs], signal="helpful", signal_at=dt.datetime.utcnow()))
    db.session.add(Route(question="how many jobs of the night batch ended in error", terms="",
                         used=[f"data:{store['replica']}:batch-jobs"], signal="not_helpful"))
    db.session.commit()
    pgstore.maintain()
    votes = pgstore.neighbors("How many jobs of the night batch ended in error?", None)
    assert votes == {jobs: 1.0}                       # the confirmed one only
    with acting_as("admin"):
        g = gather("How many jobs of the night batch ended in error?")
        assert g.tables[jobs].features.get("neighbors") == 1.0


def test_kept_in_step_and_the_search_goes_through_it(store):
    from superset.extensions import db

    from supagent.knowledge import pgstore
    from supagent.knowledge.index import sync
    from supagent.knowledge.search import search
    from supagent.models import Entry
    from supagent.security import acting_as

    rule = db.session.query(Entry).filter(Entry.title == "Decider rule").one()
    rule.content = "Exclude the UAT environment (ENV = 'UAT') unless asked; count the zebrafish reruns once."
    db.session.commit()
    sync()                                            # the chunk changes: the store with it (index's hook)
    with acting_as("admin"):
        found = search("zebrafish reruns", 3)
        assert found[0]["title"].startswith("Decider rule") and "ranks" in found[0]     # through the store
        db.session.delete(rule)
        db.session.commit()
        sync()
        assert not any(f["title"].startswith("Decider rule") for f in search("zebrafish reruns", 5))
        store["conf"]["search.store"] = "off"                         # off: the search of 0.5
        assert not pgstore.active() and all("ranks" not in f for f in search("CPU seconds", 3))


def test_another_model_rebuilds_it_and_a_wipe_falls_back(store, monkeypatch):
    from supagent.knowledge import embeddings as E, pgstore
    from supagent.knowledge.index import embed_pending
    from supagent.knowledge.search import search
    from supagent.security import acting_as

    store["conf"]["embed.model"] = "fake-32"
    monkeypatch.setattr(E, "embed", _fake_embed(32))
    embed_pending()                                   # every piece again with the new model (the index's hook syncs)
    st = pgstore.state(fresh=True)
    assert st["dims"] == 32 and st["model"] == "fake-32" and st["version"] == store["built"]["version"] + 1
    assert pgstore.wipe() and not pgstore.active()
    with acting_as("admin"):
        assert search("CPU seconds", 3)               # the search of 0.5 answers


def test_an_extension_upgrade_rebuilds_it(store, monkeypatch):
    from supagent.knowledge import pgstore

    real = pgstore.capabilities
    monkeypatch.setattr(pgstore, "capabilities", lambda con: {**real(con), "pg_textsearch": "9.9.9"})
    out = pgstore.sync(chats=False)
    assert out["rebuilt"]["version"] == store["built"]["version"] + 1       # pg_textsearch 0.5 -> 1.x in production
    assert pgstore.sync(chats=False).get("rebuilt") is None                  # then in step


def test_its_connections_never_stay_in_a_failed_transaction(store):
    from sqlalchemy import text
    from sqlalchemy.pool import NullPool

    from supagent.knowledge import pgstore
    from supagent.security import acting_as

    e = pgstore.engine()
    assert isinstance(e.pool, NullPool)
    with e.connect() as con:
        assert con.connection.dbapi_connection.autocommit                 # no transaction block, no ROLLBACK
        con.execute(text("SELECT id FROM " + pgstore._q(f"doc_{pgstore.state()['version']}") + " ORDER BY terms <@> "
                         "to_bm25query('cpu', " + repr(pgstore._q(f"doc_{pgstore.state()['version']}_bm25")) + ") LIMIT 1"))
        with pytest.raises(Exception):
            con.execute(text("SELECT 1/0"))          # an error after BM25: no ROLLBACK follows (pg_textsearch #247)
        assert con.execute(text("SELECT 1")).scalar() == 1
    with acting_as("admin"):
        assert pgstore.search("CPU seconds", 3)
