"""Charts and dashboards (0.7): every night, with the Context (after the day's learning), the agent looks at the
team's Superset charts as the learning user, through Superset's own chart data API (the dataset's permissions and
row-level security apply), and writes down two things per chart.

* What it found in the data: the chart's metrics per day over the last five weeks, with the chart's own dimensions
  and filters; the last full day before "now" against the same weekday of the four weeks before, per series (the
  median and the median absolute deviation, as compare_to_usual does): high, low, or normal. A chart whose data
  stops before that day is "stale" (its data did not come: never read as "low"). A chart left out says why: raw
  records, no metric, no time column, its dataset gone.
* What it shows, in words: written by the LLM from what the chart reads (its metrics, dimensions, filters, the
  data dictionary's descriptions of them), cached on that input (written again only when it changes), marked
  AI-written; one facts page per dashboard in the Context (no LLM): its charts, what each shows, the last check.

The agent reads what was found (the tool chart_anomalies; the charts' pieces in the knowledge search carry the
last check) and may look at one chart or dashboard again now. A user sees only the charts they may see, and no
figure computed by the night's look on a dataset with row-level security for them: for those, a look now, as
them. Bounded: charts.max_charts charts, charts.minutes, charts.max_requests_per_minute per database (12: a chart's query
weighs more than the learner's), a database that refuses learn.stop_after_errors queries in a row as overloaded
(a circuit breaker, 429) is left for the next night, Stop; the LLM after the people (background priority), at
most charts.max_llm_calls calls a night."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from typing import Any

from superset import db

from supagent import settings
from supagent.models import ChartScan

log = logging.getLogger(__name__)
WEEKS = 4                    # earlier weeks the last day is compared with (same weekday)
SERIES = 20                  # series of a chart compared at most (the biggest over the five weeks)
FINDINGS = 12                # findings kept per chart (the strongest)
ROW_LIMIT = 20_000
UNDERSTAND_PROMPT = """You explain one chart of a Superset dashboard to a colleague, in 2 to 4 short sentences of
plain text: what it measures (the measure and its unit), per what (its dimensions), over which period, and what
would look unusual on it. Use only the facts given (JSON); never invent a name, a number, a threshold or a cause;
leave out what the facts do not say. Write in the language of the chart's title."""


class ChartSkip(Exception):
    """The chart cannot be compared day by day (said, not an error)."""


# --------------------------------------------------------------------------------------------- #
# reading a chart
# --------------------------------------------------------------------------------------------- #
def _params(slc: Any) -> dict[str, Any]:
    try:
        p = json.loads(slc.params or "{}")
    except ValueError:
        return {}
    return p if isinstance(p, dict) else {}


def _columns(ds: Any) -> dict[str, Any]:
    return {c.column_name: c for c in (ds.columns or [])}


def time_column(params: dict[str, Any], ds: Any) -> str | None:
    """The calendar the chart is compared on: the dataset's main time column (the records' own time: a chart's x
    axis may be another time, e.g. a position time that puts D-1 and W-1 on the same day), else the chart's time
    axis, else its time column, else the first time column."""
    cols = _columns(ds)
    if ds.main_dttm_col and ds.main_dttm_col in cols:
        return ds.main_dttm_col
    x = params.get("x_axis")
    if isinstance(x, str) and x in cols and cols[x].is_dttm:
        return x
    g = params.get("granularity_sqla")
    if isinstance(g, str) and g in cols:
        return g
    return next((n for n, c in cols.items() if c.is_dttm), None)


def metrics_of(params: dict[str, Any]) -> list[Any]:
    m = params.get("metrics")
    if not m and params.get("metric"):
        m = [params["metric"]]
    return [x for x in (m if isinstance(m, list) else [m]) if x]


def label_of(x: Any) -> str:
    if isinstance(x, str):
        return x
    if not isinstance(x, dict):
        return str(x)
    if x.get("label"):
        return str(x["label"])
    if x.get("sqlExpression"):
        return str(x["sqlExpression"])
    col = x.get("column") or {}
    return f"{x.get('aggregate') or ''}({col.get('column_name') if isinstance(col, dict) else col})"


def dims_of(params: dict[str, Any], tc: str | None) -> list[Any]:
    dims = [d for d in (params.get("groupby") or []) if d]
    x = params.get("x_axis")
    if x and x != tc and x not in dims:                  # a category on the x axis is a dimension
        dims.insert(0, x)
    return dims


