"""Were the team's rules applied? A rule that filters on a field or a label ("Exclude the UAT
environment (ENVIRONMENT_TYPE = 'UAT')") must be applied to every query on data that has that field
or label, unless the question names the rule's own value (it asks for UAT). A query that reads such
data and never uses the field is sent back once, then the answer is marked. Only what is certain is
checked: a rule names a field of the dictionary next to a comparison; the query is checked for the
field, not for how it filters (<> 'UAT', NOT IN, = 'PROD' all apply the rule)."""

from __future__ import annotations

import json
import re
from typing import Any

from superset import db

COMPARED = re.compile(r"(?<![\w.])[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?\s*(?:=|<>|!=|<=|>=|<|>|\bNOT\s+IN\b|\bIN\b)",
                      re.I)
QUOTED = re.compile(r"'([^'\n]{1,60})'")
ASKS_FILTER = re.compile(r"\b(exclude|excluding|only|unless|never|always|must|do not|don'?t|without|keep|filter|"
                         r"remove|ignore|skip|exclure|exclu\w*|uniquement|seulement|sauf|jamais|toujours|sans|"
                         r"ne pas|filtr\w*)\b", re.I)                # a rule that asks for a filter, not a definition
PROMQL_WORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "bool", "offset", "and", "or",
                "unless", "inf", "nan", "atan2", "start", "end"}


def promql_metrics(expr: str) -> set[str]:
    """The metric names of a PromQL expression: avg(fed_temperature), rate(x_total{a="b"}[5m]) / y."""
    text = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', " ", expr or "")     # strings
    text = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", text)                                # matchers, ranges
    text = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", text)   # label lists
    names = set()
    for m in re.finditer(r"(?<![\w.])([a-zA-Z_:][a-zA-Z0-9_:]*)(\s*\()?", text):
        if m.group(2) or m.group(1).lower() in PROMQL_WORDS:
            continue                                    # a function, a keyword
        names.add(m.group(1))
    return names
QUERY_TOOLS = ("execute_sql", "promql_query", "export_excel", "chart_from_sql", "create_virtual_dataset",
               "check_health", "compare_to_usual", "save_sql_query", "send_email")


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
    return promql_metrics(text)


EXCLUDING = re.compile(r"\b(exclude|excluding|without|never|remove|ignore|skip|do not|don'?t|not|exclure|exclu\w*|"
                       r"sans|sauf|jamais|ne pas|hors)\b", re.I)
PAIR = re.compile(r"(?<![\w.])[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?\s*(?:=|==|IN\s*\(?)\s*'([^'\n]{1,60})'", re.I)


def _queries(trace: list[dict]) -> list[str]:
    from supagent.knowledge.excluded import query_texts

    out = []
    for t in trace:
        if (t.get("called") or t["tool"]) in QUERY_TOOLS and t.get("status") == "done":
            out += [q for q in query_texts(t.get("args") or {}) if q]
    return out


def _checkable(rule: dict[str, Any], asked: str) -> tuple[set[str], list[str]] | None:
    """(the rule's field names, its values) when it is a filter this question does not lift."""
    if not ASKS_FILTER.search(rule["text"]):
        return None                                     # "KO = failed": says what a value means, filters nothing
    names = {m.group(1) for m in COMPARED.finditer(rule["text"])}
    values = list(QUOTED.findall(rule["text"]))
    if not names or any(re.search(rf"(?<![\w-]){re.escape(v.lower())}(?![\w-])", asked) for v in values):
        return None                                     # nothing checkable, or the question asks for it
    return names, values


_CONCERNS: dict[str, tuple[float, set[str]]] = {}
CONCERNS_S = 60.0
RULE_WORDS = {"not", "never", "always", "only", "unless", "except", "exclude", "excluding", "include", "including",
              "add", "its", "their", "record", "records", "value", "values", "use", "used", "using", "instead",
              "count", "counts", "question", "questions", "asks", "ask", "asked", "when", "then", "otherwise",
              "same", "each", "every", "all"}     # the words of any rule: they say how, not what data it is about


