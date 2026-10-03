"""The agent's tools, inside Superset: they act as the user who asks (a question runs in a
request context whose g.user is that user, see supagent.security), so Superset's own
permission checks decide what each one may read, run or send.

  describe_data    what the metrics, indices and fields mean, their types, units, typical
                   values and time range, how they relate (the learned data dictionary)
  data_changes     what the daily learning found different (new / gone metrics, types...)
  export_excel     a SELECT to an .xlsx file; optionally e-mailed
  chart_image      PNG screenshot of a saved chart, an explore link or a dashboard
  chart_from_sql   PNG chart drawn from the result of a SELECT (bars or lines)
  send_email       a one-off e-mail now: text, a data table, chart images, an Excel file
  list_reports / create_report   scheduled e-mail reports of dashboards and charts
  promql_query     a PromQL expression on a Prometheus / Mimir database (promagg)
  check_health     the health checks of the catalog over a time window
  list_alerts      alerts firing now and alerting rules of the metrics backend
  fix_chart_time_range   date filters of a chart saved through Superset's MCP service ->
                   its time range (dashboards ignore plain filters on the time column)
"""

from __future__ import annotations

import datetime as dt
import decimal
import html
import json
import logging
import math
import os
import queue
import re
import statistics
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from supagent.registry import Registry  # noqa: E402

mcp = Registry()
log = logging.getLogger(__name__)
XLSX_SHEET_ROWS = 1_048_575
PROFILE_TTL = 24 * 3600
EXPORT_BASE_URL = os.environ.get("EXPORT_BASE_URL", "").rstrip("/")
EXPORT_KEEP_DAYS = float(os.environ.get("EXPORT_KEEP_DAYS", "7"))


def _setting(key: str, default: Any) -> Any:
    try:
        from supagent import settings

        return settings.get(key)
    except Exception:  # pylint: disable=broad-except  (outside an app, tables not created)
        return default


def _export_dir() -> str:
    return os.path.expanduser(_setting("tools.export_dir", "~/superset-exports"))


def _export_max_rows() -> int:
    return int(_setting("tools.export_max_rows", 500000))


class ToolError(Exception):
    pass


class NoEarlierDay(ToolError):
    """A comparison whose scope has rows in the window asked and in no earlier one."""


# --------------------------------------------------------------------------- #
# Superset app context, acting as the tools' user
# --------------------------------------------------------------------------- #
@contextmanager
def _as_user() -> Iterator[tuple[Any, Any]]:
    """The app and the user the tools act as: the user who asks (inside a question), else
    the service user (MCP server mode, CLI)."""
    from supagent.security import current_actor

    with current_actor() as (app, user):
        yield app, user


BACKEND_NAMES = {"osagg": "osagg", "opensearch": "osagg", "promagg": "promagg", "prometheus": "promagg",
                 "mimir": "promagg", "metrics": "promagg"}


def agent_databases(rows: list[Any]) -> list[Any]:
    """The databases the agent may use: setting agent.databases (names or ids), by default the
    OpenSearch (osagg) and Prometheus / Mimir (promagg) ones. Superset's access rules still apply."""
    from supagent import settings

    try:
        wanted = [str(x).strip() for x in settings.get("agent.databases") or [] if str(x).strip()]
    except Exception:  # pylint: disable=broad-except
        wanted = []
    if not wanted:
        return [d for d in rows if d.backend in ("osagg", "promagg")]
    low = {w.lower() for w in wanted}
    return [d for d in rows if str(d.id) in wanted or (d.database_name or "").lower() in low]


def _match_databases(ref: str | int, rows: list[Any]) -> list[Any]:
    """A database by id (int or digits), exact name, name in any case, the only name containing it,
    or the only close name (a name the LLM got slightly wrong)."""
    import difflib

    s = str(ref).strip()
    for test in (lambda d: str(d.id) == s, lambda d: d.database_name == s,
                 lambda d: (d.database_name or "").lower() == s.lower()):
        found = [d for d in rows if test(d)]
        if found:
            return found
    low = s.lower()
    inside = [d for d in rows if low and (low in (d.database_name or "").lower())]
    if len(inside) == 1:
        return inside
    names = {(d.database_name or "").lower(): d for d in rows}
    close = difflib.get_close_matches(low, list(names), n=2, cutoff=0.85)
    if len(close) == 1 or (len(close) == 2 and difflib.SequenceMatcher(None, low, close[0]).ratio() -
                           difflib.SequenceMatcher(None, low, close[1]).ratio() > 0.05):
        return [names[close[0]]]
    return []


def _all_metrics_name(conn: Any) -> str:
    return getattr(conn, "all_metrics", None) or ""


def _sql_database(sql: str | None, rows: list[Any]) -> Any | None:
    """The metrics database whose tables the SELECT reads (a metric, the all_metrics table or
    promql()), among the given ones; None when it reads no metric (the default database)."""
    if not sql:
        return None
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql.replace("\\n", "\n").replace('\\"', '"'), read="duckdb")
    except Exception:  # pylint: disable=broad-except
        return None
    names, promql = set(), False
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Anonymous) and t.this.name.lower() == "promql":
            promql = True
        elif t.name:
            names.add(t.name)
    promaggs = [d for d in rows if d.backend == "promagg"]
    try:
        preferred = (_catalog().get("metrics") or {}).get("database")
    except Exception:  # pylint: disable=broad-except
        preferred = None
    promaggs.sort(key=lambda d: (d.database_name != preferred, d.id))
    if promql and promaggs:
        return promaggs[0]
    for d in promaggs:
        try:
            conn = _promagg_connection(d)
            try:
                if names & (set(conn.list_tables()) | {_all_metrics_name(conn)}):
                    return d
            finally:
                conn.close()
        except Exception:  # pylint: disable=broad-except
            continue
    return None


def _database(ref: str | int | None, backend: str | None = None, sql: str | None = None) -> Any:
    """The database a tool reads, among those the agent may use (agent.databases) and the user
    may query (Superset's database access): by id or name (see _match_databases), else the one
    whose tables the SQL reads, else the first of the backend (osagg by default)."""
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.security import can_use_database

    allowed = {x.strip() for x in os.environ.get("EXPORT_DATABASES", "").split(",") if x.strip()}
    rows = agent_databases([d for d in db.session.query(Database).all() if can_use_database(d)])
    if allowed:
        rows = [d for d in rows if d.database_name in allowed or str(d.id) in allowed]
    if ref is not None and str(ref).strip().lower() in BACKEND_NAMES and \
            not any(d.database_name == str(ref) for d in rows):
        backend, ref = BACKEND_NAMES[str(ref).strip().lower()], None
    if ref is None or str(ref).strip() == "":
        by_sql = _sql_database(sql, rows) if backend in (None, "promagg") else None
        if by_sql is not None:
            return by_sql
        found = [d for d in rows if d.backend == backend] if backend else \
            ([d for d in rows if d.backend == "osagg"] or rows)
        if backend == "promagg" and len(found) > 1:     # several metrics databases: the catalog's first
            try:
                preferred = (_catalog().get("metrics") or {}).get("database")
            except Exception:  # pylint: disable=broad-except
                preferred = None
            found.sort(key=lambda d: (d.database_name != preferred, d.id))
    else:
        found = _match_databases(ref, rows)
    if not found:
        names = ", ".join(f"{d.id}: {d.database_name} ({d.backend})" for d in rows)
        raise ToolError(f"database {ref!r} not found or not allowed; give its id or its exact name "
                        f"(databases: {names})")
    return found[0]


def _check_select(sql: str, max_rows: int) -> tuple[str, int]:
    """One SELECT only; returns it with a LIMIT of at most max_rows + 1 (to see truncation)."""
    import sqlglot
    from sqlglot import exp

    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as ex:
        if "\\n" not in sql and '\\"' not in sql:
            raise ToolError(f"SQL syntax error: {ex}") from ex
        return _check_select(sql.replace("\\n", "\n").replace('\\"', '"'), max_rows)
    if len(stmts) != 1 or not isinstance(stmts[0], (exp.Select, exp.Union)):
        raise ToolError("give exactly one SELECT statement")
    stmt = stmts[0]
    if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
                          exp.Merge, exp.Command)) for n in stmt.walk()):
        raise ToolError("only SELECT is allowed")
    limit = stmt.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    current = int(value.this) if isinstance(value, exp.Literal) and not value.is_string else None
    if current is None or current > max_rows:
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(max_rows + 1)))
        current = max_rows + 1
    return stmt.sql(dialect="duckdb"), current


@contextmanager
def _db_connection(database: Any, extract: bool, max_rows: int = 0) -> Iterator[Any]:
    """DB-API connection of a Superset database. osagg: for extracts, the bounded row joins
    (lookup_joins) are allowed and up to max_rows raw documents are read."""
    if database.backend != "osagg":
        with database.get_raw_connection(catalog=None, schema=None) as conn:
            yield conn
        return
    conn = _connection(database, extract, max_rows, agent_query=not extract)
    try:
        yield conn
    finally:
        conn.close()


def _connection(database: Any, extract: bool, max_rows: int = 0, agent_query: bool = False) -> Any:
    """osagg DB-API connection of a Superset database. An agent's query (`agent_query`) that
    cannot be pushed down may read at most agent.osagg_max_scan_rows raw documents (or the
    connection's own cap if lower): osagg counts them first and refuses at once, with the reason,
    instead of reading them for minutes."""
    import osagg
    from osagg.sqla import OpenSearchAggDialect
    from sqlalchemy.engine.url import make_url

    _args, kwargs = OpenSearchAggDialect().create_connect_args(make_url(database.sqlalchemy_uri_decrypted))
    extra = database.get_extra() or {}
    kwargs.update((extra.get("engine_params") or {}).get("connect_args") or {})
    if extract:
        kwargs.update(lookup_joins=True, max_rows=0,
                      max_scan_rows=max(int(kwargs.get("max_scan_rows", 500_000)), max_rows + 1))
    elif agent_query:
        from supagent import settings

        cap = int(settings.get("agent.osagg_max_scan_rows") or 0)
        if cap > 0:
            kwargs["max_scan_rows"] = min(int(kwargs.get("max_scan_rows", 500_000)), cap)
    return osagg.connect(**kwargs)


TABLE_ERROR = re.compile(r'Table "[^"]*" does not exist|metric "[^"]*" does not exist|JOIN on an OpenSearch table|'
                         r"JOIN with a raw OpenSearch table|is not an OpenSearch index|Catalog Error: Table with name")


def unknown_tables(database: Any, sql: str) -> str:
    """After a failed OpenSearch (osagg) or Prometheus (promagg) query: the tables of the SQL that
    this database does not have, with the closest names ("" when they all exist, or when it
    cannot be told). A JOIN with a name that is no index fails with "JOIN not supported", and a
    wrong name with "does not exist": this says which name to fix, and with what."""
    import difflib

    import sqlglot
    from sqlglot import exp

    if getattr(database, "backend", None) not in ("osagg", "promagg"):
        return ""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        names = list(dict.fromkeys(t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes))
        if not names:
            return ""
        conn = _connection(database, extract=False) if database.backend == "osagg" else _promagg_connection(database)
        try:
            missing = [n for n in names if conn.table_meta(n) is None]
            known = [str(k) for k in conn.list_tables()] if missing else []
        finally:
            conn.close()
    except Exception:  # pylint: disable=broad-except   (a hint only)
        return ""
    parts = []
    for name in missing:
        low = name.lower()
        close = [k for k in known if low in k.lower()][:3]
        close += [k for k in difflib.get_close_matches(name, known, n=3, cutoff=0.6) if k not in close]
        parts.append(f'"{name}"' + (" (closest: " + ", ".join(f'"{k}"' for k in close[:3]) + ")" if close else ""))
    if not parts:
        return ""
    return (f"No table {', '.join(parts)} in database {database.database_name!r}: use the exact index or metric "
            "names (describe_data lists them).")


def _run(database: Any, sql: str, max_rows: int, extract: bool) -> tuple[list[str], list[tuple], bool]:
    from superset.extensions import db, security_manager

    limited, _ = _check_select(sql, max_rows)
    security_manager.raise_for_access(database=database, sql=limited, schema="default")
    with _db_connection(database, extract, max_rows) as conn:
        db.session.commit()        # no connection of Superset's own pool is held while the query runs
        cur = conn.cursor()
        cur.execute(limited)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchall()
    truncated = len(rows) > max_rows
    return columns, rows[:max_rows], truncated


def _cleanup() -> None:
    os.makedirs(_export_dir(), exist_ok=True)
    limit = time.time() - EXPORT_KEEP_DAYS * 86400
    for name in os.listdir(_export_dir()):
        path = os.path.join(_export_dir(), name)
        if os.path.isfile(path) and not name.startswith(".") and os.path.getmtime(path) < limit:
            os.remove(path)


def _file_name(name: str | None, default: str, ext: str) -> str:
    """<name>-<id>.<ext>: the 6-character id is what the model passes on (send_email)."""
    import secrets

    base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name or default)).strip("._") or default
    base = re.sub(r"\.(xlsx|png|csv)$", "", base, flags=re.I)[:40].strip("._-") or default
    return f"{base}-{secrets.token_hex(3)}.{ext}"


def _file_id(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].rsplit("-", 1)[-1]


def _public(path: str) -> str | None:
    return f"{EXPORT_BASE_URL}/{os.path.basename(path)}" if EXPORT_BASE_URL else None


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def _write_xlsx(path: str, columns: list[str], rows: list[tuple], info: dict[str, Any]) -> None:
    import xlsxwriter

    wb = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_numbers": False,
                                    "strings_to_urls": False, "strings_to_formulas": False})
    head = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    ts_fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    day_fmt = wb.add_format({"num_format": "yyyy-mm-dd"})
    widths = [min(max(len(c), 8), 60) for c in columns]
    for row in rows[:200]:
        for j, v in enumerate(row):
            widths[j] = min(max(widths[j], len(str(v)) if v is not None else 0), 60)
    sheets = max(1, math.ceil(len(rows) / XLSX_SHEET_ROWS))
    for s in range(sheets):
        ws = wb.add_worksheet("data" if s == 0 else f"data ({s + 1})")
        for j, c in enumerate(columns):
            ws.set_column(j, j, widths[j] + 2)
            ws.write_string(0, j, c, head)
        ws.freeze_panes(1, 0)
        chunk = rows[s * XLSX_SHEET_ROWS:(s + 1) * XLSX_SHEET_ROWS]
        for i, row in enumerate(chunk, start=1):
            for j, v in enumerate(row):
                if v is None:
                    continue
                if isinstance(v, bool):
                    ws.write_boolean(i, j, v)
                elif isinstance(v, dt.datetime):
                    ws.write_datetime(i, j, v.replace(tzinfo=None), ts_fmt)
                elif isinstance(v, dt.date):
                    ws.write_datetime(i, j, dt.datetime.combine(v, dt.time()), day_fmt)
                elif isinstance(v, (int, float, decimal.Decimal)) and math.isfinite(float(v)):
                    ws.write_number(i, j, float(v))
                else:
                    ws.write_string(i, j, str(v)[:32767])
        ws.autofilter(0, 0, max(len(chunk), 1), max(len(columns) - 1, 0))
    about = wb.add_worksheet("query")
    about.set_column(0, 0, 16)
    about.set_column(1, 1, 110)
    for i, (k, v) in enumerate(info.items()):
        about.write_string(i, 0, k, head)
        about.write_string(i, 1, str(v))
    wb.close()


def _export(sql: str, database: str | int | None, file_name: str | None, title: str | None,
            max_rows: int | None, user: Any) -> dict[str, Any]:
    limit = _export_max_rows()
    cap = min(max_rows or limit, limit)
    db_obj = _database(database, sql=sql)
    t0 = time.time()
    try:
        columns, rows, truncated = _run(db_obj, sql, cap, extract=True)
    except ToolError:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        hint = unknown_tables(db_obj, sql) if TABLE_ERROR.search(str(ex)) else ""
        if not hint:
            raise
        raise ToolError(f"{hint} ({type(ex).__name__}: {str(ex)[:300]})") from ex
    own = _check_select(sql, cap)[1]
    by_sql = 0 < own <= cap and len(rows) == own          # the SQL's own LIMIT was reached: more rows may match
    _cleanup()
    path = os.path.join(_export_dir(), _file_name(file_name, "extract", "xlsx"))
    _write_xlsx(path, columns, rows, {
        "title": title or "", "generated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "by": getattr(user, "username", ""), "database": db_obj.database_name, "rows": len(rows),
        "truncated": f"yes: first {cap:,} rows only" if truncated else
                     (f"maybe: the LIMIT {own:,} of the SQL was reached" if by_sql else "no"), "SQL": sql})
    res = {"id": _file_id(path), "path": path, "url": _public(path), "rows": len(rows),
           "columns": columns, "truncated": truncated, "bytes": os.path.getsize(path),
           "seconds": round(time.time() - t0, 1),
           "to_email_it": f'send_email(..., attach_paths=["{_file_id(path)}"])'}
    if by_sql:
        res["limited_by_sql"] = own
        res["note"] = (f"The SQL's own LIMIT {own:,} stopped this extract at {own:,} rows: more rows may match. "
                       f"Unless the user asked for the first {own:,} rows, run export_excel again without the "
                       f"LIMIT (an extract holds up to {cap:,} rows and says when it is cut).")
    return res


@mcp.tool
def export_excel(sql: str, database: str | int | None = None, file_name: str | None = None,
                 title: str | None = None, max_rows: int | None = None,
                 email_to: list[str] | None = None) -> dict:
    """Extract the rows of a SELECT to an Excel file (.xlsx) on the server; optionally e-mail it.

    An extract holds every matching row: no LIMIT unless the user asks for the first N (the
    file stops at max_rows, EXPORT_MAX_ROWS, and says so). For extracts only, a row list may
    join ONE big index with small ones (each at most join_max_keys matching documents), e.g.
    failed jobs with their application's TEAM, with the exact index names:
    SELECT a."@timestamp_date", a."APPLICATION", b."TEAM" FROM "<jobs index>" a
    JOIN "<applications index>" b ON a."APPLICATION" = b."APPLICATION" WHERE ... ORDER BY 1 DESC.
    Put the conditions and the time range in WHERE. Returns the file path, the row count and
    whether the rows were cut."""
    try:
        with _as_user() as (app, user):
            res = _export(sql, database, file_name, title, max_rows, user)
            if email_to:
                res["email"] = _send(app, email_to, f"Extract: {title or os.path.basename(res['path'])}",
                                     f"{title or 'Extract'}: {res['rows']:,} rows (Excel file attached).",
                                     attachments=[res["path"]])
            return res
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


# --------------------------------------------------------------------------- #
# Chart images, e-mails
# --------------------------------------------------------------------------- #
def _screenshot(chart_id: int | None, explore_url: str | None, user: Any, width: int,
                height: int, dashboard_id: int | None = None) -> tuple[bytes, str]:
    from urllib.parse import parse_qs, urlparse

    from superset.extensions import db, security_manager
    from superset.models.slice import Slice
    from superset.utils.screenshots import ChartScreenshot, DashboardScreenshot
    from superset.utils.urls import get_url_path

    if dashboard_id is not None:
        from superset.models.dashboard import Dashboard

        dash = db.session.get(Dashboard, int(dashboard_id))
        if dash is None:
            raise ToolError(f"dashboard {dashboard_id} not found")
        security_manager.raise_for_access(dashboard=dash)
        url = get_url_path("Superset.dashboard", dashboard_id_or_slug=dash.uuid or dash.id)
        png = DashboardScreenshot(url, dash.digest, window_size=(width, height)).get_screenshot(user=user)
        if not png:
            raise ToolError("no screenshot: check Chromium and WEBDRIVER_* on this host")
        return _trim_bottom(png), f"dashboard-{dashboard_id}"
    if chart_id is not None:
        chart = db.session.get(Slice, int(chart_id))
        if chart is None:
            raise ToolError(f"chart {chart_id} not found")
        security_manager.raise_for_access(chart=chart)
        url = get_url_path("ExploreView.root", form_data=json.dumps({"slice_id": int(chart_id)}))
        label = f"chart-{chart_id}"
    elif explore_url:
        q = parse_qs(urlparse(explore_url).query)
        keep = {k: v[0] for k, v in q.items() if k in ("form_data_key", "slice_id", "permalink_key")}
        if not keep:
            raise ToolError("explore_url must contain form_data_key, slice_id or permalink_key")
        url = get_url_path("ExploreView.root", **keep)
        label = "chart"
    else:
        raise ToolError("give chart_id or explore_url")
    png = ChartScreenshot(url, None, window_size=(width, height)).get_screenshot(user=user)
    if not png:
        raise ToolError("no screenshot: check Chromium and WEBDRIVER_* on this host")
    return png, label


def _trim_bottom(png: bytes, margin: int = 24) -> bytes:
    """A dashboard shorter than the browser window: the empty background below it cut off."""
    import io

    try:
        from PIL import Image, ImageChops
    except ImportError:
        return png
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        bg = Image.new("RGB", im.size, im.getpixel((im.width - 1, im.height - 1)))
        box = ImageChops.difference(im, bg).getbbox()
        if not box or box[3] + margin >= im.height * 0.9:
            return png
        out = io.BytesIO()
        im.crop((0, 0, im.width, box[3] + margin)).save(out, format="PNG")
        return out.getvalue()
    except Exception:  # pylint: disable=broad-except
        return png


@mcp.tool
def chart_image(chart_id: int | None = None, explore_url: str | None = None, dashboard_id: int | None = None,
                width: int | None = None, height: int | None = None) -> dict:
    """Screenshot (PNG) of a saved chart (chart_id), of an explore link (explore_url with
    form_data_key, e.g. from generate_chart with save_chart=false) or of a whole dashboard
    (dashboard_id), as the user sees it in Superset; shown in the chat and usable in e-mails."""
    try:
        with _as_user() as (_app_, user):
            if dashboard_id is not None:
                width, height = width or 1600, height or 2000
            png, label = _screenshot(chart_id, explore_url, user, width or 1400, height or 800, dashboard_id)
            _cleanup()
            path = os.path.join(_export_dir(), _file_name(label, "chart", "png"))
            with open(path, "wb") as fh:
                fh.write(png)
            return {"id": _file_id(path), "path": path, "url": _public(path), "bytes": len(png),
                    "to_email_it": f'send_email(..., image_paths=["{_file_id(path)}"])'}
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


# ---- charts drawn from a query result (SVG -> PNG with the headless Chromium) ------
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00"]   # Okabe-Ito


def _fmt(v: float, compact: bool = False) -> str:
    if compact:
        for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= div:
                return f"{v / div:.3g}{suf}"
    return f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.3g}"


def _ticks(top: float) -> list[float]:
    if top <= 0:
        return [0.0]
    raw = top / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [i * step for i in range(int(top / step) + 2) if i * step <= top * 1.0001 or i == 1]


def _series(columns: list[str], rows: list[tuple]) -> tuple[list[str], dict[str, list[float | None]]]:
    """labels, {series: values}; (label, series, value) with a text series column is pivoted."""
    def num(v: Any) -> float | None:
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    if len(columns) == 3 and rows and isinstance(rows[0][1], str) and num(rows[0][2]) is not None:
        labels = list(dict.fromkeys(str(r[0]) for r in rows))
        names = list(dict.fromkeys(str(r[1]) for r in rows))[:len(PALETTE)]
        data = {n: [None] * len(labels) for n in names}
        pos = {lb: i for i, lb in enumerate(labels)}
        for lb, n, v in rows:
            if str(n) in data:
                data[str(n)][pos[str(lb)]] = num(v)
        return labels, data
    labels = [str(r[0]) for r in rows]
    return labels, {c: [num(r[j]) for r in rows] for j, c in enumerate(columns[1:len(PALETTE) + 1], 1)}


