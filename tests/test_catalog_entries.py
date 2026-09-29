"""The catalog as separate entries: split, merge, conflicts, versions, restore, migration."""

from __future__ import annotations

import pytest
import yaml

CATALOG = """
glossary:
  failed job: STATUS = 'FAILED'
indices:
  jobs:
    description: One document per run
    fields: {NODE: {description: Server of the run}}
  apps:
    description: Applications
metrics:
  database: metrics
  description: Server metrics
  tables:
    node_cpu_seconds_total: {description: CPU time}
    node_load1: {description: Load}
    batch_jobs_total: {description: Jobs}
  label_relationships:
    - {label: node, index: jobs, field: NODE}
checks:
  cpu: {expr: 'x', op: '>', threshold: 90}
"""


@pytest.fixture()
def clean(ctx):
    from superset.extensions import db

    from supagent.knowledge.catalog import invalidate
    from supagent.models import Document, Entry, EntryVersion

    db.session.query(EntryVersion).delete()
    db.session.query(Entry).delete()
    db.session.query(Document).delete()
    db.session.commit()
    invalidate()
    yield
    db.session.query(EntryVersion).delete()
    db.session.query(Entry).delete()
    db.session.commit()
    invalidate()


def test_a_catalog_is_split_and_merged_back(clean):
    from supagent.knowledge.catalog import export_catalog, import_catalog, load_catalog
    from supagent.models import Entry
    from superset.extensions import db

    counts = import_catalog(CATALOG, by="t")
    titles = sorted(e.title for e in db.session.query(Entry))
    assert titles == ["Glossary", "Health checks", "Index apps", "Index jobs", "Metric labels and index fields",
                      "Metrics batch_*", "Metrics node_*"]
    assert counts["added"] == 7
    data = load_catalog()
    assert data["indices"]["jobs"]["fields"]["NODE"]["description"] == "Server of the run"
    assert set(data["metrics"]["tables"]) == {"node_cpu_seconds_total", "node_load1", "batch_jobs_total"}
    assert data["metrics"]["database"] == "metrics" and data["metrics"]["label_relationships"][0]["label"] == "node"
    back = yaml.safe_load(export_catalog())
    assert back["checks"]["cpu"]["threshold"] == 90 and back["glossary"]["failed job"]
    assert import_catalog(CATALOG, by="t")["unchanged"] == 7          # importing again changes nothing


def test_entries_are_checked_versioned_and_restorable(clean):
    from supagent.knowledge.catalog import (CatalogError, delete_entry, load_catalog, restore_entry, rules,
                                            save_entry)

    e = save_entry({"title": "Jobs", "classification": "index", "content": "jobs: {description: v1}"}, by="alice")
    with pytest.raises(CatalogError, match="line"):
        save_entry({"title": "Broken", "classification": "index", "content": "jobs: [unclosed"}, by="alice")
    save_entry({"title": "Jobs", "classification": "index", "content": "jobs: {description: v2}"}, by="bob",
               entry_id=e.id, expected_version=1)
    with pytest.raises(CatalogError, match="changed meanwhile"):              # alice's page is stale
        save_entry({"title": "Jobs", "classification": "index", "content": "jobs: {description: v3}"}, by="alice",
                   entry_id=e.id, expected_version=1)
    assert load_catalog()["indices"]["jobs"]["description"] == "v2"
    delete_entry(e.id, by="bob")
    assert "jobs" not in (load_catalog().get("indices") or {})
    restore_entry(e.id, 1, by="alice")                                          # back to v1, undeleted
    assert load_catalog()["indices"]["jobs"]["description"] == "v1"
    save_entry({"title": "No UAT", "classification": "rule", "content": "Exclude UAT unless asked."}, by="alice")
    assert [{k: v for k, v in r.items() if k != "id"} for r in rules()] == [
        {"title": "No UAT", "category": "", "text": "Exclude UAT unless asked."}]


def test_two_entries_for_one_index_are_reported(clean):
    from supagent.knowledge.catalog import conflicts, load_catalog, save_entry

    save_entry({"title": "Jobs A", "classification": "index", "content": "jobs: {description: from A}"}, by="t")
    save_entry({"title": "Jobs B", "classification": "index", "content": "jobs: {description: from B}"}, by="t")
    assert load_catalog()["indices"]["jobs"]["description"] == "from B"         # the most recent change
    found = conflicts()["conflicts"]
    assert found and found[0]["name"] == "jobs" and found[0]["kept"] == "Jobs B"


def test_the_old_single_document_is_migrated_once(clean):
    from superset.extensions import db

    from supagent.knowledge.catalog import load_catalog, migrate_document
    from supagent.models import Document

    db.session.add(Document(key="catalog", content=CATALOG, updated_by="0.1"))
    db.session.commit()
    assert migrate_document()["added"] == 7
    assert migrate_document() is None                                           # once
    assert db.session.get(Document, "catalog") is None
    assert any(d.key.startswith("catalog-backup-") for d in db.session.query(Document))
    assert "jobs" in load_catalog()["indices"]


def test_the_entries_api_is_for_admins(app):
    from conftest import login

    with app.test_client() as c:
        login(c, "alice")
        assert c.get("/supagent/admin/api/entries").status_code in (401, 403)
    with app.test_client() as c:
        login(c, "admin")
        r = c.post("/supagent/admin/api/entries", json={"title": "Glossary API", "classification": "glossary",
                                                        "category": "Business", "content": "D-1: the day before"})
        assert r.status_code == 200, r.get_json()
        entry = r.get_json()["entry"]
        bad = c.put(f"/supagent/admin/api/entries/{entry['id']}", json={**entry, "content": "x: [", "version": 1})
        assert bad.status_code == 400 and "YAML" in bad.get_json()["error"]
        listed = c.get("/supagent/admin/api/entries").get_json()
        assert any(e["title"] == "Glossary API" for e in listed["entries"]) and "rule" in listed["classifications"]
        assert c.delete(f"/supagent/admin/api/entries/{entry['id']}?version=1").status_code == 200
        hist = c.get(f"/supagent/admin/api/entries/{entry['id']}/history").get_json()["versions"]
        assert [v["deleted"] for v in hist] == [True, False]
        assert c.get("/supagent/admin/api/catalog/export").status_code == 200
