"""Searching the knowledge for a question: words (PostgreSQL full-text search, built in) and
meaning (vectors of the embedding model, when one is set), ranks fused; with the knowledge store
(pgstore, 0.6) BM25, near spellings and pgvector in PostgreSQL instead. Only what the user may
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
VECTOR_FLOOR = 0.35     # a piece found by meaning only must be at least this close (cosine)...
VECTOR_MARGIN = 0.2     # ... and not far behind the closest one: a weak neighbour is noise, not knowledge


CONTEXT_PLACES = 5      # a Context page (partly AI-written) counts as found 5 places lower: on equal
                        # relevance, the catalog and the documents come first


def _allowed_query(kinds: tuple[str, ...] | None = None) -> Any:
    from flask import g

    from supagent.knowledge.context import visible_pages
    from supagent.knowledge.curated import sources_of_user
    from supagent.security import visible_databases

    user_id = getattr(getattr(g, "user", None), "id", None)
    sources = [s.id for s in sources_of_user()] or [-1]
    dbs = list(visible_databases()) or [-1]
    q = (db.session.query(Chunk)
         .filter(or_(Chunk.source_id.is_(None), Chunk.source_id.in_(sources)))
         .filter(or_(Chunk.database_id.is_(None), Chunk.database_id.in_(dbs)))
         .filter(or_(Chunk.scope == "team", and_(Chunk.scope == "user", Chunk.user_id == user_id))))
    pages = [p.id for p in visible_pages()]                # a Context page: every database of it readable
    q = q.filter(or_(Chunk.kind != "context", *[Chunk.ref.like(f"context:{i}#%") for i in pages]))
    if kinds:
        q = q.filter(Chunk.kind.in_(kinds))
    return q


def _superset_allowed(c: Chunk) -> bool:
    """A chart or dashboard piece only for a user Superset lets open it (and, for a dashboard, every chart
    it lists): its database alone is not enough (dashboard roles, dataset access)."""
    if c.kind not in ("chart", "dashboard"):
        return True
    try:
        from superset.extensions import security_manager
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        parts = c.ref.split(":")
        if parts[1] == "chart":
            chart = db.session.get(Slice, int(parts[2]))
            return chart is not None and bool(security_manager.can_access_chart(chart))
        board = db.session.get(Dashboard, int(parts[2]))
        return board is not None and bool(security_manager.can_access_dashboard(board)) and all(
            security_manager.can_access_chart(sl) for sl in board.slices or [])
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return False


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


def search_all(query: str, kinds: tuple[str, ...] | None = None, cap: int = 500) -> list[dict[str, Any]]:
    """Every piece the search finds for a query (at most `cap`), best first: the first ones as search() orders
    them for the agent (reranked when a reranker is set), then the others as the ways of searching rank them.
    For the Data dictionary's search, which lists them all, page by page."""
    from supagent.knowledge import rerank as R

    found = _search(query, cap, kinds, None, (), n=cap)
    if not found or not R.enabled():
        return found
    return R.rerank(query, found)                 # the first rerank.depth reordered, the others after them


ITEM_REF = re.compile(r"(object|entry|memory|doc|note|context|recipe):\d+")


def link_of(ref: str) -> dict[str, str] | None:
    """Where a piece is read or edited: a chart or a dashboard in Superset, else its place in the Data dictionary
    (#open/<ref>, which the page opens: an index or a metric, a catalog entry, a note, a document...)."""
    from urllib.parse import quote

    kind, _, rest = (ref or "").partition(":")
    if kind == "superset":
        parts = rest.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            if parts[0] == "chart":
                return {"href": f"/explore/?slice_id={parts[1]}", "where": "superset"}
            if parts[0] == "dashboard":
                return {"href": f"/superset/dashboard/{parts[1]}/", "where": "superset"}
        return None
    base = rest.split("#", 1)[0]
    if kind in ("object", "entry", "memory", "doc", "note", "context", "recipe") and base.split(":")[0].isdigit():
        return {"href": "#open/" + quote(f"{kind}:{base.split(':')[0]}", safe=""), "where": "dictionary"}
    return None


