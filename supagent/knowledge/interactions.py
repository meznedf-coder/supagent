"""The interactions the team's texts state, proposed for the System map (0.9).

An admin draws on the System map how the parts of the system interact (an application depends on another, runs
on a pool, reads a feed, calls a service): what an investigation follows. Most of it is already written in the
team's documents. With the daily learning, the LLM reads each document, guide, Context page and team note next
to the names of the known parts it mentions (the approved values of the categories) and says which of them
interact and how, each with the sentence that says so. A proposal is kept only when that sentence is in the
text, word for word, and the two parts are named there; it waits in To review, and is neither drawn nor used
before an admin approves it. One that was rejected is never proposed again. A text is read once (again when it
changes).

Each interaction is explained twice (0.9): a short explanation, a few words shown when the link is clicked on the
map ("waits for its positions"), and a long one, what to do when an investigation follows it (what to check on
the other part, what a problem there does here), given to the agent with the system around a question. The LLM
writes both with the proposal; for an interaction without them (one an admin drew, an older one), `explain`
writes them from what is known of the two parts, the sentence that states it and the Context pages that name
them. What an admin wrote is never written over.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from typing import Any, Iterator

from superset import db

log = logging.getLogger(__name__)
WINDOW = 5000             # characters of a text read in one call
WINDOWS = 4               # of one text per run (the rest at the next run)
NAMES = 60                # known parts given with a window
QUOTE_CHARS = 400
KINDS = ("depends_on", "runs_on", "reads_from", "sends_to", "calls", "triggers", "monitors")
SHORT_CHARS = 90
LONG_CHARS = 700
SHORT_SAYS = ("what the interaction is, in a few words, from the first part's side: what it waits for, reads, "
              "sends, uses (\"waits for the positions of its perimeter\", \"writes its results there\")")
LONG_SAYS = ("what an investigation does when it follows this interaction, in two or three sentences: what to check "
             "on the other part (its runs, its health, its delivery, its errors, in the same window and for the same "
             "perimeter), and what a problem there does here (late, slower, failing); what the text says only")
EXPLAIN_BATCH = 8         # interactions explained in one call
TOOL = {"type": "function", "function": {
    "name": "interactions",
    "description": "The interactions between parts of the system that the text states.",
    "parameters": {"type": "object", "properties": {"interactions": {"type": "array", "items": {
        "type": "object", "properties": {
            "from": {"type": "string", "description": "a known part, written exactly"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "to": {"type": "string", "description": "another known part, written exactly"},
            "quote": {"type": "string", "description": "the sentence of the text that says it, word for word"},
            "short": {"type": "string", "description": SHORT_SAYS},
            "long": {"type": "string", "description": LONG_SAYS}},
        "required": ["from", "kind", "to", "quote", "short", "long"]}}}, "required": ["interactions"]}}}
SYSTEM = ("You read a piece of a company's documentation to find how the parts of its systems interact, for an "
          "architecture map. The known parts are listed. Give every interaction between two known parts that the "
          "text states: from depends_on to (from needs to, waits for it), from runs_on to (from is executed on to), "
          "from reads_from to, from sends_to to (from writes or publishes data to to), from calls to (from uses the "
          "service to), from triggers to (from starts or releases to), from monitors to. Only between the known "
          "parts, written exactly as listed; only what the text says, never what is likely; with the sentence that "
          "says it, copied word for word. A part that only consists of another (a server of a pool) is no "
          "interaction. For each, a short explanation (a few words, what it waits for, reads, sends or uses) and "
          "a long one (what an investigation checks on the other part when it follows it, and what a problem there "
          "does here). Nothing stated: an empty list. Call interactions once.")


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def items() -> Iterator[dict[str, Any]]:
    """The texts that say how the system works: documents, guides, Context pages, the team's notes."""
    from supagent.models import ContextPage, Doc, Entry, Note

    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.content.isnot(None)):
        yield {"ref": f"doc:{d.id}", "title": d.title or d.url or "", "text": d.content or ""}
    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                            Entry.classification == "guide"):
        yield {"ref": f"entry:{e.id}", "title": e.title, "text": e.content or ""}
    for p in db.session.query(ContextPage):
        yield {"ref": f"context:{p.id}", "title": p.title, "text": p.content or ""}
    for n in db.session.query(Note).filter(Note.scope == "team"):
        yield {"ref": f"note:{n.id}", "title": n.title or "", "text": n.text or ""}


