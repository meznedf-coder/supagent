"""How a measure of a table compares with its usual, group by group (0.9): the figures behind the tool
compare_groups. For each value of a field (an application, a server, a step...), the measure over a window against
the same window of the earlier days that have data (their median): which values are off, and whether the change
is concentrated on a few of them (a cause is usually where the excess is) or spread over all (then the field is
not what explains it).

No LLM and nothing about a domain: a table, a measure, a scope, fields to group by. The queries are plain
aggregations the connectors push down (one per window and field).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass
from typing import Any

FIELD = re.compile(r"^[A-Za-z0-9_@.\- ]{1,120}$")
MEASURE = re.compile(r"^\s*(count|avg|average|mean|sum|max|min|ratio)\s*(?:\(\s*(.*?)\s*\))?\s*$", re.I)
HIGH, LOW = 1.3, 0.77          # a value this far from its usual stands out
SHOWN = 10                     # groups shown per field
NOISE = 3.0                    # ... and beyond this many median absolute deviations of its own earlier days
COUNT_NOISE = 3.0              # a count differs when it is this many square roots of its usual away (13 against 8 is not)
APPEARED = 5.0                 # rows of a value that has none of them usually: this many at least to say so
MIN_ROWS = 3                   # an average over fewer rows than this is not judged
MEASURES = 9                   # measures of a table compared in one call ("all")
INCOMPLETE = 0.9               # an average that fewer rows than this share of the usual have yet is not compared
STRONG = 100.0                 # the strength of a value that fell to nothing
NONE = "(none)"                # the rows that have no value of a field
FEW = 12                       # a field of more values than this localizes a measure as usual in total only at x2
SCALE = 0.2                    # a value differs when it is off by this share of the measure's usual over all the rows
REPLACED_ROWS = 8              # a value that took the place of another is judged over this many rows at least
AGE_HIGH = 2.0                 # the rows waiting at a stage: their average wait stands out at twice its usual
JOURNEY = 0.1                  # a stage takes longer when it adds this share of the whole way's usual time
STATE = 0.05                   # values new now on this share of the rows, none gone: a field that changes as a run advances
MATERIAL = 0.05                # rows waiting at a stage beyond usual, as a share of all the rows: the stage is off
DOMINANT = 0.95                # values that hold this share of the rows say nothing of where a change is
CLEAN_VALUES = 6               # more than half of a field's values stand out, the others as usual: still a place to
SPARED = 0.25                  # look when they are at most this many, and the others hold this share of the rows
LONGER = 1.5                   # a stage this many times its usual time is off on its own, when it adds ...
STEP = 0.05                    # ... this share of the whole way's usual time
YOUNG = 0.9                    # rows waiting at a stage for less than this share of the usual wait came late to it
STEADY_DAYS = 5                # a count within ...
STEADY = 0.1                   # ... this share of its usual on that many earlier days at least is steady


class GroupsError(ValueError):
    pass


def name(field: str) -> str:
    """A field name as written by the model (quotes around it or not), checked."""
    f = str(field or "").strip().strip('"').strip("`").strip()
    if not FIELD.match(f):
        raise GroupsError(f"not a field name: {field!r}")
    return f


@dataclass
class Measure:
    kind: str                           # count | avg | sum | max | min | ratio
    fields: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "Measure":
        m = MEASURE.match(text or "count")
        if not m:
            raise GroupsError('measure: "count", "avg(FIELD)", "sum(FIELD)", "max(FIELD)", "min(FIELD)" or '
                              '"ratio(FIELD_A, FIELD_B)" (the sum of A over the sum of B)')
        kind = {"average": "avg", "mean": "avg"}.get(m.group(1).lower(), m.group(1).lower())
        args = [a for a in re.split(r"\s*,\s*", m.group(2) or "") if a and a != "*"]
        if kind == "count":
            return cls("count")
        if kind == "ratio":
            if len(args) != 2:
                raise GroupsError("ratio(FIELD_A, FIELD_B): two fields")
            return cls("ratio", (name(args[0]), name(args[1])))
        if len(args) != 1:
            raise GroupsError(f"{kind}(FIELD): one field")
        return cls(kind, (name(args[0]),))

    def select(self) -> str:
        if self.kind == "count":
            return "COUNT(*) AS n"
        if self.kind == "ratio":
            return f'COUNT(*) AS n, SUM("{self.fields[0]}") AS a, SUM("{self.fields[1]}") AS b'
        return f'COUNT(*) AS n, {self.kind.upper()}("{self.fields[0]}") AS a'

    def not_null(self) -> str:
        """Only the rows that have the measure (a run that has not ended has no duration yet)."""
        return " AND ".join(f'"{f}" IS NOT NULL' for f in self.fields)

    def value(self, row: tuple) -> float | None:
        n = row[0]
        if self.kind == "count":
            return float(n or 0)
        a = row[1]
        if self.kind == "ratio":
            b = row[2]
            return float(a) / float(b) if a is not None and b else None
        return None if a is None else float(a)

    def text(self) -> str:
        if self.kind == "count":
            return "number of rows"
        if self.kind == "reached":              # ("then": at the end of the window, on each day alike)
            return f"rows that had reached {self.fields[0]} by then"
        if self.kind == "between":
            return f"rows past {self.fields[0]} and not yet at {self.fields[1]} then"
        if self.kind == "lapse":
            return f"avg seconds from {self.fields[0]} to {self.fields[1]}"
        if self.kind == "age":
            return f"avg seconds since {self.fields[0]} for the rows not yet at {self.fields[1]} then"
        if self.kind == "ratio":
            return f"sum of {self.fields[0]} / sum of {self.fields[1]}"
        return f"{self.kind} of {self.fields[0]}"

    @property
    def counting(self) -> bool:
        return self.kind in ("count", "reached", "between")


def seconds_between(a: Any, b: Any) -> float | None:
    """From an average time to another (two datetimes, or two numbers of seconds or milliseconds)."""
    if a is None or b is None:
        return None
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return max(0.0, (b - a).total_seconds())
    try:
        d = float(b) - float(a)
    except (TypeError, ValueError):
        return None
    return max(0.0, d / 1000.0 if abs(float(b)) > 1e11 else d)


class Plan:
    """Several measures of a table read by one query per window and field: the rows, and for each measure its
    value with the number of rows that have it (a run that has not ended has no duration: it counts for the
    measures it has). `as_of` {measure number: time field}: that measure is over the rows whose time field was
    before the end of the window read (the runs that had ended by that time of day, on each day alike)."""

    def __init__(self, measures: list[Measure], filter_clause: bool = True, as_of: dict[int, str] | None = None):
        self.measures = measures
        self.filter_clause = filter_clause
        self.as_of = as_of or {}

    def _if(self, agg: str, field: str, conds: list[str]) -> str:
        if not conds:
            return f'{agg}("{field}")'
        cond = " AND ".join(conds)
        if self.filter_clause:
            return f'{agg}("{field}") FILTER (WHERE {cond})'
        return f'{agg}(CASE WHEN {cond} THEN "{field}" END)'

    def select(self, end: str = "") -> str:
        """`end`: the end of the window read, as a literal (what had reached a time field by then)."""
        cols = ["COUNT(*) AS n"]
        for i, m in enumerate(self.measures):
            if m.kind == "count":
                continue
            if m.kind == "reached":
                cols.append(f'COUNT(*) FILTER (WHERE "{m.fields[0]}" < {end}) AS a{i}' if self.filter_clause else
                            f'SUM(CASE WHEN "{m.fields[0]}" < {end} THEN 1 ELSE 0 END) AS a{i}')
                continue
            by_then = [f'"{self.as_of[i]}" < {end}'] if i in self.as_of and end else []
            if m.kind == "lapse":               # the average of each time field, over the rows that have both
                a, b = m.fields
                both = [f'"{a}" IS NOT NULL', f'"{b}" IS NOT NULL'] + ([f'"{b}" < {end}'] if end else [])
                cols += [self._if("AVG", a, both) + f" AS a{i}", self._if("AVG", b, both) + f" AS b{i}",
                         self._if("COUNT", b, both) + f" AS c{i}"]
                continue
            if m.kind == "age":                 # the rows at a stage at that time: since when, on average
                a, b = m.fields
                there = [f'"{a}" < {end}', f'("{b}" IS NULL OR "{b}" >= {end})']
                cols += [self._if("AVG", a, there) + f" AS a{i}", self._if("COUNT", a, there) + f" AS c{i}"]
                continue
            if m.kind == "ratio":
                a, b = m.fields
                has_a, has_b = [f'"{a}" IS NOT NULL'] + by_then, [f'"{b}" IS NOT NULL'] + by_then
                cols += [self._if("SUM", a, has_b) + f" AS a{i}", self._if("SUM", b, has_a) + f" AS b{i}",
                         self._if("COUNT", a, has_b) + f" AS c{i}"]
            else:
                cols += [self._if(m.kind.upper(), m.fields[0], by_then) + f" AS a{i}",
                         self._if("COUNT", m.fields[0], by_then) + f" AS c{i}"]
        return ", ".join(cols)

    def values(self, row: tuple, end: dt.datetime | None = None) -> list[tuple[int, float | None]]:
        """One (rows that have the measure, its value) per measure, from a row of select() (after the group's
        column when there is one: pass the row from its count on). `end`: the end of the window read."""
        out: list[tuple[int, float | None]] = []
        at = 1
        for m in self.measures:
            if m.kind == "age":
                a, c = row[at], row[at + 1]
                at += 2
                out.append((int(c or 0), seconds_between(a, end) if isinstance(a, dt.datetime) and end else None))
                continue
            if m.kind == "count":
                out.append((int(row[0] or 0), float(row[0] or 0)))
            elif m.kind == "reached":
                out.append((int(row[at] or 0), float(row[at] or 0)))
                at += 1
            elif m.kind == "lapse":
                a, b, c = row[at], row[at + 1], row[at + 2]
                at += 3
                out.append((int(c or 0), seconds_between(a, b)))
            elif m.kind == "ratio":
                a, b, c = row[at], row[at + 1], row[at + 2]
                at += 3
                out.append((int(c or 0), float(a) / float(b) if a is not None and b else None))
            else:
                a, c = row[at], row[at + 1]
                at += 2
                out.append((int(c or 0), None if a is None else float(a)))
        return out


REFERENCE = re.compile(
    r"^\s*(?:(yesterday|hier|the day before|last week|a week ago|last month|a month ago|last year|a year ago)|"
    r"(?:il y a\s+)?(\d{1,3}|an?|one|une?)\s*(d|days?|jours?|w|weeks?|semaines?|m|months?|mois|y|years?|ans?)"
    r"(?:\s+(?:ago|before|earlier|back))?|(\d{4}-\d{2}-\d{2}))\s*$", re.I)
REFERENCES = 5                 # reference days compared in one call
REFERENCE_DAYS = 800           # how far back a reference may be
WORDS = {"yesterday": (1, "day"), "hier": (1, "day"), "the day before": (1, "day"), "last week": (1, "week"),
         "a week ago": (1, "week"), "last month": (1, "month"), "a month ago": (1, "month"), "last year": (1, "year"),
         "a year ago": (1, "year")}


@dataclass
class Reference:
    """A day a window is compared with, besides its usual: yesterday, a week ago, three weeks ago, three months
    ago, a date. Weeks, months and years are counted in whole weeks: the same weekday (a Monday with a Monday)."""
    label: str                          # as it was asked: "3 weeks ago"
    kind: str                           # day | week | date
    days: int = 0                       # how far back
    date: dt.date | None = None         # (kind date)


def references(texts: list[str] | str | None) -> list[Reference]:
    """The reference days asked: "yesterday", "2 days ago", "1 week ago", "3 weeks ago", "2 months ago", "1 year
    ago", "2026-06-15" (a list, or one text with commas). A month is 30.44 days rounded to whole weeks (1 month:
    4 weeks, 3 months: 13 weeks). GroupsError for what is not one."""
    if texts is None:
        return []
    if isinstance(texts, str):
        texts = [t for t in re.split(r"[,;]", texts)]
    out: list[Reference] = []
    for raw in texts:
        text = " ".join(str(raw or "").split())
        if not text or text.lower() in ("none", "no", "usual", "the usual"):
            continue
        m = REFERENCE.match(text)
        if not m:
            raise GroupsError(f'against: not a reference day: {text!r} (write "yesterday", "2 days ago", "1 week ago", '
                              '"3 weeks ago", "2 months ago", "1 year ago" or a date "2026-06-15")')
        if m.group(4):
            try:
                out.append(Reference(text, "date", date=dt.date.fromisoformat(m.group(4))))
            except ValueError as ex:
                raise GroupsError(f"against: not a date: {text!r}") from ex
            continue
        if m.group(1):
            n, unit = WORDS[m.group(1).lower()]
        else:
            n = 1 if not m.group(2).isdigit() else int(m.group(2))
            unit = {"d": "day", "j": "day", "w": "week", "s": "week", "m": "month", "y": "year", "a": "year"}[m.group(3)[0].lower()]
        if n < 1:
            raise GroupsError(f"against: {text!r} is not before the window")
        days = n if unit == "day" else 7 * n if unit == "week" else 7 * round(n * 30.44 / 7) if unit == "month" else 364 * n
        if days > REFERENCE_DAYS:
            raise GroupsError(f"against: {text!r} is more than {REFERENCE_DAYS} days back")
        out.append(Reference(text, "day" if unit == "day" else "week", days))
    seen: set[tuple] = set()
    unique = [r for r in out if (r.days, r.date) not in seen and not seen.add((r.days, r.date))]
    if len(unique) > REFERENCES:
        raise GroupsError(f"against: {REFERENCES} reference days at most")
    return unique


def clock(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(value).strip().replace("T", " ")).replace(tzinfo=None)
    except ValueError as ex:
        raise GroupsError(f'not a time: {value!r} (write "2026-09-24 02:00")') from ex


def lit(t: dt.datetime) -> str:
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}'"


def scope(where: str, label_column: str | None) -> tuple[Any, list[str]]:
    """The scope's condition as a tree (None: no condition) and the business-date labels it filters on (they
    are relative to the day of the question: each earlier day gets its own dates)."""
    import sqlglot
    from sqlglot import exp

    text = (where or "").strip().rstrip(";").strip()
    text = re.sub(r"^\s*where\s+", "", text, flags=re.I)
    if not text:
        return None, []
    try:
        trees = [t for t in sqlglot.parse(f"SELECT 1 FROM t WHERE {text}", read="duckdb") if t is not None]
    except Exception as ex:  # pylint: disable=broad-except
        raise GroupsError(f"where: not a condition ({str(ex)[:200]})") from ex
    if len(trees) != 1:
        raise GroupsError("where: one condition, not several statements")
    tree = trees[0]
    cond = tree.args.get("where")
    if not isinstance(tree, exp.Select) or cond is None or len(list(tree.find_all(exp.Select))) > 1 or \
            tree.args.get("joins") or tree.args.get("group") or tree.args.get("order") or tree.args.get("limit"):
        raise GroupsError("where: only conditions on this table's own fields (no subquery, GROUP BY, ORDER BY or LIMIT)")
    inner = regrouped(cond.this)
    labels = literals(inner, label_column)
    if labels and isinstance(_bare(inner), exp.Or):
        raise GroupsError("where: an OR at the top leaves some of its alternatives without the business date: write "
                          "the alternatives of one field as \"FIELD\" IN ('a', 'b'), and join the conditions with AND")
    return inner, labels


def _bare(node: Any) -> Any:
    from sqlglot import exp

    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _terms(node: Any, kind: Any) -> list[Any]:
    """The operands of a chain of ANDs (or of ORs), in the order written."""
    node = _bare(node) if not isinstance(node, kind) else node
    if isinstance(node, kind):
        return _terms(node.this, kind) + _terms(node.expression, kind)
    return [node]


def _field_of(node: Any) -> str | None:
    """The field a plain condition is on ("F" = 'x', "F" IN (...), "F" LIKE '...'), else None."""
    from sqlglot import exp

    node = _bare(node)
    if isinstance(node, (exp.EQ, exp.In, exp.Like, exp.ILike)) and isinstance(node.this, exp.Column):
        return node.this.name
    return None


def one_left_out(cond: Any, skip: set[str]) -> list[tuple[str, str]]:
    """The scope with one of its conditions left out, for each condition joined with AND that is on a field
    (not on `skip`: the business date, the time fields): [(the condition left out, the scope without it)]."""
    from sqlglot import exp

    terms = _terms(cond, exp.And) if cond is not None else []
    out: list[tuple[str, str]] = []
    for i, term in enumerate(terms):
        fields = {c.name for c in term.find_all(exp.Column)}
        if not fields or fields & skip or len(terms) < 2:
            continue
        rest = [t for j, t in enumerate(terms) if j != i]
        tree: Any = rest[0].copy()
        for t in rest[1:]:
            tree = exp.And(this=tree, expression=t.copy())
        out.append((term.sql(dialect="duckdb", identify=True), tree.sql(dialect="duckdb", identify=True)))
    return out


def regrouped(cond: Any) -> Any:
    """`"A" = 'x' OR "A" = 'y' AND "ENV" = 'P' AND "DAY" = 'D-1'`, as people and models write it: in SQL the AND
    binds first, so the conditions after it hold for the last alternative only, and 'x' is read over every
    environment and every day. Read as it was meant: ("A" = 'x' OR "A" = 'y') AND "ENV" = 'P' AND "DAY" = 'D-1'.
    Only when the alternatives are plain conditions on one same field and exactly one of them carries the other
    conditions (after it, or before it); anything else is left as written."""
    from sqlglot import exp

    top = _bare(cond)
    if not isinstance(top, exp.Or) or isinstance(cond, exp.Paren):
        return cond
    alts = _terms(top, exp.Or)
    chains = [(i, _terms(a, exp.And)) for i, a in enumerate(alts) if isinstance(_bare(a), exp.And) and not isinstance(a, exp.Paren)]
    plain = [a for a in alts if _field_of(a)]
    if len(chains) != 1 or len(plain) != len(alts) - 1 or not plain:
        return cond
    field = _field_of(plain[0])
    if any(_field_of(a) != field for a in plain):
        return cond
    at, chain = chains[0]
    if at == len(alts) - 1 and _field_of(chain[0]) == field:          # x OR y AND rest
        own, rest = chain[0], chain[1:]
    elif at == 0 and _field_of(chain[-1]) == field:                   # rest AND x OR y
        own, rest = chain[-1], chain[:-1]
    else:
        return cond
    together = [a for i, a in enumerate(alts) if i != at]
    together.insert(at if at == 0 else len(together), own)
    group: Any = together[0].copy()
    for a in together[1:]:
        group = exp.Or(this=group, expression=a.copy())
    out: Any = exp.Paren(this=group)
    for r in rest:
        out = exp.And(this=out, expression=r.copy())
    return out


def literals(cond: Any, column: str | None) -> list[str]:
    """The values a condition compares a column with (= 'x', IN ('x', 'y')), in its order."""
    from sqlglot import exp

    out: list[str] = []
    if cond is None or not column:
        return out
    for node in cond.find_all(exp.EQ, exp.In):
        if isinstance(node.this, exp.Column) and node.this.name == column:
            values = [node.expression] if isinstance(node, exp.EQ) else list(node.expressions)
            out += [str(v.this) for v in values if isinstance(v, exp.Literal)]
    return list(dict.fromkeys(out))


def render(cond: Any, column: str | None = None, replacement: str | None = None) -> str:
    """The condition as SQL; with `replacement`, every condition on `column` (the business-date label, or the
    date it is read from) becomes it: the earlier day's own dates, or TRUE to leave the business date out."""
    import sqlglot
    from sqlglot import exp

    if cond is None:
        return ""
    tree = cond.copy()
    if column and replacement is not None:
        new = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {replacement}", read="duckdb").args["where"].this

        def swap(node: Any) -> Any:
            if isinstance(node, (exp.EQ, exp.In)) and isinstance(node.this, exp.Column) and node.this.name == column:
                return exp.Paren(this=new.copy())
            return node

        tree = tree.transform(swap)
    return tree.sql(dialect="duckdb", identify=True)     # (field names quoted: as the tool's own queries write them)


