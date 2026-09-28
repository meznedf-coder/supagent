"""The agent writes catalog entries itself - only on evidence it can check, never on its own
judgement:

  formulas        a calculated field (name = expression, on a table) of the answers users
                  confirmed: the same expression in two answers marked Helpful (or one, used
                  three times), never another expression under that name, never Not helpful
  team memory     a rule or a fact of the team an admin approved -> a "rule" or "note" entry
                  (the memory then leaves the prompt: the entry replaces it)
  documents       definitions the LLM points at in a document an admin added, kept only when
                  the sentence is word for word in the document and reads as a definition
                  ("X is the...", "X means...", "X: ...") -> one glossary entry per document;
                  the value is the document's own sentence, never the LLM's words

Every entry it writes is marked (author "(agent)", `origin`, `evidence`), keeps its history
like any other, and is the admins' to correct, disable or delete. Once a person changes or
deletes it, the agent never touches it again. When its evidence breaks (an answer marked Not
helpful, another expression, the document gone), the agent takes back its own entry.
A person's entry always wins over the agent's (catalog merge).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import threading
from typing import Any

import yaml
from superset import db

from supagent import settings
from supagent.knowledge.catalog import AGENT, CatalogError, delete_entry, parse_entry, save_entry
from supagent.models import Doc, Entry, Memory, Recipe

log = logging.getLogger(__name__)
_RUNNING = threading.Lock()
SQL_TOOLS = ("execute_sql", "export_excel")
FORMULA_CATEGORY = "Formulas learned from answers"
GENERIC_NAMES = {"value", "val", "v", "x", "y", "n", "cnt", "count", "total", "result", "res", "metric", "expr",
                 "col", "c", "num", "amount", "sum", "avg", "min", "max", "rate"}
DOCS_PER_RUN = 10
DOC_CHARS = 12000                 # the lines of a document that may hold definitions, per run
DOC_PART = 6000
DOC_PROMPT = """Below are lines of a company document. List the business terms these lines DEFINE
explicitly (e.g. "X is the number of...", "X means...", "X: the ..."). For each, copy the defining
sentence exactly as written, character for character, in "quote". Only definitions the text
states: nothing inferred, no general knowledge. Answer with a JSON list only, empty when there is
none: [{"term": "...", "quote": "..."}]"""
DEFINES = (r"(?:\s*\([^)]{0,80}\))?\**\s*(?::|=|–|—| - | \| |\bis (?:a|an|the)\b|\bare (?:a|an|the)\b|"
           r"\bmeans\b|\brefers to\b|\bstands for\b|\bis defined as\b|\bd[ée]signe\b|\bsignifie\b|"
           r"\bcorrespond [àa]\b|\best (?:un|une|le|la|l')|\bse d[ée]finit\b)")
NOUNS = (r"number|percentage|percent|ratio|share|sum|count|time|rate|metric|measure|field|index|label|name|value|"
         r"average|total|amount|duration|delay|period|identifier|code|status|state|level|threshold|size")
CANDIDATE = re.compile(r"^\s*(?:[-*•]\s*)?\**[^\s:=|]{2}[^:=|\n]{0,58}\**\s*(?::|=|–|—| - )\s+\S|"
                       r"^\|?\s*[^|\s][^|\n]{1,60}\s\|\s*\S+\s+\S+\s+\S+|"
                       r"\b(?:means|refers to|stands for|is defined as|d[ée]signe|signifie|correspond [àa]|"
                       r"se d[ée]finit|(?:is|are) (?:the|a|an) (?:" + NOUNS + r")|"
                       r"(?:is|are) (?:the|a|an) (?:[\w-]+ ){1,3}(?:whose|that|which|where|when))\b", re.I | re.M)


# --------------------------------------------------------------------------- #
# the agent's entries
# --------------------------------------------------------------------------- #
def _entry_of(origin: str) -> Entry | None:
    return db.session.query(Entry).filter(Entry.origin == origin).order_by(Entry.id.desc()).first()


def _state(e: Entry | None) -> str:
    """none | mine | withdrawn (the agent took it back) | theirs (a person changed or deleted it)."""
    if e is None:
        return "none"
    if e.updated_by != AGENT:
        return "theirs"
    return "withdrawn" if e.deleted_at is not None else "mine"


def _free_title(title: str, origin: str) -> str:
    taken = db.session.query(Entry.id).filter(Entry.title == title, Entry.deleted_at.is_(None),
                                              (Entry.origin.is_(None)) | (Entry.origin != origin)).first()
    return title if taken is None else f"{title} (learned)"[:255]


def _put(origin: str, title: str, classification: str, category: str, content: str, evidence: dict[str, Any],
         fmt: str = "text") -> str | None:
    """Write the agent's entry: "added", "updated" or None (unchanged, or a person's now)."""
    e = _entry_of(origin)
    state = _state(e)
    if state == "theirs":
        return None
    values = {"title": title, "classification": classification, "category": category, "fmt": fmt,
              "content": content, "enabled": True}
    if state == "mine":
        if (e.classification, e.category or "", (e.content or "").strip()) == \
                (classification, category or "", content.strip()):
            if (e.evidence or {}) != evidence:          # new counts only: no new version
                db.session.query(Entry).filter(Entry.id == e.id).update(
                    {"evidence": evidence, "updated_at": e.updated_at}, synchronize_session=False)
                db.session.commit()
            return None
        save_entry({**values, "title": e.title}, by=AGENT, entry_id=e.id, origin=origin, evidence=evidence)
        return "updated"
    if state == "withdrawn":                             # certain again: the agent's own entry comes back
        e.deleted_at = None
        db.session.flush()
        save_entry({**values, "title": e.title}, by=AGENT, entry_id=e.id, origin=origin, evidence=evidence)
        return "added"
    save_entry({**values, "title": _free_title(title, origin)}, by=AGENT, origin=origin, evidence=evidence)
    return "added"


def _withdraw(origin: str, why: str) -> bool:
    """The agent deletes its own entry (never a person's)."""
    e = _entry_of(origin)
    if _state(e) != "mine":
        return False
    e.evidence = {**(e.evidence or {}), "withdrawn": why}
    db.session.flush()
    delete_entry(e.id, by=AGENT)
    return True


def _mine(prefix: str) -> list[Entry]:
    """The agent's own entries of this kind (a duplicate of the same origin is removed)."""
    rows = (db.session.query(Entry).filter(Entry.origin.like(prefix + "%"), Entry.deleted_at.is_(None))
            .order_by(Entry.id.desc()).all())
    out, seen = [], set()
    for e in rows:
        if e.updated_by != AGENT:
            continue
        if e.origin in seen:                          # two runs at once wrote it twice: the newest stays
            delete_entry(e.id, by=AGENT)
            continue
        seen.add(e.origin)
        out.append(e)
    return out


def _outcome() -> dict[str, list[str]]:
    return {"added": [], "updated": [], "withdrawn": []}


# --------------------------------------------------------------------------- #
# formulas of the confirmed answers
# --------------------------------------------------------------------------- #
def _dialects(database_id: int | None) -> list[Any]:
    first = None
    try:
        from superset.models.core import Database
        from superset.sql.parse import SQLGLOT_DIALECTS

        d = db.session.get(Database, database_id) if database_id else None
        first = SQLGLOT_DIALECTS.get(d.backend) if d is not None else None
    except Exception:  # pylint: disable=broad-except
        pass
    out = [first] if first else []
    return out + [x for x in (None, "postgres", "trino") if x not in out]


def _core(node: Any) -> Any:
    """Without the cosmetic wrappers: ROUND(x, 2), CAST(x AS ...), (x), COALESCE(x, 0)."""
    from sqlglot import exp

    while True:
        if isinstance(node, (exp.Round, exp.Cast, exp.TryCast, exp.Paren)) or (
                isinstance(node, exp.Coalesce) and all(isinstance(a, exp.Literal) for a in node.expressions)):
            node = node.this
        else:
            return node


def _calculated(node: Any) -> bool:
    """A formula, not a plain read: COUNT(*), SUM(bytes), COUNT(DISTINCT host), ROUND(AVG(x), 2) are not."""
    from sqlglot import exp

    core = _core(node)
    if core.find(exp.AggFunc) is not None:
        if isinstance(core, exp.AggFunc):
            inner = core.this
            if isinstance(inner, exp.Distinct) and len(inner.expressions) == 1:
                inner = inner.expressions[0]
            return not (inner is None or isinstance(inner, (exp.Column, exp.Star)))
        return True
    if core.find(exp.Case) is not None:
        return True
    return any(n.find(exp.Column) is not None for n in core.find_all(exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod))


def named_expressions(sql: str, database_id: int | None = None) -> list[tuple[str, str, list[str]]]:
    """(name, expression, tables) of the calculated columns of a query."""
    import sqlglot
    from sqlglot import exp

    tree, dialect = None, None
    for dialect in _dialects(database_id):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
            break
        except Exception:  # pylint: disable=broad-except
            continue
    if tree is None:
        return []
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = sorted({(f"{t.db}." if t.db else "") + t.name for t in tree.find_all(exp.Table)
                     if t.name and t.name not in ctes})
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    out = []
    for a in tree.find_all(exp.Alias):
        name = a.alias
        if not name or name.lower() in GENERIC_NAMES or len(name) < 3 or not _calculated(a.this):
            continue
        if any(c.name.lower() in aliases for c in a.this.find_all(exp.Column)):
            continue                                 # built on other results of the query: not a formula alone
        try:
            text = a.this.sql(dialect=dialect)
        except Exception:  # pylint: disable=broad-except
            continue
        out.append((name, text, tables))
    return out


def _database_name(database_id: int | None) -> str | None:
    if not database_id:
        return None
    try:
        from superset.models.core import Database

        d = db.session.get(Database, database_id)
        return d.database_name if d is not None else None
    except Exception:  # pylint: disable=broad-except
        return None


def formulas() -> dict[str, list[str]]:
    evidence: dict[str, dict[str, Any]] = {}
    for r in db.session.query(Recipe).filter(Recipe.tool.in_(SQL_TOOLS)).order_by(Recipe.id):
        seen_here: set[str] = set()
        for name, expr, tables in named_expressions(r.query or "", r.database_id):
            if not tables:
                continue
            origin = f"formula:{r.database_id or 0}:{','.join(tables)}:{name.lower()}"[:255]
            ev = evidence.setdefault(origin, {"name": name, "tables": tables, "database_id": r.database_id,
                                              "exprs": {}, "recipes": [], "messages": [], "confirmed": 0,
                                              "rejected": 0, "uses": 0})
            key = " ".join(expr.lower().split())
            ev["exprs"].setdefault(key, expr)
            if origin in seen_here:
                continue
            seen_here.add(origin)
            ev["recipes"].append(r.id)
            ev["uses"] += r.uses or 1
            if r.status == "confirmed":             # each answer marked Helpful counts
                ev["confirmed"] += max(1, len(r.confirmations or []))
                ev["messages"] += [i for i in (r.confirmations or []) if i not in ev["messages"]]
            elif r.status == "rejected":
                ev["rejected"] += 1
    out = _outcome()
    certain = set()
    for origin, ev in evidence.items():
        if ev["rejected"] or len(ev["exprs"]) != 1 or not (ev["confirmed"] >= 2 or
                                                           (ev["confirmed"] >= 1 and ev["uses"] >= 3)):
            continue
        certain.add(origin)
        expr = next(iter(ev["exprs"].values()))
        dbname = _database_name(ev["database_id"])
        content = (f"{ev['name']} = {expr}\n"
                   f"Table: {', '.join(ev['tables'])}" + (f" (database {dbname})" if dbname else ""))
        proof = {"source": "answers", "database_id": ev["database_id"], "database": dbname, "tables": ev["tables"],
                 "recipes": ev["recipes"][-20:], "messages": ev["messages"][-20:], "confirmed": ev["confirmed"],
                 "uses": ev["uses"]}
        done = _put(origin, f"{ev['name']} on {', '.join(ev['tables'])}"[:255], "formula", FORMULA_CATEGORY,
                    content, proof)
        if done:
            out[done].append(origin)
    for e in _mine("formula:"):
        if e.origin not in certain:
            ev = evidence.get(e.origin)
            why = ("an answer using it was marked Not helpful" if ev and ev["rejected"] else
                   "another expression was used under this name" if ev and len(ev["exprs"]) > 1 else
                   "no longer confirmed by the answers")
            if _withdraw(e.origin, why):
                out["withdrawn"].append(e.origin)
    return out


# --------------------------------------------------------------------------- #
# the team's approved rules and facts
# --------------------------------------------------------------------------- #
def _short(text: str, words: int = 9) -> str:
    ws = " ".join((text or "").split()).split(" ")
    return " ".join(ws[:words]) + ("…" if len(ws) > words else "")


def team_memory() -> dict[str, list[str]]:
    out = _outcome()
    rows = (db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "active",
                                            Memory.approved_by.isnot(None), Memory.kind.in_(("rule", "fact")))
            .order_by(Memory.id).all())
    for m in rows:
        origin = f"memory:{m.id}"
        proof = {"source": "team memory", "memory_id": m.id, "approved_by": m.approved_by, "author_id": m.user_id,
                 "message_id": m.message_id}
        e = _entry_of(origin)
        if _state(e) == "theirs" and e.deleted_at is not None:
            continue                               # a person deleted the entry, then approved the memory again
        done = _put(origin, _short(m.text)[:255], "rule" if m.kind == "rule" else "note",
                    m.category or "Team memory", m.text, proof)
        if done:
            out[done].append(origin)
        m.status = "catalog"                        # the entry replaces it in the prompt and the search
        db.session.commit()
    return out


