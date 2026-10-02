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
from typing import Any, Callable, Iterator

from superset import db

log = logging.getLogger(__name__)

FACETS = ("aspect", "subject", "application", "component")
BUILTIN = ("subject", "application", "component")
NAME_OK = re.compile(r"^[a-z][a-z0-9_ ]{1,23}$")


def editable() -> tuple[str, ...]:
    """The categories an admin adds values to and moves values between: subject, application, component and the
    deployment's own (categories.custom, and those categories.fields reads from the data)."""
    from supagent import settings

    extra: list[str] = []
    try:
        custom, fields = settings.get("categories.custom"), settings.get("categories.fields")
        extra += [str(x).strip().lower() for x in (custom if isinstance(custom, (list, tuple)) else [])]
        extra += [str(x).strip().lower() for x in (fields if isinstance(fields, dict) else {})]
    except Exception:  # pylint: disable=broad-except   (no settings table yet)
        pass
    return BUILTIN + tuple(dict.fromkeys(x for x in extra if NAME_OK.match(x) and x not in BUILTIN + ("aspect",)))


def field_rules() -> list[tuple[str, Any]]:
    """(category, compiled regex of field names) from categories.fields (bad patterns left out)."""
    from supagent import settings

    out = []
    fields = settings.get("categories.fields")
    for cat, rx in (fields if isinstance(fields, dict) else {}).items():
        cat = str(cat).strip().lower()
        if cat not in editable():
            continue
        try:
            out.append((cat, re.compile(str(rx), re.I)))
        except re.error:
            log.warning("supagent categories: bad field pattern for %s: %r", cat, rx)
    return out


def review_all() -> bool:
    """categories.review_all: nothing the LLM finds is used before an admin approves it."""
    from supagent import settings

    try:
        return bool(settings.get("categories.review_all"))
    except Exception:  # pylint: disable=broad-except
        return False


def rank(category: str) -> int:
    """Wider first: subject, application, component, then the deployment's own in their order."""
    cats = list(editable())
    return cats.index(category) if category in cats else len(cats)


EDITABLE = BUILTIN       # the built-in ones; editable() adds the deployment's own
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
def _ensure(facet: str, value: str, status: str, source: str, description: str | None = None,
            origin: str | None = None) -> Any:
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
    if origin and origin not in (f.origins or []):
        f.origins = ((f.origins or []) + [origin])[-10:]          # where it comes from, the latest ten
    return f


def merge_value(f: Any, to: Any) -> None:
    """Value f joins value `to` (of the same category or another): its items move there (once each), its name
    becomes one of the other's names, f goes. Into an approved value, the items the LLM was sure of are used at
    once, as when a value is approved (they were waiting for f's approval; the unsure ones stay to review): the
    merged value's count follows at once."""
    from supagent.models import Tag

    for t in db.session.query(Tag).filter(Tag.facet_id == f.id):
        if db.session.query(Tag).filter(Tag.ref == t.ref, Tag.facet_id == to.id).first() is None:
            t.facet_id = to.id
        else:
            db.session.delete(t)
    db.session.flush()
    if to.status == "approved" and not review_all():
        for t in db.session.query(Tag).filter(Tag.facet_id == to.id, Tag.status == "proposed"):
            if (t.confidence or 0) >= CONFIDENT:
                t.status = "approved"
    names = set(to.synonyms or []) | set(f.synonyms or [])
    if f.value.lower() != to.value.lower():
        names.add(f.value)
    to.synonyms = sorted(names)
    if not to.description and f.description:
        to.description = f.description
    # what f was part of, `to` is now; what was part of f is part of `to`
    to.parents = [p for p in dict.fromkeys(list(to.parents or []) + list(f.parents or [])) if p not in (f.id, to.id)] or None
    from supagent.models import Facet

    for other in db.session.query(Facet).filter(Facet.id.notin_([f.id, to.id])):
        if other.parents and f.id in other.parents:
            other.parents = [p for p in dict.fromkeys(to.id if p == f.id else p for p in other.parents)
                             if p != other.id] or None
    db.session.delete(f)


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
        n["subject"] += _ensure("subject", c, "approved", "seed", origin="a category of the catalog") is not None
    from supagent import settings
    from supagent.models import Source

    cap = int(settings.get("categories.max_values") or 1000)
    names = {s.id: s.database_name for s in db.session.query(Source)} if db.session.query(Source).count() else {}
    rules = field_rules()
    for o in db.session.query(KObject).filter(KObject.kind.in_(("field", "label")), KObject.gone_at.is_(None)):
        for cat, rx in rules:
            if not rx.match(o.name or ""):
                continue
            values = (o.stats or {}).get("values") or []
            most = MAX_DATA_VALUES if cat == "application" else cap
            if not (0 < len(values) <= most) or (o.stats or {}).get("partial"):
                continue
            where = f"field {o.name} of {o.parent}" + (f" ({names[o.source_id]})" if o.source_id in names else "")
            for v in values:
                if named(str(v)):
                    n[cat] = n.get(cat, 0) + (_ensure(cat, str(v), "approved", "data", origin=where) is not None)
    n["relations"] = relations_from_data()
    db.session.commit()
    return n