def _svg(kind: str, title: str, columns: list[str], rows: list[tuple], unit: str,
         width: int, height: int) -> str:
    esc = html.escape
    labels, data = _series(columns, rows)
    names = list(data)
    values = [v for vs in data.values() for v in vs if v is not None]
    top = max(values + [0.0]) * 1.08 or 1.0
    ink, muted, grid = "#1F2328", "#57606A", "#E6E8EB"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="Arial, Helvetica, sans-serif"><rect width="100%" height="100%" fill="#FFFFFF"/>',
           f'<text x="24" y="34" font-size="20" font-weight="600" fill="{ink}">{esc(title)}</text>']
    if len(names) > 1:
        x = 24
        for i, n in enumerate(names):
            out.append(f'<rect x="{x}" y="50" width="12" height="12" rx="2" fill="{PALETTE[i]}"/>'
                       f'<text x="{x + 18}" y="61" font-size="13" fill="{muted}">{esc(n)}</text>')
            x += 36 + 8 * len(n)
    y0 = 78
    if kind == "bar":
        n = max(len(labels), 1)
        left = min(34 + 9 * max((len(lb[:48]) for lb in labels), default=4), 420)
        right, bottom = 110, 36
        band = (height - y0 - bottom) / n
        bar = max(2.0, min(26.0, band / max(len(names), 1) - 4))
        scale = (width - left - right) / top
        for t in _ticks(top):
            x = left + t * scale
            out.append(f'<line x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{height - bottom}" stroke="{grid}"/>'
                       f'<text x="{x:.1f}" y="{height - bottom + 18}" font-size="12" fill="{muted}" '
                       f'text-anchor="middle">{_fmt(t, True)}</text>')
        for i, lb in enumerate(labels):
            yc = y0 + band * i + band / 2
            out.append(f'<text x="{left - 10}" y="{yc + 4:.1f}" font-size="13" fill="{ink}" '
                       f'text-anchor="end">{esc(lb[:48])}</text>')
            for k, nm in enumerate(names):
                v = data[nm][i]
                if v is None:
                    continue
                yb = yc - (bar + 4) * len(names) / 2 + k * (bar + 4) + 2
                w = max(v * scale, 1)
                out.append(f'<rect x="{left}" y="{yb:.1f}" width="{w:.1f}" height="{bar:.1f}" rx="3" '
                           f'fill="{PALETTE[k]}"/>')
                out.append(f'<text x="{left + w + 6:.1f}" y="{yb + bar / 2 + 4:.1f}" font-size="12" '
                           f'fill="{ink}">{_fmt(v)}{(" " + esc(unit)) if unit else ""}</text>')
    else:
        left, right, bottom = 70, 30, 48
        n = max(len(labels), 2)
        xs = [left + (width - left - right) * i / (n - 1) for i in range(len(labels))]
        scale = (height - y0 - bottom) / top
        for t in _ticks(top):
            y = height - bottom - t * scale
            out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="{grid}"/>'
                       f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="12" fill="{muted}" '
                       f'text-anchor="end">{_fmt(t, True)}</text>')
        every = max(1, math.ceil(len(labels) / 10))
        for i in range(0, len(labels), every):
            out.append(f'<text x="{xs[i]:.1f}" y="{height - bottom + 20}" font-size="12" fill="{muted}" '
                       f'text-anchor="middle">{esc(labels[i][:19])}</text>')
        for k, nm in enumerate(names):
            pts = [f"{xs[i]:.1f},{height - bottom - v * scale:.1f}" for i, v in enumerate(data[nm]) if v is not None]
            if pts:
                out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{PALETTE[k]}" '
                           f'stroke-width="2" stroke-linejoin="round"/>')
        if unit:
            out.append(f'<text x="{left}" y="{y0 - 6}" font-size="12" fill="{muted}">{esc(unit)}</text>')
    out.append("</svg>")
    return "".join(out)