def value_possible(table: str, field: str, value: str) -> bool | None:
    """Can this field of this table have this value? False when its values are all known (learned) and the value
    is not one of them ("BOOK = 'ALL'" on an index whose 24 books are all known); None when it cannot be said."""
    from supagent.models import KObject

    o = db.session.query(KObject).filter(KObject.kind.in_(("field", "label")), KObject.parent == table,
                                         KObject.name == field, KObject.gone_at.is_(None)).first()
    return possible_in((o.stats or {}) if o is not None else {}, value)


def possible_in(stats: dict[str, Any], value: str) -> bool | None:
    known = [str(v) for v in (stats.get("values") or [])]
    if not known:
        return None
    if str(value).lower() in {k.lower() for k in known}:
        return True
    card = stats.get("cardinality")
    full = (isinstance(card, int) or str(card or "").isdigit()) and int(card) <= len(known) and not stats.get("partial")
    return False if full else None


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def concerns(rule: dict[str, Any], names: set[str]) -> set[str] | None:
    """The tables a rule is about, among those that have its field: the ones whose data carries the rule's
    distinguishing subject (a word of the rule naming a field, a value or the table itself in some of them and
    not in the others: "The VaR of a desk is its record with BOOK = 'ALL'" is about the index whose values say
    VaR, not every index with a BOOK). None: no distinguishing word, every table with the field."""
    import time

    from supagent.knowledge.describe import STOP
    from supagent.models import KObject

    key = rule["text"]
    hit = _CONCERNS.get(key)
    if hit is not None and time.time() - hit[0] < CONCERNS_S:
        return hit[1] or None
    tables = {p for (p,) in db.session.query(KObject.parent).filter(
        KObject.kind.in_(("field", "label")), KObject.name.in_(list(names)), KObject.gone_at.is_(None))}
    data: dict[str, set[str]] = {t: _words(t) for t in tables}
    for o in db.session.query(KObject).filter(KObject.kind.in_(("field", "label")), KObject.parent.in_(list(tables)),
                                              KObject.gone_at.is_(None)):
        ws = data.setdefault(o.parent, set())
        ws |= _words(o.name)
        for v in ((o.stats or {}).get("values") or [])[:300]:
            ws |= _words(str(v))
    said = {w for w in _words(rule["text"]) if w not in STOP and w not in RULE_WORDS}
    distinct = {w for w in said if 0 < sum(w in ws for ws in data.values()) < len(data)}
    about = {t for t, ws in data.items() if ws & distinct} if distinct else set()
    _CONCERNS[key] = (time.time(), about)
    return about or None


GROUP_BY = re.compile(r"\bGROUP\s+BY\b(.*?)(?:\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)", re.I | re.S)


def grouped_by(query: str) -> set[str]:
    """The fields a query groups by (a rule's filter on one of them would keep one group of a list)."""
    m = GROUP_BY.search(query or "")
    return {x.strip('"`') for x in re.findall(r'"[^"]+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_]*', m.group(1))} if m else set()


def _fields_of(tables: set[str], names: set[str]) -> set[str]:
    """The rule's fields or labels that these tables have (the dictionary)."""
    from supagent.models import KObject

    return {n for (n,) in db.session.query(KObject.name).filter(
        KObject.kind.in_(("field", "label")), KObject.parent.in_(list(tables)), KObject.name.in_(list(names)),
        KObject.gone_at.is_(None))}