DAY = re.compile(r"^\d{4}-\d{2}-\d{2}(?=$|[ T]\d{1,2}:\d{2})")     # a date, alone or before a time


def _time_compared(node: Any, columns: set[str] | None) -> tuple[str, list[Any]] | None:
    """A condition that compares a time field with values: (the field, the values it is compared with). The
    field itself, cast or cut to its day: `"ts" >= x`, `x <= "ts"`, `CAST("ts" AS DATE) = x`, BETWEEN, IN. Not a
    part of the time (the hour of "ts": it holds on every day), not a condition between two fields.
    columns None: any field."""
    from sqlglot import exp

    def field(side: Any) -> str | None:
        while isinstance(side, (exp.Paren, exp.Cast, exp.TryCast, exp.Date, exp.TsOrDsToDate, exp.DateTrunc,
                                exp.TimestampTrunc)):
            side = side.this
        return side.name if isinstance(side, exp.Column) and (columns is None or side.name in columns) else None

    def values(sides: list[Any]) -> bool:
        return all(x is not None and x.find(exp.Column) is None for x in sides)

    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        for one, other in ((node.this, node.expression), (node.expression, node.this)):
            col = field(one)
            if col and values([other]):
                return col, [other]
        return None
    if isinstance(node, exp.Between):
        col, others = field(node.this), [node.args.get("low"), node.args.get("high")]
    elif isinstance(node, exp.In) and node.expressions:
        col, others = field(node.this), list(node.expressions)
    else:
        return None
    return (col, others) if col and values(others) else None


