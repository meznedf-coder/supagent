"""How well the agent is doing, measured on what already exists (no labelling, no GUI):

* evaluate_resolver: the learned answers users marked Helpful (or admins confirmed) say where the
  data of their question was; is it among what the resolver ("Where the data is") gives first?
  hit@1, hit@3 and mean reciprocal rank. Run after each learning run and by
  `superset supagent evaluate`: it tells whether the learning improves, and catches regressions.
* gaps: the questions the agent did not answer well (marked Not helpful, failed, or with no data
  found for their words) and the learned answers about data that no longer exists: what to add
  to the dictionary (synonyms, descriptions) or the catalog. `superset supagent gaps`.
* usage_stats: where the time of the answers goes (LLM, tools), the prompt sizes and the share the
  LLM server's prompt cache saved. `superset supagent stats`.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

from superset import db


def _expected(r: Any, backends: dict[int, str]) -> set[tuple[int, str]]:
    """(database id, metric or index name) a learned answer's query reads."""
    from supagent.knowledge.experience import METRIC_FILTER, promql_pattern, sql_pattern

    if not r.query or not r.database_id:
        return set()
    if r.tool == "promql_query":
        _p, names = promql_pattern(r.query)
    elif r.tool in ("execute_sql", "export_excel"):
        _p, names = sql_pattern(r.query)
    else:
        return set()
    out = set()
    for name in names:
        if name == "all_metrics":
            for m in METRIC_FILTER.finditer(r.query):
                out |= {(r.database_id, n) for n in re.findall(r"'([^']+)'", m.group(0))}
        else:
            out.add((r.database_id, name))
    return out


def evaluate_resolver(limit: int = 200, seconds: float = 60.0) -> dict[str, Any]:
    """hit@1, hit@3 and MRR of the resolver on the Helpful / confirmed learned answers (the newest
    `limit`, at most `seconds`); run as a user who may query their databases."""
    from superset.models.core import Database

    from supagent.knowledge.experience import USED
    from supagent.knowledge.resolve import resolve
    from supagent.models import Recipe

    rows = (db.session.query(Recipe).filter(Recipe.status.in_(USED), Recipe.database_id.isnot(None))
            .order_by(Recipe.last_used_at.desc()).limit(limit).all())
    backends = {d.id: d.backend for d in db.session.query(Database)}
    t0 = time.time()
    ranks: list[int | None] = []
    misses: list[str] = []
    for r in rows:
        if time.time() - t0 > seconds:
            break
        want = _expected(r, backends)
        if not want:
            continue
        found = resolve(r.question or "")
        rank = None
        for i, c in enumerate(found):
            ids = {c["database"].id} | set(c.get("elsewhere") or [])
            if any((i2, c["name"]) in want for i2 in ids):
                rank = i
                break
        ranks.append(rank)
        if rank is None and len(misses) < 10:
            misses.append(f"{(r.question or '')[:100]} -> {', '.join(sorted(n for _d, n in want))[:120]}")
    n = len(ranks)
    if not n:
        return {"answers": 0}
    return {"answers": n, "hit_at_1": round(sum(1 for x in ranks if x == 0) / n, 3),
            "hit_at_3": round(sum(1 for x in ranks if x is not None and x < 3) / n, 3),
            "mrr": round(sum(1 / (x + 1) for x in ranks if x is not None) / n, 3), "not_found": misses}


def _question_of(m: Any) -> str:
    from supagent.models import Message

    q = (db.session.query(Message.content).filter(Message.conversation_id == m.conversation_id, Message.id < m.id,
                                                  Message.role == "user").order_by(Message.id.desc()).first())
    return (q[0] if q else "") or ""


def gaps(days: int = 30, limit: int = 50) -> dict[str, list[str]]:
    """What the agent did not answer well in the last `days`, and the learned answers about data
    that no longer exists."""
    from supagent.knowledge.experience import USED
    from supagent.knowledge.resolve import mentions_gone, resolve, terms
    from supagent.models import Message, Recipe

    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    answers = (db.session.query(Message).filter(Message.role == "assistant", Message.created_at >= since)
               .order_by(Message.id.desc()).limit(2000).all())
    out: dict[str, list[str]] = {"not_helpful": [], "failed": [], "no_data_found": [], "learned_on_gone_data": []}
    seen: set[str] = set()
    for m in answers:
        q = _question_of(m)
        if not q or q in seen:
            continue
        seen.add(q)
        when = f"{m.created_at:%Y-%m-%d}" if m.created_at else ""
        if m.feedback == -1 and len(out["not_helpful"]) < limit:
            out["not_helpful"].append(f"{when} {q[:160]}")
        elif m.status == "error" and len(out["failed"]) < limit:
            out["failed"].append(f"{when} {q[:160]} -> {(m.content or '')[:120]}")
        if len(out["no_data_found"]) < limit and len(seen) <= 300 and terms(q) and not resolve(q):
            out["no_data_found"].append(f"{when} {q[:160]} (words: {', '.join(terms(q)[:8])})")
    for r in db.session.query(Recipe).filter(Recipe.status.in_(USED)).limit(5000):
        if mentions_gone(r.query) and len(out["learned_on_gone_data"]) < limit:
            out["learned_on_gone_data"].append(f"#{r.id} {(r.question or '')[:120]}")
    return out


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))], 1)


def usage_stats(days: int = 7) -> dict[str, Any]:
    """Where the time of the answers of the last `days` went."""
    from supagent.models import Message, Usage

    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    rows = db.session.query(Usage).filter(Usage.created_at >= since).all()
    if not rows:
        return {"answers": 0}
    total = sum(u.seconds or 0 for u in rows) or 1.0
    calls = sum(u.llm_calls or 0 for u in rows) or 1
    prompt = sum(u.prompt_tokens or 0 for u in rows)
    slow = sorted(rows, key=lambda u: -(u.seconds or 0))[:5]
    out = {"answers": len(rows), "seconds_median": _pct([u.seconds or 0 for u in rows], 0.5),
           "seconds_p90": _pct([u.seconds or 0 for u in rows], 0.9),
           "llm_share": round(sum(u.llm_seconds or 0 for u in rows) / total, 3),
           "tool_share": round(sum(u.tool_seconds or 0 for u in rows) / total, 3),
           "llm_calls_per_answer": round(calls / len(rows), 2),
           "tool_calls_per_answer": round(sum(u.tool_calls or 0 for u in rows) / len(rows), 2),
           "failed_calls_per_answer": round(sum(u.failed_calls or 0 for u in rows) / len(rows), 2),
           "llm_seconds_per_call": round(sum(u.llm_seconds or 0 for u in rows) / calls, 1),
           "prompt_tokens_per_call": round(prompt / calls),
           "completion_tokens_per_call": round(sum(u.completion_tokens or 0 for u in rows) / calls),
           "prompt_cache_share": round(sum(u.cached_tokens or 0 for u in rows) / prompt, 3) if prompt else 0.0,
           "slowest": []}
    for u in slow:
        m = db.session.get(Message, u.message_id)
        q = _question_of(m) if m is not None else ""
        out["slowest"].append(f"#{u.message_id} {u.seconds:.0f} s (LLM {u.llm_seconds:.0f} s in {u.llm_calls} calls, "
                              f"tools {u.tool_seconds:.0f} s in {u.tool_calls}): {q[:100]}")
    return out
