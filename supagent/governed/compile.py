"""From a checked plan step to the query that runs, by code. The model never writes the SQL: each part
of the query comes from a part of the plan, so what the query counts is what the plan says, with the
sources checked (validate).

osagg (OpenSearch): "field" names quoted, the period on the index's time field, COUNT/SUM/AVG/MIN/MAX,
COUNT(DISTINCT), FILTER (WHERE ...) for a measure's own conditions, DATE_TRUNC buckets.
promagg (Prometheus / Mimir): one metric per query, SUM(increase) / SUM(rate) for counters, AVG/MIN/MAX(value)
for gauges, HISTOGRAM_QUANTILE for histograms, the period on ts, DATE_TRUNC / TIME_BUCKET buckets.
Units: seconds to minutes or hours, bytes to MiB or GiB, from the field's unit (the dictionary, its name,
or the plan's source_unit).
"""

from __future__ import annotations

import re
from typing import Any

from supagent.governed.decider import TableInfo
from supagent.governed.plan import Cond, Measure, Step
from supagent.governed.validate import BUILTIN_FORMULAS, _catalog_formula

ROW_CAP = 1000                 # rows of a step without a top N (more: the answer says the result was cut)
SECONDS = {"milliseconds": 0.001, "seconds": 1.0, "minutes": 60.0, "hours": 3600.0, "days": 86400.0}
BYTES = {"bytes": 1.0, "KiB": 1024.0, "MiB": 1024.0 ** 2, "GiB": 1024.0 ** 3, "TiB": 1024.0 ** 4}
UNIT_NAMES = (("_milliseconds", "milliseconds"), ("_ms", "milliseconds"), ("_seconds", "seconds"), ("_secs", "seconds"),
              ("_bytes", "bytes"), ("_mb", "MiB"), ("_gb", "GiB"), ("_percent", "percent"), ("_pct", "percent"),
              ("_ratio", "ratio"))


class CompileError(ValueError):
    pass


def ident(name: str, backend: str) -> str:
    if backend == "promagg" and re.fullmatch(r"[a-z_][a-z0-9_]*", name or ""):
        return name
    return '"' + str(name).replace('"', '""') + '"'


def literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return text
    return "'" + text.replace("'", "''") + "'"


def unit_of(t: TableInfo, field: str | None, m: Measure) -> str | None:
    """The unit of a field (an index) or of the metric's value: the plan's source_unit, the dictionary, the name."""
    if m.source_unit:
        return m.source_unit
    if t.kind == "metric" or not field:
        u = (t.unit or "").lower()
        if u in SECONDS or u in {k.lower() for k in BYTES}:
            return next((k for k in list(SECONDS) + list(BYTES) if k.lower() == u), None)
        name = t.name.lower()
    else:
        col = t.columns.get(field) or {}
        u = str(col.get("unit") or "").lower()
        if u:
            return next((k for k in list(SECONDS) + list(BYTES) if k.lower() == u), None)
        name = field.lower()
    for suffix, unit in UNIT_NAMES:
        if suffix in name:
            return unit
    return None


def factor(src: str | None, dst: str | None) -> float:
    """What a value in `src` is multiplied by to be in `dst` (seconds to minutes: 1/60)."""
    if not dst or dst == src or dst == "percent":
        return 1.0
    if src is None:
        raise CompileError(f"the unit of the value is not known (give source_unit to convert it to {dst})")
    for table in (SECONDS, BYTES):
        if src in table and dst in table:
            return table[src] / table[dst]
    raise CompileError(f"{src} cannot be converted to {dst}")


def cond_sql(c: Cond, backend: str, found: dict[str, list[Any]] | None = None) -> str:
    col = ident(c.field, backend)
    value = c.value
    if isinstance(value, str) and re.fullmatch(r"q\d+\.[\w.@-]+", value.strip(), re.I):
        key = value.strip()
        key = key.split(".", 1)[0].lower() + "." + key.split(".", 1)[1]
        values = (found or {}).get(key)
        if values is None:
            raise CompileError(f"{value}: the values of that step are not known yet")
        value = values
        op = "in" if c.op in ("=", "in") else "not in" if c.op in ("!=", "not in") else c.op
    else:
        op = c.op
    if op == "is null":
        return f"{col} IS NULL"
    if op == "is not null":
        return f"{col} IS NOT NULL"
    if op in ("in", "not in"):
        values = value if isinstance(value, list) else [value]
        if not values:
            return "FALSE" if op == "in" else "TRUE"
        return f"{col} {'NOT IN' if op == 'not in' else 'IN'} ({', '.join(literal(v) for v in values)})"
    if isinstance(value, list):
        if op == "=":
            return cond_sql(Cond(field=c.field, op="in", value=value, source=c.source), backend, found)
        if op == "!=":
            return cond_sql(Cond(field=c.field, op="not in", value=value, source=c.source), backend, found)
        raise CompileError(f"{c.field} {op}: one value only")
    sql_op = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "like": "LIKE",
              "not like": "NOT LIKE"}[op]
    return f"{col} {sql_op} {literal(value)}"


