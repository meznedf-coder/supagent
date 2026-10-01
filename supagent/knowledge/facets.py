"""The categories of the knowledge (0.6). Every item the agent searches (catalog entries, team memories, documents,
Context pages, learned answers, indices and metric families) is classified by the LLM along a few facets whose
values are the deployment's own words, and the relations the texts state between items (a note about a table, a
report that depends on a job) are kept:

  aspect       functional (what the business means and counts) or technical (how the systems work): fixed
  subject      the business or technical subjects of the deployment
  application  the applications, systems and services
  component    the parts of them: jobs, servers, pricers, feeds...

Nothing is hard-coded about a domain: the values are seeded from what people wrote (catalog and document
categories), from the data (the values of fields named application, service, system, component) and proposed by
the LLM; a proposed value is used once an admin approves it (the Data dictionary's review). The LLM chooses among
the known values first. An item is classified again only when its text changes; the work runs with the daily
learning, within its LLM time.

The router (supagent.router) reads them as evidence; the store adds them to the words of each piece (a question
naming a subject finds its items) and they are shown in the Data dictionary.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from typing import Any, Iterator

from superset import db

log = logging.getLogger(__name__)

FACETS = ("aspect", "subject", "application", "component")
ASPECTS = ("functional", "technical")
LINK_KINDS = ("about", "depends_on", "part_of", "explains", "runs_on", "same_as")
BATCH = 8                   # items per LLM call
TEXT_CHARS = 1200
APP_FIELDS = re.compile(r"^(application|app|app_name|service|service_name|system|component|platform)$", re.I)
MAX_DATA_VALUES = 60        # a field with more values than this is not a list of applications
CONFIDENT = 0.7             # a tag or a link the LLM is this sure of (medium, high) is used at once: an admin reviews
#                             the new values, the low ones and the links that say two items are the same
REVIEWED_LINKS = ("same_as",)


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p or "") for p in parts).encode()).hexdigest()[:40]


def base_ref(ref: str) -> str:
    return (ref or "").split("#", 1)[0]


# --------------------------------------------------------------------------------------------- #
# the vocabulary
# --------------------------------------------------------------------------------------------- #
def _ensure(facet: str, value: str, status: str, source: str, description: str | None = None) -> Any:
    from supagent.models import Facet

    value = " ".join(str(value or "").split())[:128]
    if not value:
        return None
    f = (db.session.query(Facet).filter(Facet.facet == facet, Facet.value.ilike(value)).first())
    if f is None:
        f = Facet(facet=facet, value=value, status=status, source=source, description=description)
        db.session.add(f)
        db.session.flush()
    elif f.status == "proposed" and status == "approved":
        f.status, f.source = "approved", source
    return f


def named(value: str) -> bool:
    """A value of the data good enough to name an application: two characters or more, a letter among them (a
    code like A, 1 or 42 names nothing a question could say)."""
    v = str(value or "").strip()
    return len(v) >= 2 and any(ch.isalpha() for ch in v)


def seed() -> dict[str, int]:
    """The values that need nobody's approval: the aspects, the categories people wrote (catalog entries,
    documents, the dictionary's categories), the values of the data's application-like fields."""
    from supagent.models import Doc, Entry, KObject

    n = {"aspect": 0, "subject": 0, "application": 0}
    for a in ASPECTS:
        n["aspect"] += _ensure("aspect", a, "approved", "seed") is not None
    cats = {c for (c,) in db.session.query(Entry.category).filter(Entry.deleted_at.is_(None), Entry.category.isnot(None))}
    cats |= {c for (c,) in db.session.query(Doc.category).filter(Doc.category.isnot(None))}
    cats |= {c for (c,) in db.session.query(KObject.category).filter(KObject.category.isnot(None),
                                                                     KObject.gone_at.is_(None))}
    for c in sorted(x for x in cats if x and x.strip() and x.lower() not in ("context", "lab")):
        n["subject"] += _ensure("subject", c, "approved", "seed") is not None
    for o in db.session.query(KObject).filter(KObject.kind.in_(("field", "label")), KObject.gone_at.is_(None)):
        if not APP_FIELDS.match(o.name or ""):
            continue
        values = (o.stats or {}).get("values") or []
        if 0 < len(values) <= MAX_DATA_VALUES and not (o.stats or {}).get("partial"):
            for v in values:
                if named(str(v)):
                    n["application"] += _ensure("application", str(v), "approved", "data") is not None
    db.session.commit()
    return n


def vocabulary(with_proposed: bool = True) -> dict[str, list[dict[str, Any]]]:
    from supagent.models import Facet

    q = db.session.query(Facet).filter(Facet.status.in_(("approved", "proposed") if with_proposed else ("approved",)))
    out: dict[str, list[dict[str, Any]]] = {f: [] for f in FACETS}
    for f in q.order_by(Facet.facet, Facet.value):
        out.setdefault(f.facet, []).append({"id": f.id, "value": f.value, "status": f.status,
                                            "description": f.description or "", "source": f.source})
    return out


# --------------------------------------------------------------------------------------------- #
# the items
# --------------------------------------------------------------------------------------------- #
def items() -> Iterator[dict[str, Any]]:
    """Every item to classify: its ref, kind, title and text (what the LLM reads)."""
    from supagent.knowledge.experience import USED
    from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Recipe

    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True)):
        yield {"ref": f"entry:{e.id}", "kind": e.classification, "title": e.title, "text": e.content or ""}
    for m in db.session.query(Memory).filter(Memory.status == "active", Memory.scope == "team"):
        yield {"ref": f"memory:{m.id}", "kind": "memory", "title": (m.text or "")[:80], "text": m.text or ""}
    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.content.isnot(None)):
        yield {"ref": f"doc:{d.id}", "kind": "document", "title": d.title or d.url or "", "text": d.content or ""}
    for p in db.session.query(ContextPage):
        yield {"ref": f"context:{p.id}", "kind": f"context ({p.section})", "title": p.title, "text": p.content or ""}
    for r in db.session.query(Recipe).filter(Recipe.status.in_(USED)):
        yield {"ref": f"recipe:{r.id}", "kind": "learned answer", "title": (r.question or "")[:120],
               "text": f"{r.question}\n{(r.query or '')[:600]}"}
    tops = db.session.query(KObject).filter(KObject.kind.in_(("index", "metric")), KObject.gone_at.is_(None)).all()
    kids: dict[tuple[int, str], list[str]] = {}
    for o in db.session.query(KObject.source_id, KObject.parent, KObject.name).filter(
            KObject.kind.in_(("field", "label")), KObject.gone_at.is_(None)):
        kids.setdefault((o[0], o[1]), []).append(o[2])
    families: dict[str, list[Any]] = {}
    for o in tops:
        if o.kind == "index":
            fields = ", ".join(sorted(kids.get((o.source_id, o.name), []))[:40])
            yield {"ref": f"object:{o.id}", "kind": "index", "title": o.name,
                   "text": f"{o.description or ''}\nfields: {fields}"}
        else:
            families.setdefault(family_of(o.source_id, o.name), []).append(o)
    for fam, members in families.items():         # metrics: one item per family, never one per metric
        lines = [f"{m.name}: {(m.description or m.backend_help or '')[:120]}" for m in
                 sorted(members, key=lambda m: m.name)[:12]]
        yield {"ref": fam, "kind": "metric family", "title": fam.split(":", 2)[2] + "_*",
               "text": f"{len(members)} metrics, e.g.\n" + "\n".join(lines)}


def family_of(source_id: int, metric: str) -> str:
    return f"family:{source_id}:{(metric or '').split('_', 1)[0].lower()}"


def pending(limit: int = 10_000) -> list[dict[str, Any]]:
    """The items never classified, or whose text changed since."""
    from supagent.models import Classified

    done = dict(db.session.query(Classified.ref, Classified.content_hash))
    out = []
    for it in items():
        h = _hash(it["title"], it["text"][:TEXT_CHARS])
        if done.get(it["ref"]) != h:
            it["hash"] = h
            out.append(it)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------------------------- #
# classifying with the LLM
# --------------------------------------------------------------------------------------------- #
CLASSIFY_TOOL = {"type": "function", "function": {
    "name": "classify_items",
    "description": "The categories of each item, and the relations its text states.",
    "parameters": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "aspect": {"type": "string", "enum": ["functional", "technical", "both"]},
            "subjects": {"type": "array", "items": {"type": "string"}},
            "applications": {"type": "array", "items": {"type": "string"}},
            "components": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "links": {"type": "array", "items": {"type": "object", "properties": {
                "to": {"type": "string", "description": "the ref of another item of the list, or a table name"},
                "kind": {"type": "string", "enum": list(LINK_KINDS)}}, "required": ["to", "kind"]}}},
            "required": ["ref", "aspect"]}},
        "new_values": {"type": "array", "items": {"type": "object", "properties": {
            "facet": {"type": "string", "enum": ["subject", "application", "component"]},
            "value": {"type": "string"}, "description": {"type": "string"}}, "required": ["facet", "value"]}}},
        "required": ["items"]}}}

CLASSIFY_SYSTEM = ("You classify pieces of knowledge of a company's data platform (catalog entries, notes, documents, "
                   "memories, learned answers, tables, metric families) so that questions reach the right ones. For "
                   "each item: aspect (functional: what the business means and counts; technical: how the systems "
                   "work; both), up to 3 subjects, the applications and the components it is about, and the "
                   "relations its text states to other items or tables (about, depends_on, part_of, explains, "
                   "runs_on). A component is a part of the systems (a service, a job or batch, a report, a "
                   "pipeline, a group of servers), never the name of a table, index, metric or field: those are "
                   "relations (about). Use the known values below, written exactly; when none fits, add a new value "
                   "in new_values with a short description (a general subject, never a single record). Only what "
                   "the text says. Call classify_items once.")


def classify_messages(batch: list[dict[str, Any]], vocab: dict[str, list[dict[str, Any]]],
                      tables: list[str]) -> list[dict[str, str]]:
    lines = ["Known values:"]
    for facet in ("subject", "application", "component"):
        vals = [v["value"] for v in vocab.get(facet, [])][:120]
        lines.append(f"- {facet}: " + (", ".join(vals) if vals else "(none yet)"))
    if tables:
        lines.append("Tables: " + ", ".join(tables[:80]))
    lines.append("\nItems:")
    for it in batch:
        text = " ".join((it["text"] or "").split())[:TEXT_CHARS]
        lines.append(f"[{it['ref']}] {it['kind']}: {it['title']}\n{text}")
    return [{"role": "system", "content": CLASSIFY_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def _args(msg: dict[str, Any]) -> dict[str, Any]:
    args: Any = None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "classify_items":
            args = (tc.get("function") or {}).get("arguments")
    if args is None:
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S), re.S)
        args = m.group(0) if m else "{}"
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _table_refs() -> dict[str, str]:
    """table name (lower case) -> data:<database>:<name>, for the links the LLM names."""
    from supagent.models import KObject, Source

    src_db = dict(db.session.query(Source.id, Source.database_id))
    out = {}
    for sid, name in db.session.query(KObject.source_id, KObject.name).filter(
            KObject.kind.in_(("index", "metric")), KObject.gone_at.is_(None)):
        if src_db.get(sid):
            out.setdefault(name.lower(), f"data:{src_db[sid]}:{name}")
    return out


def apply(args: dict[str, Any], batch: list[dict[str, Any]], table_refs: dict[str, str]) -> dict[str, int]:
    """The LLM's answer written: new values proposed, tags and links (confident ones used at once)."""
    from supagent.models import Classified, Facet, Link, Tag

    n = {"tags": 0, "proposed": 0, "links": 0}

    def data_name(facet: str, value: str) -> str | None:     # a "component" that is a table's name: a link
        return table_refs.get(value.strip().lower()) if facet == "component" else None

    for nv in args.get("new_values") or []:
        if data_name(str(nv.get("facet") or ""), str(nv.get("value") or "")):
            continue
        if nv.get("facet") in ("subject", "application", "component") and str(nv.get("value") or "").strip():
            f = _ensure(nv["facet"], nv["value"], "proposed", "llm", str(nv.get("description") or "")[:500] or None)
            if f is not None and f.status == "proposed":
                n["proposed"] += 1
    by_ref = {it["ref"]: it for it in batch}
    for row in args.get("items") or []:
        ref = str(row.get("ref") or "").strip("[] ")
        if ref not in by_ref:
            continue
        conf = {"high": 0.9, "medium": 0.7, "low": 0.4}.get(str(row.get("confidence") or "medium"), 0.7)
        wanted: list[tuple[str, str]] = []
        aspect = row.get("aspect")
        for a in (ASPECTS if aspect == "both" else [aspect]):
            if a in ASPECTS:
                wanted.append(("aspect", a))
        links = list((row.get("links") or [])[:8])
        for facet, key in (("subject", "subjects"), ("application", "applications"), ("component", "components")):
            for v in (row.get(key) or [])[:4]:
                if not str(v).strip():
                    continue
                if data_name(facet, str(v)):
                    links.append({"to": str(v), "kind": "about"})
                else:
                    wanted.append((facet, str(v)))
        for facet, value in wanted:
            f = (db.session.query(Facet).filter(Facet.facet == facet, Facet.value.ilike(value.strip())).first()
                 or _ensure(facet, value, "proposed", "llm"))
            if f is None or f.status == "rejected":
                continue
            t = db.session.query(Tag).filter(Tag.ref == ref, Tag.facet_id == f.id).first()
            if t is None:
                db.session.add(Tag(ref=ref, facet_id=f.id, confidence=conf, source="llm",
                                   status="approved" if f.status == "approved" and conf >= CONFIDENT else "proposed"))
                n["tags"] += 1
            elif t.source == "llm" and t.status != "rejected":
                t.confidence = conf
        for link in links:
            to = str(link.get("to") or "").strip("[] ")
            kind = link.get("kind")
            b = to if (to in by_ref or re.match(r"^(entry|memory|doc|context|recipe|object|family):", to)) else \
                table_refs.get(to.lower())
            if not b or b == ref or kind not in LINK_KINDS:
                continue
            if db.session.query(Link).filter(Link.a_ref == ref, Link.b_ref == b, Link.kind == kind).first() is None:
                db.session.add(Link(a_ref=ref, b_ref=b, kind=kind, confidence=conf, source="llm",
                                    status="approved" if conf >= CONFIDENT and kind not in REVIEWED_LINKS
                                    else "proposed"))
                n["links"] += 1
    for it in batch:                              # classified: not again until its text changes
        c = db.session.get(Classified, it["ref"]) or Classified(ref=it["ref"])
        c.content_hash, c.classified_at = it["hash"], dt.datetime.utcnow()
        db.session.merge(c)
    db.session.commit()
    return n


