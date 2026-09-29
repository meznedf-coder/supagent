"""Answering a question stored in supagent_message: the agent runs as the user who owns the
conversation, its steps are saved as they happen (the chat page shows them), then the answer
and the files it made (kept in the database: a Celery worker may run on another host)."""

from __future__ import annotations

import datetime as dt
import logging
import mimetypes
import os
import time
import traceback
from typing import Any

from superset import db

from supagent import settings
from supagent.agent import Cancelled
from supagent.models import Conversation, File, Message

log = logging.getLogger(__name__)
FILE_TOOLS = ("export_excel", "chart_from_sql", "chart_image")


LIVE = ("pending", "running")


def _save(message_id: int, live: tuple[str, ...] = LIVE, **values: Any) -> bool:
    """Write the progress or the end of an answer, only while it is being answered: False (and
    nothing written) once it was stopped. A stopped answer is never written over, even by a
    step that was already running when Stop was pressed."""
    values["updated_at"] = dt.datetime.utcnow()          # progress: the answer is alive
    n = (db.session.query(Message).filter(Message.id == message_id, Message.status.in_(live))
         .update(values, synchronize_session=False))
    db.session.commit()
    return bool(n)


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
QUERIES_ANSWERS = 2       # the next questions see the queries behind the last two answers
QUERIES_PER_ANSWER = 2


def queries_of(results: list | None) -> list[dict]:
    """The queries that gave the rows an answer showed (the last ones that returned rows), with the
    database and the time window: what "that finding" is for the next question."""
    out = []
    for r in [r for r in results or [] if r.get("row_count")][-QUERIES_PER_ANSWER:]:
        q = {k: r[k] for k in ("tool", "database", "database_id", "start", "end", "step") if r.get(k)}
        q.update(query=str(r.get("sql") or "")[:700], columns=list(r.get("columns") or [])[:10],
                 rows=r.get("row_count"))
        out.append(q)
    return out


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
            item = {"tool": tool, "sql": args.get("expr") or res.get("expr"), "database": res.get("database"),
                    "database_id": res.get("database_id")}
            item.update({k: res[k] for k in ("start", "end", "step") if res.get(k)})   # the finding's window
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


def _name_chat(conversation_id: int, question: str, earlier: list, trace: list[dict]) -> str | None:
    """After the first answer of a chat: a short generic name for it, written by the LLM (one
    short call, after the answer is saved: the user already has it). A chat renamed by its
    user is left alone."""
    from supagent.knowledge.experience import final_query
    from supagent.knowledge.generic import generalize

    if any(m.role == "assistant" and m.status == "done" for m in earlier):
        return None
    conv = db.session.get(Conversation, conversation_id)
    if conv is None or (conv.title or "") != (question or "")[:120]:
        return None
    try:
        tool, query = final_query(trace)
        db.session.commit()                              # no metadata connection held during the LLM call
        title = generalize(question, [], query, tool).get("title")
        conv = db.session.get(Conversation, conversation_id)
        if title and conv is not None and (conv.title or "") == (question or "")[:120]:
            conv.title = title[:255]
            db.session.commit()
        return title
    except Exception:  # pylint: disable=broad-except   (never breaks an answer)
        db.session.rollback()
        log.warning("supagent: naming the chat failed", exc_info=True)
        return None


def _record_usage(message_id: int, user_id: int | None, seconds: float, llm: dict[str, Any],
                  trace: list[dict]) -> None:
    """What the answer took (superset supagent stats); never breaks an answer."""
    from supagent.models import Usage

    try:
        db.session.merge(Usage(
            message_id=message_id, user_id=user_id, seconds=round(seconds, 1), llm_calls=int(llm.get("calls") or 0),
            llm_seconds=round(float(llm.get("seconds") or 0), 1), prompt_tokens=int(llm.get("prompt_tokens") or 0),
            completion_tokens=int(llm.get("completion_tokens") or 0), cached_tokens=int(llm.get("cached_tokens") or 0),
            tool_calls=len(trace), tool_seconds=round(sum(float(t.get("seconds") or 0) for t in trace), 1),
            failed_calls=sum(1 for t in trace if t.get("status") == "error"), nudges=int(llm.get("nudges") or 0)))
        db.session.commit()
    except Exception:  # pylint: disable=broad-except   (table not created yet: superset supagent init)
        db.session.rollback()
        log.info("supagent: usage of answer %s not recorded", message_id)


