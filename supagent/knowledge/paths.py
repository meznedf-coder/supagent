"""Investigation paths (0.9): how an investigation that found its cause went, kept as a procedure the team can
follow again.

When a user marks the answer of an investigation Helpful, the LLM writes its path (one short call, in the
background): the kind of problem, the kind of cause, the checks that led to it in their order (tool, table,
field or metric: the names are kept, the values of that one case are not), what confirms the cause and what was
ruled out. It waits in To review like a learned answer; once an admin confirms it (corrected if need be), it is
given with the next similar investigation as a path to follow first, and to check again. Nothing is written
when the answer found no cause.

A path is kept as a learned answer whose tool is "investigation" (its text in place of a query): the review, the
learned answers' page, the search and the ranking of what the team learned work for it as they are.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

from superset import db

from supagent.models import Recipe

log = logging.getLogger(__name__)
TOOL = "investigation"
LISTED = 8                # paths given with a question: the causes found before for such a problem
FULL = 3                  # of them, given with their steps (the others: their cause and what confirms it)
PATH_CHARS = 1300         # of one path in the prompt
SAME = 0.7                # two texts share this much of their words: the same problem, the same cause
STEP_CHARS = 320
MAX_STEPS = 22            # steps of the answer shown to the LLM
PROMPT = """You file how a data assistant found the cause of a problem, so that the team can follow the same
path the next time a similar problem shows up. Read the question, the steps it took and its answer, then write JSON:
- "problem": the kind of problem, generic, at most 15 words ("The night batch of a family of applications is late").
- "cause": the kind of cause found, generic, at most 15 words ("An upstream feed arrived late").
- "steps": 3 to 7 steps in the order that leads to the cause, one sentence each: what to check, with which tool
  and on which table, field or metric, and what the result tells. Keep the names of tools, tables, fields and
  metrics as they are; replace every value of this one case (application, server, feed or job names, ids,
  dates, times, numbers) by its kind ("the applications of the family", "the servers that stand out", "that
  window"). Leave out the steps that led nowhere.
- "confirm": one sentence: what shows this cause is the right one (what it must cover, what must be free of it).
- "ruled_out": what else was checked and discarded, generic, or "".
- "reusable": false when the answer found no cause, or does not say what shows it.
Write in the language of the question. Answer with the JSON only."""


def _steps_text(trace: list[dict]) -> list[str]:
    """The answer's successful steps, short: the tool and what it was asked, with what a comparison concluded."""
    out = []
    for t in trace:
        if t.get("status") != "done":
            continue
        tool = t.get("called") or t.get("tool") or ""
        args = t.get("args") or {}
        req = args.get("request") if isinstance(args.get("request"), dict) else args
        asked = req.get("sql") or req.get("expr") or req.get("promql") or json.dumps(
            {k: v for k, v in req.items() if v not in (None, "", [])}, default=str)
        line = f"{tool}: {' '.join(str(asked).split())[:STEP_CHARS]}"
        m = re.search(r'"conclusion":\s*"((?:[^"\\]|\\.)*)"', str(t.get("result") or ""))
        if m:
            line += f" -> {m.group(1)[:240]}"
        out.append(line)
    return out[-MAX_STEPS:]


def write(question: str, answer: str, trace: list[dict], llm: Any = None) -> dict[str, Any] | None:
    """The path of a solved investigation as the LLM writes it, checked; None when there is none to keep."""
    from supagent.knowledge.generic import _parse, case_values, leaks
    from supagent.llm import LLM

    steps = _steps_text(trace)
    if len(steps) < 3:
        return None
    user = (f"Question: {(question or '')[:1200]}\n\nSteps:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            + f"\n\nAnswer:\n{(answer or '')[:3000]}")
    client = llm or LLM()
    data = _parse(client.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
                              max_tokens=900).get("content") or "")
    if not data or data.get("reusable") is False:
        return None
    problem = " ".join(str(data.get("problem") or "").split())[:200]
    cause = " ".join(str(data.get("cause") or "").split())[:200]
    found = [" ".join(str(s).split())[:400] for s in (data.get("steps") or []) if str(s).strip()][:8]
    if not problem or not cause or len(found) < 2:
        return None
    path = {"problem": problem, "cause": cause, "steps": found,
            "confirm": " ".join(str(data.get("confirm") or "").split())[:400],
            "ruled_out": " ".join(str(data.get("ruled_out") or "").split())[:400]}
    hard, _soft = case_values([question], None)             # ids, long numbers and dates never stay
    for key in ("problem", "cause", "confirm", "ruled_out"):
        for v in leaks(path[key], hard):
            path[key] = re.sub(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", "that one", path[key], flags=re.I)
    path["steps"] = [re.sub("|".join(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])" for v in hard) or r"(?!x)x", "that one", s,
                            flags=re.I) for s in path["steps"]]
    return path


def text_of(path: dict[str, Any]) -> str:
    lines = [f"Problem: {path['problem']}", f"Cause found: {path['cause']}", "Steps:"]
    lines += [f"{i + 1}. {s}" for i, s in enumerate(path["steps"])]
    if path.get("confirm"):
        lines.append(f"What confirms it: {path['confirm']}")
    if path.get("ruled_out"):
        lines.append(f"Ruled out: {path['ruled_out']}")
    return "\n".join(lines)


def record(message_id: int, user_id: int, question: str, answer: str, trace: list[dict], llm: Any = None) -> Recipe | None:
    """The path of an investigation marked Helpful: a learned answer waiting for an admin, or one more
    confirmation of the path of the same problem and cause."""
    from supagent.knowledge.experience import USED, final_database, signature, words

    path = write(question, answer, trace, llm)
    if path is None:
        return None
    ws = words(f"{path['problem']} {path['cause']}")
    sig = signature(f"{TOOL}:{' '.join(sorted(ws))}")
    for old in db.session.query(Recipe).filter(Recipe.tool == TOOL, Recipe.status.in_(USED)):
        kept = parts_of(old)
        # the same problem with another cause is another path (one symptom, several known causes)
        if old.signature == sig or (_alike(kept["problem"], path["problem"]) and _alike(kept["cause"], path["cause"])):
            if message_id not in (old.confirmations or []):
                old.confirmations = list(old.confirmations or []) + [message_id]
                old.uses = (old.uses or 1) + 1
                old.last_used_at = dt.datetime.utcnow()
                db.session.commit()
            return old                                   # the same path found again: counted, an admin's text stays
    tables = sorted({t for s in path["steps"] for t in re.findall(r"[A-Za-z0-9_@.\-]*[_\-][A-Za-z0-9_@.\-]+", s)})[:12]
    r = Recipe(question=path["problem"], words=" ".join(sorted(ws))[:2000], tool=TOOL, database_id=final_database(trace),
               target=", ".join(tables)[:512], query=text_of(path), signature=sig, args={"path": path},
               steps=len(trace), status="helpful", uses=1, user_id=user_id, message_id=message_id,
               confirmations=[message_id], generic=True)
    db.session.add(r)
    db.session.commit()
    return r


# --------------------------------------------------------------------------------------------- #
# the path of any answer (0.9): what it used and in which order, read from its steps without the LLM
# --------------------------------------------------------------------------------------------- #
PLAIN = "path"            # a learned way with no query to run again: the checks an answer made
PATH_STEPS = 12
STEP_QUERY = 220
LOOKED_UP = {"describe_data": "describe_data", "search_knowledge": "search_knowledge", "search_notes": "search_notes",
             "system_links": "system_links for the parts the question names", "list_alerts": "list_alerts",
             "data_changes": "data_changes", "chart_anomalies": "chart_anomalies"}


def _sql_fields(sql: str) -> dict[str, list[str]]:
    """{table: the fields the query names} (one table: every field is its; several: under each table the fields
    written with it, the others under "")."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="duckdb")
        alias = {(t.alias or t.name): t.name for t in tree.find_all(exp.Table) if t.name}
        tables = sorted(set(alias.values()))
        out: dict[str, list[str]] = {t: [] for t in tables}
        for c in tree.find_all(exp.Column):
            name = c.name
            if not name or name == "*":
                continue
            owner = alias.get(c.table) if c.table else (tables[0] if len(tables) == 1 else "")
            out.setdefault(owner or "", [])
            if name not in out[owner or ""]:
                out[owner or ""].append(name)
        return out
    except Exception:  # pylint: disable=broad-except
        return {}


def _promql_labels(expr: str) -> list[str]:
    labels: list[str] = []
    for inside in re.findall(r"\{([^}]*)\}", expr or ""):
        labels += re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)", inside)
    for inside in re.findall(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)", expr or ""):
        labels += [x.strip() for x in inside.split(",") if x.strip()]
    return list(dict.fromkeys(x for x in labels if x != "__name__"))


def used(trace: list[dict]) -> dict[str, Any]:
    """What an answer used, in the order it used it, read from its successful steps (no LLM): the tables with the
    fields its queries name, the metrics with their labels, and the steps: the tool, what it was asked on, the
    query's shape (its literals are not kept: the case's dates, names and numbers stay out).
    {"tables": {name: [fields]}, "metrics": {name: [labels]}, "steps": [text]}"""
    from supagent.knowledge.experience import promql_pattern, sql_pattern

    tables: dict[str, list[str]] = {}
    metrics: dict[str, list[str]] = {}
    steps: list[str] = []

    def add(text: str) -> None:
        if steps and steps[-1].split(" ×", 1)[0] == text:
            n = int(steps[-1].rsplit(" ×", 1)[1]) + 1 if " ×" in steps[-1] else 2
            steps[-1] = f"{text} ×{n}"
        else:
            steps.append(text)

    for t in trace:
        if t.get("status") != "done":
            continue
        tool = t.get("called") or t.get("tool") or ""
        args = t.get("args") or {}
        req = args.get("request") if isinstance(args.get("request"), dict) else args
        if tool in ("execute_sql", "export_excel"):
            sql = str(req.get("sql") or "")
            pattern, names = sql_pattern(sql)
            for table, fields in _sql_fields(sql).items():
                if table:
                    have = tables.setdefault(table, [])
                    have += [f for f in fields if f not in have]
            for n in names:
                tables.setdefault(n, [])
            add(f"{tool} on {', '.join(names) or '?'}: {pattern[:STEP_QUERY]}")
        elif tool in ("promql_query", "compare_to_usual"):
            expr = str(req.get("expr") or req.get("promql") or "")
            pattern, names = promql_pattern(expr)
            for n in names:
                have = metrics.setdefault(n, [])
                have += [x for x in _promql_labels(expr) if x not in have]
            add(f"{tool}: {pattern[:STEP_QUERY]}")
        elif tool == "compare_groups":
            table = str(req.get("table") or "")
            fields = [str(f) for f in (req.get("group_by") or [])]
            more = re.findall(r'"([^"]+)"', str(req.get("where") or "")) + re.findall(
                r"\(\s*([A-Za-z_@][\w@.]*)", str(req.get("measure") or "")) + re.findall(
                r",\s*([A-Za-z_@][\w@.]*)\s*\)", str(req.get("measure") or ""))
            if table:
                have = tables.setdefault(table, [])
                have += [f for f in fields + more if f not in have]
            add(f"compare_groups on {table}: {req.get('measure') or 'count'}" + (f" by {', '.join(fields)}" if fields else ""))
        elif tool == "compare_logs":
            table = str(req.get("table") or "")
            more = re.findall(r'"([^"]+)"', str(req.get("where") or ""))
            if table:
                have = tables.setdefault(table, [])
                have += [f for f in more if f not in have]
            add(f"compare_logs on {table}" + (f" where {', '.join(dict.fromkeys(more))}" if more else ""))
        elif tool == "check_health":
            checks = [str(c) for c in (req.get("checks") or [])]
            add("check_health" + (f" ({', '.join(checks[:6])})" if checks else "")
                + (" for the parts the question names" if req.get("entities") else ""))
        elif tool in ("generate_chart", "update_chart"):
            conf = req.get("config") if isinstance(req.get("config"), dict) else {}
            add(f"{tool}" + (f" ({conf.get('chart_type') or conf.get('viz_type')})" if conf.get("chart_type") or conf.get("viz_type") else ""))
        elif tool == "describe_data":
            add("describe_data" + (f": {req.get('index') or req.get('name')}" if req.get("index") or req.get("name") else ""))
        elif tool in LOOKED_UP:
            add(LOOKED_UP[tool])
        elif tool:
            add(tool)
    return {"tables": tables, "metrics": metrics, "steps": steps[:PATH_STEPS]}


def path_text(path: dict[str, Any] | None) -> str:
    """A path in words: what an admin wrote of it, else the data it used and its steps."""
    if not path:
        return ""
    if str(path.get("text") or "").strip():
        return str(path["text"]).strip()
    lines = []
    data = [f"{t}" + (f" (fields {', '.join(fs[:14])})" if fs else "") for t, fs in (path.get("tables") or {}).items()]
    data += [f"metric {m}" + (f" (labels {', '.join(ls[:8])})" if ls else "") for m, ls in (path.get("metrics") or {}).items()]
    if data:
        lines.append("Data: " + "; ".join(data))
    steps = path.get("steps") or []
    if steps:
        lines.append("Steps:")
        lines += [f"{i + 1}. {step}" for i, step in enumerate(steps)]
    return "\n".join(lines)


def path_brief(path: dict[str, Any] | None, chars: int = 420) -> str:
    """A path in a line, for the prompt: the data it used, then its tools in their order."""
    if not path:
        return ""
    if str(path.get("text") or "").strip():
        return " ".join(str(path["text"]).split())[:chars]
    data = [f"{t}" + (f" (fields {', '.join(fs[:10])})" if fs else "") for t, fs in (path.get("tables") or {}).items()]
    data += [f"metric {m}" + (f" (labels {', '.join(ls[:6])})" if ls else "") for m, ls in (path.get("metrics") or {}).items()]
    tools = [s.split(":", 1)[0].split(" on ", 1)[0] for s in (path.get("steps") or [])]
    out = "; ".join(data)
    if len(tools) > 1:
        out += ("; " if out else "") + "steps: " + " -> ".join(tools)
    return out[:chars]


def of_recipe(r: Recipe) -> dict[str, Any] | None:
    """The path kept with a learned answer (None: an older one, or an investigation, whose text is its path)."""
    path = (r.args or {}).get("_path") if isinstance(r.args, dict) else None
    return path if isinstance(path, dict) else None


def set_text(r: Recipe, text: str | None) -> None:
    """An admin writes a learned answer's path in their own words (empty: back to what the answer did)."""
    args = dict(r.args or {})
    path = dict(args.get("_path") or {})
    text = str(text or "").strip()[:4000]
    if text and text != path_text({k: v for k, v in path.items() if k != "text"}):
        path["text"] = text
    else:
        path.pop("text", None)
    args["_path"] = path
    r.args = args


