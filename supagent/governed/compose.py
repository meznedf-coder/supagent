"""Writing the answer from the plan and its results.

Code writes what must not be invented: how the answer counted (the interpretation, each condition with where
it comes from: the question's words, a team rule, a note, an earlier step), and the figures sheet the model
writes from (every row of small results, the totals of counts, the first rows of big ones, what was cut or
empty). The model writes the sentences from that sheet; its numbers are then checked against the results
(the classic checks), and the interpretation is added under the answer by code.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from supagent.governed.decider import Pack
from supagent.governed.plan import Cond, Plan, Step
from supagent.governed.validate import Checked, cond_key, parse_time

SHEET_ROWS = 40               # rows of one result given to the model in full
OPS_TEXT = {"=": "=", "!=": "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤", "in": "in", "not in": "not in",
            "like": "like", "not like": "not like", "is null": "is empty", "is not null": "is not empty"}

ANSWER_SYSTEM = """You write the answer to the user's question from the figures sheet below: the results of the \
queries that were run for it, exactly as the plan counted. Use only these numbers, written as they are (you may \
round them); a total, a share or a difference only if it is in the sheet. Do not describe how it was counted: a line \
saying it is added under your answer. If a result is empty, say that nothing matched. If a result was cut, say you \
show the first rows. Answer in the language of the question with a full sentence that says what each number is \
("42 BILLING jobs failed on 23 September."), then a small table when there are several rows. No SQL, no working \
notes, no T or K refs."""


def fmt_number(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4g}"
    return str(v)


def period_text(start: str, end: str) -> str:
    a, b = parse_time(start), parse_time(end)
    if a is None or b is None:
        return f"{start} to {end}"
    if a.time() == dt.time(0) and b.time() == dt.time(0):
        last = b - dt.timedelta(days=1)
        if last.date() == a.date():
            return f"{a:%d %b %Y}"
        return f"{a:%d %b} to {last:%d %b %Y}"
    if a.date() == b.date() or (b - a) <= dt.timedelta(days=1):
        return f"{a:%d %b %Y} {a:%H:%M} to {b:%H:%M}"
    return f"{a:%d %b %Y %H:%M} to {b:%d %b %Y %H:%M}"


def cond_text(step: Step, c: Cond, checked: Checked) -> str:
    value = c.value if not isinstance(c.value, list) else ", ".join(map(str, c.value))
    how = checked.source_of.get(cond_key(step.id, c), "")
    return f"{c.field} {OPS_TEXT.get(c.op, c.op)} {value}" + (f" ({how})" if how else "")


def interpretation(plan: Plan, pack: Pack, checked: Checked) -> str:
    """One line per step: what was counted, where, with which conditions and their sources, over which period."""
    lines = []
    for step in plan.steps:
        t = pack.table(step.table)
        where = f"{t.name} (database {t.database_id})" if t else step.table
        what = ", ".join(_measure_text(m) for m in step.measures)
        parts = [f"{what} in {where}"]
        parts += [cond_text(step, c, checked) for c in step.where]
        for m in step.measures:
            parts += [f"{m.label}: {cond_text(step, c, checked)}" for c in m.where]
        if step.period:
            parts.append(period_text(step.period.start, step.period.end))
        if step.by or step.bucket:
            parts.append("per " + ", ".join(([step.bucket] if step.bucket else []) + list(step.by)))
        if step.limit:
            parts.append(f"top {step.limit}")
        lines.append(" · ".join(parts))
    head = "How this was counted: " if len(lines) == 1 else "How this was counted:\n- "
    return "\n\n_" + head + ("\n- ".join(lines)) + "_"


def _measure_text(m: Any) -> str:
    base = {"count": "count of rows", "count_distinct": f"distinct {m.field}", "sum": f"sum of {m.field}",
            "avg": f"average of {m.field or 'the value'}", "min": f"minimum of {m.field or 'the value'}",
            "max": f"maximum of {m.field or 'the value'}", "share": "share in %", "increase": "increase over the period",
            "rate": "rate per second", "quantile": f"quantile {m.q}", "formula": f"formula {m.formula}"}.get(m.fn, m.fn)
    unit = f", in {m.unit}" if m.unit else ""
    return f"{m.label} = {base}{unit}"


def result_rows(content: str) -> tuple[list[str], list[dict], dict]:
    try:
        res = json.loads(content)
    except (TypeError, ValueError):
        return [], [], {}
    if not isinstance(res, dict):
        return [], [], {}
    cols = [c.get("name") if isinstance(c, dict) else str(c) for c in res.get("columns") or []]
    rows = [r for r in res.get("rows") or [] if isinstance(r, dict)]
    return cols, rows, res


