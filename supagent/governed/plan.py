"""The plan: what an answer counts, as data, before any query. The model fills it from the knowledge pack
(tables as T refs, knowledge as K refs); it never writes SQL. Every condition carries its source:

  "question: <the words of the question that say it>"   "chat: <the words of an earlier message>"
  "K2" (a knowledge item: a rule, a glossary term, a note, the memory, a learned answer)
  "q1" (an earlier step: its value is then "q1.<column>", filled by code from that step's rows)

A plan that cannot give a source for a condition is sent back (validate); code adds the team's rules
itself, fills the values found by earlier steps, and builds the queries (compile).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

INDEX_FNS = ("count", "count_distinct", "sum", "avg", "min", "max", "share")
METRIC_FNS = ("increase", "rate", "avg", "min", "max", "quantile", "share", "formula")
OPS = ("=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "not like", "is null", "is not null")
BUCKETS = ("minute", "5 minutes", "15 minutes", "hour", "day", "week", "month")
UNITS = ("seconds", "minutes", "hours", "days", "milliseconds", "bytes", "KiB", "MiB", "GiB", "TiB", "percent")
KINDS = ("answer", "clarify", "cannot", "explain")


class Cond(BaseModel):
    field: str
    op: str = "="
    value: Any = None
    source: str = ""

    @field_validator("op")
    @classmethod
    def _op(cls, v: str) -> str:
        v = " ".join(str(v or "=").lower().split()).replace("==", "=").replace("<>", "!=")
        if v not in OPS:
            raise ValueError(f"op must be one of {', '.join(OPS)}")
        return v


class Measure(BaseModel):
    label: str
    fn: str
    field: str | None = None
    where: list[Cond] = Field(default_factory=list)
    q: float | None = None
    formula: str | None = None
    unit: str | None = None               # the unit to show; code converts from the field's own unit
    source_unit: str | None = None        # the field's unit, when the dictionary does not say it
    unit_source: str | None = None        # where source_unit comes from (K ref, "question: ...", "data: ...")

    @field_validator("fn")
    @classmethod
    def _fn(cls, v: str) -> str:
        v = str(v or "").lower().strip()
        if v not in set(INDEX_FNS) | set(METRIC_FNS):
            raise ValueError(f"fn must be one of {', '.join(sorted(set(INDEX_FNS) | set(METRIC_FNS)))}")
        return v


class Period(BaseModel):
    start: str
    end: str
    source: str = ""
    field: str | None = None          # a date field of the table other than its time field (the question names it)


class Order(BaseModel):
    by: str
    desc: bool = True


class Step(BaseModel):
    id: str
    table: str
    purpose: str = ""
    measures: list[Measure] = Field(default_factory=list)
    by: list[str] = Field(default_factory=list)
    bucket: str | None = None
    where: list[Cond] = Field(default_factory=list)
    period: Period | None = None
    order: list[Order] = Field(default_factory=list)
    limit: int | None = None
    limit_source: str | None = None

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, v: str | None) -> str | None:
        if v in (None, "", "none"):
            return None
        v = str(v).lower().strip()
        if v not in BUCKETS:
            raise ValueError(f"bucket must be one of {', '.join(BUCKETS)}")
        return v


class Plan(BaseModel):
    kind: str = "answer"
    steps: list[Step] = Field(default_factory=list)
    question_back: str | None = None
    options: list[str] = Field(default_factory=list)
    reason: str | None = None
    knowledge: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = str(v or "answer").lower().strip()
        if v not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        return v


def _cond_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "field": {"type": "string", "description": "a field (index) or label (metric) of the step's table, exactly"},
        "op": {"type": "string", "enum": list(OPS)},
        "value": {"description": "the value exactly as listed for that field; a list for in / not in; "
                                 "\"q1.COLUMN\" for the values an earlier step found"},
        "source": {"type": "string", "description": "\"question: <its words>\", \"chat: <words>\", a K ref, or a "
                                                    "step id (q1)"}},
        "required": ["field", "op", "value", "source"]}


PLAN_TOOL = {"type": "function", "function": {
    "name": "submit_plan",
    "description": "The plan of the answer: what to count, in which table, with which conditions and period.",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS),
                 "description": "answer: steps that read the data; clarify: a question back with options; cannot: "
                                "the data cannot answer (reason); explain: the knowledge items answer it"},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "description": "q1, q2, ..."},
            "table": {"type": "string", "description": "a T ref"},
            "purpose": {"type": "string", "description": "the part of the question this step answers"},
            "measures": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string", "description": "the name of the result column, in the question's words"},
                "fn": {"type": "string", "enum": sorted(set(INDEX_FNS) | set(METRIC_FNS))},
                "field": {"type": "string", "description": "the field for count_distinct, sum, avg, min, max of an "
                                                            "index; empty for a metric (its value)"},
                "where": {"type": "array", "items": _cond_schema(),
                          "description": "conditions of this measure only (a share: the part counted)"},
                "q": {"type": "number", "description": "quantile, 0..1 (fn quantile)"},
                "formula": {"type": "string", "description": "a formula's name (fn formula)"},
                "unit": {"type": "string", "enum": list(UNITS), "description": "the unit to give it in"},
                "source_unit": {"type": "string", "enum": list(UNITS),
                                "description": "the field's own unit if the table does not say it"},
                "unit_source": {"type": "string"}},
                "required": ["label", "fn"]}},
            "by": {"type": "array", "items": {"type": "string"}, "description": "fields or labels to group by"},
            "bucket": {"type": "string", "enum": list(BUCKETS), "description": "a time bucket to group by"},
            "where": {"type": "array", "items": _cond_schema()},
            "period": {"type": "object", "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "end": {"type": "string", "description": "YYYY-MM-DD HH:MM, excluded"},
                "source": {"type": "string"}}, "required": ["start", "end", "source"]},
            "order": {"type": "array", "items": {"type": "object", "properties": {
                "by": {"type": "string"}, "desc": {"type": "boolean"}}, "required": ["by"]}},
            "limit": {"type": "integer", "description": "only when the question asks for the top N"},
            "limit_source": {"type": "string"}},
            "required": ["id", "table", "measures"]}},
        "question_back": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "knowledge": {"type": "array", "items": {"type": "string"},
                      "description": "K refs the answer uses (explain: the ones that answer it)"}},
        "required": ["kind"]}}}

PLAN_SYSTEM = """You write the plan of an answer: what to count, in which table, with which conditions and period. You \
never write SQL and you do not answer yet: call submit_plan once.

