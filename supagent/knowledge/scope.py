"""Which database a question, a chart or a dashboard is about, when the same index or metric is in
several databases (two OpenSearch clusters, one Mimir reached directly and through a gateway, a
federated one):

  named     the question names a database: its whole name, or words of its name that no other
            database's name has ("federated", "replica") next to a word for a database ("the
            federated Prometheus", "on the DR replica cluster"); or a Mimir tenant only it has
  charts    the charts and dashboards the question or the chat is about (an id, a link, a title) or
            that a tool of this answer read: the database of each chart's dataset
  default   otherwise the resolver's choice (agent.preferred_databases, the catalog's metrics
            database, the database the team's charts use for that table, the learned one): only
            said, never enforced

The first two are evidence: a query that reads one of those tables in another database is sent back
once with the reason; sent again unchanged, it runs (the question may ask for that database)."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from superset import db

log = logging.getLogger(__name__)

GENERIC = {"prometheus", "mimir", "opensearch", "elasticsearch", "osagg", "promagg", "database", "databases",
           "via", "des", "les", "sur", "par", "avec", "base", "bases", "test", "tests", "set", "new", "old", "main",
           "all", "data", "lab", "copy", "only", "not", "one", "two", "our", "your"}
# words that name a database, never data ("server", "tenant", "gateway", "instance" can be data too)
DB_WORDS = {"database", "databases", "db", "cluster", "clusters", "connection", "prometheus", "mimir", "opensearch",
            "elasticsearch", "elastic", "replica", "federation"}
NEAR = 2                      # words between a name's word and a word for a database
DB_ID = re.compile(r"\b(?:database|db|base de donn[ée]es)\s*(?:id\s*)?#?\s*(\d{1,6})\b", re.I)
CHART_ID = re.compile(r"\b(?:chart|graph|graphique|slice)\s*(?:id\s*)?(?:#|n[°o]\.?\s*)?(\d{1,7})\b", re.I)
DASH_ID = re.compile(r"\b(?:dashboard|tableau de bord)\s*(?:id\s*)?(?:#|n[°o]\.?\s*)?(\d{1,7})\b", re.I)
DASH_URL = re.compile(r"/superset/dashboard/([\w-]+)")
CHART_URL = re.compile(r"[?&]slice_id=(\d+)")
QUOTED = re.compile(r"[\"'“”‘’«]([^\"'“”‘’«»\n]{4,160}?)[\"'“”‘’»]")
MIN_TITLE = 10                # a title found in a question without quotes: at least this long
POINTS_BACK = re.compile(r"\b(that|this|these|those|the same|its|their|ce|cet|cette|ces|son|sa|ses|leurs?|m[êe]mes?)\b"
                         r"[^.?!\n]{0,24}\b(dashboards?|charts?|graphs?|tableaux? de bord|graphiques?)\b", re.I)
MAX_BOUND = 12                # charts read for one question at most (a dashboard of 40 charts: its first 12)


@dataclass
class Scope:
    named: dict[int, str] = field(default_factory=dict)                  # database id -> why
    bound: dict[str, dict[int, str]] = field(default_factory=dict)       # table (lower case) -> {database id: why}
    names: dict[int, str] = field(default_factory=dict)                  # database id -> its name
    lines: list[str] = field(default_factory=list)                       # what the prompt says
    charts: set[int] = field(default_factory=set)                        # charts bound
    strong: set[tuple[str, int]] = field(default_factory=set)            # bound by the question itself
    strong_named: set[int] = field(default_factory=set)                  # named by the question itself

    def bind(self, table: str, database_id: int, why: str, strong: bool = False) -> None:
        self.bound.setdefault(table.lower(), {}).setdefault(int(database_id), why)
        if strong:
            self.strong.add((table.lower(), int(database_id)))

    def block(self) -> str:
        if not self.lines:
            return ""
        return ("\n\nThe databases of this question (the same index or metric in another database can hold other "
                "data: query these ones for it):\n" + "\n".join(f"- {x}" for x in self.lines[:10]))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _name_tokens(name: str) -> set[str]:
    """The words of a database's name that may tell it from the others ("DR", "federated", "archive")."""
    from supagent.knowledge.describe import STOP

    out = set()
    for w in re.findall(r"[A-Za-z0-9]+", name or ""):
        low = w.lower()
        if low in GENERIC or low in STOP:
            continue
        if len(w) >= 3 or (len(w) == 2 and w.isupper() and w.isalpha()):
            out.add(low)
    return out


def _has(word: str, qwords: list[str]) -> list[int]:
    """Where the word (or its plural) is in the question."""
    return [i for i, w in enumerate(qwords) if w == word or w == word + "s" or (word.endswith("s") and w == word[:-1])]