def chromium_binary() -> str | None:
    """The headless Chromium chart images are drawn with (CHROMIUM_BIN, else the PATH)."""
    import shutil

    return os.environ.get("CHROMIUM_BIN") or next(
        (b for b in (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome")) if b), None)


def screenshots_possible() -> bool:
    """Superset's webdriver for screenshots of saved charts and dashboards: a driver on the PATH."""
    import shutil

    return bool(shutil.which("chromedriver") or shutil.which("geckodriver"))


def _png(svg: str, width: int, height: int) -> bytes:
    import subprocess
    import tempfile

    binary = chromium_binary()
    if binary is None:
        raise ToolError("no Chromium on this host (CHROMIUM_BIN or PATH): needed for chart images")
    with tempfile.TemporaryDirectory() as tmp:
        page, png = os.path.join(tmp, "chart.html"), os.path.join(tmp, "chart.png")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(f'<html><body style="margin:0;background:#fff">{svg}</body></html>')
        subprocess.run([binary, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                        f"--screenshot={png}", f"--window-size={width},{height}", "file://" + page],
                       capture_output=True, timeout=120, check=False)
        if not os.path.exists(png):
            raise ToolError("Chromium made no image")
        with open(png, "rb") as fh:
            return fh.read()


def _chart_png(spec: dict[str, Any], database: str | int | None) -> tuple[bytes, int]:
    kind = "line" if str(spec.get("kind", "bar")).lower().startswith("line") else "bar"
    columns, rows, _t = _run(_database(spec.get("database", database), sql=spec["sql"]), spec["sql"], 500, extract=False)
    if len(columns) < 2 or not rows:
        raise ToolError("the chart query must return rows with a label column and at least one value column")
    width = int(spec.get("width", 1200))
    height = int(spec.get("height", max(420, 110 + 34 * len(rows)) if kind == "bar" else 560))
    svg = _svg(kind, str(spec.get("title", "")), columns, rows, str(spec.get("unit", "")), width, min(height, 2400))
    return _png(svg, width, min(height, 2400)), len(rows)


@mcp.tool
def chart_from_sql(sql: str, kind: Literal["bar", "line"] = "bar", title: str = "", unit: str = "",
                   database: str | int | None = None) -> dict:
    """An IMAGE (PNG) drawn from the result of a SELECT: it does NOT create a chart in Superset (for
    a saved Superset chart use generate_chart). First column: the
    labels (categories, or times for a line); each next column: one series of numbers; a
    result (label, series, value) is pivoted into one line or bar per series. kind "bar" =
    ranking (horizontal bars, give an ORDER BY), "line" = time series. Saved on the server;
    for an e-mail give the same {sql, kind, title} in send_email(chart_sqls=[...])."""
    try:
        with _as_user():
            png, n = _chart_png({"sql": sql, "kind": kind, "title": title, "unit": unit}, database)
            _cleanup()
            path = os.path.join(_export_dir(), _file_name(title or "chart", "chart", "png"))
            with open(path, "wb") as fh:
                fh.write(png)
            return {"id": _file_id(path), "path": path, "url": _public(path), "rows": n,
                    "bytes": len(png), "to_email_it": f'send_email(..., image_paths=["{_file_id(path)}"])'}
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


def _export_file(path: str, suffixes: tuple[str, ...]) -> str:
    """A file made by these tools (inside EXPORT_DIR), by path, name or 6-character id; else the
    same file of an earlier answer of this user, kept in Superset's database (made by another
    worker or web server)."""
    root = os.path.realpath(_export_dir())
    name = os.path.basename(str(path).strip())
    full = os.path.realpath(os.path.join(root, name))
    if not (full.startswith(root + os.sep) and full.lower().endswith(suffixes) and os.path.isfile(full)):
        ident = _file_id(name) if "-" in name else os.path.splitext(name)[0]
        hits = [f for f in (os.listdir(root) if os.path.isdir(root) else [])
                if f.lower().endswith(suffixes) and not f.startswith(".")
                and re.fullmatch(r"[0-9a-f]{6}", ident or "") and _file_id(f) == ident]
        if len(hits) == 1:
            return os.path.join(root, hits[0])
        kept = _kept_file(name, ident, suffixes, root) if not hits else None
        if kept is None:
            raise ToolError(f"{path}: not a {'/'.join(suffixes)} file made by these tools "
                            "(give the id or the path returned by the tool)")
        full = kept
    return full


def _kept_file(name: str, ident: str, suffixes: tuple[str, ...], root: str) -> str | None:
    """A file of an earlier answer of the user who asks (supagent_file), written into EXPORT_DIR."""
    from flask import g
    from sqlalchemy import or_
    from superset import db

    from supagent.models import Conversation, File, Message

    uid = getattr(getattr(g, "user", None), "id", None)
    if uid is None or not name:
        return None
    q = (db.session.query(File).join(Message, File.message_id == Message.id)
         .join(Conversation, Message.conversation_id == Conversation.id).filter(Conversation.user_id == uid))
    if re.fullmatch(r"[0-9a-f]{6}", ident or ""):
        q = q.filter(or_(File.name == name, File.name.like(f"%-{ident}.%")))
    else:
        q = q.filter(File.name == name)
    f = next((x for x in q.order_by(File.id.desc()).limit(10)
              if (x.name or "").lower().endswith(suffixes) and x.data is not None), None)
    if f is None:
        return None
    os.makedirs(root, exist_ok=True)
    full = os.path.join(root, os.path.basename(f.name))
    with open(full, "wb") as fh:
        fh.write(f.data)
    return full


def _check_recipients(to: list[str]) -> list[str]:
    allowed = [d.strip().lower().lstrip("@") for d in (_setting("tools.email_allowed_domains", [])
                                                         or os.environ.get("EMAIL_ALLOWED_DOMAINS", "").split(","))
               if d.strip()]
    out = []
    for addr in to:
        addr = addr.strip()
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", addr):
            raise ToolError(f"not an e-mail address: {addr!r}")
        if allowed and addr.rsplit("@", 1)[1].lower() not in allowed:
            raise ToolError(f"{addr}: only these domains are allowed: {', '.join(allowed)}")
        out.append(addr)
    if not out:
        raise ToolError("no recipient")
    return out


def _html_table(columns: list[str], rows: list[tuple]) -> str:
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">'
                 f"{html.escape(c)}</th>" for c in columns)

    def cell(v: Any) -> str:
        if isinstance(v, float):
            v = f"{v:,.2f}"
        elif isinstance(v, int) and not isinstance(v, bool):
            v = f"{v:,}"
        align = "right" if isinstance(v, str) and re.fullmatch(r"-?[\d,]+(\.\d+)?", v) else "left"
        return (f'<td style="padding:3px 8px;border-bottom:1px solid #ddd;text-align:{align}">'
                f'{html.escape("" if v is None else str(v))}</td>')

    body = "".join("<tr>" + "".join(cell(v) for v in r) + "</tr>" for r in rows)
    return (f'<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px">'
            f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")


def _short_lines(text: str, width: int = 900) -> str:
    """SMTP forbids lines over 998 characters (Superset sends the HTML unencoded)."""
    text = re.sub(r"(</(?:tr|p|li|ul|ol|table|thead|tbody|div|h\d)>)", r"\1\n", text)
    out = []
    for line in text.split("\n"):
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > 0 else width
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return "\n".join(out)


def _style_tables(text: str) -> str:
    """Inline styles for Markdown tables (e-mail clients ignore style sheets)."""
    text = text.replace("<table>", '<table style="border-collapse:collapse;font-size:13px">')
    text = re.sub(r"<th([^>]*)>", r'<th\1 style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">', text)

    def td(m: re.Match) -> str:
        num = re.fullmatch(r"\s*-?[\d.,\s]+%?\s*", re.sub(r"<[^>]+>", "", m.group(2)))
        align = "right" if num else "left"
        return (f'<td{m.group(1)} style="padding:3px 8px;border-bottom:1px solid #ddd;'
                f'text-align:{align}">{m.group(2)}</td>')

    return re.sub(r"<td([^>]*)>(.*?)</td>", td, text, flags=re.S)


def _place_images(text: str, cids: list[str]) -> tuple[str, list[str]]:
    """The images the text refers to (Markdown ![...](...)) become the attached ones, in
    order; references beyond them are dropped; images not referenced are returned."""
    todo = list(cids)

    def swap(m: re.Match) -> str:
        return (f'<img src="cid:{todo.pop(0)}" alt="{html.escape(m.group(1) or "chart")}" '
                f'style="max-width:100%">') if todo else ""

    text = re.sub(r'<img[^>]*?alt="([^"]*)"[^>]*>|<img[^>]*>', swap, text)
    return text, todo


def _send(app: Any, to: list[str], subject: str, body_html: str, images: dict[str, bytes] | None = None,
          attachments: list[str] | None = None) -> dict[str, Any]:
    from superset.utils.core import send_email_smtp

    recipients = _check_recipients(to)
    limit = float(os.environ.get("EMAIL_MAX_ATTACH_MB", "15")) * 1024 * 1024
    files, links = [], []
    for path in attachments or []:
        if os.path.getsize(path) <= limit:
            files.append(path)
        else:
            links.append(_public(path) or path)
    if links:
        body_html += "<p>Files too big to attach: " + ", ".join(html.escape(x) for x in links) + "</p>"
    page = (f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">{body_html}'
            f'<p style="color:#888;font-size:11px">Sent by the Superset assistant.</p></div>')
    page = _short_lines(page)
    send_email_smtp(", ".join(recipients), subject, page, app.config, files=files or None,
                    images=images or None)
    return {"sent_to": recipients, "attached": [os.path.basename(f) for f in files],
            "images": len(images or {})}


@mcp.tool
def send_email(to: list[str], subject: str, body_markdown: str = "", sql: str | None = None,
               database: str | int | None = None, max_table_rows: int = 100,
               chart_sqls: list[dict] | None = None, image_paths: list[str] | None = None,
               chart_ids: list[int] | None = None, explore_urls: list[str] | None = None,
               excel_sql: str | None = None, excel_name: str | None = None,
               attach_paths: list[str] | None = None) -> dict:
    """Send an e-mail now (not scheduled): text (Markdown), optionally the result of a SELECT as
    a table (sql, up to max_table_rows rows), chart images drawn from SELECTs (chart_sqls:
    [{"sql": ..., "kind": "bar"|"line", "title": ...}], see chart_from_sql), images already
    made (image_paths: paths returned by chart_from_sql / chart_image), saved Superset
    charts (chart_ids) / explore links, an Excel extract (excel_sql) or files already made
    (attach_paths: paths returned by export_excel). Use create_report for a recurring e-mail."""
    try:
        import markdown

        with _as_user() as (app, user):
            text_html = markdown.markdown(body_markdown or "", extensions=["tables"])
            parts: list[str] = []
            info: dict[str, Any] = {}
            if sql:
                columns, rows, truncated = _run(_database(database, sql=sql), sql, max(1, min(max_table_rows, 1000)),
                                                extract=False)
                parts.append(_html_table(columns, rows))
                if truncated:
                    parts.append(f"<p><i>First {len(rows):,} rows.</i></p>")
                info["table_rows"] = len(rows)
            images: dict[str, bytes] = {}
            for spec in chart_sqls or []:
                png, _rows = _chart_png(spec if isinstance(spec, dict) else {"sql": str(spec)}, database)
                images[f"chart{len(images) + 1}"] = png
            for path in image_paths or []:
                with open(_export_file(path, (".png",)), "rb") as fh:
                    images[f"chart{len(images) + 1}"] = fh.read()
            for cid, url in [(c, None) for c in chart_ids or []] + [(None, u) for u in explore_urls or []]:
                png, _label = _screenshot(cid, url, user, 1400, 800)
                images[f"chart{len(images) + 1}"] = png
            text_html, rest = _place_images(_style_tables(text_html), list(images))
            parts = [text_html] + parts + [f'<p><img src="cid:{c}" style="max-width:100%"></p>' for c in rest]
            attachments = [_export_file(p, (".xlsx", ".png", ".csv")) for p in attach_paths or []]
            if excel_sql and any(str(a).endswith(".xlsx") for a in attachments):
                info["excel"] = "not made again: an Excel file made before is attached (attach_paths)"
            elif excel_sql:
                res = _export(excel_sql, database, excel_name, excel_name, None, user)
                attachments.append(res["path"])
                info["excel"] = {k: res[k] for k in ("path", "rows", "truncated", "note") if k in res}
            info.update(_send(app, to, subject, "".join(parts), images, attachments))
            return info
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


# --------------------------------------------------------------------------- #
# Data dictionary: catalog.yaml + cached profile + Superset datasets
# --------------------------------------------------------------------------- #
def _catalog() -> dict[str, Any]:
    from supagent.knowledge.catalog import load_catalog

    return load_catalog()


def _profile(conn: Any, index: str, time_field: str | None) -> dict[str, Any]:
    """Doc count, time range and, on a sample, top values of low-cardinality keyword fields and
    ranges of numeric fields; cached for a day (one small request per index and day)."""
    cache = os.path.join(_export_dir(), ".profile.json")
    try:
        with open(cache, encoding="utf-8") as fh:
            all_profiles = json.load(fh)
    except (OSError, ValueError):
        all_profiles = {}
    hit = all_profiles.get(index)
    if hit and time.time() - hit.get("at", 0) < PROFILE_TTL:
        return hit
    meta = conn.table_meta(index)
    if meta is None:
        return {}
    aggs: dict[str, Any] = {}
    for f in meta.fields.values():
        if f.virtual or f.agg_field is None or f.name == "_id":
            continue
        if f.sql_type == "VARCHAR" and not re.search(r"(^|_)ID(_|$)|UUID", f.name, re.I):
            aggs[f"t:{f.name}"] = {"terms": {"field": f.agg_field, "size": 31}}
        elif f.is_numeric:
            aggs[f"s:{f.name}"] = {"stats": {"field": f.agg_field}}
    body: dict[str, Any] = {"size": 0, "track_total_hits": True, "terminate_after": 200_000, "aggs": aggs}
    tf = meta.resolve(time_field) if time_field else None
    res = conn.transport.search(index, body)
    out: dict[str, Any] = {"at": time.time(), "fields": {}}
    for key, val in (res.get("aggregations") or {}).items():
        kind, name = key.split(":", 1)
        if kind == "t":
            buckets = val.get("buckets", [])
            if buckets and len(buckets) <= 30:
                out["fields"][name] = {"values": [b["key"] for b in buckets]}
        elif val.get("count"):
            out["fields"][name] = {"min": val.get("min"), "max": val.get("max"), "avg": val.get("avg")}
    if tf is not None:
        full = conn.transport.search(index, {"size": 0, "track_total_hits": True, "aggs": {
            "lo": {"min": {"field": tf.agg_field}}, "hi": {"max": {"field": tf.agg_field}}}})
        out["docs"] = full["hits"]["total"]["value"]
        out["time_range"] = [
            f"{dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).astimezone(conn.tz):%Y-%m-%d %H:%M}"
            if v is not None else None
            for v in (full["aggregations"]["lo"].get("value"), full["aggregations"]["hi"].get("value"))]
    all_profiles[index] = out
    os.makedirs(_export_dir(), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(all_profiles, fh, default=str)
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


@mcp.tool
def describe_data(topic: str | None = None, index: str | None = None) -> str:
    """What the data contains: OpenSearch indices (meaning of each field, synonyms, typical
    values, time range, how the indices relate, saved metrics) and Prometheus / Mimir metrics
    (one SQL table per metric: labels, units, how labels match index fields, example SQL,
    health checks). Call it before writing SQL or a chart for a question; `topic` (words of the
    question) keeps the relevant fields and metrics, `index` one index or metric."""
    try:
        with _as_user():
            from superset.connectors.sqla.models import SqlaTable
            from superset.extensions import db

            from supagent.knowledge.describe import describe

            learned = describe(topic, index)           # the dictionary learned every day
            if learned is not None:
                return learned
            cat = _catalog()
            indices: dict[str, Any] = cat.get("indices") or {}
            unknown_note = ""
            if index:
                matched = {k: v for k, v in indices.items() if k == index}
                is_metric = False
                if not matched:
                    try:
                        mdb = _metrics_database(None)
                        mconn = _promagg_connection(mdb)
                        try:
                            is_metric = index in mconn.list_tables()
                        finally:
                            mconn.close()
                    except Exception:  # pylint: disable=broad-except
                        is_metric = False
                if matched or is_metric:
                    indices = matched
                else:              # a guessed name: answer from the topic instead
                    unknown_note = (f"(there is no index or metric named {index!r}; below: what matches "
                                    "the topic)")
                    topic = f"{topic or ''} {index.replace('_', ' ')}"
                    index = None
            database = _database(None)
            conn = _connection(database, extract=False)
            words = _tokens(topic or "")
            out: list[str] = [unknown_note] if unknown_note else []
            glossary = cat.get("glossary") or {}
            if glossary:
                out.append("Business terms:")
                out += [f"- {k}: {v}" for k, v in glossary.items()]
            try:
                for name, spec in indices.items():
                    spec = spec or {}
                    prof = _profile(conn, name, spec.get("time_field"))
                    meta = conn.table_meta(name)
                    ds = db.session.query(SqlaTable).filter_by(table_name=name, database_id=database.id).first()
                    out.append(f"\nIndex {name}: {spec.get('description', '').strip()}")
                    out.append(f"  SQL: FROM \"{name}\" on database id {database.id}"
                               + (f"; Superset dataset id {ds.id} (charts: dataset_id={ds.id})" if ds else ""))
                    if prof.get("docs") is not None:
                        out.append(f"  {prof['docs']:,} documents; time field {spec.get('time_field')} "
                                   f"from {prof['time_range'][0]} to {prof['time_range'][1]}")
                    for rel in spec.get("relationships") or []:
                        keys = rel.get("keys") or rel.get(True) or {}      # an unquoted "on" is YAML's True
                        on = " AND ".join(f'{name}."{a}" = {rel["to"]}."{b}"' for a, b in keys.items())
                        out.append(f"  relationship: {on} ({rel.get('description', '')})")
                    fields = spec.get("fields") or {}
                    rows = []
                    for fname, f in (meta.fields.items() if meta else []):
                        if fname == "_id":
                            continue
                        d = fields.get(fname) or {}
                        col = next((c for c in (ds.columns if ds else []) if c.column_name == fname), None)
                        desc = d.get("description") or (col.description if col else "") or ""
                        syn = [str(x) for x in d.get("synonyms") or []]
                        vals = (prof.get("fields") or {}).get(fname, {})
                        text = f"{fname} {desc} {' '.join(syn)} {' '.join(map(str, vals.get('values', [])))}"
                        score = len(words & _tokens(text)) if words else 1
                        line = f'  - "{fname}" ({f.sql_type}{", " + d["unit"] if d.get("unit") else ""})'
                        line += f": {desc}" if desc else ""
                        if syn:
                            line += f" [also: {', '.join(syn)}]"
                        if vals.get("values"):
                            line += f" values: {', '.join(map(str, vals['values'][:30]))}"
                        elif vals.get("min") is not None:
                            line += f" range {vals['min']:.4g} .. {vals['max']:.4g}, avg {vals['avg']:.4g}"
                        rows.append((score, line))
                    keep = [ln for sc, ln in sorted(rows, key=lambda r: -r[0])
                            if sc > 0][:40] if words else [ln for _s, ln in rows]
                    out.append("  fields:" if keep else "  fields: (none matches the topic; call "
                               "describe_data without topic)")
                    out += keep
                    if ds is not None and ds.metrics:
                        out.append("  saved metrics (use {\"name\": <metric>, \"saved_metric\": true} in charts):")
                        out += [f"  - {m.metric_name}: {m.description or m.expression}" for m in ds.metrics]
            finally:
                conn.close()
            out += _describe_metrics(cat, words, index)
            return "\n".join(out)[:16000]
    except ToolError as ex:
        return f"error: {ex}"
    except Exception as ex:  # pylint: disable=broad-except
        return f"error: {type(ex).__name__}: {str(ex)[:1500]}"


@mcp.tool
def search_knowledge(query: str, kind: str | None = None, limit: int = 8) -> dict:
    """Search what is known about the data and how the team works: metrics and indices (what
    they mean, labels, fields), the catalog's guides (documentation, runbooks) and the users' notes
    (meetings, decisions), rules, glossary and formulas (calculated fields), answers that worked
    before, team and personal preferences, documents and sites. `kind`: metric, index, guide, note
    (the users' notes and the catalog's guides), rule, glossary, formula, recipe, memory or doc
    (empty: all)."""
    try:
        with _as_user():
            from supagent.knowledge.notes import KIND as USERS_NOTES
            from supagent.knowledge.search import search

            kinds = (kind,) if kind else None
            if kind in ("note", "notes", USERS_NOTES):      # "note": a meeting's note as well as the catalog's guides
                kinds = ("guide", USERS_NOTES)
            elif kind in ("guide", "guides"):
                kinds = ("guide",)
            found = search(query, k=max(1, min(int(limit or 8), 20)), kinds=kinds)
            return {"query": query, "results": [{**f, "text": (f["text"] or "")[:1200]} for f in found]}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


def _find_board(key: Any) -> Any:
    """A dashboard by id or title (exact, else the only one whose title has it)."""
    from superset.extensions import db as meta
    from superset.models.dashboard import Dashboard

    if isinstance(key, int) or str(key).strip().isdigit():
        return meta.session.get(Dashboard, int(key))
    title = str(key).strip().strip("'\"").lower()
    rows = meta.session.query(Dashboard).all()
    exact = [d for d in rows if (d.dashboard_title or "").lower() == title]
    part = [d for d in rows if title and title in (d.dashboard_title or "").lower()]
    return (exact or (part if len(part) == 1 else [None]))[0]


def _find_chart(key: Any) -> Any:
    from superset.extensions import db as meta
    from superset.models.slice import Slice

    if isinstance(key, int) or str(key).strip().isdigit():
        return meta.session.get(Slice, int(key))
    name = str(key).strip().strip("'\"").lower()
    rows = meta.session.query(Slice).all()
    exact = [c for c in rows if (c.slice_name or "").lower() == name]
    part = [c for c in rows if name and name in (c.slice_name or "").lower()]
    return (exact or (part if len(part) == 1 else [None]))[0]


@mcp.tool
def chart_anomalies(dashboard: str | int | None = None, chart: str | int | None = None, look_now: bool = False) -> dict:
    """What the nightly look at the team's Superset charts found: for each chart (of a dashboard, one chart, or every
    chart), its last full day against the same weekday of the 4 weeks before, per series (high, low; "stale": its
    data stopped), and what the chart shows (AI-written). `dashboard` / `chart`: an id or a title. `look_now`: look
    again now, as the user (10 charts at most). For "anything unusual on the X dashboard?", "any anomaly in the
    charts?". Charts the user may not see are left out."""
    try:
        with _as_user():
            from supagent import settings
            from supagent.agent import now as agent_now
            from supagent.knowledge import charts as K

            where, slices = "every chart", None
            if dashboard not in (None, ""):
                d = _find_board(dashboard)
                if d is None:
                    return {"error": f"no dashboard {dashboard!r} (list_dashboards gives their titles and ids)"}
                where, slices = f"dashboard {d.dashboard_title} (id {d.id})", list(d.slices or [])
            if chart not in (None, ""):
                c = _find_chart(chart)
                if c is None:
                    return {"error": f"no chart {chart!r} (list_charts gives their names and ids)"}
                where, slices = f"chart {c.slice_name} (id {c.id})", [c]
            if slices is None:
                slices = K.charts_to_scan(int(settings.get("charts.max_charts") or 200))
            scans = K.scans_of([s.id for s in slices])
            out, hidden, looked, now = [], 0, 0, agent_now()
            for s in slices:
                ok, figures = K.may_see(s)
                if not ok:
                    hidden += 1
                    continue
                r = scans.get(s.id)
                if look_now or not figures or r is None or r.scanned_at is None:
                    if looked >= 10:
                        out.append({"chart_id": s.id, "chart": s.slice_name, "status": "not looked at",
                                    "note": "10 charts are looked at now at most: ask for one dashboard or chart"})
                        continue
                    res, when = K.look(s, now), "now, as you"
                    looked += 1
                else:
                    res = {"status": r.status, "reason": r.reason, "findings": r.findings or [], "checked": r.checked,
                           "day": r.day}
                    when = f"the nightly look ({r.scanned_at:%Y-%m-%d %H:%M} UTC)"
                out.append({"chart_id": s.id, "chart": s.slice_name, "status": res["status"],
                            "day": str(res.get("day") or ""), "series_compared": res.get("checked"),
                            "unusual": [K.describe_finding(f) for f in res.get("findings") or []][:6],
                            "note": res.get("reason"), "looked": when,
                            "what_it_shows": (r.understanding if r is not None and r.understanding else None)})
            out.sort(key=lambda c: (-len(c.get("unusual") or []), c["status"] != "stale"))
            return {"where": where, "compared": "each chart's last full day against the same weekday of the 4 weeks "
                    "before (median and median absolute deviation, per series)",
                    "with_anomalies": sum(1 for c in out if c.get("unusual")),
                    "stale": sum(1 for c in out if c["status"] == "stale"), "charts": out[:60],
                    "not_shown": hidden}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def search_notes(words: str = "", since: str | None = None, until: str | None = None, mine: bool = False,
                 limit: int = 8) -> dict:
    """The notes people wrote down (what a meeting decided, a fact the team keeps): the team's and the user's own,
    with every word of `words` (empty: any), of the days `since` to `until` (YYYY-MM-DD: the day a note is about,
    else the day it was written), newest first; `mine`: the user's own only. A note is someone's, not verified:
    say whose it is and of which day, never take it as a rule."""
    try:
        with _as_user():
            from supagent.knowledge import notes as N

            uid, _name, admin = _note_user()
            rows, total = N.listing(uid, words or "", 0, max(1, min(int(limit or 8), 20)), mine=bool(mine),
                                    since=since or None, until=until or None)
            names = N.authors({n.user_id for n in rows if n.user_id})
            return {"total": total, "about": "notes are not verified: give their author and day",
                    "notes": [_note_view(n, uid, admin, names) for n in rows]}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


def _note_user() -> tuple[int | None, str, bool]:
    """(the user's id, their username, admin) inside _as_user()."""
    from flask import g
    from superset.extensions import security_manager

    user = getattr(g, "user", None)
    try:
        from supagent.views import ADMIN_VIEW

        admin = bool(security_manager.is_admin()) or bool(security_manager.can_access("can_write", ADMIN_VIEW))
    except Exception:  # pylint: disable=broad-except
        admin = False
    return getattr(user, "id", None), str(getattr(user, "username", "") or ""), admin


def _note_view(n: Any, me: int | None, admin: bool, names: dict[int, str], chars: int = 2000) -> dict:
    from supagent.knowledge import notes as N

    text = n.text or ""
    return {"id": n.id, "title": n.title, "author": names.get(n.user_id, "?"), "day": N.day_of(n).isoformat(),
            "for": "the team" if n.scope == "team" else "its author only", "tags": n.tags or [],
            "can_change": N.can_change(n, me, admin), "earlier_versions": len(n.versions or []),
            "text": text[:chars] + (" …" if len(text) > chars else "")}


def _notes_changed() -> None:
    """The notes' search pieces follow at once: the next search finds the change."""
    from superset.extensions import db

    try:
        from supagent.knowledge.index import sync

        sync(("note:",))
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()


def _note_to_change(note_id: Any) -> tuple[Any, int | None, str, bool, dict[int, str], str | None]:
    """(the note, the user's id, username, admin, its author's name, why it cannot be changed or None)."""
    from superset.extensions import db

    from supagent.knowledge import notes as N
    from supagent.models import Note

    me, name, admin = _note_user()
    n = db.session.get(Note, int(note_id))
    if n is None or not (n.scope == "team" or n.user_id == me):
        return None, me, name, admin, {}, f"no note {note_id} this user may read (search_notes finds them)"
    names = N.authors({n.user_id})
    if not N.can_change(n, me, admin):
        return n, me, name, admin, names, (f"note {note_id} is {names.get(n.user_id, '?')}'s: only its author, or an "
                                           "admin for a team note, may change it")
    return n, me, name, admin, names, None


@mcp.tool
def read_note(note_id: int) -> dict:
    """One note in full (a team note, or the user's own): its text, author, day, tags, for whom, whether the
    user may change it and how many earlier versions it has. A note is someone's, not verified."""
    try:
        with _as_user():
            from supagent.knowledge import notes as N
            from supagent.models import Note

            me, _name, admin = _note_user()
            n = N.visible(me).filter(Note.id == int(note_id)).first()
            if n is None:
                return {"error": f"no note {note_id} this user may read (search_notes finds them)"}
            return {"note": _note_view(n, me, admin, N.authors({n.user_id}), chars=20000)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def add_note(text: str, title: str | None = None, personal: bool = False, tags: str | None = None,
             day: str | None = None) -> dict:
    """Write a note the user asks for (what a meeting decided, a fact to keep), in their words: for the team,
    or for the user only (personal=true when they say "for me", "my note", "personal", "private"). day: the day
    it is about (a meeting's, YYYY-MM-DD) when said; tags: a few words, comma-separated. Never add a fact the
    user did not give. Returns the saved note: say its title and for whom."""
    from supagent.knowledge import notes as N

    try:
        with _as_user():
            me, name, admin = _note_user()
            if me is None:
                return {"error": "no user: a note is written by a user"}
            n = N.add(me, text, title=title, scope="user" if personal else "team", tags=tags, meeting_on=day or None,
                      source="agent", by=name)
            _notes_changed()
            return {"saved": _note_view(n, me, admin, N.authors({n.user_id}))}
    except N.NoteError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def change_note(note_id: int, text: str | None = None, add_text: str | None = None, title: str | None = None,
                tags: str | None = None, day: str | None = None, personal: bool | None = None,
                undo: bool = False) -> dict:
    """Change a note the user asks to change: add_text adds a paragraph at its end (what is there stays); text
    replaces its whole text; title, tags (comma-separated), day (YYYY-MM-DD; "" removes it), personal (true: its
    author's only; false: the team's) change those; undo=true puts back the version before its last change. Only
    the user's own notes, and a team note for an admin. Its earlier versions are kept. Returns the note before
    and after: say what changed."""
    from supagent.knowledge import notes as N

    try:
        with _as_user():
            n, me, name, admin, names, why = _note_to_change(note_id)
            if why:
                return {"error": why}
            before = _note_view(n, me, admin, names, chars=4000)
            if undo:
                N.undo(n, by=name)
            else:
                values: dict[str, Any] = {}
                if text is not None and str(text).strip():
                    values["text"] = str(text)
                if add_text and str(add_text).strip():
                    values["text"] = f"{str(values.get('text') or n.text or '').rstrip()}\n\n{str(add_text).strip()}"
                if title is not None:
                    values["title"] = title
                if tags is not None:
                    values["tags"] = tags
                if day is not None:
                    values["meeting_on"] = day or None
                if personal is not None:
                    if n.user_id != me:
                        return {"error": "only its author makes a note personal or the team's"}
                    values["scope"] = "user" if personal else "team"
                if not values:
                    return {"error": "nothing to change: give text, add_text, title, tags, day, personal or undo"}
                N.update(n, values, by=name)
            _notes_changed()
            return {"before": before, "after": _note_view(n, me, admin, names, chars=4000)}
    except N.NoteError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def delete_note(note_id: int, confirmed: bool = False) -> dict:
    """Delete a note the user asks to delete. Without confirmed, nothing is deleted: the note is returned to
    show the user (its title, author and day) with the question whether to delete it. Call with confirmed=true
    only in the answer to their yes. Only the user's own notes, and a team note for an admin."""
    from supagent.knowledge import notes as N

    try:
        with _as_user():
            n, me, _name, admin, names, why = _note_to_change(note_id)
            if why:
                return {"error": why}
            shown = _note_view(n, me, admin, names, chars=600)
            if not confirmed:
                return {"to_confirm": shown, "about": "not deleted: ask the user whether to delete this note"}
            N.remove(n)
            _notes_changed()
            return {"deleted": {k: shown[k] for k in ("id", "title", "author", "day", "for")}}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def search_my_chats(query: str, days: int | None = None, limit: int = 6) -> dict:
    """Search the user's own earlier chats (their questions, the beginning of the answers, their dates and
    the tables they read) for a question about something asked before ("as last week", "the number you gave
    me"). The numbers of an old answer are of its date: run the query again for today's numbers."""
    try:
        with _as_user():
            from flask import g

            from supagent.knowledge import pgstore

            if not pgstore.active():
                return {"error": "the chats are searched in the knowledge store only (superset supagent store rebuild)"}
            uid = getattr(getattr(g, "user", None), "id", None)
            found = pgstore.chats(query, uid, k=max(1, min(int(limit or 6), 12)), days=days)
            return {"query": query, "chats": found}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def data_changes(days: int = 7) -> dict:
    """What the daily learning found different in the last days: new or gone metrics, indices,
    fields and labels, changed types or units, big changes of series counts or distinct values."""
    try:
        with _as_user():
            from supagent.knowledge.describe import changes

            rows = changes(days)
            return {"days": days, "changes": rows[:150], "count": len(rows)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


def push_descriptions(labels: bool = False) -> None:
    """catalog.yaml descriptions -> descriptions of the dataset columns (and, with labels, their
    verbose names, which also relabel existing charts)."""
    with _as_user():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db

        for name, spec in (_catalog().get("indices") or {}).items():
            fields = (spec or {}).get("fields") or {}
            for ds in db.session.query(SqlaTable).filter_by(table_name=name).all():
                n = 0
                for col in ds.columns:
                    f = fields.get(col.column_name)
                    if f:
                        col.description = f.get("description") or col.description
                        if labels:
                            col.verbose_name = f.get("label") or col.verbose_name
                        n += 1
                if spec.get("description"):
                    ds.description = spec["description"].strip()
                db.session.commit()
                print(f"dataset {ds.id} ({name}): {n} column descriptions")


# --------------------------------------------------------------------------- #
# Metrics (Prometheus / Mimir through promagg)
# --------------------------------------------------------------------------- #
MAX_PROMQL_RANGE_DAYS = float(os.environ.get("PROMQL_MAX_RANGE_DAYS", "31"))


def _promagg_connection(database: Any) -> Any:
    import promagg
    from promagg.sqla import PromAggDialect
    from sqlalchemy.engine.url import make_url

    _args, kwargs = PromAggDialect().create_connect_args(make_url(database.sqlalchemy_uri_decrypted))
    extra = database.get_extra() or {}
    kwargs.update((extra.get("engine_params") or {}).get("connect_args") or {})
    return promagg.connect(**kwargs)


def _metrics_database(ref: str | int | None) -> Any:
    from superset.extensions import security_manager

    spec = _catalog().get("metrics") or {}
    database = _database(ref if ref not in (None, "") else spec.get("database"), backend="promagg")
    if database.backend != "promagg":
        raise ToolError(f"database {database.database_name!r} is not a Prometheus / Mimir (promagg) database")
    if not security_manager.can_access_database(database):
        raise ToolError(f"no access to database {database.database_name!r}")
    return database


def _now_ms(conn: Any) -> int:
    """Now for the tools' times: the agent's now when an admin pinned it (agent.now, a copy of data that ends
    in the past), else the clock."""
    fixed = str(_setting("agent.now", "") or "").strip()
    if fixed:
        try:
            return conn.zone.utc_ms(dt.datetime.fromisoformat(fixed))
        except Exception:  # pylint: disable=broad-except
            pass
    return int(time.time() * 1000)


def _local_now(conn: Any) -> dt.datetime:
    """Now as a user writes a time (local, no zone): the agent's now when an admin pinned it, else the clock in
    the connection's time zone (osagg, promagg), else the server's."""
    fixed = str(_setting("agent.now", "") or "").strip()
    if fixed:
        try:
            return dt.datetime.fromisoformat(fixed).replace(tzinfo=None)
        except ValueError:
            pass
    tz = getattr(conn, "tz", None) or getattr(getattr(conn, "zone", None), "tz", None)
    try:
        return dt.datetime.now(tz).replace(tzinfo=None) if tz is not None else dt.datetime.now()
    except Exception:  # pylint: disable=broad-except
        return dt.datetime.now()


RAW_COUNTER = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*(?:_total|_count|_sum|_bucket))\b")
OVER_TIME = re.compile(r"\b(rate|irate|increase|delta|idelta|resets|changes|histogram_quantile)\s*\(")


def raw_counter(expr: str) -> str | None:
    """A counter the expression reads as it is (no rate, increase...): its value only grows, so a level, an
    average or a comparison of it says nothing. Its name, else None."""
    names = RAW_COUNTER.findall(expr or "")
    if not names or OVER_TIME.search(expr or ""):
        return None
    return names[0]


def _time_arg(conn: Any, value: str | None, default_ms: int) -> int:
    if value in (None, ""):
        return default_ms
    v = str(value).strip()
    m = re.fullmatch(r"now(?:\s*-\s*(\S+))?", v.lower())
    if m:
        from promagg.timegrid import parse_duration

        return _now_ms(conn) - (parse_duration(m.group(1)) if m.group(1) else 0)
    d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    if d.tzinfo is not None:
        return int(d.timestamp() * 1000)
    return conn.zone.utc_ms(d)


def _series_summary(conn: Any, series: list, max_points: int) -> list[dict]:
    out = []
    for s in series:
        vals = [v for _t, v in s.points if v is not None and not math.isnan(v)]
        item: dict[str, Any] = {"labels": {k: v for k, v in s.labels.items() if k != "__name__"}}
        if vals:
            item.update(min=min(vals), max=max(vals), avg=sum(vals) / len(vals), last=vals[-1],
                        points=len(s.points))
        pts = s.points
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
        item["values"] = [[f"{conn.zone.local(t):%Y-%m-%d %H:%M}", None if math.isnan(v) else round(v, 6)]
                          for t, v in pts]
        out.append(item)
    return out


USUAL_DAYS = 7                   # a level is given with the same window of this many previous days
USUAL_SERIES = 12                # ... when the query returns at most this many series
USUAL_RANGE_DAYS = 2             # ... over at most this long
USUAL_SECONDS = 20               # ... while there is time
USUAL_HIGH, USUAL_LOW = 1.3, 0.77


def _levels_usual(conn: Any, expr: str, t0: int, t1: int, step_ms: int, items: list[dict]) -> str | None:
    """Each series of a query gets what it was over the same window of the previous days (the median of their
    averages and of their highest values): a pool at 40 of 40 slots, a queue of 300 or a CPU at 90% is a finding
    only when it is not what it is every day at that hour. A line that says so, or None (nothing to compare)."""
    from concurrent.futures import ThreadPoolExecutor

    day = 86_400_000
    step = max(step_ms, (t1 - t0) // 60 // 15_000 * 15_000, 15_000) if t1 > t0 else 0

    def key(labels: dict) -> tuple:
        return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items() if k != "__name__"))

    def one(k: int) -> dict[tuple, tuple[float, float]] | None:
        try:
            got = conn.client.query_range(expr, t0 - k * day, t1 - k * day, step) if step else \
                conn.client.query(expr, t1 - k * day)
        except Exception:  # pylint: disable=broad-except   (that day is not counted)
            return None
        out = {}
        for s in got:
            vals = [v for _t, v in s.points if v is not None and not math.isnan(v)]
            if vals:
                out[key(s.labels)] = (sum(vals) / len(vals), max(vals))
        return out or None

    started = time.time()
    days: list[dict[tuple, tuple[float, float]]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for got in pool.map(one, range(1, USUAL_DAYS + 1)):
            if got is not None:
                days.append(got)
            if time.time() - started > USUAL_SECONDS:
                break
    if len(days) < 3:
        return None
    off, same = [], 0
    for item in items:
        if item.get("avg") is None:
            continue
        had = [d[key(item["labels"])] for d in days if key(item["labels"]) in d]
        if len(had) < 3:
            item["against_usual"] = "no usual: not there on the previous days"
            continue
        usual_avg = _median([a for a, _m in had])
        item["usual_avg"], item["usual_max"] = round(usual_avg, 6), round(_median([m for _a, m in had]), 6)
        if abs(usual_avg) < 1e-12:
            ratio = None if abs(item["avg"]) < 1e-12 else math.inf
        else:
            ratio = item["avg"] / usual_avg
        if ratio is None or USUAL_LOW <= ratio <= USUAL_HIGH:
            item["against_usual"] = "as on the previous days"
            same += 1
        else:
            item["against_usual"] = ("nothing usually" if ratio == math.inf else f"x{ratio:.2f} its usual") + \
                (": above usual" if ratio > 1 else ": below usual")
            off.append(", ".join(f"{k}={v}" for k, v in item["labels"].items()) or "the series")
    compared = same + len(off)
    if not compared:
        return None
    what = f"the same window of the {len(days)} previous days (the median of their averages: usual_avg, usual_max)"
    if not off:
        return (f"every series is as on {what}: these levels are their usual, not a change" if compared > 1 else
                f"the series is as on {what}: this level is its usual, not a change")
    return f"{len(off)} of {compared} series differ from {what}: {_some(off, 6)}; the others are as usual"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


@mcp.tool
def promql_query(expr: str, start: str | None = None, end: str | None = None, step: str | None = None,
                 database: str | int | None = None, max_series: int = 50) -> dict:
    """Run a PromQL expression on the metrics database and return each series (labels,
    min / max / avg / last and up to 60 points). `start` / `end`: local times like
    "2026-09-24 02:00" or "now-6h" (end alone or start == end: one instant). `step` like "5m"
    (default: about 200 points). Use for questions SQL cannot express (ratios of two metrics,
    offsets, label_replace...); the SQL tables of the same database cover the usual cases.
    A query of a few series over at most two days also says what each was over the same window of the
    previous days (usual_avg, usual_max, against_usual): a level is a finding only when it is not its usual."""
    try:
        with _as_user():
            db_obj = _metrics_database(database)
            conn = _promagg_connection(db_obj)
            try:
                from promagg.timegrid import duration, parse_duration
                from superset.extensions import db as meta

                meta.session.commit()    # no connection of Superset's own pool is held while the query runs

                if len(expr) > 4000:
                    raise ToolError("expression too long (4000 characters max)")
                now = _now_ms(conn)
                t1 = _time_arg(conn, end, now)
                t0 = _time_arg(conn, start, t1)
                if t1 < t0:
                    t0, t1 = t1, t0
                if (t1 - t0) > MAX_PROMQL_RANGE_DAYS * 86_400_000:
                    raise ToolError(f"time range longer than {MAX_PROMQL_RANGE_DAYS:g} days")
                if t0 == t1:
                    series = conn.client.query(expr, t1)
                    step_ms = 0
                else:
                    step_ms = parse_duration(step) if step else max(15_000, (t1 - t0) // 200 // 15_000 * 15_000)
                    if (t1 - t0) // step_ms > 11_000:
                        raise ToolError("too many points: give a larger step")
                    series = conn.client.query_range(expr, t0, t1, step_ms)
                total = len(series)
                series.sort(key=lambda s: -max((v for _t, v in s.points if not math.isnan(v)), default=-math.inf))
                hint = ""
                if not total:
                    names = conn.list_tables()
                    used = [w for w in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", expr) if w in names]
                    if not used:
                        words = _tokens(expr.replace("_", " "))
                        near = [n for n in names if words & _tokens(n.replace("_", " "))][:12]
                        hint = ("no series: the expression names no existing metric; metrics with these words: "
                                + (", ".join(near) or ", ".join(names[:20])) + " (describe_data lists them)")
                    else:
                        from supagent.knowledge.empty import why_empty_promql

                        hint = why_empty_promql(db_obj, expr) or \
                            "no series in this time range (check the dates: describe_data gives the data range)"
                items = _series_summary(conn, series[:max(1, min(max_series, 200))], 60)
                usual = None
                if 0 < total <= USUAL_SERIES and (t1 - t0) <= USUAL_RANGE_DAYS * 86_400_000 and not raw_counter(expr):
                    try:
                        usual = _levels_usual(conn, expr, t0, t1, step_ms, items)
                    except Exception:  # pylint: disable=broad-except   (the query's own answer stands)
                        log.debug("supagent: the usual of %s not read", expr[:120], exc_info=True)
                return {"expr": expr, "database": db_obj.database_name, "database_id": db_obj.id,
                        **({"hint": hint} if hint else {}),
                        "start": f"{conn.zone.local(t0):%Y-%m-%d %H:%M}", "end": f"{conn.zone.local(t1):%Y-%m-%d %H:%M}",
                        "step": duration(step_ms) if step_ms else "instant", "series_count": total,
                        **({"against_usual": usual} if usual else {}),
                        "series": items, "truncated": total > max_series}
            finally:
                conn.close()
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


def _breaches(conn: Any, series: list, op: str, threshold: float, min_ms: int, step_ms: int) -> list[dict]:
    out = []
    for s in series:
        run: list[tuple[int, float]] = []
        pts = [(t, v) for t, v in s.points if not math.isnan(v)]

        def close() -> None:
            if not run:
                return
            dur = run[-1][0] - run[0][0] + step_ms
            if dur >= min_ms:
                worst = max(v for _t, v in run) if op == "above" else min(v for _t, v in run)
                out.append({"labels": {k: v for k, v in s.labels.items() if k != "__name__"},
                            "from": f"{conn.zone.local(run[0][0] - step_ms):%Y-%m-%d %H:%M}",
                            "to": f"{conn.zone.local(run[-1][0]):%Y-%m-%d %H:%M}",
                            "minutes": round(dur / 60000), "worst": round(worst, 3)})

        prev_t = None
        for t, v in pts:
            bad = v > threshold if op == "above" else v < threshold
            if bad and (not run or prev_t is None or t - prev_t <= step_ms):
                run.append((t, v))
            else:
                close()
                run = [(t, v)] if bad else []
            prev_t = t
        close()
    return out


_GROUPING = re.compile(r"\b(by|on)\s*\((?!\s*__tenant_id__\b)")


def _per_tenant(expr: str) -> str:
    """Aggregations and vector matching keep the label __tenant_id__: with several tenants
    (Mimir tenant federation: tenant=a|b, or tenants set by a gateway) the same server or
    application name in two tenants stays two entities, and each result says its tenant. With
    one tenant the label is absent and the results are the same."""
    expr = re.sub(r"\b(by|on)\s*\(\s*\)", r"\1 (__tenant_id__)", expr)
    return _GROUPING.sub(lambda m: f"{m.group(1)} (__tenant_id__, ", expr)


@mcp.tool
def check_health(start: str, end: str, entities: list[str] | None = None, checks: list[str] | None = None,
                 database: str | int | None = None) -> dict:
    """Evaluate the health checks of the data dictionary (CPU saturation, memory pressure, disk
    full, OOM kills, servers down, queue backlog, HTTP errors, latency, licences...) over a time
    window and list every breach: check, server / application, from, to, minutes, worst value, and
    whether the same breach also happened in this window on the previous days (earlier_days;
    usual: true = on most of them: it does not single out this window). The new ones come first.
    `start` / `end`: local times ("2026-09-24 02:00"); `entities`: only these servers,
    applications or pools (label values, e.g. ["srv-amer-002"]); `checks`: only these checks."""
    try:
        with _as_user():
            cat = _catalog()
            defs = cat.get("checks") or {}
            if not defs:
                raise ToolError("no checks in catalog.yaml")
            if checks:
                unknown = [c for c in checks if c not in defs]
                if unknown:
                    raise ToolError(f"unknown checks {unknown}; checks: {', '.join(defs)}")
                defs = {k: v for k, v in defs.items() if k in checks}
            db_obj = _metrics_database(database)
            conn = _promagg_connection(db_obj)
            try:
                from promagg.timegrid import parse_duration

                t0, t1 = _time_arg(conn, start, 0), _time_arg(conn, end, 0)
                if t1 <= t0:
                    raise ToolError("end must be after start")
                if (t1 - t0) > MAX_PROMQL_RANGE_DAYS * 86_400_000:
                    raise ToolError(f"time range longer than {MAX_PROMQL_RANGE_DAYS:g} days")
                step = max(60_000, (t1 - t0) // 1440 // 60_000 * 60_000)
                wanted = {e.lower() for e in entities or []}
                found, errors = [], {}

                def one(item):
                    name, c = item
                    try:
                        series = conn.client.query_range(_per_tenant(c["promql"]), t0 + step, t1, step)
                    except Exception as ex:  # pylint: disable=broad-except
                        return name, c, None, str(ex)[:300]
                    return name, c, series, None

                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(one, defs.items()))
                hidden, matched = 0, not wanted
                for name, c, series, err in results:
                    if err:
                        errors[name] = err
                        continue
                    if wanted and any({str(v).lower() for v in (getattr(x, "labels", None) or {}).values()} & wanted
                                      for x in series or []):
                        matched = True
                    op = "above" if "above" in c else "below"
                    thr = float(c.get(op))
                    min_ms = parse_duration(str(c.get("for", "0s"))) if c.get("for") else 0
                    for b in _breaches(conn, series or [], op, thr, min_ms, step):
                        if wanted and not ({str(v).lower() for v in b["labels"].values()} & wanted):
                            hidden += 1
                            continue
                        found.append({"check": name, "description": c.get("description", ""),
                                      "threshold": f"{op} {thr:g}{c.get('unit', '')}", **b})
                found.sort(key=lambda b: (b["from"], b["check"]))
                usual = _breaches_usual(conn, defs, found, t0, t1, step) if found else 0
                found.sort(key=lambda b: bool(b.get("usual")))       # the new ones first (stable: by time within)
                note = "no breach" if not found else ""
                if usual:
                    note = (f"{usual} of the {len(found)} breach(es) also happen in this window on most of the "
                            f"previous days (usual: true): they do not single out this window")
                if not matched:                       # "no breach" would be wrong: nothing was looked at
                    note = (f"entities {sorted(wanted)} match no label value of the checked series (they are not "
                            f"server, application or pool names), so nothing was checked"
                            + (f"; {hidden} breach(es) on other entities: call again without entities"
                               if hidden else ""))
                elif not found and hidden:
                    note = f"no breach on {sorted(wanted)}; {hidden} breach(es) on other entities"
                return {"database": db_obj.database_name, "from": start, "to": end,
                        "step_minutes": step // 60000, "checks": list(defs), "breaches": found[:200],
                        "breach_count": len(found), "errors": errors, "note": note}
            finally:
                conn.close()
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


HEALTH_DAYS = 7                  # earlier days a breach is looked for on (the same window; a whole week, so
                                 # that what happens every working day counts as usual on a Monday too)
HEALTH_USUAL_CHECKS = 8          # checks with breaches that are compared that way
HEALTH_USUAL_SHARE = 0.6         # breached on this share of the earlier days (3 at least): usual
HEALTH_USUAL_SECONDS = 30.0      # spent on it at most (a check over thousands of series is slow): then as before


def _breaches_usual(conn: Any, defs: dict[str, Any], found: list[dict], t0: int, t1: int, step: int) -> int:
    """Each breach gets `earlier_days` (on how many of the previous days the same series breached the same check
    in the same window) and `usual` when that is most of them: an alert that fires every night is not what
    changed today. Only the checks that breached are run again; a day with no data does not count. The number
    of usual breaches."""
    from concurrent.futures import ThreadPoolExecutor

    from promagg.timegrid import parse_duration

    names = list(dict.fromkeys(b["check"] for b in found))[:HEALTH_USUAL_CHECKS]
    period = max(1, math.ceil((t1 - t0) / 86_400_000)) * 86_400_000

    def key(labels: dict) -> tuple:
        return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))

    def one(arg: tuple[str, int]) -> tuple[str, int, set | None]:
        name, k = arg
        c = defs[name]
        try:
            series = conn.client.query_range(_per_tenant(c["promql"]), t0 - k * period + step, t1 - k * period, step)
        except Exception:  # pylint: disable=broad-except   (that day is not counted)
            return name, k, None
        if not series:
            return name, k, None
        op = "above" if "above" in c else "below"
        min_ms = parse_duration(str(c.get("for", "0s"))) if c.get("for") else 0
        return name, k, {key(b["labels"]) for b in _breaches(conn, series, op, float(c.get(op)), min_ms, step)}

    started = time.time()
    seen: dict[str, list[set]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name in names:                           # a check at a time, while there is time
            if time.time() - started > HEALTH_USUAL_SECONDS:
                break
            for _n, _k, keys in pool.map(one, [(name, k) for k in range(1, HEALTH_DAYS + 1)]):
                if keys is not None:
                    seen.setdefault(name, []).append(keys)
    usual = 0
    for b in found:
        had = seen.get(b["check"])
        if not had:
            continue
        n = sum(1 for keys in had if key(b["labels"]) in keys)
        b["earlier_days"] = f"{n} of the {len(had)} previous days"
        if len(had) >= 3 and n >= HEALTH_USUAL_SHARE * len(had):
            b["usual"] = True
            usual += 1
    return usual


USUAL_MIN_WEEKS = 3              # below this many earlier weeks with data: no verdict
USUAL_SENSITIVITY = 3.5          # median absolute deviations from the median that count as unusual
USUAL_MIN_CHANGE = 0.2           # and at least this relative change (a flat series moves little)


def _usual(now: float | None, before: list[float]) -> dict[str, Any]:
    """Verdict of one series: normal, high, low or unknown, from the same window on earlier weeks
    (median and median absolute deviation: one bad week does not widen the band)."""
    import statistics

    out: dict[str, Any] = {"now": None if now is None else round(now, 6),
                           "previous_weeks": [round(v, 6) for v in before]}
    if now is None or len(before) < USUAL_MIN_WEEKS:
        out.update(verdict="unknown", reason=f"{len(before)} earlier week(s) with data, {USUAL_MIN_WEEKS} needed")
        return out
    med = statistics.median(before)
    mad = statistics.median([abs(v - med) for v in before])
    scale = max(mad, abs(med) * 0.05, 1e-9)
    dev = (now - med) / scale
    change = abs(now - med) / abs(med) if med else (math.inf if now else 0.0)
    verdict = "normal"
    if abs(dev) >= USUAL_SENSITIVITY and change >= USUAL_MIN_CHANGE:
        verdict = "high" if dev > 0 else "low"
    out.update(median=round(med, 6), deviations=round(dev, 1), change_pct=None if math.isinf(change) else
               round(100 * (now - med) / abs(med), 1) if med else None, verdict=verdict)
    if verdict == "normal" and change >= 0.5 and not math.isinf(change):
        # far from the median, yet no more than the earlier weeks differ from each other: said, not hidden
        out["note"] = (f"{out['change_pct']:+g}% against the median, but the earlier weeks vary as much "
                       f"({min(before):g} to {max(before):g}): not unusual for this series")
    return out


@mcp.tool
def compare_to_usual(promql: str, start: str, end: str, weeks: int = 4, database: str | int | None = None,
                     against: list[str] | None = None) -> dict:
    """Is a metric unusual for this time? The average of a PromQL expression over start-end
    compared with the same window of each of the previous `weeks` weeks (median and median
    absolute deviation, per series): verdict normal, high, low (or unknown without 3 earlier
    weeks), with the numbers. `start` / `end`: local times ("2026-09-24 02:00"), at most 7 days
    apart. For "is it unusual / abnormal / higher than usual" questions.
    against: reference days to give each series on too (its average over the same window then):
    ["yesterday", "1 week ago", "3 weeks ago", "2 months ago", "2026-06-15"]; the team's own reference
    days when none is given."""
    try:
        with _as_user():
            db_obj = _metrics_database(database)
            conn = _promagg_connection(db_obj)
            try:
                from superset.extensions import db as meta

                meta.session.commit()        # no connection of Superset's own pool is held while the queries run
                if len(promql) > 4000:
                    raise ToolError("expression too long (4000 characters max)")
                counter = raw_counter(promql)
                if counter:                           # its value only grows: every week looks "normal" or not by chance
                    raise ToolError(f"{counter} is a counter (its value only grows since the process started): compare "
                                    f"its rate or its increase, e.g. sum(rate({counter}[5m])) or "
                                    f"sum(increase({counter}[1h])), with the same labels")
                t0, t1 = _time_arg(conn, start, 0), _time_arg(conn, end, 0)
                if t1 <= t0:
                    raise ToolError("end must be after start")
                week = 7 * 86_400_000
                if t1 - t0 > week:
                    raise ToolError("the window must be at most 7 days (it is compared with earlier weeks)")
                weeks = max(1, min(int(weeks or 4), 8))
                step = max(60_000, (t1 - t0) // 60 // 60_000 * 60_000)

                def means(k: int) -> dict[tuple, float]:
                    series = conn.client.query_range(promql, t0 - k * week, t1 - k * week, step)
                    out: dict[tuple, float] = {}
                    for s in series:
                        vals = [v for _t, v in s.points if v is not None and not math.isnan(v)]
                        if vals:
                            key = tuple(sorted((k2, v2) for k2, v2 in s.labels.items() if k2 != "__name__"))
                            out[key] = sum(vals) / len(vals)
                    return out

                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=3) as pool:
                    per_week = list(pool.map(means, range(weeks + 1)))
                keys = list(per_week[0]) or sorted({k for w in per_week[1:] for k in w})
                rows = []
                for key in keys:
                    item = {"labels": dict(key)}
                    item.update(_usual(per_week[0].get(key), [w[key] for w in per_week[1:] if key in w]))
                    rows.append(item)
                rank = {"high": 0, "low": 0, "normal": 1, "unknown": 2}
                rows.sort(key=lambda r: (rank[r["verdict"]], -abs(r.get("deviations") or 0)))
                unusual = sum(1 for r in rows if r["verdict"] in ("high", "low"))
                out = {"database": db_obj.database_name, "from": start, "to": end, "weeks": weeks,
                       "series_count": len(rows), "unusual": unusual, "series": rows[:30],
                       "note": (f"{unusual} of {len(rows)} series unusual for this time" if rows else
                                "no series in this window (check the expression and the dates)")}
                refs = _metric_references(against)
                if refs and rows:                     # each series on the reference days (its average over the window then)
                    day = 86_400_000

                    def then(ref: Any) -> tuple[str, str, dict[tuple, float]]:
                        back = ref.days if ref.kind != "date" else \
                            (conn.zone.local(t0).date() - ref.date).days
                        if back <= 0:
                            return ref.label, "not before the window", {}
                        try:
                            series = conn.client.query_range(promql, t0 - back * day, t1 - back * day, step)
                        except Exception:  # pylint: disable=broad-except   (that day is not given)
                            return ref.label, "not read", {}
                        got: dict[tuple, float] = {}
                        for sr in series:
                            vals = [v for _t, v in sr.points if v is not None and not math.isnan(v)]
                            if vals:
                                got[tuple(sorted((k2, v2) for k2, v2 in sr.labels.items() if k2 != "__name__"))] = sum(vals) / len(vals)
                        return ref.label, f"{conn.zone.local(t0 - back * day):%Y-%m-%d}" if got else "no data that day", got

                    with ThreadPoolExecutor(max_workers=3) as pool:
                        days = list(pool.map(then, refs))
                    out["reference_days"] = {label: when for label, when, _got in days}
                    for item in out["series"]:
                        key = tuple(sorted(item["labels"].items()))
                        then_values = {label: round(got[key], 6) for label, _when, got in days if key in got}
                        if then_values:               # (a day the metrics do not reach has no figure)
                            item["then"] = then_values
                return out
            finally:
                conn.close()
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


def _metric_references(against: list[str] | str | None) -> list[Any]:
    """The reference days of a comparison of metrics: the ones given, else the team's own (agent.compare_against)."""
    from supagent.knowledge import groups as G

    if against is not None:
        try:
            return G.references(against)
        except G.GroupsError as ex:
            raise ToolError(str(ex)) from ex
    try:
        return G.references(str(_setting("agent.compare_against", "") or ""))
    except G.GroupsError:
        return []


GROUP_FIELDS = 9                 # fields compared in one call: the ones asked, then the table's other fields of few values
GROUP_DAYS = 10                  # earlier days at most
GROUP_ROWS = 400                 # values of a field read per window
GROUP_SECONDS = 45               # after this long, no further field is read (agent.compare_seconds)
GROUP_THREADS = 3                # queries of one call that run at a time (OpenSearch: a connection each;
                                 # agent.compare_threads)
GROUP_CHARS = 6500               # a result longer than this gives up what repeats its conclusion
PAST_HOURS = 30                  # a window that ended this long before now is said to be of another day
WEEKLY = 1.18                    # within this of the same weekday of the earlier weeks: what that weekday is
IN_PROGRESS_MINUTES = 60         # a window that ends this close to now (or later) is still in progress
RELATED_DAYS = 7                 # records of the related tables: this many days before the end of the window
RELATED_ROWS = 4                 # ... the latest ones
RELATED_VALUES = 8               # ... about at most this many values that stand out
FOLLOW_FIELDS = 6                # the comparison made for a lead (every row on what the rows wait for): this many fields
OUTSIDE_FIELDS = 3               # who else is on what stands out, outside the scope: by this many of the scope's fields
PRESENT_WAYS = 3.0               # ... a row is there when it came at most this many usual whole ways before the window
FOLLOW_SHARE = 0.5               # a lead is followed when at most this share of the call's time is spent
OUTSIDE_LEADS = 2                # ... for this many of the fields the change is concentrated on
ID_NAME = re.compile(r"(^|_)(ID|UUID|KEY)(_|$)|^_id$", re.I)
NUMERIC = ("double", "float", "long", "integer", "int", "short", "byte", "half_float", "scaled_float", "bigint",
           "decimal", "real", "numeric", "smallint", "unsigned_long")
NO_FILTER_CLAUSE = ("mysql", "mariadb", "mssql", "oracle", "clickhouse")     # no FILTER (WHERE ...) on an aggregate


def _table_database(table: str, ref: str | int | None) -> Any:
    """The database a table is in: the one given, else the one the dictionary learned it in (among those the
    agent may use), else the first one."""
    if ref not in (None, ""):
        return _database(ref)
    try:
        from superset.extensions import db
        from superset.models.core import Database

        from supagent.models import KObject, Source
        from supagent.security import can_use_database

        ids = [i for (i,) in db.session.query(Source.database_id).join(KObject, KObject.source_id == Source.id).filter(
            KObject.kind == "index", KObject.name == table, KObject.gone_at.is_(None))]
        found = agent_databases([d for d in db.session.query(Database).filter(Database.id.in_(ids or [-1]))
                                 if can_use_database(d)])
        if found:
            return found[0]
    except Exception:  # pylint: disable=broad-except   (no dictionary: the first database)
        pass
    return _database(None)


def _time_field(table: str, given: str | None) -> str:
    from supagent.knowledge.groups import GroupsError, name

    if given:
        return name(given)
    spec = (_catalog().get("indices") or {}).get(table) or {}
    if spec.get("time_field"):
        return str(spec["time_field"])
    try:
        from superset.extensions import db

        from supagent.models import KObject

        o = db.session.query(KObject).filter(KObject.kind == "index", KObject.name == table,
                                             KObject.gone_at.is_(None)).first()
        if o is not None and (o.stats or {}).get("time_field"):
            return str(o.stats["time_field"])
    except Exception:  # pylint: disable=broad-except
        pass
    raise GroupsError(f"give time_field: the time field of {table!r} is not known")


def _row_time_field(table: str, given: str | None) -> tuple[str, str | None]:
    """The time field of a comparison: the one asked, unless it is a column the connector computes (the business
    date's own time is no time of the rows: every earlier day would be read on the wrong rows); then the table's
    own, and the one that was asked (to say so)."""
    tf = _time_field(table, given)
    if not given:
        return tf, None
    try:
        from superset.extensions import db

        from supagent.models import KObject

        o = db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.name == tf,
                                             KObject.gone_at.is_(None)).first()
        if o is not None and (o.stats or {}).get("computed"):
            own = _time_field(table, None)
            if own != tf:
                return own, tf
    except Exception:  # pylint: disable=broad-except   (no dictionary, no time field known: as asked)
        pass
    return tf, None


def _group_fields(table: str, fixed: set[str], label_column: str | None) -> list[str]:
    """The fields worth grouping by when none is given: the table's fields of few values (the dictionary), those a
    category is read from first (application, server...), never an identifier nor a field the scope pins."""
    from superset.extensions import db

    from supagent.knowledge.facets import field_rules
    from supagent.models import KObject

    rules = [rx for _c, rx in field_rules()]
    rows = []
    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.gone_at.is_(None)):
        card = (o.stats or {}).get("cardinality") or len((o.stats or {}).get("values") or [])
        kind = str(o.data_type or "").lower()
        if not (2 <= int(card or 0) <= 80) or o.name in fixed or o.name == label_column or ID_NAME.search(o.name) or \
                kind in ("date", "boolean", "text", "timestamp") or kind in NUMERIC:
            continue
        rows.append((not any(rx.match(o.name) for rx in rules), int(card), o.name))
    return [n for _a, _b, n in sorted(rows)][:GROUP_FIELDS]


@contextmanager
def _closing(connections: list[Any]) -> Iterator[None]:
    try:
        yield
    finally:
        for c in connections:
            try:
                c.close()
            except Exception:  # pylint: disable=broad-except
                pass


def _many_values(table: str, fields: list[str]) -> dict[str, int]:
    """The fields asked that have too many values to compare value by value (the dictionary's count): {field: n}."""
    if not fields:
        return {}
    from superset.extensions import db

    from supagent.models import KObject

    out = {}
    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table,
                                              KObject.name.in_(fields), KObject.gone_at.is_(None)):
        card = int((o.stats or {}).get("cardinality") or 0)
        if card > GROUP_ROWS:
            out[o.name] = card
    return out


def _time_fields(table: str, time_field: str) -> set[str]:
    """A table's time fields (the dictionary), with the one its window is on."""
    out = {time_field}
    try:
        from superset.extensions import db

        from supagent.models import KObject

        for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.gone_at.is_(None)):
            if str(o.data_type or "").lower().startswith(("date", "timestamp")):
                out.add(o.name)
    except Exception:  # pylint: disable=broad-except   (no dictionary: the window's field)
        pass
    return out


def _measures(table: str, time_field: str) -> list[Any]:
    """The measures of a table compared when none is asked: its rows, each field against the field that holds
    its usual value (the catalog: usual_of), the rows that had reached each of its other time fields by the end
    of the window (ready, started, ended: where its rows are late), and the average of its other numeric fields
    (the dictionary; the ones the catalog describes first)."""
    from superset.extensions import db

    from supagent.knowledge import groups as G
    from supagent.models import KObject

    spec = ((_catalog().get("indices") or {}).get(table) or {}).get("fields") or {}
    usual = {name: str(fs["usual_of"]) for name, fs in spec.items() if isinstance(fs, dict) and fs.get("usual_of")}
    out = [G.Measure("count")] + [G.Measure("ratio", (a, b)) for b, a in usual.items()]
    numbers, times = [], []
    try:
        own = _time_field(table, None)               # (the table's own time field is no stage of its rows)
    except Exception:  # pylint: disable=broad-except
        own = time_field
    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.gone_at.is_(None)):
        st = o.stats or {}
        kind = str(o.data_type or "").lower()
        described = not isinstance(spec.get(o.name), dict)
        if kind == "date" and o.name not in (time_field, own, "@timestamp") and not st.get("computed"):
            times.append((described, o.name))         # what had reached it by the end of the window
        if kind not in NUMERIC or ID_NAME.search(o.name) or o.name in usual or o.name in usual.values() or \
                (st.get("min") is not None and st.get("min") == st.get("max")):
            continue                                  # (a field compared with its usual: the ratio says it)
        numbers.append((described, o.name))
    out += [G.Measure("reached", (n,)) for _d, n in sorted(times)[:4]]
    return (out + [G.Measure("avg", (n,)) for _d, n in sorted(numbers)])[:G.MEASURES]


def _neighbours(table: str) -> dict[str, str]:
    """The tables the catalog relates to a table, within two joins: {table: what the join says}."""
    cat = _catalog().get("indices") or {}
    out: dict[str, str] = {}

    def of(t: str) -> list[tuple[str, str]]:
        found = [(str(r["to"]), str(r.get("description") or "")) for r in (cat.get(t) or {}).get("relationships") or []
                 if r.get("to")]
        found += [(src, str(r.get("description") or "")) for src, spec in cat.items()
                  for r in (spec or {}).get("relationships") or [] if r.get("to") == t]
        return found

    for other, why in of(table):
        out.setdefault(other, why)
    for near in list(out):
        for other, why in of(near):
            out.setdefault(other, why)
    out.pop(table, None)
    return out


def _records(run: Any, other: str, db_obj: Any, where: str, until: dt.datetime) -> dict[str, Any] | None:
    """The latest records of a related table that satisfy a condition, over the days before `until`."""
    from superset.extensions import security_manager

    from supagent.knowledge import groups as G

    if _table_database(other, None).id != db_obj.id:
        return None
    tf = _time_field(other, None)
    security_manager.raise_for_access(database=db_obj, sql=f'SELECT COUNT(*) FROM "{G.name(other)}"', schema="default")
    since = until - dt.timedelta(days=RELATED_DAYS)
    where = f'{where} AND "{tf}" >= {G.lit(since)} AND "{tf}" < {G.lit(until)}'
    total = run(f'SELECT COUNT(*) AS n FROM "{other}" WHERE {where}')
    n = int(total[0][0] or 0) if total else 0
    out: dict[str, Any] = {"table": other, "since": f"{since:%Y-%m-%d %H:%M}", "records": n}
    if not n:
        return out
    sql = f'SELECT * FROM "{other}" WHERE {where} ORDER BY "{tf}" DESC LIMIT {RELATED_ROWS}'
    rows, cols = run(sql, described=True)
    latest = []
    for r in rows:
        rec: dict[str, Any] = {}
        for c, v in zip(cols, r):
            if v in (None, "") or c.startswith("_") or (c == "@timestamp" and tf != "@timestamp"):
                continue
            rec[c] = f"{v:%Y-%m-%d %H:%M}" if isinstance(v, dt.datetime) else (v if isinstance(v, (int, float, bool)) else str(v)[:150])
        if len({k for k, v in rec.items() if v == rec.get(tf)}) > 1:
            rec.pop(tf, None)                    # the same time under another name
        latest.append(dict(list(rec.items())[:9]))
    out["latest"] = latest
    out["sql"] = sql
    return out


def _related(run: Any, table: str, db_obj: Any, field: str, values: list[str], until: dt.datetime) -> list[dict]:
    """What the related tables hold about values that stand out: the catalog's joins on that field (the changes
    made to an application, the alerts raised on a server), their latest records of the days before the end of
    the window; for a field nothing joins, the records of the related tables whose text mentions the value (a
    change that says it concerns that perimeter). A join declared on a field holds for the field of the same
    name in the table compared."""
    from superset.extensions import db

    from supagent.knowledge import groups as G
    from supagent.models import KObject

    cat = _catalog().get("indices") or {}
    rels: dict[tuple[str, str], str] = {}
    for src, spec in cat.items():
        for rel in (spec or {}).get("relationships") or []:
            keys = rel.get("keys") or {}
            if len(keys) != 1 or not rel.get("to"):
                continue
            (mine, theirs), = keys.items()
            if mine == field and rel["to"] != table:
                rels.setdefault((str(rel["to"]), str(theirs)), str(rel.get("description") or ""))
            elif theirs == field and rel["to"] == table and src != table:      # declared from the other side
                rels.setdefault((str(src), str(mine)), str(rel.get("description") or ""))
    values = [str(v) for v in values if str(v) != "(none)"][:RELATED_VALUES]
    if not values:
        return []
    out = []
    listed = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    for (other, key), why in list(rels.items())[:2]:
        try:
            found = _records(run, other, db_obj, f'"{G.name(key)}" IN ({listed})', until)
        except Exception:  # pylint: disable=broad-except   (no such table for this user, no time field: not followed)
            continue
        if found is not None:
            out.append({"about": f'"{key}" = {_some(values, 6)}', "what": why or f'joined on "{field}" = "{key}" (the catalog)',
                        **found})
    if rels:
        return out
    for other, why in list(_neighbours(table).items())[:6]:          # nothing joins this field: who mentions the value
        texts = [o.name for o in db.session.query(KObject).filter(
            KObject.kind == "field", KObject.parent == other, KObject.gone_at.is_(None))
            if str(o.data_type or "").lower() == "text"][:2]
        if not texts or len(out) >= 2:
            continue
        cond = " OR ".join(f'"{G.name(t)}" LIKE \'%{v.replace(chr(39), "")}%\'' for t in texts for v in values[:3]
                           if len(v) >= 3 and "%" not in v)       # (an underscore in the value: any one character)
        if not cond:
            continue
        try:
            found = _records(run, other, db_obj, f"({cond})", until)
        except Exception:  # pylint: disable=broad-except
            continue
        if found is not None and found["records"]:
            out.append({"about": f"records whose {_some(texts, 2)} mentions {_some(values[:3], 3)}",
                        "what": why or "related by the catalog", **found})
    return out


def _some(items: list[str], n: int) -> str:
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _label_now(conn: Any) -> dt.datetime:
    now = getattr(conn, "label_now", None) or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is not None:
        tz = getattr(conn, "label_tz", None)
        now = (now.astimezone(tz) if tz else now.astimezone()).replace(tzinfo=None)
    return now


def _labels_of_dates(conn: Any, dates: list[str], at: dt.datetime | None = None) -> list[str]:
    """Business dates the scope pins (POSITION_DATE = '20260924'), as their labels (D-1...) on the day `at` (now
    when not given): the same scope can then be read on the earlier days. [] when one of them is no date of the
    calendar."""
    try:
        from osagg import calendar
    except ImportError:
        return []
    fmt = getattr(conn, "label_date_format", "%Y%m%d")
    key = calendar.asof_key(at or _label_now(conn), conn.label_cutoff, getattr(conn, "label_years", True))
    out = []
    for d in dates:
        try:
            label = calendar.label(dt.datetime.strptime(str(d), fmt).strftime("%Y%m%d"), key)
        except ValueError:
            return []
        if not label:
            return []
        out.append(label)
    return list(dict.fromkeys(out))


def _earlier_dates(conn: Any, labels: list[str], back: dt.timedelta, at: dt.datetime | None = None) -> str | None:
    """The business-date labels of the scope, as the dates they were `back` before `at` (now when not given), as
    a condition on the labels' source field; None when the connection has no such calendar."""
    try:
        from osagg import calendar
    except ImportError:
        return None
    src, fmt = getattr(conn, "label_source", None), getattr(conn, "label_date_format", "%Y%m%d")
    if not src or not labels or not hasattr(conn, "label_cutoff"):
        return None
    key = calendar.asof_key((at or _label_now(conn)) - back, conn.label_cutoff, getattr(conn, "label_years", True))
    dates = calendar.dates_for_labels(labels, key)
    if not dates:
        return "1 = 0"
    values = ", ".join("'" + dt.datetime.strptime(d, "%Y%m%d").strftime(fmt) + "'" for d in dates)
    return f'"{src}" IN ({values})'


class _Comparison:
    """One call of compare_groups: the windows (now, the earlier days of the usual, the reference days), the
    queries (one per window and field, every measure at once), the judgments and the result."""

    def __init__(self, conn: Any, db_obj: Any, more: list[Any], table: str, tf: str, t0: dt.datetime,
                 t1: dt.datetime, where: str, group_by: list[str] | None, measure: str, days: int,
                 against: list[str] | str | None, inside: "_Comparison | None" = None, cursors: Any = None) -> None:
        """inside: the comparison this one is made for (a lead it follows): its connections, its time.
        cursors: the connections of an earlier comparison of the same call."""
        from supagent.knowledge import groups as G

        self.G, self.conn, self.db, self.more = G, conn, db_obj, more
        self.inside = inside
        self.table, self.tf, self.t0, self.t1 = table, tf, t0, t1
        self.every = str(measure or "all").strip().lower() in ("all", "every", "*", "")
        self.measures = _measures(table, tf) if self.every else [G.Measure.parse(measure)]
        self.days = max(2, min(int(days or 8), GROUP_DAYS))
        label_column = getattr(conn, "label_column", None) if db_obj.backend == "osagg" else None
        self.cond, self.labels = G.scope(where, label_column)
        # an instant written in the scope would leave every earlier day empty: a date moves with each earlier day,
        # anything else (since now minus six hours) is left out, the window gives the time
        self.times = G.time_columns(self.cond, _time_fields(table, tf))
        self.cond, self.moving, self.untimed = G.timed(self.cond, self.times)
        if self.labels and _earlier_dates(conn, self.labels, dt.timedelta(0)) == "1 = 0":
            raise ToolError(f'where: "{label_column}" = {", ".join(self.labels)}: not a business-date label (D, D-1, '
                            "D-2, W-1...); a state or another value belongs to its own field")
        self.moved = label_column                    # the column whose condition moves with the day
        # a business date is of the day the window ends: asked now about today, today's; asked about last Monday
        # (a window that ended then), that Monday's, and each earlier day's own from there
        self.anchor = min(t1, _label_now(conn)) if label_column else t1
        if not self.labels and label_column and getattr(conn, "label_source", None):
            pinned = G.literals(self.cond, conn.label_source)        # a business date written as a date
            self.labels = _labels_of_dates(conn, pinned, self.anchor) if pinned else []
            self.moved = conn.label_source if self.labels else label_column
        # a business date in the scope says which rows: each earlier day is one day back, whatever the window's
        # length (a window that starts the day before does not skip every other day)
        self.period = dt.timedelta(days=1 if self.labels else max(1, math.ceil((t1 - t0).total_seconds() / 86400)))
        fixed = G.fixed_fields(self.cond) | {getattr(conn, "label_source", None) or "", label_column or ""}
        asked = [G.name(f) for f in (group_by or [])]
        self.dropped = [f for f in asked if f in fixed and f]
        asked = [f for f in dict.fromkeys(asked) if f not in fixed][:GROUP_FIELDS]
        self.wide = _many_values(table, asked)       # an identifier, a job name: thousands of values say nothing
        self.asked = [f for f in asked if f not in self.wide]
        own = [f for f in _group_fields(table, fixed, label_column) if f not in self.asked]
        self.fields = (self.asked + own)[:GROUP_FIELDS if inside is None else FOLLOW_FIELDS]
        if not self.fields:
            raise ToolError(f"give group_by: the fields to compare (no field of few values is known for {table!r})")
        self.plan = G.Plan(self.measures, filter_clause=db_obj.backend not in NO_FILTER_CLAUSE)
        self.cursors: queue.Queue = queue.Queue()
        threads = 1 if GROUP_THREADS <= 1 else max(1, min(int(_setting("agent.compare_threads", GROUP_THREADS) or 1), 8))
        self.budget = max(5, int(_setting("agent.compare_seconds", GROUP_SECONDS) or GROUP_SECONDS))
        if inside is not None:
            self.cursors, self.budget = inside.cursors, inside.budget
        elif cursors is not None:
            self.cursors = cursors
        else:
            self.cursors.put(conn.cursor())
        if db_obj.backend == "osagg" and inside is None and cursors is None:     # a connection per thread (the queries of a field at a time)
            for _ in range(threads - 1):
                try:
                    more.append(_connection(db_obj, False, agent_query=True))
                    self.cursors.put(more[-1].cursor())
                except Exception:  # pylint: disable=broad-except   (one connection then)
                    break
        # every measure of a business date: its rows as they were at the end of the window on each day (what had
        # been released, started, ended by then), whatever their own time: a row not started yet, or started
        # after that time on an earlier day, is in the set the same way on every day
        self.as_of = bool(self.every and self.labels
                          and _earlier_dates(conn, self.labels, dt.timedelta(days=1), self.anchor) is not None)   # (a calendar)
        self.label_moved = False                     # an earlier day was read on its own business date
        self.now_scope = G.render(self.cond)
        today = _earlier_dates(conn, self.labels, dt.timedelta(0), self.anchor) if self.labels else None
        if today and today != "1 = 0":
            # the business date as its dates, for the window asked as for the earlier days: a past window gets its
            # own day's, and a date is pushed down where the label (a computed column) may not be
            self.now_scope = G.render(self.cond, self.moved, today)
        self.in_progress = t1 >= _local_now(conn) - dt.timedelta(minutes=IN_PROGRESS_MINUTES)
        self.explicit = against is not None          # reference days asked with the call, or the team's own
        try:
            self.refs = G.references(against if self.explicit else str(_setting("agent.compare_against", "") or ""))
        except G.GroupsError:
            if self.explicit:
                raise
            self.refs = []                           # (a setting nobody can read is no reference)
        self.began = inside.began if inside is not None else time.time()
        self.look_far = True                         # (False: a quick look at the last days, to try a scope)
        self.left_out: str | None = None             # a condition of the scope that holds only now, left out
        self.journey = 0.0                           # the usual time of the whole way through the stages (seconds)
        self.stages: list[str] = []                  # the table's time fields, in the order its rows reach them
        self.over: dict[str, str] = {}               # {measure: the rows it is over}
        self.errors: dict[str, str] = {}
        self.renamed: dict[str, dict[str, str]] = {}
        self.unfilled: set[str] = set()              # fields many rows do not have yet (the server of a row not started)
        self.chosen: list[tuple[dt.timedelta, str]] = []

    # ------------------------------------------------------------------ queries
    def run(self, sql: str, described: bool = False, built: bool = False) -> Any:
        """`built`: a query of this tool's own making (names checked, the scope parsed as one condition): not
        parsed once more."""
        cur = self.cursors.get()
        try:
            cur.execute(sql if built else _check_select(sql, GROUP_ROWS)[0])
            rows = cur.fetchall()
            return (rows, [c[0] for c in (getattr(cur, "description", None) or [])]) if described else rows
        finally:
            self.cursors.put(cur)

    def run_all(self, sqls: list[str]) -> list[Any]:
        if not self.more or len(sqls) < 2:
            return [self.run(q, built=True) for q in sqls]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(self.more) + 1) as pool:
            return list(pool.map(lambda q: self.run(q, built=True), sqls))

    def late(self) -> bool:
        return time.time() - self.began > self.budget

    def window(self, a: dt.datetime, b: dt.datetime, scope: str) -> str:
        parts = [f"({scope})"] if scope else []
        if not self.as_of:
            parts += [f'"{self.tf}" >= {self.G.lit(a)}', f'"{self.tf}" < {self.G.lit(b)}']
        return " AND ".join(parts) or "TRUE"

    def scope_at(self, back: dt.timedelta, dates: str | None = None) -> str:
        """The scope as SQL, `back` earlier: the dates it compares its time fields with moved back as much, its
        business date as `dates` (None: left out)."""
        cond = self.G.moved_back(self.cond, self.times, back.days) if self.moving else self.cond
        return self.G.render(cond, self.moved, dates or "TRUE")

    def scope_back(self, back: dt.timedelta) -> str | None:
        """The scope as it was `back` earlier (the business date of that day); None: that day is the same business
        date as the day after it (a weekend: not the day its rows were made)."""
        if not self.labels:
            return self.scope_at(back)
        dates = _earlier_dates(self.conn, self.labels, back, self.anchor)
        if not dates:
            return self.scope_at(back) if not self.as_of else None
        if self.as_of and dates == _earlier_dates(self.conn, self.labels, back - dt.timedelta(days=1), self.anchor):
            return None
        self.label_moved = True
        return self.scope_at(back, dates)

    def has_rows(self, back: dt.timedelta, scope: str) -> bool:
        n = self.run(f'SELECT COUNT(*) AS n FROM "{self.table}" WHERE {self.window(self.t0 - back, self.t1 - back, scope)}')
        return bool(n and n[0][0])

    def earlier(self) -> None:
        """The earlier windows that have data (a weekend, a day off: skipped), the latest first."""
        if self.inside is not None:                   # (the days of the comparison it is made for)
            self.chosen = [(back, sc) for back, sc in ((b, self.scope_back(b)) for b, _s in self.inside.chosen) if sc is not None]
        for k in range(1, (3 * self.days + 8) if self.look_far else 5) if self.inside is None and not self.chosen else []:
            if len(self.chosen) >= self.days:
                break
            back = k * self.period
            scope = self.scope_back(back)
            if scope is not None and self.has_rows(back, scope):
                self.chosen.append((back, scope))
        if len(self.chosen) < 2:
            if self.inside is None and self.has_rows(dt.timedelta(0), self.now_scope):
                raise NoEarlierDay("this scope has rows in the window asked and none on the earlier days: one of its "
                                   "conditions holds only now (a state that exists only while a row is in progress, a "
                                   "value that is new). Leave it out: the stages say how many rows wait at each")
            raise ToolError(f"no row matches this scope, in the window asked or in the {3 * self.days + 7} windows before "
                            f"it (on {self.tf!r}): check the table, the values of the scope and the dates (start: when "
                            "the rows looked at began, today's from midnight today; end: now)")
        self.wins = [(self.now_scope, self.t0, self.t1)] + [(sc, self.t0 - bk, self.t1 - bk) for bk, sc in self.chosen]

    def without_a_condition(self) -> list[tuple[str, str]]:
        """The scope without one of its conditions on a field, one at a time (the business date and the times kept)."""
        label_source = getattr(self.conn, "label_source", None) or ""
        label_column = getattr(self.conn, "label_column", None) or ""
        return self.G.one_left_out(self.cond, self.times | {label_source, label_column, self.moved or ""})

    def totals_of(self, windows: list[tuple[str, dt.datetime, dt.datetime]]) -> list[tuple[int, list]]:
        G = self.G
        got = self.run_all([f'SELECT {self.plan.select(G.lit(b))} FROM "{self.table}" WHERE {self.window(a, b, sc)}'
                            for sc, a, b in windows])
        return [(int(rows[0][0] or 0), self.plan.values(rows[0], b)) if rows else (0, [(0, None)] * len(self.measures))
                for rows, (_sc, _a, b) in zip(got, windows)]

    def by(self, field: str, windows: list[tuple[str, dt.datetime, dt.datetime]]) -> tuple[list[dict], list[dict]]:
        """A field's values in each window: their rows, and their measures."""
        G = self.G
        got = self.run_all([f'SELECT "{field}" AS g, {self.plan.select(G.lit(b))} FROM "{self.table}" WHERE '
                            f'{self.window(a, b, sc)} GROUP BY "{field}" ORDER BY 2 DESC LIMIT {GROUP_ROWS}'
                            for sc, a, b in windows])

        def value(r: tuple) -> str:
            return G.NONE if r[0] is None else str(r[0])

        return ([{value(r): int(r[1] or 0) for r in rows} for rows in got],
                [{value(r): self.plan.values(r[1:], b) for r in rows} for rows, (_sc, _a, b) in zip(got, windows)])

    # ------------------------------------------------------------------ the stages
    def find_stages(self) -> None:
        """The table's time fields in the order its rows reach them (their average on the latest earlier day, over
        the rows that reached them all), then the time from each to the next and the rows waiting at each."""
        G = self.G
        dates = [m.fields[0] for m in self.measures if m.kind == "reached"]
        if not self.every or len(dates) < 2:
            return
        try:
            if self.inside is not None and self.inside.stages:       # (the order is the table's)
                self.stages = list(self.inside.stages)
            else:
                bk, sc = self.chosen[0]
                every_date = " AND ".join(f'"{f}" IS NOT NULL' for f in dates)
                row = self.run("SELECT " + ", ".join(f'AVG("{f}") AS t{i}' for i, f in enumerate(dates))
                               + f' FROM "{self.table}" WHERE {self.window(self.t0 - bk, self.t1 - bk, sc)} AND {every_date}',
                               built=True)[0]
                self.stages = [f for _v, f in sorted((v, f) for v, f in zip(row, dates) if isinstance(v, dt.datetime))]
        except Exception:  # pylint: disable=broad-except   (no average of a time here: no stages)
            self.stages = []
        if len(self.stages) >= 2:
            pairs = list(zip(self.stages, self.stages[1:]))
            self.measures = self.measures + [G.Measure("lapse", pr) for pr in pairs] + [G.Measure("age", pr) for pr in pairs]
            self.plan = G.Plan(self.measures, self.plan.filter_clause)

    def rows_by_then(self) -> None:
        """A measure only some rows have yet (the duration of the rows that ended): the time field that says
        when they got it, so that each day's measure is over the rows that had it by the same time of day."""
        G = self.G
        if not (self.as_of or self.in_progress):
            return
        first = self.totals_of(self.wins[:1])[0]
        reached = {m.fields[0]: first[1][i][0] for i, m in enumerate(self.measures) if m.kind == "reached"}
        when: dict[int, str] = {}
        for i, m in enumerate(self.measures):
            have = first[1][i][0]
            if m.kind in ("ratio", "avg", "sum", "max", "min") and 0 < have < first[0]:
                near = [(abs(r - have), f) for f, r in reached.items() if abs(r - have) <= max(2, 0.01 * have)]
                if near:
                    when[i] = min(near)[1]
                    self.over[m.text()] = (f"the rows that had reached {when[i]} by {self.t1:%H:%M}, on each day alike "
                                           f"({have} of {first[0]} now)")
        if when:
            self.plan = G.Plan(self.measures, self.plan.filter_clause, when)

    # ------------------------------------------------------------------ reading and judging
    def read_fields(self) -> None:
        G = self.G
        self.totals = self.totals_of(self.wins)
        self.read: dict[str, list[dict]] = {}
        last = next((i for i, m in enumerate(self.measures) if m.kind == "reached" and self.stages
                     and m.fields[0] == self.stages[-1]), None)
        for f in self.fields:
            if (self.read or self.inside is not None) and self.late():      # (one field at least, whatever it takes,
                # unless the comparison is made for another's lead: the call's time is the first one's)
                self.errors[f] = (f"not compared: the call had run for {time.time() - self.began:.0f} s "
                                  "(agent.compare_seconds); ask it alone in group_by")
                continue
            try:
                counts, values = self.by(f, self.wins)
            except Exception as ex:  # pylint: disable=broad-except   (one field refused: the others answer)
                self.errors[f] = str(ex)[:300]
                continue
            if (self.as_of or self.in_progress) and self.every and (
                    G.follows_the_stage({g: (counts[0].get(g, 0), v[last][0]) for g, v in values[0].items()}, counts[1:])
                    if last is not None else G.state_like(counts[0], counts[1:])):
                self.errors[f] = ("not compared: its values change as a row advances (a status), and the earlier days "
                                  "are read as they ended")
                continue
            self.renamed[f] = G.replaced(counts[0], counts[1:])
            self._rename(f, values[1:])
            if self.as_of and G.unfilled(counts[0], counts[1:]):
                self.unfilled.add(f)
            self.read[f] = values
        if not self.read:
            raise ToolError("no field could be compared: " + "; ".join(f"{k}: {v}" for k, v in self.errors.items()))

    def _rename(self, field: str, windows: list[dict]) -> None:
        """A new version is read against the old one's days: the old value's history under the new name."""
        for new_value, old_value in (self.renamed.get(field) or {}).items():
            for w in windows:
                if old_value in w and new_value not in w:
                    w[new_value] = w.pop(old_value)

    def judge(self, totals: list[tuple[int, list]], read: dict[str, list[dict]]) -> tuple[list[tuple], list[str]]:
        """Each measure in total against the earlier windows given (the usual's days, or one reference day), and
        each field's values: [(measure, its total, {field: its summary})], and the time fields every row had
        reached on every one of those days."""
        G = self.G
        judged: list[tuple] = []
        in_all = (totals[0][0], [t[0] for t in totals[1:]])
        every_row: list[str] = []
        ci = next((i for i, m in enumerate(self.measures) if m.kind == "count"), None)
        sizes: dict[str, dict[str, float]] = {}      # {field: {value: its rows, now or usually}}
        for f, ws in read.items() if ci is not None else []:
            sizes[f] = {g: max(float(ws[0].get(g, [(0, None)] * (ci + 1))[ci][0]),
                               _median([float(w[g][ci][0]) for w in ws[1:] if g in w]) if any(g in w for w in ws[1:]) else 0.0)
                        for g in set().union(*[set(w) for w in ws])}
        for i, m in enumerate(self.measures):
            if m.kind == "reached" and all(t[1][i][0] == t[0] for t in totals):
                every_row.append(m.fields[0])        # every row had reached it, as on the earlier days
                continue
            overall = G.overall(totals[0][1][i], [t[1][i] for t in totals[1:]],
                                in_all if self.every and m.kind in ("avg", "sum", "max", "min")
                                and m.text() not in self.over else None)
            summaries = {f: G.summarize({g: v[i] for g, v in ws[0].items()},
                                        [{g: v[i] for g, v in w.items()} for w in ws[1:]], m.counting,
                                        scale=None if m.counting else overall.get("usual"),
                                        change=G.change(overall, m.counting),
                                        high=G.AGE_HIGH if m.kind == "age" else G.HIGH,
                                        above_only=m.kind in ("age", "lapse"), renamed=self.renamed.get(f),
                                        open_field=f in self.unfilled and m.text() not in self.over
                                        and m.kind in ("avg", "sum", "max", "min", "ratio"), sizes=sizes.get(f))
                         for f, ws in read.items()}
            judged.append((m, overall, summaries))
        for i, m in enumerate(self.measures):        # the rows at each stage at that time: reached one, not the next
            if m.kind != "age" or not any(t[1][i][0] for t in totals):
                continue

            def rows_of(v: tuple[int, float | None]) -> tuple[int, float]:
                return v[0], float(v[0])

            waiting = G.overall(rows_of(totals[0][1][i]), [rows_of(t[1][i]) for t in totals[1:]])
            judged.append((G.Measure("between", m.fields), waiting,
                           {f: G.summarize({g: rows_of(v[i]) for g, v in ws[0].items()},
                                           [{g: rows_of(v[i]) for g, v in w.items()} for w in ws[1:]], True,
                                           change=G.change(waiting, True), renamed=self.renamed.get(f),
                                           sizes=sizes.get(f))
                            for f, ws in read.items()}))
        return judged, every_row

    def since(self, name: str, field: str, value: str, usual: float | None, now: float | None) -> str | None:
        """Since when a value is off: the earlier days of the usual, the latest first, that were off the same way
        (a change of two days ago is "since" that day; none: new today)."""
        if not usual or now is None or field not in self.read:
            return None
        G = self.G
        up = now > usual
        days = []
        for (back, _sc), w in zip(self.chosen, self.read[field][1:]):
            v = self.figure(name, w[value]) if value in w else None
            if v is None:
                break
            if (up and v / usual >= G.HIGH) or (not up and v / usual <= G.LOW):
                days.append(self.t1 - back)
            else:
                break
        if not days:
            return "new on this day (as usual on the days before)"
        return (f"off since {days[-1]:%Y-%m-%d} (the {len(days)} day(s) before too)" if len(days) < len(self.chosen)
                else f"off on every earlier day compared (since {days[-1]:%Y-%m-%d} at least)")

    def figure(self, text: str, values: list[tuple[int, float | None]]) -> float | None:
        """A measure's figure (by its name in the result) in the values of one window or of one of its groups."""
        for i, m in enumerate(self.measures):
            if m.text() == text:
                return values[i][1]
            if m.kind == "age" and self.G.Measure("between", m.fields).text() == text:
                return float(values[i][0])
        return None

    # ------------------------------------------------------------------ the reference days
    def reference_window(self, ref: Any) -> tuple[dt.timedelta, str] | None:
        """The window of a reference day: the same clock window that many days back, on its own business date.
        "yesterday" with no row (a weekend) is the day before that has some; a week or a month ago with no row
        (a day off), the week before."""
        back = (self.t0.date() - ref.date).days if ref.kind == "date" else ref.days
        if back <= 0:
            return None
        tries = [back + i for i in range(7)] if ref.kind == "day" else [back, back + 7] if ref.kind == "week" else [back]
        for b in tries:
            delta = dt.timedelta(days=b)
            scope = self.scope_back(delta)
            if scope is not None and self.has_rows(delta, scope):
                return delta, scope
        return None

    def references(self, res: dict[str, Any], judged: list[tuple]) -> None:
        """The reference days (yesterday, a week ago, three weeks ago, months ago): each measure's figure then, in
        total and for the values that stand out; a measure that is as usual and far from an older reference (a
        change older than the days of the usual) is said, with where; the references asked with the call are each
        compared in full (what differs from that day, and where)."""
        G = self.G
        if not self.refs:
            return
        found: list[tuple[Any, dt.timedelta, str]] = []
        days: dict[str, str] = {}
        for ref in self.refs:
            got = None if self.late() else self.reference_window(ref)
            if got is None:
                days[ref.label] = "not read: the call's time was up" if self.late() else "no data that day"
                continue
            found.append((ref, got[0], got[1]))
            days[ref.label] = f"{self.t1 - got[0]:%Y-%m-%d}"
        res["reference_days"] = days
        # a day the question named and the data does not reach is said, and nothing is compared with it
        missing = [label for label, when in days.items() if when == "no data that day"] if self.explicit else []
        nothing = [f"no data on {', '.join(missing)}: nothing to compare with there"] if missing else []
        if not found:
            if nothing:
                res["against_reference_days"] = nothing
            return
        windows = [(sc, self.t0 - bk, self.t1 - bk) for _r, bk, sc in found]
        totals = self.totals_of(windows)
        then_fields: dict[str, list[dict]] = {}      # {field: its values on each reference day}

        def values_then(field: str) -> list[dict]:
            if field not in then_fields:
                _counts, values = self.by(field, windows)
                self._rename(field, values)
                then_fields[field] = values
            return then_fields[field]

        if self.every:
            listed = list(res["measures"][:4])
        else:                                        # the one measure asked: its total, the values of its best field
            m, overall, summaries = judged[0]
            best = G.localized(summaries)
            groups = [g for g in (res["fields"].get(best[0]) or {}).get("groups") or [] if g.get("unusual")] if best else []
            listed = [{"measure": m.text(), "now": overall.get("now"), "field": best[0] if best else None,
                       "values": groups, "_total": res["total"]}]
        weekly = {r.label for r, _b, _s in found if r.kind == "week" and r.days % 7 == 0}

        def pattern(now: Any, then: dict[str, Any], usual: Any) -> str:
            """Off against the last days and the same as on the same weekday of the earlier weeks: a weekly pattern."""
            same = [then[k] for k in weekly if isinstance(then.get(k), (int, float))]
            if not same or not isinstance(now, (int, float)) or not isinstance(usual, (int, float)) or not usual:
                return ""
            if not (now / usual >= G.HIGH or now / usual <= G.LOW):
                return ""
            if all(x and 1 / WEEKLY <= now / x <= WEEKLY for x in same):
                return " (as on the same weekday of the earlier weeks: what this weekday is, not a change)"
            return ""

        lines: list[str] = []
        for e in listed:                             # what stands out: its figure on each reference day
            name = e["measure"]
            then = {r.label: _rounded(self.figure(name, t[1])) for (r, _b, _s), t in zip(found, totals)}
            usual_total = (e.get("_total") or e).get("usual")
            (e.pop("_total", None) or e)["then"] = then
            line = (f"{name}: {_plain(e.get('now'))} now, " + ", ".join(f"{_plain(x)} {k}" for k, x in then.items())
                    + pattern(e.get("now"), then, usual_total))
            if e.get("field") and e.get("values") and not self.late():
                try:
                    per_day = values_then(e["field"])
                except Exception:  # pylint: disable=broad-except   (the totals of the reference days stand)
                    per_day = []
                for v in e["values"][:3] if per_day else []:
                    v["then"] = {r.label: (_rounded(self.figure(name, day[v["value"]])) if v["value"] in day else None)
                                 for (r, _b, _s), day in zip(found, per_day)}
                first = e["values"][0]
                if first.get("then"):
                    line = (f'{name}, {e["field"]} = {first["value"]}: {_plain(first.get("now"))} now, '
                            + ", ".join(f"{_plain(x)} {k}" for k, x in first["then"].items())
                            + pattern(first.get("now"), first["then"], first.get("usual")))
            lines.append(line)
        if lines or nothing:
            res["against_reference_days"] = lines[:4] + nothing
        # each reference on its own: what differs from that day, and where. The ones asked with the call: all that
        # differs. The team's own: only what the usual does not show (a change older than its days).
        said_already = {e["measure"] for e in listed} if self.every else set()
        stage_kinds = ("reached", "between", "lapse", "age")
        apart: list[dict[str, Any]] = []
        for k, (ref, _bk, _sc) in enumerate(found):
            if self.late() or (not self.explicit and len(apart) >= 2):
                break
            pair = [self.totals[0], totals[k]]
            in_total, _every = self.judge(pair, {})
            moved = [e for e in in_total if G.moved(e) and (self.explicit or (
                e[0].kind not in stage_kinds and e[0].text() not in said_already))]
            if not moved:
                continue
            read = {}
            for f in self.read:
                if self.late():
                    break
                try:
                    read[f] = [self.read[f][0], values_then(f)[k]]
                except Exception:  # pylint: disable=broad-except
                    continue
            names = {e[0].text() for e in moved}
            judged_then, _every = self.judge(pair, read)
            said = G.survey([e for e in judged_then if e[0].text() in names], shown=3)
            if said["measures"]:
                text = said["conclusion"].replace("What stands out, the strongest first: ", "")
                for usual, then in ((" its usual", " its value then"), (" usually", " then"), ("above usual", "above then"),
                                    ("below usual", "below then"), ("as usual", "as then")):
                    text = text.replace(usual, then)
                apart.append({"reference": f"{ref.label} ({days[ref.label]})", "what_differs": text[:900]})
        if apart:
            res["against"] = apart
            if not self.explicit:
                res["against_note"] = ("as usual against the last days, but not against these older days: a change "
                                       "older than the days of the usual (look at what changed since)")

    # ------------------------------------------------------------------ who else is on what stands out
    def present(self, a: dt.datetime, b: dt.datetime) -> str:
        """The rows that are there between a and b, whatever the scope. With stages: the rows whose life overlaps
        the window (their first stage before its end and within a day of its start, their last stage not before
        its start): the time field of a row moves as the row advances, and would count a day in progress and a
        day that ended differently. Without stages: the rows of the window by the time field."""
        G = self.G
        if len(self.stages) >= 2:
            first, last = self.stages[0], self.stages[-1]
            # (rows that came long before the window and never ended are left out: three times the usual way,
            # a day at least, a month at most)
            came = dt.timedelta(seconds=min(max(86400.0, PRESENT_WAYS * self.journey), 30 * 86400.0))
            return (f'"{first}" >= {G.lit(a - came)} AND "{first}" < {G.lit(b)} AND '
                    f'("{last}" IS NULL OR "{last}" >= {G.lit(a)})')
        return f'"{self.tf}" >= {G.lit(a)} AND "{self.tf}" < {G.lit(b)}'

    def outside(self, leads: list[tuple[str, list[str]]], waits_on: str | None = None) -> list[dict[str, Any]]:
        """The rows outside the scope that share the value the change is concentrated on (the other rows on that
        server, that queue, that pool: who else is on it), during the same window of the same days, by the
        scope's own fields: more of them than usual, and under which values. Said when there are more (and, for
        what the scope's rows wait on, also when there are not: nobody else took it). A query per window."""
        G = self.G
        label_source = getattr(self.conn, "label_source", None) or ""
        by = G.conditioned(self.cond, self.times | {self.moved or "", label_source})[:OUTSIDE_FIELDS]
        if self.inside is not None or (not by and not self.labels):       # no scope: nothing is outside it
            return []
        today = _earlier_dates(self.conn, self.labels, dt.timedelta(0), self.anchor) if self.labels else None
        windows = [(dt.timedelta(0), self.scope_at(dt.timedelta(0), today))] + list(self.chosen)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for field, values in sorted(leads, key=lambda lead: (lead[0] != waits_on, lead[0] in by)):
            values = [v for v in values if v != G.NONE][:3]
            if field in seen or not values or len(seen) >= OUTSIDE_LEADS or self.late():
                continue
            seen.add(field)
            group = [f for f in by if f != field]
            cols = ", ".join(f'"{f}"' for f in group)
            on = f'"{field}" IN (' + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"
            sqls = []
            for back, scope in windows:
                where = f"{on} AND {self.present(self.t0 - back, self.t1 - back)} AND NOT ({scope})"
                sqls.append(f'SELECT {cols + ", " if cols else ""}COUNT(*) AS n FROM "{self.table}" WHERE {where}'
                            + (f" GROUP BY {cols} ORDER BY {len(group) + 1} DESC LIMIT {GROUP_ROWS}" if cols else ""))
            got = self.run_all(sqls)
            rows = [{tuple(G.NONE if x is None else str(x) for x in r[:-1]): int(r[-1] or 0) for r in day} for day in got]
            found = G.others(field, values, group, rows[0], rows[1:])
            found["sql"] = sqls[0]
            if found["more"] or (group and field == waits_on):
                out.append(found)
        return out

    def there(self, waits_on: dict[str, Any] | None) -> dict[str, Any] | None:
        """The lead followed once. The rows wait at a stage where they do not usually, on one value of a field
        (the rows of one pool waiting for a slot): the same comparison over every row that has this value,
        whatever the scope's other conditions (its business date kept): what holds what they wait for, rows that
        last longer than usual there (and where), or rows that are not usually there."""
        if self.inside is not None or not waits_on or not self.every or \
                time.time() - self.began > FOLLOW_SHARE * self.budget or not _setting("agent.compare_follow", True):
            return None
        G = self.G
        field, values = waits_on["field"], waits_on["values"]
        label_column = getattr(self.conn, "label_column", None)
        label_source = getattr(self.conn, "label_source", None) or ""
        if not [f for f in G.conditioned(self.cond, self.times | {self.moved or "", label_source}) if f != field]:
            return None                               # (no other condition to leave out: the same rows again)
        where = f'"{field}" IN (' + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"
        if self.labels and label_column:
            where += f' AND "{label_column}" IN (' + ", ".join("'" + x.replace("'", "''") + "'" for x in self.labels) + ")"
        inner = _Comparison(self.conn, self.db, self.more, self.table, self.tf, self.t0, self.t1, where, None, "all",
                            self.days, [], inside=self)
        got = inner.result()
        on = f'"{field}" = {values[0]}' if len(values) == 1 else f'"{field}" in ({", ".join(values)})'
        out: dict[str, Any] = {"rows": f"every row with {on}" + (" of the business date" if self.labels else "")
                               + f", whatever the scope's other conditions (the scope's rows wait there at "
                                 f"{waits_on['stage']}): what holds it"}
        said = getattr(inner, "said", None) or {}
        if not said.get("lines"):
            out["what_stands_out"] = ["nothing: each measure is as usual among them, in total and on each field"]
            return out
        if said.get("first"):
            out["stages"] = (f"first clearly off there: {said['first']}"
                             + (f"; off on its own too: {'; '.join(said['own'][:2])}" if said.get("own") else ""))
        # what stands out there, the most telling first, each in a line (not what repeats another's values)
        out["what_stands_out"] = [re.sub(r" \(the same values for: [^)]*\)[^)]*\)", "", line)[:340] for line in said["lines"][:3]]
        for key in ("since_when", "related"):
            if got.get(key):
                out[key] = got[key][:1]
        return out

    # ------------------------------------------------------------------ the result
    def result(self) -> dict[str, Any]:
        G = self.G
        self.earlier()
        if self.as_of or self.every:
            self.find_stages()
        self.rows_by_then()
        self.read_fields()
        judged, every_row = self.judge(self.totals, self.read)
        self.journey = sum(float(e[1].get("usual") or 0.0) for e in judged if e[0].kind == "lapse")
        t0, t1 = self.t0, self.t1
        res: dict[str, Any] = {
            "table": self.table, "database": self.db.database_name, "database_id": self.db.id,
            "window": f"{t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} on {self.tf}",
            "compared_with": "the same window on " + ", ".join(f"{t0 - bk:%Y-%m-%d}" for bk, _s in self.chosen)
                             + " (the usual: their median)"}
        if self.as_of:
            res["window"] = (f"the rows of the business date ({', '.join(self.labels)}) as they were on "
                             f"{t1:%Y-%m-%d} at {t1:%H:%M}")
            res["compared_with"] = (f"the rows of each earlier day's own business date as they were at {t1:%H:%M} on "
                                    + ", ".join(f"{t1 - bk:%Y-%m-%d}" for bk, _s in self.chosen) + " (the usual: their median)")
        if self.every:
            res["fields_compared"] = list(self.read)
            spec = ((_catalog().get("indices") or {}).get(self.table) or {}).get("fields") or {}
            what = {f: str(spec[f]["description"])[:130] for f in self.stages
                    if isinstance(spec.get(f), dict) and spec[f].get("description")}
            res.update(G.survey(judged, over=self.over, stages=self.stages, what=what, every_row=every_row,
                                fixed=set(G.conditioned(self.cond, self.times | {self.moved or ""}))))
            waits_on = res.pop("waits_on", None)
            self.said = res.pop("said", None) or {}
            every_row = [f for f in every_row if f not in self.stages]
            if every_row:
                res.setdefault("as_usual", []).append("every row had reached " + ", ".join(every_row)
                                                      + " by then, as on the earlier days")
            leads = [(e["field"], [str(v["value"]) for v in e.get("values") or []])
                     for e in res["measures"][:3] if e.get("field")]
        else:
            waits_on = None
            m, overall, summaries = judged[0]
            res["measure"] = m.text()
            res["total"] = overall
            res["conclusion"] = G.conclusion(summaries)
            res["fields"] = {f: G.brief(sm, f in self.asked) for f, sm in summaries.items()}
            first = next(iter(self.read))
            res["sql_of_the_window"] = (f'SELECT "{first}", {m.select()} FROM "{self.table}" WHERE '
                                        + " AND ".join(x for x in (self.window(t0, t1, self.now_scope), m.not_null()) if x)
                                        + f' GROUP BY "{first}"')
            best = G.localized(summaries)
            leads = [(best[0], [str(g["value"]) for g in summaries[best[0]]["groups"] if g.get("unusual")])] if best else []
        when = []                                    # since when what stands out is off (the days of the usual)
        for e in (res.get("measures") or [])[:3] if self.every else []:
            if e.get("field") and e.get("values"):
                v = e["values"][0]
                said = self.since(e["measure"], e["field"], str(v["value"]), v.get("usual"), v.get("now"))
                if said:
                    v["since"] = said
                    when.append(f'{e["measure"]}, {e["field"]} = {v["value"]}: {said}')
        if not self.every and leads and leads[0][1]:
            groups = (res["fields"].get(leads[0][0]) or {}).get("groups") or []
            if groups and groups[0].get("unusual"):
                said = self.since(res["measure"], leads[0][0], str(groups[0]["value"]), groups[0].get("usual"), groups[0].get("now"))
                if said:
                    groups[0]["since"] = said
                    when.append(f'{leads[0][0]} = {groups[0]["value"]}: {said}')
        if when:
            res["since_when"] = when
        try:
            if self.inside is None:
                self.references(res, judged)
        except ToolError:
            raise
        except Exception:  # pylint: disable=broad-except   (the comparison with the usual stands)
            log.warning("supagent: the reference days of a comparison were not read", exc_info=True)
        try:                                         # who else is on what stands out, outside the scope
            shared = self.outside(leads, (waits_on or {}).get("field"))
        except Exception:  # pylint: disable=broad-except   (the comparison stands)
            log.debug("supagent: the rows outside the scope of a comparison were not read", exc_info=True)
            shared = []
        try:                                         # the lead followed once: every row on what the rows wait for
            held = self.there(waits_on)
        except Exception:  # pylint: disable=broad-except   (the comparison stands)
            log.debug("supagent: the rows on what a comparison's rows wait for were not read", exc_info=True)
            held = None
        if held:
            res["the_rows_there"] = held
        if shared:
            res["outside_the_scope"] = [o["line"] for o in shared]
            more = next((o for o in shared if o["more"]), None)
            if more:
                res["sql_outside_the_scope"] = more["sql"]
        related: list[dict] = []
        followed: set[str] = set()
        for f, values in leads:                      # what the related tables hold about the values that stand out
            if f in followed or not values or len(related) >= 2 or (self.inside is not None and self.late()):
                continue
            followed.add(f)
            try:
                related += _related(self.run, self.table, self.db, f, values, t1)
            except Exception:  # pylint: disable=broad-except
                log.debug("supagent: related records of %s not read", f, exc_info=True)
        if related:
            res["related"] = related[:2]
        notes = []
        if self.labels and self.label_moved:
            notes.append(f"the business date ({', '.join(self.labels)}) was read for each earlier day as that day's own")
        if self.left_out:
            notes.append(f"the condition {self.left_out} was left out: no row of the earlier days has it (a state that "
                         "exists only while a row is in progress, or a value that is new); the stages say how many rows "
                         "wait at each")
        if getattr(self, "not_used", None):
            notes.append(f"time_field {self.not_used} is computed from the business date, not a time of the rows: "
                         f"{self.tf} was used")
        if self.moving:
            notes.append(f"the date the scope compares {', '.join(self.moving)} with was moved back with each earlier "
                         "day (the same time of day)")
        if self.untimed:
            notes.append(f"the condition on {', '.join(self.untimed)} in where was left out: start and end give the time")
        if self.dropped:
            notes.append(f"not compared: {', '.join(self.dropped)} (the scope pins it to one value)")
        if self.wide:
            notes.append("not compared: " + ", ".join(f"{f} ({n:,} values: too many to say where a change is)"
                                                      for f, n in self.wide.items()))
        if self.as_of:
            notes.append(f"\"then\": at {t1:%H:%M}, on each day alike (the progress at that time of day against "
                         "usual). Rows pile up at a stage when the stage before released them late, or when the rows "
                         "of the stage after last longer than usual and hold its capacity: look at both before "
                         "naming a queue as the cause")
        elif t1 >= _local_now(self.conn) - dt.timedelta(minutes=IN_PROGRESS_MINUTES):
            notes.append("this window ends now: what is in progress is counted as it is at this moment, while the "
                         "earlier days are read as they ended. A state that only exists in progress (not started, "
                         "waiting, running) has no equivalent on the earlier days, and fewer rows \"done\" than usual "
                         "may only mean that the day is not over; to compare what was finished by this time of day, "
                         "call again with time_field = the field of the end time")
        past = _long_past(self.conn, t1)
        if past:
            res["warning"] = past
        if str(res.get("conclusion") or "").startswith("Nothing stands out") and not res.get("against") and \
                self.inside is None:                 # nothing to follow: said, so that the answer is not delayed
            res["next"] = ("Nothing is unusual in these rows: answer that now, with these figures (what is as usual, "
                           "and the stages when there are some). If the question says something is wrong here, say "
                           "that the data does not show it. Look at something else only if the question names it "
                           "(another table, other parts of the system).")
        if notes:
            res["note"] = "; ".join(notes)
        if self.errors:
            res["not_compared"] = self.errors
        # what it found first (the conclusion, the related records), then the figures behind it
        first_keys = ("table", "database", "database_id", "window", "warning", "conclusion", "next", "outside_the_scope",
                      "the_rows_there", "since_when", "related", "against_reference_days", "against", "against_note",
                      "stages", "measure", "total", "measures", "as_usual", "not_comparable_yet", "fields")
        res = {**{k: res[k] for k in first_keys if k in res}, **{k: v for k, v in res.items() if k not in first_keys}}
        return _fitted(res)