TIME_OPS = ("TEMPORAL_RANGE", ">", ">=", "<", "<=", "==")


def _last_day(value: Any, exclusive: bool) -> dt.date | None:
    """The last day a date bound keeps: an exclusive bound at midnight keeps the day before."""
    try:
        t = dt.datetime.fromisoformat(str(value).strip().replace("T", " ")[:19])
    except ValueError:                                   # "Last week", "No filter", DATEADD(...): follows the time
        return None
    return (t - dt.timedelta(days=1)).date() if exclusive and t.time() == dt.time() else t.date()


def period_end(params: dict[str, Any], ds: Any | None) -> dt.date | None:
    """The last day of the fixed period a chart shows (its own dates: "2026-09-01 : 2026-09-26", "timestamp_date <
    2026-09-01"), None when it follows the time ("Last week", no date)."""
    times = {n for n, c in _columns(ds).items() if c.is_dttm} if ds is not None else set()
    ends: list[dt.date] = []
    ranges = [params.get("time_range")] + [f.get("comparator") for f in params.get("adhoc_filters") or []
                                            if isinstance(f, dict) and f.get("operator") == "TEMPORAL_RANGE"]
    for r in ranges:
        if isinstance(r, str) and " : " in r:
            end = _last_day(r.split(" : ", 1)[1], exclusive=True)
            if end is not None:
                ends.append(end)
    for f in params.get("adhoc_filters") or []:
        if isinstance(f, dict) and f.get("expressionType") == "SIMPLE" and f.get("subject") in times \
                and f.get("operator") in ("<", "<=", "=="):
            end = _last_day(f.get("comparator"), exclusive=f.get("operator") == "<")
            if end is not None:
                ends.append(end)
    return min(ends) if ends else None


def filters_of(params: dict[str, Any], times: set[str] | None = None) -> tuple[list[dict[str, Any]], list[str],
                                                                              list[str]]:
    """The chart's own conditions without its time conditions (the look sets its own period): query filters, WHERE
    and HAVING expressions. `times`: the dataset's time columns."""
    filters, where, having = [], [], []
    for f in params.get("adhoc_filters") or []:
        if not isinstance(f, dict) or f.get("operator") == "TEMPORAL_RANGE" or f.get("comparator") == "No filter":
            continue
        if f.get("expressionType") == "SIMPLE" and f.get("subject") in (times or set()) and f.get("operator") in TIME_OPS:
            continue
        clause = (f.get("clause") or "WHERE").upper()
        if f.get("expressionType") == "SQL" and f.get("sqlExpression"):
            (having if clause == "HAVING" else where).append(f"({f['sqlExpression']})")
        elif f.get("expressionType") == "SIMPLE" and f.get("subject") and clause == "WHERE":
            q: dict[str, Any] = {"col": f["subject"], "op": f.get("operator")}
            if f.get("operator") not in ("IS NULL", "IS NOT NULL"):
                q["val"] = f.get("comparator")
            filters.append(q)
    return filters, where, having


def query_body(slc: Any, ds: Any, start: dt.datetime, end: dt.datetime) -> tuple[dict[str, Any], str, list[str],
                                                                                   list[str]]:
    """The chart's metrics per day from start to end with its dimensions and conditions, as a chart data query:
    (body, time column, dimension labels, metric labels)."""
    params = _params(slc)
    if params.get("query_mode") == "raw":
        raise ChartSkip("it lists raw records: no metric to compare")
    metrics = metrics_of(params)
    if not metrics:
        raise ChartSkip("no metric")
    tc = time_column(params, ds)
    if not tc:
        raise ChartSkip("its dataset has no time column")
    dims = dims_of(params, tc)
    ended = period_end(params, ds)
    last_day = (end - dt.timedelta(days=1)).date()
    if ended is not None and ended < last_day:
        raise ChartSkip(f"it shows a fixed period that ended on {ended.isoformat()}")
    filters, where, having = filters_of(params, {n for n, c in _columns(ds).items() if c.is_dttm})
    filters.append({"col": tc, "op": "TEMPORAL_RANGE", "val": f"{start.isoformat()} : {end.isoformat()}"})
    axis = {"timeGrain": "P1D", "columnType": "BASE_AXIS", "sqlExpression": tc, "label": tc, "expressionType": "SQL"}
    extras = {k: " AND ".join(v) for k, v in (("where", where), ("having", having)) if v}
    query = {"columns": [axis] + dims, "metrics": metrics, "filters": filters, "extras": extras,
             "row_limit": ROW_LIMIT, "series_columns": dims, "orderby": []}
    body = {"datasource": {"id": ds.id, "type": "table"}, "queries": [query], "result_type": "full",
            "result_format": "json", "force": True}
    return body, tc, [label_of(d) for d in dims], [label_of(m) for m in metrics]


