"""Every condition of a query must come from what was said: the question and the chat, the team's rules
and words (glossary), the memory, the documents, notes and learned answers found for the question, the
formulas of the instructions, or the rows an earlier query of the same answer gave (the 3 servers with
the most failures, then their CPU). A condition the model adds by itself ("late jobs" counted among the
successful ones only, "only PROD" where the team said "not UAT", a threshold nobody gave) changes the
number without anyone asking for it: the query is sent back once before it runs, and when it is sent
again unchanged it runs and the answer says which condition nobody asked for.

Not checked: open questions (what is happening, why, is it normal: the agent looks at failures and
peaks by itself), queries that only list values (SELECT DISTINCT, no aggregate), dates and times (the
period check), IS [NOT] NULL, comparisons with 0. The dictionary's lists of values are not a source:
every status is in them."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# values the question may say in other words (stems of the question's words -> words of the value)
VALUE_WORDS = {
    "success": {"success", "succeed", "successful", "complet", "complete", "completed", "ok", "done", "pass",
                "passed", "reussi", "termin", "termine"},
    "fail": {"fail", "failed", "failure", "error", "errors", "ko", "crash", "broken", "echec", "echou", "erreur"},
    "run": {"run", "running", "active", "ongoing", "progress", "cours"},
    "kill": {"kill", "killed", "cancel", "cancelled", "canceled", "abort", "aborted", "stop", "stopped", "annul"},
    "warn": {"warn", "warning", "alert", "avertissement"},
    "prod": {"prod", "production", "prd"},
    "uat": {"uat", "acceptance", "recette"},
    "dev": {"dev", "development", "developpement"},
    "critic": {"critic", "critical", "critique"},
}
TIME_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{8}$|^\d{1,2}:\d{2}")
DB_VALUE_LISTS = (re.compile(r";\s*values:[^)\n]*"),                               # where_block field lines
                  re.compile(r"tenants \(__tenant_id__[^)]*\):[^;\n)]*"),          # where_block metric lines
                  re.compile(r"^- \[(?:metric|index)\] .*$", re.M))               # dictionary pieces found
NOTE_LINES = re.compile(r"^- \[teamnote\] .*$", re.M)          # the users' notes found (knowledge_block lines)
CHECK_TOOLS = ("execute_sql", "create_virtual_dataset", "export_excel", "chart_from_sql", "save_sql_query",
               "promql_query", "compare_to_usual", "send_email")
FACTORS = (1.0, 60.0, 3600.0, 86400.0, 1000.0, 1024.0, 1024.0 ** 2, 1024.0 ** 3, 1e6, 1e9, 100.0, 0.01, 1 / 60)
OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE", "ilike": "ILIKE"}
MATCHER = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


@dataclass
class Condition:
    column: str
    op: str
    values: list[Any]
    text: str

    def said(self) -> str:
        vals = ", ".join(repr(v) if isinstance(v, str) else str(v) for v in self.values[:4])
        return f"{self.column} {self.op} {vals}"


@dataclass
class Support:
    """What was said. `text`: everything given before the first call and the rows of this answer's queries
    (a value must be written there as it is); `words`: the people's words (the question, the chat's
    questions, the memory, the rules, the glossary), where "failed" also says FAILED and "production" PROD."""
    text: str = ""
    words: set[str] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)
    told: set[tuple] = field(default_factory=set)        # the conditions already sent back in this answer

    def add(self, text: str, people: bool = False) -> None:
        from supagent.grounding import _values
        from supagent.knowledge.describe import stem

        low = " ".join((text or "").lower().split())
        self.text += "\n" + low
        found = _values(text or "")
        self.numbers |= set(found)
        if people:                                      # "45 minutes" is 2,700 seconds
            raw = {w for w in re.findall(r"[a-z0-9\u00c0-\u00ff]+", low) if len(w) >= 2}
            self.words |= raw | {stem(w) for w in raw}
            self.numbers |= {v * f for v in found for f in FACTORS}

    def add_result(self, content: str, query: str = "") -> None:
        """What a query of this answer found (its rows, a PromQL result's series): the next queries may use
        it. Not a mere list of values (SELECT DISTINCT status, with no figure and no condition: the
        dictionary, not a finding)."""
        try:
            res = json.loads(content)
        except (TypeError, ValueError):                 # cut by the size limit: its values as written
            pairs = re.findall(r'"[^"]{1,80}":\s*"([^"]{1,200})"', content or "")
            if pairs and re.search(r'":\s*-?\d', content or ""):
                self.add(" ".join(pairs))
            return
        if not isinstance(res, dict):
            return
        values: list[str] = []
        figures = False
        for s in res.get("series") or []:               # promql_query: a series per label set, with values
            if isinstance(s, dict):
                values += [str(v) for v in (s.get("labels") or {}).values()]
                figures = figures or bool(s.get("values") or s.get("last") is not None)
        rows = res.get("rows") or res.get("first_rows") or []
        for r in rows[:1000] if isinstance(rows, list) else []:
            if isinstance(r, dict):
                values += [str(v) for v in r.values() if isinstance(v, str)]
                figures = figures or any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r.values())
        filtered = bool(query) and any(not TIME_LIKE.match(str(v)) for c in sql_conditions(query) for v in c.values)
        if values and (figures or filtered):
            self.add(" ".join(values))


def build(messages: list[dict], people: list[str]) -> Support:
    """The support of a question: the messages given to the LLM before its first call (instructions, chat,
    the question with its blocks) without the dictionary's lists of values, and the people's words."""
    support = Support()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if m.get("role") == "system":                   # the instructions: their formulas only ('idle')
            content = " ".join(re.findall(r"'[^'\n]{1,40}'", content))
        for rx in DB_VALUE_LISTS:
            content = rx.sub(" ", content)
        content = NOTE_LINES.sub(" ", content)          # a user's note is not verified: never a condition's source
        support.add(content)
    for text in people:
        support.add(text, people=True)
    try:                                                # the team's rules, however the prompt was made
        from supagent.knowledge.rulecheck import team_rules

        for rule in team_rules():
            support.add(rule["text"], people=True)
    except Exception:  # pylint: disable=broad-except
        pass
    return support


def _literal(node: Any) -> Any:
    from sqlglot import exp

    neg = isinstance(node, exp.Neg)
    if neg:
        node = node.this
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        return node.this
    try:
        v = float(node.this)
    except ValueError:
        return None
    return -v if neg else v


def sql_conditions(sql: str) -> list[Condition]:
    """The comparisons of a column (or an aggregate, in HAVING) with values, anywhere in the query."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # pylint: disable=broad-except
        return []
    out = []
    pairs = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)
    for node in tree.find_all(*pairs, exp.In, exp.Between):
        negated = isinstance(node.parent, exp.Not)
        if isinstance(node, exp.In):
            if node.args.get("query") is not None:
                continue
            target, values = node.this, [_literal(e) for e in node.expressions]
            op = "NOT IN" if negated else "IN"
        elif isinstance(node, exp.Between):
            target, values, op = node.this, [_literal(node.args.get("low")), _literal(node.args.get("high"))], "BETWEEN"
        else:
            left, right = node.this, node.expression
            if _literal(left) is not None and _literal(right) is None:
                left, right = right, left
            target, values = left, [_literal(right)]
            op = ("NOT " if negated else "") + OPS.get(node.key, node.key.upper())
        if not values or any(v is None for v in values):
            continue
        if isinstance(target, exp.Column):
            column = target.name
        elif isinstance(target, exp.AggFunc):
            column = target.sql(dialect="duckdb")
        else:
            continue
        out.append(Condition(column, op, values, node.sql(dialect="duckdb")))
    return out