def _rounded(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


def _plain(v: Any) -> str:
    """A figure in a sentence."""
    if v is None:
        return "nothing"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.3g}"
    return str(v)


@mcp.tool
def compare_groups(table: str, start: str, end: str, group_by: list[str] | None = None, measure: str = "all",
                   where: str = "", days: int = 8, time_field: str | None = None,
                   database: str | int | None = None, against: list[str] | None = None) -> dict:
    """What is unusual in a table, and where is it concentrated? Its measures over the window start-end,
    compared with the same clock window of each of the previous `days` days that have data (their median: the
    usual): in total, then value by value for each field of few values (each field on its own). One call instead
    of one query per day, per measure and per field.

    measure: "all" (the default: the number of rows, each field against the field that holds its usual value,
    the average of each numeric field, the rows that reached each time field: which measure is off, and where),
    or one of "count" (rows), "avg(FIELD)", "sum(FIELD)", "max(FIELD)", "ratio(FIELD_A, FIELD_B)" = sum of A /
    sum of B, for its values in full.
    where: the scope as SQL conditions, without the time: "ENV" = 'PROD' AND "APP" IN ('A', 'B'); a business-date
    label (POSITION_LABEL = 'D-1') is moved to each earlier day by itself. start / end: local times
    ("2026-09-24 00:00"): from the start of what is looked at (today's rows: from midnight today) to now.
    against: reference days to compare with besides the usual, when the question names them or to see how old a
    change is: ["yesterday", "1 week ago", "3 weeks ago", "2 months ago", "2026-06-15"] (weeks and months: the
    same weekday; the team's own reference days are compared when none is given).
    time_field: the table's time field unless another is meant. group_by: the fields to look at first; the table's
    other fields of few values are compared too.
    The result says which measure is off, which field's values stand out and whether the change is concentrated
    on a few of them (where to look) or spread over all (look upstream), each figure on the reference days, and
    the latest records of the related tables (changes, alerts...) about what stands out. When the rows wait at a
    stage on one value (a pool, a queue), it also says who else is there: outside_the_scope (the rows outside
    the scope on that value, against usual) and the_rows_there (the same comparison over every row there)."""
    try:
        with _as_user():
            from superset.extensions import db as meta, security_manager

            from supagent.knowledge import groups as G

            t0, t1 = G.clock(start), G.clock(end)
            if t1 <= t0:
                raise ToolError("end must be after start")
            table = G.name(table)
            db_obj = _table_database(table, database)
            tf, not_used = _row_time_field(table, time_field)
            security_manager.raise_for_access(database=db_obj, sql=f'SELECT COUNT(*) FROM "{table}"', schema="default")
            more: list[Any] = []             # the other connections of this call (closed with it)
            with _db_connection(db_obj, extract=False) as conn, _closing(more):
                meta.session.commit()        # no connection of Superset's own pool is held while the queries run
                made = _Comparison(conn, db_obj, more, table, tf, t0, t1, where, group_by, measure, days, against)
                made.not_used = not_used
                try:
                    return made.result()
                except NoEarlierDay:
                    # a condition that holds only while a row is in progress (a status) leaves every earlier day
                    # empty: the scope without it, when that gives the earlier days their rows
                    for condition, rest in made.without_a_condition():
                        other = _Comparison(conn, db_obj, more, table, tf, t0, t1, rest, group_by, measure, days, against,
                                            cursors=made.cursors)
                        other.not_used, other.look_far = not_used, False
                        try:
                            other.earlier()
                        except ToolError:
                            continue
                        other.look_far, other.chosen = True, []
                        other.left_out = condition
                        return other.result()
                    raise
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        from supagent.knowledge.groups import GroupsError

        if isinstance(ex, GroupsError):
            text = str(ex)
            if "this table's own fields" in text:     # a subquery to another table: what this one has itself
                try:
                    from supagent.knowledge.groups import name as field_name

                    with _as_user():
                        own = _group_fields(field_name(table), set(), None)
                    text += (f"; {field_name(table)} has: " + ", ".join(own[:12])) if own else ""
                except Exception:  # pylint: disable=broad-except
                    pass
            return {"error": text}
        if type(ex).__name__ == "PushdownError":      # the scope as written cannot be run by the database
            return {"error": "where: the database cannot run this scope as written (" + str(ex)[:160].rstrip() + "...). "
                             "Write plain conditions joined with AND: \"FIELD\" = 'x', \"FIELD\" IN ('a', 'b'), the "
                             "business-date label as a condition of its own (= 'D-1'); no function, no OR between "
                             "different fields. Then call compare_groups again"}
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