def _dates(values: list[Any]) -> list[Any]:
    """The dates written in the values a time field is compared with ('2026-08-06 00:00', DATE '2026-08-06')."""
    from sqlglot import exp

    return [x for v in values for x in v.find_all(exp.Literal) if x.is_string and DAY.match(str(x.this).strip())]


COMPARISONS = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Between", "In")
NOW_WORDS = ("NOW", "TODAY", "GETDATE", "SYSDATE", "SYSDATETIME", "LOCALTIMESTAMP", "UTC_TIMESTAMP", "CURDATE")


def time_columns(cond: Any, known: set[str]) -> set[str]:
    """The scope's time fields: the ones known (the dictionary), and any field the scope compares with a value
    that is an instant by its own writing (TIMESTAMP '...', DATE '...', now, today, an interval)."""
    from sqlglot import exp

    out = set(known)
    if cond is None:
        return out

    def instant(value: Any) -> bool:
        if value.find(exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.CurrentDatetime, exp.Interval):
            return True
        if any(str(a.this).upper() in NOW_WORDS for a in value.find_all(exp.Anonymous)):
            return True
        return any(c.to is not None and c.to.is_type(*exp.DataType.TEMPORAL_TYPES) for c in value.find_all(exp.Cast, exp.TryCast))

    for node in cond.find_all(*[getattr(exp, k) for k in COMPARISONS]):
        got = _time_compared(node, None)
        if got and any(instant(v) for v in got[1]):
            out.add(got[0])
    return out