def promql_conditions(expr: str) -> list[Condition]:
    return [Condition(m.group(1), m.group(2), [m.group(3)], m.group(0)) for m in MATCHER.finditer(expr or "")
            if m.group(1) != "__name__"]


def chart_conditions(config: dict) -> list[Condition]:
    out = []
    for f in (config or {}).get("filters") or []:
        if isinstance(f, dict) and f.get("column"):
            values = f.get("value") if isinstance(f.get("value"), list) else [f.get("value")]
            if all(v is not None for v in values):
                out.append(Condition(str(f["column"]), str(f.get("op") or "="), values, json.dumps(f)))
    return out


def _value_said(value: Any, column: str, support: Support) -> bool:
    from supagent.knowledge.describe import stem
    from supagent.knowledge.resolve import name_tokens

    if isinstance(value, float) and value == 0.0:
        return True                                     # > 0, <> 0: not a choice of the data
    if isinstance(value, bool) or (isinstance(value, float) and value == 1.0):
        return bool(set(name_tokens(column)) & support.words)   # a flag (RELAUNCHED = true): its field said
    if isinstance(value, float):
        if value.is_integer() and 19000101 <= value <= 21001231:
            return True                                 # 20260923: a date (the period check)
        return any(abs(value - n) <= max(1e-9, 0.005 * abs(n)) for n in support.numbers)
    text = str(value)
    if TIME_LIKE.match(text.strip()):
        return True                                     # a date: the period check
    core = re.sub(r"[%*^$()|\\.]+", " ", text).strip().lower()   # LIKE / regex marks ('%gpu%', '5..')
    if not core:
        return True
    if re.search(rf"(?<![\w-]){re.escape(core)}(?![\w-])", support.text):
        return True
    if re.fullmatch(r"\d+", core) and re.search(rf"(?<![\w-]){core[0]}x+(?![\w-])", support.text):
        return True                                     # '5%', '503' for "5xx"
    try:
        if float(core) and any(abs(float(core) - n) <= max(1e-9, 0.005 * abs(n)) for n in support.numbers):
            return True                                 # le = '2700' for "45 minutes"
    except ValueError:
        pass
    words = support.words
    if re.search(r"[a-z0-9]_[a-z0-9]", core):             # a code (FX_SPOT): a part said inside another code
        free = re.sub(r"[a-z0-9À-ÿ]+(?:_[a-z0-9À-ÿ]+)+", " ", support.text)   # (FX_OPT_G10) does not say it
        raw = set(re.findall(r"[a-z0-9À-ÿ]+", free))
        words = raw | {stem(w) for w in raw}
    for token in re.findall(r"[a-z0-9À-ÿ]+", core):
        if len(token) < 2:
            continue
        s = stem(token)
        if s in words:
            return True
        family = next((k for k, v in VALUE_WORDS.items() if s == k or s in v or token in v), None)
        if family:                                      # "succeeded" says SUCCESS, "errors" FAILED
            fam = VALUE_WORDS[family] | {family}
            if any(w in fam or any(len(f) >= 4 and w.startswith(f) for f in fam) for w in words):
                return True
        if len(s) >= 4 and any(len(w) >= 4 and (w.startswith(s) or s.startswith(w)) for w in words):
            return True
    return False


