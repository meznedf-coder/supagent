"""Relations measured on the data: which metric labels and index fields hold the same values
(label node = field NODE: 200 of 200 values in common), and which fields of different indices
do (join keys). Every relation keeps its evidence; the ones the data no longer supports go. One an
admin marked wrong is kept as such and never measured again (nor shown to the agent)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from superset import db

from supagent.models import KObject, Relation

MIN_COMMON = 2           # values in common at least (3 when the smaller side has more than 3 values)
MIN_COVERAGE = 0.6       # share of the smaller side's values found in the other one
IGNORED_VALUES = {"", "true", "false", "0", "1", "none", "null", "unknown", "n/a", "-"}


def _values(obj: KObject) -> set[str]:
    vals = (obj.stats or {}).get("values") or []
    return {str(v) for v in vals if str(v).strip().lower() not in IGNORED_VALUES}


BATCH = 100              # relations written per commit
READ = 1000              # objects read at a time (their statistics hold up to 1000 values each)


def _stream(kind: str) -> Any:
    """(id, source id, name, parent, values) of the live labels or fields, READ rows at a time:
    thousands of metrics have tens of thousands of labels, never all loaded at once."""
    q = (db.session.query(KObject.id, KObject.source_id, KObject.name, KObject.parent, KObject.stats)
         .filter(KObject.kind == kind, KObject.gone_at.is_(None)).order_by(KObject.id))
    if kind == "label":
        q = q.filter(KObject.name.notin_(("le", "quantile", "__name__")))
    for oid, sid, name, parent, stats in q.yield_per(READ):
        vals = (stats or {}).get("values") or []
        yield oid, sid, name, parent or "", {str(v) for v in vals if str(v).strip().lower() not in IGNORED_VALUES}


def _end(c: tuple) -> tuple:
    """What an end of a relation stands for: a label stands for its name in its database (every
    metric that has it), a field for itself."""
    return ("label", c[2], c[3]) if c[1] == "label" else ("id", c[0])


def _wrong_pairs(rejected: list[Relation]) -> set[frozenset]:
    """The pairs admins marked Wrong, by what their ends stand for: a Wrong mark made on one
    metric's label holds for the same label name of every metric of that database."""
    ids = {r.a_id for r in rejected} | {r.b_id for r in rejected}
    if not ids:
        return set()
    objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(list(ids)))}

    def end(oid: int) -> tuple:
        o = objs.get(oid)
        return ("label", o.source_id, o.name) if o is not None and o.kind == "label" else ("id", oid)

    return {frozenset((end(r.a_id), end(r.b_id))) for r in rejected}


def learn_relations() -> dict[str, int]:
    """Recompute the value-overlap relations over every source: the objects are read in steps,
    one label per database and name stands for all the metrics that have it, and the relations
    are written BATCH at a time against the ones already known (read once)."""
    # one representative label per (source, label name), with the values of all its metrics
    rep: dict[tuple[int, str], int] = {}
    label_values: dict[tuple[int, str], set[str]] = defaultdict(set)
    for oid, sid, name, _parent, vals in _stream("label"):
        key = (sid, name)
        rep.setdefault(key, oid)
        label_values[key] |= vals
    # candidates: (id, kind, source, name, parent, values)
    cands: list[tuple[int, str, int, str, str, set[str]]] = [
        (rep[k], "label", k[0], k[1], "", v) for k, v in label_values.items() if v]
    cands += [(oid, "field", sid, name, parent, vals) for oid, sid, name, parent, vals in _stream("field") if vals]
    index: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(cands):
        for v in c[5]:
            index[v].append(i)
    common: dict[tuple[int, int], int] = defaultdict(int)
    for members in index.values():
        if len(members) > 50:                     # a value everybody has says nothing
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                common[(members[x], members[y])] += 1
    existing = {(r.a_id, r.b_id): r for r in db.session.query(Relation).filter(Relation.relation == "same_values")}
    wrong = _wrong_pairs([r for r in existing.values() if r.rejected_at is not None])
    kept: set[int] = set()
    made = skipped = pending = 0
    for (i, j), n in common.items():
        a, b = cands[i], cands[j]
        if a[1] == b[1] == "label" and a[3] == b[3]:
            continue                              # the same label in two databases
        if a[1] == b[1] == "field" and a[4] == b[4] and a[2] == b[2]:
            continue                              # two fields of one index
        va, vb = a[5], b[5]
        smaller = min(len(va), len(vb))
        need = MIN_COMMON if smaller <= 3 else 3
        coverage = n / smaller if smaller else 0
        if n < need or coverage < MIN_COVERAGE:
            continue
        if a[0] > b[0]:                           # the smaller id first: same for the evidence
            a, b, va, vb = b, a, vb, va
        rel = existing.get((a[0], b[0]))
        if (rel is not None and rel.rejected_at is not None) or frozenset((_end(a), _end(b))) in wrong:
            skipped += 1                          # an admin said it is wrong: never again
            continue
        if rel is None:
            rel = Relation(a_id=a[0], b_id=b[0], relation="same_values")
            db.session.add(rel)
            existing[(a[0], b[0])] = rel
        if rel.origin == "curated":
            kept.add(id(rel))
            continue
        rel.evidence = {"a_values": len(va), "b_values": len(vb), "common": n, "coverage": round(coverage, 3),
                        "examples": sorted(va & vb)[:6]}
        rel.confidence = round(float(coverage), 3)
        rel.origin = "learned"
        kept.add(id(rel))
        made += 1
        pending += 1
        if pending >= BATCH:
            db.session.commit()
            pending = 0
    db.session.commit()
    gone = [r.id for r in existing.values() if id(r) not in kept and r.origin == "learned" and r.rejected_at is None
            and r.id is not None]
    for i in range(0, len(gone), BATCH):
        db.session.query(Relation).filter(Relation.id.in_(gone[i:i + BATCH])).delete(synchronize_session=False)
    db.session.commit()
    return {"same_values": made, "removed": len(gone), "rejected": skipped}


def relations_of(obj_ids: list[int]) -> list[tuple[Relation, Any, Any]]:
    """Relations touching these objects, with both ends."""
    if not obj_ids:
        return []
    rels = (db.session.query(Relation).filter((Relation.a_id.in_(obj_ids)) | (Relation.b_id.in_(obj_ids)))
            .filter(Relation.rejected_at.is_(None)).all())
    ids = {r.a_id for r in rels} | {r.b_id for r in rels}
    objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ids))} if ids else {}
    return [(r, objs.get(r.a_id), objs.get(r.b_id)) for r in rels]