def record_plain(message_id: int, user_id: int, question: str, trace: list[dict], generic: dict | None = None) -> Recipe | None:
    """An answer marked Helpful that ran no query to keep (a check of the health, a comparison with usual): its
    path is the learned way. Waits for an admin like the others; the same path found again is counted."""
    from supagent.knowledge.experience import USED, final_database, signature, words

    path = used(trace)
    worked = [s for s in path["steps"] if not s.startswith(tuple(LOOKED_UP.values()))]
    if not worked:
        return None                                   # it only looked things up: nothing to follow again
    if generic is not None:
        if not generic.get("reusable", True):
            return None
        question = generic.get("question") or question
    sig = signature(f"{PLAIN}:" + "|".join(path["steps"]))
    ws = " ".join(sorted(words(question)))
    old = (db.session.query(Recipe).filter(Recipe.tool == PLAIN, Recipe.signature == sig, Recipe.status.in_(USED))
           .order_by(Recipe.id.desc()).first())
    if old is not None:
        if message_id not in (old.confirmations or []):
            old.confirmations = list(old.confirmations or []) + [message_id]
            old.uses = (old.uses or 1) + 1
            old.last_used_at = dt.datetime.utcnow()
            db.session.commit()
        return old
    target = ", ".join(list(path["tables"]) + list(path["metrics"]))[:512]
    r = Recipe(question=(question or "")[:2000], words=ws[:2000], tool=PLAIN, database_id=final_database(trace) or _database(trace),
               target=target, query=path_text(path), signature=sig, args={"_path": path}, steps=len(trace),
               status="helpful", uses=1, user_id=user_id, message_id=message_id, confirmations=[message_id],
               generic=bool(generic and generic.get("generic")))
    db.session.add(r)
    db.session.commit()
    return r