def details(question: str, queries: list[str]) -> list[dict[str, Any]]:
    """The rules the last query on their data should have applied and did not: [{rule, table, field,
    value, exclude}] (the field and value the rule gives for that table)."""
    rules = team_rules()
    if not rules or not queries:
        return []
    asked = (question or "").lower()
    out = []
    for rule in rules:
        found = _checkable(rule, asked)
        if found is None:
            continue
        names, values = found
        about = concerns(rule, names)
        for q in reversed(queries):                    # the last query on such data decides (a query run
            tables = _tables(q)                        # again with the rule makes up for the first one)
            if about is not None:
                tables = tables & about                # the tables the rule is about, not all with its field
            if not tables:
                continue
            has = _fields_of(tables, names)
            if not has:
                continue
            if values and all(value_possible(t, f, values[0]) is False for t in tables for f in has):
                break                                  # its value cannot be in this data: it filters nothing here
            if has & grouped_by(q):                    # per BOOK: every book, not the rule's one
                break
            if not any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", q) for n in has):
                out.append(_detail(rule, has, values, tables))
            break
    return out


KEEPING = re.compile(r"\b(only|uniquement|seulement|keep|garder|include|inclure)\b", re.I)


def _detail(rule: dict[str, Any], has: set[str], values: list[str], tables: set[str]) -> dict[str, Any]:
    """The field of the rule this table has, its value, and whether the rule leaves that value out (the
    words before the field: "Exclude ... (ENVIRONMENT_TYPE = 'UAT')") or keeps only it ("Only ...")."""
    text = rule["text"]
    fld = sorted(has)[0]
    value, exclude = (values[0] if values else None), bool(EXCLUDING.search(text)) and not KEEPING.search(text)
    for m in PAIR.finditer(text):
        if m.group(1) == fld:
            before = text[max(0, m.start() - 80):m.start()]
            value = m.group(2)
            exclude = bool(EXCLUDING.search(before)) and not KEEPING.search(before)
            break
    return {"rule": text, "table": sorted(tables)[0], "field": fld, "value": value, "exclude": exclude}


def unapplied(question: str, trace: list[dict]) -> list[str]:
    """The rules a query of this answer should have applied and did not (their text)."""
    return [d["rule"] for d in details(question, _queries(trace))]


def _suggestion(d: dict[str, Any], tool: str) -> str:
    f, v = d["field"], d.get("value")
    if v is None:
        return f"a condition on {f}"
    if tool in ("promql_query", "compare_to_usual"):
        return f'{{{f}{"!=" if d["exclude"] else "="}"{v}"}} in the selector'
    if tool == "generate_chart":
        return json.dumps({"column": f, "op": "!=" if d["exclude"] else "=", "value": v}) + " in the filters"
    return f'"{f}" {"<>" if d["exclude"] else "="} \'{v}\' in the WHERE'


def call_refusal(question: str, tool: str, args: dict) -> str | None:
    """Why a query call must not run as it is (a team rule its data needs), or None."""
    if tool not in QUERY_TOOLS:
        return None
    from supagent.knowledge.excluded import query_texts

    found = details(question, [q for q in query_texts(args) if q])
    return _refusal(found[0], tool) if found else None


def chart_refusal(question: str, dataset: Any, config: dict) -> str | None:
    """A chart on a table (not a query) whose data a team rule filters, without a filter on its field."""
    if dataset is None or (dataset.sql or "").strip():
        return None                                     # a query: checked when it was saved as a dataset
    rules = team_rules()
    asked = (question or "").lower()
    used = set(re.findall(r'"([^"]+)"', json.dumps(config)))        # filtered or shown per value: applied
    for rule in rules:
        found = _checkable(rule, asked)
        if found is None:
            continue
        names, values = found
        about = concerns(rule, names)
        if about is not None and dataset.table_name not in about:
            continue
        has = _fields_of({dataset.table_name}, names)
        if has and values and all(value_possible(dataset.table_name, f, values[0]) is False for f in has):
            continue
        if has and not (has & used):
            return _refusal(_detail(rule, has, values, {dataset.table_name}), "generate_chart")
    return None


def _refusal(d: dict[str, Any], tool: str) -> str:
    return (f"tool error (not run: team rule): the team's rule \"{d['rule'][:300]}\" applies to {d['table']} (it has "
            f"{d['field']}), and this call does not use {d['field']}. Call it again with the rule applied (for "
            f"example {_suggestion(d, tool)}). If the question asks for what the rule leaves out, send this same "
            "call again unchanged and say so in the answer.")
