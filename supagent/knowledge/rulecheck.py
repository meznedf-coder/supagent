"""Were the team's rules applied? A rule that filters on a field or a label ("Exclude the UAT
environment (ENVIRONMENT_TYPE = 'UAT')") must be applied to every query on data that has that field
or label, unless the question names the rule's own value (it asks for UAT). A query that reads such
data and never uses the field is sent back once, then the answer is marked. Only what is certain is
checked: a rule names a field of the dictionary next to a comparison; the query is checked for the
field, not for how it filters (<> 'UAT', NOT IN, = 'PROD' all apply the rule)."""

from __future__ import annotations

import re
from typing import Any

from superset import db

COMPARED = re.compile(r"(?<![\w.])[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?\s*(?:=|<>|!=|<=|>=|<|>|\bNOT\s+IN\b|\bIN\b)",
                      re.I)
QUOTED = re.compile(r"'([^'\n]{1,60})'")
ASKS_FILTER = re.compile(r"\b(exclude|excluding|only|unless|never|always|must|do not|don'?t|without|keep|filter|"
                         r"remove|ignore|skip|exclure|exclu\w*|uniquement|seulement|sauf|jamais|toujours|sans|"
                         r"ne pas|filtr\w*)\b", re.I)                # a rule that asks for a filter, not a definition
METRIC = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[)")
QUERY_TOOLS = ("execute_sql", "promql_query", "export_excel", "chart_from_sql", "create_virtual_dataset",
               "check_health")


def team_rules() -> list[dict[str, Any]]:
    """The catalog's rules and the team's active rules of the memory: [{title, text}]."""
    from supagent.knowledge.catalog import rules
    from supagent.models import Memory

    out = [{"title": r["title"], "text": r["text"]} for r in rules()]
    try:
        out += [{"title": (m.text or "")[:60], "text": m.text or ""} for m in
                db.session.query(Memory).filter(Memory.scope == "team", Memory.kind == "rule",
                                                Memory.status == "active")]
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    return out


def _tables(text: str) -> set[str]:
    """The indices, tables and metrics a query reads (SQL, else PromQL)."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(text, read="duckdb")
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        names = {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes}
        if names:
            return names
    except Exception:  # pylint: disable=broad-except
        pass
    return {m.group(1) for m in METRIC.finditer(text or "") if m.group(1) not in ("by", "without", "on", "ignoring")}


def unapplied(question: str, trace: list[dict]) -> list[str]:
    """The rules a query of this answer should have applied and did not (their text)."""
    from supagent.knowledge.excluded import query_texts
    from supagent.models import KObject

    rules = team_rules()
    if not rules:
        return []
    queries = []
    for t in trace:
        if (t.get("called") or t["tool"]) in QUERY_TOOLS and t.get("status") == "done":
            queries += [q for q in query_texts(t.get("args") or {}) if q]
    if not queries:
        return []
    asked = (question or "").lower()
    out = []
    for rule in rules:
        if not ASKS_FILTER.search(rule["text"]):
            continue                                    # "KO = failed": says what a value means, filters nothing
        names = {m.group(1) for m in COMPARED.finditer(rule["text"])}
        values = [v for v in QUOTED.findall(rule["text"])]
        if not names or any(re.search(rf"(?<![\w-]){re.escape(v.lower())}(?![\w-])", asked) for v in values):
            continue                                    # nothing checkable, or the question asks for it
        for q in reversed(queries):                    # the last query on such data decides (a query run
            tables = _tables(q)                        # again with the rule makes up for the first one)
            if not tables:
                continue
            has = {n for (n,) in db.session.query(KObject.name).filter(
                KObject.kind.in_(("field", "label")), KObject.parent.in_(list(tables)), KObject.name.in_(list(names)),
                KObject.gone_at.is_(None))}
            if not has:
                continue
            if not any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", q) for n in has):
                out.append(rule["text"])
            break
    return out
