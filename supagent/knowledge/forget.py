"""Forgetting what the learner learned, to learn it again from scratch
(`superset supagent forget-learned`).

Forgotten, for the chosen databases (every learned one by default): the indices, fields,
metrics, labels and families with their statistics and AI-written descriptions, the measured
relations and the history of changes; the next run learns them all as new.

Kept unless `everything`: what people did in the Data dictionary page, on the objects that carry
it (a description written or approved by an admin, synonyms; not what the catalog gives, which
the next run applies again) and the relations an admin marked Wrong. Those objects stay with
their learned facts cleared, and are profiled again as new ones.

Never touched: the catalog entries (the next run applies them again), the learned answers, query
timings, memory, documents, chats, settings and the list of learning runs.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import and_, or_

from superset import db

from supagent.models import Change, KObject, Relation, Source


def _people(o: KObject, catalog: dict[tuple[str, str, str], dict[str, Any]]) -> bool:
    """What an admin did on this object in the Data dictionary page: a description written there, an
    AI or HELP text approved, synonyms. What the catalog gives it is not counted: the next run
    applies the catalog again."""
    spec = catalog.get((o.kind, o.parent or "", o.name)) or {}
    if o.description_source == "curated" and o.description and \
            o.description.strip() != str(spec.get("description") or "").strip():
        return True
    if o.verified and o.description and o.description_source != "curated":
        return True
    syn = spec.get("synonyms")
    syn = [str(x) for x in (syn if isinstance(syn, list) else [syn])] if syn else None
    return bool(o.synonyms) and o.synonyms != syn


def forget(databases: list[str] | None = None, everything: bool = False, apply: bool = False) -> list[dict[str, Any]]:
    """What would be (or, with `apply`, is) forgotten, per database."""
    from supagent.knowledge.curated import catalog_texts

    wanted = {str(d) for d in databases or []}
    catalog = catalog_texts()
    out = []
    for src in db.session.query(Source).order_by(Source.id):
        if wanted and src.database_name not in wanted and str(src.database_id) not in wanted:
            continue
        ids_q = db.session.query(KObject.id).filter(KObject.source_id == src.id)
        objs = db.session.query(KObject).filter(KObject.source_id == src.id).all()
        mine = {o.id for o in objs}
        keep: set[int] = set()
        if not everything:
            keep = {o.id for o in objs if _people(o, catalog)}
            for r in db.session.query(Relation).filter(Relation.rejected_at.isnot(None),
                                                       or_(Relation.a_id.in_(ids_q), Relation.b_id.in_(ids_q))):
                keep |= {r.a_id, r.b_id} & mine
        gone_q = ids_q.filter(KObject.id.notin_(keep)) if keep else ids_q
        # the relations of the objects that go, and the measured or catalog ones of those that stay
        # (measured again, applied again); a Wrong mark stays while both its ends stay
        rels = db.session.query(Relation).filter(or_(
            Relation.a_id.in_(gone_q), Relation.b_id.in_(gone_q),
            and_(or_(Relation.a_id.in_(ids_q), Relation.b_id.in_(ids_q)), Relation.rejected_at.is_(None))))
        row = {"database": src.database_name, "backend": src.backend,
               "objects": dict(Counter(o.kind for o in objs if o.id not in keep)), "kept": len(keep),
               "relations": rels.count(), "changes": db.session.query(Change).filter(Change.object_id.in_(ids_q)).count()}
        if apply:
            db.session.query(Change).filter(Change.object_id.in_(ids_q)).delete(synchronize_session=False)
            rels.delete(synchronize_session=False)
            gone = db.session.query(KObject).filter(KObject.source_id == src.id)
            if keep:
                gone = gone.filter(KObject.id.notin_(keep))
            gone.delete(synchronize_session=False)
            for o in objs:
                if o.id in keep:                       # people's work stays; the learned facts go
                    o.stats, o.fingerprint, o.gone_at = None, None, None
                    if o.description_source == "llm" and not o.verified:
                        o.description, o.description_source = None, None
            src.last_learned_at, src.stats = None, None
            db.session.commit()
        out.append(row)
    if apply and out:
        from supagent.knowledge.index import sync

        sync(("object:",))                             # their searchable pieces go too
    if not apply:
        db.session.rollback()
    return out