def settle() -> dict[str, int]:
    """What waits for an admin, by the rules above (for what an older rule left waiting): the confident tags of
    approved values and the confident links are used; the "components" that are tables' names are dropped."""
    from supagent.models import Facet, Link, Tag

    out = {"tags": 0, "links": 0, "values": 0}
    for f in db.session.query(Facet).filter(Facet.facet == "application", Facet.source == "data",
                                             Facet.status != "rejected"):
        if not named(f.value):                        # seeded by an older rule: a code that names nothing
            db.session.query(Tag).filter(Tag.facet_id == f.id).delete(synchronize_session=False)
            f.status = "rejected"
            out["values"] += 1
    names = _table_refs()
    for f in db.session.query(Facet).filter(Facet.facet == "component", Facet.status == "proposed",
                                             Facet.source == "llm"):
        if f.value.strip().lower() in names:
            db.session.query(Tag).filter(Tag.facet_id == f.id).delete(synchronize_session=False)
            db.session.delete(f)
            out["values"] += 1
    approved = [f.id for f in db.session.query(Facet.id).filter(Facet.status == "approved")]
    out["tags"] = (db.session.query(Tag).filter(Tag.status == "proposed", Tag.source == "llm",
                                               Tag.confidence >= CONFIDENT, Tag.facet_id.in_(approved or [-1]))
                   .update({Tag.status: "approved"}, synchronize_session=False))
    out["links"] = (db.session.query(Link).filter(Link.status == "proposed", Link.source == "llm",
                                                 Link.confidence >= CONFIDENT, Link.kind.notin_(REVIEWED_LINKS))
                    .update({Link.status: "approved"}, synchronize_session=False))
    db.session.commit()
    if any(out.values()):
        _CACHE.update(at=0.0)
    return out