def measure_sql(m: Measure, t: TableInfo, found: dict[str, list[Any]] | None = None) -> str:
    backend = "promagg" if t.kind == "metric" else "osagg"
    flt = ""
    if m.where:
        flt = " FILTER (WHERE " + " AND ".join(cond_sql(c, backend, found) for c in m.where) + ")"
    field = ident(m.field, backend) if m.field else ""
    if t.kind == "index":
        expr = {"count": f"COUNT(*){flt}", "count_distinct": f"COUNT(DISTINCT {field}){flt}",
                "sum": f"SUM({field}){flt}", "avg": f"AVG({field}){flt}", "min": f"MIN({field}){flt}",
                "max": f"MAX({field}){flt}", "share": f"100.0 * COUNT(*){flt} / COUNT(*)"}.get(m.fn)
    else:
        counter = (t.metric_type or "").lower() == "counter" or t.name.endswith(("_total", "_count", "_sum"))
        base = "increase" if counter else "value"
        if m.fn == "quantile":
            expr = f"HISTOGRAM_QUANTILE({float(m.q)}, SUM(RATE(value)))"
        elif m.fn == "formula":
            expr = BUILTIN_FORMULAS.get(m.formula or "", (None,))[0] or _catalog_formula(m.formula)
            if not expr:
                raise CompileError(f"formula {m.formula!r} is unknown")
            expr = _formula_sql(expr)
        elif m.fn == "share":
            expr = f"100.0 * SUM({base}){flt} / SUM({base})" if counter else f"100.0 * AVG(value){flt} / AVG(value)"
        else:
            expr = {"increase": f"SUM(increase){flt}", "rate": f"SUM(rate){flt}", "avg": f"AVG(value){flt}",
                    "min": f"MIN(value){flt}", "max": f"MAX(value){flt}"}.get(m.fn)
    if not expr:
        raise CompileError(f"{m.fn} cannot be computed on {t.name}")
    if m.unit and m.fn not in ("count", "count_distinct", "share") and not (m.fn == "formula" and m.unit == "percent"):
        f = factor(unit_of(t, m.field, m), m.unit)       # the value times f is in the unit asked
        if f != 1.0:
            expr = f"({expr}) / {1 / f:g}" if f < 1 else f"({expr}) * {f:g}"
    return expr


def _formula_sql(text: str) -> str:
    """A formula entry's expression: "name = <sql>" or just the SQL."""
    t = (text or "").strip()
    m = re.match(r"^[\w .%-]{1,60}=\s*(.+)$", t, re.S)
    return (m.group(1) if m and "(" in m.group(1) else t).strip().rstrip(";")


def bucket_sql(bucket: str, t: TableInfo) -> str:
    col = ident(t.time_field, "osagg") if t.kind == "index" else "ts"
    if bucket in ("minute", "hour", "day", "week", "month"):
        return f"DATE_TRUNC('{bucket}', {col})"
    return f"TIME_BUCKET(INTERVAL '{bucket}', {col})"


def compile_step(step: Step, t: TableInfo, found: dict[str, list[Any]] | None = None) -> str:
    """The SQL of one step (checked by parsing it)."""
    backend = "promagg" if t.kind == "metric" else "osagg"
    select, group = [], []
    if step.bucket:
        select.append(f"{bucket_sql(step.bucket, t)} AS {ident('time', backend)}")
        group.append(bucket_sql(step.bucket, t))
    for b in step.by:
        select.append(f"{ident(b, backend)}")
        group.append(ident(b, backend))
    for m in step.measures:
        select.append(f"{measure_sql(m, t, found)} AS {ident(m.label, 'osagg')}")
    where = [cond_sql(c, backend, found) for c in step.where]
    if step.period is not None:
        if t.kind == "index":
            if not t.time_field:
                raise CompileError(f"{t.name} has no time field for the period")
            tf = ident(t.time_field, backend)
            where = [f"{tf} >= {literal(step.period.start)}", f"{tf} < {literal(step.period.end)}"] + where
        else:
            where = [f"ts >= TIMESTAMP {literal(step.period.start)}", f"ts < TIMESTAMP {literal(step.period.end)}"] + where
    sql = f"SELECT {', '.join(select)} FROM {ident(t.name, 'osagg')}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    order = []
    labels = {m.label.lower(): m.label for m in step.measures}
    for o in step.order:
        real = labels.get(o.by.lower())
        target = ident(real, "osagg") if real else ident(next((b for b in step.by if b.lower() == o.by.lower()), o.by),
                                                             backend)
        order.append(f"{target} {'DESC' if o.desc else 'ASC'}")
    if not order and step.bucket:
        order.append(f"{ident('time', backend)} ASC")
    if order:
        sql += " ORDER BY " + ", ".join(order)
    sql += f" LIMIT {int(step.limit) if step.limit else ROW_CAP}"
    _parse(sql)
    return sql


