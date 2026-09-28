"""What the agent learns from its own answers, for the whole team:

* recipes: the shortest successful way an answer was reached (the final SQL / PromQL, or the
  chart configuration that Superset saved), with its time and size. They are `auto` after an
  answer, `confirmed` by Helpful, `rejected` by Not helpful; a similar question later starts from
  the confirmed ones (and the automatic ones used several times), adapted to its own dates and
  filters, only on databases the user may query;
* query timings: how long each kind of query takes (literals removed) per table or metric, so
  that the agent knows which shapes are fast on the big metrics and indices;
* compact results: a result too big for the LLM is summarised for it (columns, first rows,
  statistics, top values); the page still shows every row.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

from superset import db

from supagent.models import QueryStat, Recipe

QUERY_TOOLS = ("execute_sql", "promql_query")
RECIPE_TOOLS = ("execute_sql", "promql_query", "generate_chart", "update_chart", "export_excel")
LLM_ROWS = 60             # a result with more rows reaches the LLM as a summary
LLM_ROWS_ALL = 500        # when the question asks for JSON or for every row
FIRST_ROWS = 25
STOP = set("the a an of to in on for by and or is are was were what which how many much show give me my "
           "please with from at as be do does did this that these those today yesterday le la les des du de et "
           "est quel quelle quels combien par pour avec sur dans une un".split())


def words(text: str) -> set[str]:
    from supagent.knowledge.describe import stem

    return {stem(w) for w in re.findall(r"[a-z0-9_]+", (text or "").lower()) if w not in STOP and len(w) > 1}


# --------------------------------------------------------------------------- #
# signatures of queries (literals removed)
# --------------------------------------------------------------------------- #
def sql_pattern(sql: str) -> tuple[str, list[str]]:
    """(the SQL with its literals replaced by ?, the tables it reads)."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="duckdb")
        tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name})
        for lit in list(tree.find_all(exp.Literal)):
            lit.replace(exp.Placeholder())
        return tree.sql(dialect="duckdb"), tables
    except Exception:  # pylint: disable=broad-except
        text = re.sub(r"'[^']*'", "?", sql or "")
        text = re.sub(r"\b\d+(\.\d+)?\b", "?", text)
        return " ".join(text.split()), re.findall(r'(?:FROM|JOIN)\s+"?([\w\-.*]+)"?', sql or "", re.I)