# --------------------------------------------------------------------------- #
# definitions quoted from the documents
# --------------------------------------------------------------------------- #
def _squash(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _human_terms() -> set[str]:
    terms: set[str] = set()
    for e in db.session.query(Entry).filter(Entry.classification == "glossary", Entry.deleted_at.is_(None),
                                            Entry.enabled.is_(True)):
        if e.updated_by == AGENT:
            continue
        try:
            terms |= {str(k).lower() for k in (parse_entry("glossary", e.fmt, e.content or "") or {})}
        except CatalogError:
            continue
    return terms


def candidate_lines(text: str, limit: int = DOC_CHARS) -> str:
    """The lines of a document that may hold a definition."""
    out, size = [], 0
    for line in (text or "").split("\n"):
        line = line.strip()
        if 12 <= len(line) <= 600 and CANDIDATE.search(line):
            if size + len(line) > limit:
                break
            out.append(line)
            size += len(line) + 1
    return "\n".join(out)


QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})


def clean_term(term: str) -> str:
    """The LLM's term without decoration: quotes, bold, an article, an expansion in parentheses."""
    t = " ".join(str(term or "").translate(QUOTES).split()).strip("*`\"' ")
    t = re.sub(r"\s*\([^)]*\)$", "", t)
    return re.sub(r"^(?:a|an|the|le|la|les|l'|un|une)\s+", "", t, flags=re.I).strip()