def relations_from_data() -> int:
    """Two category fields of one index (APPLICATION and NODE...): the values that go together in its documents
    (the profile's sample) are proposed as one part of the other, the wider category above (an application above
    its servers), to approve in To review; never applied without an admin."""
    from supagent import settings
    from supagent.models import Facet, KObject

    least = int(settings.get("categories.relation_min_docs") or 5)
    values: dict[tuple[str, str], Any] = {}
    for f in db.session.query(Facet).filter(Facet.status != "rejected", Facet.facet.in_(list(editable()))):
        values.setdefault((f.facet, f.value.lower()), f)
    n = 0
    for idx in db.session.query(KObject).filter(KObject.kind == "index", KObject.gone_at.is_(None)):
        for grp in (idx.stats or {}).get("category_pairs") or []:
            (pcat, pfield), (ccat, cfield) = grp.get("parent") or ("", ""), grp.get("child") or ("", "")
            for pv, cv, docs in (grp.get("pairs") or [])[:5000]:
                if docs < least:
                    continue
                parent, child = values.get((pcat, str(pv).lower())), values.get((ccat, str(cv).lower()))
                if parent is None or child is None or parent.id == child.id or parent.id in (child.parents or []):
                    continue
                sug = dict(child.suggested or {})
                if parent.id in (sug.get("parents") or []) or parent.id in (sug.get("declined") or []):
                    continue
                sug["parents"] = list(sug.get("parents") or []) + [parent.id]
                sug.setdefault("from", {})[str(parent.id)] = f"{idx.name}: {pfield} {pv} with {cfield} {cv} in {docs} documents"
                child.suggested = sug
                n += 1
    return n


def vocabulary(with_proposed: bool = True) -> dict[str, list[dict[str, Any]]]:
    from supagent.models import Facet

    q = db.session.query(Facet).filter(Facet.status.in_(("approved", "proposed") if with_proposed else ("approved",)))
    out: dict[str, list[dict[str, Any]]] = {f: [] for f in ("aspect",) + editable()}
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
    from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Note, Recipe

    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True)):
        yield {"ref": f"entry:{e.id}", "kind": e.classification, "title": e.title, "text": e.content or ""}
    for m in db.session.query(Memory).filter(Memory.status == "active", Memory.scope == "team"):
        yield {"ref": f"memory:{m.id}", "kind": "memory", "title": (m.text or "")[:80], "text": m.text or ""}
    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.content.isnot(None)):
        yield {"ref": f"doc:{d.id}", "kind": "document", "title": d.title or d.url or "", "text": d.content or ""}
    for n in db.session.query(Note).filter(Note.scope == "team"):          # a personal note stays its author's
        yield {"ref": f"note:{n.id}", "kind": "team note", "title": n.title or "", "text": n.text or ""}
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
            "value": {"type": "string"}, "description": {"type": "string"},
            "part_of": {"type": "array", "items": {"type": "string"},
                        "description": "known values this new one is part of (the applications a component belongs to, "
                                       "the subject of an application); several when it is shared"},
            "same_as": {"type": "string", "description": "a known value naming the same thing, if any"}},
            "required": ["facet", "value"]}},
        "known_value_parents": {"type": "array", "description": "what the texts say about known values: one is part of "
                                "others (a component of two applications)", "items": {"type": "object", "properties": {
                                    "value": {"type": "string"}, "part_of": {"type": "array", "items": {"type": "string"}}},
                                    "required": ["value", "part_of"]}}},
        "required": ["items"]}}}

