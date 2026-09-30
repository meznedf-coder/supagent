"""What the agent learns from its own answers, for the whole team:

* learned answers (recipes): the way an answer marked Helpful was reached (the final SQL /
  PromQL, or the chart configuration that Superset saved), with its time and size, under a
  short generic question the LLM writes. Only Helpful makes one (`helpful`); an admin then
  confirms or rejects it. A similar question later starts from the helpful and confirmed
  ones, adapted to its own dates and filters, only on databases the user may query. (0.2.1
  and before saved every answer by itself: those `auto` ones are no longer listed nor used);
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
USED = ("helpful", "confirmed")      # the learned answers that are listed and used
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
        row.last_query = query[:8000]
        if t.get("status") == "error":
            row.errors = (row.errors or 0) + 1
            row.last_error = (t.get("result") or "")[:1500]
        else:
            row.total_rows = (row.total_rows or 0) + int(_rows_of(t) or 0)
        row.last_at = dt.datetime.utcnow()
        n += 1
    db.session.commit()
    return n


def _rows_of(t: dict) -> int | None:
    if isinstance(t.get("rows"), int):
        return t["rows"]
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


def final_step(trace: list[dict]) -> dict | None:
    """The step a recipe keeps: the last saved chart, else the last successful query."""
    done = [t for t in trace if (t.get("called") or t["tool"]) in RECIPE_TOOLS and t.get("status") == "done"]
    charts = [t for t in done if (t.get("called") or t["tool"]) in ("generate_chart", "update_chart")]
    return (charts or done or [None])[-1]


def final_query(trace: list[dict]) -> tuple[str | None, str | None]:
    """(tool, query or chart configuration) of the step a recipe keeps."""
    step = final_step(trace)
    if step is None:
        return None, None
    tool = step.get("called") or step["tool"]
    args = step.get("args") or {}
    if tool in ("generate_chart", "update_chart"):
        req = args.get("request") if isinstance(args.get("request"), dict) else args
        return tool, json.dumps(req.get("config") or req, sort_keys=True, default=str)[:1500]
    return tool, _query_of(tool, args)[0]


def final_database(trace: list[dict]) -> int:
    """The database of the step a recipe keeps (0 when unknown)."""
    step = final_step(trace)
    if step is None:
        return 0
    args = step.get("args") or {}
    req = args.get("request") if isinstance(args.get("request"), dict) else args
    return database_of(step.get("called") or step["tool"], req)


def kept_questions(text: str, database_id: int | None, exclude: int | None = None, limit: int = 5,
                   generic_only: bool = False) -> list[tuple[int, str]]:
    """(id, question) of the learned answers on this database that share the most words with
    `text`: the LLM says whether one of them is the same question (knowledge/generic.py)."""
    ws = words(text)
    if not ws or not database_id:
        return []
    q = db.session.query(Recipe.id, Recipe.question, Recipe.words).filter(Recipe.database_id == database_id,
                                                                          Recipe.status.in_(USED))
    if generic_only:
        q = q.filter(Recipe.generic.is_(True))
    scored = sorted(((len(ws & set((w or "").split())), i, question) for i, question, w in q.limit(5000)
                     if i != exclude), key=lambda x: (-x[0], -x[1]))
    return [(i, question) for n, i, question in scored[:limit] if n >= 2]


def record_recipe(message_id: int, user_id: int, question: str, trace: list[dict],
                  generic: dict | None = None) -> Recipe | None:
    """The final successful query (or saved chart) of an answer marked Helpful -> a learned
    answer waiting for an admin, or one more confirmation of the same one. `generic`: the
    question as the LLM wrote it, generic (knowledge/generic.py); the user's own words, which
    may hold the data of one case, are not kept."""
    done = [t for t in trace if (t.get("called") or t["tool"]) in RECIPE_TOOLS and t.get("status") == "done"]
    if not done:
        return None
    if generic is not None:
        if not generic.get("reusable", True):
            return None
        question = generic.get("question") or question
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
    is_generic = bool(generic and generic.get("generic"))
    same = db.session.get(Recipe, generic["same_as"]) if is_generic and generic.get("same_as") else None
    if same is not None and (same.status not in USED or same.database_id != database_id
                             or (same.status == "confirmed" and same.signature != sig)):
        same = None               # a way an admin confirmed is only changed by an admin
    if same is not None and same.signature != sig:      # the same question, answered another way: the newest
        same.tool, same.query, same.signature, same.args, same.target = tool, query, sig, args, target[:512]
        same.rows, same.steps = _rows_of(final), len(trace)
    old = same or (db.session.query(Recipe).filter(Recipe.signature == sig, Recipe.status.in_(USED))
                   .order_by(Recipe.id.desc()).first())
    if old is not None and (old is same or
                            len(set(old.words.split()) & set(ws.split())) >= max(1, len(ws.split()) // 2)):
        if is_generic and not old.generic:        # the same answer, now with a generic question
            old.question, old.words, old.generic = question[:2000], ws[:2000], True
        if message_id in (old.confirmations or []):
            return old                            # this answer already counted
        old.confirmations = list(old.confirmations or []) + [message_id]
        old.uses = (old.uses or 1) + 1
        old.last_used_at = dt.datetime.utcnow()
        old.seconds = round(((old.seconds or 0) * (old.uses - 1) + float(final.get("seconds") or 0)) / old.uses, 2)
        old.message_id = message_id
        db.session.commit()
        return old
    r = Recipe(question=(question or "")[:2000], words=ws[:2000], tool=tool, database_id=database_id,
               target=target[:512], query=query, signature=sig, args=args, seconds=final.get("seconds"),
               rows=_rows_of(final), steps=len(trace), status="helpful", uses=1, user_id=user_id,
               message_id=message_id, confirmations=[message_id], generic=is_generic)
    db.session.add(r)
    db.session.commit()
    return r


def feedback(message_id: int, value: int) -> int:
    """Not helpful, or Helpful taken back, on an answer: its Helpful no longer counts. A learned
    answer that only rested on it is removed; one an admin confirmed stays (an admin decides).
    Helpful itself is learned in the background (learn_from_helpful: it asks the LLM)."""
    if value == 1:
        return 0
    changed = 0
    for r in db.session.query(Recipe).filter(Recipe.status.in_(USED)).all():
        ids = list(r.confirmations or [])
        if message_id not in ids:
            continue
        ids = [i for i in ids if i != message_id]
        if not ids and r.status == "helpful":
            db.session.delete(r)
        else:
            r.confirmations, r.uses = ids, max(1, (r.uses or 1) - 1)
        changed += 1
    db.session.commit()
    return changed


def trace_of(message: Any) -> list[dict]:
    """The steps of a saved answer as a trace (the page keeps each step's tool, status, time and
    arguments; the row counts are in its results)."""
    counts = {(r.get("sql") or ""): r.get("row_count") for r in message.results or []}
    out = []
    for s in message.steps or []:
        step = {"tool": s.get("tool"), "called": s.get("tool"), "status": s.get("status"), "args": s.get("args") or {},
                "seconds": s.get("seconds"), "result": s.get("result") or ""}
        req = step["args"].get("request") if isinstance(step["args"].get("request"), dict) else step["args"]
        if isinstance(counts.get(req.get("sql") or req.get("expr") or ""), int):
            step["rows"] = counts[req.get("sql") or req.get("expr")]
        out.append(step)
    return out


def learn_from_helpful(message_id: int, llm: Any = None) -> int | None:
    from supagent.llm import llm_task

    with llm_task("helpful", message_id=message_id):
        return _learn_from_helpful(message_id, llm)


def _learn_from_helpful(message_id: int, llm: Any = None) -> int | None:
    """An answer marked Helpful -> a learned answer (or one more confirmation of the same one),
    under the generic question the LLM writes, as the user who asked; its id. Nothing when the
    Helpful was taken back meanwhile, or when the answer ran no query."""
    from superset.extensions import security_manager

    from supagent.knowledge.generic import generalize
    from supagent.models import Conversation, Message
    from supagent.security import acting_as

    m = db.session.get(Message, message_id)
    if m is None or m.role != "assistant" or m.feedback != 1 or m.status != "done":
        return None
    conv = db.session.get(Conversation, m.conversation_id)
    user = security_manager.get_user_by_id(conv.user_id) if conv is not None else None
    if user is None:
        return None
    said = [u.content or "" for u in db.session.query(Message).filter(
        Message.conversation_id == m.conversation_id, Message.id < m.id, Message.role == "user").order_by(Message.id)]
    if not said:
        return None
    trace = trace_of(m)
    tool, query = final_query(trace)
    if not query:
        return None
    with acting_as(user.username):
        kept = kept_questions(" ".join(said[-3:]), final_database(trace))
        db.session.commit()                              # no metadata connection held during the LLM call
        generic = generalize(said[-1], said[:-1], query, tool, llm=llm, kept=kept)
        m = db.session.get(Message, message_id)
        if m is None or m.feedback != 1:
            return None                                  # Helpful taken back meanwhile
        r = record_recipe(message_id, conv.user_id, said[-1], trace, generic=generic)
        return r.id if r is not None else None


METRIC_FILTER = re.compile(r"metric_name\s*(?:=\s*'([^']+)'|IN\s*\(([^)]*)\))", re.I)


def _found_nothing(t: dict) -> bool:
    """A query step that failed or returned no rows (no series)."""
    if t.get("status") == "error":
        return True
    try:
        res = json.loads(t.get("full") or t.get("result") or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(res, dict) and (res.get("row_count") == 0 or res.get("series_count") == 0)


def _tables_read(trace: list[dict], empty: bool = False) -> set[tuple[int, str, str]]:
    """(database id, metric | index, name) of what the successful queries of an answer read (with
    `empty`: of the queries that failed or found nothing)."""
    from superset.models.core import Database

    out: set[tuple[int, str, str]] = set()
    backends: dict[int, str] = {}
    for t in trace:
        tool = t.get("called") or t["tool"]
        if tool not in ("execute_sql", "export_excel", "promql_query"):
            continue
        if (not _found_nothing(t)) if empty else t.get("status") != "done":
            continue
        query, database_id = _query_of(tool, t.get("args") or {})
        if not query or not database_id:
            continue
        if database_id not in backends:
            d = db.session.get(Database, database_id)
            backends[database_id] = d.backend if d is not None else ""
        kind = "metric" if backends[database_id] == "promagg" else "index"
        _p, names = promql_pattern(query) if tool == "promql_query" else sql_pattern(query)
        for name in names:
            if name == "all_metrics":                    # the metrics it filtered
                for m in METRIC_FILTER.finditer(query):
                    for n in re.findall(r"'([^']+)'", m.group(0)):
                        out.add((database_id, "metric", n))
                continue
            out.add((database_id, kind, name))
    return out


def record_associations(message_id: int, question: str, trace: list[dict]) -> int:
    """The words of the question and the metrics or indices its successful queries read: where the
    data of such words is, for the next questions (learn.associations; knowledge/resolve.py). A
    metric or index an association pointed to that this answer queried in vain (an error, no
    rows) while the answer came from elsewhere loses a use (a miss), and goes at none."""
    from supagent import settings
    from supagent.knowledge.resolve import terms
    from supagent.models import Association

    if not settings.get("learn.associations"):
        return 0
    words = terms(question)[:12]
    last = final_step(trace)                       # the query that answered, not the tries before it
    tables = _tables_read([last]) if last is not None else set()
    missed = _tables_read(trace, empty=True) - tables if tables else set()
    for database_id, kind, name in missed:
        for a in db.session.query(Association).filter(Association.word.in_([w[:64] for w in words]),
                                                      Association.database_id == database_id,
                                                      Association.kind == kind, Association.name == name[:512]):
            a.uses = (a.uses or 1) - 1
            if a.uses <= 0:
                db.session.delete(a)
    n = 0
    for database_id, kind, name in tables:
        for w in words:
            a = (db.session.query(Association).filter_by(word=w[:64], database_id=database_id, kind=kind, parent="",
                                                         name=name[:512]).one_or_none())
            if a is None:
                a = Association(word=w[:64], database_id=database_id, kind=kind, parent="", name=name[:512], uses=0,
                                messages=[])
                db.session.add(a)
            a.uses = (a.uses or 0) + 1
            a.messages = (list(a.messages or []) + [message_id])[-50:]
            n += 1
    db.session.commit()
    return n


def forget_associations(message_id: int) -> int:
    """Not helpful: what this answer taught about where the data is goes."""
    from supagent.models import Association

    n = 0
    for a in db.session.query(Association).all():
        if message_id not in (a.messages or []):
            continue
        a.messages = [i for i in a.messages if i != message_id]
        a.uses = (a.uses or 1) - 1
        if a.uses <= 0:
            db.session.delete(a)
        n += 1
    db.session.commit()
    return n


def learn_from_answer(message_id: int, user_id: int, question: str, trace: list[dict]) -> dict[str, Any]:
    """After every answer: the timings of its queries, where its data was (associations); a
    learned answer needs Helpful."""
    try:
        return {"timings": record_timings(trace), "associations": record_associations(message_id, question, trace)}
    except Exception as ex:  # pylint: disable=broad-except   (learning never breaks an answer)
        db.session.rollback()
        return {"error": str(ex)[:300]}


# --------------------------------------------------------------------------- #
# using what was learned
# --------------------------------------------------------------------------- #
def recipes_for(question: str, limit: int = 3) -> list[dict[str, Any]]:
    """Learned answers for a similar question, on databases the current user may query: the ones
    an admin confirmed first, then the ones marked Helpful; never rejected or automatic ones."""
    from superset.models.core import Database

    from supagent.security import can_use_database

    ws = words(question)
    if not ws:
        return []
    rows = (db.session.query(Recipe).filter(Recipe.status.in_(USED))
            .order_by(Recipe.last_used_at.desc()).limit(2000).all())
    scored = []
    from supagent.knowledge.describe import stem

    for r in rows:
        common = ws & {stem(w) for w in (r.words or "").split()}      # stored by an older stemmer too
        if len(common) < max(2, len(ws) // 3):
            continue
        scored.append((len(common) + (2 if r.status == "confirmed" else 0) + min(r.uses or 1, 5) * 0.2, r))  # admin first
    from supagent.knowledge.resolve import gone_names, mentions_gone

    out = []
    allowed: dict[int, bool] = {}
    gone = gone_names()
    for _score, r in sorted(scored, key=lambda x: -x[0]):
        if not r.database_id:                          # database unknown: shared with nobody
            continue
        if mentions_gone(r.query, gone):               # its metric or index no longer exists
            continue
        if r.database_id not in allowed:
            d = db.session.get(Database, r.database_id)
            allowed[r.database_id] = d is not None and can_use_database(d)
        if not allowed[r.database_id]:
            continue
        out.append({"id": r.id, "question": r.question, "tool": r.tool, "database_id": r.database_id, "query": r.query,
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
        note = ("column_sums: the totals of the numeric columns (right for counts and amounts, not for rates or "
                "averages): quote them, never add numbers yourself")
        if res.get("note"):                             # the SQL's own LIMIT reached: said, and the sums are of these rows
            note = (f"{res['note']} column_sums: the totals of these {len(rows)} rows only, not of every matching "
                    "row (and not for rates or averages); never add numbers yourself")
        return json.dumps({**res, "column_sums": sums, "note": note}, ensure_ascii=False, default=str)
    columns = [c.get("name") if isinstance(c, dict) else c for c in res.get("columns") or []]
    numeric: dict[str, dict[str, float]] = {}
    top: dict[str, list] = {}
    empty: dict[str, int] = {}
    for c in columns:
        vals = [r.get(c) for r in rows if isinstance(r, dict)]
        blank = sum(1 for v in vals if v is None or (isinstance(v, str) and not v.strip()))
        if blank:
            empty[c] = blank
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
    out = {"success": True, "database": res.get("database"), "row_count": len(rows),
           "truncated": res.get("truncated"), "columns": columns, "first_rows": rows[:FIRST_ROWS],
           "numeric_columns": numeric, "top_values": top,
           "note": f"only the first {FIRST_ROWS} of {len(rows)} rows are shown to you; the user sees "
                   "every row under your answer (table, chart, CSV, Excel): summarise, do not list them, and never "
                   "guess the names of the rows you do not see"}
    if empty:
        out["empty_values"] = empty
        out["note"] += ("; empty_values: rows where that column is empty (null): they are not a name, say so when "
                        "you count them (e.g. 200 servers and 1 row without a server)")
    if res.get("note"):                                 # the SQL's own LIMIT reached...
        out["note"] += ". " + str(res["note"])
    return json.dumps(out, ensure_ascii=False, default=str)


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


def count_of_counter(database: Any, sql: str) -> str | None:
    """COUNT(...) over a counter, histogram or summary of a metrics database counts samples (one per series
    and scrape), not the requests, jobs or errors the question counts: the reason, or None."""
    if getattr(database, "backend", None) != "promagg" or not sql or "count" not in sql.lower():
        return None
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # pylint: disable=broad-except
        return None
    if tree.find(exp.Count) is None:
        return None
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    names = {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes}
    if not names:
        return None
    from supagent.models import KObject, Source

    src = db.session.query(Source).filter_by(database_id=database.id).one_or_none()
    kinds = {} if src is None else {o.name: o.metric_type for o in db.session.query(KObject).filter(
        KObject.source_id == src.id, KObject.kind == "metric", KObject.name.in_(names))}
    counters = sorted(n for n in names if kinds.get(n) in ("counter", "histogram", "summary")
                      or (kinds.get(n) in (None, "unknown") and n.endswith(COUNTER_SUFFIXES)))
    if not counters:
        return None
    name = counters[0]
    return (f"tool error (not run: counter): COUNT on {name} counts samples (one per series and scrape interval), "
            "not the requests, jobs or errors it counts. For a number of events over the period use SUM(increase) "
            "(with FILTER (WHERE ...) for a part, e.g. the 5xx codes), for a rate SUM(rate); a histogram's events "
            "are its _count metric's increase. If you do want the number of samples (which series have data), "
            "send this same call again.")


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
