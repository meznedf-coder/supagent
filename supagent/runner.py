"""Answering a question stored in supagent_message: the agent runs as the user who owns the
conversation, its steps are saved as they happen (the chat page shows them), then the answer
and the files it made (kept in the database: a Celery worker may run on another host)."""

from __future__ import annotations

import datetime as dt
import logging
import mimetypes
import os
import traceback
from typing import Any

from superset import db

from supagent import settings
from supagent.models import Conversation, File, Message

log = logging.getLogger(__name__)
FILE_TOOLS = ("export_excel", "chart_from_sql", "chart_image")


def _save(message_id: int, **values: Any) -> None:
    msg = db.session.get(Message, message_id)
    if msg is None:
        return
    for k, v in values.items():
        setattr(msg, k, v)
    msg.updated_at = dt.datetime.utcnow()          # progress: the answer is alive
    db.session.commit()


def _steps_for_page(trace: list[dict]) -> list[dict]:
    out = []
    for t in trace:
        out.append({"tool": t.get("called") or t["tool"], "status": t.get("status"), "seconds": t.get("seconds"),
                    "args": t.get("args"), "result": (t.get("result") or "")[:1500]})
    return out


def _files_of(trace: list[dict]) -> list[dict]:
    """Files the tools wrote (path, and for extracts the row count)."""
    import json

    out: list[dict] = []
    for t in trace:
        if (t.get("called") or t["tool"]) not in FILE_TOOLS or t.get("status") != "done":
            continue
        try:
            res = json.loads(t.get("result") or "{}")
        except ValueError:
            continue
        path = res.get("path") if isinstance(res, dict) else None
        if path and os.path.isfile(path) and all(f["path"] != path for f in out):
            out.append({"path": path, "rows": res.get("rows"), "truncated": res.get("truncated")})
    return out


RESULT_ROWS = 5000        # rows of one query result kept for the page (table, chart, CSV / Excel)
RESULTS_KEPT = 6


def _promql_rows(res: dict) -> tuple[list[str], list[list]]:
    """promql_query series -> long rows (time, series, value): a line chart per series."""
    rows: list[list] = []
    for s in res.get("series") or []:
        labels = s.get("labels") or {}
        name = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) or res.get("expr", "value")
        for t, v in s.get("values") or []:
            rows.append([t, name, v])
    return ["time", "series", "value"], rows


def _results_of(trace: list[dict]) -> list[dict]:
    """The rows of the queries the agent ran (successful ones), newest last."""
    import json

    out = []
    for t in trace:
        full = t.get("full")
        if not full:
            continue
        try:
            res = json.loads(full)
        except ValueError:
            continue
        if not isinstance(res, dict) or res.get("error") or res.get("success") is False:
            continue
        args = t.get("args") or {}
        tool = t.get("called") or t["tool"]
        if tool == "execute_sql":
            req = args.get("request") or args
            columns = [c.get("name") if isinstance(c, dict) else str(c) for c in res.get("columns") or []]
            rows = [[r.get(c) for c in columns] if isinstance(r, dict) else list(r) for r in res.get("rows") or []]
            item = {"tool": tool, "sql": req.get("sql"), "database": res.get("database"),
                    "database_id": req.get("database_id")}
        elif tool == "promql_query":
            columns, rows = _promql_rows(res)
            item = {"tool": tool, "sql": args.get("expr") or res.get("expr"), "database": res.get("database")}
        else:
            continue
        if not columns:
            continue
        item.update(columns=columns, rows=rows[:RESULT_ROWS], row_count=len(rows),
                    truncated=bool(res.get("truncated")) or len(rows) > RESULT_ROWS)
        out.append(item)
    if any(r["row_count"] for r in out):          # empty probes only clutter the page
        out = [r for r in out if r["row_count"]]
    return out[-RESULTS_KEPT:]


def _keep_files(message_id: int, files: list[dict]) -> list[dict]:
    limit = int(settings.get("tools.max_file_mb")) * 1024 * 1024
    out = []
    for item in files:
        path = item["path"]
        size = os.path.getsize(path)
        name = os.path.basename(path)
        extra = {k: item[k] for k in ("rows", "truncated") if item.get(k) is not None}
        if size > limit:
            out.append({"name": name, "size": size, **extra,
                        "note": f"too big to keep here ({size // 1048576} MB): {path}"})
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        f = File(message_id=message_id, name=name, mime=mime, size=size, data=data)
        db.session.add(f)
        db.session.flush()
        out.append({"id": f.id, "name": name, "mime": mime, "size": size, **extra})
    db.session.commit()
    return out


def run_answer(message_id: int) -> None:
    """Compute the answer of an assistant message (status pending -> running -> done / error)."""
    from superset.extensions import security_manager

    from supagent.agent import Agent, Cancelled
    from supagent.security import acting_as

    msg = db.session.get(Message, message_id)
    if msg is None or msg.status not in ("pending", "running"):
        return
    conv = db.session.get(Conversation, msg.conversation_id)
    user = security_manager.get_user_by_id(conv.user_id) if conv else None
    user_id = conv.user_id if conv else None
    if user is None:
        _save(message_id, status="error", content="the user of this conversation no longer exists",
              finished_at=dt.datetime.utcnow())
        return
    username = user.username
    earlier = (db.session.query(Message).filter(Message.conversation_id == conv.id, Message.id < message_id)
               .order_by(Message.id).all())
    question = next((m.content for m in reversed(earlier) if m.role == "user"), "")
    history = [{"role": m.role, "content": m.content} for m in earlier[:-1]
               if m.status == "done" and m.content]
    _save(message_id, status="running")

    def on_step(trace: list[dict]) -> None:
        _save(message_id, steps=_steps_for_page(trace))

    def should_stop() -> bool:
        # a column query reads the row as committed now (no cached object, nothing expired)
        return db.session.query(Message.status).filter(Message.id == message_id).scalar() in (None, "cancelling")

    agent = None
    trace: list[dict] = []
    try:
        with acting_as(username):
            agent = Agent(username, on_step=on_step, rich_results=True, should_stop=should_stop)
            try:
                answer, trace = agent.ask(question, history)
            finally:
                agent.close()
            files = _keep_files(message_id, _files_of(trace))
            _save(message_id, content=answer, status="done", steps=_steps_for_page(trace), files=files,
                  results=_results_of(trace), finished_at=dt.datetime.utcnow())
            from supagent.knowledge.experience import learn_from_answer

            learn_from_answer(message_id, user_id, question, trace)
            try:
                from supagent.knowledge.index import sync

                sync(("recipe:",))
                from supagent.knowledge.memory import learn_from_message, worth_learning

                if worth_learning(question):      # "always...", "from now on...", "remember..."
                    learn_from_message(message_id)
            except Exception:  # pylint: disable=broad-except
                db.session.rollback()
    except Cancelled:
        db.session.rollback()
        _save(message_id, status="cancelled", content="(stopped)", finished_at=dt.datetime.utcnow())
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent: answer %s failed", message_id)
        detail = "".join(traceback.format_exception_only(type(ex), ex)).strip()[-1500:]
        _save(message_id, status="error", content=f"The agent could not answer: {detail}",
              finished_at=dt.datetime.utcnow())
    finally:
        db.session.remove()


def purge_old_files() -> int:
    limit = dt.datetime.utcnow() - dt.timedelta(days=int(settings.get("tools.keep_days")))
    n = db.session.query(File).filter(File.created_at < limit).delete(synchronize_session=False)
    db.session.commit()
    return n