def _parse(sql: str) -> None:
    import sqlglot

    try:
        sqlglot.parse_one(sql, read="duckdb")
    except Exception as ex:  # pylint: disable=broad-except
        raise CompileError(f"the query built from the plan does not parse: {ex}") from ex


CHART_OPS = {"=": "==", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "in": "IN", "not in": "NOT IN",
             "like": "LIKE", "not like": "NOT LIKE", "is null": "IS NULL", "is not null": "IS NOT NULL"}


def dataset_for(t: TableInfo) -> Any:
    """The Superset dataset of this table (physical, same database and name), or None."""
    from superset import db
    from superset.connectors.sqla.models import SqlaTable

    return (db.session.query(SqlaTable).filter(SqlaTable.database_id == t.database_id, SqlaTable.table_name == t.name,
                                               SqlaTable.sql.is_(None)).order_by(SqlaTable.id).first())


def query_object(step: Step, t: TableInfo, found: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    """The step as a Superset query object (what a chart asks): the dataset's permissions and row-level security
    apply when it runs through the chart data API."""
    columns: list[Any] = []
    if step.bucket:
        columns.append({"expressionType": "SQL", "sqlExpression": bucket_sql(step.bucket, t), "label": "time"})
    columns += list(step.by)
    metrics = [{"expressionType": "SQL", "sqlExpression": measure_sql(m, t, found), "label": m.label}
               for m in step.measures]
    filters = []
    for c in step.where:
        value = c.value
        op = c.op
        if isinstance(value, str) and re.fullmatch(r"q\d+\.[\w.@-]+", value.strip(), re.I):
            key = value.strip().split(".", 1)[0].lower() + "." + value.strip().split(".", 1)[1]
            if key not in (found or {}):
                raise CompileError(f"{value}: the values of that step are not known yet")
            value, op = found[key], "in" if c.op in ("=", "in") else "not in"
        if isinstance(value, list) and op in ("=", "!="):
            op = "in" if op == "=" else "not in"
        f: dict[str, Any] = {"col": c.field, "op": CHART_OPS[op]}
        if op not in ("is null", "is not null"):
            f["val"] = value
        filters.append(f)
    if step.period is not None:
        tf = t.time_field if t.kind == "index" else "ts"
        start, end = step.period.start.replace(" ", "T") + ":00", step.period.end.replace(" ", "T") + ":00"
        filters.append({"col": tf, "op": "TEMPORAL_RANGE", "val": f"{start} : {end}"})
    by_label = {m["label"].lower(): m for m in metrics}
    orderby = []
    for o in step.order:
        target = by_label.get(o.by.lower()) or next((b for b in step.by if b.lower() == o.by.lower()), None)
        if target is not None:
            orderby.append([target, not o.desc])
    return {"metrics": metrics, "columns": columns, "filters": filters, "orderby": orderby,
            "row_limit": int(step.limit) if step.limit else ROW_CAP, "extras": {}, "is_timeseries": False}


def run_on_dataset(step: Step, t: TableInfo, dataset: Any, found: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    """The step through Superset's chart data API on its dataset (as the current user): the result shaped like
    execute_sql's (success, database, columns, rows, row_count, truncated) with the SQL Superset ran."""
    from superset.charts.schemas import ChartDataQueryContextSchema
    from superset.commands.chart.data.get_data_command import ChartDataCommand

    qo = query_object(step, t, found)
    body = {"datasource": {"id": dataset.id, "type": "table"}, "queries": [qo], "result_type": "full",
            "result_format": "json", "force": True}
    ctx = ChartDataQueryContextSchema().load(body)
    command = ChartDataCommand(ctx)
    command.validate()                                   # the user's access to the dataset
    out = command.run()
    q = (out.get("queries") or [{}])[0]
    if q.get("error"):
        raise CompileError(str(q.get("error"))[:1500])
    rows = q.get("data") or []
    cols = q.get("colnames") or (list(rows[0]) if rows else [])
    return {"success": True, "database": t.database, "columns": [{"name": c} for c in cols], "rows": rows,
            "row_count": len(rows), "truncated": len(rows) >= qo["row_limit"], "sql": q.get("query"),
            "via": f"the dataset {dataset.table_name} (id {dataset.id}): its permissions and row-level security"}