LOG_ROWS = 150                   # compare_logs: lines read of each level in each third of each window
LOG_LEVELS_QUIET = ("info", "information", "informational", "debug", "trace", "notice", "verbose", "fine", "finer",
                    "finest", "config", "i", "d", "t", "dbg")
LOG_FIELDS = 6                   # fields of few values said for where a pattern comes from
LOG_CHECKED = 8                  # patterns counted line by line in the database (the others: from what was read)
LOG_SECONDS = 40.0               # ... while the call has run less than this (a big table: the others estimated)
LOG_OWN = 50                     # lines of a pattern counted read in each third (its names, its wordings)


def _message_field(table: str) -> str | None:
    """The field that holds a log line's text: a text field of many values (the dictionary), the one named like a
    message first."""
    from superset.extensions import db

    from supagent.models import KObject

    best: list[tuple[int, int, str]] = []
    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.gone_at.is_(None)):
        kind = str(o.data_type or "").lower()
        if kind not in ("text", "string", "keyword", "varchar"):
            continue
        named = bool(re.search(r"(^|_)(message|msg|log|line|text|event)(_|$)", o.name, re.I))
        card = int((o.stats or {}).get("cardinality") or 0)
        if kind == "text" or named:
            best.append((int(named) + int(kind == "text"), card, o.name))
    return max(best)[2] if best else None


