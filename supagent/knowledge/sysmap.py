"""The system map (0.8): the architecture of the system as the categories describe it, for the Data dictionary's
System map. Every approved value of the categories (subjects, applications, components, the deployment's own:
servers, environments...), what it is part of, the items about it that exist now, a short explanation (its
description, else the sentence of a document, a guide or a Context page that names it), and the interactions an
admin drew between values (depends on, sends data to, calls...: links between "facet:<id>" refs). The boxes' places
an admin moved are kept (Meta "system_map").

Who sees what: admins everything; another user the values that have an item they may see (an index or a metric of a
database they may query, a document, a guide, a Context page of their databases...) and what those are part of.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from superset import db

INTERACTIONS = {"depends_on": "depends on", "sends_to": "sends data to", "reads_from": "reads from", "calls": "calls",
                "runs_on": "runs on", "triggers": "triggers", "monitors": "monitors", "about": "relates to"}
LAYOUT_KEY = "system_map"
HINT_KINDS = ("doc", "guide", "context")
HINT_CHARS = 220
_HINTS: dict[str, Any] = {"stamp": None, "at": 0.0, "hints": {}}
_LOCK = threading.Lock()


CITES = re.compile(r"\s*\[(?:[A-Z]{1,3}\d+(?:,\s*)?)+\]")      # the Context's citations: [E1], [S2, S3]
MARKUP = re.compile(r"\*\*|__|`|^#+\s*|^[-*+]\s+|^\d+[.)]\s+")
SAYS = re.compile(r"^\W*(?:the\s+|a\s+|an\s+|le\s+|la\s+|les\s+|l')?$", re.I)


def _sentences(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        s = MARKUP.sub("", s.strip()).strip()
        if len(s) >= 12:
            out.append(s)
    return out


def _score(sentence: str, start: int, end: int, kind: str) -> float:
    """How well a sentence says what a part is: the part as its subject ("The X authorizes...", "X: the ..."), from a
    document or a guide rather than the AI-written Context, of a readable length."""
    score = 0.0
    if SAYS.match(sentence[:start]):
        score += 3.0                                     # it starts the sentence: the sentence is about it
    elif start <= 30:
        score += 1.0
    if re.match(r"\s*(?::|\(|\bis\b|\bare\b|\bdoes\b|\bhandles?\b|\bruns?\b|\btakes?\b|\bsends?\b|"
                r"\breads?\b|\bwrites?\b|\bbooks?\b|\bopens?\b|\bauthori[sz]es?\b|\bcomputes?\b|\bstores?\b|"
                r"\bmanages?\b|\bprovides?\b|\bcontains?\b)", sentence[end:], re.I):
        score += 2.0                                     # "X: ...", "X is ...", "X authorizes ..."
    score += {"doc": 1.0, "guide": 1.0}.get(kind, 0.0)
    if 40 <= len(sentence) <= HINT_CHARS:
        score += 0.5
    return score


def hints(values: dict[int, Any]) -> dict[int, list[tuple[str, str, str]]]:
    """For the values with no description: the sentences of the documents, guides and Context pages that say what
    they are {facet id: [(ref, title, sentence)]} (the 3 best each: the part as the sentence's subject, a document
    before the AI-written Context), read once per state of the knowledge."""
    from supagent.knowledge.freshness import stamp
    from supagent.models import Chunk

    s = stamp()
    with _LOCK:
        if _HINTS["stamp"] == s and time.time() - _HINTS["at"] < 600:
            cached = _HINTS["hints"]
            if all(fid in cached or values[fid].description for fid in values):
                return cached
    wanted = {fid: v.value for fid, v in values.items() if not (v.description or "").strip() and len(v.value) >= 3}
    found: dict[int, list[tuple[float, str, str, str]]] = {}
    if wanted:
        by_name: dict[str, list[int]] = {}
        for fid, name in wanted.items():
            by_name.setdefault(name.lower(), []).append(fid)
        names = sorted(by_name, key=len, reverse=True)
        rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in names[:2000]) + r")(?![\w-])", re.I)
        t0 = time.time()
        q = db.session.query(Chunk.ref, Chunk.kind, Chunk.title, Chunk.text).filter(Chunk.kind.in_(HINT_KINDS)).yield_per(500)
        for ref, kind, title, text in q:
            if time.time() - t0 > 8:                     # the page answers; the next load goes on
                break
            for sentence in _sentences(text or ""):
                sentence = CITES.sub("", sentence).strip()
                named = {m.group(1).lower() for m in rx.finditer(sentence)}
                if len(named) >= 3 or sentence.count(",") >= 5:
                    continue                             # a list of parts: it says what they are, not what one is
                if len(sentence) < 25 or sentence.strip(" .:").lower() in named:
                    continue                             # a heading, the name alone: no explanation
                for m in rx.finditer(sentence):
                    for fid in by_name.get(m.group(1).lower(), []):
                        got = found.setdefault(fid, [])
                        if any(x[1] == ref and x[3] == sentence for x in got):
                            continue
                        short = sentence[:HINT_CHARS] + ("…" if len(sentence) > HINT_CHARS else "")
                        got.append((_score(sentence, m.start(), m.end(), kind or ""), ref, title or ref, short))
        db.session.commit()
    out = {fid: [(ref, title, sentence) for _sc, ref, title, sentence in sorted(got, key=lambda x: -x[0])[:3]]
           for fid, got in found.items()}
    with _LOCK:
        _HINTS.update(stamp=s, at=time.time(), hints=out)
    return out


def _visible_refs(refs: set[str]) -> set[str]:
    """The refs of these items the current user may see (see the module)."""
    from supagent.knowledge.context import visible_pages
    from supagent.knowledge.curated import sources_of_user
    from supagent.models import Entry, KObject, Note, Recipe
    from supagent.security import visible_databases

    out: set[str] = set()
    by_kind: dict[str, set[int]] = {}
    for r in refs:
        k, _, rest = r.partition(":")
        ident = rest.split("#", 1)[0]
        if ident.isdigit():
            by_kind.setdefault(k, set()).add(int(ident))
        elif k == "family":
            out.add(r)                                     # checked with the objects of its database below
    sources = {s.id for s in sources_of_user()}
    dbs = visible_databases()
    if by_kind.get("object"):
        out |= {f"object:{i}" for (i,) in db.session.query(KObject.id).filter(
            KObject.id.in_(list(by_kind["object"])), KObject.source_id.in_(list(sources) or [-1]))}
    fam = {r for r in out if r.startswith("family:")}
    for r in fam:                                          # family:<source id>:<prefix>
        sid = r.split(":")[1]
        if not (sid.isdigit() and int(sid) in sources):
            out.discard(r)
    if by_kind.get("context"):
        pages = {p.id for p in visible_pages()}
        out |= {f"context:{i}" for i in by_kind["context"] if i in pages}
    if by_kind.get("entry"):
        for e in db.session.query(Entry).filter(Entry.id.in_(list(by_kind["entry"])), Entry.deleted_at.is_(None)):
            database_id = (e.evidence or {}).get("database_id")
            if database_id is None or int(database_id) in dbs:
                out.add(f"entry:{e.id}")
    out |= {f"doc:{i}" for i in by_kind.get("doc", ())}
    if by_kind.get("note"):
        out |= {f"note:{i}" for (i,) in db.session.query(Note.id).filter(Note.id.in_(list(by_kind["note"])),
                                                                         Note.scope == "team")}
    if by_kind.get("recipe"):
        out |= {f"recipe:{i}" for (i,) in db.session.query(Recipe.id).filter(
            Recipe.id.in_(list(by_kind["recipe"])), Recipe.database_id.in_(list(dbs) or [-1]))}
    if by_kind.get("memory"):
        from supagent.models import Memory

        out |= {f"memory:{i}" for (i,) in db.session.query(Memory.id).filter(Memory.id.in_(list(by_kind["memory"])),
                                                                             Memory.scope == "team")}
    return out


def layout() -> dict[str, Any]:
    from supagent.models import Meta

    row = db.session.get(Meta, LAYOUT_KEY)
    try:
        data = json.loads(row.value) if row is not None and row.value else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def save_layout(data: dict[str, Any], by: str) -> dict[str, Any]:
    """An admin's places of the boxes ({"positions": {id: [x, y]}, "hidden": [ids], "folded": {category: bool}})."""
    import datetime as dt

    from supagent.models import Meta

    pos = {}
    for k, v in (data.get("positions") or {}).items():
        if str(k).isdigit() and isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                pos[str(int(k))] = [round(float(v[0]), 1), round(float(v[1]), 1)]
            except (TypeError, ValueError):
                continue
    hidden = sorted({int(x) for x in data.get("hidden") or [] if str(x).isdigit()})
    folded = {str(k)[:24]: bool(v) for k, v in (data.get("folded") or {}).items()}
    out = {"positions": pos, "hidden": hidden, "folded": folded, "updated_by": by,
           "updated_at": dt.datetime.utcnow().isoformat(timespec="seconds")}
    row = db.session.get(Meta, LAYOUT_KEY)
    if row is None:
        db.session.add(Meta(key=LAYOUT_KEY, value=json.dumps(out)))
    else:
        row.value = json.dumps(out)
    db.session.commit()
    return out


