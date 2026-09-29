"""What the team said not to use: an index, field, metric or label whose description, written by a
person (the Data dictionary page, the catalog), says it is not used: "not used", "do not use",
"deprecated", "obsolete", "ne pas utiliser", "n'est plus utilisé"... The agent is told so (where
the data is, describe_data), and a query or a chart that uses it is refused with the team's words,
so that the agent takes another field or metric. A description written by the LLM never counts."""

from __future__ import annotations

import fnmatch
import re
import time
from typing import Any

from superset import db

from supagent.models import KObject, Source

# said of the object itself: at the start of the description or of one of its sentences ("Not used",
# "does not used: take X", "This field is deprecated", "Ce champ n'est plus utilisé"); never a word
# inside an explanation ("mode idle = unused, the other modes = busy")
NOT_USED = re.compile(
    r"(?:^|[.;!?]\s+|\n)[\s\W]*"
    r"(?:(?:this|the)\s+(?:field|metric|label|index|column)\s+(?:is\s+|are\s+)?|"
    r"(?:ce|cette|le|la)\s+(?:champ|m[ée]trique|label|index|colonne)\s+)?"
    r"(?:(?:does|do|is|are)\s+)?"
    r"(not\s+(?:be\s+)?used|unused|no\s+longer\s+used|deprecated|obsolete|do\s*n[o']?t\s+use|never\s+use|"
    r"not\s+to\s+be\s+used|n['’]?\s*(?:est|sont)\s+plus\s+utilis\w*|ne\s+pas\s+utiliser|ne\s+plus\s+utiliser|"
    r"non\s+utilis\w*|inutilis\w*|obsol[eè]te\w*|d[ée]pr[ée]ci[ée]\w*|[àa]\s+ne\s+pas\s+utiliser)\b", re.I)
CACHE_S = 60.0
QUERY_KEYS = ("sql", "expr", "promql", "query", "excel_sql", "chart_sqls", "sqls")
_CACHE: dict[str, Any] = {"at": 0.0, "items": [], "stamp": ""}


def says_not_used(obj: Any) -> bool:
    """A person's description of this object says not to use it."""
    return bool(getattr(obj, "description_source", None) == "curated" and obj.description
                and NOT_USED.search(obj.description))


def excluded() -> list[dict[str, Any]]:
    """Every object the team said not to use: kind, database id, parent (index or metric), name and
    the team's words (read again every CACHE_S seconds, and as soon as the knowledge changed)."""
    from supagent.knowledge.freshness import stamp

    changed = stamp()
    if time.time() - _CACHE["at"] < CACHE_S and _CACHE["stamp"] == changed:
        return _CACHE["items"]
    items = []
    try:
        rows = (db.session.query(KObject, Source.database_id).join(Source, Source.id == KObject.source_id)
                .filter(KObject.description_source == "curated", KObject.gone_at.is_(None),
                        KObject.description.isnot(None)).all())
        for o, database_id in rows:
            if NOT_USED.search(o.description or ""):
                items.append({"kind": o.kind, "database_id": database_id, "parent": o.parent or "", "name": o.name,
                              "why": " ".join(o.description.split())[:200]})
        db.session.commit()
    except Exception:  # pylint: disable=broad-except   (tables not created yet)
        db.session.rollback()
    _CACHE.update(at=time.time(), items=items, stamp=changed)
    return items


def of_table(database_id: int | None, table: str) -> list[dict[str, Any]]:
    """The fields or labels of one index / metric (or index pattern) the team said not to use."""
    return [x for x in excluded() if x["kind"] in ("field", "label") and x["parent"]
            and (database_id is None or x["database_id"] == database_id)
            and (x["parent"] == table or ("*" in table and fnmatch.fnmatch(x["parent"], table)))]


def is_excluded(kind: str, database_id: int | None, name: str, parent: str = "") -> dict[str, Any] | None:
    for x in excluded():
        if x["kind"] == kind and x["name"] == name and (x["parent"] or "") == (parent or "") and \
                (database_id is None or x["database_id"] == database_id):
            return x
    return None


def _named(text: str, name: str) -> bool:
    """`name` used as an identifier in a query (bare word, "quoted" or `quoted`), not inside a longer name."""
    return re.search(r'(?<![\w:-])["`]?' + re.escape(name) + r'["`]?(?![\w.:-])', text) is not None   # alias.NAME too


def _tables(text: str) -> set[str]:
    """Table-like names of a query: after FROM / JOIN (SQL), and every identifier (PromQL metric names)."""
    out = {m.group(1) or m.group(2) for m in re.finditer(r'\b(?:from|join)\s+(?:"([^"]+)"|([\w.:*-]+))', text, re.I)}
    return {t for t in out if t}


def violations(texts: list[str]) -> list[str]:
    """What a query uses that the team said not to use, with the team's words."""
    items = excluded()
    if not items:
        return []
    out = []
    for text in texts:
        if not text:
            continue
        tables = _tables(text)
        for x in items:
            if x["kind"] in ("index", "metric"):
                if _named(text, x["name"]):
                    out.append(f'{x["kind"]} "{x["name"]}": the team wrote "{x["why"]}"')
            elif any(t == x["parent"] or ("*" in t and fnmatch.fnmatch(x["parent"], t)) for t in tables) or \
                    _named(text, x["parent"]):
                if _named(text, x["name"]):
                    out.append(f'{x["kind"]} "{x["name"]}" of "{x["parent"]}": the team wrote "{x["why"]}"')
    return sorted(set(out))


def query_texts(args: Any) -> list[str]:
    """The SQL and PromQL of a tool call's arguments (not its other texts: an e-mail body may name a field)."""
    found: list[str] = []

    def walk(v: Any, key: str = "") -> None:
        if isinstance(v, dict):
            for k, vv in v.items():
                walk(vv, str(k))
        elif isinstance(v, list):
            for vv in v:
                walk(vv, key)
        elif isinstance(v, str) and key in QUERY_KEYS:
            found.append(v)

    walk(args)
    return found


def refusal(problems: list[str]) -> str:
    return ("tool error: this uses what the team marked as not to be used: " + "; ".join(problems[:5]) +
            ". Write it without them (another field or metric); do not use them in the answer either.")