def item(ref: str) -> dict[str, Any] | None:
    """One piece of the knowledge as the search sees it (its parts joined), for a user who may search it."""
    ref = (ref or "").strip()
    if not ITEM_REF.fullmatch(ref):
        return None
    q = _allowed_query().filter(or_(Chunk.ref == ref, Chunk.ref.like(f"{ref}#%")))
    rows = sorted(q.limit(200).all(), key=lambda c: (len(c.ref), c.ref))
    rows = [c for c in rows if _superset_allowed(c)]
    if not rows:
        return None
    first = rows[0]
    parts = sorted(rows, key=lambda c: int(c.ref.rsplit("#", 1)[1]) if c.ref.rsplit("#", 1)[-1].isdigit() else 0)
    return {"ref": ref, "kind": first.kind, "title": first.title, "text": "\n\n".join(c.text or "" for c in parts),
            "link": link_of(ref)}


def search(query: str, k: int | None = None, kinds: tuple[str, ...] | None = None,
           lower: dict[str, int] | None = None, skip: tuple[str, ...] = (), rerank: bool = True) -> list[dict[str, Any]]:
    """The pieces for a query, best first. `lower`: kinds counted as found that many places lower
    (default: the Context, CONTEXT_PLACES); `skip`: kinds left out; `rerank`: the reranker orders the first
    ones again (rerank.url; the router, which must be quick, does without)."""
    from supagent.knowledge import rerank as R

    k = int(k or settings.get("search.top_k"))
    if not (rerank and R.enabled()):
        return _search(query, k, kinds, lower, skip)
    depth = max(k, int(settings.get("rerank.depth") or 40))
    return R.rerank(query, _search(query, depth, kinds, lower, skip), k)


def _search(query: str, k: int, kinds: tuple[str, ...] | None, lower: dict[str, int] | None,
            skip: tuple[str, ...], n: int | None = None) -> list[dict[str, Any]]:
    from supagent.knowledge import embeddings as E, pgstore

    lower = {"context": CONTEXT_PLACES} if lower is None else lower
    if pgstore.active():                          # the knowledge store (0.6): BM25, near spellings, pgvector
        try:
            return pgstore.search(query, k, kinds=kinds, lower=lower, skip=skip, n=n)
        except Exception as ex:  # pylint: disable=broad-except   (the search of 0.5 answers)
            log.warning("supagent search: the store failed, searched without it: %s", str(ex)[:300])
    q = _allowed_query(kinds)
    if skip:
        q = q.filter(Chunk.kind.notin_(list(skip)))
    found: list[tuple[int, int, str]] = []                # (chunk, rank, list)
    by_words = _lexical(q, _terms(query), max(50, int(n or 50)))
    found += [(cid, rank, "words") for rank, cid in enumerate(by_words)]
    by_meaning: set[int] = set()
    if E.enabled():
        try:
            allowed = {cid for (cid,) in q.with_entities(Chunk.id)}
            qv = E.embed([query])[0]
            hits = E.nearest(qv, allowed, max(50, int(n or 50)))
            floor = max(VECTOR_FLOOR, (hits[0][1] - VECTOR_MARGIN) if hits else 0.0)
            words = set(by_words)
            for rank, (cid, score) in enumerate(hits):
                if score < floor and cid not in words:       # a weak neighbour found by meaning only
                    continue
                by_meaning.add(cid)
                found.append((cid, rank, "meaning"))
        except Exception as ex:  # pylint: disable=broad-except   (words still work)
            log.warning("supagent search: vectors not used: %s", ex)
    ranks: dict[int, float] = {}
    if found:
        kinds_of = dict(db.session.query(Chunk.id, Chunk.kind).filter(Chunk.id.in_({cid for cid, _r, _l in found})))
        for cid, rank, _list in found:
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF + rank + lower.get(kinds_of.get(cid) or "", 0))
    best = sorted(ranks.items(), key=lambda x: -x[1])[:k + 10]
    if not best:
        return []
    rows = {c.id: c for c in db.session.query(Chunk).filter(Chunk.id.in_([cid for cid, _s in best]))}
    best = [(cid, sc) for cid, sc in best if cid in rows and _superset_allowed(rows[cid])][:k]
    words_found = set(by_words)
    out = []
    for cid, score in best:
        c = rows.get(cid)
        if c is not None:
            via = "words and meaning" if cid in words_found and cid in by_meaning else \
                ("words" if cid in words_found else "meaning")
            out.append({"ref": c.ref, "kind": c.kind, "title": c.title, "text": c.text, "score": round(score, 4),
                        "via": via})
    return out