def named_databases(question: str, databases: list[Any]) -> dict[int, str]:
    """The databases the question names (id -> why): its whole name, two of its own words, or one of
    its own words next to a word for a database."""
    q = " ".join((question or "").lower().split())
    qwords = _words(question)
    df = Counter(t for d in databases for t in _name_tokens(d.database_name))
    out: dict[int, str] = {}
    ids = {int(x) for x in DB_ID.findall(question or "")}
    for d in databases:
        if d.id in ids:
            out[d.id] = f'the question names database {d.id} "{d.database_name}"'
            continue
        full = " ".join((d.database_name or "").lower().split())
        if len(full) >= 6 and len(_words(full)) >= 2 and re.search(rf"(?<!\w){re.escape(full)}(?!\w)", q):
            out[d.id] = f'the question names database {d.id} "{d.database_name}"'
            continue
        own = sorted(t for t in _name_tokens(d.database_name) if df[t] == 1)
        hits = [t for t in own if _has(t, qwords)]
        if not hits:
            continue
        near = any(abs(i - j) <= NEAR for t in hits for i in _has(t, qwords)
                   for j, w in enumerate(qwords) if w in DB_WORDS and w != t)
        if len(hits) >= 2 or near:
            out[d.id] = f'the question names database {d.id} "{d.database_name}" ({", ".join(hits)})'
    return out


def tenant_databases(question: str, databases: list[Any]) -> dict[int, str]:
    """A Mimir tenant the question names that only one of the databases has (id -> why)."""
    from supagent.knowledge.resolve import tenants_of

    words = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", question or ""))
    if not words:
        return {}
    have: dict[str, list[Any]] = {}
    for d in databases:
        if d.backend != "promagg":
            continue
        for t in tenants_of(d.id):
            if t in words:
                have.setdefault(t, []).append(d)
    out: dict[int, str] = {}
    for t, ds in have.items():
        if len(ds) == 1:
            out[ds[0].id] = f'tenant {t} is only in database {ds[0].id} "{ds[0].database_name}"'
    return out


def references(text: str) -> tuple[list[int], list[int]]:
    """The charts and dashboards a text names (ids, links, titles): (chart ids, dashboard ids)."""
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    if not (text or "").strip():
        return [], []
    charts = [int(x) for x in CHART_ID.findall(text)] + [int(x) for x in CHART_URL.findall(text)]
    boards: list[int] = [int(x) for x in DASH_ID.findall(text)]
    slugs = [x for x in DASH_URL.findall(text)]
    for s in slugs:
        if s.isdigit():
            boards.append(int(s))
        else:
            row = db.session.query(Dashboard.id).filter(Dashboard.slug == s).first()
            if row:
                boards.append(row[0])
    low = " ".join(text.lower().split())
    quoted = {" ".join(x.lower().split()) for x in QUOTED.findall(text)}
    for bid, title in db.session.query(Dashboard.id, Dashboard.dashboard_title):
        t = " ".join((title or "").lower().split())
        if t and (t in quoted or (len(t) >= MIN_TITLE and " " in t and t in low)):
            boards.append(bid)
    for cid, name in db.session.query(Slice.id, Slice.slice_name):
        t = " ".join((name or "").lower().split())
        if t and (t in quoted or (len(t) >= MIN_TITLE and " " in t and t in low)):
            charts.append(cid)
    return list(dict.fromkeys(charts)), list(dict.fromkeys(boards))


def _tables_of_sql(sql: str) -> set[str]:
    from supagent.knowledge.rulecheck import _tables

    return _tables(sql)


def _chart_tables(chart: Any) -> tuple[Any, set[str]]:
    """(the dataset, the tables it reads) of a chart; (None, set()) when it has none."""
    from superset.connectors.sqla.models import SqlaTable

    if chart.datasource_type != "table" or chart.datasource_id is None:
        return None, set()
    ds = db.session.get(SqlaTable, chart.datasource_id)
    if ds is None:
        return None, set()
    return ds, (_tables_of_sql(ds.sql) if (ds.sql or "").strip() else {ds.table_name})


def _usable(chart: Any, allowed: set[int] | None) -> tuple[Any, set[str]]:
    """(dataset, tables) of a chart the user may open, on a database the agent may use; (None, set()) else."""
    from superset.extensions import security_manager

    try:
        if not security_manager.can_access_chart(chart):
            return None, set()
    except Exception:  # pylint: disable=broad-except
        return None, set()
    ds, tables = _chart_tables(chart)
    if ds is None or (allowed is not None and ds.database_id not in allowed):
        return None, set()
    return ds, tables


