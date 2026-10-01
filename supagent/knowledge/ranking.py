"""What the agent learned, ranked by what happened in the discussions and the data requests (0.6). After every
answer: the knowledge items its prompt was given (learned answers, catalog entries, memories, documents, Context
pages), the tables its queries read, the learned queries it ran again (and whether they worked). Then, from what
people said of those answers (Helpful, Not helpful), each learned item has a usefulness:

  score   (helpful + 1) / (helpful + not helpful + 2): 0.5 for an item nobody rated yet, up with each Helpful
          answer it was given to, down with each Not helpful one
  weight  how sure: the rated answers, up to RATED_FULL
  reused  its query run again by later answers, and how many of them failed
  last    when it was last given; unused for UNUSED_DAYS: proposed for retirement

The learned answers proposed for a question use it (the useful ones first, the ones people keep saying are wrong
not at all), and the Data dictionary's Learned tab is sorted by it with its reasons.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from superset import db

log = logging.getLogger(__name__)

RANKED = ("recipe:", "entry:", "memory:", "doc:", "context:")    # the items ranked (the tables: data:, as used)
RATED_FULL = 5          # rated answers at which the score counts in full
UNUSED_DAYS = 90
DEMOTE = 0.25           # a score this low with RATED_FULL ratings: no longer proposed


def record(message_id: int, user_id: int | None, given: set[str] | None, trace: list[dict],
           route_id: int | None = None) -> int:
    """The uses of one answer: given (its prompt's items), used (the tables its queries read), reused / failed
    (a learned answer's query run again). Never breaks an answer."""
    from supagent.models import ItemUse

    rows: dict[tuple[str, str], None] = {}
    for ref in sorted(given or ()):
        base = ref.split("#", 1)[0]
        if base.startswith(RANKED):
            rows[(base, "given")] = None
    try:
        from supagent.knowledge.experience import _tables_read

        for dbid, _kind, table in _tables_read(trace):          # (database id, metric | index, name)
            rows[(f"data:{dbid}:{table}"[:128], "used")] = None
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent ranking: the tables read", exc_info=True)
    for rid, ok in _reused(trace).items():
        rows[(f"recipe:{rid}", "reused" if ok else "failed")] = None
    try:
        for ref, how in rows:
            db.session.add(ItemUse(ref=ref, how=how, message_id=message_id, user_id=user_id, route_id=route_id))
        db.session.commit()
        return len(rows)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        log.warning("supagent ranking: uses not recorded", exc_info=True)
        return 0


def _reused(trace: list[dict]) -> dict[int, bool]:
    """The learned answers whose query this answer ran again (the same shape of query): {recipe id: worked}."""
    from supagent.knowledge.experience import _query_of, promql_pattern, signature, sql_pattern
    from supagent.models import Recipe

    sigs: dict[str, bool] = {}
    for t in trace or []:
        tool = t.get("called") or t.get("tool") or ""
        q, _db = _query_of(tool, t.get("args") or {})
        if not q:
            continue
        pattern = promql_pattern(q)[0] if tool == "promql_query" else sql_pattern(q)[0]
        s = signature(f"{tool}:{pattern}")                    # as a learned answer's (record_recipe)
        ok = t.get("status") != "error" and not str(t.get("result") or "").lstrip().lower().startswith("error")
        sigs[s] = sigs.get(s, False) or ok
    if not sigs:
        return {}
    out: dict[int, bool] = {}
    for r in db.session.query(Recipe.id, Recipe.signature).filter(Recipe.signature.in_(list(sigs))):
        out[r.id] = sigs[r.signature]
    return out


def usefulness(refs: list[str] | None = None, days: int = 365) -> dict[str, dict[str, Any]]:
    """{ref: {given, helpful, not_helpful, reused, failed, last, score, weight, unused_days}} over the last `days`
    (refs: these only), from the answers' feedback."""
    from sqlalchemy import case, func

    from supagent.models import ItemUse, Message

    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    q = (db.session.query(ItemUse.ref, ItemUse.how,
                          func.count(func.distinct(ItemUse.message_id)),
                          func.sum(case((Message.feedback == 1, 1), else_=0)),
                          func.sum(case((Message.feedback == -1, 1), else_=0)),
                          func.max(ItemUse.created_at))
         .outerjoin(Message, Message.id == ItemUse.message_id)
         .filter(ItemUse.created_at >= since))
    if refs is not None:
        if not refs:
            return {}
        q = q.filter(ItemUse.ref.in_(list(refs)))
    out: dict[str, dict[str, Any]] = {}
    now = dt.datetime.utcnow()
    try:
        rows = q.group_by(ItemUse.ref, ItemUse.how).all()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        log.warning("supagent ranking: usefulness", exc_info=True)
        return {}
    for ref, how, n, good, bad, last in rows:
        u = out.setdefault(ref, {"given": 0, "helpful": 0, "not_helpful": 0, "reused": 0, "failed": 0, "used": 0,
                                 "last": None})
        if how == "given":
            u["given"], u["helpful"], u["not_helpful"] = int(n or 0), int(good or 0), int(bad or 0)
        elif how in ("reused", "failed", "used"):
            u[how] = int(n or 0)
        if last is not None and (u["last"] is None or last > u["last"]):
            u["last"] = last
    for u in out.values():
        rated = u["helpful"] + u["not_helpful"]
        u["score"] = round((u["helpful"] + 1) / (rated + 2), 3)
        u["weight"] = round(min(1.0, rated / RATED_FULL), 2)
        u["unused_days"] = (now - u["last"]).days if u["last"] else None
        u["last"] = u["last"].isoformat(timespec="minutes") if u["last"] else None
    return out


def adjust(u: dict[str, Any] | None) -> float:
    """What a learned item's usefulness adds to its match with a question: -1..+1 (sure and good: +1; sure and
    bad: -1; not rated: 0), a reused query that fails counting against it."""
    if not u:
        return 0.0
    value = (u["score"] - 0.5) * 2 * u["weight"]
    tried = u["reused"] + u["failed"]
    if tried:
        value += 0.5 * (u["reused"] - u["failed"]) / tried * min(1.0, tried / RATED_FULL)
    return max(-1.0, min(1.0, value))


def demoted(u: dict[str, Any] | None) -> bool:
    """People said often enough that the answers it was given to were wrong: no longer proposed."""
    return bool(u) and u["weight"] >= 1.0 and u["score"] <= DEMOTE


def reasons(u: dict[str, Any] | None) -> str:
    """Its usefulness in words (the Learned tab)."""
    if not u:
        return "not used yet"
    parts = [f"given to {u['given']} answer{'s' if u['given'] != 1 else ''}"]
    if u["helpful"] or u["not_helpful"]:
        parts.append(f"{u['helpful']} helpful, {u['not_helpful']} not helpful")
    if u["reused"] or u["failed"]:
        parts.append(f"query run again {u['reused'] + u['failed']} time{'s' if u['reused'] + u['failed'] != 1 else ''}"
                     + (f", {u['failed']} failed" if u["failed"] else ""))
    if u.get("unused_days") is not None and u["unused_days"] >= UNUSED_DAYS:
        parts.append(f"not used for {u['unused_days']} days")
    if demoted(u):
        parts.append("no longer proposed")
    return "; ".join(parts)


def purge(days: int = 400) -> int:
    """Uses older than this are forgotten (the usefulness reads the last year)."""
    from supagent.models import ItemUse

    try:
        n = db.session.query(ItemUse).filter(
            ItemUse.created_at < dt.datetime.utcnow() - dt.timedelta(days=days)).delete(synchronize_session=False)
        db.session.commit()
        return int(n or 0)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return 0