def unsaid(conditions: list[Condition], support: Support, time_fields: set[str] | None = None) -> list[Condition]:
    """The conditions whose values nothing said gives."""
    out = []
    for c in conditions:
        if c.column.lower() in {"ts", "@timestamp_date"} | {t.lower() for t in (time_fields or set())}:
            continue
        if c.op in ("IS", "IS NOT"):
            continue
        if not all(_value_said(v, c.column, support) for v in c.values):
            out.append(c)
    return out


def call_conditions(tool: str, args: dict) -> list[Condition]:
    """The conditions of a call's queries (SQL, PromQL, a chart's filters); none for a list of values."""
    from supagent.knowledge.excluded import query_texts
    from supagent.knowledge.period import AGGREGATE

    if tool in ("generate_chart", "update_chart"):
        req = args.get("request", args)
        return chart_conditions(req.get("config") if isinstance(req, dict) else {})
    if tool not in CHECK_TOOLS:
        return []
    out = []
    for q in query_texts(args):
        if not q:
            continue
        if tool in ("promql_query", "compare_to_usual"):
            out += promql_conditions(q)
            continue
        if re.search(r"\bSELECT\s+DISTINCT\b", q, re.I) and not AGGREGATE.search(q):
            continue                                    # a list of values: looking, not counting
        if re.search(r"\binformation_schema\b|\bpg_catalog\b", q, re.I):
            continue                                    # the catalogs: which tables, metrics, columns
        out += sql_conditions(q)
    return out


def _told(c: Condition) -> set[tuple]:
    """le IN ('1800', '3600') sent back also tells le = '3600' of the next variant."""
    negative = c.op.upper().startswith("NOT") or c.op in ("<>", "!=", "!~")
    return {(c.column.lower(), negative, str(v)) for v in c.values}


def refusal(support: Support | None, tool: str, args: dict) -> str | None:
    if support is None:
        return None
    found = [c for c in unsaid(call_conditions(tool, args), support) if not _told(c) <= support.told]
    if not found:                                       # each one is sent back once: kept, the answer says it
        return None
    support.told.update(k for c in found for k in _told(c))
    said = "; ".join(c.said() for c in found[:3])
    bucket = any(c.column.lower() == "le" for c in found)
    return (f"tool error (not run: a condition nobody asked for): this call keeps only {said}, which neither the "
            "question, the chat, the team's rules and words, the documents nor an earlier result of this answer "
            "gives. Remove it and count exactly what the question asks. If the question does mean it, send the "
            "call again and say that condition in the answer." +
            (" A histogram bucket (le) counts those up to its bound only: when no bound is the asked threshold, "
             "count from data holding each one's own value, or say it cannot be counted exactly (no estimate "
             "between buckets)." if bucket else ""))


def note(support: Support | None, trace: list[dict]) -> str:
    """After the answer: the conditions nobody asked for that its queries kept (sent again unchanged)."""
    if support is None:
        return ""
    found: list[Condition] = []
    for t in trace:
        if t.get("status") == "done":
            found += unsaid(call_conditions(t.get("called") or t["tool"], t.get("args") or {}), support)
    if not found:
        return ""
    said = "; ".join(dict.fromkeys(c.said() for c in found))
    return (f"\n\n(Check: this answer counts only {said[:300]}, a condition the question did not ask for: the "
            "numbers are for that part only.)")
