"""The curated catalog: what people wrote about the data. It always wins over what the
learner guesses.

It is kept as separate **entries**, each edited on its own (title, classification, category,
content), so that one edit never touches the rest: the glossary, one entry per index, groups
of metrics, relationships, health checks, rules for the agent, notes. Every change is kept
(supagent_entry_version) and can be restored; deleting is soft.

The structured entries (YAML) are merged into one catalog, the shape the tools read:
    glossary: {term: definition}
    indices: {index: {description, time_field, fields, relationships}}
    metrics: {database, description, tables: {metric: {...}}, label_relationships: [...]}
    checks: {name: {...}}
When two entries define the same index, metric, term or check, the most recently changed one
wins - except that a person's entry always wins over one the agent wrote -, and the conflict is
reported (`conflicts()`).

`superset supagent import-catalog catalog.yaml` splits a whole catalog into entries; the old
single document (supagent_document "catalog"), if any, is split the same way once and kept
as a backup; `export-catalog` merges the entries back into one YAML.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from typing import Any

import yaml

_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "conflicts": [], "errors": [], "stamp": ""}
_LOCK = threading.Lock()
TTL = 60.0

CLASSIFICATIONS = {
    "glossary": "Business terms: YAML  term: definition",
    "index": "One or more indices: YAML  index-name: {description, time_field, fields, relationships}",
    "metrics": "Metrics: YAML  tables: {metric: {description, unit, labels, sql, saved_metrics}}, and optionally "
               "database, description, label_relationships",
    "relationships": "How metric labels match index fields: YAML list of {label, index, field, description}",
    "checks": "Health checks (check_health): YAML  name: {expr, op, threshold, ...}",
    "rule": "A rule the agent always follows (text), e.g. 'Exclude UAT unless the user asks for it'",
    "note": "Documentation, runbooks, explanations (text or Markdown), found by the agent when relevant",
    "formula": "A calculated field (text): name = expression, and the table it applies to, e.g. "
               "'failure_rate = 100.0 * COUNT(*) FILTER (WHERE status = 'failed') / COUNT(*)'",
}
STRUCTURED = ("glossary", "index", "metrics", "relationships", "checks")
AGENT = "(agent)"          # the author of the entries the agent writes (no user name has parentheses)


class CatalogError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def invalidate() -> None:
    """After a change of the entries: read again here, and by every other process at its next
    question (the knowledge stamp)."""
    with _LOCK:
        _CACHE.update(at=0.0, data=None)
    try:
        from superset import db

        from supagent.knowledge.freshness import touch

        touch()
        db.session.commit()
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        from superset import db

        db.session.rollback()


def load_catalog() -> dict[str, Any]:
    """The merged catalog (entries; else the old single document; else SUPAGENT_CATALOG)."""
    from supagent.knowledge.freshness import stamp

    changed = stamp()
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - _CACHE["at"] < TTL and _CACHE["stamp"] == changed:
            return _CACHE["data"]
        data, found_conflicts, errors = _from_entries()
        if data is None:
            data = _from_document()
        if data is None:
            data = _from_file()
        _CACHE.update(at=time.time(), data=data, conflicts=found_conflicts, errors=errors, stamp=changed)
        return data


def conflicts() -> dict[str, list]:
    load_catalog()
    return {"conflicts": list(_CACHE["conflicts"]), "errors": list(_CACHE["errors"])}


def _entries_of(classification: str) -> list[dict[str, str]]:
    try:
        from superset import db

        from supagent.models import Entry

        rows = (db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                               Entry.classification == classification)
                .order_by(Entry.category, Entry.title).all())
    except Exception:  # pylint: disable=broad-except
        return []
    return [{"id": e.id, "title": e.title, "category": e.category or "", "text": (e.content or "").strip()}
            for e in rows if (e.content or "").strip()]


def rules() -> list[dict[str, str]]:
    """The enabled rule entries (the agent's prompt carries them)."""
    return _entries_of("rule")


def notes() -> list[dict[str, str]]:
    return _entries_of("note")


def formulas() -> list[dict[str, Any]]:
    """The enabled formula entries, with the database the agent learned each from (None: any)."""
    try:
        from superset import db

        from supagent.models import Entry

        rows = (db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                               Entry.classification == "formula")
                .order_by(Entry.category, Entry.title).all())
    except Exception:  # pylint: disable=broad-except
        return []
    return [{"title": e.title, "category": e.category or "", "text": (e.content or "").strip(),
             "database_id": (e.evidence or {}).get("database_id") if e.origin else None}
            for e in rows if (e.content or "").strip()]