def _database(trace: list[dict]) -> int:
    """The database of the first data step of an answer that kept no query (so that the learned way is shown to
    who may query it; 0: to nobody)."""
    from supagent import tools as T

    for t in trace:
        if t.get("status") != "done":
            continue
        tool = t.get("called") or t.get("tool") or ""
        req = (t.get("args") or {}).get("request") if isinstance((t.get("args") or {}).get("request"), dict) else (t.get("args") or {})
        try:
            if tool == "compare_groups" and req.get("table"):
                return int(T._table_database(str(req["table"]), req.get("database")).id)
            if tool in ("check_health", "compare_to_usual", "promql_query"):
                return int(T._metrics_database(req.get("database")).id)
        except Exception:  # pylint: disable=broad-except
            continue
    return 0


def _alike(a: str, b: str) -> bool:
    from supagent.knowledge.experience import words

    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) >= SAME * max(len(wa), len(wb))


def parts_of(r: Recipe) -> dict[str, str]:
    """A kept path's problem, cause and what confirms it, read from its text (an admin may have corrected it)."""
    text = r.query or ""

    def line(label: str) -> str:
        m = re.search(rf"^{label}:[ \t]*(.+)$", text, re.M)
        return " ".join(m.group(1).split()) if m else ""

    return {"problem": line("Problem") or " ".join((r.question or "").split()), "cause": line("Cause found"),
            "confirm": line("What confirms it")}