Tables are T refs, knowledge items are K refs (both below). Use a table's fields or labels and values exactly as listed.

Conditions: every condition (in where, and in a measure's where) needs its source: "question: <the question's own \
words that say it>", "chat: <words of the chat>", a K ref (a rule, a glossary term, a note, the memory, a learned \
answer), or an earlier step (q1). Never add a condition nobody gave: no status, environment, threshold or value of \
your own. The team's rules are applied by code: do not repeat them, and never ask about one: a question that \
names what a rule leaves out (in UAT) lifts it, and code sees that.

Period: from the question ("on 23 September" = 2026-09-23 00:00 to 2026-09-24 00:00; "the week of 14 to 20 \
September" = 2026-09-14 00:00 to 2026-09-21 00:00; hours of a day as said), its source the question's words, on \
every step. Two periods to compare: one step for each. A day after now may have data: the table's time range says.

Measures: an index counts documents (count), or sums, averages, min, max a field; a counter metric: increase (how \
many in the period) or rate (per second); a gauge: avg, min, max; a histogram (_bucket): quantile with q; a \
percentage of a part: share (its where = the part). A formula of the knowledge: fn formula with its name. Units: \
set unit to give it in minutes, hours, GiB...; code converts from the field's unit.

Groups: by = the fields or labels the question asks per; bucket for per hour, per day. Top N: limit only when the \
question asks for the top N or the most (limit_source its words), ordered by the measure it ranks.

Several parts: one step per table or per period. A step can use what an earlier one found: value "q1.NODE" (the \
NODE column of step q1), source "q1".

If the question can be read two ways that give different numbers and nothing settles it: kind clarify, with \
question_back and the options. If no table can answer: kind cannot, with the reason. If the knowledge items answer \
the question by themselves: kind explain, with their K refs."""


def plan_messages(question: str, pack_text: str, now: str, previous: str = "",
                  feedback: str = "") -> list[dict[str, str]]:
    user = f"{pack_text}\n\n(Now: {now}.)\n"
    if previous:
        user += f"(The chat before this question: {previous[:1200]})\n"
    user += f"Question: {question}"
    msgs = [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}]
    if feedback:
        msgs.append({"role": "user", "content": feedback})
    return msgs


def parse_plan(args: Any) -> tuple[Plan | None, str]:
    """(the plan, "") or (None, what is wrong with it: sent back to the model)."""
    if isinstance(args, str):
        text = re.sub(r"<think>.*?</think>", "", args, flags=re.S)
        m = re.search(r"\{.*\}", text, re.S)
        try:
            args = json.loads(m.group(0)) if m else None
        except ValueError as ex:
            return None, f"the plan is not valid JSON: {ex}"
    if not isinstance(args, dict):
        return None, "no plan was given: call submit_plan"
    try:
        return Plan.model_validate(args), ""
    except ValidationError as ex:
        parts = []
        for e in ex.errors()[:8]:
            where = ".".join(str(x) for x in e.get("loc") or [])
            parts.append(f"{where}: {e.get('msg')}")
        return None, "the plan does not fit its schema: " + "; ".join(parts)


SOURCE = re.compile(r"^\s*(question|chat|data)\s*:\s*(.+)$", re.I | re.S)
REF = re.compile(r"^\s*([KTq]\d+)\b", re.I)


def source_of(text: str | None) -> tuple[str, str]:
    """("question" | "chat" | "data", the quote) or ("knowledge", "K2") or ("step", "q1") or ("", "")."""
    t = (text or "").strip().strip("\"'")
    m = SOURCE.match(t)
    if m:
        return m.group(1).lower(), m.group(2).strip().strip("\"'")
    m = REF.match(t)
    if m:
        ref = m.group(1)
        return ("knowledge", ref.upper()) if ref[0] in "Kk" else (("step", ref.lower()) if ref[0] in "qQ" else ("", ""))
    return "", ""
