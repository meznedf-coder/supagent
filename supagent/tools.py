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
import math
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from supagent.registry import Registry  # noqa: E402

mcp = Registry()
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
    they mean, labels, fields), catalog notes, rules, glossary and formulas (calculated fields),
    answers that worked before, team and personal preferences, documents and sites. `kind`:
    metric, index, note, rule, glossary, formula, recipe, memory or doc (empty: all)."""
    try:
        with _as_user():
            from supagent.knowledge.search import search

            kinds = (kind,) if kind else None
            found = search(query, k=max(1, min(int(limit or 8), 20)), kinds=kinds)
            return {"query": query, "results": [{**f, "text": (f["text"] or "")[:1200]} for f in found]}
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


def _time_arg(conn: Any, value: str | None, default_ms: int) -> int:
    if value in (None, ""):
        return default_ms
    v = str(value).strip()
    m = re.fullmatch(r"now(?:\s*-\s*(\S+))?", v.lower())
    if m:
        from promagg.timegrid import parse_duration

        return int(time.time() * 1000) - (parse_duration(m.group(1)) if m.group(1) else 0)
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


@mcp.tool
def promql_query(expr: str, start: str | None = None, end: str | None = None, step: str | None = None,
                 database: str | int | None = None, max_series: int = 50) -> dict:
    """Run a PromQL expression on the metrics database and return each series (labels,
    min / max / avg / last and up to 60 points). `start` / `end`: local times like
    "2026-09-24 02:00" or "now-6h" (end alone or start == end: one instant). `step` like "5m"
    (default: about 200 points). Use for questions SQL cannot express (ratios of two metrics,
    offsets, label_replace...); the SQL tables of the same database cover the usual cases."""
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
                now = int(time.time() * 1000)
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
                return {"expr": expr, "database": db_obj.database_name, "database_id": db_obj.id,
                        **({"hint": hint} if hint else {}),
                        "start": f"{conn.zone.local(t0):%Y-%m-%d %H:%M}", "end": f"{conn.zone.local(t1):%Y-%m-%d %H:%M}",
                        "step": duration(step_ms) if step_ms else "instant", "series_count": total,
                        "series": _series_summary(conn, series[:max(1, min(max_series, 200))], 60),
                        "truncated": total > max_series}
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
    window and list every breach: check, server / application, from, to, minutes, worst value.
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
                note = "no breach" if not found else ""
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
    return out


@mcp.tool
def compare_to_usual(promql: str, start: str, end: str, weeks: int = 4, database: str | int | None = None) -> dict:
    """Is a metric unusual for this time? The average of a PromQL expression over start-end
    compared with the same window of each of the previous `weeks` weeks (median and median
    absolute deviation, per series): verdict normal, high, low (or unknown without 3 earlier
    weeks), with the numbers. `start` / `end`: local times ("2026-09-24 02:00"), at most 7 days
    apart. For "is it unusual / abnormal / higher than usual" questions."""
    try:
        with _as_user():
            db_obj = _metrics_database(database)
            conn = _promagg_connection(db_obj)
            try:
                from superset.extensions import db as meta

                meta.session.commit()        # no connection of Superset's own pool is held while the queries run
                if len(promql) > 4000:
                    raise ToolError("expression too long (4000 characters max)")
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
                return {"database": db_obj.database_name, "from": start, "to": end, "weeks": weeks,
                        "series_count": len(rows), "unusual": unusual, "series": rows[:30],
                        "note": (f"{unusual} of {len(rows)} series unusual for this time" if rows else
                                 "no series in this window (check the expression and the dates)")}
            finally:
                conn.close()
    except ToolError as ex:
        return {"error": str(ex)}
    except Exception as ex:  # pylint: disable=broad-except
        return {"error": f"{type(ex).__name__}: {str(ex)[:1500]}"}


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
