"""Checking a plan against what was said and what the data is, before any query.

Errors go back to the model once (it writes the plan again); what code can settle it settles itself:

* every condition needs a source that says it: the question's or the chat's words (the value said in
  them: "failed" says FAILED, "45 minutes" says 2700 when the field is in seconds), a knowledge item of the
  pack whose text says the value, or an earlier step whose rows give the values (q1.NODE);
* the tables, fields, labels, functions and formulas exist and fit (a counter counts with increase, an index
  field is summed, a histogram gives quantiles);
* a value of a field with a known list of values is one of them (its case fixed); one not listed is
  checked in the data when the query runs;
* the period is the one the question names (its days, its spans), and has a source;
* a limit only when the question asks for the top N;
* the team's rules: code adds their condition to every step on a table the rule is about (that has the rule's
  field and, when the rule names what it is about, that data), unless the question lifts the rule (it asks for
  that value), already says that field, or the step groups by it;
* what the question names is counted: a value of the data it names (BILLING, UAT, BILLING_API, failed) is a
  condition or a group of the plan, a glossary term it uses (failed job) is counted as the glossary defines
  it, a unit it asks for (in GiB, in minutes) is on a measure (code converts), and a time bucket only when it
  asks for one (per hour, daily, over time, when...): else code removes it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from supagent.governed.decider import Pack, TableInfo, exact_count
from supagent.governed.plan import INDEX_FNS, METRIC_FNS, Cond, Plan, Step, source_of

TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
PER = re.compile(r"\b(?:per|by|for each|each|every|which|par|pour chaque|chaque|quels?|quelles?)\s+"
                 r"((?:\d+\s+)?(?:[a-zA-Z][\w-]*\s+){0,2}[a-zA-Z][\w-]*)", re.I)
TIME_WORDS = {"minute": "minute", "hour": "hour", "hourly": "hour", "heure": "hour", "day": "day", "daily": "day",
              "jour": "day", "week": "week", "weekly": "week", "semaine": "week", "month": "month", "mois": "month"}
TOP_N = re.compile(r"\b(top|first|best|worst|highest|lowest|most|least|biggest|largest|smallest|premiers?|"
                   r"meilleurs?|pires?)\b", re.I)
ASKS_TIME = re.compile(r"\b(?:per|by|each|every|which|what|par|chaque|quel(?:le)?s?)\s+(?:\d+\s+)?(minute|hour|day|week|"
                       r"month|heure|jour|semaine|mois)s?\b|\b(hourly|daily|weekly|monthly)\b|\b(?:over time|trend|"
                       r"timeline|time series|evolution|évolution|line chart|hour by hour|day by day|heure par heure|"
                       r"jour par jour|when|quand|at what time|à quelle heure)\b", re.I)
GRAIN = {"minute": "minute", "hour": "hour", "heure": "hour", "hourly": "hour", "day": "day", "jour": "day",
         "daily": "day", "week": "week", "semaine": "week", "weekly": "week", "month": "month", "mois": "month",
         "monthly": "month"}
ASKS_SHARE = re.compile(r"\b(share|percentage|percent|rate|ratio|proportion|availability|taux|pourcentage|"
                        r"disponibilit[ée])\b|%", re.I)
ASKED_UNIT = re.compile(r"\b(?:in|en)\s+(GiB|GB|MiB|MB|KiB|KB|TiB|TB|bytes|octets|minutes?|hours?|heures?|seconds?|"
                        r"secondes?|ms|milliseconds?|days?|jours?)\b", re.I)
UNIT_WORDS = {"gib": "GiB", "gb": "GiB", "mib": "MiB", "mb": "MiB", "kib": "KiB", "kb": "KiB", "tib": "TiB", "tb": "TiB",
              "bytes": "bytes", "octets": "bytes", "minute": "minutes", "minutes": "minutes", "hour": "hours",
              "hours": "hours", "heure": "hours", "heures": "hours", "second": "seconds", "seconds": "seconds",
              "seconde": "seconds", "secondes": "seconds", "ms": "milliseconds", "millisecond": "milliseconds",
              "milliseconds": "milliseconds", "day": "days", "days": "days", "jour": "days", "jours": "days"}
BUILTIN_FORMULAS = {
    "cpu_busy_pct": ("100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate)",
                     "CPU busy %: the share of CPU time not idle (node_cpu_seconds_total, label mode)"),
    "availability_pct": ("100 - 100 * SUM(increase) FILTER (WHERE code LIKE '5%') / SUM(increase)",
                         "availability %: the share of requests that were not 5xx errors (label code)"),
}


@dataclass
class Checked:
    plan: Plan
    errors: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)            # conditions code added (the team's rules)
    fixed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_values: list[tuple[str, str, Any]] = field(default_factory=list)   # (step, field, value)
    source_of: dict[str, str] = field(default_factory=dict)  # "<step>|<field>|<op>|<value>" -> how it is said

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_time(text: str) -> dt.datetime | None:
    t = (text or "").strip().replace("T", " ")[:19]
    for fmt in TIME_FORMATS:
        try:
            return dt.datetime.strptime(t, fmt.replace("T", " "))
        except ValueError:
            continue
    return None


def cond_key(step: str, c: Cond) -> str:
    return f"{step}|{c.field}|{c.op}|{c.value}"


def check(plan: Plan, pack: Pack, question: str, chat: str = "", today: dt.date | None = None,
          previous_steps: dict[str, list[str]] | None = None) -> Checked:
    """The plan checked (and settled where code can); `previous_steps`: {step id: its columns} when known."""
    from supagent.knowledge.period import _now

    today = today or _now().date()
    out = Checked(plan=plan)
    if plan.kind == "clarify":
        if not (plan.question_back or "").strip():
            out.errors.append("kind clarify needs question_back (the question to ask) and the options")
        return out
    if plan.kind == "cannot":
        if not (plan.reason or "").strip():
            out.errors.append("kind cannot needs the reason")
        return out
    if plan.kind == "explain":
        known = {k.ref for k in pack.knowledge}
        refs = [r for r in plan.knowledge if r.upper() in known]
        if not refs:
            out.errors.append(f"kind explain needs the K refs that answer the question (known: {', '.join(sorted(known))})")
        plan.knowledge = [r.upper() for r in refs]
        return out
    if not plan.steps:
        out.errors.append("kind answer needs at least one step")
        return out
    seen: dict[str, Step] = {}
    for step in plan.steps:
        step.id = (step.id or f"q{len(seen) + 1}").strip().lower()
        if step.id in seen:
            out.errors.append(f"step id {step.id} is used twice")
            continue
        _check_step(step, seen, pack, question, chat, today, out)
        seen[step.id] = step
    _check_date_conditions(plan, pack, out)
    _check_rule_conditions(plan, pack, out)
    _check_periods(plan, question, today, out)
    _check_bucket_asked(plan, question, out)
    _check_period_given(plan, question, today, out)
    _check_dimensions(plan, pack, question, out)
    _check_named_values(plan, pack, question, out)
    _check_glossary(plan, pack, question, out)
    _check_qualified_counts(plan, pack, question, out)
    _check_units_asked(plan, question, out)
    _check_ranked(plan, out)
    _check_share_asked(plan, question, out)
    return out


def _fields_used(plan: Plan, pack: Pack) -> dict[str, set[str]]:
    """{table ref: the fields its steps filter on or group by}."""
    used: dict[str, set[str]] = {}
    for s in plan.steps:
        t = pack.table(s.table)
        if t is None:
            continue
        u = used.setdefault(t.ref, set())
        u |= {c.field for c in s.where} | {c.field for m in s.measures for c in m.where} | set(s.by)
    return used


QUALIFIERS = ("fail", "kill", "success", "warn")   # the states a question qualifies what it counts with


def _check_qualified_counts(plan: Plan, pack: Pack, question: str, out: Checked) -> None:
    """A question that qualifies what it counts ("failed jobs", "cancelled trades", "successful runs") counts with
    a condition on the field that holds that state; a plan without one counts everything (the lab: every job
    counted as "failed" when the glossary term was missing)."""
    from supagent.knowledge.conditions import VALUE_WORDS
    from supagent.knowledge.describe import stem

    raw = set(re.findall(r"[a-zà-ÿ]+", (question or "").lower()))
    said = raw | {stem(w) for w in raw}
    families = [f for f in QUALIFIERS if said & (VALUE_WORDS[f] | {f})]
    if not families:
        return
    for ref, fields in _fields_used(plan, pack).items():
        t = pack.table(ref)
        if t is None:
            continue
        for fam in families:
            words = VALUE_WORDS[fam] | {fam}
            holders = []
            for name, col in t.columns.items():
                vals = [str(v) for v in (col.get("values") or [])]
                hits = [v for v in vals if any(stem(w) in words or w in words
                                               for w in re.findall(r"[a-z]+", v.lower()))]
                if hits:
                    holders.append((name, hits))
            if holders and not ({n for n, _h in holders} & fields):
                name, hits = holders[0]
                out.errors.append(f"the question counts {fam!r} ones: {t.name} says it in {name} ({', '.join(hits[:4])}): "
                                  f"add that condition (source: the question's words), or the count takes every "
                                  f"{name}")


def _check_named_values(plan: Plan, pack: Pack, question: str, out: Checked) -> None:
    """A value of the data the question names (BILLING, UAT, srv-a-01) is a condition or a group of the plan on
    its field: else the answer counts every value (the BILLING jobs in UAT counted as all jobs). Named = written
    as the data writes it, or a code-like value (digits, _ or -) in any case; an ordinary word ("the critical
    applications", when CRITICAL is a value) is not, nor a word the knowledge the plan uses explains."""
    used = _fields_used(plan, pack)
    per = [m.span() for m in PER.finditer(question or "")]
    explained = " ".join(f"{k.title} {k.text}" for k in pack.knowledge
                         if any(source_of(c.source) == ("knowledge", k.ref.upper()) for s in plan.steps
                                for c in list(s.where) + [x for m in s.measures for x in m.where])).lower()
    for ref, fields in used.items():
        t = pack.table(ref)
        names = {c.lower() for c in t.columns} | {w.lower() for w in re.split(r"[^A-Za-z0-9]+", t.name) if w}
        missing: dict[str, list[str]] = {}
        for fld, col in t.columns.items():
            if fld == t.time_field:
                continue
            for v in col.get("values") or []:
                v = str(v)
                if len(v) < 2 or re.fullmatch(r"[\d.:+-]+", v) or v.lower() in names:
                    continue
                code_like = bool(re.search(r"[\d_-]", v))
                m = re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", question or "", 0 if not code_like else re.I)
                if m is None or any(a <= m.start() < b for a, b in per):    # "per node": a group, said so
                    continue
                if re.search(r"(?<![\w-])" + re.escape(v.lower()) + r"(?![\w-])", explained):
                    continue                                   # the knowledge the plan counts with says it
                missing.setdefault(v, []).append(fld)
        for v, flds in missing.items():
            if not set(flds) & fields:
                out.errors.append(f"the question names {v!r}, a value of {' or '.join(flds)} in {t.name}: count with "
                                  f"a condition {flds[0]} = {v!r} (its source: the question's words) or group by "
                                  f"{flds[0]}; else the answer counts every {flds[0]}")


def _check_glossary(plan: Plan, pack: Pack, question: str, out: Checked) -> None:
    """A glossary term the question uses (failed job) is counted as the glossary defines it (STATUS_INFO =
    'FAILED') on the tables that have that field."""
    from supagent.knowledge.resolve import terms as stems
    from supagent.knowledge.rulecheck import PAIR

    asked = set(stems(question or ""))
    used = _fields_used(plan, pack)
    for k in pack.knowledge:
        if k.kind != "glossary":
            continue
        term = re.sub(r"\s*\([^)]*\)\s*$", "", k.title or "").strip()
        words = set(stems(term))
        if not words or not words <= asked:
            continue
        pairs = [(m.group(1), m.group(2)) for m in PAIR.finditer(k.text or "")]
        for ref, fields in used.items():
            t = pack.table(ref)
            here = [(f, v) for f, v in pairs if f in t.columns]
            if here and not {f for f, _v in here} & fields:
                f, v = here[0]
                out.errors.append(f"the question says {term!r}: {k.ref} ({k.title}) defines it as {f} = {v!r}: count "
                                  f"with that condition in {t.name} (source {k.ref})")


def _check_units_asked(plan: Plan, question: str, out: Checked) -> None:
    """A unit the question asks for (in GiB, in minutes) is on a measure: code converts, the model never does."""
    m = ASKED_UNIT.search(question or "")
    want = UNIT_WORDS.get(m.group(1).lower()) if m else None
    for s in plan.steps:                        # a unit nobody asked for, not the field's own: not said in the answer
        for x in s.measures:                    # (a PnL summed "in percent", a count "in seconds")
            if x.unit and x.unit != want and x.unit != (x.source_unit or ""):
                out.fixed.append(f"{s.id}: unit {x.unit!r} of {x.label!r} removed (the question does not ask for it)")
                x.unit = None
    if not want:
        return
    measures = [x for s in plan.steps for x in s.measures if x.fn not in ("count", "count_distinct", "share")]
    if measures and not any((x.unit or "") == want for x in measures):
        out.errors.append(f"the question asks {m.group(0).strip()!r}: set unit {want!r} on the measure that gives it "
                          f"(and source_unit, the field's unit, when the table does not say it), so that code converts")


def _check_bucket_asked(plan: Plan, question: str, out: Checked) -> None:
    """A time bucket only when the question asks for one (per hour, daily, over time, when...), of the grain it
    asks (a weekly review is not per day): a total cut per minute nobody asked for is a total the answer gets
    wrong."""
    found = list(ASKS_TIME.finditer(question or ""))
    grains = {GRAIN[(m.group(1) or m.group(2) or "").lower()] for m in found if (m.group(1) or m.group(2))}
    free = any(not (m.group(1) or m.group(2)) for m in found)       # over time, trend, when: any grain
    for s in plan.steps:
        if not s.bucket or free or (grains and s.bucket.split()[-1].rstrip("s") in grains):
            continue
        out.fixed.append(f"{s.id}: bucket {s.bucket} removed (the question does not ask per {s.bucket}"
                         + (f"; it asks per {', '.join(sorted(grains))}" if grains else " or over time") + ")")
        s.bucket = None


def _check_period_given(plan: Plan, question: str, today: dt.date, out: Checked) -> None:
    """The period the question names is on every step: a step without one counts all the data there is."""
    from supagent.knowledge.period import days_named, ranges_named

    if not (days_named(question, today) or ranges_named(question, today)):
        return
    for s in plan.steps:
        if s.period is None:
            out.errors.append(f"{s.id}: the question names its period: set period on this step (start, end, source "
                              f"the question's words)")


def _check_ranked(plan: Plan, out: Checked) -> None:
    """A top N is ordered by the measure it ranks (the most heap used: ordered by it, descending)."""
    for s in plan.steps:
        labels = {m.label.lower() for m in s.measures}
        if re.search(r"\b(first|last|earliest|latest|premi\w*|derni\w*)\b", s.limit_source or "", re.I):
            continue                                     # the first N hours: in time order, not a ranking
        if s.limit is not None and not any(o.by.lower() in labels for o in s.order):
            out.errors.append(f"{s.id}: limit {s.limit} ranks by a measure: order by the measure it ranks "
                              f"({', '.join(m.label for m in s.measures)}), desc for the most, asc for the least")


def _check_share_asked(plan: Plan, question: str, out: Checked) -> None:
    """A share, a rate, an availability the question asks for is a measure of its own (share, formula or rate):
    never two counts the answer divides."""
    m = ASKS_SHARE.search(question or "")
    if m and plan.steps and not any(x.fn in ("share", "formula", "rate") for s in plan.steps for x in s.measures):
        out.errors.append(f"the question asks for {m.group(0)!r}: a measure fn share (its where = the part counted), "
                          f"or a formula of the knowledge, gives it; the answer never divides two counts itself")


def _check_dimensions(plan: Plan, pack: Pack, question: str, out: Checked) -> None:
    """What the question asks per (per business line, which application, each server, per hour) is grouped by
    in a step on a table that has it: else the plan answers something else than what was asked."""
    from supagent.knowledge.resolve import SYNONYMS, name_tokens, terms

    for m in PER.finditer(question or ""):
        phrase = m.group(1).strip()
        words = [w for w in re.findall(r"[a-zA-Z]+", phrase.lower())]
        time = next((TIME_WORDS[w] for w in words[:1] if w in TIME_WORDS), None)
        if time:
            if not any(s.bucket for s in plan.steps):
                out.errors.append(f"the question asks per {time} ({m.group(0).strip()!r}): set bucket {time!r} on "
                                  f"the step that counts it")
            continue
        stems = set(terms(phrase))
        stems |= {x for st in list(stems) for x in SYNONYMS.get(st, set())}
        if not stems:
            continue
        fields: dict[str, list[str]] = {}               # step id -> its table's fields the phrase names
        for step in plan.steps:
            t = pack.table(step.table)
            if t is None:
                continue
            hit = [c for c in t.columns if set(name_tokens(c)) & stems and c != t.time_field]
            if hit:
                fields[step.id] = hit
        if not fields:
            continue                                     # a word of the question, not a field of these tables
        if not any(set(s.by) & set(fields.get(s.id, [])) for s in plan.steps):
            names = sorted({f for fs in fields.values() for f in fs})
            out.errors.append(f"the question asks {m.group(0).strip()!r}: group by {' or '.join(names)} in the step "
                              f"that counts it (by), so that the result has one row each")


def _check_step(step: Step, earlier: dict[str, Step], pack: Pack, question: str, chat: str, today: dt.date,
                out: Checked) -> None:
    t = pack.table(step.table)
    if t is None:
        out.errors.append(f"{step.id}: table {step.table!r} is not one of the tables given "
                          f"({', '.join(x.ref for x in pack.tables)})")
        return
    step.table = t.ref
    cols = {c.lower(): c for c in t.columns}
    if not step.measures:
        out.errors.append(f"{step.id}: no measure (what to count)")
    for m in step.measures:
        _check_measure(step, m, t, cols, out)
        for c in m.where:
            _check_cond(step, c, t, cols, earlier, pack, question, chat, out, where=f"measure {m.label!r}")
        if m.fn == "share" and not m.where:
            out.errors.append(f"{step.id}: measure {m.label!r} is a share: its where says the part counted")
    time_cols = {c.lower() for c in (t.time_field, "ts", "time", "timestamp") if c}
    if any(str(b).lower() in time_cols for b in step.by):     # a time is grouped by a bucket, never raw
        if step.bucket:
            out.fixed.append(f"{step.id}: the raw time field left out of by (the bucket {step.bucket} groups it)")
            step.by = [b for b in step.by if str(b).lower() not in time_cols]
        else:
            out.errors.append(f"{step.id}: group a time with bucket (hour, day, week...), not its raw field")
    for i, b in enumerate(step.by):
        real = cols.get(str(b).lower())
        if real is None:
            out.errors.append(f"{step.id}: {b!r} to group by is not a field or label of {t.name}")
        else:
            step.by[i] = real
    if step.bucket and t.kind == "index" and not t.time_field:
        out.errors.append(f"{step.id}: {t.name} has no time field to group by {step.bucket}")
    for c in step.where:
        _check_cond(step, c, t, cols, earlier, pack, question, chat, out)
    if step.limit is not None:
        kind, quote = source_of(step.limit_source)
        said = kind in ("question", "chat") and (str(step.limit) in quote or TOP_N.search(quote))
        if not (said and _quoted(quote, question, chat)):
            out.fixed.append(f"{step.id}: limit {step.limit} removed (the question does not ask for the top "
                             f"{step.limit})")
            step.limit, step.limit_source = None, None
    labels = {m.label.lower() for m in step.measures} | {b.lower() for b in step.by}
    kept = [o for o in step.order if o.by.lower() in labels]
    for o in step.order:
        if o not in kept:                                # an order on nothing the step returns: dropped
            out.fixed.append(f"{step.id}: order by {o.by!r} dropped (not a measure or a group of the step)")
    step.order = kept
    _apply_rules(step, t, question, pack, out)


def _check_measure(step: Step, m: Any, t: TableInfo, cols: dict[str, str], out: Checked) -> None:
    if t.kind == "index":
        if m.fn not in INDEX_FNS:
            out.errors.append(f"{step.id}: {m.fn} is for metrics; an index counts documents (count), or sums, "
                              f"averages, min, max a field")
            return
        if m.fn in ("count_distinct", "sum", "avg", "min", "max"):
            real = cols.get(str(m.field or "").lower())
            if real is None:
                out.errors.append(f"{step.id}: measure {m.label!r} needs a field of {t.name} ({m.field!r} is not one)")
                return
            m.field = real
        _check_unit(step, m, t, out)
        return
    mtype = (t.metric_type or "").lower()
    if m.fn not in METRIC_FNS:
        out.errors.append(f"{step.id}: {m.fn} is for an index; a metric: increase, rate, avg, min, max, quantile, "
                          f"share, formula")
    elif mtype == "counter" and m.fn in ("avg", "min", "max"):
        out.errors.append(f"{step.id}: {t.name} is a counter: how many in the period = increase, per second = rate")
    elif m.fn == "quantile" and not (t.name.endswith("_bucket") or mtype == "histogram"):
        out.errors.append(f"{step.id}: quantile needs a histogram (a _bucket metric); {t.name} is not one")
    elif m.fn == "quantile" and not (m.q and 0 < m.q < 1):
        out.errors.append(f"{step.id}: quantile needs q between 0 and 1")
    elif m.fn == "formula":
        if (m.formula or "") not in BUILTIN_FORMULAS and not _catalog_formula(m.formula):
            out.errors.append(f"{step.id}: formula {m.formula!r} is unknown (known: "
                              f"{', '.join(sorted(BUILTIN_FORMULAS))} and the catalog's)")
    _check_unit(step, m, t, out)


def _check_unit(step: Step, m: Any, t: TableInfo, out: Checked) -> None:
    """A unit to give the measure in needs the unit of the value (the table, the name, or source_unit)."""
    if not m.unit or m.fn in ("count", "count_distinct", "share"):
        return
    from supagent.governed.compile import CompileError, factor, unit_of

    try:
        factor(unit_of(t, m.field, m), m.unit)
    except CompileError as ex:
        out.errors.append(f"{step.id}: measure {m.label!r}: {ex}")


def _catalog_formula(name: str | None) -> str | None:
    if not name:
        return None
    try:
        from supagent.knowledge.catalog import formulas

        for f in formulas():
            if f["title"].strip().lower() == name.strip().lower():
                return f["text"]
    except Exception:  # pylint: disable=broad-except
        pass
    return None


def _check_cond(step: Step, c: Cond, t: TableInfo, cols: dict[str, str], earlier: dict[str, Step], pack: Pack,
                question: str, chat: str, out: Checked, where: str = "where") -> None:
    real = cols.get(str(c.field).lower())
    if real is None:
        out.errors.append(f"{step.id}: {where}: {c.field!r} is not a field or label of {t.name}")
        return
    c.field = real
    kind, ref = source_of(c.source)
    label = f"{step.id}: {where}: {c.field} {c.op} {c.value!r}"
    if isinstance(c.value, str) and re.fullmatch(r"q\d+\.[\w.@-]+", c.value.strip(), re.I):
        sid, col = c.value.strip().split(".", 1)
        if sid.lower() not in earlier:
            out.errors.append(f"{label}: step {sid} is not an earlier step")
        elif kind != "step" or ref != sid.lower():
            c.source = sid.lower()
        out.source_of[cond_key(step.id, c)] = f"found by step {sid.lower()}"
        return
    if kind in ("question", "chat"):
        if not _quoted(ref, question, chat):
            out.errors.append(f"{label}: its source {c.source!r} is not in the {'question' if kind == 'question' else 'chat'}")
        elif not _said(c, ref + " " + (question if kind == "question" else chat)):
            out.errors.append(f"{label}: the words {ref!r} do not say {c.value!r}; give the words that say it, or "
                              f"remove the condition if nobody asked for it")
        else:
            out.source_of[cond_key(step.id, c)] = f'{"you said" if kind == "question" else "said in the chat"} "{ref}"'
    elif kind == "knowledge":
        item = pack.item(ref)
        if item is None:
            out.errors.append(f"{label}: {ref} is not one of the knowledge items given")
        elif not _said(c, f"{item.title} {item.text}"):
            out.errors.append(f"{label}: {ref} ({item.title}) does not say {c.value!r}")
        else:
            out.source_of[cond_key(step.id, c)] = f'{_kind_label(item.kind)} "{item.title}"'
    elif kind == "step":
        out.errors.append(f"{label}: a condition from {ref} needs the value \"{ref}.COLUMN\"")
    else:
        out.errors.append(f"{label}: no source. Every condition needs one (question: <its words>, a K ref, a step); "
                          "remove a condition nobody asked for")
        return
    _check_value(step, c, t, out)


def _kind_label(kind: str) -> str:
    return {"rule": "the team's rule", "glossary": "the glossary's", "memory": "the memory", "recipe": "a learned answer",
            "doc": "the document", "note": "the note", "context": "the Context"}.get(kind, kind)


def _quoted(quote: str, question: str, chat: str) -> bool:
    """The quote is (mostly) words of the question or the chat."""
    words = re.findall(r"[\w%.-]+", (quote or "").lower())
    if not words:
        return False
    text = f"{question}\n{chat}".lower()
    found = sum(1 for w in words if w in text)
    return found >= max(1, int(0.6 * len(words) + 0.5))


def _said(c: Cond, text: str) -> bool:
    from supagent.knowledge.conditions import Support, _value_said

    s = Support()
    s.add(text, people=True)
    values = c.value if isinstance(c.value, list) else [c.value]
    if c.op in ("is null", "is not null"):
        return bool(re.search(r"\b(empty|missing|null|none|no |without|sans|vide)", text, re.I)) or \
            c.field.lower() in text.lower()
    return all(_value_said(_number(v), c.field, s) for v in values if v is not None)


def _number(v: Any) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _check_value(step: Step, c: Cond, t: TableInfo, out: Checked) -> None:
    """A value of a listed field: one of its values (case fixed); not listed: checked when the query runs."""
    col = t.columns.get(c.field) or {}
    known = [str(v) for v in col.get("values") or []]
    if not known or c.op in ("like", "not like", ">", ">=", "<", "<=", "is null", "is not null"):
        return
    values = c.value if isinstance(c.value, list) else [c.value]
    fixed = []
    for v in values:
        match = next((k for k in known if k == str(v)), None) or next((k for k in known if k.lower() == str(v).lower()),
                                                                     None)
        if match is None:
            count = exact_count(col.get("cardinality"))       # ">=200": not every value is known
            full = count is not None and count <= len(known)
            if full:
                out.warnings.append(f"{step.id}: {c.field} has no value {v!r} (its values: {', '.join(known[:12])})")
            out.unknown_values.append((step.id, c.field, v))
            fixed.append(v)
        else:
            if match != str(v):
                out.fixed.append(f"{step.id}: {c.field} {v!r} written as {match!r}")
            fixed.append(match)
    c.value = fixed if isinstance(c.value, list) else fixed[0]


def _apply_rules(step: Step, t: TableInfo, question: str, pack: Pack, out: Checked) -> None:
    """The team's rules on a field of this table: added unless the question lifts them or says that field."""
    from supagent.knowledge.rulecheck import EXCLUDING, KEEPING, PAIR, _checkable, concerns, possible_in, team_rules

    asked = (question or "").lower()
    said_fields = {c.field for c in step.where if source_of(c.source)[0] in ("question", "chat")}
    for rule in team_rules():
        found = _checkable(rule, asked)
        if found is None:
            continue
        about = concerns(rule, found[0])
        if about is not None and t.name not in about:
            continue                                  # a rule about another table's data (VaR, not trades)
        text = rule["text"]
        for m in PAIR.finditer(text):
            fld, value = m.group(1), m.group(2)
            real = next((c for c in t.columns if c == fld), None)
            if real is None or real in said_fields or real in (step.by or []):
                continue                              # grouped per that field: every value, not the rule's
            if possible_in(t.columns.get(real) or {}, value) is False:
                continue                              # its value is not in this data (no BOOK = 'ALL' in PnL)
            before = text[max(0, m.start() - 80):m.start()]
            exclude = bool(EXCLUDING.search(before)) and not KEEPING.search(before)
            if any(c.field == real for c in step.where):
                continue
            item = next((k for k in pack.knowledge if k.kind == "rule" and k.text.strip() == text.strip()), None)
            source = item.ref if item is not None else f"rule: {rule['title']}"
            c = Cond(field=real, op="!=" if exclude else "=", value=value, source=source)
            step.where.append(c)
            out.added.append(f"{step.id}: {real} {'!=' if exclude else '='} {value!r} (the team's rule "
                             f"\"{rule['title']}\")")
            out.source_of[cond_key(step.id, c)] = f'the team\'s rule "{rule["title"]}"'


DATE_VALUE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?$")


def _check_date_conditions(plan: Plan, pack: Pack, out: Checked) -> None:
    """A day said as an equality on a date field ("COB_DATE = 2026-09-23") finds nothing where the field holds a
    time too (dates stored at 00:00 UTC read 02:00 here): on the table's time field it is the step's period (or
    goes when the period already says that day); on another date field it becomes that day's range."""
    from supagent.governed.plan import Period

    for s in plan.steps:
        t = pack.table(s.table)
        if t is None:
            continue
        dates = {n for n, c in t.columns.items() if str(c.get("type") or "").lower().startswith("date")}
        if t.time_field:
            dates.add(t.time_field)
        for holder in [s, *s.measures]:
            keep: list[Cond] = []
            for c in holder.where:
                m = DATE_VALUE.match(str(c.value).strip()) if (c.field in dates and c.op == "="
                                                              and not isinstance(c.value, list)) else None
                if m is None:
                    keep.append(c)
                    continue
                day = dt.date.fromisoformat(m.group(1))
                start, end = f"{day} 00:00", f"{day + dt.timedelta(days=1)} 00:00"
                if c.field != t.time_field:
                    keep += [Cond(field=c.field, op=">=", value=start, source=c.source),
                             Cond(field=c.field, op="<", value=end, source=c.source)]
                    out.fixed.append(f"{s.id}: {c.field} = {c.value} made {start} to {end} (a date field holds a "
                                     "time too)")
                elif s.period is None:
                    s.period = Period(start=start, end=end, source=c.source)
                    out.fixed.append(f"{s.id}: {c.field} = {c.value} made the period {start} to {end}")
                else:
                    p_start, p_end = parse_time(s.period.start), parse_time(s.period.end)
                    if p_start is not None and p_end is not None and p_start <= dt.datetime(day.year, day.month,
                                                                                           day.day) < p_end:
                        out.fixed.append(f"{s.id}: {c.field} = {c.value} removed (the period says that day)")
                    else:
                        keep.append(c)               # another day than the period: the period check says so
            holder.where = keep


def _check_rule_conditions(plan: Plan, pack: Pack, out: Checked) -> None:
    """A condition the model took from a team rule follows the rule's own limits, as the ones code adds: not on a
    step grouped by its field (per BOOK: every book, not BOOK = 'ALL'), not where its value cannot be."""
    from supagent.knowledge.rulecheck import possible_in

    for s in plan.steps:
        t = pack.table(s.table)
        for holder in [s, *s.measures]:
            keep: list[Cond] = []
            for c in holder.where:
                kind, ref = source_of(c.source)
                item = pack.item(ref) if kind == "knowledge" else None
                if item is None or item.kind != "rule":
                    keep.append(c)
                    continue
                values = c.value if isinstance(c.value, list) else [c.value]
                if c.field in (s.by or []) and c.op in ("=", "in"):
                    out.fixed.append(f"{s.id}: {c.field} = {c.value} (from {item.title!r}) removed: the step is per "
                                     f"{c.field}")
                    continue
                if t is not None and values and all(possible_in(t.columns.get(c.field) or {}, v) is False
                                                    for v in values if v is not None):
                    out.fixed.append(f"{s.id}: {c.field} = {c.value} (from {item.title!r}) removed: {t.name} has no "
                                     "such value")
                    continue
                keep.append(c)
            holder.where = keep


def _check_periods(plan: Plan, question: str, today: dt.date, out: Checked) -> None:
    """Each step's period: a start before its end, a source, and one of the periods the question names."""
    from supagent.knowledge.period import days_named, ranges_named

    days = days_named(question, today)
    spans = ranges_named(question, today)
    named = [(dt.datetime(d.year, d.month, d.day), dt.datetime(d.year, d.month, d.day) + dt.timedelta(days=1))
             for d in days] + spans
    for step in plan.steps:
        p = step.period
        if p is None:
            continue
        a, b = parse_time(p.start), parse_time(p.end)
        if a is None or b is None or a >= b:
            out.errors.append(f"{step.id}: period {p.start} to {p.end}: a start before an end, as YYYY-MM-DD HH:MM")
            continue
        p.start, p.end = a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M")
        kind, _ref = source_of(p.source)
        if kind not in ("question", "chat", "knowledge", "step"):
            out.errors.append(f"{step.id}: the period needs its source (question: <its words>, a K ref)")
        if named and not any(s <= a and b <= e for s, e in named):
            spans_text = "; ".join(f"{s:%Y-%m-%d %H:%M} to {e:%Y-%m-%d %H:%M}" for s, e in named)
            out.errors.append(f"{step.id}: period {p.start} to {p.end} is not within what the question names "
                              f"({spans_text})")
