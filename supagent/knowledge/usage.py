"""The LLM usage report of the admins' page: every call to the LLM (supagent_llm_call, what it was for
and for whom), and the answers (supagent_usage): tokens, calls, context sizes, the prompt cache, times,
errors, per person, per task, per model, over time; the biggest contexts and the slowest answers."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from superset import db

MAX_ROWS = 500_000
TASKS = {"answer": "Answers in the chat", "learn": "Daily learning (descriptions)", "context": "Context (nightly)",
         "memory": "Memory from the chats", "helpful": "Learned answers (Helpful)", "catalog": "Agent catalog",
         "tidy": "Tidying learned answers", "names": "Chat names", "test": "LLM tests",
         "classify": "Categories of the knowledge", "charts": "What the charts show",
         "background": "Other background work",
         "other": "Other"}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, int(math.ceil(p * len(v))) - 1)], 2)


def _bucket(at: dt.datetime, grain: str, offset: dt.timedelta) -> str:
    local = at + offset
    return local.strftime("%Y-%m-%d %H:00") if grain == "hour" else local.strftime("%Y-%m-%d")


def purge(keep_days: int) -> int:
    """Calls older than keep_days removed (the hourly tick)."""
    from supagent.models import LLMCall

    if keep_days <= 0:
        return 0
    n = db.session.query(LLMCall).filter(LLMCall.at < dt.datetime.utcnow() - dt.timedelta(days=keep_days)).delete(
        synchronize_session=False)
    db.session.commit()
    return n


def report(start: dt.datetime, end: dt.datetime, grain: str = "hour", tz_offset_minutes: int = 0) -> dict[str, Any]:
    """The usage between start and end (UTC), bucketed by hour or day in the viewer's time zone."""
    from superset.extensions import security_manager

    from supagent.models import LLMCall, Message, Usage

    offset = dt.timedelta(minutes=tz_offset_minutes)
    grain = "day" if grain == "day" else "hour"
    rows = (db.session.query(LLMCall.at, LLMCall.task, LLMCall.user_id, LLMCall.model, LLMCall.prompt_tokens,
                             LLMCall.completion_tokens, LLMCall.cached_tokens, LLMCall.seconds, LLMCall.ok,
                             LLMCall.message_id, LLMCall.tools)
            .filter(LLMCall.at >= start, LLMCall.at < end).order_by(LLMCall.at).limit(MAX_ROWS).all())
    names: dict[Any, str] = {}

    def who(user_id: Any) -> str:
        if user_id is None:
            return "(nightly and background work)"
        if user_id not in names:
            u = security_manager.get_user_by_id(user_id)
            names[user_id] = u.username if u is not None else f"user {user_id}"
        return names[user_id]

    total: dict[str, Any] = defaultdict(float)
    series: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_task: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    by_user: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    by_model: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    seconds, prompts = [], []
    biggest: list[tuple[int, Any]] = []
    answers_of: dict[str, set] = defaultdict(set)
    for r in rows:
        prompt, completion, cached = r.prompt_tokens or 0, r.completion_tokens or 0, r.cached_tokens or 0
        task = r.task or "other"
        for d in (total, by_task[task], by_user[who(r.user_id)], by_model[r.model or "?"]):
            d["calls"] += 1
            d["prompt_tokens"] += prompt
            d["completion_tokens"] += completion
            d["cached_tokens"] += cached
            d["seconds"] += r.seconds or 0
            d["errors"] += 0 if r.ok else 1
            d["max_prompt"] = max(d["max_prompt"], prompt)
        s = series[(_bucket(r.at, grain, offset), TASKS.get(task, task))]
        s["tokens"] += prompt + completion
        s["calls"] += 1
        seconds.append(r.seconds or 0)
        prompts.append(prompt)
        if r.message_id and task == "answer":
            answers_of[who(r.user_id)].add(r.message_id)
        biggest.append((prompt, r))
    biggest.sort(key=lambda x: -x[0])

    def finish(d: dict[str, Any]) -> dict[str, Any]:
        calls = d.get("calls") or 0
        out = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}
        out["tokens"] = int(d.get("prompt_tokens", 0) + d.get("completion_tokens", 0))
        out["avg_prompt"] = round(d.get("prompt_tokens", 0) / calls) if calls else 0
        out["avg_seconds"] = round(d.get("seconds", 0) / calls, 2) if calls else 0
        out["cache_share"] = round(100 * d.get("cached_tokens", 0) / d["prompt_tokens"], 1) if d.get("prompt_tokens") else 0
        for k in ("calls", "errors", "prompt_tokens", "completion_tokens", "cached_tokens", "max_prompt"):
            out[k] = int(d.get(k, 0))
        return out

    totals = finish(total)
    totals.update(p95_seconds=_pct(seconds, 0.95), p95_prompt=int(_pct(prompts, 0.95)),
                  people=len([u for u in by_user if not u.startswith("(")]))
    usages = (db.session.query(Usage).filter(Usage.created_at >= start, Usage.created_at < end)
              .order_by(Usage.seconds.desc()).limit(50_000).all())
    answer_seconds = [u.seconds or 0 for u in usages]
    marked = (db.session.query(Message.id).filter(Message.role == "assistant", Message.created_at >= start,
                                                  Message.created_at < end, Message.content.like("%(Check:%")).count())
    answers = {"count": len(usages), "avg_seconds": round(sum(answer_seconds) / len(usages), 1) if usages else 0,
               "p95_seconds": _pct(answer_seconds, 0.95),
               "avg_llm_calls": round(sum(u.llm_calls or 0 for u in usages) / len(usages), 1) if usages else 0,
               "avg_tool_calls": round(sum(u.tool_calls or 0 for u in usages) / len(usages), 1) if usages else 0,
               "failed_tool_calls": sum(u.failed_calls or 0 for u in usages),
               "sent_back": sum(u.nudges or 0 for u in usages), "marked": marked}
    questions = {}
    slow_ids = [u.message_id for u in usages[:10]]
    for m in db.session.query(Message).filter(Message.id.in_(slow_ids or [-1])):
        q = (db.session.query(Message.content).filter(Message.conversation_id == m.conversation_id,
                                                     Message.id < m.id, Message.role == "user")
             .order_by(Message.id.desc()).first())
        questions[m.id] = (q[0] if q else "")[:160]
    slowest = [{"message_id": u.message_id, "user": who(u.user_id), "at": u.created_at, "seconds": u.seconds,
                "llm_calls": u.llm_calls, "tool_calls": u.tool_calls, "prompt_tokens": u.prompt_tokens,
                "question": questions.get(u.message_id, "")} for u in usages[:10]]
    people = []
    for name, d in by_user.items():
        row = finish(d)
        row.update(user=name, answers=len(answers_of.get(name, ())))
        people.append(row)
    return {
        "start": start, "end": end, "grain": grain, "truncated": len(rows) >= MAX_ROWS, "totals": totals,
        "answers": answers,
        "series": {"columns": ["time", "task", "tokens", "calls"],
                   "rows": [[b, t, int(v["tokens"]), int(v["calls"])] for (b, t), v in sorted(series.items())]},
        "by_task": sorted(({**finish(d), "task": k, "label": TASKS.get(k, k)} for k, d in by_task.items()),
                          key=lambda x: -x["tokens"]),
        "by_user": sorted(people, key=lambda x: -x["tokens"]),
        "by_model": sorted(({**finish(d), "model": k} for k, d in by_model.items()), key=lambda x: -x["tokens"]),
        "biggest_contexts": [{"at": r.at, "task": TASKS.get(r.task or "other", r.task), "user": who(r.user_id),
                              "prompt_tokens": p, "cached_tokens": r.cached_tokens or 0, "seconds": r.seconds,
                              "message_id": r.message_id} for p, r in biggest[:10]],
        "slowest_answers": slowest,
    }
