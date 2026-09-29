"""The Context: the system's functional and technical documentation, built every night from what the
team shares. Facts pages need no LLM; summary pages are written by the LLM only when their sources
changed (a second build with nothing new asks nothing), at most context.max_llm_calls per build; a
person's edit is never written over; each page is shown only to the users who may query all its
databases, in the Data dictionary and in the agent's knowledge search."""

from __future__ import annotations

import json

import pytest

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


class FakeLLM:
    calls = 0

    def __init__(self, *a, **k):
        self.last_usage = None

    def chat(self, messages, tools=None, max_tokens=None):
        type(self).calls += 1
        items = json.loads(messages[1]["content"])
        self.last_usage = {"prompt_tokens": 1000, "completion_tokens": 200}
        title = messages[0]["content"].split("The page: ")[1].split(".")[0]
        return {"content": f"## {title}\n\nWritten from {len(items)} pieces of evidence [E1]."}


@pytest.fixture()
def shared(world, monkeypatch):
    """What the team shares, next to the world's two databases."""
    from superset.extensions import db, security_manager as sm

    from supagent import llm as L
    from supagent.knowledge.store import upsert
    from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Run

    upsert(world["run"], world["s_jobs"], "field", "jobs", "APPLICATION",
           {"data_type": "keyword", "stats": {"values": ["A", "7", "BILLING", "PAYROLL"]}})   # "A", "7": not names
    rows = [Doc(kind="upload", title="Runbook", status="ok", enabled=True,
                content="BILLING sends the invoices every night.\nPAYROLL waits for BILLING."),
            Entry(title="SLA", classification="glossary", content="Service level agreement.", updated_by="admin"),
            Entry(title="Durations", classification="rule", content="Give durations in minutes.", updated_by="admin"),
            Memory(scope="team", user_id=sm.find_user(username="admin").id, kind="fact", text="Jobs restart at 02:00",
                   status="active")]
    db.session.add_all(rows)
    db.session.query(Run).filter(Run.status == "running").update({"status": "done"})   # the world's learning run
    db.session.commit()
    FakeLLM.calls = 0
    monkeypatch.setattr(L, "LLM", FakeLLM)
    yield world
    db.session.query(ContextPage).delete()
    db.session.query(Run).filter(Run.kind == "context").delete()
    for r in rows:
        db.session.delete(db.session.merge(r))
    db.session.query(KObject).filter(KObject.name == "APPLICATION").delete()
    db.session.commit()


def _pages():
    from superset.extensions import db

    from supagent.models import ContextPage

    return {(p.section, p.slug): p for p in db.session.query(ContextPage)}


def test_the_table_comes_with_an_upgrade_from_schema_4(ctx):
    import sqlalchemy as sa
    from superset.extensions import db

    from supagent.models import ContextPage, Meta, create_or_upgrade

    ContextPage.__table__.drop(bind=db.engine, checkfirst=True)
    db.session.get(Meta, "schema_version").value = "4"
    db.session.commit()
    from supagent.models import SCHEMA_VERSION

    assert create_or_upgrade() == (4, SCHEMA_VERSION)
    assert "supagent_context" in sa.inspect(db.engine).get_table_names()


def test_a_build_writes_facts_and_summaries_and_the_next_one_asks_the_llm_nothing(shared):
    from supagent.knowledge.context import build_context

    out = build_context(reason="test")
    assert out["status"] == "done"
    pages = _pages()
    jobs, metrics = shared["jobs"], shared["metrics"]
    facts = pages[("technical", "data-sources-jobs")]
    assert facts.kind == "facts" and facts.database_ids == [jobs.id] and "`jobs`" in facts.content
    inventory = pages[("technical", "inventory-jobs")].content
    assert "## Applications" in inventory and "BILLING, PAYROLL" in inventory and "## Servers and hosts" in inventory
    from superset.extensions import db
    from test_knowledge import _database

    from supagent.knowledge.context import collect
    from supagent.knowledge.store import source_for

    empty_db = _database("never learned", "promagg://127.0.0.1:1/prometheus")
    empty = source_for(empty_db)                                     # a source with no object learned
    db.session.commit()
    try:
        assert empty_db.id not in collect()["databases"] and shared["jobs"].id in collect()["databases"]
    finally:
        db.session.delete(empty)
        db.session.delete(empty_db)
        db.session.commit()
    assert "Service level agreement." in pages[("functional", "glossary")].content
    rules = pages[("functional", "rules-and-facts")].content
    assert "Give durations in minutes." in rules and "Jobs restart at 02:00" in rules
    overview = pages[("functional", "overview")]
    assert overview.kind == "summary" and overview.llm_calls == 1 and overview.tokens == 1200
    assert sorted(overview.database_ids) == sorted([jobs.id, metrics.id])
    assert any(s["ref"].startswith("doc:") for s in overview.sources)       # it cites what it was given
    assert ("functional", "application-a") not in pages and ("functional", "application-7") not in pages
    app = pages[("functional", "application-billing")]
    assert app.database_ids == [jobs.id] and any(s["ref"].startswith("doc:") for s in app.sources)
    first = FakeLLM.calls
    assert first == len([p for p in pages.values() if p.kind == "summary"]) >= 3
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["summary pages (LLM)"]["llm_calls"] == first and steps["summary pages (LLM)"]["tokens"] == 1200 * first

    again = build_context(reason="test")
    assert FakeLLM.calls == first and again["status"] == "done"          # nothing changed: no LLM call
    assert {s["step"]: s for s in again["steps"]}["summary pages (LLM)"].get("unchanged") == first