def _level_field(table: str) -> str | None:
    """The field that holds a log line's level (INFO, WARN, ERROR...): named like one, of few values."""
    from superset.extensions import db

    from supagent.models import KObject

    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent == table, KObject.gone_at.is_(None)):
        if re.fullmatch(r"(log_?)?(level|severity|loglevel|lvl|priority_name)", o.name, re.I) and \
                int((o.stats or {}).get("cardinality") or 0) <= 12:
            return o.name
    return None


@mcp.tool
def compare_logs(table: str, start: str, end: str, where: str = "", days: int = 8, levels: list[str] | None = None,
                 against: list[str] | None = None, time_field: str | None = None, message_field: str | None = None,
                 database: str | int | None = None) -> dict:
    """What do the logs say that they do not usually? The lines of a log table (application logs, batch logs,
    events) over the window start-end, grouped into patterns (their numbers, times, ids and names left out), each
    counted against the same window of the previous `days` days that have data: the patterns that are new, far
    more frequent than usual, rare (there on few earlier days), or as frequent with numbers far from their usual
    (a slow write of 40 s where it is 8 s), most lines first, with where their lines come from (the host, the
    application, the logger, the names they hold), when (the first and the last line) and an example; what is
    gone; and the patterns of every day (a warning that comes every night is no finding). One call instead of
    reading lines.

    where: the scope as SQL conditions, without the time: "APPLICATION" IN ('A', 'B'), "HOST" = 'srv-1' (a
    business-date label is moved to each earlier day by itself). levels: the levels to read (default: all but
    INFO and DEBUG, when the table has a level field). against: reference days to count each pattern on too
    ("1 week ago", "4 weeks ago", "2026-06-15"). start / end: local times ("2026-09-24 00:00")."""
    began = time.time()
    try:
        with _as_user():
            from superset.extensions import db as meta, security_manager

            from supagent.knowledge import groups as G
            from supagent.knowledge import logs as L

            t0, t1 = G.clock(start), G.clock(end)
            if t1 <= t0:
                raise ToolError("end must be after start")
            table = G.name(table)
            db_obj = _table_database(table, database)
            tf, _computed = _row_time_field(table, time_field)
            msg = G.name(message_field) if message_field else _message_field(table)
            if not msg:
                raise ToolError(f"no field of {table!r} holds the text of a line: give message_field")
            lvl = _level_field(table)
            security_manager.raise_for_access(database=db_obj, sql=f'SELECT COUNT(*) FROM "{table}"', schema="default")
            days = max(2, min(int(days or 8), GROUP_DAYS))
            try:
                refs = G.references(against if against is not None else str(_setting("agent.compare_against", "") or ""))
            except G.GroupsError:
                if against is not None:
                    raise
                refs = []                            # (a setting nobody can read is no reference)
            with _db_connection(db_obj, extract=False) as conn:
                meta.session.commit()
                label_column = getattr(conn, "label_column", None) if db_obj.backend == "osagg" else None
                cond, labels = G.scope(where, label_column)
                times = G.time_columns(cond, _time_fields(table, tf))
                cond, moving, _untimed = G.timed(cond, times)
                anchor = min(t1, _label_now(conn)) if label_column else t1
                label_source = getattr(conn, "label_source", None) if label_column else None
                if labels and _earlier_dates(conn, labels, dt.timedelta(0), anchor) == "1 = 0":
                    raise ToolError(f'where: "{label_column}" = {", ".join(labels)}: not a business-date label (D, '
                                    "D-1, D-2, W-1...); a state or another value belongs to its own field")
                moved = label_column                 # the column whose condition moves with the day
                if not labels and label_source:      # a business date written as a date: its label, moved too
                    pinned = G.literals(cond, label_source)
                    labels = _labels_of_dates(conn, pinned, anchor) if pinned else []
                    moved = label_source if labels else label_column
                where_fields = [f for f in _group_fields(table, G.fixed_fields(cond), label_column)
                                if f not in (lvl, msg, label_source, tf)][:LOG_FIELDS]
                cur = conn.cursor()

                def run(sql: str) -> list:
                    cur.execute(sql)
                    return cur.fetchall()

                def scope_at(back: dt.timedelta) -> str:
                    dates = _earlier_dates(conn, labels, back, anchor) if labels else None
                    c = G.moved_back(cond, times, back.days) if moving else cond
                    sql = G.render(c, moved, dates if dates else "TRUE") if cond is not None else ""
                    return sql or "TRUE"

                def window(back: dt.timedelta, a: dt.datetime | None = None, b: dt.datetime | None = None) -> str:
                    a = (a or t0) - back
                    b = (b or t1) - back
                    return f'({scope_at(back)}) AND "{tf}" >= {G.lit(a)} AND "{tf}" < {G.lit(b)}'

                def quote(v: Any) -> str:
                    return "'" + str(v).replace("'", "''") + "'"

                # the earlier windows with lines (a holiday, a weekend without runs: skipped), then the reference days
                windows: list[dt.timedelta] = [dt.timedelta(0)]
                for k in range(1, 3 * days + 8):
                    if len(windows) > days:
                        break
                    back = dt.timedelta(days=k)
                    n = run(f'SELECT COUNT(*) AS n FROM "{table}" WHERE {window(back)}')
                    if n and n[0][0]:
                        windows.append(back)
                if len(windows) < 3:
                    raise ToolError("no earlier day with lines to compare with: check the table, the scope and the dates")
                usual_days = len(windows) - 1
                ref_windows: list[tuple[str, dt.timedelta]] = []
                for r in refs:
                    back = dt.timedelta(days=r.days) if r.days else dt.timedelta(days=(t0.date() - r.date).days)
                    if back.days >= 1:
                        ref_windows.append((r.label if r.date else f"{r.label} ({t0 - back:%Y-%m-%d})", back))
                every = windows + [b for _t, b in ref_windows if b not in windows]
                # the levels (of the window asked and of the day before: a level gone is read too), the quiet ones
                # (INFO, DEBUG) left out unless asked
                found_levels: dict[str, int] = {}
                if lvl:
                    for back in windows[:2]:
                        for r in run(f'SELECT "{lvl}", COUNT(*) AS n FROM "{table}" WHERE {window(back)} GROUP BY "{lvl}"'):
                            if r[0] is not None:
                                found_levels[str(r[0])] = found_levels.get(str(r[0]), 0) + (int(r[1] or 0) if back == windows[0] else 0)
                    wanted = [x for x in (levels or [k for k in found_levels if k.lower() not in LOG_LEVELS_QUIET])
                              if x in found_levels] or list(found_levels)
                else:
                    wanted = [None]
                cols = ", ".join(f'"{c}"' for c in [msg, *where_fields, *([lvl] if lvl else [])])
                names = [msg, *where_fields, *([lvl] if lvl else []), "_t"]
                thirds = [(t0 + (t1 - t0) * i / 3, t0 + (t1 - t0) * (i + 1) / 3) for i in range(3)]
                found: dict[str, dict] = {}
                per_level: dict[str, list[float]] = {}
                for level in wanted:
                    on = f' AND "{lvl}" = {quote(level)}' if lvl else ""
                    samples, totals = [], []
                    for back in every:
                        total = run(f'SELECT COUNT(*) AS n FROM "{table}" WHERE {window(back)}{on}')
                        totals.append(float(total[0][0] or 0) if total else 0.0)
                        rows: list[dict] = []
                        for a, b in thirds:      # each third of the window: the lines are not all of its end
                            got = run(f'SELECT {cols}, "{tf}" FROM "{table}" WHERE {window(back, a, b)}{on} '
                                      f'ORDER BY "{tf}" DESC LIMIT {LOG_ROWS}')
                            rows += [dict(zip(names, r)) for r in got]
                        samples.append(rows)
                    per_level[str(level)] = totals
                    L.merge(found, L.read(samples, totals, msg, where_fields, level=str(level) if lvl else None))
                # the patterns that will be said, counted line by line in the database (what was read of each window
                # is a part of it: a burst at one time of a window can crowd the other patterns out of what is read)
                dates = [f"{t0 - b:%Y-%m-%d}" for b in windows[1:]]
                said = sorted(found.items(), key=lambda kv: (L.judge(kv[1], usual_days)["verdict"] == "as usual",
                                                             -kv[1]["counts"][0]))
                levels_on = (f' AND "{lvl}" IN ({", ".join(quote(x) for x in wanted)})' if lvl else "")
                checked, used, stopped, uncounted = 0, set(), False, ""
                for key, p in said:
                    if checked >= LOG_CHECKED:
                        break
                    if time.time() - began > LOG_SECONDS:
                        stopped = True
                        break
                    piece = L.fragment(key)
                    if piece is None or piece in used or max(p["counts"]) < L.NEW_ERRORS:
                        continue
                    used.add(piece)
                    checked += 1
                    exact, edges = [], None
                    try:
                        for back in every:
                            if time.time() - began > LOG_SECONDS:    # (checked before each query: a big table)
                                stopped = True
                                break
                            first = ', MIN("{0}") AS a, MAX("{0}") AS b'.format(tf) if back == windows[0] else ""
                            got = run(f'SELECT COUNT(*) AS n{first} FROM "{table}" WHERE {window(back)}{levels_on} '
                                      f'AND "{msg}" LIKE {quote("%" + piece + "%")}')
                            exact.append(float(got[0][0] or 0) if got else 0.0)
                            if first and got and len(got[0]) == 3:
                                edges = got[0][1:]
                    except Exception as ex:  # pylint: disable=broad-except   (a text field the database cannot filter so)
                        uncounted = f"{type(ex).__name__}: {str(ex)[:160]}"
                        break
                    if stopped:                              # a pattern counted on part of its windows: not used
                        break
                    if exact[0] or any(exact[1:]):
                        p["counts"] = exact
                        p["exact"] = True
                        if edges and all(isinstance(e, dt.datetime) for e in edges):    # (the window's first and last)
                            p["first"], p["last"] = edges
                    if exact[0]:
                        _own_lines(p, key, run, f"{window(windows[0])}{levels_on} AND \"{msg}\" LIKE {quote('%' + piece + '%')}",
                                   table, msg, tf, where_fields, [window(windows[0], a, b) for a, b in thirds],
                                   f"{levels_on} AND \"{msg}\" LIKE {quote('%' + piece + '%')}")
                if ref_windows:
                    for p in found.values():
                        p["on"] = {text: round(p["counts"][every.index(b)]) for text, b in ref_windows}
                res = L.survey(found, usual_days, where_fields, dates, refs=against is not None)
                out = {"table": table, "database": db_obj.database_name, "database_id": db_obj.id,
                       "window": f"{t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} on {tf}",
                       "conclusion": res["conclusion"],
                       "levels": {str(k): {"now": int(v[0]), "usual": statistics.median(v[1:usual_days + 1])}
                                  for k, v in per_level.items() if k != "None"},
                       "patterns": res["patterns"],
                       "compared_with": "the same window on " + ", ".join(dates) + " (the usual: their median)",
                       **({"against": [t for t, _b in ref_windows]} if ref_windows else {}),
                       "read": f"the lines of the levels {', '.join(map(str, wanted))}" if lvl else "every line",
                       "note": (f"the patterns are counted from up to {3 * LOG_ROWS} lines of each level read in each "
                                "window (spread over its three thirds), scaled to the lines it has; the ones said "
                                "\"counted: line by line\" are exact")}
                if stopped:
                    out["note"] += f"; the counting line by line stopped after {LOG_SECONDS:g} s (a big table)"
                if uncounted:
                    out["note"] += f"; no counting line by line: the database cannot filter {msg} by a piece of text ({uncounted})"
                past = _long_past(conn, t1)
                if past:
                    out["warning"] = past
                return _fitted_logs(out)
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        from supagent.knowledge.groups import GroupsError

        if isinstance(ex, GroupsError):
            return {"error": str(ex)}
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