def is_investigation(question: str, message_id: int | None = None) -> bool:
    """The answer was an investigation: the question's words, or the route the router gave it."""
    from supagent.agent import intents

    if "investigation" in intents(question or ""):
        return True
    if message_id:
        from supagent.models import Route

        try:
            row = db.session.query(Route.moa).filter(Route.message_id == message_id).order_by(Route.id.desc()).first()
            return bool(row and row[0] == "incident")
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
    return False


def paths_for(question: str, limit: int = LISTED) -> list[dict[str, Any]]:
    """The validated paths (an admin confirmed them) for problems like the question's, on databases the user may
    query: those whose problem shares the most words with the question first, then the causes found most often.
    One symptom has several known causes: they come together. With few paths kept, the most used ones when none
    shares a word: a path is a way of looking, worth having at hand for any investigation."""
    from superset.models.core import Database

    from supagent.knowledge.experience import words
    from supagent.security import can_use_database

    rows = db.session.query(Recipe).filter(Recipe.tool == TOOL, Recipe.status == "confirmed").limit(500).all()
    if not rows:
        return []
    ws = words(question)
    allowed: dict[int, bool] = {}
    scored = []
    for r in rows:
        if r.database_id:
            if r.database_id not in allowed:
                d = db.session.get(Database, r.database_id)
                allowed[r.database_id] = d is not None and can_use_database(d)
            if not allowed[r.database_id]:
                continue
        p = parts_of(r)
        problem = words(p["problem"])
        share = len(ws & problem) / len(problem) if problem else 0.0
        scored.append((round(share, 2), r.uses or 1, r, p))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2].id))
    if scored and scored[0][0] > 0:
        floor = scored[0][0] / 2                          # the same kind of problem as the closest one
        scored = [x for x in scored if x[0] >= floor]
    else:
        scored = sorted(scored, key=lambda x: (-x[1], x[2].id))[:FULL]
    return [{"id": r.id, "problem": p["problem"], "cause": p["cause"], "confirm": p["confirm"], "text": r.query or "",
             "uses": uses, "share": share} for share, uses, r, p in scored[:limit]]