def classify(llm: Any, seconds: float = 900.0, limit: int = 400) -> dict[str, Any]:
    """The items that changed, BATCH at a time, until `seconds` or `limit`; the next run continues."""
    from supagent.knowledge.stopping import check

    t0 = time.time()
    out: dict[str, Any] = {"seeded": seed(), "settled": settle(), "items": 0, "tags": 0, "proposed": 0, "links": 0,
                           "calls": 0}
    todo = pending(limit)
    if not todo:
        return out
    table_refs = _table_refs()
    tables = sorted({r.split(":", 2)[2] for r in table_refs.values()})
    for i in range(0, len(todo), BATCH):
        if time.time() - t0 > seconds:
            out["left"] = len(todo) - i
            break
        check()
        batch = todo[i:i + BATCH]
        try:
            msg = llm.chat(classify_messages(batch, vocabulary(), tables), tools=[CLASSIFY_TOOL], max_tokens=2500)
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent facets: classification failed: %s", str(ex)[:300])
            out["error"] = str(ex)[:300]
            break
        out["calls"] += 1
        n = apply(_args(msg), batch, table_refs)
        out["items"] += len(batch)
        for k, v in n.items():
            out[k] += v
    if out["tags"] or out["links"]:
        from supagent.knowledge.freshness import touch

        touch()
        db.session.commit()
    return out


