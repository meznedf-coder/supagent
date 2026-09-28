"""Relations measured on the data: which metric labels and index fields hold the same values
(label node = field NODE: 200 of 200 values in common), and which fields of different indices
do (join keys). Every relation keeps its evidence; the ones the data no longer supports go. One an
admin marked wrong is kept as such and never measured again (nor shown to the agent)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from superset import db

from supagent.knowledge.store import relate
from supagent.models import KObject, Relation

MIN_COMMON = 2           # values in common at least (3 when the smaller side has more than 3 values)
MIN_COVERAGE = 0.6       # share of the smaller side's values found in the other one
IGNORED_VALUES = {"", "true", "false", "0", "1", "none", "null", "unknown", "n/a", "-"}


def _values(obj: KObject) -> set[str]:
    vals = (obj.stats or {}).get("values") or []
    return {str(v) for v in vals if str(v).strip().lower() not in IGNORED_VALUES}


def learn_relations() -> dict[str, int]:
    """Recompute the value-overlap relations over every source."""
    labels = [o for o in db.session.query(KObject).filter(KObject.kind == "label", KObject.gone_at.is_(None))
              if o.name not in ("le", "quantile", "__name__")]
    fields = [o for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.gone_at.is_(None))]
    # one representative label object per (source, label name): metrics share their labels
    by_label: dict[tuple[int, str], KObject] = {}
    label_values: dict[tuple[int, str], set[str]] = defaultdict(set)
    for o in labels:
        key = (o.source_id, o.name)
        label_values[key] |= _values(o)
        by_label.setdefault(key, o)
    candidates: list[tuple[KObject, set[str]]] = [(by_label[k], v) for k, v in label_values.items() if v]
    candidates += [(f, _values(f)) for f in fields if _values(f)]
    index: dict[str, list[int]] = defaultdict(list)
    for i, (_obj, vals) in enumerate(candidates):
        for v in vals:
            index[v].append(i)
    common: dict[tuple[int, int], int] = defaultdict(int)
    for members in index.values():
        if len(members) > 50:                     # a value everybody has says nothing
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                common[(members[x], members[y])] += 1
    kept: set[int] = set()
    rejected = {(r.a_id, r.b_id) for r in db.session.query(Relation).filter(
        Relation.relation == "same_values", Relation.rejected_at.isnot(None))}
    made = skipped = 0
    for (i, j), n in common.items():
        a, va = candidates[i]
        b, vb = candidates[j]
        if a.kind == "label" and b.kind == "label" and a.name == b.name:
            continue                              # the same label in two databases
        if a.kind == "field" and b.kind == "field" and a.parent == b.parent and a.source_id == b.source_id:
            continue                              # two fields of one index
        smaller = min(len(va), len(vb))
        need = MIN_COMMON if smaller <= 3 else 3
        coverage = n / smaller if smaller else 0
        if n < need or coverage < MIN_COVERAGE:
            continue
        if a.id > b.id:                           # relate() keeps the smaller id first: same for the evidence
            a, b, va, vb = b, a, vb, va
        if (a.id, b.id) in rejected:              # an admin said it is wrong: never again
            skipped += 1
            continue
        sample = sorted(va & vb)[:6]
        rel = relate(a, b, "same_values", {"a_values": len(va), "b_values": len(vb), "common": n,
                                           "coverage": round(coverage, 3), "examples": sample}, coverage)
        db.session.flush()
        kept.add(rel.id)
        made += 1
    removed = 0
    for rel in db.session.query(Relation).filter_by(relation="same_values", origin="learned"):
        if rel.id not in kept and rel.rejected_at is None:
            db.session.delete(rel)
            removed += 1
    db.session.commit()
    return {"same_values": made, "removed": removed, "rejected": skipped}


def relations_of(obj_ids: list[int]) -> list[tuple[Relation, Any, Any]]:
    """Relations touching these objects, with both ends."""
    if not obj_ids:
        return []
    rels = (db.session.query(Relation).filter((Relation.a_id.in_(obj_ids)) | (Relation.b_id.in_(obj_ids)))
            .filter(Relation.rejected_at.is_(None)).all())
    ids = {r.a_id for r in rels} | {r.b_id for r in rels}
    objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ids))} if ids else {}
    return [(r, objs.get(r.a_id), objs.get(r.b_id)) for r in rels]