def _own_lines(p: dict[str, Any], key: str, run: Any, where_now: str, table: str, msg: str, tf: str,
               fields: list[str], parts: list[str], on: str) -> None:
    """A pattern counted line by line, where its lines come from counted too (the lines read of a window are its
    latest of each third: a burst at the end of a third leans them), and its names and wordings from its own
    lines over the window."""
    from collections import Counter, defaultdict

    from supagent.knowledge import logs as L

    counted = {}
    for f in fields:
        try:
            got = run(f'SELECT "{f}", COUNT(*) AS n FROM "{table}" WHERE {where_now} GROUP BY "{f}"')
        except Exception:  # pylint: disable=broad-except   (a field that cannot be grouped: from what was read)
            continue
        counted[f] = Counter({str(r[0]): int(r[1] or 0) for r in got if r[0] not in (None, "")})
    if counted:
        p["where"] = defaultdict(Counter, {**p["where"], **counted})
        p["where_counted"] = set(counted)
    names, wordings = defaultdict(Counter), Counter()
    for part in parts:
        try:
            got = run(f'SELECT "{msg}", "{tf}" FROM "{table}" WHERE {part}{on} ORDER BY "{tf}" DESC LIMIT {LOG_OWN}')
        except Exception:  # pylint: disable=broad-except
            return
        for m, _t in got:
            s = L.shape(str(m or ""))
            if s.key != key:
                continue
            wordings[s.wording] += 1
            for k, name in enumerate(s.names):
                names[k][name] += 1
    if wordings:
        p["names"], p["wordings"], p["read"] = names, wordings, sum(wordings.values())


def _fitted_logs(res: dict[str, Any], chars: int = GROUP_CHARS) -> dict[str, Any]:
    """A comparison of logs within the room of a tool result: fewer wordings and examples first, then the patterns
    of every day, then the least of the others."""
    def size() -> int:
        return len(json.dumps(res, default=str))

    for cut in ("wordings", "example", "usual", "where", "patterns"):
        if size() <= chars:
            break
        pats = res.get("patterns") or []
        for p in pats:
            if cut == "wordings":
                p["wordings"] = p.get("wordings", [])[:1]
            elif cut == "example":
                p.pop("example", None)
            elif cut == "where" and p.get("verdict") == "as usual":
                p.pop("where", None)
        if cut == "usual":
            res["patterns"] = [p for p in pats if p.get("verdict") != "as usual"] or pats[:1]
        elif cut == "patterns":
            res["patterns"] = pats[:4]
    return res


def _long_past(conn: Any, end: dt.datetime) -> str | None:
    """A window that ended days ago, said so: a date written one digit off (the 4th for the 8th at 04:40) reads
    another day's incident as today's."""
    now = _local_now(conn)
    if now - end < dt.timedelta(hours=PAST_HOURS):
        return None
    days = (now - end).total_seconds() / 86400
    return (f"this window ended {days:.0f} day(s) before now (now is {now:%A %Y-%m-%d %H:%M}): if the question is about "
            "today, call again with end = now")