def promql_pattern(expr: str) -> tuple[str, list[str]]:
    text = re.sub(r'"[^"]*"', '"?"', expr or "")
    text = re.sub(r"\[\d+[smhdwy]\]", "[?]", text)
    text = re.sub(r"\b\d+(\.\d+)?\b", "?", text)
    metrics = sorted(set(re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[)", expr or "")))
    return " ".join(text.split()), metrics


def signature(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _query_of(tool: str, args: dict[str, Any]) -> tuple[str | None, int | None]:
    req = args.get("request") if isinstance(args.get("request"), dict) else args
    if tool == "execute_sql":
        return req.get("sql"), database_of(tool, req)
    if tool == "promql_query":
        return req.get("expr"), database_of(tool, req)
    if tool == "export_excel":
        return req.get("sql"), database_of(tool, req)
    return None, None


def database_of(tool: str, req: dict[str, Any]) -> int:
    """The database a query or chart of an answer ran on, resolved as the user who asked (the
    tools resolve it the same way). 0 when unknown: the recipe is then shown to nobody rather
    than to everyone."""
    try:
        if tool == "execute_sql":
            return int(req.get("database_id") or 0)
        from supagent import tools as T

        if tool == "promql_query":
            return int(T._metrics_database(req.get("database")).id)
        if tool == "export_excel":
            return int(T._database(req.get("database"), sql=req.get("sql")).id)
        if tool in ("generate_chart", "update_chart"):
            from superset.connectors.sqla.models import SqlaTable
            from superset.models.slice import Slice

            if req.get("dataset_id") not in (None, ""):
                ds = db.session.get(SqlaTable, int(req["dataset_id"]))
                return int(ds.database_id) if ds is not None else 0
            ref = req.get("identifier") or req.get("chart_id")
            chart = db.session.get(Slice, int(ref)) if str(ref or "").isdigit() else None
            ds = db.session.get(SqlaTable, chart.datasource_id) if chart is not None and chart.datasource_id else None
            return int(ds.database_id) if ds is not None else 0
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    return 0


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def record_timings(trace: list[dict]) -> int:
    """Every query of an answer -> the timings of its kind of query."""
    n = 0
    for t in trace:
        tool = t.get("called") or t["tool"]
        if tool not in QUERY_TOOLS:
            continue
        query, database_id = _query_of(tool, t.get("args") or {})
        if not query:
            continue
        pattern, targets = sql_pattern(query) if tool == "execute_sql" else promql_pattern(query)
        sig = signature(f"{tool}:{pattern}")
        row = db.session.query(QueryStat).filter_by(database_id=database_id, signature=sig).one_or_none()
        if row is None:
            row = QueryStat(database_id=database_id, signature=sig, pattern=pattern[:4000],
                            target=",".join(targets)[:512], calls=0, errors=0, total_seconds=0.0, max_seconds=0.0,
                            total_rows=0)
            db.session.add(row)
        seconds = float(t.get("seconds") or 0)
        row.calls = (row.calls or 0) + 1
        row.total_seconds = (row.total_seconds or 0) + seconds
        row.max_seconds = max(row.max_seconds or 0, seconds)
        if t.get("status") == "error":
            row.errors = (row.errors or 0) + 1
            row.last_error = (t.get("result") or "")[:500]
        else:
            row.total_rows = (row.total_rows or 0) + int(_rows_of(t) or 0)
        row.last_at = dt.datetime.utcnow()
        n += 1
    db.session.commit()
    return n


def _rows_of(t: dict) -> int | None:
    try:
        res = json.loads(t.get("full") or t.get("result") or "{}")
    except ValueError:
        return None
    if not isinstance(res, dict):
        return None
    if isinstance(res.get("row_count"), int):
        return res["row_count"]
    if isinstance(res.get("rows"), int):
        return res["rows"]
    return len(res.get("series") or []) or None


def record_recipe(message_id: int, user_id: int, question: str, trace: list[dict]) -> Recipe | None:
    """The final successful query (or saved chart) of an answer -> a recipe (or one more use)."""
    done = [t for t in trace if (t.get("called") or t["tool"]) in RECIPE_TOOLS and t.get("status") == "done"]
    if not done:
        return None
    charts = [t for t in done if (t.get("called") or t["tool"]) in ("generate_chart", "update_chart")]
    final = charts[-1] if charts else done[-1]
    tool = final.get("called") or final["tool"]
    args = final.get("args") or {}
    if tool in ("generate_chart", "update_chart"):
        req = args.get("request") if isinstance(args.get("request"), dict) else args
        query = json.dumps(req.get("config") or req, sort_keys=True, default=str)[:8000]
        database_id, target = database_of(tool, req), str(req.get("dataset_id") or req.get("identifier") or "")
        pattern = query
    else:
        query, database_id = _query_of(tool, args)
        if not query:
            return None
        pattern, targets = sql_pattern(query) if tool != "promql_query" else promql_pattern(query)
        target = ",".join(targets)
    sig = signature(f"{tool}:{pattern}")
    ws = " ".join(sorted(words(question)))
    old = (db.session.query(Recipe).filter(Recipe.signature == sig, Recipe.status != "rejected")
           .order_by(Recipe.id.desc()).first())
    if old is not None and len(set(old.words.split()) & set(ws.split())) >= max(1, len(ws.split()) // 2):
        old.uses = (old.uses or 1) + 1
        old.last_used_at = dt.datetime.utcnow()
        old.seconds = round(((old.seconds or 0) * (old.uses - 1) + float(final.get("seconds") or 0)) / old.uses, 2)
        old.message_id = message_id
        db.session.commit()
        return old
    r = Recipe(question=(question or "")[:2000], words=ws[:2000], tool=tool, database_id=database_id,
               target=target[:512], query=query, signature=sig, args=args, seconds=final.get("seconds"),
               rows=_rows_of(final), steps=len(trace), status="auto", uses=1, user_id=user_id, message_id=message_id)
    db.session.add(r)
    db.session.commit()
    return r


def feedback(message_id: int, value: int) -> int:
    """Helpful / Not helpful on an answer -> its recipes confirmed / rejected (0: back to what the
    other answers said). The answers that confirmed a recipe are counted: two similar questions
    answered the same way are one recipe confirmed twice."""
    rows = db.session.query(Recipe).filter_by(message_id=message_id).all()
    for r in rows:
        ids = [i for i in (r.confirmations or []) if i != message_id]
        if value == 1:
            ids.append(message_id)
            r.status = "confirmed"
        elif value == -1:
            r.status = "rejected"
        else:
            r.status = "confirmed" if ids else "auto"
        r.confirmations = ids
    db.session.commit()
    return len(rows)


def learn_from_answer(message_id: int, user_id: int, question: str, trace: list[dict]) -> dict[str, Any]:
    try:
        timings = record_timings(trace)
        recipe = record_recipe(message_id, user_id, question, trace)
        return {"timings": timings, "recipe": recipe.id if recipe is not None else None}
    except Exception as ex:  # pylint: disable=broad-except   (learning never breaks an answer)
        db.session.rollback()
        return {"error": str(ex)[:300]}


# --------------------------------------------------------------------------- #
# using what was learned
# --------------------------------------------------------------------------- #
def recipes_for(question: str, limit: int = 3) -> list[dict[str, Any]]:
    """Recipes for a similar question, on databases the current user may query: confirmed ones,
    and automatic ones used at least twice; never rejected ones."""
    from superset.models.core import Database

    from supagent.security import can_use_database

    ws = words(question)
    if not ws:
        return []
    rows = (db.session.query(Recipe).filter(Recipe.status != "rejected")
            .order_by(Recipe.last_used_at.desc()).limit(2000).all())
    scored = []
    for r in rows:
        if r.status == "auto" and (r.uses or 1) < 2:
            continue
        common = ws & set((r.words or "").split())
        if len(common) < max(2, len(ws) // 3):
            continue
        scored.append((len(common) + (2 if r.status == "confirmed" else 0) + min(r.uses or 1, 5) * 0.2, r))
    out = []
    allowed: dict[int, bool] = {}
    for _score, r in sorted(scored, key=lambda x: -x[0]):
        if not r.database_id:                          # database unknown: shared with nobody
            continue
        if r.database_id not in allowed:
            d = db.session.get(Database, r.database_id)
            allowed[r.database_id] = d is not None and can_use_database(d)
        if not allowed[r.database_id]:
            continue
        out.append({"question": r.question, "tool": r.tool, "database_id": r.database_id, "query": r.query,
                    "seconds": r.seconds, "rows": r.rows, "status": r.status, "uses": r.uses, "steps": r.steps})
        if len(out) >= limit:
            break
    return out


def timing_hints(targets: list[str], database_id: int | None = None, limit: int = 3) -> list[str]:
    """What is known of the queries on these tables / metrics: typical time, the slowest shape."""
    out = []
    for target in targets:
        q = db.session.query(QueryStat).filter(QueryStat.target.like(f"%{target}%"))
        if database_id is not None:
            q = q.filter(QueryStat.database_id == database_id)
        rows = q.order_by(QueryStat.calls.desc()).limit(50).all()
        if not rows:
            continue
        calls = sum(r.calls or 0 for r in rows)
        total = sum(r.total_seconds or 0 for r in rows)
        slow = max(rows, key=lambda r: r.max_seconds or 0)
        errors = sum(r.errors or 0 for r in rows)
        text = f"{calls} queries seen, {total / max(calls, 1):.1f} s on average"
        if (slow.max_seconds or 0) >= 10:
            text += f"; slowest {slow.max_seconds:.0f} s: {slow.pattern[:160]}"
        if errors:
            text += f"; {errors} failed"
        out.append(text)
        if len(out) >= limit:
            break
    return out


def wants_all_rows(question: str) -> bool:
    return bool(re.search(r"\bjson\b|\ball (the )?rows\b|\bevery row\b|\blist (all|every)\b|\btoutes les lignes\b",
                          question or "", re.I))


def column_sums(res: dict[str, Any]) -> dict[str, float]:
    """The sums of the numeric columns of a result of two rows or more (the model adds badly)."""
    rows = [r for r in res.get("rows") or [] if isinstance(r, dict)]
    if len(rows) < 2:
        return {}
    out = {}
    for c in [c.get("name") if isinstance(c, dict) else c for c in res.get("columns") or []]:
        vals = [r.get(c) for r in rows]
        if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            total = sum(vals)
            out[c] = int(total) if all(isinstance(v, int) for v in vals) else round(total, 4)
    return out


def compact_for_llm(content: str, question: str) -> str:
    """A big execute_sql result -> columns, first rows, statistics and top values for the LLM."""
    try:
        res = json.loads(content)
    except ValueError:
        return content
    rows = res.get("rows") if isinstance(res, dict) else None
    limit = LLM_ROWS_ALL if wants_all_rows(question) else LLM_ROWS
    if not isinstance(rows, list):
        return content
    if len(rows) <= limit:
        sums = column_sums(res)
        if not sums:
            return content
        return json.dumps({**res, "column_sums": sums,
                           "note": "column_sums: the totals of the numeric columns (right for counts and amounts, "
                                   "not for rates or averages): quote them, never add numbers yourself"},
                          ensure_ascii=False, default=str)
    columns = [c.get("name") if isinstance(c, dict) else c for c in res.get("columns") or []]
    numeric: dict[str, dict[str, float]] = {}
    top: dict[str, list] = {}
    for c in columns:
        vals = [r.get(c) for r in rows if isinstance(r, dict)]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums and len(nums) >= len(vals) * 0.8:
            numeric[c] = {"min": min(nums), "max": max(nums), "avg": round(sum(nums) / len(nums), 4),
                          "sum": round(sum(nums), 4)}
        else:
            counts: dict[str, int] = {}
            for v in vals:
                counts[str(v)] = counts.get(str(v), 0) + 1
            if len(counts) <= 1000:
                top[c] = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    return json.dumps({"success": True, "database": res.get("database"), "row_count": len(rows),
                       "truncated": res.get("truncated"), "columns": columns, "first_rows": rows[:FIRST_ROWS],
                       "numeric_columns": numeric, "top_values": top,
                       "note": f"only the first {FIRST_ROWS} of {len(rows)} rows are shown to you; the user sees "
                               "every row under your answer (table, chart, CSV, Excel): summarise, do not list them"},
                      ensure_ascii=False, default=str)


BIG_SERIES = 50_000


COUNTER_SUFFIXES = ("_total", "_count", "_sum", "_bucket")


def counter_formulas(name: str) -> list[str]:
    """The catalog's formulas for a metric (saved metrics), e.g. cpu_busy_pct = 100 * SUM(rate) ..."""
    try:
        from supagent.knowledge.catalog import load_catalog

        spec = ((load_catalog().get("metrics") or {}).get("tables") or {}).get(name) or {}
    except Exception:  # pylint: disable=broad-except
        return []
    out = [f"{k} = {v.get('sql')}" for k, v in (spec.get("saved_metrics") or {}).items()
           if isinstance(v, dict) and v.get("sql")]
    return out + [str(x) for x in spec.get("sql") or [] if isinstance(spec.get("sql"), list)][:2]


def _counter_misuse(tree: Any, names: set[str], src: Any) -> str | None:
    """SUM(value) or AVG(value) of a counter: its value is cumulative since the process started."""
    from sqlglot import exp

    from supagent.models import KObject

    if not any(isinstance(a.this, exp.Column) and a.this.name.lower() == "value"
               for a in tree.find_all(exp.Sum, exp.Avg)):
        return None
    kinds = {o.name: o.metric_type for o in db.session.query(KObject).filter(
        KObject.source_id == src.id, KObject.kind == "metric", KObject.name.in_(names))}
    counters = [n for n in names if kinds.get(n) in ("counter", "histogram", "summary")
                or (kinds.get(n) in (None, "unknown") and n.endswith(COUNTER_SUFFIXES))]
    if not counters or len(counters) != len(names):
        return None                                  # a gauge in the query: its value may be meant
    name = counters[0]
    hint = "; ".join(counter_formulas(name))
    return (f"refused before running: {name} is a counter, its value only grows (cumulative since the process "
            "started), so SUM(value) or AVG(value) means nothing. Use the column rate (per second, e.g. "
            "SUM(rate) for a total rate) or increase (count over each time bucket, e.g. SUM(increase))"
            + (f". The catalog's formulas for it: {hint}" if hint else "") + ".")


def guard_sql(database: Any, sql: str) -> str | None:
    """A query that would give a wrong answer or pull too much, refused before it runs (with the
    right way): SUM / AVG of a counter's raw value; raw samples of a metric the learner knows to
    have more than BIG_SERIES series."""
    if getattr(database, "backend", None) != "promagg":
        return None
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # pylint: disable=broad-except
        return None
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    names = {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes}
    if not names:
        return None
    from supagent.models import KObject, Source

    src = db.session.query(Source).filter_by(database_id=database.id).one_or_none()
    if src is None:
        return None
    misuse = _counter_misuse(tree, names, src)
    if misuse:
        return misuse
    if tree.find(exp.Group) is not None or tree.find(exp.AggFunc) is not None:
        return None
    for o in db.session.query(KObject).filter(KObject.source_id == src.id, KObject.kind == "metric",
                                              KObject.name.in_(names)):
        series = (o.stats or {}).get("series") or 0
        if series > BIG_SERIES:
            return (f"refused before running: {o.name} has about {series:,} series, and this query reads its raw "
                    "samples. Aggregate instead: GROUP BY a time bucket (DATE_TRUNC('hour', ts)) and a few labels, "
                    "filter on labels, use SUM(rate) / AVG(value) / MAX(value), and keep the time range short.")
    return None
