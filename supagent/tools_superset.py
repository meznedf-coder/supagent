"""The Superset tools the agent uses most, in-process (no MCP service needed): SQL, databases,
datasets. Same names and request shapes as Superset's MCP tools; Superset's own checks decide
what the user may see (raise_for_access for SQL, database / dataset access for the lists)."""

from __future__ import annotations

import datetime as dt
import decimal
import math
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from supagent.tools import TABLE_ERROR, _as_user, _run, agent_databases, mcp, unknown_tables


def _plain(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


# What each connector can run, shown next to its databases (list_databases) and with its refusals
SQL_RULES = {
    "osagg": ("Not full SQL: OpenSearch runs the WHERE filters and the GROUP BY / aggregates. One index per query; "
              "WHERE with =, IN, ranges, LIKE, IS NULL on single fields joined by AND / OR (pairs of values: "
              "(A = x AND B = y) OR ..., never (A, B) IN ...); "
              "row lists filtered, with ORDER BY and LIMIT; the latest per key = GROUP BY key with MAX(timestamp); "
              "JOIN only in aggregating queries on equal fields. No subquery, WITH, window function or self-join "
              "over raw rows (refused above a few thousand documents): work in steps, WHERE key IN (values found "
              "by the previous query)."),
    "promagg": ("Not full SQL: turned into PromQL. One metric table per query, a ts range, GROUP BY a time bucket "
                "and labels, SUM(rate) / SUM(increase) / AVG(value) / MAX(value) / HISTOGRAM_QUANTILE. No row-by-row "
                "join of metrics, no WITH, window function or subquery over raw samples, no quantile of raw "
                "samples, no filter on the value (HAVING): promql_query for arithmetic between metrics."),
}
WHERE_HINT = (" Do not send this query again, nor a variant of it: its WHERE has a condition OpenSearch cannot run, "
              "so every document would be read. Use only =, IN, ranges, LIKE, IS NULL on single fields, joined by "
              "AND / OR; for pairs of values write (\"A\" = 'x' AND \"B\" = 'y') OR (\"A\" = 'z' AND \"B\" = 'w') "
              "in one query.")
STEPS_HINT = (" Do not send this query again, nor a variant of it: rewrite it in steps. 1) One query for the few keys "
              "you need (ids, dates), filtered, with GROUP BY the key and MAX of the timestamp for the latest one. "
              "2) One query per index with WHERE key IN (the values found), with a LIMIT. Then put the results "
              "together in your answer.")


class ExecuteSqlRequest(BaseModel):
    database_id: int = Field(description="Database id (list_databases)")
    sql: str = Field(description="One SELECT statement")
    limit: int = Field(default=1000, description="Rows returned at most (at most 10000)")


@mcp.tool
def execute_sql(request: ExecuteSqlRequest) -> dict:
    """Run one SELECT on a database, with your permissions, and return its rows."""
    t0 = time.time()
    try:
        with _as_user():
            from superset.extensions import db
            from superset.models.core import Database

            database = db.session.get(Database, int(request.database_id))
            if database is None or not agent_databases([database]):
                return {"success": False, "error": f"database {request.database_id} not found or not one the agent "
                                                   "may use (list_databases)"}
            limit = max(1, min(int(request.limit or 1000), 10000))
            from supagent.knowledge.experience import guard_sql

            refused = guard_sql(database, request.sql)
            if refused:
                return {"success": False, "error": refused}
            backend = database.backend
            try:
                columns, rows, truncated = _run(database, request.sql, limit, extract=False)
            except Exception as ex:  # pylint: disable=broad-except
                text = f"{type(ex).__name__}: {str(ex)[:1500]}"
                hint = unknown_tables(database, request.sql) if TABLE_ERROR.search(text) else ""
                if hint:                               # the real cause: a name that is no index or metric
                    return {"success": False, "error": f"{hint} ({text[:300]})"}
                if backend == "osagg" and "WHERE term evaluated" in text:
                    text += WHERE_HINT
                elif backend == "osagg" and re.search(r"pushed down|cannot run in OpenSearch|JoinRefused|safety cap",
                                                      text):
                    text += STEPS_HINT
                elif backend == "promagg" and NESTED_AGG.search(text):
                    text += NESTED_HINT
                elif backend in SQL_RULES and re.search(r"not possible|not supported|unsupported|cannot|refused",
                                                        text, re.I):
                    text += " What this database can run: " + SQL_RULES[backend]
                return {"success": False, "error": text}
            out = {"success": True, "database": database.database_name,
                   "columns": [{"name": c} for c in columns],
                   "rows": [{c: _plain(v) for c, v in zip(columns, r)} for r in rows],
                   "row_count": len(rows), "truncated": truncated, "seconds": round(time.time() - t0, 2),
                   "error": None}
            try:                                        # the SQL's own LIMIT reached: not all the rows
                from supagent.tools import _check_select

                own = _check_select(request.sql, limit)[1]
            except Exception:  # pylint: disable=broad-except
                own = 0
            if 0 < own <= limit and len(rows) == own and own > 1:
                out["note"] = (f"The SQL's own LIMIT {own:,} was reached: more rows may match. These {own:,} rows are "
                               "not all of them: for a total, count in a query without that LIMIT (COUNT(*), the sum "
                               "of the counts), and never say these rows are all.")
            zeros = len(rows) == 1 and all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)
                                                         and v == 0) for v in rows[0])
            if not rows or zeros:          # nothing (an aggregate of no rows: one row of NULLs, or a count
                # of 0): why, from the dictionary (no query)
                from supagent.knowledge.empty import why_empty

                hint = why_empty(database, request.sql, counted=bool(rows))
                if hint:
                    out["hint"] = hint
            return out
    except Exception as ex:  # pylint: disable=broad-except
        return {"success": False, "error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


@mcp.tool
def list_databases() -> dict:
    """The databases you may query: id, name, engine (osagg = OpenSearch indices, promagg =
    Prometheus / Mimir metrics)."""
    with _as_user():
        from superset.extensions import db, security_manager
        from superset.models.core import Database

        out = []
        for d in agent_databases(list(db.session.query(Database).order_by(Database.id))):
            if security_manager.can_access_database(d):
                item = {"id": d.id, "name": d.database_name, "backend": d.backend,
                        "kind": {"osagg": "OpenSearch indices", "promagg": "Prometheus / Mimir metrics"}.get(
                            d.backend, d.backend)}
                if d.backend in SQL_RULES:
                    item["sql"] = SQL_RULES[d.backend]
                out.append(item)
        return {"databases": out}


class ListDatasetsRequest(BaseModel):
    search: str | None = Field(default=None, description="Part of the dataset name")
    database_id: int | None = Field(default=None, description="Only this database")
    page_size: int = Field(default=50, description="Datasets returned at most")


@mcp.tool
def list_datasets(request: ListDatasetsRequest | None = None) -> dict:
    """Superset datasets you may use (for charts): id, name, database."""
    request = request or ListDatasetsRequest()
    with _as_user():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db, security_manager

        from superset.models.core import Database

        usable = [d.id for d in agent_databases(db.session.query(Database).all())] or [-1]
        q = db.session.query(SqlaTable).filter(SqlaTable.database_id.in_(usable)).order_by(SqlaTable.id)
        if request.database_id is not None:
            q = q.filter(SqlaTable.database_id == int(request.database_id))
        if request.search:
            q = q.filter(SqlaTable.table_name.ilike(f"%{request.search}%"))
        out = []
        for ds in q:
            if len(out) >= max(1, min(request.page_size, 200)):
                break
            if security_manager.can_access_datasource(ds):
                out.append({"id": ds.id, "table_name": ds.table_name, "database_id": ds.database_id,
                            "database": ds.database.database_name, "schema": ds.schema,
                            "description": (ds.description or "")[:200]})
        return {"datasets": out, "count": len(out)}


class DatasetInfoRequest(BaseModel):
    identifier: int = Field(description="Dataset id (list_datasets)")


@mcp.tool
def get_dataset_info(request: DatasetInfoRequest) -> dict:
    """A dataset's columns (name, type, time column, description) and saved metrics."""
    with _as_user():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db, security_manager

        ds = db.session.get(SqlaTable, int(request.identifier))
        if ds is None or not security_manager.can_access_datasource(ds) or not agent_databases([ds.database]):
            return {"error": f"dataset {request.identifier} not found or not allowed"}
        from supagent.knowledge.excluded import of_table

        banned = {} if ds.sql else {x["name"]: x["why"] for x in of_table(ds.database_id, ds.table_name)}
        return {"id": ds.id, "table_name": ds.table_name, "database_id": ds.database_id,
                "database": ds.database.database_name, "main_dttm_col": ds.main_dttm_col,
                "columns": [{"column_name": c.column_name, "type": c.type, "is_dttm": bool(c.is_dttm),
                             "description": c.description or "",
                             **({"do_not_use": f"the team: {banned[c.column_name]}"} if c.column_name in banned else {})}
                            for c in ds.columns],
                "metrics": [{"metric_name": m.metric_name, "expression": m.expression,
                             "description": m.description or ""} for m in ds.metrics]}


TIME_NAMES = ("ts", "time", "timestamp", "@timestamp", "datetime", "date", "t", "bucket", "hour", "day")
PROMQL_CALL = re.compile(r"\bpromql\s*\(", re.I)
TS_WINDOW = re.compile(r'(\bts\b|"ts")\s*(>=|>|<=|<|between\b)', re.I)


class VirtualDatasetRequest(BaseModel):
    database_id: int = Field(description="The database the query runs on")
    sql: str = Field(description="The query as it ran for the finding (a PromQL finding, on a promagg database: "
                                 "SELECT ts, <its labels>, value FROM promql('<the PromQL>') WHERE ts >= "
                                 "TIMESTAMP '<start>' AND ts < TIMESTAMP '<end>')")
    name: str = Field(description="A short name for the new dataset, e.g. 'CPU busy % srv-amer-002'")


def _virtual_out(ds: Any, reused: bool = False) -> dict:
    time_col = ds.main_dttm_col
    return {"dataset_id": ds.id, "name": ds.table_name, "database_id": ds.database_id, "reused": reused,
            "columns": [{"name": c.column_name, "type": c.type, "is_dttm": bool(c.is_dttm)} for c in ds.columns],
            "time_column": time_col,
            "next": f"generate_chart with dataset_id {ds.id}" + (f", x = {time_col}" if time_col else "") +
                    ", y = AVG of the value column, the finding's time range, save_chart=true"}


NESTED_AGG = re.compile(r"\b(AVG|SUM|MIN|MAX|COUNT)\s*\([^)]*\b(SUM|AVG|RATE|INCREASE|MIN|MAX)\s*\(", re.I)
NESTED_HINT = (" An aggregate inside another (AVG(SUM(...))) cannot be pushed to Prometheus: one level only. The busy "
               "share of each server over the whole window is already 100 * SUM(rate) FILTER (WHERE mode <> 'idle') / "
               "SUM(rate) grouped by \"node\" (no AVG around it); ORDER BY it DESC LIMIT 5 gives the busiest; per hour: "
               "GROUP BY DATE_TRUNC('hour', ts), \"node\" with WHERE \"node\" IN (those 5).")


def osagg_safe(sql: str, backend: str | None) -> str:
    """A dataset's SQL for osagg: Superset reads the columns of a new dataset with the SQL + LIMIT 0,
    and osagg (0.2.6) turns ORDER BY ... LIMIT 0 into a top-0 terms request that OpenSearch refuses
    ("[terms] failed to parse field [size]"). Wrapped, the LIMIT 0 goes to the outer SELECT."""
    if backend != "osagg" or not re.search(r"\border\s+by\b", sql, re.I):
        return sql
    try:
        import sqlglot

        tree = sqlglot.parse_one(sql, read="duckdb")
        if tree.args.get("order") is None:           # ORDER BY only inside: fine as it is
            return sql
    except Exception:  # pylint: disable=broad-except
        pass
    return f"SELECT * FROM ({sql}) AS agent_query"


@mcp.tool
def create_virtual_dataset(request: VirtualDatasetRequest) -> dict:
    """Save a query as a Superset dataset (a virtual dataset), to chart a finding that is a
    calculation (a percentage, a ratio, PromQL); then call generate_chart with the dataset_id it
    returns. The same name and query give back the same dataset."""
    sql = (request.sql or "").strip().rstrip(";").strip()
    name = re.sub(r"\s+", " ", request.name or "").strip()[:200]
    if not sql or not name:
        return {"error": "give the query (sql) and a name"}
    with _as_user() as (_app, user):
        from superset.commands.dataset.create import CreateDatasetCommand
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db, security_manager
        from superset.models.core import Database

        database = db.session.get(Database, int(request.database_id))
        if database is None or not agent_databases([database]):
            return {"error": f"database {request.database_id} not found or not allowed"}
        # what Superset's REST API checks before its command: the right to create datasets
        if not security_manager.can_access("can_write", "Dataset"):
            return {"error": "you may not create datasets in Superset (the Dataset write permission): chart an "
                             "existing dataset, or ask an admin"}
        if PROMQL_CALL.search(sql) and not security_manager.can_access_database(database):
            return {"error": "promql() reads the whole database: it needs access to the database in Superset"}
        if PROMQL_CALL.search(sql) and not TS_WINDOW.search(sql):   # else a chart grouped by a label fails
            return {"error": "a promql() dataset needs the finding's time window in its SQL: add WHERE ts >= "
                             "TIMESTAMP '<start>' AND ts < TIMESTAMP '<end>' after promql(...)"}
        sql = osagg_safe(sql, database.backend)
        same = (db.session.query(SqlaTable)
                .filter(SqlaTable.database_id == database.id, SqlaTable.table_name.like(name + "%")).all())
        for ds in same:                                  # asked again (a retry): the same dataset
            if (ds.sql or "").strip().rstrip(";").strip() == sql and user.id in {o.id for o in ds.owners or []}:
                return _virtual_out(ds, reused=True)
        taken, final, n = {ds.table_name for ds in same}, name, 2
        while final in taken:
            final, n = f"{name} ({n})", n + 1
        try:
            ds = CreateDatasetCommand({"database": database.id, "table_name": final, "sql": sql,
                                       "owners": [user.id]}).run()      # Superset checks the SQL's access
        except Exception as ex:  # pylint: disable=broad-except
            db.session.rollback()
            detail = ex.normalized_messages() if hasattr(ex, "normalized_messages") else None
            cause = f" ({ex.__cause__})" if ex.__cause__ else ""
            return {"error": f"the dataset was not created: {detail or ex}{cause}"[:1500]}
        cols = list(ds.columns)
        if not any(c.is_dttm for c in cols):            # the time column: charts filter and group on it
            for c in cols:
                if c.column_name.lower() in TIME_NAMES:
                    c.is_dttm = True
                    break
        if not ds.main_dttm_col:
            ds.main_dttm_col = next((c.column_name for c in cols if c.is_dttm), None)
        db.session.commit()
        return _virtual_out(ds)


def drop_unused_datasets(trace: list[dict]) -> list[int]:
    """After an answer that saved a chart: the datasets it created that no chart uses (tries that did
    not work) are deleted, as the user who asked. Nothing is deleted when no chart was saved, nor a
    dataset that any chart of Superset uses."""
    import json

    created, used, saved = [], set(), False
    for t in trace:
        tool = t.get("called") or t.get("tool")
        if t.get("status") != "done":
            continue
        if tool == "create_virtual_dataset":
            try:
                res = json.loads(t.get("result") or "{}")
            except ValueError:
                continue
            if isinstance(res, dict) and res.get("dataset_id") and not res.get("reused"):
                created.append(int(res["dataset_id"]))
        elif tool in ("generate_chart", "update_chart"):
            args = t.get("args") or {}
            req = args.get("request") if isinstance(args.get("request"), dict) else args
            saved = saved or tool == "update_chart" or bool(req.get("save_chart"))
            if req.get("dataset_id") is not None:
                used.add(int(req["dataset_id"]))
    unused = [d for d in created if d not in used]
    if not saved or not unused:
        return []
    with _as_user():
        from superset.commands.dataset.delete import DeleteDatasetCommand
        from superset.extensions import db
        from superset.models.slice import Slice

        charted = {i for (i,) in db.session.query(Slice.datasource_id)
                   .filter(Slice.datasource_type == "table", Slice.datasource_id.in_(unused))}
        unused = [d for d in unused if d not in charted]
        if not unused:
            return []
        try:
            DeleteDatasetCommand(unused).run()
        except Exception:  # pylint: disable=broad-except   (kept: the answer is not affected)
            db.session.rollback()
            return []
    return unused