def verified_definition(term: str, quote: str, text: str) -> str | None:
    """The document's own sentence when it is word for word in it and defines the term."""
    term = " ".join((term or "").split())
    quote = " ".join(str(quote or "").translate(QUOTES).split()).strip("\"` ")
    quote = re.sub(r"^(?:[-*\u2022]|\d+[.)])\s+", "", quote)          # the list mark is not the sentence
    text = (text or "").translate(QUOTES)
    if not 2 <= len(term) <= 80 or len(quote) < 12 or len(quote) > 600:
        return None
    m = re.search(r"\s+".join(re.escape(w) for w in quote.split(" ")), text or "", re.I)
    if m is None:
        return None                                   # not in the document: never kept
    original = " ".join(m.group(0).split())
    d = re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])" + DEFINES, original, re.I)
    if d is None:
        return None                                   # the sentence does not define the term
    if d.group(0).rstrip().endswith("|"):             # a table row: the term is its first cell
        cells = [c.strip() for c in original.strip("| ").split("|")]
        if cells[0].lower() != term.lower() or len(cells) < 2 or len(cells[1].split()) < 3:
            return None
        return cells[1]
    head = re.match(r"\**" + re.escape(term) + r"\**\s*(?::|=|–|—| - )\s*(\S.*)$", original, re.I)
    return head.group(1) if head else original        # "MTTR: the mean time..." -> "the mean time..."


def still_defined(term: str, value: str, text: str) -> bool:
    """A definition found before is still in the document, as it was written."""
    t = (text or "").translate(QUOTES)
    v = r"\s+".join(re.escape(w) for w in value.split())
    k = r"\s+".join(re.escape(w) for w in term.split())
    if not v or not k:
        return False
    if re.search(r"(?<![\w-])" + k + r"(?![\w-])", value, re.I):
        return re.search(v, t, re.I) is not None                   # the whole sentence was kept
    return any(re.search(p, t, re.I) for p in (r"(?<![\w-])" + k + r"\**\s*(?::|=|\u2013|\u2014| - )\s*" + v,
                                                r"\|\s*" + k + r"\s*\|\s*" + v))


def _llm_items(client: Any, lines: str) -> list[dict[str, Any]] | None:
    items: list[dict[str, Any]] = []
    for start in range(0, len(lines), DOC_PART):
        part = lines[start:start + DOC_PART]
        try:
            msg = client.chat([{"role": "system", "content": DOC_PROMPT}, {"role": "user", "content": part}],
                              max_tokens=1200)
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent autocatalog: LLM: %s", ex)
            return None                               # tried again next time
        raw = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
        a, b = raw.find("["), raw.rfind("]")
        try:
            got = json.loads(raw[a:b + 1]) if 0 <= a < b else []
        except ValueError:
            got = []
        items += [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []
    return items


def definitions_from_docs(llm: Any = None) -> dict[str, Any]:
    from supagent.llm import LLM

    out: dict[str, Any] = {**_outcome(), "documents_read": 0, "refused": 0}
    docs = {d.id: d for d in db.session.query(Doc)}
    for e in _mine("doc:"):                           # the document is gone or disabled: its definitions too
        d = docs.get(int(e.origin.split(":")[1])) if e.origin.split(":")[1].isdigit() else None
        if d is None or not d.enabled:
            if _withdraw(e.origin, "the document was removed or disabled"):
                out["withdrawn"].append(e.origin)
    for d in docs.values():                           # uploads of 0.2 before their hash was kept
        if d.content and not d.content_hash:
            d.content_hash = hashlib.sha256(d.content.encode()).hexdigest()[:40]
    todo = [d for d in sorted(docs.values(), key=lambda x: x.id)
            if d.enabled and d.status == "ok" and d.content and d.content_hash != d.learned_hash]
    client = None
    human = _human_terms()
    for d in todo[:DOCS_PER_RUN]:
        lines = candidate_lines(d.content or "")
        items: list[dict[str, Any]] | None = []
        if lines:
            client = client or llm or LLM()
            items = _llm_items(client, lines)
            if items is None:
                continue
        origin = f"doc:{d.id}"
        found: dict[str, str] = {}
        before = _entry_of(origin)
        if _state(before) == "mine":                  # what it found before, while still word for word there
            try:
                previous = yaml.safe_load(before.content or "") or {}
            except yaml.YAMLError:
                previous = {}
            found = {str(k): str(v) for k, v in previous.items() if still_defined(str(k), str(v), d.content or "")} \
                if isinstance(previous, dict) else {}
        for it in items:
            term = clean_term(it.get("term"))
            original = verified_definition(term, str(it.get("quote") or ""), d.content or "")
            if original is None:
                original = verified_definition(" ".join(str(it.get("term") or "").split()),
                                               str(it.get("quote") or ""), d.content or "")
            if original is None:
                out["refused"] += 1
                log.info("supagent autocatalog: document %s: not kept: %r", d.id, it)
                continue
            if term.lower() not in human and term.lower() not in {k.lower() for k in found}:
                found[term] = original
        if found:
            name = d.title or d.url or f"document {d.id}"
            proof = {"source": "document", "doc_id": d.id, "title": d.title, "url": d.url,
                     "content_hash": d.content_hash, "terms": len(found)}
            done = _put(origin, f"Glossary from {name}"[:255], "glossary", d.category or "Documents",
                        yaml.safe_dump(found, allow_unicode=True, sort_keys=True, width=1000), proof, fmt="yaml")
            if done:
                out[done].append(origin)
        elif _withdraw(origin, "the document no longer defines these terms"):
            out["withdrawn"].append(origin)
        d.learned_hash = d.content_hash
        db.session.commit()
        out["documents_read"] += 1
    return out


# --------------------------------------------------------------------------- #
def run(llm_docs: bool = True, parts: tuple[str, ...] = ("formulas", "team_memory", "documents")) -> dict[str, Any]:
    """What the agent writes into the catalog this time (learn.agent_catalog)."""
    if not settings.get("learn.agent_catalog"):
        return {"note": "off (setting learn.agent_catalog)"}
    if not _RUNNING.acquire(timeout=300):
        return {"note": "busy: another pass is running"}
    try:
        return _run(llm_docs, parts)
    finally:
        _RUNNING.release()


def _run(llm_docs: bool, parts: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"at": dt.datetime.utcnow().isoformat(timespec="seconds")}
    jobs = [("formulas", formulas), ("team_memory", team_memory)]
    if llm_docs and settings.get("learn.agent_catalog_docs"):
        jobs.append(("documents", definitions_from_docs))
    changed = False
    for name, fn in jobs:
        if name not in parts:
            continue
        try:
            res = fn()
            out[name] = res
            changed = changed or any(res.get(k) for k in ("added", "updated", "withdrawn"))
        except Exception as ex:  # pylint: disable=broad-except
            db.session.rollback()
            log.exception("supagent autocatalog: %s", name)
            out[name] = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    if changed:
        try:
            from supagent.knowledge.index import sync

            sync(("entry:", "memory:"))
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
    return out