OBJECT_KINDS = ("metric", "index")
OBJECT_PLACES = 3       # in the knowledge found with a question: "Where the data is" gives the data first
CONTEXT_LINES = 2       # at most, the pages about a subject of the question (an application...) first
PAGE_WORDS = ("context ai written overview application data source inventory how the system works architecture "
              "glossary rules and facts")


SUPERSET_KINDS = ("chart", "dashboard")


def knowledge_block(question: str, shown: set[str] | None = None, with_charts: bool = False,
                    prefer: dict[str, int] | None = None) -> str:
    """The knowledge relevant to a question, for the agent's prompt (short). `shown`: what the
    other blocks give already (refs, titles of the metrics and indices): not given twice, nor a
    metric or an index of the same name in another database, so that the room goes to the
    catalog, the documents and the Context. Superset's charts and dashboards only `with_charts` (a
    question about them, or about what is happening now)."""
    shown = set() if shown is None else shown        # the caller's set: it learns what was given (ranking)
    top_k = int(settings.get("search.top_k"))
    try:
        lower = {"context": CONTEXT_PLACES, **{kind: OBJECT_PLACES for kind in OBJECT_KINDS + SUPERSET_KINDS}}
        for kind, places in (prefer or {}).items():         # the router's kind of question: its knowledge first
            lower[kind] = lower.get(kind, 0) + places
        found = search(question, k=top_k + len(shown),     # data objects, charts: "Where the data is" and the
                       lower=lower,                          # tools give them; the room is for the team's words
                       skip=() if with_charts else SUPERSET_KINDS)    # charts: for questions about them
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent search: %s", ex)
        db.session.rollback()
        return ""
    if not found:
        return ""
    from supagent.knowledge.resolve import gone_names, mentions_gone

    gone = gone_names()
    found = [f for f in found if f["kind"] not in ("recipe", "memory") or not mentions_gone(f["text"], gone)]
    given, kept = set(shown), []
    for f in found:          # one line per metric or index name, glossary term, entry, document, page
        key = f["title"] if f["kind"] in OBJECT_KINDS else (f["ref"] if f["kind"] == "glossary"
                                                            else f["ref"].split("#")[0])
        if key in given:
            continue
        given.add(key)
        kept.append(f)
    from supagent.knowledge.experience import words

    asked, generic = words(question), words(PAGE_WORDS)

    def about_it(f: dict[str, Any]) -> bool:          # a Context page named after a subject of the question
        subject = words(f["title"]) - generic
        return bool(subject) and subject <= asked

    pages = [f for f in kept if f["kind"] == "context"]
    pages = ([f for f in pages if about_it(f)] + [f for f in pages if not about_it(f)])[:CONTEXT_LINES]
    found = [f for f in kept if f["kind"] != "context" or f in pages][:top_k]
    found.sort(key=lambda f: f["kind"] == "context" and not about_it(f))   # the others' order kept
    try:                                 # their categories (the application, the subject): which rule is whose
        from supagent.knowledge.facets import facets_of

        cats = facets_of([f["ref"] for f in found])
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        cats = {}
    budget = int(settings.get("search.prompt_chars"))
    lines = ["\n\nBackground that looks relevant to this question (from the data dictionary, the catalog, the "
             "team's learned answers and memory, the documents). It is a summary, not an answer: call "
             "describe_data for the fields and their meaning, and run the query for any number; search_knowledge "
             "gives more:"]
    for f in found:
        text = " ".join((f["text"] or "").split())
        about = [t.split(": ", 1)[1] for t in cats.get(f["ref"], []) if not t.startswith("aspect")][:3]
        line = f"- [{f['kind']}] {f['title']}" + (f" ({', '.join(about)})" if about else "") + f": {text[:450]}"
        if sum(len(x) for x in lines) + len(line) > budget:
            break
        lines.append(line)
        shown.add(f["ref"])
    return "\n".join(lines) if len(lines) > 1 else ""
