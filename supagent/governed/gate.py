"""The gate of the decider: how much each clue counts when ranking the tables a question may need,
learned from the answers people confirmed (the gate of a mixture of experts, as a small linear model
whose weights stay readable).

A table's score is the sum of weight x feature, each feature from 0 to 1:

  resolver   names, descriptions, synonyms and associations match the question (the resolver's score)
  rank       its place in the resolver's list
  first      the resolver's policy put this database first (the same table in several databases)
  value      a value the question names (BILLING_API, srv-a-1) is in one of its fields or labels
  named      the question names its database, or a chart or dashboard that reads it
  recipe     a learned answer to a similar question read it
  search     its knowledge piece was found for the question (words, meaning)
  meaning    found by meaning (vectors) too
  preferred  the policy of the databases, when the same table is in several of them
  charts     the team's charts read it
  prior      confirmed answers to questions with the same words read it
  family     the question names its kind of source ("from the Prometheus metrics", "the jobs index")
  ai_only    only an AI-written, unverified description says what it is

Learning (learn()): each answer records the tables shown with their features and the tables its
queries read (supagent_route). Only routes a person confirmed teach: Helpful, an admin's confirmation,
the reply to a question back. A pairwise logistic step makes the table read score above the ones not
read; each step is small, pulled back towards the defaults and clipped to a quarter to four times the
default: learning refines the defaults, it never replaces them. With no confirmed route, the defaults.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

from superset import db

log = logging.getLogger(__name__)

DEFAULTS: dict[str, float] = {"resolver": 1.0, "rank": 0.5, "first": 0.3, "value": 1.2, "named": 2.5,
                              "recipe": 0.8, "search": 0.6, "meaning": 0.2, "preferred": 0.4, "charts": 0.2,
                              "prior": 0.8, "family": 0.8, "ai_only": -0.1, "spelling": 0.3, "neighbors": 1.0}
CONFIRMED = ("helpful", "confirmed", "clarified")
META_KEY = "decider_weights"
LEARN_RATE = 0.05
PULL = 0.02                   # towards the defaults, at every step
EPOCHS = 4
NEGATIVES = 8                 # tables shown but not read, per route (the best scored ones)
MIN_ROUTES = 5                # fewer confirmed routes: the defaults
TTL = 300.0

_CACHE: dict[str, tuple[float, Any]] = {}


def _bounds(name: str) -> tuple[float, float]:
    d = DEFAULTS[name]
    lo, hi = d / 4, d * 4
    return (min(lo, hi), max(lo, hi))


def weights() -> dict[str, float]:
    """The learned weights (the defaults for any feature they lack), cached TTL seconds."""
    hit = _CACHE.get("weights")
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out = dict(DEFAULTS)
    try:
        from supagent.models import Meta

        row = db.session.get(Meta, META_KEY)
        if row and row.value:
            learned = json.loads(row.value).get("weights") or {}
            for k, v in learned.items():
                if k in DEFAULTS:
                    lo, hi = _bounds(k)
                    out[k] = min(hi, max(lo, float(v)))
    except Exception:  # pylint: disable=broad-except   (the defaults)
        db.session.rollback()
    _CACHE["weights"] = (time.time(), out)
    return out


def score(features: dict[str, float], w: dict[str, float] | None = None) -> float:
    w = w or weights()
    return sum(w.get(k, 0.0) * float(v) for k, v in features.items())


def priors(terms: Iterable[str]) -> dict[str, float]:
    """subject -> how often confirmed answers to questions with these words read it (0..1): for each
    word, the share of its confirmed routes that read the subject; the best word counts."""
    counts = _term_counts()
    out: dict[str, float] = {}
    for t in terms:
        per = counts.get(t)
        if not per:
            continue
        total = sum(per.values())
        for subject, n in per.items():
            out[subject] = max(out.get(subject, 0.0), n / (1.0 + total))
    return out


def _term_counts() -> dict[str, Counter]:
    hit = _CACHE.get("terms")
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    counts: dict[str, Counter] = defaultdict(Counter)
    try:
        from supagent.models import Route

        for terms, used in (db.session.query(Route.terms, Route.used)
                            .filter(Route.signal.in_(CONFIRMED)).order_by(Route.id.desc()).limit(5000)):
            for t in set((terms or "").split()):
                for subject in used or []:
                    counts[t][subject] += 1
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    _CACHE["terms"] = (time.time(), counts)
    return counts


def record(question: str, terms: list[str], shown: list[dict[str, Any]], chosen: list[str],
           user_id: int | None = None, route_id: int | None = None) -> int | None:
    """The route of an answer (the tables shown with their features, the ones chosen); its id. `route_id`: the
    row the router made for this answer, completed."""
    try:
        from supagent.models import Route

        r = db.session.get(Route, route_id) if route_id else None
        if r is None:
            r = Route(question=(question or "")[:2000], user_id=user_id)
            db.session.add(r)
        r.terms, r.shown, r.chosen, r.used = " ".join(terms)[:2000], shown[:40], chosen[:20], []
        db.session.commit()
        return r.id
    except Exception:  # pylint: disable=broad-except   (an answer never fails for its route)
        db.session.rollback()
        log.warning("supagent decider: route not recorded", exc_info=True)
        return None


def used(route_id: int | None, subjects: list[str], message_id: int | None = None) -> None:
    """The tables the answer's queries read, and its message."""
    if not route_id:
        return
    try:
        from supagent.models import Route

        r = db.session.get(Route, route_id)
        if r is not None:
            r.used = sorted(set(subjects))[:20]
            if message_id:
                r.message_id = message_id
            db.session.commit()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()