def paths_block(question: str, shown: set[str] | None = None) -> str:
    """The validated paths as a block of the prompt ("" when there is none): the closest ones with their steps,
    the other causes found for such a problem in a line each."""
    try:
        found = paths_for(question)
    except Exception:  # pylint: disable=broad-except   (the agent answers without them)
        log.warning("supagent paths: not given", exc_info=True)
        db.session.rollback()
        return ""
    if not found:
        return ""
    lines = ["\n\nInvestigation paths the team validated (causes found before for problems like this one, each with "
             "the checks that showed it; the closest and the most often found first). Tell which of them holds here: "
             "take the check that tells them apart, then make sure it covers this case (the same runs, the same "
             "time); another cause is possible:"]
    for i, p in enumerate(found[:FULL]):
        times = f" (found {p['uses']} times)" if p["uses"] > 1 else ""
        lines.append(f"{i + 1}.{times} " + p["text"][:PATH_CHARS].replace("\n", "\n   "))
    rest = [p for p in found[FULL:] if p["cause"]]
    if rest:
        lines.append("Other causes found for such a problem:")
        for p in rest:
            lines.append(f"- {p['cause']}" + (f" (what confirms it: {p['confirm'][:260]})" if p["confirm"] else ""))
    if shown is not None:
        shown.update(f"recipe:{p['id']}" for p in found)
    return "\n".join(lines)