def timed(cond: Any, columns: set[str]) -> tuple[Any, list[str], list[str]]:
    """The scope's conditions that compare a time field with an instant: (the scope, the fields of the ones that
    move, the fields of the ones left out). One written with a date moves with each earlier day (`moved_back`:
    the rows since 01:00 that day are, a day earlier, the rows since 01:00 the day before); as it stands it
    would leave every earlier day empty. One written without a date (since now minus six hours) cannot move: it
    is left out, the window gives the time."""
    from sqlglot import exp

    if cond is None or not columns:
        return cond, [], []
    moving: list[str] = []
    removed: list[str] = []

    def visit(node: Any) -> Any:
        got = _time_compared(node, columns)
        if got is None:
            return node
        if all(_dates([v]) for v in got[1]):
            moving.append(got[0])
            return node
        removed.append(got[0])
        return exp.true()

    tree = exp.Paren(this=cond.copy()).transform(visit)
    return (tree.this if isinstance(tree, exp.Paren) else tree), list(dict.fromkeys(moving)), list(dict.fromkeys(removed))


def moved_back(cond: Any, columns: set[str], days: int) -> Any:
    """The scope as it was `days` days earlier: the dates its conditions compare the time fields with, that many
    days back (their time of day as it is)."""
    from sqlglot import exp

    if cond is None or not columns or not days:
        return cond
    tree = exp.Paren(this=cond.copy())
    for node in list(tree.find_all(*[getattr(exp, k) for k in COMPARISONS])):
        got = _time_compared(node, columns)
        for literal in _dates(got[1]) if got else []:
            text = str(literal.this).strip()
            try:
                day = dt.date.fromisoformat(text[:10]) - dt.timedelta(days=days)
            except ValueError:
                continue
            literal.set("this", day.isoformat() + text[10:])
    return tree.this


def conditioned(cond: Any, skip: set[str]) -> list[str]:
    """The fields the scope puts a condition on ("ENV" = 'PROD', "APP" IN (...), <> , LIKE), in its order."""
    from sqlglot import exp

    out: list[str] = []
    if cond is None:
        return out
    for node in cond.find_all(exp.EQ, exp.NEQ, exp.In, exp.Like, exp.ILike, bfs=False):
        for side in (node.this, node.args.get("expression")):
            if isinstance(side, exp.Column) and side.name not in skip and side.name not in out:
                out.append(side.name)
    return out


def others(field: str, values: list[str], fields: list[str], now: dict[tuple, int], days: list[dict[tuple, int]]) -> dict[str, Any]:
    """Who else is on what stands out: the rows outside the scope that share the value(s) the change is
    concentrated on (the other rows on that server, that queue, that pool), over the same window of the same
    days. `now` / `days`: their rows by the values of the scope's own `fields` ({(a value of each field): rows}).
    More of them than usual or not, and under which values: {"on", "rows", "usual", "more", "line"}."""
    on = f'"{field}" = {values[0]}' if len(values) == 1 else f'"{field}" in ({", ".join(values)})'
    total = sum(now.values())
    totals = [sum(d.values()) for d in days]
    usual = statistics.median(totals) if totals else 0
    seen = overall((total, float(total)), [(n, float(n)) for n in totals])
    out: dict[str, Any] = {"on": on, "rows": total, "usual": usual, "more": False}
    if not total and not usual:
        out["line"] = f"on {on}: no row outside the scope during the window, as on the earlier days"
        return out
    if seen.get("verdict") != "above usual":
        much = "as usual" if seen.get("verdict") in (None, "as usual") else seen["verdict"]
        out["line"] = (f"on {on}, the rows outside the scope during the window: {total:,} against {usual:,g} usually "
                       f"({much}): nothing else has more rows there than on the earlier days")
        return out

    def one(g: dict[str, Any]) -> str:
        if g.get("new") or not g.get("usual"):
            return f'{g["value"]} ({g["now"]:,g} rows, none on the earlier days)'
        return f'{g["value"]} ({g["now"]:,g} rows against {g["usual"]:,g} usually)'

    who: list[tuple[int, int, float, str]] = []      # (the fewest values first, the never seen before the grown)
    for i, f in enumerate(fields):
        def by(d: dict[tuple, int], i: int = i) -> dict[str, tuple[int, float]]:
            got: dict[str, int] = {}
            for key, n in d.items():
                got[key[i]] = got.get(key[i], 0) + n
            return {k: (n, float(n)) for k, n in got.items()}

        s = summarize(by(now), [by(d) for d in days], True, change=float(total - usual))
        up = [g for g in s["groups"] if g.get("unusual") and (g.get("appeared") or (g.get("ratio") or 0) > 1)]
        if not s.get("concentrated"):                     # every value above alike says nothing: only the new ones
            up = [g for g in up if g.get("new") or not g.get("usual")]
        if up and (s.get("coverage") or 0) >= 0.5:        # the values that hold most of what is there beyond usual
            seen_before = int(any(g.get("usual") and not g.get("new") for g in up))
            who.append((len(up), seen_before, -(s.get("coverage") or 0), f'"{f}" = ' + ", ".join(one(g) for g in up[:3])
                        + (f" and {len(up) - 3} more" if len(up) > 3 else "")))
    who.sort()
    out["more"] = True
    out["line"] = (f"on {on}, the rows outside the scope during the window: {total:,} against {usual:,g} usually "
                   "(above usual)" + (". Who they are: " + "; ".join(w[3] for w in who[:3]) if who else ""))
    return out


def fixed_fields(cond: Any) -> set[str]:
    """Fields the scope pins to one value ("ENV" = 'P' among its conditions joined with AND; not one of two
    alternatives): grouping by them says nothing."""
    from sqlglot import exp

    out: set[str] = set()
    if cond is None:
        return out
    for term in _terms(cond, exp.And):
        node = _bare(term)
        if isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
            out.add(node.this.name)
        elif isinstance(node, exp.In) and isinstance(node.this, exp.Column) and len(node.expressions) == 1:
            out.add(node.this.name)
    return out


