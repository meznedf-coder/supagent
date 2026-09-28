"""Writing what the learner found: objects (upsert with change detection) and relations."""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import re
from typing import Any

from superset import db

from supagent.models import Change, KObject, Relation, Run, Source

UNIT_SUFFIXES = [("_seconds_total", "seconds"), ("_seconds", "seconds"), ("_milliseconds", "milliseconds"),
                 ("_bytes_total", "bytes"), ("_bytes", "bytes"), ("_ratio", "ratio (0-1)"), ("_percent", "%"),
                 ("_celsius", "degrees Celsius"), ("_volts", "volts"), ("_joules", "joules"), ("_watts", "watts"),
                 ("_meters", "meters"), ("_hertz", "hertz"), ("_total", "count"), ("_count", "count")]


def unit_from_name(name: str) -> str:
    for suffix, unit in UNIT_SUFFIXES:
        if name.endswith(suffix):
            return unit
    return ""


def matches(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatchcase(name, p) for p in include):
        return False
    return not any(fnmatch.fnmatchcase(name, p) for p in exclude)


def fingerprint(facts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def source_for(database: Any) -> Source:
    src = db.session.query(Source).filter_by(database_id=database.id).one_or_none()
    if src is None:
        src = Source(database_id=database.id)
        db.session.add(src)
    src.database_name = database.database_name
    src.backend = database.backend
    db.session.flush()
    return src


def record(run: Run | None, obj: KObject, change: str, detail: dict | None = None) -> None:
    if run is not None:
        db.session.add(Change(run_id=run.id, object_id=obj.id, change=change, detail=detail or {}))


def _ratio_changed(old: Any, new: Any) -> bool:
    try:
        old, new = float(old), float(new)
    except (TypeError, ValueError):
        return False
    if old <= 0 or new <= 0:
        return old != new and max(old, new) >= 10
    return max(old, new) / min(old, new) >= 2 and abs(old - new) >= 10


def upsert(run: Run | None, source: Source, kind: str, parent: str, name: str, facts: dict[str, Any]) -> KObject:
    """Create or refresh one object; the differences from the previous run become changes.
    Descriptions written by people (curated) or by the LLM are never overwritten here."""
    obj = (db.session.query(KObject)
           .filter_by(source_id=source.id, kind=kind, parent=parent or "", name=name).one_or_none())
    now = _now()
    if obj is None:
        obj = KObject(source_id=source.id, kind=kind, parent=parent or "", name=name, first_seen=now)
        db.session.add(obj)
        db.session.flush()
        if source.last_learned_at is not None:     # the first run of a source finds everything new
            record(run, obj, "new", {k: facts.get(k) for k in ("metric_type", "data_type", "unit") if facts.get(k)})
    else:
        if obj.gone_at is not None:
            record(run, obj, "back", {"gone_since": obj.gone_at.isoformat()})
        for key in ("metric_type", "data_type", "unit"):
            if facts.get(key) and getattr(obj, key) and getattr(obj, key) != facts[key]:
                record(run, obj, "type" if key != "unit" else "unit", {"from": getattr(obj, key), "to": facts[key]})
        old_stats, new_stats = obj.stats or {}, facts.get("stats") or {}
        for key in ("series", "cardinality", "docs"):
            if key in old_stats and key in new_stats and _ratio_changed(old_stats[key], new_stats[key]):
                record(run, obj, "cardinality", {"what": key, "from": old_stats[key], "to": new_stats[key]})
    for key in ("data_type", "metric_type", "unit", "backend_help"):
        if facts.get(key) is not None:
            setattr(obj, key, facts[key])
    if "stats" in facts:
        obj.stats = facts["stats"]
    if facts.get("backend_help") and obj.description_source in (None, "", "backend"):
        obj.description = facts["backend_help"]
        obj.description_source = "backend"
    obj.last_seen = now
    obj.gone_at = None
    obj.fingerprint = fingerprint({k: facts.get(k) for k in ("data_type", "metric_type", "unit", "backend_help")})
    return obj


def mark_gone(run: Run | None, source: Source, kind: str, seen: set[tuple[str, str]]) -> int:
    """Objects of a source that the run did not see (a complete listing only)."""
    n = 0
    for obj in db.session.query(KObject).filter_by(source_id=source.id, kind=kind).filter(KObject.gone_at.is_(None)):
        if (obj.parent or "", obj.name) not in seen:
            obj.gone_at = _now()
            record(run, obj, "gone")
            n += 1
    return n


def relate(a: KObject, b: KObject, relation: str, evidence: dict[str, Any], confidence: float,
           origin: str = "learned") -> Relation:
    if a.id > b.id and relation != "curated":
        a, b = b, a
    rel = db.session.query(Relation).filter_by(a_id=a.id, b_id=b.id, relation=relation).one_or_none()
    if rel is None:
        rel = Relation(a_id=a.id, b_id=b.id, relation=relation)
        db.session.add(rel)
    rel.evidence = evidence
    rel.confidence = round(float(confidence), 3)
    rel.origin = origin
    return rel


def words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 1}