def bind_charts(scope: Scope, chart_ids: list[int], board_ids: list[int], source: str,
                allowed: set[int] | None = None, strong: bool = False) -> None:
    """Bind the tables of these charts and dashboards (the ones the user may open) to their databases."""
    from superset.extensions import security_manager
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    for bid in board_ids:
        board = db.session.get(Dashboard, bid)
        if board is None:
            continue
        try:
            if not security_manager.can_access_dashboard(board):
                continue
        except Exception:  # pylint: disable=broad-except
            continue
        why = f'dashboard {board.id} "{board.dashboard_title}"'
        dbs: dict[int, set[str]] = {}
        for sl in list(board.slices or [])[:MAX_BOUND]:
            ds, tables = _usable(sl, allowed)
            if ds is None:
                continue
            scope.charts.add(sl.id)
            dbs.setdefault(ds.database_id, set()).update(tables)
            for t in tables:
                scope.bind(t, ds.database_id, why, strong)
        if dbs:
            where = "; ".join(f'{", ".join(sorted(t)[:6])} in database {i} "{scope.names.get(i, i)}"'
                              for i, t in sorted(dbs.items()))
            scope.lines.append(f"{why} ({source}): its charts read {where}")
    for cid in chart_ids:
        chart = db.session.get(Slice, cid)
        if chart is None or chart.id in scope.charts or len(scope.charts) >= 4 * MAX_BOUND:
            continue
        ds, tables = _usable(chart, allowed)
        if ds is None:
            continue
        scope.charts.add(chart.id)
        why = f'chart {chart.id} "{chart.slice_name}"'
        for t in tables:
            scope.bind(t, ds.database_id, why, strong)
        scope.lines.append(f"{why} ({source}): dataset {ds.table_name} (id {ds.id}) in database {ds.database_id} "
                           f'"{scope.names.get(ds.database_id, ds.database_id)}"')


def scope_for(question: str, previous_question: str = "", previous_answer: str = "") -> Scope:
    """What the question (and, for a follow-up, the chat) says about databases, charts and dashboards."""
    from supagent.knowledge.resolve import _databases

    scope = Scope()
    try:
        databases = _databases()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return scope
    scope.names = {d.id: d.database_name for d in databases}
    if len(databases) > 1:
        for text in (question, previous_question):
            for i, why in {**tenant_databases(text, databases), **named_databases(text, databases)}.items():
                if text is question:
                    scope.strong_named.add(i)
                if i not in scope.named:
                    scope.named[i] = why + ("" if text is question else " (earlier in the chat)")
        for i, why in scope.named.items():
            scope.lines.append(why)
    try:
        allowed = set(scope.names)
        charts, boards = references(question)
        bind_charts(scope, charts, boards, "named in the question", allowed, strong=True)
        if previous_question or previous_answer:
            charts, boards = references(f"{previous_question}\n{previous_answer}")
            # "that dashboard", "its first chart": the chat's dashboards and charts are the question's own
            bind_charts(scope, charts, boards, "from the chat", allowed, strong=bool(POINTS_BACK.search(question)))
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent scope: charts and dashboards of the question not read", exc_info=True)
        db.session.rollback()
    return scope


READ_TOOLS = {"get_chart_info": "chart", "get_chart_data": "chart", "get_dashboard_info": "dashboard",
              "generate_explore_link": None, "chart_image": None, "update_chart": "chart"}


def learn_from_call(scope: Scope, name: str, args: dict) -> None:
    """A chart or dashboard a tool of this answer read: its tables are bound to its database too."""
    if name not in READ_TOOLS:
        return
    req = args.get("request") if isinstance(args.get("request"), dict) else args
    charts, boards = [], []
    kind = READ_TOOLS[name]
    ident = req.get("identifier")
    if kind == "chart" and str(ident or "").isdigit():
        charts.append(int(ident))
    elif kind == "dashboard" and str(ident or "").isdigit():
        boards.append(int(ident))
    if name == "chart_image":
        if str(req.get("chart_id") or "").isdigit():
            charts.append(int(req["chart_id"]))
        if str(req.get("dashboard_id") or "").isdigit():
            boards.append(int(req["dashboard_id"]))
    if charts or boards:
        try:
            bind_charts(scope, charts, boards, f"read by {name}", set(scope.names) or None)
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()