def sheet(plan: Plan, results: dict[str, str], pack: Pack) -> str:
    """The figures the model writes from: each step's rows (the first SHEET_ROWS), totals of counts, empties, cuts."""
    out = []
    for step in plan.steps:
        cols, rows, res = result_rows(results.get(step.id, ""))
        t = pack.table(step.table)
        out.append(f"Result {step.id} ({step.purpose or ', '.join(m.label for m in step.measures)}; "
                   f"{t.name if t else step.table}):")
        if res.get("success") is False or res.get("error"):
            out.append(f"  (the query failed: {str(res.get('error'))[:300]})")
            continue
        if not rows:
            out.append("  (no rows: nothing matched)")
            continue
        out.append("  | " + " | ".join(cols) + " |")
        for r in rows[:SHEET_ROWS]:
            out.append("  | " + " | ".join(fmt_number(r.get(c)) for c in cols) + " |")
        if len(rows) > SHEET_ROWS:
            out.append(f"  ... {len(rows) - SHEET_ROWS} more rows (shown to the user under the answer)")
        top = bool(step.limit) and len(rows) <= int(step.limit)  # the top N asked: all of it, not a cut
        cut = not top and (res.get("truncated") or (step.limit is None and res.get("row_count")
                                                    and int(res.get("row_count")) >= 1000))
        counts = [m.label for m in step.measures if m.fn in ("count", "count_distinct", "sum", "increase")]
        if len(rows) > 1 and counts and not cut:          # a total of cut rows is not the total
            totals = {c: sum(r.get(c) or 0 for r in rows if isinstance(r.get(c), (int, float))) for c in counts
                      if c in cols}
            if totals:
                out.append("  totals of these rows: " + ", ".join(f"{k} {fmt_number(v)}" for k, v in totals.items()))
        if cut:
            out.append("  (cut: the query gave its first rows only, there are more: no sum of these rows is a total)")
    return "\n".join(out)


def named(text: str, pack: Pack | None) -> str:
    """T1 / K2 written by the model: the table's or the item's name (the user never sees refs)."""
    if pack is None or not text:
        return text or ""

    def one(m: re.Match) -> str:
        ref = m.group(0)
        t = pack.table(ref)
        if t is not None:
            return t.name
        k = pack.item(ref)
        return f'"{k.title}"' if k is not None else ref
    return re.sub(r"\b[TK]\d{1,3}\b", one, text)


def clarify(plan: Plan, pack: Pack | None = None) -> str:
    """The question back, its options first and the question last (a reply is read as its answer only when the
    message ends with the question: agent.asks_back)."""
    text = named((plan.question_back or "").strip(), pack)
    if not plan.options:
        return text
    options = "\n".join(f"- {named(o, pack)}" for o in plan.options[:6])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    ask = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].rstrip("*_ )").endswith("?")), None)
    if ask is None:
        return f"{text}\n{options}"
    rest = "\n".join(ln for i, ln in enumerate(lines) if i != ask)
    return (f"{rest}\n" if rest else "") + f"{options}\n\n{lines[ask]}"


def cannot(plan: Plan, pack: Pack) -> str:
    text = named((plan.reason or "").strip(), pack)
    if pack.missing:
        text += "\n\nNot found in the data: " + "; ".join(pack.missing[:4]) + "."
    return text


def explain_messages(question: str, plan: Plan, pack: Pack) -> list[dict[str, str]]:
    items = [pack.item(r) for r in plan.knowledge]
    lines = [f"[{k.ref}] {k.kind} \"{k.title}\": {k.text}" for k in items if k is not None]
    return [{"role": "system", "content": "Answer the question from these knowledge items only, briefly, saying "
                                          "which item says it (by its title). If they do not answer it, say so."},
            {"role": "user", "content": "\n".join(lines) + f"\n\nQuestion: {question}"}]


def explained(answer: str, items: list[Any]) -> str:
    """The model's explanation when it says something of the items; else the items themselves (their text)."""
    body = re.sub(r"\[?K\d{1,3}\]?", "", answer or "").strip()
    known = {w for k in items for w in re.findall(r"[a-z0-9_]{4,}", f"{k.title} {k.text}".lower())}
    said = {w for w in re.findall(r"[a-z0-9_]{4,}", body.lower())} & known
    if len(body) >= 40 and len(said) >= 3:
        return answer
    return "\n".join(f"- **{k.title}**: {k.text}" for k in items)


def answer_messages(question: str, figures: str, previous: str = "") -> list[dict[str, str]]:
    user = f"Figures sheet:\n{figures}\n\n"
    if previous:
        user += f"(The chat before: {previous[:800]})\n"
    user += f"Question: {question}"
    return [{"role": "system", "content": ANSWER_SYSTEM}, {"role": "user", "content": user}]


def notes(checked: Checked, found_nothing: list[str], plan: Plan | None = None, pack: Pack | None = None) -> str:
    """What the user must know that the numbers do not say: a value not in the data, a period outside it."""
    out = ""
    for step_id, fld, value in checked.unknown_values:
        if step_id in found_nothing:
            out += f"\n\n({fld} = {value}: no such value in the data.)"
    for step in (plan.steps if plan is not None else []):
        t = pack.table(step.table) if pack is not None else None
        if step.id not in found_nothing or t is None or step.period is None or len(t.time_range) < 2:
            continue
        a, b = parse_time(step.period.start), parse_time(step.period.end)
        first, last = parse_time(str(t.time_range[0])), parse_time(str(t.time_range[-1]))
        if a and b and first and last and (b <= first or a > last):
            out += (f"\n\n(There is no data for that period: {t.name} holds data from {first:%d %b %Y} to "
                    f"{last:%d %b %Y}.)")
    return out


def strip_answer(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    return text