def run_query(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows of a chart data query, as the current user (validate: their access to the dataset)."""
    from superset.charts.schemas import ChartDataQueryContextSchema
    from superset.commands.chart.data.get_data_command import ChartDataCommand

    command = ChartDataCommand(ChartDataQueryContextSchema().load(body))
    command.validate()
    out = command.run()
    q = (out.get("queries") or [{}])[0]
    if q.get("error"):
        raise RuntimeError(str(q["error"])[:800])
    return list(q.get("data") or [])


def extent_body(ds: Any, tc: str, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """The first and last day of the dataset's records from start to end (none of a chart's conditions)."""
    metrics = [{"expressionType": "SIMPLE", "column": {"column_name": tc}, "aggregate": agg, "label": label}
               for agg, label in (("MIN", "first"), ("MAX", "last"))]
    query = {"columns": [], "metrics": metrics, "row_limit": 1, "orderby": [],
             "filters": [{"col": tc, "op": "TEMPORAL_RANGE", "val": f"{start.isoformat()} : {end.isoformat()}"}]}
    return {"datasource": {"id": ds.id, "type": "table"}, "queries": [query], "result_type": "full",
            "result_format": "json", "force": True}


def extent(ds: Any, tc: str, start: dt.datetime, end: dt.datetime) -> tuple[dt.date | None, dt.date | None]:
    rows = run_query(extent_body(ds, tc, start, end))
    if not rows:
        return None, None
    return _date(rows[0].get("first")), _date(rows[0].get("last"))


def _date(v: Any) -> dt.date | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)):                     # epoch milliseconds (the chart data API's JSON)
        return dt.datetime.utcfromtimestamp(v / 1000.0).date()
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")[:26]).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------- #
# comparing the last day with the weeks before
# --------------------------------------------------------------------------------------------- #
def _countlike(values: dict[dt.date, float]) -> bool:
    return all(v >= 0 and float(v).is_integer() for v in values.values())


def analyze(rows: list[dict[str, Any]], tc: str, dims: list[str], metrics: list[str], day: dt.date,
            weeks: int = WEEKS, first: dt.date | None = None, last: dt.date | None = None) -> dict[str, Any]:
    """{status: ok | stale, findings: the series high or low on `day`, checked, last_data, reason}. `first`,
    `last`: the dataset's own first and last day in the window (its records, none of the chart's conditions):
    the data stopped (stale) when its last day is before `day`; a count series without a row on a day the dataset
    has counts 0 that day (no failed job is 0 failed jobs), another measure has no value that day. A difference
    of a few events is not unusual (at least 3 and twice the square root of the usual count)."""
    import math

    from supagent.tools import _usual

    series: dict[tuple, dict[dt.date, float]] = defaultdict(dict)
    days: set[dt.date] = set()
    for r in rows:
        d = _date(r.get(tc))
        if d is None:
            continue
        days.add(d)
        key = tuple((x, str(r.get(x))) for x in dims)
        for m in metrics:
            v = r.get(m)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
                series[(key, m)][d] = series[(key, m)].get(d, 0.0) + float(v)
    seen_last = last if last is not None else (max(days) if days else None)
    if seen_last is None:
        return {"status": "stale", "findings": [], "checked": 0, "last_data": None,
                "reason": f"no data in the {weeks + 1} weeks to {day.isoformat()}"}
    if seen_last < day:
        return {"status": "stale", "findings": [], "checked": 0, "last_data": seen_last.isoformat(),
                "reason": f"its data stops on {seen_last.isoformat()}: nothing on {day.isoformat()}"}
    if not series:
        return {"status": "ok", "findings": [], "checked": 0, "last_data": seen_last.isoformat(),
                "reason": "nothing matched its conditions in these weeks (no event)"}
    begin = first if first is not None else min(days)
    earlier = [day - dt.timedelta(days=7 * k) for k in range(1, weeks + 1) if day - dt.timedelta(days=7 * k) >= begin]
    ranked = sorted(series.items(), key=lambda kv: -sum(abs(v) for v in kv[1].values()))
    findings, checked = [], 0
    per_metric: Counter = Counter()
    for (key, m), values in ranked:
        if per_metric[m] >= SERIES:
            continue
        counts = _countlike(values)
        if counts:
            now, before = values.get(day, 0.0), [values.get(d, 0.0) for d in earlier]
        else:
            if day not in values:
                continue                                 # no measure that day: nothing to compare
            now, before = values[day], [values[d] for d in earlier if d in values]
        per_metric[m] += 1
        verdict = _usual(now, before)
        checked += 1
        if verdict.get("verdict") not in ("high", "low"):
            continue
        usual = verdict.get("median") or 0.0
        if counts and abs(now - usual) < max(3.0, 2.0 * math.sqrt(max(abs(usual), 1.0))):
            continue                                     # a few events more or less
        findings.append({"metric": m, "series": dict(key), "now": verdict["now"], "median": verdict.get("median"),
                         "change_pct": verdict.get("change_pct"), "deviations": verdict.get("deviations"),
                         "previous_weeks": verdict.get("previous_weeks"), "verdict": verdict["verdict"]})
    findings.sort(key=lambda f: -abs(f.get("deviations") or 0))
    return {"status": "ok", "findings": findings[:FINDINGS], "checked": checked, "last_data": seen_last.isoformat(),
            "reason": None}


def _day_of(now: dt.datetime) -> dt.date:
    """The last full day before now."""
    return (now - dt.timedelta(days=1)).date()


def look(slc: Any, now: dt.datetime, throttle: Any = None, extents: dict | None = None) -> dict[str, Any]:
    """Look at one chart as the current user, nothing written: {status, reason, findings, checked, day,
    datasource_id, database_id, dashboard_ids}. `extents`: the datasets' first and last days already read in this
    look ({(dataset id, column): (first, last)})."""
    from superset.connectors.sqla.models import SqlaTable

    from supagent.knowledge.stopping import LearningStopped

    day = _day_of(now)
    out: dict[str, Any] = {"day": day, "dashboard_ids": sorted(d.id for d in (getattr(slc, "dashboards", None) or [])),
                           "datasource_id": None, "database_id": None, "findings": [], "checked": 0}
    if slc.datasource_type != "table":
        return {**out, "status": "skipped", "reason": "not on a dataset"}
    ds = db.session.get(SqlaTable, slc.datasource_id)
    if ds is None:
        return {**out, "status": "skipped", "reason": "its dataset is gone"}
    out.update(datasource_id=ds.id, database_id=ds.database_id)
    start = dt.datetime.combine(day - dt.timedelta(days=7 * WEEKS), dt.time())
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time())
    try:
        body, tc, dims, metrics = query_body(slc, ds, start, end)
        db.session.commit()                               # no connection of Superset's pool held during the query
        extents = {} if extents is None else extents
        if (ds.id, tc) not in extents:
            if throttle is not None:
                throttle.wait()
            try:
                extents[(ds.id, tc)] = extent(ds, tc, start, end)
            except Exception:  # pylint: disable=broad-except   (not known: the chart's own rows say it)
                db.session.rollback()
                extents[(ds.id, tc)] = (None, None)
        first, last = extents[(ds.id, tc)]
        if last is not None and last < day:              # the data did not come: no need to read the chart
            res = analyze([], tc, dims, metrics, day, first=first, last=last)
            return {**out, "status": res["status"], "reason": res["reason"], "findings": [], "checked": 0}
        if throttle is not None:
            throttle.wait()
        rows = run_query(body)
        res = analyze(rows, tc, dims, metrics, day, first=first, last=last)
        reason = res["reason"] or (f"cut at {ROW_LIMIT:,} rows: the biggest series compared"
                                   if len(rows) >= ROW_LIMIT else None)
        return {**out, "status": res["status"], "reason": reason, "findings": res["findings"], "checked": res["checked"]}
    except ChartSkip as ex:
        return {**out, "status": "skipped", "reason": str(ex)}
    except LearningStopped:
        raise
    except Exception as ex:  # pylint: disable=broad-except   (one chart's error never stops the look)
        db.session.rollback()
        return {**out, "status": "error", "reason": f"{type(ex).__name__}: {str(ex)[:500]}"}


def scan_chart(slc: Any, now: dt.datetime, throttle: Any = None, extents: dict | None = None) -> ChartScan:
    """The night's look at one chart (as the learning user): its row written (replaced)."""
    res = look(slc, now, throttle, extents)
    row = db.session.query(ChartScan).filter(ChartScan.chart_id == slc.id).one_or_none()
    if row is None:
        row = ChartScan(chart_id=slc.id)
        db.session.add(row)
    row.scanned_at = dt.datetime.utcnow()
    for k in ("day", "dashboard_ids", "datasource_id", "database_id", "status", "reason", "findings", "checked"):
        setattr(row, k, res[k])
    db.session.commit()
    return row