CLASSIFY_SYSTEM = ("You classify pieces of knowledge of a company's data platform (catalog entries, notes, documents, "
                   "memories, learned answers, tables, metric families) so that questions reach the right ones. For "
                   "each item: aspect (functional: what the business means and counts; technical: how the systems "
                   "work; both), up to 3 subjects, the applications and the components it is about, and the "
                   "relations its text states to other items or tables (about, depends_on, part_of, explains, "
                   "runs_on). A component is a part of the systems (a service, a job or batch, a report, a "
                   "pipeline, a group of servers), never the name of a table, index, metric or field: those are "
                   "relations (about). Use the known values below, written exactly; when none fits, add a new value "
                   "in new_values with a short description (a general subject, never a single record), the known "
                   "values it is part of (a component of one application or several, an application of a subject) "
                   "and, when it names the same thing as a known value, that value (same_as). When a text says a "
                   "known value is part of other known values, give it in known_value_parents. Only what the text "
                   "says. Call classify_items once.")


def classify_tool() -> dict[str, Any]:
    """The tool with the deployment's own categories (categories.custom / categories.fields) as well."""
    import copy

    tool = copy.deepcopy(CLASSIFY_TOOL)
    props = tool["function"]["parameters"]["properties"]
    cats = list(editable())
    props["new_values"]["items"]["properties"]["facet"]["enum"] = cats
    own = [c for c in cats if c not in BUILTIN]
    if own:
        props["items"]["items"]["properties"]["others"] = {
            "type": "array", "description": "values of the other categories: " + ", ".join(own),
            "items": {"type": "object", "properties": {"category": {"type": "string", "enum": own},
                                                       "value": {"type": "string"}}, "required": ["category", "value"]}}
    return tool


