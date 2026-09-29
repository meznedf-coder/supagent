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


def catalog_texts() -> dict[tuple[str, str, str], dict[str, Any]]:
    """(kind, parent, name) -> the description and synonyms the catalog gives that object (what
    apply_catalog writes again at every run)."""
    cat = load_catalog()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, spec in (cat.get("indices") or {}).items():
        out[("index", "", index)] = {"description": spec.get("description")}
        for fname, fspec in (spec.get("fields") or {}).items():
            fspec = fspec if isinstance(fspec, dict) else {"description": str(fspec)}
            out[("field", index, fname)] = {"description": fspec.get("description") or fspec.get("label"),
                                            "synonyms": fspec.get("synonyms")}
    for name, spec in ((cat.get("metrics") or {}).get("tables") or {}).items():
        spec = spec or {}
        out[("metric", "", name)] = {"description": spec.get("description"), "synonyms": spec.get("synonyms")}
        for label, text in (spec.get("labels") or {}).items():
            out[("label", name, label)] = {"description": str(text)}
    return out


def apply_catalog(changed: set[int] | None = None) -> dict[str, int]:
    """The catalog's descriptions, units, synonyms and relationships written on the dictionary's
    objects (`changed` gets the ids of the objects it changed)."""
    cat = load_catalog()
    out = {"curated": 0, "relations": 0}
    changed = set() if changed is None else changed

    def curate(obj: KObject, *args: Any) -> int:
        if _curate(obj, *args):
            changed.add(obj.id)
            return 1
        return 0

    for index, spec in (cat.get("indices") or {}).items():
        for obj in _objects("index", index):
            out["curated"] += curate(obj, spec.get("description"))
        for fname, fspec in (spec.get("fields") or {}).items():
            fspec = fspec if isinstance(fspec, dict) else {"description": str(fspec)}
            for obj in _objects("field", fname, index):
                out["curated"] += curate(obj, fspec.get("description") or fspec.get("label"),
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
            out["curated"] += curate(obj, spec.get("description"), spec.get("unit"), spec.get("synonyms"),
                                     spec.get("category"))
        for label, text in (spec.get("labels") or {}).items():
            for obj in _objects("label", label, name):
                out["curated"] += curate(obj, str(text))
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
    if out["curated"]:
        from supagent.knowledge.freshness import touch

        touch()                                    # every process: the new descriptions at once
    db.session.commit()
    return out


def release(before: dict[tuple[str, str, str], dict[str, Any]]) -> set[int]:
    """The descriptions the catalog gave (`before`, catalog_texts() before a change) and no longer
    gives: taken back from the objects (the learner describes them again), unless a person wrote
    another one since. Returns the ids of the objects changed."""
    now = catalog_texts()
    out: set[int] = set()
    for (kind, parent, name), spec in before.items():
        old = str((spec or {}).get("description") or "").strip()
        if not old or str((now.get((kind, parent, name)) or {}).get("description") or "").strip():
            continue
        for obj in _objects(kind, name, parent if kind in ("field", "label") else None):
            if obj.description_source == "curated" and (obj.description or "").strip() == old:
                obj.description, obj.description_source, obj.verified = None, None, False
                out.add(obj.id)
    if out:
        from supagent.knowledge.freshness import touch

        touch()
    db.session.commit()
    return out


def after_change(before: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    """After a change of the catalog (an entry saved, deleted, restored, a catalog imported): its
    descriptions on the dictionary, the ones it no longer gives taken back, and the searchable
    pieces of the entries and of the objects concerned made again at once."""
    from supagent.knowledge.index import sync, sync_objects

    changed: set[int] = set()
    out: dict[str, Any] = apply_catalog(changed)
    released = release(before)
    out["released"] = len(released)
    pieces = sync(("entry:",))
    if changed | released:
        more = sync_objects(changed | released)
        pieces = {k: pieces.get(k, 0) + more.get(k, 0) for k in set(pieces) | set(more)}
    out["pieces"] = pieces
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
