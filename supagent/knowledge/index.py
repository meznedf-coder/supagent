"""The searchable pieces of knowledge (supagent_chunk), kept in step with what they come from:

  metric / index   one piece per metric or index (its labels or fields summarised in it; one
                   piece per field would cost hundreds of thousands of embeddings on OTel data)
  rule / note / glossary / formula   the catalog's text entries (notes cut into parts)
  recipe           the learned ways to an answer (not the rejected ones)
  memory           the preferences, rules and facts of a user or of the team (active ones)
  doc              the parts of the documents and sites

`sync()` writes the pieces whose text changed (and removes the ones whose origin is gone):
cheap, done after a learning run, a catalog change, an answer. `embed_pending()` gives the new
or changed pieces their vector (the embedding model), a batch at a time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Any, Iterator

from superset import db

from supagent import settings
from supagent.models import Chunk, Doc, Entry, KObject, Memory, Recipe

log = logging.getLogger(__name__)
PART_CHARS = 1500
OVERLAP = 200


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p or "") for p in parts).encode()).hexdigest()[:40]


def split_text(text: str, size: int = PART_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Parts of about `size` characters, cut at paragraph or sentence ends, overlapping a little."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in parts if p]


# --------------------------------------------------------------------------- #
# the pieces
# --------------------------------------------------------------------------- #
def _object_pieces() -> Iterator[dict[str, Any]]:
    from supagent.knowledge.experience import COUNTER_SUFFIXES, counter_formulas

    children: dict[tuple[int, str], list[KObject]] = {}
    for o in db.session.query(KObject).filter(KObject.kind.in_(("label", "field")), KObject.gone_at.is_(None)):
        children.setdefault((o.source_id, o.parent), []).append(o)
    for o in db.session.query(KObject).filter(KObject.kind.in_(("metric", "index")), KObject.gone_at.is_(None)):
        st = o.stats or {}
        kids = children.get((o.source_id, o.name), [])
        lines = [f"{o.kind} {o.name}" + (f" ({o.metric_type}{', ' + o.unit if o.unit else ''})" if o.metric_type
                                           else ""),
                 (o.description or "").strip()]
        if o.synonyms:
            lines.append("also called: " + ", ".join(map(str, o.synonyms)))
        if o.category:
            lines.append(f"category: {o.category}")
        if st.get("series") is not None:
            lines.append(f"{st['series']} series")
        if st.get("docs") is not None:
            lines.append(f"{st['docs']} documents, time field {st.get('time_field')}")
        kid_word = "labels" if o.kind == "metric" else "fields"
        described = []
        for k in sorted(kids, key=lambda x: x.name)[:120]:
            item = k.name
            if k.description:
                item += f" ({k.description[:120]})"
            vals = (k.stats or {}).get("values") or []
            if vals and len(vals) <= 20:
                item += ": " + ", ".join(map(str, vals[:8]))
            described.append(item)
        if described:
            lines.append(f"{kid_word}: " + "; ".join(described))
        if o.kind == "metric":
            if o.metric_type in ("counter", "histogram", "summary") or o.name.endswith(COUNTER_SUFFIXES):
                lines.append("SQL: a counter: use the column rate (per second) or increase (per time bucket); "
                             "value is cumulative, never SUM or AVG it")
            formulas = counter_formulas(o.name)
            if formulas:
                lines.append("formulas (catalog): " + " | ".join(formulas)[:900])
        yield {"ref": f"object:{o.id}", "kind": o.kind, "source_id": o.source_id, "title": f"{o.kind} {o.name}",
               "text": "\n".join(x for x in lines if x)[:4000]}


def _entry_pieces() -> Iterator[dict[str, Any]]:
    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                            Entry.classification.in_(("rule", "note", "glossary", "formula"))):
        # a formula the agent learned on a database: found only by the users who may query it
        database_id = (e.evidence or {}).get("database_id") if e.origin and e.classification == "formula" else None
        for i, part in enumerate(split_text(e.content or "")):
            yield {"ref": f"entry:{e.id}#{i}", "kind": e.classification, "database_id": database_id,
                   "title": e.title + (f" ({e.category})" if e.category else ""), "text": part}


def _recipe_pieces() -> Iterator[dict[str, Any]]:
    for r in db.session.query(Recipe).filter(Recipe.status != "rejected"):
        yield {"ref": f"recipe:{r.id}", "kind": "recipe", "database_id": r.database_id or 0,   # unknown: nobody
               "title": f"answered before ({r.status}): {(r.question or '')[:200]}",
               "text": f"{r.question}\n{r.tool}: {(r.query or '')[:2500]}"}


def _memory_pieces() -> Iterator[dict[str, Any]]:
    for m in db.session.query(Memory).filter(Memory.status == "active"):
        yield {"ref": f"memory:{m.id}", "kind": "memory", "scope": m.scope,
               "user_id": m.user_id if m.scope == "user" else None,
               "title": f"{m.kind} ({'team' if m.scope == 'team' else 'personal'})"
               + (f", {m.category}" if m.category else ""), "text": m.text}


def _doc_pieces() -> Iterator[dict[str, Any]]:
    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.content.isnot(None)):
        for i, part in enumerate(split_text(d.content or "")):
            yield {"ref": f"doc:{d.id}#{i}", "kind": "doc", "title": (d.title or d.url or f"document {d.id}")
                   + (f" ({d.category})" if d.category else ""), "text": part}


def pieces() -> Iterator[dict[str, Any]]:
    yield from _object_pieces()
    yield from _entry_pieces()
    yield from _recipe_pieces()
    yield from _memory_pieces()
    yield from _doc_pieces()


# --------------------------------------------------------------------------- #
# keeping the chunks in step
# --------------------------------------------------------------------------- #
def sync(prefixes: tuple[str, ...] | None = None) -> dict[str, int]:
    """Write the pieces that changed, remove the ones whose origin is gone. `prefixes` limits
    the work to some kinds of refs (e.g. ("recipe:",) after an answer)."""
    from supagent.knowledge.embeddings import qdrant_delete

    existing = {c.ref: c for c in db.session.query(Chunk)
                if prefixes is None or c.ref.startswith(prefixes)}
    seen: set[str] = set()
    out = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
    for p in pieces():
        if prefixes is not None and not p["ref"].startswith(prefixes):
            continue
        seen.add(p["ref"])
        h = _hash(p.get("title"), p.get("text"), p.get("source_id"), p.get("database_id"), p.get("scope"),
                  p.get("user_id"))
        c = existing.get(p["ref"])
        if c is not None and c.content_hash == h:
            out["unchanged"] += 1
            continue
        if c is None:
            c = Chunk(ref=p["ref"])
            db.session.add(c)
            out["added"] += 1
        else:
            out["changed"] += 1
        c.kind, c.title, c.text = p["kind"], (p.get("title") or "")[:512], p.get("text") or ""
        c.source_id, c.database_id = p.get("source_id"), p.get("database_id")
        c.scope, c.user_id = p.get("scope") or "team", p.get("user_id")
        c.content_hash, c.vector, c.embed_model = h, None, None     # a new text needs a new vector
        c.updated_at = dt.datetime.utcnow()
    gone = [c for ref, c in existing.items() if ref not in seen]
    for c in gone:
        db.session.delete(c)
    out["removed"] = len(gone)
    db.session.commit()
    if gone:
        try:
            qdrant_delete([c.id for c in gone])
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent: qdrant delete: %s", ex)
    return out


def embed_pending(limit: int | None = None) -> dict[str, Any]:
    """Vectors for the pieces that have none (or another model's)."""
    from supagent.knowledge import embeddings as E

    if not E.enabled():
        return {"embedded": 0, "note": "no embedding model (embed.model): search by words only"}
    model = E.model()
    limit = int(limit or settings.get("embed.per_run"))
    todo = (db.session.query(Chunk).filter((Chunk.vector.is_(None)) | (Chunk.embed_model != model))
            .order_by(Chunk.id).limit(limit).all())
    done, batch = 0, max(1, int(settings.get("embed.batch")))
    qdrant = settings.get("search.vector_store") == "qdrant"
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vectors = E.embed([f"{c.title}\n{c.text}" for c in part])
        for c, v in zip(part, vectors):
            c.vector = E.to_bytes(v)
            c.embed_model = model
        if qdrant:
            E.qdrant_upsert([(c.id, v) for c, v in zip(part, vectors)])
        db.session.commit()
        done += len(part)
    left = db.session.query(Chunk).filter((Chunk.vector.is_(None)) | (Chunk.embed_model != model)).count()
    return {"embedded": done, "left": left}


def index_knowledge() -> dict[str, Any]:
    out: dict[str, Any] = {"sync": sync()}
    try:
        out["embed"] = embed_pending()
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        out["embed"] = {"error": str(ex)[:300]}
    return out