def _fitted(res: dict[str, Any], chars: int = GROUP_CHARS) -> dict[str, Any]:
    """A comparison's result within the room of a tool result: what repeats the conclusion goes first (the other
    fields a measure is also seen on, the fourth measure's figures, the oldest related records), then what the
    conclusion already says in words."""
    def size() -> int:
        return len(json.dumps(res, default=str))

    held = res.get("the_rows_there") or {}
    for cut in ("also_on", "then", "sql", "there", "measures", "latest", "against", "fields", "there related",
                "one record", "one measure", "there short", "reference lines", "stages", "notes"):
        if size() <= chars:
            break
        if cut == "also_on":
            for e in res.get("measures") or []:
                e.pop("also_on", None)
        elif cut == "then":                           # (the lines of against_reference_days say them)
            for e in res.get("measures") or []:
                for v in e.get("values") or []:
                    v.pop("then", None)
        elif cut == "there":                          # (the lead followed: its records, fewer)
            for r in held.get("related") or []:
                r.pop("sql", None)
                r["latest"] = (r.get("latest") or [])[:2]
        elif cut == "against":
            for e in res.get("against") or []:
                e["what_differs"] = e["what_differs"][:400]
        elif cut == "sql":
            res.pop("sql_outside_the_scope", None)
            for r in res.get("related") or []:
                r.pop("sql", None)
        elif cut == "measures" and len(res.get("measures") or []) > 2:
            res["measures"] = res["measures"][:2]
        elif cut == "latest":
            for r in res.get("related") or []:
                r["latest"] = (r.get("latest") or [])[:2]
        elif cut == "fields":
            for f, sm in (res.get("fields") or {}).items():
                sm["groups"] = (sm.get("groups") or [])[:4]
        elif cut == "there related":
            for r in held.get("related") or []:
                r["latest"] = (r.get("latest") or [])[:1]
        elif cut == "one record":
            for r in res.get("related") or []:
                r["latest"] = (r.get("latest") or [])[:1]
        elif cut == "one measure" and len(res.get("measures") or []) > 1:
            res["measures"] = res["measures"][:1]
        elif cut == "there short" and held.get("what_stands_out"):
            held["what_stands_out"] = [line[:220] for line in held["what_stands_out"][:2]]
        elif cut == "reference lines" and res.get("against_reference_days"):
            lines = res["against_reference_days"]
            res["against_reference_days"] = lines[:2] + [x for x in lines[2:] if x.startswith("no data on ")]
        elif cut == "stages" and res.get("stages"):
            res["stages"] = [line[:260] for line in res["stages"]]
        elif cut == "notes":                          # (what is said last, in fewer words)
            for key in ("note", "compared_with"):
                if res.get(key):
                    res[key] = str(res[key])[:300]
    return res


@mcp.tool
def system_links(names: list[str]) -> dict:
    """What the team's system map says about parts of the system, by their exact names (an application, a
    server, a pool, a service, a feed, a family...): what each is, what it is part of and consists of, what it
    depends on, runs on, reads and calls, what depends on it, and where its kind is in the data (fields, metric
    labels, joins). In an investigation: from the part that looks wrong to what it depends on, and to what
    depends on it."""
    try:
        with _as_user():
            from supagent.knowledge.brief import links_of

            res = links_of([str(n) for n in (names or [])][:8])
            if not res.get("parts"):
                res["note"] = ("none of these names is a value of the team's categories (Data dictionary, "
                               "Categories): search_knowledge may know them")
            return res
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def list_alerts(database: str | int | None = None) -> dict:
    """Alerts firing or pending now in the metrics backend (Mimir ruler / Prometheus) and the
    alerting rules defined there."""
    try:
        with _as_user():
            db_obj = _metrics_database(database)
            conn = _promagg_connection(db_obj)
            try:
                try:
                    alerts = conn.client.alerts()
                except Exception as ex:  # pylint: disable=broad-except
                    return {"error": f"alerts are not available on this backend: {str(ex)[:300]}"}
                try:
                    groups = conn.client.rules()
                except Exception:  # pylint: disable=broad-except
                    groups = []
                rules = [{"group": g.get("name"), "name": r.get("name"), "query": r.get("query"),
                          "state": r.get("state"), "health": r.get("health")}
                         for g in groups for r in g.get("rules") or [] if r.get("type") == "alerting"]
                return {"database": db_obj.database_name,
                        "alerts": [{"name": a.get("labels", {}).get("alertname"), "state": a.get("state"),
                                    "labels": a.get("labels"), "since": a.get("activeAt"), "value": a.get("value"),
                                    "summary": (a.get("annotations") or {}).get("summary")} for a in alerts],
                        "rules": rules}
            finally:
                conn.close()
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


def _metric_profile(conn: Any, name: str, labels: list[str]) -> dict[str, list[str]]:
    """Values of the small labels of a metric (index lookups only), cached for a day."""
    cache = os.path.join(_export_dir(), ".profile.json")
    try:
        with open(cache, encoding="utf-8") as fh:
            all_profiles = json.load(fh)
    except (OSError, ValueError):
        all_profiles = {}
    key = f"metric:{name}"
    hit = all_profiles.get(key)
    if hit and time.time() - hit.get("at", 0) < PROFILE_TTL:
        return hit["values"]
    now = conn.now_ms()
    t0 = now - (conn.schema_window_ms or 7 * 86_400_000)
    out: dict[str, list[str]] = {}
    sel = '{__name__="%s"}' % name
    for lb in labels:
        try:
            vals = conn.client.label_values(lb, [sel], t0, now, limit=31)
        except Exception:  # pylint: disable=broad-except
            continue
        if len(vals) <= 30:
            out[lb] = vals
    rng = _metric_range(conn, sel, t0, now)
    if rng:
        out["__range__"] = [f"{conn.zone.local(t):%Y-%m-%d %H:%M}" for t in rng]
    all_profiles[key] = {"at": time.time(), "values": out}
    os.makedirs(_export_dir(), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(all_profiles, fh, default=str)
    return out


def _metric_range(conn: Any, sel: str, start: int, end: int) -> tuple[int, int] | None:
    """About when the data of a metric starts and ends (to the hour or the index block): the
    series index bisected, no samples read."""
    def exists(a: int, b: int) -> bool:
        try:
            return bool(conn.client.series([sel], a, b, limit=1))
        except Exception:  # pylint: disable=broad-except
            return False

    if not exists(start, end):
        return None
    lo, hi = start, end
    while hi - lo > 3_600_000:
        mid = (lo + hi) // 2
        if exists(mid, hi):
            lo = mid
        else:
            hi = mid
    last = hi
    lo, hi = start, last
    while hi - lo > 3_600_000:
        mid = (lo + hi) // 2
        if exists(lo, mid):
            hi = mid
        else:
            lo = mid
    return lo, last


def _describe_metrics(cat: dict[str, Any], words: set[str], only: str | None) -> list[str]:
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db

    spec = cat.get("metrics") or {}
    try:
        database = _metrics_database(None)
    except ToolError:
        return []
    conn = _promagg_connection(database)
    try:
        names = conn.list_tables()
        tables = spec.get("tables") or {}
        if only:
            if only not in names:
                return []
            chosen = [only]
        else:
            scored = []
            for n in names:
                t = tables.get(n) or {}
                text = f"{n} {t.get('description', '')} {' '.join(map(str, t.get('synonyms') or []))}"
                sc = len(words & _tokens(text.replace('_', ' '))) if words else (1 if n in tables else 0)
                scored.append((sc, n))
            chosen = [n for sc, n in sorted(scored, key=lambda x: -x[0]) if sc > 0][:12]
        out = [f"\nMetrics (Prometheus / Mimir) on database id {database.id} \"{database.database_name}\": "
               f"{(spec.get('description') or '').strip()}",
               "  SQL: one table per metric; columns ts (sample time; in GROUP BY use DATE_TRUNC('hour', ts)), one column "
               "per label, value (sample), and for counters rate (per second) / increase (count) per series and time "
               "bucket: SUM(rate) GROUP BY node = sum by (node) (rate(...)). Functions: RATE(value), INCREASE(value), "
               "AVG_OVER_TIME(value), MAX_OVER_TIME(value), QUANTILE_OVER_TIME(0.95, value), on *_bucket tables "
               "HISTOGRAM_QUANTILE(0.95, SUM(RATE(value))); FILTER (WHERE label = 'x'). Always filter ts on a time range. "
               "Series with samples in a window: GROUP BY label with COUNT(*) (SELECT DISTINCT label reads the label "
               "index, like a Grafana variable, and may list series that stopped earlier).",
               f"  health checks (check_health): {', '.join(cat.get('checks') or {}) or 'none'}"]
        tenants = conn.client.tenants() if hasattr(conn.client, "tenants") else []
        if len(tenants) > 1:
            out.append(f"  tenants: {', '.join(tenants)} (Mimir tenant federation): every series has the label "
                       "__tenant_id__; keep it in GROUP BY (__tenant_id__, node) so that the same name in two "
                       "tenants stays apart, filter with WHERE __tenant_id__ = '...'.")
        for rel in spec.get("label_relationships") or []:
            out.append(f"  relationship: metric label {rel['label']} = {rel['index']}.\"{rel['field']}\" "
                       f"({rel.get('description', '')})")
        for n in chosen:
            meta = conn.table_meta(n)
            if meta is None:
                continue
            t = tables.get(n) or {}
            ds = db.session.query(SqlaTable).filter_by(table_name=n, database_id=database.id).first()
            unit = t.get("unit") or meta.unit
            out.append(f"  - {n} ({meta.kind}{', ' + unit if unit else ''}): {t.get('description') or meta.help or ''}"
                       + (f" [dataset id {ds.id}]" if ds else ""))
            vals = _metric_profile(conn, n, meta.labels)
            if vals.get("__range__"):
                out.append(f"      data from about {vals['__range__'][0]} to {vals['__range__'][1]}")
            labels = t.get("labels") or {}
            parts = []
            for lb in meta.labels:
                p = f"{lb}"
                if labels.get(lb):
                    p += f" ({labels[lb]})"
                if vals.get(lb):
                    p += ": " + ", ".join(vals[lb][:12]) + (" ..." if len(vals[lb]) > 12 else "")
                parts.append(p)
            out.append("      labels: " + "; ".join(parts))
            for q in t.get("sql") or []:
                out.append(f"      e.g. {q}")
            if ds is not None and ds.metrics:
                out.append("      saved metrics: " + "; ".join(f"{m.metric_name} = {m.expression}" for m in ds.metrics))
        if not only and len(chosen) < len(names):
            out.append(f"  other metrics ({len(names) - len(chosen)}): " + ", ".join(n for n in names if n not in chosen)[:1500])
        return out
    finally:
        conn.close()


def push_metrics() -> None:
    """catalog.yaml "metrics" -> one Superset dataset per metric that has saved metrics (time
    column ts), with the descriptions and the saved metrics (created or updated)."""
    with _as_user():
        from superset.connectors.sqla.models import SqlaTable, SqlMetric
        from superset.extensions import db

        spec = _catalog().get("metrics") or {}
        database = _database(spec.get("database"), backend="promagg")
        for name, t in (spec.get("tables") or {}).items():
            t = t or {}
            saved = t.get("saved_metrics") or {}
            if not saved:
                continue
            tbl = db.session.query(SqlaTable).filter_by(table_name=name, database_id=database.id).first()
            created = tbl is None
            if created:
                tbl = SqlaTable(table_name=name, database=database, schema="default")
                db.session.add(tbl)
                db.session.flush()
                tbl.fetch_metadata()
            tbl.main_dttm_col = "ts"
            for col in tbl.columns:
                if col.column_name == "ts":
                    col.is_dttm = True
            if t.get("description"):
                tbl.description = str(t["description"]).strip()
            existing = {m.metric_name: m for m in tbl.metrics}
            for mname, m in saved.items():
                metric = existing.get(mname)
                if metric is None:
                    metric = SqlMetric(metric_name=mname)
                    tbl.metrics.append(metric)
                metric.expression = m["sql"]
                metric.description = m.get("description")
                metric.verbose_name = m.get("label")
            db.session.commit()
            print(f"dataset {tbl.id} {name} ({'created' if created else 'updated'}): {', '.join(saved)}")


# --------------------------------------------------------------------------- #
# Scheduled reports (REST API)
# --------------------------------------------------------------------------- #
def _find_id(kind: str, title: str) -> int | None:
    """A dashboard (title) or chart (name) the user may see: exact name first, then partial."""
    from superset.extensions import db, security_manager
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    model, field = (Dashboard, Dashboard.dashboard_title) if kind == "dashboard" else (Slice, Slice.slice_name)
    for cond in (field == title, field.ilike(f"%{title}%")):
        for row in db.session.query(model).filter(cond).order_by(model.id).limit(20):
            try:
                security_manager.raise_for_access(**({"dashboard": row} if kind == "dashboard" else {"chart": row}))
            except Exception:  # pylint: disable=broad-except
                continue
            return row.id
    return None


class ReportRequest(BaseModel):
    name: str = Field(description="Report name (unique)")
    dashboard_id: int | None = Field(default=None, description="Dashboard to send (or dashboard_title, chart_id, chart_name)")
    dashboard_title: str | None = Field(default=None, description="Dashboard title, when the id is not known")
    chart_id: int | None = Field(default=None, description="Chart to send")
    chart_name: str | None = Field(default=None, description="Chart name, when the id is not known")
    report_format: Literal["PNG", "PDF", "CSV", "TEXT"] = Field(
        default="PNG", description="PNG: screenshot in the e-mail body; PDF: attachment; CSV: chart "
                                   "data attached (charts only); TEXT: chart data as a table in the body")
    crontab: str = Field(default="0 8 * * 1-5", description="Schedule, cron syntax (Mon-Fri 08:00)")
    timezone: str = Field(default="Europe/Paris")
    email_recipients: list[str] = Field(description="E-mail addresses")
    email_subject: str | None = Field(default=None, description="E-mail subject (default: report name)")
    description_html: str | None = Field(
        default=None, description="Text at the top of the e-mail. Allowed HTML: p, b, strong, i, em, "
                                  "ul, ol, li, br, a, blockquote, code, div, table (no headings)")
    custom_width: int | None = Field(default=None, description="Screenshot width in px (e.g. 1600)")
    active: bool = True


def _query_context(params: dict[str, Any], ds_id: int) -> dict[str, Any]:
    """The query context Superset's front end would save for a table / XY / big-number / pie chart, and a
    mixed chart (its two queries: metrics and metrics_b)."""
    if params.get("viz_type") == "mixed_timeseries":
        first = {k: v for k, v in params.items() if not k.endswith("_b")}
        second = {**first, **{k[:-2]: v for k, v in params.items() if k.endswith("_b")}}
        a = _query_context({**first, "viz_type": "echarts_timeseries"}, ds_id)
        b = _query_context({**second, "viz_type": "echarts_timeseries", "x_axis": params.get("x_axis")}, ds_id)
        a["queries"] += b["queries"]
        a["form_data"] = {**params, "datasource": f"{ds_id}__table"}
        return a
    metrics = list(params.get("metrics") or ([params["metric"]] if params.get("metric") else []))
    grain = params.get("time_grain_sqla")
    if params.get("query_mode") == "raw":
        columns: list[Any] = list(params.get("all_columns") or [])
        metrics = []
    else:
        columns = list(params.get("groupby") or [])
        x = params.get("x_axis")
        if x and x not in columns:
            columns.insert(0, {"columnType": "BASE_AXIS", "sqlExpression": x, "label": x,
                               "expressionType": "SQL", "timeGrain": grain} if grain else x)
    filters, time_range = [], params.get("time_range") or "No filter"
    for f in params.get("adhoc_filters") or []:
        if f.get("expressionType") != "SIMPLE" or f.get("clause", "WHERE") != "WHERE":
            continue
        if f.get("operator") == "TEMPORAL_RANGE":
            time_range = f.get("comparator") or time_range
        else:
            filters.append({"col": f["subject"], "op": f["operator"], "val": f.get("comparator")})
    sort = params.get("timeseries_limit_metric")
    orderby = ([[sort, not params.get("order_desc", True)]] if sort
               else [] if params.get("x_axis") or not metrics else [[metrics[0], False]])
    return {"datasource": {"id": ds_id, "type": "table"}, "force": False,
            "result_format": "json", "result_type": "full",
            "form_data": {**params, "datasource": f"{ds_id}__table"},
            "queries": [{"columns": columns, "metrics": metrics, "orderby": orderby,
                         "row_limit": params.get("row_limit") or 1000, "filters": filters,
                         "time_range": time_range,
                         "extras": {"time_grain_sqla": grain, "having": "", "where": ""}}]}


QUERY_CONTEXT_VIZ = {"table", "pie", "big_number_total", "big_number", "mixed_timeseries", "echarts_timeseries",
                     "echarts_timeseries_line",
                     "echarts_timeseries_bar", "echarts_timeseries_area", "echarts_timeseries_scatter",
                     "echarts_timeseries_smooth", "echarts_timeseries_step", "echarts_area"}


def refresh_query_context(chart_id: int, keep_existing: bool = False) -> bool:
    """A chart the agent saved or changed gets the query context Superset's front end would save
    (Superset's MCP service saves none: the chart data API, CSV and text reports need one). A chart
    type it cannot be written for keeps none, never a stale one. Only for the chart's owners;
    `keep_existing`: a chart that has one (saved in Explore, with its post-processing) keeps it."""
    from superset.extensions import db, security_manager
    from superset.models.slice import Slice

    chart = db.session.get(Slice, int(chart_id))
    if chart is None or (keep_existing and chart.query_context):
        return False
    try:
        security_manager.raise_for_ownership(chart)
    except Exception:  # pylint: disable=broad-except
        return False
    params = json.loads(chart.params or "{}")
    viz = params.get("viz_type") or chart.viz_type
    chart.query_context = (json.dumps(_query_context(params, chart.datasource_id)) if viz in QUERY_CONTEXT_VIZ
                           else None)
    db.session.commit()
    return chart.query_context is not None


def _ensure_query_context(chart_id: int) -> None:
    """Charts saved without a query context (e.g. through Superset's MCP service) get the one
    Superset's front end would save: CSV / TEXT reports need it."""
    from superset.extensions import db
    from superset.models.slice import Slice

    chart = db.session.get(Slice, int(chart_id))
    if chart is None or chart.query_context:
        return
    chart.query_context = json.dumps(_query_context(json.loads(chart.params or "{}"), chart.datasource_id))
    db.session.commit()


@mcp.tool
def list_reports() -> list[dict]:
    """List scheduled reports and alerts: name, type, target, format, schedule, last state."""
    with _as_user() as (_app_, user):
        from superset.extensions import db, security_manager
        from superset.reports.models import ReportSchedule

        rows = db.session.query(ReportSchedule).order_by(ReportSchedule.id).all()
        if not security_manager.is_admin():
            rows = [r for r in rows if any(o.id == user.id for o in r.owners)]
        out = []
        for r in rows[:100]:
            targets = []
            for rec in r.recipients:
                try:
                    targets.append(json.loads(rec.recipient_config_json or "{}").get("target"))
                except ValueError:
                    pass
            out.append({"id": r.id, "name": r.name, "type": r.type, "active": r.active, "crontab": r.crontab,
                        "timezone": r.timezone, "format": r.report_format, "last_state": r.last_state,
                        "target": {"dashboard": r.dashboard_id} if r.dashboard_id else {"chart": r.chart_id},
                        "recipients": targets})
        return out


def _time_range_fix(params: dict) -> dict | None:
    """Move >=, >, <=, < filters on the chart's time column into its TEMPORAL_RANGE filter
    (the agent's time_range_fix does the same); None when there is nothing to move."""
    col = params.get("granularity_sqla") or params.get("x_axis")
    filters = params.get("adhoc_filters") or []
    moved = [f for f in filters if f.get("expressionType") == "SIMPLE" and f.get("subject") == col
             and f.get("operator") in (">=", ">", "<=", "<")]
    if not isinstance(col, str) or not moved:
        return None
    start = next((f["comparator"] for f in moved if f["operator"] in (">=", ">")), "")
    end = next((f["comparator"] for f in moved if f["operator"] in ("<=", "<")), "")
    time_range = f"{start} : {end}"
    kept = [f for f in filters if f not in moved]
    temporal = [f for f in kept if f.get("operator") == "TEMPORAL_RANGE" and f.get("subject") == col]
    if temporal:
        temporal[0]["comparator"] = time_range
    else:
        kept.append({"clause": "WHERE", "expressionType": "SIMPLE", "subject": col,
                     "operator": "TEMPORAL_RANGE", "comparator": time_range})
    return {**params, "adhoc_filters": kept, "time_range": time_range}


@mcp.tool
def fix_chart_time_range(chart_id: int) -> dict:
    """Call after saving a chart with Superset's MCP service (generate_chart / update_chart)
    when its config filters the time column (ts >= '...', ts < '...'): Superset keeps such
    filters as plain filters, which the chart's own page applies but dashboards ignore (they
    show the default range). This moves them into the chart's time range, which dashboards keep."""
    try:
        with _as_user():
            from superset.extensions import db, security_manager
            from superset.models.slice import Slice

            chart = db.session.get(Slice, int(chart_id))
            if chart is None:
                return {"error": f"chart {chart_id} not found"}
            try:
                security_manager.raise_for_ownership(chart)
            except Exception:  # pylint: disable=broad-except
                return {"error": f"chart {chart_id}: only its owners (or an admin) may change it"}
            fixed = _time_range_fix(json.loads(chart.params or "{}"))
            if fixed is None:
                return {"chart_id": chart_id, "note": "no filter on the time column to move"}
            chart.params = json.dumps(fixed)
            db.session.commit()
            return {"chart_id": chart_id, "time_range": fixed["time_range"]}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:500]}"}


@mcp.tool
def create_report(request: ReportRequest) -> dict:
    """Schedule a recurring e-mail report of a dashboard or a chart (PNG screenshot in the
    e-mail, PDF / CSV attachment or data table), with subject and HTML description. Give the
    dashboard or the chart by id or by title/name. For a one-off e-mail use send_email."""
    try:
        with _as_user() as (_app_, user):
            from superset.extensions import db, security_manager
            from superset.models.dashboard import Dashboard
            from superset.models.slice import Slice

            if not security_manager.can_access("can_write", "ReportSchedule"):
                return {"error": "you may not create reports (Superset permission can write on ReportSchedule)"}
            dash, chart = request.dashboard_id, request.chart_id
            if dash is None and request.dashboard_title:
                dash = _find_id("dashboard", request.dashboard_title)
                if dash is None:
                    return {"error": f"no dashboard titled {request.dashboard_title!r} that you may see"}
            if chart is None and request.chart_name:
                chart = _find_id("chart", request.chart_name)
                if chart is None:
                    return {"error": f"no chart named {request.chart_name!r} that you may see"}
            if (dash is None) == (chart is None):
                return {"error": "missing target: add dashboard_id (or dashboard_title) for a dashboard "
                                 "report, or chart_id (or chart_name) for a chart report - exactly one"}
            target = db.session.get(Dashboard, int(dash)) if dash is not None else db.session.get(Slice, int(chart))
            if target is None:
                return {"error": f"{'dashboard' if dash is not None else 'chart'} {dash or chart} not found"}
            security_manager.raise_for_access(**({"dashboard": target} if dash is not None else {"chart": target}))
            body: dict[str, Any] = {
                "type": "Report", "name": request.name, "active": request.active,
                "crontab": request.crontab, "timezone": request.timezone, "creation_method": "alerts_reports",
                "report_format": request.report_format, "owners": [user.id],
                "description": request.description_html or "", "log_retention": 90, "working_timeout": 600,
                "recipients": [{"type": "Email", "recipient_config_json": {
                    "target": ", ".join(request.email_recipients)}}],
            }
            if request.email_subject:
                body["email_subject"] = request.email_subject
            if request.custom_width:
                body["custom_width"] = request.custom_width
            if dash is not None:
                body["dashboard"] = int(dash)
            else:
                body["chart"] = int(chart)
                body["force_screenshot"] = request.report_format == "PNG"
                if request.report_format in ("CSV", "TEXT"):
                    _ensure_query_context(int(chart))
            from superset.commands.report.create import CreateReportScheduleCommand

            try:
                report = CreateReportScheduleCommand(body).run()
            except Exception as ex:  # pylint: disable=broad-except
                detail = getattr(ex, "normalized_messages", None)
                return {"error": "report not created", "detail": detail() if callable(detail) else str(ex)[:1000]}
            return {"id": report.id, "name": request.name, "schedule": request.crontab,
                    "target": {"dashboard": dash} if dash is not None else {"chart": chart},
                    "format": request.report_format, "list_url": "/report/list/"}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1000]}"}