def pending(limit: int, again: bool = False) -> list[dict[str, Any]]:
    from supagent.knowledge.facets import _hash
    from supagent.models import Classified

    done = dict(db.session.query(Classified.ref, Classified.content_hash).filter(Classified.ref.like("%#links")))
    out = []
    for it in items():
        if len(it["text"]) < 80:
            continue
        it["hash"] = _hash(it["title"], it["text"])
        if again or done.get(it["ref"] + "#links") != it["hash"]:
            out.append(it)
            if len(out) >= limit:
                break
    return out


def windows(text: str) -> list[str]:
    """The text in pieces of about WINDOW characters, cut between paragraphs."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text or ""):
        if cur and len(cur) + len(para) > WINDOW:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + para
        while len(cur) > 2 * WINDOW:                # one paragraph longer than two windows
            out.append(cur[:WINDOW])
            cur = cur[WINDOW:]
    if cur.strip():
        out.append(cur)
    return out


def _args(msg: dict[str, Any]) -> list[dict[str, Any]]:
    args: Any = None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "interactions":
            args = (tc.get("function") or {}).get("arguments")
    if args is None:
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S), re.S)
        args = m.group(0) if m else "{}"
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    found = args.get("interactions") if isinstance(args, dict) else None
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []


def apply(found: list[dict[str, Any]], text: str, g: dict[str, Any], origin: str) -> int:
    """The LLM's interactions kept: both parts known and named in the text, the sentence in the text word for
    word, no such interaction already drawn, proposed or rejected."""
    from supagent.knowledge.brief import named
    from supagent.models import Link

    V, plain = g["values"], _norm(text)
    in_text = set(named(text, g))
    n = 0
    for x in found[:40]:
        kind = str(x.get("kind") or "")
        quote = " ".join(str(x.get("quote") or "").split())
        a = [i for i in g["names"].get(_norm(str(x.get("from") or "")), []) if i in in_text]
        b = [i for i in g["names"].get(_norm(str(x.get("to") or "")), []) if i in in_text]
        if kind not in KINDS or not a or not b or a[0] == b[0] or len(quote) < 15 or _norm(quote) not in plain:
            continue
        said = set(named(quote, g))
        if a[0] not in said and b[0] not in said:       # the sentence names neither: it says something else
            continue
        a_ref, b_ref = f"facet:{a[0]}", f"facet:{b[0]}"
        if db.session.query(Link.id).filter(Link.a_ref == a_ref, Link.b_ref == b_ref, Link.kind == kind).first():
            continue
        if b[0] in V[a[0]]["parents"] or a[0] in V[b[0]]["parents"]:
            continue                                     # one is part of the other: that is said already
        short = " ".join(str(x.get("short") or "").split())[:SHORT_CHARS] or None
        long_ = " ".join(str(x.get("long") or "").split())[:LONG_CHARS] or None
        db.session.add(Link(a_ref=a_ref, b_ref=b_ref, kind=kind, confidence=0.7, source="llm", status="proposed",
                            note=short, detail=long_, evidence=f"\"{quote[:QUOTE_CHARS]}\" ({origin})",
                            explained_by="llm" if short or long_ else None))
        n += 1
    return n


def run(llm: Any, seconds: float = 600.0, limit: int = 50, again: bool = False) -> dict[str, Any]:
    """The texts not read yet, until `seconds` or `limit`; the next run continues."""
    from supagent.knowledge.brief import _graph, named
    from supagent.knowledge.stopping import check
    from supagent.models import Classified

    t0 = time.time()
    out: dict[str, Any] = {"texts": 0, "calls": 0, "proposed": 0}
    g = _graph()
    if len(g["values"]) < 2:
        return out
    V = g["values"]
    for it in pending(limit, again):
        if time.time() - t0 > seconds:
            out["left"] = True
            break
        check()
        for piece in windows(it["text"])[:WINDOWS]:
            known = named(piece, g)[:NAMES]
            if len(known) < 2:                       # an interaction needs two parts
                continue
            parts = "\n".join(f"- {V[i]['name']} ({V[i]['cat']})" for i in known)
            try:
                msg = llm.chat([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": f"Known parts named in this text:\n{parts}\n\nText "
                                                             f"({it['title'][:120]}):\n{piece}"}],
                               tools=[TOOL], max_tokens=1800)
            except Exception as ex:  # pylint: disable=broad-except
                log.warning("supagent interactions: %s: %s", it["ref"], str(ex)[:300])
                out["error"] = str(ex)[:300]
                db.session.commit()
                return out
            out["calls"] += 1
            out["proposed"] += apply(_args(msg), piece, g, it["title"][:80] or it["ref"])
        c = db.session.get(Classified, it["ref"] + "#links") or Classified(ref=it["ref"] + "#links")
        c.content_hash, c.classified_at = it["hash"], dt.datetime.utcnow()
        db.session.merge(c)
        db.session.commit()
        out["texts"] += 1
    return out


EXPLAIN_TOOL = {"type": "function", "function": {
    "name": "explanations",
    "description": "The explanations of the interactions given, by their number.",
    "parameters": {"type": "object", "properties": {"explanations": {"type": "array", "items": {
        "type": "object", "properties": {
            "n": {"type": "integer", "description": "the number of the interaction"},
            "short": {"type": "string", "description": SHORT_SAYS},
            "long": {"type": "string", "description": LONG_SAYS}},
        "required": ["n", "short", "long"]}}}, "required": ["explanations"]}}}
EXPLAIN_SYSTEM = ("You explain the interactions of an architecture map to the people and the agent who investigate "
                  "problems on it. For each interaction (a first part, how it interacts, a second part, what is known "
                  "of both and the text that states it, when there is one), write a short explanation (a few words, "
                  "from the first part's side: what it waits for, reads, sends, uses) and a long one (two or three "
                  "sentences: what to check on the second part when an investigation follows this interaction, in "
                  "the same window and for the same perimeter, and what a problem there does to the first part). "
                  "Only what is known: no figure, no name that is not given. Call explanations once.")


def _context_lines(names: list[str], pages: list[tuple[str, str]], chars: int = 600) -> str:
    """The sentences of the Context pages that name both parts (what the team's system says of them together)."""
    want = [n.lower() for n in names if n]
    out: list[str] = []
    for title, text in pages:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
            low = sentence.lower()
            if all(re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", low) for n in want):
                out.append(" ".join(sentence.split())[:300] + f" ({title[:60]})")
                if sum(len(x) for x in out) > chars:
                    return " ".join(out)[:chars]
    return " ".join(out)[:chars]


def unexplained(limit: int = 200) -> list[Any]:
    """The interactions between parts with a short or a long explanation missing (an admin's too: only what is
    empty is written), the approved ones first."""
    from supagent.models import Link

    q = (db.session.query(Link).filter(Link.a_ref.like("facet:%"), Link.b_ref.like("facet:%"),
                                       Link.status.in_(("approved", "proposed")),
                                       (Link.note.is_(None)) | (Link.detail.is_(None)))
         .order_by((Link.status == "approved").desc(), Link.id))
    return q.limit(limit).all()


def explain(llm: Any, seconds: float = 300.0, limit: int = 200) -> dict[str, Any]:
    """The short and long explanations of the interactions that lack them (see the module), EXPLAIN_BATCH at a
    time, until `seconds`; the next run continues. What an admin wrote is kept: only an empty one is filled
    (and the link says the AI wrote a part of it)."""
    from supagent.knowledge.brief import _graph
    from supagent.knowledge.stopping import check
    from supagent.knowledge.sysmap import INTERACTIONS
    from supagent.models import ContextPage, Facet

    t0 = time.time()
    out: dict[str, Any] = {"explained": 0, "calls": 0}
    todo = unexplained(limit)
    if not todo:
        return out
    facets = {f.id: f for f in db.session.query(Facet).filter(
        Facet.id.in_({int(r.split(":", 1)[1]) for x in todo for r in (x.a_ref, x.b_ref) if r.split(":", 1)[1].isdigit()}))}
    pages = [(p.title or "", p.content or "") for p in db.session.query(ContextPage)]
    _graph()                                          # (the map's names are read once)
    for start in range(0, len(todo), EXPLAIN_BATCH):
        if time.time() - t0 > seconds:
            out["left"] = len(todo) - start
            break
        check()
        batch = todo[start:start + EXPLAIN_BATCH]
        lines = []
        for n, x in enumerate(batch, 1):
            a = facets.get(int(x.a_ref.split(":", 1)[1])) if x.a_ref.split(":", 1)[1].isdigit() else None
            b = facets.get(int(x.b_ref.split(":", 1)[1])) if x.b_ref.split(":", 1)[1].isdigit() else None
            if a is None or b is None:
                continue
            said = _context_lines([a.value, b.value], pages)
            lines.append(f"{n}. {a.value} ({a.facet}{': ' + (a.description or '')[:200] if a.description else ''}) "
                         f"{INTERACTIONS.get(x.kind, x.kind)} {b.value} ({b.facet}"
                         f"{': ' + (b.description or '')[:200] if b.description else ''})"
                         + (f". Said: {x.evidence[:400]}" if x.evidence else "")
                         + (f". The admin's note: {x.note[:200]}" if x.note and x.explained_by != "llm" else "")
                         + (f". The Context: {said}" if said else ""))
        if not lines:
            continue
        try:
            msg = llm.chat([{"role": "system", "content": EXPLAIN_SYSTEM},
                            {"role": "user", "content": "Interactions:\n" + "\n".join(lines)}],
                           tools=[EXPLAIN_TOOL], max_tokens=1800)
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent interactions: explanations: %s", str(ex)[:300])
            out["error"] = str(ex)[:300]
            break
        out["calls"] += 1
        for e in _explanations(msg):
            n = e.get("n")
            if not isinstance(n, int) or not 1 <= n <= len(batch):
                continue
            x = batch[n - 1]
            short = " ".join(str(e.get("short") or "").split())[:SHORT_CHARS]
            long_ = " ".join(str(e.get("long") or "").split())[:LONG_CHARS]
            changed = False
            if short and not x.note:
                x.note, changed = short, True
            if long_ and not x.detail:
                x.detail, changed = long_, True
            if changed:                       # (an admin's own words stay; what the AI added is said)
                who = x.explained_by or ("llm" if x.source != "admin" or (x.note == short and x.detail == long_)
                                         else (x.reviewed_by or "an admin"))       # (drawn before 0.9: its note)
                x.explained_by = who if who == "llm" or who.endswith(" and the AI") else f"{who} and the AI"
                out["explained"] += 1
        db.session.commit()
    if out["explained"]:
        from supagent.knowledge.freshness import touch

        touch()
        db.session.commit()
    return out


def _explanations(msg: dict[str, Any]) -> list[dict[str, Any]]:
    args: Any = None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "explanations":
            args = (tc.get("function") or {}).get("arguments")
    if args is None:
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S), re.S)
        args = m.group(0) if m else "{}"
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    found = args.get("explanations") if isinstance(args, dict) else None
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []
