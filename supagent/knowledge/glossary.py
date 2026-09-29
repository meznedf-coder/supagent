"""The team's words (the catalog's glossary), term by term. The terms of a question are found
without the LLM and given with it in full, before where the data is: the words of a term (all of
them, or its rare word), a code its definition uses (D-1, W-4, UAT...), and the terms that a found
term names or that name it. Each term is also its own piece of the knowledge search."""

from __future__ import annotations

import re
from typing import Any

from superset import db

MAX_TERMS = 6
MAX_CHARS = 2400
DEFINITION_CHARS = 700
CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:[-+][0-9A-Z]+)+\b|\b[A-Z]{2,}[0-9]*\b")      # D-1, W-4, Y-1, UAT, KO
ALTERNATIVES = re.compile(r"\s*(?:/|,|;|\bor\b|\bou\b)\s*", re.I)
_CACHE: dict[str, Any] = {"stamp": None, "terms": []}


def slug(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(term).lower()).strip("-")[:60] or "term"


def entry_terms(entry: Any) -> list[tuple[str, str, str]]:
    """(ref, term, definition) of a glossary entry ([] when its YAML is not a mapping)."""
    from supagent.knowledge.catalog import CatalogError, parse_entry

    try:
        data = parse_entry("glossary", entry.fmt or "yaml", entry.content or "")
    except CatalogError:
        return []
    if not isinstance(data, dict):
        return []
    out, seen = [], set()
    for term, definition in data.items():
        if definition is None or not str(term).strip():
            continue
        key = slug(str(term))
        while key in seen:
            key += "-"
        seen.add(key)
        text = definition if isinstance(definition, str) else _flat(definition)
        out.append((f"entry:{entry.id}#{key}", str(term).strip(), " ".join(str(text).split())))
    return out


def _flat(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_flat(v)}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(_flat(v) for v in value)
    return str(value)


def terms() -> list[dict[str, Any]]:
    """Every term of the enabled glossary entries (made again when the knowledge changed)."""
    from supagent.knowledge.experience import words
    from supagent.knowledge.freshness import stamp
    from supagent.models import Entry

    changed = stamp()
    if _CACHE["stamp"] == changed and _CACHE["stamp"] is not None:
        return _CACHE["terms"]
    out = []
    try:
        rows = (db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                               Entry.classification == "glossary").order_by(Entry.id).all())
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        db.session.rollback()
        rows = []
    for e in rows:
        for ref, term, definition in entry_terms(e):
            alts = [a for a in ALTERNATIVES.split(term) if a.strip()]
            out.append({"ref": ref, "term": term, "definition": definition,
                        "alts": [(a.strip(), words(a.replace("_", " "))) for a in alts],
                        "codes": set(CODE.findall(f"{term} {definition}")),
                        "names": {a.strip().upper() for a in alts if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]+",
                                                                                  a.strip())}})
    _CACHE.update(stamp=changed, terms=out)
    return out


def found(question: str) -> list[dict[str, Any]]:
    """The terms of a question, the most certain first (at most MAX_TERMS)."""
    from supagent.knowledge.experience import words

    all_terms = terms()
    if not all_terms or not (question or "").strip():
        return []
    text, lower = question, question.lower()
    q_words = words(question.replace("_", " "))
    q_codes = set(CODE.findall(question))
    df: dict[str, int] = {}                               # in how many terms a word, a code is
    for t in all_terms:
        for w in set().union(*(ws for _a, ws in t["alts"])) | t["codes"]:
            df[w] = df.get(w, 0) + 1
    scores: dict[str, float] = {}
    for t in all_terms:
        score = 0.0
        for alt, ws in t["alts"]:
            if len(alt) >= 2 and re.search(rf"(?<![A-Za-z0-9_]){re.escape(alt.lower())}(?![A-Za-z0-9_])", lower):
                score = max(score, 6.0 + len(ws))
            elif ws and ws <= q_words:
                score = max(score, 4.0 + len(ws))
            elif any(len(w) >= 4 and df.get(w, 0) <= 2 for w in ws & q_words):
                score = max(score, 2.0)
        common = {c for c in q_codes & t["codes"] if df.get(c, 0) <= 3}     # a code few terms use
        if common:
            score += min(6.0, 3.0 * len(common))
        if score:
            scores[t["ref"]] = score
    by_ref = {t["ref"]: t for t in all_terms}
    first = set(scores)
    for t in all_terms:                                   # one step further: the terms they name, or that name them
        if t["ref"] in first:
            continue
        for ref in first:
            f = by_ref[ref]
            if any(n and re.search(rf"\b{re.escape(n)}\b", t["definition"], re.I) for n in f["names"]) or any(
                    n and re.search(rf"\b{re.escape(n)}\b", f["definition"], re.I) for n in t["names"]):
                scores[t["ref"]] = max(scores.get(t["ref"], 0.0), 1.0)
    ranked = sorted(scores, key=lambda r: -scores[r])[:MAX_TERMS]
    return [by_ref[r] for r in ranked]


def glossary_block(question: str, shown: set[str] | None = None) -> str:
    """The terms of the question with their definitions, for the prompt ("" when none)."""
    try:
        hits = found(question)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return ""
    if not hits:
        return ""
    lines = ["\n\nThe team's words in this question (the catalog's glossary: use these meanings, the fields and "
             "values they name):"]
    used = 0
    for t in hits:
        definition = t["definition"][:DEFINITION_CHARS] + ("..." if len(t["definition"]) > DEFINITION_CHARS else "")
        line = f"- {t['term']}: {definition}"
        if used + len(line) > MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
        if shown is not None:
            shown.add(t["ref"])
    return "\n".join(lines) if len(lines) > 1 else ""