OVERLOAD = re.compile(r"circuit_breaking|too many requests|\b429\b|rejected execution|es_rejected|"
                      r"timed? ?out|timeout|overload|out of memory|heap", re.I)


def charts_to_scan(limit: int) -> list[Any]:
    """The charts on dashboards first, then the most recently changed."""
    from superset.models.slice import Slice

    rows = db.session.query(Slice).all()
    rows.sort(key=lambda s: (not bool(getattr(s, "dashboards", None)),
                             -(s.changed_on or dt.datetime(1970, 1, 1)).timestamp()))
    return rows[:max(0, int(limit))]


def scan(seconds: float | None = None, limit: int | None = None, now: dt.datetime | None = None,
         chart_ids: list[int] | None = None) -> dict[str, Any]:
    """The night's look at the charts (or at these ones), as the learning user."""
    from superset.models.slice import Slice

    from supagent.knowledge.learner import learning_username
    from supagent.knowledge.stopping import check
    from supagent.knowledge.throttle import Throttle
    from supagent.security import acting_as

    if not settings.get("charts.scan") and chart_ids is None:
        return {"skipped": "charts.scan is off"}
    if now is None:
        from supagent.agent import now as agent_now

        now = agent_now()
    seconds = float(seconds if seconds is not None else 60 * int(settings.get("charts.minutes") or 20))
    limit = int(limit if limit is not None else settings.get("charts.max_charts") or 200)
    per_minute = int(settings.get("charts.max_requests_per_minute") or 12)
    stop_after = int(settings.get("learn.stop_after_errors") or 5)
    throttles: dict[int, Throttle] = {}
    extents: dict[tuple[int, str], tuple] = {}
    overloaded: Counter = Counter()          # a database's overload errors in a row (circuit breaker, 429...)
    out: Counter = Counter()
    t0 = time.time()
    with acting_as(learning_username()):
        charts = ([s for s in db.session.query(Slice).filter(Slice.id.in_(chart_ids))] if chart_ids is not None
                  else charts_to_scan(limit))
        for i, slc in enumerate(charts):
            if time.time() - t0 > seconds:
                out["left"] += len(charts) - i
                break
            check()
            key = int(getattr(getattr(slc, "datasource", None), "database_id", 0) or 0)
            if overloaded[key] >= stop_after:        # the database says it is overloaded: its charts tomorrow
                out["left_overloaded"] += 1          # (their last look kept as it was)
                continue
            throttle = throttles.setdefault(key, Throttle(per_minute, stop_after, f"charts of database {key}"))
            row = scan_chart(slc, now, throttle, extents)
            if row.status == "error" and OVERLOAD.search(row.reason or ""):
                overloaded[key] += 1
            elif row.status in ("ok", "stale"):              # read: the database answers again
                overloaded[key] = 0
            out[row.status] += 1
            out["anomalies"] += len(row.findings or [])
    out["seconds"] = round(time.time() - t0, 1)
    return dict(out)