def test_a_persons_edit_is_kept_and_the_budget_is_respected(shared, monkeypatch):
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge.context import build_context

    build_context(reason="test")
    page = _pages()[("functional", "overview")]
    page.content, page.author = "Our own words.", "admin"
    db.session.commit()
    from supagent.models import Doc

    db.session.query(Doc).filter(Doc.title == "Runbook").update({"content": "BILLING now runs twice a day."})
    db.session.commit()
    settings.set_value("context.max_llm_calls", 1)
    try:
        calls = FakeLLM.calls
        out = build_context(reason="test")
    finally:
        settings.set_value("context.max_llm_calls", 12)
    assert _pages()[("functional", "overview")].content == "Our own words."   # never written over
    assert FakeLLM.calls == calls + 1 and out["status"] == "partial"         # the next build goes on
    llm = {s["step"]: s for s in out["steps"]}["summary pages (LLM)"]
    assert llm["kept"] == 1 and llm["left"] >= 1


def test_each_user_sees_the_pages_of_the_databases_they_may_query(shared, app):
    from supagent.knowledge.context import build_context

    build_context(reason="test")
    with app.app_context(), app.test_client() as c:
        login(c, "alice")                                              # the jobs database only
        d = c.get("/supagent/dictionary/api/context").get_json()
        titles = {p["title"] for p in d["pages"]}
        assert {"Data source: jobs", "Inventory: jobs", "Glossary", "Application BILLING"} <= titles
        assert "Data source: metrics" not in titles and "How the system works" not in titles
        pid = next(p["id"] for p in d["pages"] if p["title"] == "Glossary")
        assert c.post(f"/supagent/dictionary/api/context/{pid}", json={"content": "x"}).status_code in (401, 403)
        assert c.post("/supagent/dictionary/api/context/build", json={}).status_code in (401, 403)
        hidden = next(p for p in _pages().values() if p.title == "Data source: metrics").id
        assert c.get(f"/supagent/dictionary/api/context/{hidden}").status_code == 404
    with app.app_context(), app.test_client() as c:
        login(c, "admin")
        assert "How the system works" in {p["title"] for p in c.get("/supagent/dictionary/api/context").get_json()["pages"]}