def interactions() -> list[dict[str, Any]]:
    from supagent.models import Link

    rows = (db.session.query(Link).filter(Link.a_ref.like("facet:%"), Link.b_ref.like("facet:%"),
                                          Link.status != "rejected").order_by(Link.id))
    out = []
    for x in rows:
        a, b = x.a_ref.split(":", 1)[1], x.b_ref.split(":", 1)[1]
        if a.isdigit() and b.isdigit():
            out.append({"id": x.id, "a": int(a), "b": int(b), "kind": x.kind, "label": INTERACTIONS.get(x.kind, x.kind),
                        "note": x.note or "", "status": x.status, "source": x.source})
    return out


def save_interaction(a: int, b: int, kind: str, note: str, by: str, link_id: int | None = None) -> dict[str, Any]:
    """An admin draws (or changes) an interaction between two values: approved at once."""
    from supagent.models import Facet, Link

    if kind not in INTERACTIONS:
        raise ValueError("kind: " + ", ".join(INTERACTIONS))
    if a == b:
        raise ValueError("an interaction links two different parts")
    fa, fb = db.session.get(Facet, int(a)), db.session.get(Facet, int(b))
    if fa is None or fb is None or fa.status != "approved" or fb.status != "approved":
        raise ValueError("both parts must be approved values of the categories")
    x = db.session.get(Link, link_id) if link_id else None
    if x is None:
        x = (db.session.query(Link).filter(Link.a_ref == f"facet:{a}", Link.b_ref == f"facet:{b}", Link.kind == kind)
             .first())
    if x is None:
        x = Link(a_ref=f"facet:{a}", b_ref=f"facet:{b}", kind=kind)
        db.session.add(x)
    x.a_ref, x.b_ref, x.kind = f"facet:{a}", f"facet:{b}", kind
    x.note = (note or "").strip()[:500] or None
    x.status, x.source, x.reviewed_by, x.confidence = "approved", "admin", by, 1.0
    db.session.commit()
    return {"id": x.id, "a": a, "b": b, "kind": kind, "label": INTERACTIONS[kind], "note": x.note or ""}