def run_answer(message_id: int, taking_over: bool = False) -> bool:
    """Compute the answer of an assistant message (status pending -> running -> done / error).
    The process that moves it from pending to running answers it, whatever the number of workers
    and web servers: False (nothing done) when another one took it first, or it was stopped."""
    from superset.extensions import security_manager

    from supagent.security import acting_as

    if not _save(message_id, live=("pending",), status="running"):
        return False                                     # taken by another process, or stopped
    if taking_over:
        log.info("supagent: no worker started answer %s in time: answered in the web server", message_id)
    msg = db.session.get(Message, message_id)
    if msg is None:
        return False
    conv = db.session.get(Conversation, msg.conversation_id)
    user = security_manager.get_user_by_id(conv.user_id) if conv else None
    user_id = conv.user_id if conv else None
    if user is None:
        _save(message_id, status="error", content="the user of this conversation no longer exists",
              finished_at=dt.datetime.utcnow())
        return True
    username = user.username
    earlier = (db.session.query(Message).filter(Message.conversation_id == conv.id, Message.id < message_id)
               .order_by(Message.id).all())
    question = next((m.content for m in reversed(earlier) if m.role == "user"), "")
    done = [m for m in earlier[:-1] if m.status == "done" and m.content]
    history = [{"role": m.role, "content": m.content} for m in done]
    with_rows = [(h, m) for h, m in zip(history, done) if m.role == "assistant" and m.results]
    for h, m in with_rows[-QUERIES_ANSWERS:]:            # "that finding": the queries behind the last answers
        h["queries"] = queries_of(m.results)

    def on_step(trace: list[dict]) -> None:
        if not _save(message_id, steps=_steps_for_page(trace)):
            raise Cancelled("stopped by the user")

    def should_stop() -> bool:
        # a column query reads the row as committed now (no cached object, nothing expired);
        # the commit ends the read's transaction, so that no connection of Superset's pool is
        # held during the LLM call or the tool that follows (many answers at once)
        status = db.session.query(Message.status).filter(Message.id == message_id).scalar()
        db.session.commit()
        return status != "running"

    from supagent.agent import Agent

    agent = None
    trace: list[dict] = []
    started = time.time()
    try:
        with acting_as(username):
            agent = Agent(username, on_step=on_step, rich_results=True, should_stop=should_stop)
            try:
                answer, trace = agent.ask(question, history)
            finally:
                agent.close()
            try:
                from supagent.tools_superset import drop_unused_datasets

                dropped = drop_unused_datasets(trace)       # datasets of tries that did not make the chart
                if dropped:
                    log.info("supagent: answer %s: unused datasets %s deleted", message_id, dropped)
            except Exception:  # pylint: disable=broad-except
                db.session.rollback()
            if should_stop():
                raise Cancelled("stopped by the user")
            files = _keep_files(message_id, _files_of(trace))
            if not _save(message_id, content=answer, status="done", steps=_steps_for_page(trace), files=files,
                         results=_results_of(trace), finished_at=dt.datetime.utcnow()):
                kept = [f["id"] for f in files if f.get("id")]    # stopped meanwhile: nothing of it stays
                if kept:
                    db.session.query(File).filter(File.id.in_(kept)).delete(synchronize_session=False)
                    db.session.commit()
                return True
            _record_usage(message_id, user_id, time.time() - started, getattr(agent, "usage", None) or {}, trace)
            from supagent.knowledge.experience import learn_from_answer

            learn_from_answer(message_id, user_id, question, trace)      # query timings
            _name_chat(conv.id, question, earlier, trace)
            try:
                from supagent.knowledge.memory import learn_from_message, worth_learning

                if worth_learning(question):      # "always...", "from now on...", "remember..."
                    learn_from_message(message_id)
            except Exception:  # pylint: disable=broad-except
                db.session.rollback()
    except Cancelled:
        db.session.rollback()
        _save(message_id, live=LIVE + ("cancelling",), status="cancelled", content="(stopped)",
              finished_at=dt.datetime.utcnow())
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent: answer %s failed", message_id)
        detail = "".join(traceback.format_exception_only(type(ex), ex)).strip()[-1500:]
        _save(message_id, status="error", content=f"The agent could not answer: {detail}",
              finished_at=dt.datetime.utcnow())
    finally:
        db.session.remove()
    return True


def purge_old_chats() -> int:
    """Chats nobody used for chats.keep_days days (0: never) go, 100 at a time, with their messages,
    files and usage; what they taught stays (learned answers, memory, associations)."""
    from sqlalchemy import func

    from supagent.models import Usage

    days = int(settings.get("chats.keep_days") or 0)
    if days <= 0:
        return 0
    limit = dt.datetime.utcnow() - dt.timedelta(days=days)
    last = (db.session.query(Message.conversation_id, func.max(Message.created_at).label("at"))
            .group_by(Message.conversation_id).subquery())
    old = [cid for (cid,) in db.session.query(Conversation.id).outerjoin(last, last.c.conversation_id == Conversation.id)
           .filter(func.coalesce(last.c.at, Conversation.created_at) < limit)]
    for i in range(0, len(old), 100):
        ids = old[i:i + 100]
        mids = [m for (m,) in db.session.query(Message.id).filter(Message.conversation_id.in_(ids))]
        for j in range(0, len(mids), 500):
            part = mids[j:j + 500]
            db.session.query(File).filter(File.message_id.in_(part)).delete(synchronize_session=False)
            db.session.query(Usage).filter(Usage.message_id.in_(part)).delete(synchronize_session=False)
        db.session.query(Message).filter(Message.conversation_id.in_(ids)).delete(synchronize_session=False)
        db.session.query(Conversation).filter(Conversation.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
    return len(old)


def purge_old_files() -> int:
    limit = dt.datetime.utcnow() - dt.timedelta(days=int(settings.get("tools.keep_days")))
    n = db.session.query(File).filter(File.created_at < limit).delete(synchronize_session=False)
    db.session.commit()
    return n