def classify_messages(batch: list[dict[str, Any]], vocab: dict[str, list[dict[str, Any]]],
                      tables: list[str]) -> list[dict[str, str]]:
    lines = ["Known values:"]
    for facet in editable():
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
    strict = review_all()

    def data_name(facet: str, value: str) -> str | None:     # a "component" that is a table's name: a link
        return table_refs.get(value.strip().lower()) if facet == "component" else None

    def known(name: Any, not_id: int | None = None) -> Any:
        """A known value (any editable category, not retired) by its name or one of its other names."""
        name = " ".join(str(name or "").split())
        if not name:
            return None
        rows = db.session.query(Facet).filter(Facet.facet.in_(list(editable())), Facet.status != "rejected")
        hit = rows.filter(Facet.value.ilike(name)).order_by(Facet.status).first()
        if hit is None:
            low = name.lower()
            hit = next((x for x in rows if any(str(s).lower() == low for s in (x.synonyms or []))), None)
        return None if hit is None or hit.id == not_id else hit

    for nv in args.get("new_values") or []:
        if data_name(str(nv.get("facet") or ""), str(nv.get("value") or "")):
            continue
        if nv.get("facet") in editable() and str(nv.get("value") or "").strip():
            f = _ensure(nv["facet"], nv["value"], "proposed", "llm", str(nv.get("description") or "")[:500] or None)
            if f is not None and f.status == "proposed":
                n["proposed"] += 1
                if not f.origins:
                    f.origins = ["proposed by the LLM from the texts it read"]
                parents = [p.id for p in (known(x, f.id) for x in (nv.get("part_of") or [])[:6]) if p is not None]
                if parents:                           # shown with the value in the review, approved with it
                    f.parents = list(dict.fromkeys(list(f.parents or []) + parents))
                same = known(nv.get("same_as"), f.id) if nv.get("same_as") else None
                if same is not None and same.facet == f.facet:
                    f.suggested = {**(f.suggested or {}), "same_as": same.id}     # one click: merge
    for kv in args.get("known_value_parents") or []:  # known values said part of others: an admin decides
        f = known(kv.get("value"))
        if f is None:
            continue
        have = set(f.parents or [])
        new = [p.id for p in (known(x, f.id) for x in (kv.get("part_of") or [])[:6])
               if p is not None and p.id not in have]
        if not new:
            continue
        if f.status == "proposed":                    # waiting anyway: with the value
            f.parents = list(dict.fromkeys(list(f.parents or []) + new))
        else:
            old = (f.suggested or {}).get("parents") or []
            f.suggested = {**(f.suggested or {}), "parents": list(dict.fromkeys(list(old) + new))}
            n["relations"] = n.get("relations", 0) + 1
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
        own = set(editable()) - set(BUILTIN)
        for o in (row.get("others") or [])[:6]:            # the deployment's own categories (server, team...)
            if isinstance(o, dict) and o.get("category") in own and str(o.get("value") or "").strip():
                wanted.append((o["category"], str(o["value"])))
        for facet, value in wanted:
            f = (db.session.query(Facet).filter(Facet.facet == facet, Facet.value.ilike(value.strip())).first()
                 or _ensure(facet, value, "proposed", "llm"))
            if f is None or f.status == "rejected":
                continue
            t = db.session.query(Tag).filter(Tag.ref == ref, Tag.facet_id == f.id).first()
            if t is None:
                db.session.add(Tag(ref=ref, facet_id=f.id, confidence=conf, source="llm",
                                   status="approved" if f.status == "approved" and conf >= CONFIDENT and not strict
                                   else "proposed"))
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
                                    status="approved" if conf >= CONFIDENT and kind not in REVIEWED_LINKS and not strict
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
    if review_all():                                  # nothing used before an admin approves it
        db.session.commit()
        return out
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
            msg = llm.chat(classify_messages(batch, vocabulary(), tables), tools=[classify_tool()], max_tokens=2500)
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent facets: classification failed: %s", str(ex)[:300])
            out["error"] = str(ex)[:300]
            break
        out["calls"] += 1
        n = apply(_args(msg), batch, table_refs)
        out["items"] += len(batch)
        for k, v in n.items():
            out[k] = out.get(k, 0) + v
    if out["tags"] or out["links"]:
        from supagent.knowledge.freshness import touch

        touch()
        db.session.commit()
    return out


# --------------------------------------------------------------------------------------------- #
# reading them
# --------------------------------------------------------------------------------------------- #
_CACHE: dict[str, Any] = {"at": 0.0, "stamp": None, "tags": {}}


def lineage(fid: int, values: dict[int, Any], depth: int = 4) -> list[int]:
    """The value and every approved value it is part of (its parents, theirs... `depth` levels up), each once,
    cycles ignored."""
    out: list[int] = []
    level = [fid]
    for _ in range(depth + 1):
        nxt: list[int] = []
        for i in level:
            v = values.get(i)
            if v is None or i in out:
                continue
            out.append(i)
            nxt += [int(x) for x in (v.parents or []) if str(x).isdigit()]
        if not nxt:
            break
        level = nxt
    return out


