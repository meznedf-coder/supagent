"""Searching the knowledge for a question: words (PostgreSQL full-text search, built in) and
meaning (vectors of the embedding model, when one is set), ranks fused. Only what the user may
see is searched: pieces about databases the user may query, the team's pieces and the user's
own (personal memories); the filter is applied before ranking."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import and_, func, or_
from superset import db

from supagent import settings
from supagent.models import Chunk

log = logging.getLogger(__name__)
RRF = 60


def _allowed_query(kinds: tuple[str, ...] | None = None) -> Any:
    from flask import g

    from supagent.knowledge.curated import sources_of_user
    from supagent.security import visible_databases

    user_id = getattr(getattr(g, "user", None), "id", None)
    sources = [s.id for s in sources_of_user()] or [-1]
    dbs = list(visible_databases()) or [-1]
    q = (db.session.query(Chunk)
         .filter(or_(Chunk.source_id.is_(None), Chunk.source_id.in_(sources)))
         .filter(or_(Chunk.database_id.is_(None), Chunk.database_id.in_(dbs)))
         .filter(or_(Chunk.scope == "team", and_(Chunk.scope == "user", Chunk.user_id == user_id))))
    if kinds:
        q = q.filter(Chunk.kind.in_(kinds))
    return q


def _terms(query: str) -> list[str]:
    from supagent.knowledge.describe import STOP, stem

    words = [stem(w) for w in re.findall(r"[a-z0-9_]+", (query or "").lower())]
    return [w for w in dict.fromkeys(words) if w not in STOP and len(w) > 1][:24]


def _lexical(q: Any, terms: list[str], limit: int = 50) -> list[int]:
    if not terms:
        return []
    if db.engine.dialect.name == "postgresql":
        doc = func.to_tsvector("simple", func.coalesce(Chunk.title, "") + " " + func.coalesce(Chunk.text, ""))
        tsq = func.to_tsquery("simple", " | ".join(f"{re.sub(r'[^a-z0-9_]', '', t)}:*" for t in terms))
        rows = (q.filter(doc.op("@@")(tsq)).with_entities(Chunk.id, func.ts_rank_cd(doc, tsq).label("r"))
                .order_by(func.ts_rank_cd(doc, tsq).desc()).limit(limit).all())
        return [r[0] for r in rows]
    scored = []                                   # other databases: words counted here
    for c in q.limit(20000):
        text = f"{c.title} {c.text}".lower()
        hits = sum(1 for t in terms if t in text)
        if hits:
            scored.append((hits, c.id))
    return [cid for _h, cid in sorted(scored, key=lambda x: -x[0])[:limit]]


def search(query: str, k: int | None = None, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    from supagent.knowledge import embeddings as E

    k = int(k or settings.get("search.top_k"))
    q = _allowed_query(kinds)
    ranks: dict[int, float] = {}
    for rank, cid in enumerate(_lexical(q, _terms(query))):
        ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF + rank)
    if E.enabled():
        try:
            allowed = {cid for (cid,) in q.with_entities(Chunk.id)}
            qv = E.embed([query])[0]
            for rank, (cid, _score) in enumerate(E.nearest(qv, allowed, 50)):
                ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF + rank)
        except Exception as ex:  # pylint: disable=broad-except   (words still work)
            log.warning("supagent search: vectors not used: %s", ex)
    best = sorted(ranks.items(), key=lambda x: -x[1])[:k]
    if not best:
        return []
    rows = {c.id: c for c in db.session.query(Chunk).filter(Chunk.id.in_([cid for cid, _s in best]))}
    out = []
    for cid, score in best:
        c = rows.get(cid)
        if c is not None:
            out.append({"ref": c.ref, "kind": c.kind, "title": c.title, "text": c.text, "score": round(score, 4)})
    return out


def knowledge_block(question: str) -> str:
    """The knowledge relevant to a question, for the agent's prompt (short)."""
    try:
        found = search(question)
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent search: %s", ex)
        db.session.rollback()
        return ""
    if not found:
        return ""
    budget = int(settings.get("search.prompt_chars"))
    lines = ["\n\nBackground that looks relevant to this question (from the data dictionary, the catalog, the "
             "team's learned answers and memory, the documents). It is a summary, not an answer: call "
             "describe_data for the fields and their meaning, and run the query for any number; search_knowledge "
             "gives more:"]
    for f in found:
        text = " ".join((f["text"] or "").split())
        line = f"- [{f['kind']}] {f['title']}: {text[:450]}"
        if sum(len(x) for x in lines) + len(line) > budget:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""