def delete_interaction(link_id: int) -> bool:
    from supagent.models import Link

    x = db.session.get(Link, link_id)
    if x is None or not (x.a_ref.startswith("facet:") and x.b_ref.startswith("facet:")):
        return False
    db.session.delete(x)
    db.session.commit()
    return True


def map_data(admin: bool) -> dict[str, Any]:
    """The map as the page draws it (see the module)."""
    from supagent.knowledge.facets import base_ref, editable, rank, system_map
    from supagent.models import Facet, Tag

    values = system_map()
    cats = list(editable())
    if not admin and values:
        ids = [v["id"] for v in values]
        refs: dict[int, set[str]] = {}
        for ref, fid in db.session.query(Tag.ref, Tag.facet_id).filter(Tag.status == "approved", Tag.facet_id.in_(ids)):
            refs.setdefault(fid, set()).add(base_ref(ref))
        seen = _visible_refs({r for rs in refs.values() for r in rs})
        keep = {fid for fid, rs in refs.items() if rs & seen}
        by_id = {v["id"]: v for v in values}
        todo = list(keep)
        while todo:                                         # what a visible value is part of: shown too
            v = by_id.get(todo.pop())
            for p in (v or {}).get("parents") or []:
                if p not in keep and p in by_id:
                    keep.add(p)
                    todo.append(p)
        values = [v for v in values if v["id"] in keep]
    facets = {f.id: f for f in db.session.query(Facet).filter(Facet.id.in_([v["id"] for v in values] or [-1]))}
    found = hints({fid: f for fid, f in facets.items()})
    pages_ok = None
    for v in values:
        f = facets.get(v["id"])
        v["origins"] = list((f.origins or []) if f is not None else [])[-3:]
        v["source"] = (f.source if f is not None else None) or ""
        v["synonyms"] = list((f.synonyms or []) if f is not None else [])
        v["gone"] = bool(v.get("tagged")) and not v.get("items")
        v["hint"] = None
        if not (v.get("description") or "").strip():
            for ref, title, sentence in found.get(v["id"], []):
                if not admin and ref.startswith("context:"):
                    if pages_ok is None:
                        from supagent.knowledge.context import visible_pages

                        pages_ok = {f"context:{p.id}" for p in visible_pages()}
                    if ref.split("#", 1)[0] not in pages_ok:
                        continue
                v["hint"] = {"text": sentence, "from": title, "ref": ref.split("#", 1)[0]}
                break
    shown = {v["id"] for v in values}
    links = [x for x in interactions() if x["a"] in shown and x["b"] in shown]
    counts: dict[str, int] = {}
    for v in values:
        counts[v["facet"]] = counts.get(v["facet"], 0) + 1
    order = sorted({v["facet"] for v in values} | set(cats), key=rank)
    return {"categories": [{"name": c, "count": counts.get(c, 0), "builtin": c in ("subject", "application", "component")}
                           for c in order if counts.get(c)],
            "values": values, "links": links, "layout": layout(), "interactions": INTERACTIONS, "is_admin": admin}