def _approved() -> dict[str, list[str]]:
    """ref -> ["facet: value", ...] of the approved tags (cached, reloaded when the knowledge changes); an item
    tagged with a value is also about each value that one is part of (a component of two applications: both)."""
    from supagent.knowledge.freshness import stamp
    from supagent.models import Facet, Tag

    s = stamp()
    if _CACHE["stamp"] == s and time.time() - _CACHE["at"] < 300:
        return _CACHE["tags"]
    out: dict[str, list[str]] = {}
    try:
        values = {f.id: f for f in db.session.query(Facet).filter(Facet.status == "approved")}
        rows = db.session.query(Tag.ref, Tag.facet_id).filter(Tag.status == "approved")
        for ref, fid in rows:
            for i in lineage(fid, values):
                out.setdefault(ref, []).append(f"{values[i].facet}: {values[i].value}")
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        db.session.rollback()
    _CACHE.update(at=time.time(), stamp=s, tags=out)
    return out


def _chunks(ids: list[int], size: int = 500) -> Iterator[list[int]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def categories_info() -> list[dict[str, Any]]:
    """The categories (subject, application, component, then the deployment's own) with what each holds: its
    values in use or proposed, the items they are given to, the "part of" that name them, the interactions drawn
    with them. What removing one takes is said from these."""
    from sqlalchemy import func

    from supagent import settings
    from supagent.models import Facet, Link, Tag

    fields = settings.get("categories.fields")
    fields = {str(k).strip().lower(): str(v) for k, v in (fields if isinstance(fields, dict) else {}).items()}
    info = {c: {"name": c, "builtin": c in BUILTIN, "fields": fields.get(c) or "", "values": 0, "tags": 0, "parts": 0,
                "interactions": 0} for c in editable()}
    cat_of: dict[int, str] = {}
    rows = db.session.query(Facet.id, Facet.facet, Facet.status, Facet.parents).filter(Facet.facet.in_(list(info))).all()
    for fid, cat, _status, _parents in rows:
        cat_of[fid] = cat
    for fid, cat, status, parents in rows:
        if status != "rejected":
            info[cat]["values"] += 1
        for p in parents or []:
            info[cat]["parts"] += 1
            if p in cat_of and cat_of[p] != cat:
                info[cat_of[p]]["parts"] += 1                 # named as what a value of another category is part of
    for cat, n in db.session.query(Facet.facet, func.count(Tag.id)).join(Tag, Tag.facet_id == Facet.id).filter(
            Facet.facet.in_(list(info))).group_by(Facet.facet):
        info[cat]["tags"] = int(n)
    for a, b in db.session.query(Link.a_ref, Link.b_ref).filter(Link.a_ref.like("facet:%"), Link.b_ref.like("facet:%")):
        cats = {cat_of.get(int(r.split(":", 1)[1])) for r in (a, b) if r.split(":", 1)[1].isdigit()}
        for cat in cats - {None}:
            info[cat]["interactions"] += 1
    return list(info.values())


def _own(name: str) -> str:
    """The name of a category of the deployment's own, as it is kept (an error: built in, unknown)."""
    name = " ".join(str(name or "").lower().split())
    if name in BUILTIN or name == "aspect":
        raise ValueError(f"{name} is built in: it is not renamed nor removed (its field names can change)")
    if name not in editable():
        raise ValueError(f"no category {name!r}")
    return name


def _set_categories(change: Callable[[list[str], dict[str, str]], None], by: str) -> None:
    from supagent import settings

    custom = [str(x).strip().lower() for x in (settings.get("categories.custom") or [])]
    fields = settings.get("categories.fields")
    fields = {str(k).strip().lower(): str(v) for k, v in (fields if isinstance(fields, dict) else {}).items()}
    change(custom, fields)
    settings.set_value("categories.custom", list(dict.fromkeys(custom)), by=by)
    settings.set_value("categories.fields", fields, by=by)


def rename_category(old: str, new: str, by: str) -> str:
    """A category of the deployment's own under another name: its values follow, with everything given to them
    (their ids do not change), the field names it reads, its fold on the map."""
    from supagent.knowledge import sysmap
    from supagent.knowledge.freshness import touch
    from supagent.models import Facet

    old = _own(old)
    new = " ".join(str(new or "").lower().split())
    if not NAME_OK.match(new) or new == "aspect":
        raise ValueError("a name of 2 to 24 letters, digits, spaces or _ (not aspect)")
    if new == old:
        return old
    if new in editable() or db.session.query(Facet.id).filter(Facet.facet == new).first() is not None:
        raise ValueError(f"the category {new!r} exists already: move the values there (Edit), then remove this one")
    db.session.query(Facet).filter(Facet.facet == old).update({"facet": new}, synchronize_session=False)

    def change(custom: list[str], fields: dict[str, str]) -> None:
        if old in custom:
            custom[custom.index(old)] = new
        else:
            custom.append(new)
        if old in fields:
            fields[new] = fields.pop(old)

    _set_categories(change, by)
    lay = sysmap.layout()
    if old in (lay.get("folded") or {}):
        lay["folded"][new] = lay["folded"].pop(old)
        sysmap.save_layout(lay, by)
    touch()
    db.session.commit()
    return new


def remove_category(name: str, by: str) -> dict[str, int]:
    """A category of the deployment's own goes with everything that names it: its values (the retired ones too),
    the items they were given to, the "part of" that name them (other values' and the LLM's suggestions), the
    interactions drawn with them, their places on the map; then its name and its field names in the settings.
    The built-in ones (subject, application, component) stay. What went, counted."""
    from sqlalchemy import or_

    from supagent.knowledge import sysmap
    from supagent.knowledge.freshness import touch
    from supagent.models import Facet, Link, Tag

    name = _own(name)
    ids = [i for (i,) in db.session.query(Facet.id).filter(Facet.facet == name)]
    gone = set(ids)
    out = {"values": len(ids), "tags": 0, "parts": 0, "interactions": 0}
    if ids:
        for part in _chunks(ids):
            out["tags"] += db.session.query(Tag).filter(Tag.facet_id.in_(part)).delete(synchronize_session=False)
            refs = [f"facet:{i}" for i in part]
            out["interactions"] += db.session.query(Link).filter(
                or_(Link.a_ref.in_(refs), Link.b_ref.in_(refs))).delete(synchronize_session=False)
        for f in db.session.query(Facet):
            if f.id in gone:
                out["parts"] += len(f.parents or [])
                continue
            if f.parents and any(p in gone for p in f.parents):
                kept = [p for p in f.parents if p not in gone]
                out["parts"] += len(f.parents) - len(kept)
                f.parents = kept or None
            sug = dict(f.suggested or {})
            before = json.dumps(sug, sort_keys=True, default=str)
            for key in ("parents", "declined"):
                if sug.get(key):
                    sug[key] = [p for p in sug[key] if p not in gone]
                    if not sug[key]:
                        sug.pop(key)
            if sug.get("same_as") in gone:
                sug.pop("same_as")
            if isinstance(sug.get("from"), dict):
                sug["from"] = {k: v for k, v in sug["from"].items() if not (str(k).isdigit() and int(k) in gone)}
                if not sug["from"] or not sug.get("parents"):
                    sug.pop("from")
            if json.dumps(sug, sort_keys=True, default=str) != before:
                f.suggested = sug or None
        db.session.flush()
        for part in _chunks(ids):
            db.session.query(Facet).filter(Facet.id.in_(part)).delete(synchronize_session=False)

    def change(custom: list[str], fields: dict[str, str]) -> None:
        while name in custom:
            custom.remove(name)
        fields.pop(name, None)

    _set_categories(change, by)
    lay = sysmap.layout()
    places = {k: v for k, v in (lay.get("positions") or {}).items() if not (str(k).isdigit() and int(k) in gone)}
    hidden = [i for i in lay.get("hidden") or [] if i not in gone]
    folded = {k: v for k, v in (lay.get("folded") or {}).items() if k != name}
    if lay and (places != (lay.get("positions") or {}) or hidden != (lay.get("hidden") or []) or folded != (lay.get("folded") or {})):
        sysmap.save_layout({"positions": places, "hidden": hidden, "folded": folded}, by)
    touch()
    db.session.commit()
    return out


def clean_parents(fid: int | None, wanted: Any) -> list[int]:
    """The parents asked for a value (ids or "1,2"), kept when they are other values of the editable categories
    that are not retired; the order kept, each once."""
    from supagent.models import Facet

    raw = wanted if isinstance(wanted, list) else str(wanted or "").split(",")
    ids = list(dict.fromkeys(int(x) for x in raw if str(x).strip().isdigit() and int(x) != fid))
    if not ids:
        return []
    ok = {f.id for f in db.session.query(Facet).filter(Facet.id.in_(ids), Facet.facet.in_(list(editable())),
                                                       Facet.status != "rejected")}
    return [i for i in ids if i in ok]


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


REF_KINDS = {"entry": "catalog entries", "memory": "team memories", "doc": "documents", "note": "notes",
             "context": "Context pages", "recipe": "learned answers", "family": "metric families"}


def system_map() -> list[dict[str, Any]]:
    """The system picture from the categories: every approved value, what it is part of (several: a jvm of two
    applications and four components), and the items about it that exist now, by kind (a metric removed, a note
    deleted: no longer counted; a value with none says so). The page draws it as a tree from the values that are
    part of nothing."""
    from supagent.models import Chunk, Facet, KObject, Tag

    values = {f.id: f for f in db.session.query(Facet).filter(Facet.status == "approved", Facet.facet.in_(list(editable())))}
    if not values:
        return []
    refs: dict[int, set[str]] = {}
    for ref, fid in db.session.query(Tag.ref, Tag.facet_id).filter(Tag.status == "approved",
                                                                   Tag.facet_id.in_(list(values))):
        refs.setdefault(fid, set()).add(base_ref(ref))
    every = {r for rs in refs.values() for r in rs}
    obj_ids = [int(r.split(":")[1]) for r in every if re.match(r"^object:\d+$", r)]
    # the columns only: a platform has tens of thousands of metrics, each with its statistics (the page waited on them)
    objs = {o.id: o for o in db.session.query(KObject.id, KObject.kind, KObject.gone_at).filter(
        KObject.id.in_(obj_ids or [-1]))}
    live_families = set()
    if any(r.startswith("family:") for r in every):
        for source_id, name in db.session.query(KObject.source_id, KObject.name).filter(
                KObject.kind == "metric", KObject.gone_at.is_(None)):
            live_families.add(family_of(source_id, name))
    pieces = {r.split("#", 1)[0] for (r,) in db.session.query(Chunk.ref).filter(
        Chunk.ref.notlike("object:%"), Chunk.ref.notlike("superset:%"))}

    def kind_if_live(r: str) -> str | None:
        k, _, rest = r.partition(":")
        if k == "object":
            o = objs.get(int(rest)) if rest.isdigit() else None
            return None if o is None or o.gone_at is not None else {"metric": "metrics", "index": "indices"}.get(o.kind, o.kind + "s")
        if k == "family":
            return REF_KINDS[k] if r in live_families else None
        return REF_KINDS.get(k) if r in pieces else None

    out = []
    for f in sorted(values.values(), key=lambda x: (FACETS.index(x.facet) if x.facet in FACETS else 9, x.value.lower())):
        kinds: dict[str, int] = {}
        for r in refs.get(f.id, ()):
            k = kind_if_live(r)
            if k:
                kinds[k] = kinds.get(k, 0) + 1
        out.append({"id": f.id, "facet": f.facet, "value": f.value, "description": f.description,
                    "parents": [i for i in (f.parents or []) if i in values and i != f.id],
                    "items": sum(kinds.values()), "kinds": kinds, "tagged": len(refs.get(f.id, ()))})
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
