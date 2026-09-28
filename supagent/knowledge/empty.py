"""Why a query returned no rows, from the data dictionary only (no query runs: an empty result
must not cost the databases more): a filter value that is not a value of the field or label
(with the right case, or the closest values), or a time window before the data starts, or after
it stopped. Nothing is said when the dictionary does not know, so that the agent is never misled.

It turns "no rows" into the fix, instead of the agent guessing other values one query at a time."""

from __future__ import annotations

import datetime as dt
import difflib
import re
from typing import Any

from superset import db

SHOWN = 12                      # values listed at most
STOPPED_DAYS = 2                # data that ended this long before it was learned is "stopped"


def _literal(node: Any) -> str | None:
    from sqlglot import exp

    return node.this if isinstance(node, exp.Literal) and node.is_string else None


def _when(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    t = re.sub(r"\s*UTC$", "", str(text).strip())
    t = re.sub(r"(\d)T(\d)", r"\1 \2", t)
    t = re.sub(r"(Z|[+-]\d\d:?\d\d)$", "", t)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _conditions(tree: Any) -> tuple[dict[str, list[str]], list[tuple[str, str, dt.datetime]]]:
    """{column: values it must equal} and [(column, '>=' | '<', time)] of the WHERE clauses."""
    from sqlglot import exp

    equal: dict[str, list[str]] = {}
    times: list[tuple[str, str, dt.datetime]] = []
    for where in tree.find_all(exp.Where):
        for node in where.find_all(exp.EQ, exp.In, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
            col = node.this if isinstance(node.this, exp.Column) else None
            if col is None:
                continue
            if isinstance(node, exp.Not) or isinstance(node.parent, exp.Not):
                continue
            name = col.name
            if isinstance(node, exp.EQ):
                v = _literal(node.expression)
                if v is not None:
                    equal.setdefault(name, []).append(v)
                    when = _when(v)
                    if when:
                        times += [(name, ">=", when), (name, "<", when + dt.timedelta(days=1))]
            elif isinstance(node, exp.In):
                vals = [_literal(x) for x in node.expressions]
                if vals and all(v is not None for v in vals):
                    equal.setdefault(name, []).extend(vals)
            elif isinstance(node, exp.Between):
                lo, hi = _when(_literal(node.args.get("low"))), _when(_literal(node.args.get("high")))
                if lo:
                    times.append((name, ">=", lo))
                if hi:
                    times.append((name, "<", hi))
            else:
                when = _when(_literal(node.expression))
                if when:
                    times.append((name, ">=" if isinstance(node, (exp.GT, exp.GTE)) else "<", when))
    return equal, times


def _values(stats: dict) -> tuple[list[str] | None, bool]:
    """(the known values, whether the list is complete)."""
    vals = stats.get("values")
    if not isinstance(vals, list):
        return None, False
    card = stats.get("cardinality")
    complete = not stats.get("partial") and isinstance(card, int) and card <= len(vals)
    return [str(v) for v in vals], complete


def _loose(v: str) -> str:
    """A value without case, separators and leading zeros: srv_AMER_2 ~ srv-amer-002."""
    return re.sub(r"\d+", lambda m: str(int(m.group())), re.sub(r"[\s_.\-/]+", "-", v.strip().lower()))


def _value_hint(column: str, wanted: list[str], stats: dict, backend: str) -> str | None:
    known, complete = _values(stats)
    if not known:
        return None
    lower = {k.lower(): k for k in known}
    loose = {_loose(k): k for k in known}
    missing = [w for w in dict.fromkeys(wanted) if w not in known]
    if not missing:
        return None
    parts = []
    for w in missing[:4]:
        same = lower.get(w.lower()) or loose.get(_loose(w))
        if same:
            parts.append(f"'{w}' is written '{same}'")
            continue
        close = difflib.get_close_matches(w, known, n=3, cutoff=0.6)
        if close:
            parts.append(f"'{w}' is not a value (closest: {', '.join(repr(c) for c in close)})")
        elif complete:
            parts.append(f"'{w}' is not a value")
    if not parts:
        return None
    shown = ", ".join(known[:SHOWN]) + (" ..." if len(known) > SHOWN else "")
    seen = "its values" if backend == "promagg" else "values seen when learning"
    return f'"{column}": ' + "; ".join(parts) + f" ({seen}: {shown})"


def _time_hint(table: str, times: list[tuple[str, str, dt.datetime]], time_col: str | None,
               start: str | None, end: str | None, stopped: bool) -> str | None:
    lows = [t for c, op, t in times if op == ">=" and (time_col is None or c == time_col)]
    highs = [t for c, op, t in times if op == "<" and (time_col is None or c == time_col)]
    first, last = _when(start), _when(end)
    if first and highs and max(highs) <= first:
        return f"{table} has data from {start} only: the time window is before it"
    if stopped and last and lows and min(lows) > last:
        return f"{table} has no data after {end}: the time window is after it"
    return None


def why_empty(database: Any, sql: str) -> str:
    """"" or one line: why this SELECT on an osagg / promagg database found nothing."""
    import sqlglot
    from sqlglot import exp

    from supagent.models import KObject, Source

    backend = getattr(database, "backend", None)
    if backend not in ("osagg", "promagg"):
        return ""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # pylint: disable=broad-except
        return ""
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = list(dict.fromkeys(t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes))
    equal, times = _conditions(tree)
    if backend == "promagg" and "metric_name" in equal:             # all_metrics WHERE metric_name = '...'
        tables = [t for t in tables if t != "all_metrics"] + equal.pop("metric_name")
    if not tables or len(tables) > 3:
        return ""
    try:
        src = db.session.query(Source).filter(Source.database_id == database.id).one_or_none()
        if src is None:
            return ""
        objs = (db.session.query(KObject).filter(KObject.source_id == src.id, KObject.gone_at.is_(None),
                                                 KObject.name.in_(tables) | KObject.parent.in_(tables)).all())
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return ""
    hints: list[str] = []
    for table in tables:
        top = next((o for o in objs if o.kind in ("index", "metric") and o.name == table), None)
        columns = {o.name: o for o in objs if o.kind in ("field", "label") and o.parent == table}
        for col, wanted in equal.items():
            o = columns.get(col)
            if o is not None:
                h = _value_hint(col, wanted, o.stats or {}, backend)
                if h:
                    hints.append(h)
        if top is None:
            continue
        st = top.stats or {}
        if backend == "osagg":
            rng = st.get("time_range") or [None, None]
            learned = _when(st.get("profiled_at"))
            stopped = bool(learned and _when(rng[1]) and learned - _when(rng[1]) > dt.timedelta(days=STOPPED_DAYS))
            h = _time_hint(table, times, st.get("time_field"), rng[0], rng[1], stopped)
        else:
            stopped = "no data" in str(st.get("note") or "")
            h = _time_hint(table, times, "ts", st.get("data_from"), st.get("data_to"), stopped)
        if h:
            hints.append(h)
    if not hints:
        return ""
    return "No rows. From the data dictionary: " + "; ".join(hints[:4]) + "."


PROMQL_MATCHER = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)?\s*\{([^}]*)\}')
LABEL_EQ = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"([^"]*)"')