# ---------------------------------------------------------------------------------------------- #
# a tool call: which database, which tables
# ---------------------------------------------------------------------------------------------- #
def call_target(name: str, args: dict) -> tuple[int | None, set[str]]:
    """(database id, tables or metrics read) of a query tool call; (None, set()) when not a query."""
    req = args.get("request") if isinstance(args.get("request"), dict) else args
    if not isinstance(req, dict):
        return None, set()
    if name in ("execute_sql", "create_virtual_dataset", "save_sql_query"):
        ref, text, sql = req.get("database_id"), req.get("sql"), True
    elif name == "open_sql_lab_with_context":
        ref, text, sql = req.get("database_connection_id"), req.get("sql"), True
    elif name in ("export_excel", "chart_from_sql"):
        ref, text, sql = req.get("database"), req.get("sql"), True
    elif name in ("promql_query", "compare_to_usual"):
        ref, text, sql = req.get("database"), req.get("expr") or req.get("promql"), False
    elif name == "generate_chart":
        from superset.connectors.sqla.models import SqlaTable

        try:
            ds = db.session.get(SqlaTable, int(req.get("dataset_id")))
        except (TypeError, ValueError):
            return None, set()
        if ds is None:
            return None, set()
        return ds.database_id, (_tables_of_sql(ds.sql) if (ds.sql or "").strip() else {ds.table_name})
    else:
        return None, set()
    if not text:
        return None, set()
    if sql:
        tables = _tables_of_sql(str(text))
    else:
        from supagent.knowledge.rulecheck import promql_metrics

        tables = promql_metrics(str(text))
    database_id: int | None = None
    if ref is not None and str(ref).strip().isdigit():
        database_id = int(str(ref).strip())
    else:
        from supagent.tools import _database

        try:
            d = _database(ref, backend=None if sql else "promagg", sql=str(text) if sql else None)
            database_id = d.id
        except Exception:  # pylint: disable=broad-except
            database_id = None
    return database_id, tables


def _has_table(database_id: int, table: str) -> bool:
    """The database has this index or metric (the dictionary, else the live metric list)."""
    from supagent.models import KObject, Source

    try:
        from superset.models.core import Database

        d = db.session.get(Database, database_id)
        if d is None:
            return False
        src = db.session.query(Source.id).filter(Source.database_id == database_id).first()
        if src is not None and db.session.query(KObject.id).filter(
                KObject.source_id == src[0], KObject.kind.in_(("index", "metric")), KObject.name == table,
                KObject.gone_at.is_(None)).first():
            return True
        if d.backend == "promagg":
            from supagent.knowledge.resolve import _metric_names

            return table in {n for n, _t in _metric_names(d)}
        if d.backend == "osagg":                        # a database not learned: its live list of indices
            return table in _index_names(d)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    return False


def _index_names(database: Any) -> set[str]:
    """The indices (and index patterns) an OpenSearch database shows (cached like the metric lists)."""
    from supagent.knowledge.resolve import _cached
    from supagent.tools import _connection

    def make() -> set[str]:
        conn = _connection(database, extract=False)
        try:
            return set(conn.list_tables())
        finally:
            conn.close()

    return _cached(("indices", database.id), make, fresh=False)


def refusal(scope: Scope, name: str, args: dict) -> str | None:
    """The reason to send this call back (a table the question or its charts put in another database),
    or None."""
    if not scope.bound and not scope.named:
        return None
    database_id, tables = call_target(name, args)
    if database_id is None or not tables:
        return None
    for t in sorted(tables):
        bound = scope.bound.get(t.lower())
        if bound and database_id not in bound and database_id not in scope.named:
            target = sorted(bound, key=lambda i: ((t.lower(), i) not in scope.strong, i))[0]
            why = bound[target]
            return _message(t, database_id, target, f"{why} reads it there", scope, name,
                            (t.lower(), target) in scope.strong)
        if scope.named and database_id not in scope.named and not bound:
            for target in sorted(scope.named, key=lambda i: (i not in scope.strong_named, i)):
                if _has_table(target, t):
                    return _message(t, database_id, target, scope.named[target], scope, name,
                                    target in scope.strong_named)
    return None


def _message(table: str, used: int, target: int, why: str, scope: Scope, name: str, strong: bool) -> str:
    """The reason, with what to do. From the question itself (a database or a chart it names): another
    database is never read; from the chat or a tool's read: sent again unchanged, the call runs."""
    arg = {"generate_chart": f"a dataset of database {target}", "export_excel": f"database={target}",
           "chart_from_sql": f"database={target}", "promql_query": f"database={target}",
           "compare_to_usual": f"database={target}",
           "open_sql_lab_with_context": f"database_connection_id={target}"}.get(name, f"database_id={target}")
    head = "another database" if strong else "check the database"
    text = (f"tool error (not run: {head}): this call reads {table} in database {used} "
            f'"{scope.names.get(used, used)}", but {why}: database {target} "{scope.names.get(target, target)}". The '
            f"same name in another database can hold other data. Call it again with {arg}.")
    return text + (" Another database is read only when the question names it (its name, or database <id>)."
                   if strong else f" If the question does mean database {used}, send this same call again unchanged.")


def describe(scope: Scope) -> dict[str, Any]:
    """For logs and tests."""
    return {"named": scope.named, "bound": scope.bound, "charts": sorted(scope.charts), "lines": scope.lines}


def dumps(scope: Scope) -> str:
    return json.dumps(describe(scope), default=str)