def parse_entry(classification: str, fmt: str, content: str) -> Any:
    """The structured content of an entry, or CatalogError with the reason (and line)."""
    if classification not in STRUCTURED:
        return content
    try:
        data = yaml.safe_load(content or "") if (fmt or "yaml") == "yaml" else None
    except yaml.YAMLError as ex:
        mark = getattr(ex, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise CatalogError(f"invalid YAML{where}: {getattr(ex, 'problem', None) or ex}") from ex
    if data is None:
        return {} if classification != "relationships" else []
    if classification == "relationships":
        if isinstance(data, dict) and "label_relationships" in data:
            data = data["label_relationships"]
        if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
            raise CatalogError("relationships: a YAML list of {label, index, field, description}")
        return data
    if not isinstance(data, dict):
        raise CatalogError(f"{classification}: a YAML mapping is expected")
    if classification == "metrics" and "tables" not in data and data and \
            all(isinstance(v, dict) for v in data.values()):
        data = {"tables": data}                        # a bare {metric: spec} mapping
    return data


def _from_entries() -> tuple[dict[str, Any] | None, list, list]:
    try:
        from superset import db

        from supagent.models import Entry

        rows = (db.session.query(Entry).filter(Entry.deleted_at.is_(None))
                .order_by(Entry.updated_at, Entry.id).all())
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        try:
            from superset import db

            db.session.rollback()
        except Exception:  # pylint: disable=broad-except
            pass
        return None, [], []
    if not rows:
        return None, [], []
    out: dict[str, Any] = {"glossary": {}, "indices": {}, "metrics": {"tables": {}, "label_relationships": []},
                           "checks": {}}
    owner: dict[tuple[str, str], str] = {}
    found_conflicts: list[dict] = []
    errors: list[dict] = []

    def put(section: dict, kind: str, key: str, value: Any, entry: Any) -> None:
        if (kind, key) in owner and owner[(kind, key)] != entry.title:
            found_conflicts.append({"kind": kind, "name": key, "entries": [owner[(kind, key)], entry.title],
                                    "kept": entry.title})
        owner[(kind, key)] = entry.title
        section[key] = value

    rows.sort(key=lambda e: e.updated_by != AGENT)     # the agent's first: a person's entry always wins
    for e in rows:                                      # oldest first: the most recent change wins
        if not e.enabled or e.classification not in STRUCTURED:
            continue
        try:
            data = parse_entry(e.classification, e.fmt, e.content or "")
        except CatalogError as ex:
            errors.append({"entry": e.title, "id": e.id, "error": str(ex)})
            continue
        if e.classification == "glossary":
            for k, v in data.items():
                put(out["glossary"], "term", str(k), v, e)
        elif e.classification == "index":
            for k, v in data.items():
                put(out["indices"], "index", str(k), v or {}, e)
        elif e.classification == "checks":
            for k, v in data.items():
                put(out["checks"], "check", str(k), v, e)
        elif e.classification == "relationships":
            out["metrics"]["label_relationships"] += data
        elif e.classification == "metrics":
            for k, v in (data.get("tables") or {}).items():
                put(out["metrics"]["tables"], "metric", str(k), v or {}, e)
            out["metrics"]["label_relationships"] += list(data.get("label_relationships") or [])
            for key in ("database", "description"):
                if data.get(key):
                    out["metrics"][key] = data[key]
    return out, found_conflicts, errors


def _from_document() -> dict[str, Any] | None:
    try:
        from superset import db

        from supagent.models import Document

        doc = db.session.get(Document, "catalog")
    except Exception:  # pylint: disable=broad-except
        try:
            from superset import db

            db.session.rollback()
        except Exception:  # pylint: disable=broad-except
            pass
        return None
    if doc is None or not doc.content:
        return None
    return yaml.safe_load(doc.content) or {}


def _from_file() -> dict[str, Any]:
    path = os.path.expanduser(os.environ.get("SUPAGENT_CATALOG") or os.environ.get("CATALOG") or "")
    if not path or not os.path.exists(path):
        return {"indices": {}, "glossary": {}}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def _prefix(metric: str) -> str:
    return metric.split("_", 1)[0] if "_" in metric else metric


def split_catalog(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A whole catalog -> entries: glossary, one per index, metrics grouped by name prefix
    (node_*, batch_*...), relationships, checks, the rest as a note."""
    out: list[dict[str, Any]] = []
    if data.get("glossary"):
        out.append({"title": "Glossary", "classification": "glossary", "category": "Business",
                    "content": _dump(data["glossary"])})
    for name, spec in (data.get("indices") or {}).items():
        out.append({"title": f"Index {name}", "classification": "index", "category": "Indices",
                    "content": _dump({name: spec})})
    metrics = data.get("metrics") or {}
    tables = metrics.get("tables") or {}
    head = {k: metrics[k] for k in ("database", "description") if metrics.get(k)}
    groups: dict[str, dict] = {}
    for name, spec in tables.items():
        groups.setdefault(_prefix(name), {})[name] = spec
    for i, (prefix, group) in enumerate(sorted(groups.items())):
        body = dict(head) if i == 0 else {}
        body["tables"] = group
        out.append({"title": f"Metrics {prefix}_*", "classification": "metrics", "category": "Metrics",
                    "content": _dump(body)})
    if head and not groups:
        out.append({"title": "Metrics", "classification": "metrics", "category": "Metrics", "content": _dump(head)})
    if metrics.get("label_relationships"):
        out.append({"title": "Metric labels and index fields", "classification": "relationships",
                    "category": "Relationships", "content": _dump(metrics["label_relationships"])})
    if data.get("checks"):
        out.append({"title": "Health checks", "classification": "checks", "category": "Checks",
                    "content": _dump(data["checks"])})
    rest = {k: v for k, v in data.items() if k not in ("glossary", "indices", "metrics", "checks")}
    if rest:
        out.append({"title": "Other catalog keys", "classification": "note", "category": "Other",
                    "fmt": "yaml", "content": _dump(rest)})
    return out


def _record(e: Any, by: str, deleted: bool = False) -> None:
    from superset import db

    from supagent.models import EntryVersion

    db.session.add(EntryVersion(entry_id=e.id, version=e.version, title=e.title, classification=e.classification,
                                category=e.category, fmt=e.fmt, content=e.content, enabled=e.enabled,
                                deleted=deleted, changed_by=by))


def save_entry(values: dict[str, Any], by: str, entry_id: int | None = None,
               expected_version: int | None = None, origin: str | None = None,
               evidence: dict[str, Any] | None = None) -> Any:
    """Create or change one entry (checked first; a stale version is refused). `origin` and
    `evidence`: an entry the agent writes."""
    from superset import db

    from supagent.models import Entry

    title = str(values.get("title") or "").strip()
    classification = str(values.get("classification") or "").strip()
    if not title:
        raise CatalogError("a title is required")
    if classification not in CLASSIFICATIONS:
        raise CatalogError(f"classification: one of {', '.join(CLASSIFICATIONS)}")
    fmt = str(values.get("fmt") or ("yaml" if classification in STRUCTURED else "text"))
    content = str(values.get("content") or "")
    parse_entry(classification, fmt, content)             # refuse a broken entry before saving it
    _refuse_copy(title, classification, content, entry_id)
    if entry_id is None:
        e = Entry(title=title[:255], classification=classification, created_by=by, version=1)
        db.session.add(e)
    else:
        e = db.session.get(Entry, entry_id)
        if e is None or e.deleted_at is not None:
            raise CatalogError("entry not found")
        if expected_version is not None and int(expected_version) != e.version:
            raise CatalogError(f"the entry changed meanwhile (version {e.version} by {e.updated_by}): reload it")
        e.version = (e.version or 1) + 1
        e.title = title[:255]
        e.classification = classification
    e.category = (str(values.get("category") or "").strip() or None)
    e.fmt = fmt
    e.content = content
    e.enabled = bool(values.get("enabled", True))
    e.updated_by = by
    e.updated_at = dt.datetime.utcnow()
    if origin is not None:
        e.origin, e.evidence = origin, evidence
    db.session.flush()
    _record(e, by)
    db.session.commit()
    invalidate()
    return e


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _refuse_copy(title: str, classification: str, content: str, entry_id: int | None) -> None:
    """No second entry of the same title, nor of the same classification and content: edit that one."""
    from superset import db

    from supagent.models import Entry

    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None)):
        if e.id == entry_id:
            continue
        if _norm(e.title) == _norm(title):
            raise CatalogError(f"an entry is already named {e.title!r} (#{e.id}): edit it, or choose another title")
        if e.classification == classification and _norm(e.content) and _norm(e.content) == _norm(content):
            raise CatalogError(f"the entry {e.title!r} (#{e.id}) already says exactly this: edit it instead")


def delete_entry(entry_id: int, by: str, expected_version: int | None = None) -> None:
    from superset import db

    from supagent.models import Entry

    e = db.session.get(Entry, entry_id)
    if e is None or e.deleted_at is not None:
        raise CatalogError("entry not found")
    if expected_version is not None and int(expected_version) != e.version:
        raise CatalogError(f"the entry changed meanwhile (version {e.version} by {e.updated_by}): reload it")
    e.deleted_at = dt.datetime.utcnow()
    e.version = (e.version or 1) + 1
    e.updated_by = by
    _record(e, by, deleted=True)
    db.session.commit()
    invalidate()


def restore_entry(entry_id: int, version: int, by: str) -> Any:
    """Back to one of its versions (also undeletes it)."""
    from superset import db

    from supagent.models import Entry, EntryVersion

    e = db.session.get(Entry, entry_id)
    v = db.session.query(EntryVersion).filter_by(entry_id=entry_id, version=version, deleted=False).first()
    if e is None or v is None:
        raise CatalogError("version not found")
    e.deleted_at = None
    e.title, e.classification, e.category, e.fmt, e.content, e.enabled = (
        v.title, v.classification, v.category, v.fmt, v.content, v.enabled)
    e.version = (e.version or 1) + 1
    e.updated_by = by
    e.updated_at = dt.datetime.utcnow()
    _record(e, by)
    db.session.commit()
    invalidate()
    return e


def import_catalog(text: str, by: str = "", mode: str = "merge") -> dict[str, int]:
    """A whole catalog (YAML) split into entries. merge: entries of the same title are
    updated, others added; replace: the structured entries not in the file are deleted too."""
    from superset import db

    from supagent.models import Entry

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as ex:
        raise CatalogError(f"invalid YAML: {ex}") from ex
    if not isinstance(data, dict):
        raise CatalogError("the catalog must be a YAML mapping")
    parts = split_catalog(data)
    existing = {e.title: e for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None))}
    counts = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    for part in parts:
        old = existing.get(part["title"])
        if old is None:
            save_entry(part, by)
            counts["added"] += 1
        elif (old.content or "").strip() != part["content"].strip() or old.classification != part["classification"]:
            save_entry({**part, "category": old.category or part["category"], "enabled": old.enabled}, by,
                       entry_id=old.id)
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    if mode == "replace":
        titles = {p["title"] for p in parts}
        for title, e in existing.items():
            if e.classification in STRUCTURED and title not in titles:
                delete_entry(e.id, by)
                counts["deleted"] += 1
    metrics = (data.get("metrics") or {}).get("tables") or {}
    counts.update(indices=len(data.get("indices") or {}), metrics=len(metrics),
                  checks=len(data.get("checks") or {}), glossary=len(data.get("glossary") or {}))
    invalidate()
    return counts


def export_catalog() -> str:
    """The merged catalog as one YAML file (what import-catalog reads)."""
    data = {k: v for k, v in load_catalog().items()}
    metrics = dict(data.get("metrics") or {})
    if not metrics.get("label_relationships"):
        metrics.pop("label_relationships", None)
    if metrics.get("tables") or len(metrics) > 1:
        data["metrics"] = metrics
    else:
        data.pop("metrics", None)
    for key in ("glossary", "indices", "checks"):
        if not data.get(key):
            data.pop(key, None)
    return _dump(data)


def migrate_document(by: str = "migration") -> dict[str, Any] | None:
    """The single catalog document of 0.1 -> entries, once (the document is kept as a backup)."""
    from superset import db

    from supagent.models import Document, Entry

    if db.session.query(Entry.id).first() is not None:
        return None
    doc = db.session.get(Document, "catalog")
    if doc is None or not (doc.content or "").strip():
        return None
    stamp = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    db.session.merge(Document(key=f"catalog-backup-{stamp}", content=doc.content, updated_by=by))
    db.session.commit()
    counts = import_catalog(doc.content, by=by)
    db.session.delete(db.session.get(Document, "catalog"))
    db.session.commit()
    invalidate()
    return counts