def test_a_page_is_shown_as_safe_html_and_admins_can_correct_it(shared, app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge.context import build_context

    build_context(reason="test")
    page = _pages()[("functional", "glossary")]
    page.content = '## Terms\n\n<script>alert(1)</script><img src=x onerror="alert(2)">**SLA**'
    db.session.commit()
    with app.app_context(), app.test_client() as c:
        login(c, "admin")
        d = c.get(f"/supagent/dictionary/api/context/{page.id}").get_json()
        assert "<script" not in d["html"] and "onerror" not in d["html"] and "<strong>SLA</strong>" in d["html"]
        r = c.post(f"/supagent/dictionary/api/context/{page.id}", json={"content": "## Terms\n\nOurs."}).get_json()
        assert r["author"] == "admin"
        started = []
        from supagent import tasks

        monkeypatch.setattr(tasks, "dispatch_context", lambda reason="manual": started.append(reason) or "thread")
        assert c.post("/supagent/dictionary/api/context/build", json={}).get_json() == {"started": "thread"}
    assert started == ["manual"]


def test_the_agent_finds_the_context_below_the_catalog_and_within_the_users_databases(shared, app):
    from flask import g
    from superset.extensions import security_manager as sm

    from supagent.knowledge.context import build_context
    from supagent.knowledge.search import search

    build_context(reason="test")
    from supagent.knowledge.index import sync

    sync()                                                            # the documents too
    with app.test_request_context():
        g.user = sm.find_user(username="alice")
        refs = [r["ref"] for r in search("BILLING invoices", k=20)]
    titles = {p.id: p.title for p in _pages().values()}
    seen = {titles[int(r.split(":")[1].split("#")[0])] for r in refs if r.startswith("context:")}
    assert "Application BILLING" in seen and "How the system works" not in seen      # all databases: not alice's
    assert refs.index(next(r for r in refs if r.startswith("doc:"))) < refs.index(
        next(r for r in refs if r.startswith("context:")))                          # the document first


def test_stop_during_a_build_and_the_nightly_schedule(shared, monkeypatch):
    import datetime as dt

    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import context as C
    from supagent.knowledge.stopping import request_stop
    from supagent.models import Run

    def stopping(spec, llm):
        request_stop()
        return {"content": "x", "sources": [], "tokens": 1}

    from supagent.knowledge import stopping as ST

    monkeypatch.setattr(C, "write_summary", stopping)
    monkeypatch.setattr(ST, "CHECK_S", 0.0)                           # the next check sees the Stop
    out = C.build_context(reason="test")
    assert out["status"] == "stopped" and any(s.get("interrupted") for s in out["steps"])
    monkeypatch.undo()
    db.session.query(Run).filter(Run.kind == "context").delete()
    db.session.commit()
    at = dt.datetime.now().replace(hour=5, minute=0)
    assert C.context_due(at) is True
    assert C.context_due(at.replace(hour=3)) is False                 # before context.hour
    settings.set_value("context.enabled", False)
    try:
        assert C.context_due(at) is False
    finally:
        settings.set_value("context.enabled", True)
    db.session.add(Run(kind="context", reason="schedule", status="done", finished_at=dt.datetime.utcnow()))
    db.session.commit()
    assert C.context_due(at) is False                                 # built today already


def test_a_big_dictionary_gives_a_short_page_with_what_matters(shared):
    """10,000 metrics: the page lists the ones people described, the answers used and the catalog names, not
    every description (the Context stays short enough for the search and the LLM)."""
    from superset.extensions import db

    from supagent.knowledge.context import MAIN_ITEMS, collect, fact_pages
    from supagent.models import Association, KObject, Recipe

    s_prom, metrics = shared["s_prom"], shared["metrics"]
    db.session.bulk_insert_mappings(KObject, [
        {"source_id": s_prom.id, "kind": "metric", "parent": "", "name": f"bulk_metric_{i:05d}",
         "description": f"An AI description of bulk metric {i}", "description_source": "llm", "category": "bulk"}
        for i in range(3000)])
    used = Association(word="queue", database_id=metrics.id, kind="metric", name="bulk_metric_02999", uses=7)
    learned = Recipe(question="queue depth", words="queue depth", tool="execute_sql", database_id=metrics.id,
                     target="bulk_metric_01500", status="confirmed", query="SELECT 1")
    db.session.add_all([used, learned])
    db.session.commit()
    try:
        ev = collect()
        page = next(p for p in fact_pages(ev) if p["slug"].startswith("data-sources-") and metrics.id in p["database_ids"])
        main = [ln for ln in page["content"].splitlines() if ln.startswith("- metric") or ln.startswith("- index")]
        assert len(main) == MAIN_ITEMS and len(page["content"]) < 12000
        names = " ".join(main[:3])
        assert "bulk_metric_01500" in names and "bulk_metric_02999" in names          # used by the answers first
        assert "and 2,9" in page["content"] and "- bulk: 3000" in page["content"]      # the rest, by domain
    finally:
        db.session.delete(used)
        db.session.delete(learned)
        db.session.query(KObject).filter(KObject.name.like("bulk_metric_%")).delete(synchronize_session=False)
        db.session.commit()


def test_mimir_tenants_are_subjects(shared):
    from superset.extensions import db

    from supagent.knowledge.context import collect, fact_pages
    from supagent.knowledge.store import upsert

    upsert(shared["run"], shared["s_prom"], "label", "node_cpu_seconds_total", "__tenant_id__",
           {"data_type": "string", "stats": {"values": ["billing-prod", "payroll-prod"]}})
    db.session.commit()
    inventory = next(p for p in fact_pages(collect()) if p["slug"].startswith("inventory-") and
                     shared["metrics"].id in p["database_ids"])["content"]
    assert "## Tenants (each usually a different application or subject)" in inventory
    assert "billing-prod, payroll-prod" in inventory


def test_links_to_another_database_are_on_a_page_of_both(shared, app):
    """A data source's page names nothing of another database; the links of two sources are a page of
    both, seen only by the users who may query both."""
    from superset.extensions import db

    from supagent.knowledge.context import build_context
    from supagent.models import KObject, Relation

    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    label = db.session.query(KObject).filter_by(kind="label", parent="node_cpu_seconds_total", name="node").one()
    rel = Relation(a_id=node.id, b_id=label.id, relation="same_values", origin="learned", confidence=0.9)
    db.session.add(rel)
    db.session.commit()
    try:
        build_context(reason="test", llm=False)
        pages = _pages()
        jobs_page = pages[("technical", "data-sources-jobs")]
        assert "node_cpu_seconds_total" not in jobs_page.content
        links = next(p for p in pages.values() if p.slug.startswith("links-"))
        assert "node_cpu_seconds_total.node" in links.content
        assert sorted(links.database_ids) == sorted([shared["jobs"].id, shared["metrics"].id])
        with app.app_context(), app.test_client() as c:
            login(c, "alice")                                           # the jobs database only
            titles = {p["title"] for p in c.get("/supagent/dictionary/api/context").get_json()["pages"]}
        assert links.title not in titles and "Data source: jobs" in titles
    finally:
        db.session.delete(db.session.merge(rel))
        db.session.commit()