def _mad(values: list[float]) -> float:
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def summarize(now: dict[str, tuple[int, float | None]], days: list[dict[str, tuple[int, float | None]]],
              counting: bool, scale: float | None = None, min_rows: int = MIN_ROWS, change: float | None = None,
              high: float = HIGH, above_only: bool = False, renamed: dict[str, str] | None = None,
              open_field: bool = False, sizes: dict[str, float] | None = None) -> dict[str, Any]:
    """One field: each value now against its usual (the median of the earlier days), the values that stand
    out first, and what the field says: concentrated on a few values, every value off alike, or nothing.

    A count stands out only beyond what counts differ by from one day to the next (13 against 8 does not), an
    average only over enough rows. A field that many rows do not have yet (the server of a run that has not
    started) does not say where a count changed, and rows that moved to values that are new (as many rows in
    all) are not lost.
    scale: the measure's usual over all the rows; a value differs when it is off by a fifth of it at least (10 s
        instead of 60 is x0.17 and nothing, where the usual of all is an hour).
    change: the measure's change in total (rows x (now - usual), or now - usual of a count); how much of it the
        values that stand out hold is their coverage. A field is the better place to look the more of the change
        its values hold and the more of their rows are off (the region whose every run is late, rather than the
        application of which a third is).
    high: how far above its usual a value stands out (and 1 / high below); above_only: a value below its usual
        is not said (a wait shorter than usual is no finding).
    renamed {new: old}: the values that took the place of others (a new version: `replaced`), already read
        against the history of the one they replace: said so.
    open_field: many rows have no value of this field yet (the server of the runs not started), and the measure
        is one every row has: the rows that have the field today are not the rows of the earlier days.
    sizes {value: its rows}: values that stand out and hold nearly all the rows (the one kind of run there is)
        say nothing of where the change is: not a place to look."""
    if open_field:
        return {"reading": "not comparable yet: many rows have no value of this field so far", "concentrated": False,
                "values": len(now), "groups": [], "strength": 1.0, "flagged": 0, "coverage": None, "purity": 0.0,
                "counting": counting}
    low_at = 0.0 if above_only else (LOW if high == HIGH else round(1 / high, 2))
    renamed = renamed or {}
    total = sum(n for n, _v in now.values()) or 1
    usual_total = statistics.median([sum(n for n, _v in d.values()) for d in days]) if days else 0
    floor = max(3.0, 0.02 * max(total, usual_total))
    if counting:
        empty = now.get(NONE, (0, None))[0]
        empty_usual = statistics.median([d.get(NONE, (0, None))[0] for d in days]) if days else 0
        if empty >= floor and empty > 3 * max(empty_usual, 1):
            return {"reading": f"not comparable yet: {empty} rows have no value of this field so far "
                               f"({empty_usual:g} usually)", "concentrated": False, "values": len(now), "groups": [],
                    "strength": 1.0, "flagged": 0, "coverage": None, "purity": 0.0, "counting": True}
    rows: list[dict[str, Any]] = []
    for key in set(now) | {k for d in days for k in d}:
        n, v = now.get(key, (0, 0.0 if counting else None))
        hist = [d[key][1] for d in days if key in d and d[key][1] is not None]
        if counting:
            hist = [float(d.get(key, (0, 0.0))[0]) for d in days]
        ns = [d.get(key, (0, None))[0] for d in days]
        enough = len(hist) >= (1 if len(days) == 1 else max(2, (len(days) + 1) // 2))     # (one reference day: itself)
        usual = statistics.median(hist) if hist and enough else None
        usual_n = statistics.median(ns) if ns else 0
        row: dict[str, Any] = {"value": key, "rows_now": n, "now": None if v is None else round(v, 4),
                               "usual": None if usual is None else round(usual, 4), "rows_usual": usual_n}
        if key in renamed:
            row["in_place_of"] = renamed[key]
        noise = _mad(hist) if len(hist) >= 3 else 0.0
        if v is not None and usual:
            row["ratio"] = round(v / usual, 2)
            off = row["ratio"] >= high or row["ratio"] <= low_at
            if counting:
                # counts differ from one day to the next by about their square root (13 against 8 is nothing);
                # a count that is the same every day (five orders a day, each day) has moved when it moves
                steady = len(hist) >= STEADY_DAYS and max(hist) - min(hist) <= max(1.0, STEADY * usual)
                enough = abs(v - usual) >= APPEARED and (abs(v - usual) > COUNT_NOISE * math.sqrt(max(usual, 1.0)) or steady)
            else:
                enough = n >= (max(min_rows, REPLACED_ROWS) if key in renamed else min_rows) and \
                    (not scale or abs(v - usual) >= SCALE * abs(scale))
            row["unusual"] = bool(off and enough and abs(v - usual) > NOISE * noise)
        elif counting and v and not usual and any(key in d for d in days):
            # a value the earlier days had, with nothing of this measure then (no run waiting at that hour)
            row["appeared"] = row["unusual"] = bool(v >= max(floor, APPEARED) and v > NOISE * noise)
        elif v is not None and n >= floor and (usual is None or (counting and not usual)):
            row["note"] = "no usual: not seen on the earlier days"
            if counting and v >= max(floor, APPEARED) and key != NONE:
                # rows under a value the earlier days never had (another environment on that pool): they stand
                # out when there are more rows in all (below: as many rows in all are rows that moved)
                row["appeared"] = row["unusual"] = row["new"] = True
        row["sizeable"] = bool(n >= floor or usual_n >= floor)
        rows.append(row)
    if counting and not total > 1.25 * usual_total:
        for r in rows:                  # a value never seen stands out only when there are more rows in all
            if r.get("new"):
                r["unusual"] = r["appeared"] = False
    came = [r for r in rows if (r.get("note") or r.get("appeared")) and r["value"] != NONE]
    if counting and came and usual_total and abs(total - usual_total) <= 0.25 * usual_total:
        for r in rows:                  # as many rows in all, some under values that are new: the rows moved to
            if r.get("appeared") or (r.get("ratio") is not None and r["ratio"] < 1):     # them, none is lost
                r["unusual"] = r["appeared"] = False
    for r in rows:
        if r["value"] == NONE:          # the rows without a value of the field are no place to look
            r["unusual"] = r["appeared"] = False
    sized = [r for r in rows if r["sizeable"] and (r.get("ratio") is not None or r.get("appeared"))]
    above = sorted((r for r in sized if r.get("unusual") and (r.get("appeared") or r["ratio"] >= high)),
                   key=lambda r: -((r["ratio"] - 1) if r.get("ratio") is not None else STRONG) * max(r["rows_now"], 1))
    below = sorted((r for r in sized if r.get("unusual") and not r.get("appeared") and r["ratio"] <= low_at),
                   key=lambda r: (r["ratio"] - 1) * max(r["rows_usual"], 1))
    flat = [r for r in sized if r not in above and r not in below]
    if counting and sizes:              # a value with rows of its own and as few of this as usually (one waiting
        enough = max(3.0, 0.02 * sum(sizes.values()))       # there, as every day) is as usual, however few
        flat += [r for r in rows if r not in flat and r not in above and r not in below and not r.get("note")
                 and r["value"] != NONE and sizes.get(r["value"], 0.0) >= enough]
    if counting and not flat:           # the values that have none of it now, as usually (nobody waiting there)
        flat = [r for r in rows if r not in above and r not in below and not r.get("note") and r["value"] != NONE
                and not r.get("now") and not r.get("usual")]
    new = [r for r in rows if r.get("note") and r["sizeable"] and not r.get("appeared")]

    def names(rs: list[dict[str, Any]], k: int = 5) -> str:
        def one(r: dict[str, Any]) -> str:
            was = f'new, in place of {r["in_place_of"]}: ' if r.get("in_place_of") else ""
            if counting and r.get("new"):
                return f'{r["value"]} (new: {r["now"]:g}, not seen on the earlier days)'
            if counting:
                return f'{r["value"]} ({was}{r["now"]:g} against {r["usual"]:g} usually)'
            return f'{r["value"]} ({was}x{r["ratio"]:g} its usual)'

        return ", ".join(one(r) for r in rs[:k]) + (f" and {len(rs) - k} more" if len(rs) > k else "")

    if not sized:
        reading, concentrated = "no value with enough rows to compare", False
    elif not above and not below:
        reading, concentrated = "no value stands out (each within its usual)", False
    else:
        parts = []
        concentrated = False
        whole = sum((sizes or {}).values())
        for label, rs in (("above usual", above), ("below usual", below)):
            if not rs:
                continue
            # the values that are not off this way: as usual, or off the other way (the rows of a campaign that
            # are new, next to the usual rows of which half have not started)
            rest = flat + [r for r in (below if rs is above else above)]
            held = sum((sizes or {}).get(r["value"], 0.0) for r in rs) / whole if whole else 0.0
            spared = sum((sizes or {}).get(r["value"], 0.0) for r in rest) / whole if whole else 0.0
            few = len(rs) <= max(1, (len(rs) + len(rest)) // 2)
            # four servers of seven three times slower, the three others as usual: a clean split is a place to look
            clean = len(rs) <= CLEAN_VALUES and len(rs) <= 2 * len(rest) and spared >= SPARED
            others = (f"while the {len(rest)} other value(s) are as usual" if len(rest) == len(flat)
                      else f"while the {len(rest)} other value(s) are not")
            if (few or clean) and rest and held >= DOMINANT:
                parts.append(f"{names(rs)} {label}, with {held:.0%} of the rows: not specific to this field")
            elif (few or clean) and rest:
                concentrated = True
                parts.append(f"concentrated on {names(rs)} {label}, {others}")
            else:
                ratios = [r["ratio"] for r in sized if r.get("ratio") is not None]
                med = statistics.median(ratios) if ratios else None
                parts.append(f"{len(rs)} of {len(sized)} values {label}" + (f" (median x{med:g})" if med is not None else "")
                             + ": not specific to this field")
        reading = "; ".join(parts)
    if new:
        reading += "; not seen on the earlier days: " + ", ".join(str(r["value"]) for r in new[:6])

    def deviation(r: dict[str, Any]) -> float:
        """How far a value is from its usual, in the measure's own unit times its rows (signed)."""
        if counting:
            return (r.get("now") or 0.0) - (r.get("usual") or 0.0)      # (a new value: all its rows)
        return r["rows_now"] * (r["now"] - r["usual"]) if r.get("usual") is not None and r.get("now") is not None else 0.0

    off_values = above + below
    held = sum(abs(deviation(r)) for r in off_values)
    if counting:
        purity = held / max(sum(max(r.get("now") or 0.0, r.get("usual") or 0.0) for r in off_values), 1.0)
    else:
        per_row = held / max(sum(r["rows_now"] for r in off_values), 1)
        purity = per_row / abs(scale) if scale else per_row
    coverage = None
    if change:                          # the share of the change in total that these values hold (the same way)
        same_way = sum(deviation(r) for r in off_values if deviation(r) * change > 0)
        coverage = round(min(1.0, abs(same_way) / abs(change)), 2)
    shown = (above[:SHOWN] + below[:3] + new[:3] + sorted(flat, key=lambda r: -r["rows_now"])[:3])[:SHOWN + 4]
    for r in rows:
        r.pop("sizeable", None)
    strength = max([r["ratio"] if r.get("ratio") is not None else STRONG for r in above]
                   + [1 / r["ratio"] if r["ratio"] else STRONG for r in below] + [1.0])
    return {"reading": reading, "concentrated": concentrated, "values": len(rows), "groups": shown,
            "strength": min(strength, STRONG), "flagged": len(off_values), "coverage": coverage,
            "purity": round(purity, 4), "counting": counting}


def unfilled(now: dict[str, int], days: list[dict[str, int]]) -> bool:
    """Many rows have no value of the field yet, more than usually (the server of the runs not started)."""
    total = sum(now.values()) or 1
    empty = now.get(NONE, 0)
    usual = statistics.median([d.get(NONE, 0) for d in days]) if days else 0
    return empty >= max(3.0, 0.02 * total) and empty > 3 * max(usual, 1)


def replaced(now: dict[str, int], days: list[dict[str, int]]) -> dict[str, str]:
    """The values that took the place of others, from the rows of each value now and on the earlier days: {new:
    old}. A value most of the earlier days did not have, with about as many rows as a value most of them had and
    that has none any more (a new version of an application; fewer rows when some have no value yet, the runs
    not started). The new value is then read against the history of the old one."""
    if not days:
        return {}

    def usually(k: str) -> float:
        return statistics.median([d.get(k, 0) for d in days])

    before = set().union(*[set(d) for d in days])
    waiting = max(0, now.get(NONE, 0) - usually(NONE))                # rows with no value yet
    came = {k: n for k, n in now.items() if k != NONE and n >= 3 and usually(k) == 0}
    gone = {k: usually(k) for k in before if not now.get(k) and k != NONE and usually(k) >= 3}
    if len(came) == 1 and len(gone) == 1:         # one came, one went: the same rows under a new name, however many
        return {next(iter(came)): next(iter(gone))}
    pairs: dict[str, str] = {}
    for new, n in sorted(came.items(), key=lambda kv: -kv[1]):
        fits = [(abs(u - n), old) for old, u in gone.items() if old not in pairs.values()
                and n <= 1.25 * u and n + waiting >= 0.75 * u]
        if fits:
            pairs[new] = min(fits)[1]
    return pairs


def overall(now: tuple[int, float | None], days: list[tuple[int, float | None]],
            rows: tuple[int, list[int]] | None = None) -> dict[str, Any]:
    """A measure in total against its usual. `rows` (the rows of the window now and on each earlier day): an
    average that fewer of the rows have now than usually (the runs still in progress have no duration yet) is
    over other rows than usual, and is said so instead of compared."""
    hist = [v for _n, v in days if v is not None]
    usual = statistics.median(hist) if hist else None
    out: dict[str, Any] = {"rows_now": now[0], "now": None if now[1] is None else round(now[1], 4),
                           "usual": None if usual is None else round(usual, 4),
                           "earlier_days": [None if v is None else round(v, 4) for _n, v in days],
                           "rows_usual": statistics.median([n for n, _v in days]) if days else None}
    if now[1] is not None and usual:
        ratio = now[1] / usual
        noise = _mad(hist) if len(hist) >= 3 else 0.0
        out["ratio"] = round(ratio, 2)
        out["verdict"] = "as usual"
        if (ratio >= HIGH or ratio <= LOW) and abs(now[1] - usual) > NOISE * noise:
            out["verdict"] = "above usual" if ratio > 1 else "below usual"
        elif not math.isclose(ratio, 1.0, abs_tol=0.1):
            out["verdict"] = "slightly " + ("above" if ratio > 1 else "below") + " usual"
    elif now[1] and usual == 0 and now[1] >= APPEARED:      # (a count) nothing usually, some now
        out["verdict"] = "above usual"
    if rows and rows[0] and now[0] is not None:
        shares = [n / r for (n, _v), r in zip(days, rows[1]) if r]
        share_usual = statistics.median(shares) if shares else None
        share_now = now[0] / rows[0]
        if share_usual and share_now < INCOMPLETE * share_usual:
            out["verdict"] = "not comparable yet"
            out["why"] = (f"{share_now:.0%} of the rows have it now, {share_usual:.0%} usually (the others are still "
                          "in progress): its average is over other rows than usual")
    return out


CLASSES = (1.5, 2, 4, 8, 16)   # how far off, by classes


def far(s: dict[str, Any]) -> int:
    """How far the values that stand out are from their usual, by classes (x1.5, x2, x4, x8, x16)."""
    return sum(1 for x in CLASSES if (s.get("strength") or 1.0) >= x)


def rank(s: dict[str, Any]) -> tuple:
    """How well a field localizes a change: its values that stand out hold most of the change in total, then
    their rows are the most off (the region whose every run is late, rather than the application of which a
    third is), then they are the fewest (a region rather than its ten servers), then the farthest."""
    cover = s.get("coverage")
    purity = s.get("purity") or 0.0
    if s.get("counting"):
        pure = round(min(purity, 1.0) * 4)
    else:
        pure = round(math.log(purity, 1.5)) if purity > 0 else -99
    return (1 if cover is None else 2 if cover >= 0.6 else 1 if cover >= 0.25 else 0, pure, -(s.get("flagged") or 0),
            s.get("strength") or 1.0)


def localized(fields: dict[str, dict[str, Any]]) -> list[str]:
    """The fields that localize the change, the best first."""
    return sorted((f for f, s in fields.items() if s.get("concentrated")), key=lambda f: rank(fields[f]), reverse=True)


def change(overall: dict[str, Any], counting: bool) -> float | None:
    """A measure's change in total, for the coverage of the values that stand out: rows x (now - usual), or
    now - usual of a count; None when the total has not moved enough to speak of a share of it."""
    now, usual = overall.get("now"), overall.get("usual")
    if now is None or usual is None:
        return None
    if counting:
        return now - usual if abs(now - usual) >= max(APPEARED, 0.02 * usual) else None
    if abs(now - usual) < 0.05 * abs(usual):
        return None
    return (overall.get("rows_now") or 0) * (now - usual) or None


def conclusion(fields: dict[str, dict[str, Any]]) -> str:
    """Which field localizes the change, in a line."""
    found = localized(fields)
    if found:
        best = found[0]
        also = [f'"{f}" ({fields[f]["reading"]})' for f in found[1:3]]
        others = [f for f, s in fields.items() if not s.get("concentrated")]
        return (f'The change is concentrated on "{best}": {fields[best]["reading"]}.'
                + (f' Also seen on {"; ".join(also)}.' if also else "")
                + (f' Not specific to: {", ".join(others)}.' if others else ""))
    if any("not specific" in s["reading"] for s in fields.values()):
        return ("No field localizes the change: its values are off alike. The cause is likely upstream or shared "
                "(what all these rows depend on), or another field.")
    return "Nothing stands out on these fields."


def brief(summary: dict[str, Any], asked: bool = True) -> dict[str, Any]:
    """A field's summary as the tool gives it: its reading, and its values when it was asked or stands out (a
    field compared by the way and where nothing stands out is one line)."""
    out = {"reading": summary["reading"], "values": summary["values"]}
    if asked or summary.get("flagged") or "not seen on the earlier days" in summary["reading"] or \
            "not specific" in summary["reading"]:
        out["groups"] = summary["groups"] if asked or summary.get("concentrated") else summary["groups"][:4]
    return out


def _off(overall: dict[str, Any]) -> bool:
    return str(overall.get("verdict") or "") in ("above usual", "below usual")


def _weight(entry: tuple) -> tuple:
    """How much a measure has to say: a field localizes it (how well), else its total is off (how far). A
    measure as usual in total whose values are only a little off on some field (x1.3 on one server of thirty)
    says nothing."""
    _m, overall, summaries = entry
    if overall.get("verdict") == "not comparable yet":
        return (0, 0, 0, 0.0, 0.0, 0.0)
    best = localized(summaries)
    ratio = overall.get("ratio") or 1.0
    far = max(ratio, 1 / ratio) if ratio else STRONG
    if best:
        many = (summaries[best[0]].get("values") or 0) > FEW       # the more values, the more one is off by chance
        verdict = str(overall.get("verdict") or "")
        loose = _m.kind == "age"            # (the wait of the few rows at a stage: only a clear change counts)
        moved = verdict not in ("", "as usual") and not (loose and verdict.startswith("slightly"))
        if globals()["far"](summaries[best[0]]) >= (2 if many or loose else 1) or moved:
            return (2,) + rank(summaries[best[0]]) + (far,)
    if _off(overall):                   # off in total, on every value alike
        return (1, 0, 0, 0.0, 0.0, far)
    return (0, 0, 0, 0.0, 0.0, far)


def moved(entry: tuple) -> bool:
    """A measure says something: its total is off, or a field localizes a change of it."""
    return _weight(entry)[0] > 0


def ordered(judged: list[tuple]) -> list[tuple]:
    """The measures that say something, the one that says the most first."""
    return sorted((e for e in judged if _weight(e)[0] > 0), key=_weight, reverse=True)


def _figures(g: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in g.items() if k in ("value", "rows_now", "now", "usual", "ratio", "in_place_of")
            and not (k == "rows_now" and g.get("now") == g.get("rows_now"))}


def _much(o: dict[str, Any], unit: str = "") -> str:
    """A total against its usual, in a few words."""
    def n(v: Any) -> str:
        return f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 100 else f"{v:g}"

    text = f'{n(o.get("now"))}{unit} against {n(o.get("usual"))}{unit} usually'
    if o.get("verdict") and o.get("verdict") != "as usual":
        text += f' ({o["verdict"]})'
    return text


def stage_lines(judged: list[tuple], stages: list[str], what: dict[str, str], every_row: list[str]) -> tuple[list[str], int | None, dict[int, str]]:
    """The stages of the rows in their order (the table's time fields): how many rows had reached each by the
    end of the window, how long after the stage before, how many were waiting for it and since when, each against
    usual; the first stage that is off (None: none); and the stages that are off on their own {stage: how}: the
    rows take longer than usual to get there from the stage before, or wait there where they do not usually
    (fewer rows at a stage is the wake of the stages before; a longer way to it is not)."""
    by = {(m.kind, tuple(m.fields)): e for e in judged for m in [e[0]]}
    rows = max([e[1].get("now") or 0 for e in judged if e[0].kind == "count"] + [1])
    journey = sum(e[1].get("usual") or 0.0 for e in judged if e[0].kind == "lapse")     # the usual time of the whole way
    lines, first, own = [], None, {}
    for k, f in enumerate(stages):
        parts, off = [], False
        got = by.get(("reached", (f,)))
        if f in every_row:
            parts.append("every row had reached it, as usual")
        elif got and got[1].get("now") is not None:
            parts.append(f"rows that had reached it: {_much(got[1])}")
            off = off or got[1].get("verdict") == "below usual"
        if k:
            pair = (stages[k - 1], f)
            lapse, between, age = by.get(("lapse", pair)), by.get(("between", pair)), by.get(("age", pair))
            if lapse and lapse[1].get("now") is not None and lapse[1].get("usual") is not None:
                parts.append(f"on average {_much(lapse[1], ' s')} after {stages[k - 1]}")
                added = lapse[1]["now"] - lapse[1]["usual"]
                if lapse[1].get("verdict") == "above usual" and (added >= JOURNEY * journey or (
                        lapse[1]["usual"] and lapse[1]["now"] >= LONGER * lapse[1]["usual"] and added >= STEP * journey)):
                    off = True
                    own[k] = f"{lapse[1]['now']:,.0f} s against {lapse[1]['usual']:,.0f} s usually after {stages[k - 1]}"
            if between and between[1].get("now"):
                text = f"rows past {stages[k - 1]} and not yet there: {_much(between[1])}"
                if age and age[1].get("now") is not None:
                    text += f", since {age[1]['now']:,.0f} s on average" + (
                        f" against {age[1]['usual']:,.0f} usually" if age[1].get("usual") else "")
                parts.append(text)
                more = (between[1].get("now") or 0) - (between[1].get("usual") or 0)     # waiting there beyond usual
                if more >= max(APPEARED, MATERIAL * rows):
                    off = True
                    # rows that wait there since less long than usual came late to it (the wake of the stage
                    # before); since as long or longer, they are held there
                    young = bool(age and age[1].get("now") is not None and age[1].get("usual")
                                 and age[1]["now"] < YOUNG * age[1]["usual"])
                    if not young:
                        own.setdefault(k, f"{between[1]['now']:,.0f} rows past {stages[k - 1]} and not yet there "
                                          f"against {between[1].get('usual') or 0:,g} usually")
        if off and first is None:
            first = k
        lines.append(f + (f" ({what[f]})" if what.get(f) else "") + ": " + "; ".join(parts or ["no figure"]))
    return lines, first, own


def waited_on(summaries: dict[str, dict[str, Any]], fields: list[str], fixed: set[str],
              before_last: bool) -> tuple[str, list[str]] | None:
    """What the rows waiting at a stage wait on: (field, its one or two values that stand out). `fields`: the ones
    that localize the change, the best first. Waiting before the last stage (for a slot, a token, a queue), a field
    the scope does not fix comes first: the application asked is the question's own, the pool its rows share is
    what they wait for (outside the scope on the application are only its other rows). Still running at the last
    stage: the best field only."""
    for f in (sorted(fields, key=lambda f: f in fixed) if before_last else fields[:1]):
        flagged = [g for g in summaries[f]["groups"] if g.get("unusual") and g["value"] != NONE]
        if flagged and len(flagged) <= 2:
            return f, [str(g["value"]) for g in flagged]
    return None


def survey(judged: list[tuple], shown: int = 4, over: dict[str, str] | None = None, stages: list[str] | None = None,
           what: dict[str, str] | None = None, every_row: list[str] | None = None,
           fixed: set[str] | None = None) -> dict[str, Any]:
    """Several measures of a table at once: which are off and where each is concentrated, in words (the
    conclusion) and in figures (the values that stand out); the ones as usual in a line. Measures that say
    the same thing (the same values of the same field: what had started and what had ended) are said once.
    `over` {measure: the rows it is over}: said with the measure. `stages`: the table's time fields in the order
    the rows reach them (`what` each is): the stages are given in that order, and the first that is off is
    said first (what is late after it is late in its wake). `fixed`: the fields the scope conditions: what the
    rows wait on is looked for on the others first (the application asked is the question's; the pool its rows
    share is what they wait for)."""
    out: list[dict[str, Any]] = []
    lines: list[str] = []
    said: dict[tuple, dict[str, Any]] = {}
    res: dict[str, Any] = {}
    first, own = None, {}
    if stages:
        res["stages"], first, own = stage_lines(judged, stages, what or {}, every_row or [])

    def stage_of(m: Any) -> int | None:
        if not stages or m.kind not in ("reached", "between", "lapse", "age"):
            return None
        f = m.fields[-1]
        return stages.index(f) if f in stages else None

    order = ordered(judged)
    if stages:                          # the first stage that is off, what the other measures say, the other stages
        head = [e for e in order if first is not None and stage_of(e[0]) == first]
        plain = [e for e in order if stage_of(e[0]) is None]
        rest = [e for e in order if stage_of(e[0]) is not None and e not in head]
        order = head[:1] + plain[:2] + head[1:] + rest + plain[2:]
        for e in head:                  # the rows wait at that stage: on which value(s) of which field
            found = localized(e[2]) if e[0].kind in ("between", "age", "lapse") else []
            on = waited_on(e[2], found, fixed or set(), first < len(stages) - 1)
            if on:
                res["waits_on"] = {"stage": stages[first], "field": on[0], "values": on[1]}
                break
    for m, overall, summaries in order:
        best = localized(summaries)
        verdict = overall.get("verdict", "no usual")
        entry: dict[str, Any] = {"measure": m.text(), **{k: v for k, v in overall.items() if k not in (
            ("earlier_days", "rows_now", "rows_usual") if m.counting else ("earlier_days", "rows_usual"))}}
        if (over or {}).get(m.text()):
            entry["over"] = over[m.text()]
        if best:
            top = summaries[best[0]]
            flagged = [g for g in top["groups"] if g.get("unusual")]
            key = (best[0], tuple(sorted(str(g["value"]) for g in flagged)))
            if key in said and m.counting:                  # the same values of the same field as a measure above
                said[key].setdefault("same_for", []).append(f'{m.text()} ({verdict} in total)')
                continue
            entry["field"] = best[0]
            entry["values"] = [_figures(g) for g in flagged][:4]
            also = [f'{f}: {summaries[f]["reading"]}'[:120] for f in best[1:3]]
            if also:
                entry["also_on"] = also
            said.setdefault(key, entry)
            line = f'{m.text()} ({verdict} in total): on "{best[0]}", {top["reading"]}'
        else:
            line = f'{m.text()}: {_much(overall)} in total, on no field in particular'
        if len(out) < shown:
            out.append(entry)
            lines.append(line)
    lines = [line + (f' (the same values for: {"; ".join(e.pop("same_for")[:3])})' if e.get("same_for") else "")
             for line, e in zip(lines, out)]
    quiet, later = [], []
    for e in judged:
        m, overall, _s = e
        if overall.get("verdict") == "not comparable yet":
            later.append(f'{m.text()}: {overall.get("why")}')
        elif _weight(e)[0] == 0 and stage_of(m) is None:    # (the stages are in their own lines)
            quiet.append(f'{m.text()} (x{overall["ratio"]})' if overall.get("ratio") is not None else m.text())
    if lines:
        text = ""
        if first is not None and stages:
            text = (f"In the order of the stages, {stages[first]} is the first that is clearly off: what is late "
                    "after it may only be late in its wake. ")
            later = [f"{stages[k]} ({how})" for k, how in sorted(own.items()) if k > first]
            if later:                   # a longer way to a later stage is no wake of the first: a finding of its own
                text += ("Off on its own too, whatever came before: " + "; ".join(later[:2])
                         + ": a late start does not explain it, look at it separately. ")
        text += "What stands out" + (", that stage first" if first is not None else ", the strongest first") + ": " \
            + " ".join(f"({i + 1}) {line}." for i, line in enumerate(lines))
    else:
        text = "Nothing stands out: every measure is as usual, in total and for each value of each field."
    res.update({"conclusion": text, "measures": out})
    res["said"] = {"lines": lines, "first": stages[first] if stages and first is not None else None,
                   "own": [f"{stages[k]} ({how})" for k, how in sorted(own.items()) if first is not None and k > first]}
    if quiet:
        res["as_usual"] = quiet
    if later:
        res["not_comparable_yet"] = later
    return res


def state_like(now: dict[str, int], days: list[dict[str, int]]) -> bool:
    """A field whose values change while a row advances (a status): today's rows hold values the earlier days,
    read as they ended, never had, and none of theirs is gone. Such a field does not say where a change is."""
    total = sum(now.values()) or 1
    before = set().union(*[set(d) for d in days]) if days else set()
    came = sum(n for k, n in now.items() if k not in before and k != NONE)
    gone = [k for k in before if not now.get(k) and statistics.median([d.get(k, 0) for d in days]) >= max(3.0, 0.02 * total)]
    return came >= max(3.0, STATE * total) and not gone


def follows_the_stage(now: dict[str, tuple[int, int]], days: list[dict[str, int]]) -> bool:
    """A field that says where a row is in its life (a status): {value: (rows, rows that reached the last stage)}
    today, and the rows of each value on the earlier days. Each value's rows have all reached the last stage or
    none has, both kinds exist, and the values none has reached it with are values the earlier days, read as
    they ended, do not have (RUNNING, QUEUED). Such a field does not say where a change is. (A value of the
    earlier days whose rows are all stuck today is a finding, not a status.)"""
    total = sum(n for n, _r in now.values()) or 1
    kinds = set()
    for value, (n, reached) in now.items():
        if value == NONE or n < 3:
            continue
        slack = max(1.0, 0.02 * n)              # (a row that ends at this very minute)
        if slack < reached < n - slack:
            return False
        if reached <= slack and days and statistics.median([d.get(value, 0) for d in days]) > 0:
            return False
        if n >= max(3.0, 0.02 * total):
            kinds.add(reached >= n - slack)
    return kinds == {True, False}