# --------------------------------------------------------------------------------------------- #
# reading them
# --------------------------------------------------------------------------------------------- #
_CACHE: dict[str, Any] = {"at": 0.0, "stamp": None, "tags": {}}


def _approved() -> dict[str, list[str]]:
    """ref -> ["facet: value", ...] of the approved tags (cached, reloaded when the knowledge changes)."""
    from supagent.knowledge.freshness import stamp
    from supagent.models import Facet, Tag

    s = stamp()
    if _CACHE["stamp"] == s and time.time() - _CACHE["at"] < 300:
        return _CACHE["tags"]
    out: dict[str, list[str]] = {}
    try:
        rows = (db.session.query(Tag.ref, Facet.facet, Facet.value).join(Facet, Facet.id == Tag.facet_id)
                .filter(Tag.status == "approved", Facet.status == "approved"))
        for ref, facet, value in rows:
            out.setdefault(ref, []).append(f"{facet}: {value}")
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        db.session.rollback()
    _CACHE.update(at=time.time(), stamp=s, tags=out)
    return out


def facets_of(refs: list[str]) -> dict[str, list[str]]:
    """The approved categories of these refs (a metric's are its family's)."""
    tags = _approved()
    out: dict[str, list[str]] = {}
    fam: dict[int, str] = {}
    objs = [int(r.split(":")[1]) for r in refs if re.match(r"^object:\d+$", base_ref(r))]
    if objs:
        from supagent.models import KObject

        for o in db.session.query(KObject).filter(KObject.id.in_(objs), KObject.kind == "metric"):
            fam[o.id] = family_of(o.source_id, o.name)
    for r in refs:
        b = base_ref(r)
        got = list(tags.get(b, []))
        m = re.match(r"^object:(\d+)$", b)
        if m and int(m.group(1)) in fam:
            got += tags.get(fam[int(m.group(1))], [])
        if got:
            out[r] = sorted(set(got))
    return out


def links_of(refs: list[str]) -> list[dict[str, Any]]:
    """The approved relations of these items (both ways)."""
    from sqlalchemy import or_

    from supagent.models import Link

    bases = sorted({base_ref(r) for r in refs})
    if not bases:
        return []
    rows = (db.session.query(Link).filter(Link.status == "approved",
                                          or_(Link.a_ref.in_(bases), Link.b_ref.in_(bases))).limit(200))
    return [{"a": x.a_ref, "b": x.b_ref, "kind": x.kind} for x in rows]


def review_counts() -> dict[str, int]:
    """What waits for an admin: proposed values, tags and links."""
    from supagent.models import Facet, Link, Tag

    try:
        return {"values": db.session.query(Facet).filter(Facet.status == "proposed").count(),
                "tags": db.session.query(Tag).filter(Tag.status == "proposed").count(),
                "links": db.session.query(Link).filter(Link.status == "proposed").count()}
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return {"values": 0, "tags": 0, "links": 0}