def scan_isolated(run_id: int | None = None, **kw: Any) -> dict[str, Any]:
    """scan() in a thread of its own: its own application context and database session (acting as the learning
    user ends with the session of its context: the caller's objects stay as they were); the run's Stop applies."""
    import threading

    from flask import current_app

    app = current_app._get_current_object()
    box: dict[str, Any] = {}

    def work() -> None:
        from supagent.knowledge.stopping import watching

        with app.app_context():
            try:
                if run_id is not None:
                    with watching(run_id):
                        box["out"] = scan(**kw)
                else:
                    box["out"] = scan(**kw)
            except BaseException as ex:  # pylint: disable=broad-except   (raised again in the caller)
                box["error"] = ex
            finally:
                db.session.remove()
    t = threading.Thread(target=work, name="supagent-charts", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("out") or {}


# --------------------------------------------------------------------------------------------- #
# what each chart shows, in words (LLM, cached)
# --------------------------------------------------------------------------------------------- #
def brief(slc: Any) -> dict[str, Any] | None:
    """What the LLM reads about a chart: its title, kind, dataset, metrics and dimensions with what the data
    dictionary says of them, its conditions and time range, the dashboards it is on."""
    from superset.connectors.sqla.models import SqlaTable

    ds = db.session.get(SqlaTable, slc.datasource_id) if slc.datasource_type == "table" else None
    if ds is None:
        return None
    params = _params(slc)
    cols = _columns(ds)
    saved = {m.metric_name: m for m in (ds.metrics or [])}

    def about(name: str) -> str:
        c, m = cols.get(name), saved.get(name)
        if m is not None:
            return " ".join(x for x in (m.verbose_name or "", m.description or "", f"= {m.expression}") if x)[:300]
        if c is not None:
            return " ".join(x for x in (c.verbose_name or "", c.description or "", c.type or "") if x)[:300]
        return ""
    metrics = [{"metric": label_of(m), "about": about(m if isinstance(m, str) else
                                                       str(((m or {}).get("column") or {}).get("column_name") or ""))}
               for m in metrics_of(params)]
    tc = time_column(params, ds) if ds is not None else None
    dims = [{"by": label_of(d), "about": about(d) if isinstance(d, str) else ""} for d in dims_of(params, tc)]
    filters, where, having = filters_of(params)
    return {"chart": slc.slice_name, "kind": slc.viz_type, "description": slc.description or "",
            "dataset": ds.table_name, "dataset_description": (ds.description or "")[:400],
            "metrics": metrics, "by": dims, "time_column": tc, "time_range": params.get("time_range"),
            "conditions": [f"{f['col']} {f['op']} {f.get('val', '')}".strip() for f in filters] + where + having,
            "dashboards": sorted(d.dashboard_title for d in (getattr(slc, "dashboards", None) or []))}


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def understand(llm: Any, budget: int | None = None, seconds: float = 900.0, force: bool = False) -> dict[str, Any]:
    """What each chart shows, written again only for the charts whose input changed."""
    from supagent.knowledge.stopping import check

    budget = int(budget if budget is not None else settings.get("charts.max_llm_calls") or 40)
    out: Counter = Counter()
    t0 = time.time()
    for slc in charts_to_scan(int(settings.get("charts.max_charts") or 200)):
        b = brief(slc)
        if b is None:
            continue
        h = _hash([UNDERSTAND_PROMPT, b])
        row = db.session.query(ChartScan).filter(ChartScan.chart_id == slc.id).one_or_none()
        if row is not None and row.understanding_hash == h and row.understanding and not force:
            out["unchanged"] += 1
            continue
        if out["llm_calls"] >= budget or time.time() - t0 > seconds:
            out["left"] += 1
            continue
        check()
        db.session.commit()                               # no connection held during the LLM call
        msg = llm.chat([{"role": "system", "content": UNDERSTAND_PROMPT},
                        {"role": "user", "content": json.dumps(b, ensure_ascii=False, default=str)}], max_tokens=400)
        out["llm_calls"] += 1
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        if not text:
            out["empty"] += 1
            continue
        row = db.session.query(ChartScan).filter(ChartScan.chart_id == slc.id).one_or_none()
        if row is None:
            row = ChartScan(chart_id=slc.id)
            db.session.add(row)
        row.understanding, row.understanding_hash, row.understood_at = text[:2000], h, dt.datetime.utcnow()
        db.session.commit()
        out["written"] += 1
    return dict(out)


# --------------------------------------------------------------------------------------------- #
# what the agent and the pages read
# --------------------------------------------------------------------------------------------- #
def _n(v: Any) -> str:
    return f"{v:,}" if isinstance(v, (int, float)) else str(v)


def describe_finding(f: dict[str, Any]) -> str:
    series = ", ".join(f"{k} {v}" for k, v in (f.get("series") or {}).items())
    change = f" ({f['change_pct']:+.0f}%)" if isinstance(f.get("change_pct"), (int, float)) else ""
    return (f"{f['metric']}" + (f" for {series}" if series else "") + f": {_n(f.get('now'))} against usually "
            f"{_n(f.get('median'))}{change}, {f['verdict']}")


def summary_line(row: ChartScan | None) -> str:
    """The last check of a chart in one line, without its figures nor its series (its piece in the knowledge
    search and the dashboard pages are read by every user who may query the database: the figures are given by
    chart_anomalies, which applies the chart's permissions and row-level security)."""
    if row is None or row.scanned_at is None:
        return ""
    day = row.day.isoformat() if row.day else "?"
    if row.status == "ok":
        if not row.findings:
            return f"last check ({day}): normal ({row.checked} series against the same weekday of the 4 weeks before)"
        kinds = sorted({f.get("verdict") for f in row.findings or [] if f.get("verdict")})
        return (f"last check ({day}): {len(row.findings)} series unusual ({', '.join(kinds)}): chart_anomalies "
                "gives them")
    if row.status == "stale":
        return f"last check ({day}): its data stopped (no data that day): chart_anomalies says since when"
    if row.status == "skipped":
        return f"not checked day by day: {row.reason}"
    return f"last check ({day}): it could not be read"


def scans_of(chart_ids: list[int]) -> dict[int, ChartScan]:
    if not chart_ids:
        return {}
    return {r.chart_id: r for r in db.session.query(ChartScan).filter(ChartScan.chart_id.in_(chart_ids))}


def may_see(slc: Any) -> tuple[bool, bool]:
    """(the current user may see this chart, the night's figures may be shown to them: no row-level security
    of theirs on its dataset)."""
    from superset.extensions import security_manager as sm

    ds = getattr(slc, "datasource", None)
    if ds is None:
        return False, False
    try:
        if not sm.can_access_datasource(ds):
            return False, False
    except Exception:  # pylint: disable=broad-except
        return False, False
    try:
        rls = sm.get_rls_filters(ds)
    except Exception:  # pylint: disable=broad-except
        rls = [True]                                      # not known: a look now, as them
    return True, not rls


def dashboard_pages() -> list[dict[str, Any]]:
    """One Context facts page per dashboard (no LLM): its charts, what each shows, the last check."""
    from superset.models.dashboard import Dashboard

    from supagent.knowledge.context import slugify

    pages = []
    for d in db.session.query(Dashboard).order_by(Dashboard.id):
        charts = [s for s in (d.slices or []) if s.datasource_type == "table"]
        if not charts:
            continue
        scans = scans_of([s.id for s in charts])
        dbs = sorted({int(s.datasource.database_id) for s in charts if getattr(s, "datasource", None) is not None})
        lines = [f"# Dashboard {d.dashboard_title}", "",
                 f"Dashboard id {d.id}, {len(charts)} chart{'s' if len(charts) > 1 else ''}."
                 + (f" {d.description}" if getattr(d, "description", None) else "")]
        if any(r.day for r in scans.values()):
            day = max(r.day for r in scans.values() if r.day)
            lines += ["", f"## Last check ({day.isoformat()})", "",
                      "Each chart's last full day against the same weekday of the 4 weeks before (the figures: "
                      "chart_anomalies, for the users who may see the chart)."]
            marked = [(s, summary_line(scans[s.id])) for s in charts if scans.get(s.id) is not None
                      and scans[s.id].status in ("ok", "stale") and (scans[s.id].findings or scans[s.id].status == "stale")]
            lines += [f"- {s.slice_name}: {line}" for s, line in marked[:30]] or \
                ["- Nothing unusual on its charts that day."]
        lines += ["", "## Its charts", ""]
        for s in charts:
            r = scans.get(s.id)
            what = (r.understanding if r is not None and r.understanding else "") or f"{s.viz_type}"
            lines.append(f"- **{s.slice_name}** (chart id {s.id}): {' '.join(what.split())}"
                         + (" (AI-written)" if r is not None and r.understanding else ""))
        pages.append({"section": "technical", "slug": f"dashboard-{d.id}-{slugify(d.dashboard_title or '')}"[:120],
                      "title": f"Dashboard {d.dashboard_title}", "content": "\n".join(lines), "database_ids": dbs,
                      "sources": [{"ref": f"superset:dashboard:{d.id}", "title": d.dashboard_title}]})
    return pages