def why_empty_promql(database: Any, expr: str) -> str:
    """The same for a PromQL expression: label values of its selectors ({label="value"})."""
    from supagent.models import KObject, Source

    wanted: dict[str, dict[str, list[str]]] = {}
    for metric, body in PROMQL_MATCHER.findall(expr or ""):
        if not metric:
            m = re.search(r'__name__\s*=\s*"([^"]+)"', body)
            metric = m.group(1) if m else ""
        if metric:
            for label, value in LABEL_EQ.findall(body):
                if label != "__name__":
                    wanted.setdefault(metric, {}).setdefault(label, []).append(value)
    if not wanted:
        return ""
    try:
        src = db.session.query(Source).filter(Source.database_id == database.id).one_or_none()
        if src is None:
            return ""
        objs = (db.session.query(KObject).filter(KObject.source_id == src.id, KObject.kind == "label",
                                                 KObject.gone_at.is_(None), KObject.parent.in_(list(wanted))).all())
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return ""
    hints = []
    for o in objs:
        values = (wanted.get(o.parent) or {}).get(o.name)
        if values:
            h = _value_hint(o.name, values, o.stats or {}, "promagg")
            if h:
                hints.append(f"{o.parent} {h}")
    return ("From the data dictionary: " + "; ".join(hints[:4]) + ".") if hints else ""