def confirm(message_id: int, signal: str | None) -> bool:
    """A person confirmed (or not) the answer of this message: its route teaches (or not); None takes it back."""
    import datetime as dt

    try:
        from supagent.models import Route

        r = db.session.query(Route).filter(Route.message_id == message_id).order_by(Route.id.desc()).first()
        if r is None:
            return False
        r.signal, r.signal_at = signal, (dt.datetime.utcnow() if signal else None)
        db.session.commit()
        _CACHE.pop("terms", None)
        return True
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return False


def learn(max_routes: int = 2000) -> dict[str, Any]:
    """Weights from the confirmed routes (pairwise logistic, pulled to the defaults, clipped)."""
    from supagent.models import Meta, Route

    rows = (db.session.query(Route).filter(Route.signal.in_(CONFIRMED)).order_by(Route.id.desc())
            .limit(max_routes).all())
    pairs: list[dict[str, float]] = []
    for r in rows:
        used_set = set(r.used or [])
        shown = r.shown or []
        pos = [s for s in shown if s.get("subject") in used_set]
        neg = sorted((s for s in shown if s.get("subject") not in used_set), key=lambda s: -(s.get("score") or 0))
        for p in pos:
            for n in neg[:NEGATIVES]:
                diff = {k: float((p.get("features") or {}).get(k, 0)) - float((n.get("features") or {}).get(k, 0))
                        for k in DEFAULTS}
                if any(diff.values()):
                    pairs.append(diff)
    w = dict(DEFAULTS)
    if len(rows) >= MIN_ROUTES and pairs:
        for _ in range(EPOCHS):
            for diff in pairs:
                margin = sum(w[k] * diff[k] for k in DEFAULTS)
                g = 1.0 / (1.0 + math.exp(min(50.0, margin)))          # sigmoid(-margin)
                for k in DEFAULTS:
                    w[k] += LEARN_RATE * g * diff[k] + PULL * (DEFAULTS[k] - w[k])
                    lo, hi = _bounds(k)
                    w[k] = min(hi, max(lo, w[k]))
    correct = sum(1 for d in pairs if sum(w[k] * d[k] for k in DEFAULTS) > 0)
    out = {"routes": len(rows), "pairs": len(pairs), "ordered": round(correct / len(pairs), 3) if pairs else None,
           "weights": {k: round(v, 4) for k, v in w.items()}, "learned": len(rows) >= MIN_ROUTES}
    row = db.session.get(Meta, META_KEY)
    if row is None:
        db.session.add(Meta(key=META_KEY, value=json.dumps(out)))
    else:
        row.value = json.dumps(out)
    db.session.commit()
    _CACHE.clear()
    return out
