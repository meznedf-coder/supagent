"""What people wrote (the catalog) applied to the learned objects: descriptions, units,
synonyms and relationships written by people always win and are marked verified."""

from __future__ import annotations

from typing import Any

from superset import db

from supagent.knowledge.catalog import load_catalog
from supagent.knowledge.store import relate
from supagent.models import KObject, Source


def _objects(kind: str, name: str, parent: str | None = None) -> list[KObject]:
    q = db.session.query(KObject).filter(KObject.kind == kind, KObject.name == name)
    if parent is not None:
        q = q.filter(KObject.parent == parent)
    return q.all()


def _curate(obj: KObject, description: str | None, unit: str | None = None, synonyms: Any = None,
            category: str | None = None) -> bool:
    changed = False
    if description and (obj.description != description.strip() or obj.description_source != "curated"):
        obj.description = description.strip()
        obj.description_source = "curated"
        obj.verified = True
        changed = True
    if unit and obj.unit != unit:
        obj.unit = unit
        changed = True
    if synonyms:
        syn = [str(s) for s in (synonyms if isinstance(synonyms, list) else [synonyms])]
        if obj.synonyms != syn:
            obj.synonyms = syn
            changed = True
    if category and obj.category != category:
        obj.category = category
        changed = True
    return changed


def apply_catalog() -> dict[str, int]:
    cat = load_catalog()
    out = {"curated": 0, "relations": 0}
    for index, spec in (cat.get("indices") or {}).items():
        for obj in _objects("index", index):
            out["curated"] += _curate(obj, spec.get("description"))
        for fname, fspec in (spec.get("fields") or {}).items():
            fspec = fspec if isinstance(fspec, dict) else {"description": str(fspec)}
            for obj in _objects("field", fname, index):
                out["curated"] += _curate(obj, fspec.get("description") or fspec.get("label"),
                                          fspec.get("unit"), fspec.get("synonyms"))
        for rel in spec.get("relationships") or []:
            for a_field, b_field in (rel.get("keys") or {}).items():
                for a in _objects("field", a_field, index):
                    for b in _objects("field", b_field, rel.get("to", "")):
                        relate(a, b, "curated", {"description": rel.get("description", ""), "join": True}, 1.0,
                               origin="curated")
                        out["relations"] += 1
    metrics = cat.get("metrics") or {}
    for name, spec in (metrics.get("tables") or {}).items():
        spec = spec or {}
        for obj in _objects("metric", name):
            out["curated"] += _curate(obj, spec.get("description"), spec.get("unit"), spec.get("synonyms"),
                                      spec.get("category"))
        for label, text in (spec.get("labels") or {}).items():
            for obj in _objects("label", label, name):
                out["curated"] += _curate(obj, str(text))
    for rel in metrics.get("label_relationships") or []:
        fields = _objects("field", rel.get("field", ""), rel.get("index", ""))
        labels = _objects("label", rel.get("label", ""))
        seen_sources: set[int] = set()
        for lb in labels:                          # one label object per metrics source is enough
            if lb.source_id in seen_sources:
                continue
            seen_sources.add(lb.source_id)
            for f in fields:
                relate(lb, f, "curated", {"description": rel.get("description", ""), "label": lb.name,
                                          "field": f.name, "index": f.parent}, 1.0, origin="curated")
                out["relations"] += 1
    db.session.commit()
    return out


def sources_of_user() -> list[Source]:
    """The learned sources whose database the current user may query."""
    from superset.extensions import db as sdb, security_manager
    from superset.models.core import Database

    out = []
    for src in sdb.session.query(Source):
        database = sdb.session.get(Database, src.database_id)
        if database is not None and security_manager.can_access_database(database):
            out.append(src)
    return out
