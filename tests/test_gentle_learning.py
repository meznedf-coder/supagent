"""Gentle learning: index families, the rolling cycle, the throttle and its breaker, the
learning of metrics with few requests."""

from __future__ import annotations

import datetime as dt
import time as _real_time

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)

REAL_SLEEP = _real_time.sleep


def test_dated_and_rolled_over_indices_are_one_family(app):
    from supagent.knowledge.learn_indices import family_of, group_indices

    assert family_of("logs-otel-2026.09.27") == "logs-otel-*"
    assert family_of("app-logs-2026.09") == "app-logs-*"
    assert family_of("traces-000123") == "traces-*"
    assert family_of("jobs_20260927") == "jobs-*"
    assert family_of("batch-jobs") is None and family_of(".ds-logs-2026.09.27-000001") is None
    objects, families = group_indices(["logs-otel-2026.09.26", "logs-otel-2026.09.27", "logs-otel-2026.09.25",
                                       "jobs", "single-2026.09.27"])
    assert objects == {"jobs": "jobs", "single-2026.09.27": "single-2026.09.27",
                       "logs-otel-*": "logs-otel-2026.09.27"}                 # the latest member is read
    assert families == {"logs-otel-*": ["logs-otel-2026.09.25", "logs-otel-2026.09.26", "logs-otel-2026.09.27"]}


def test_the_rolling_cycle_spreads_profiles_over_the_week(app):
    from supagent.knowledge.learn_metrics import due

    today = dt.date(2026, 9, 28)
    assert due("m", None, 7, today)                                          # never profiled
    assert not due("m", {"profiled_on": "2026-09-28"}, 7, today)             # today already
    names = [f"metric_{i}" for i in range(700)]
    per_day = [sum(due(n, {"profiled_on": "2026-09-25"}, 7, today + dt.timedelta(days=d)) for n in names)
               for d in range(3)]
    assert all(60 < n < 145 for n in per_day), per_day                      # about 1/7 each day
    assert due("m", {"profiled_on": "2026-09-01"}, 7, today)                 # overdue (twice the cycle)


def test_the_throttle_limits_and_the_breaker_stops(monkeypatch):
    import time as _time

    from supagent.knowledge import throttle as T

    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    t = T.Throttle(per_minute=6000, stop_after=3, name="mimir")
    assert t.call(lambda: 42) == 42

    class Busy(Exception):
        status_code = 429

    def busy():
        raise Busy("429 Too Many Requests")

    for _ in range(2):
        with pytest.raises(Busy):
            t.call(busy)
    with pytest.raises(T.SourceStopped):
        t.call(busy)                                      # the third failure in a row opens the breaker
    with pytest.raises(T.SourceStopped):
        t.call(lambda: 1)                                 # and it stays open for this run
    assert t.stats()["requests"] == 4 and t.stats()["errors"] == 3

    class Bad(Exception):
        pass

    t2 = T.Throttle(per_minute=6000, stop_after=2)
    for _ in range(3):                                    # a bad query is not an overload
        with pytest.raises(Bad):
            t2.call(lambda: (_ for _ in ()).throw(Bad("syntax error")))
    assert t2.call(lambda: 5) == 5
    slow = T.Throttle(per_minute=600, stop_after=3)       # 0.1 s apart
    monkeypatch.setattr(T.time, "sleep", REAL_SLEEP)
    t0 = _time.monotonic()
    for _ in range(4):
        slow.call(lambda: None)
    assert _time.monotonic() - t0 >= 0.28


def test_metrics_are_learned_with_few_requests(world, monkeypatch):
    """New metric: count, one statistics query, one series sample, the history once."""
    import time as _time

    from superset.extensions import db

    from supagent.knowledge import learn_metrics as lm
    from supagent.models import KObject, Run

    calls = []

    class S:
        def __init__(self, labels=None, v=5.0):
            self.labels, self.points = labels or {}, [[0, v]]

    class Client:
        def query(self, expr, t, timeout=None):
            calls.append(("query", expr[:40]))
            if expr.startswith("count("):
                return [S(v=3)]
            return [S({"stat": "min"}, 1), S({"stat": "max"}, 9), S({"stat": "avg"}, 5)]

        def series(self, sels, a, b, limit=None):
            calls.append(("series", limit))
            if limit == 1:
                return [{"__name__": "x"}]
            return [{"__name__": "node_temp", "node": f"srv-{i}", "job": "node"} for i in range(3)]

        def metadata(self):
            calls.append(("metadata", None))
            return {"node_temp": [{"type": "gauge", "unit": "celsius", "help": "Temperature"}]}

    class Conn:
        client = Client()

        def list_tables(self):
            return ["node_temp"]

        def now_ms(self):
            return 1_790_000_000_000

        def close(self):
            pass

    monkeypatch.setattr("supagent.tools._promagg_connection", lambda database: Conn())
    monkeypatch.setattr(lm.Throttle, "wait", lambda self: None)
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    out = lm.learn_metrics(run, world["s_prom"], world["metrics"], deadline=_time.time() + 60)
    assert out["profiled"] == 1 and out["errors"] == 0 if "errors" in out else out["profiled"] == 1
    obj = db.session.query(KObject).filter_by(kind="metric", name="node_temp").one()
    assert obj.metric_type == "gauge" and obj.backend_help == "Temperature"
    assert (obj.stats["series"], obj.stats["min"], obj.stats["max"]) == (3, 1.0, 9.0)
    label = db.session.query(KObject).filter_by(kind="label", parent="node_temp", name="node").one()
    assert label.stats["values"] == ["srv-0", "srv-1", "srv-2"]
    queries = [c for c in calls if c[0] == "query"]
    assert len(queries) == 2, queries                      # the count, then all statistics in one query
    calls.clear()
    out2 = lm.learn_metrics(run, world["s_prom"], world["metrics"], deadline=_time.time() + 60)
    assert out2["profiled"] == 0 and out2["not_due"] >= 1  # profiled today: only the cheap listing
    assert [c[0] for c in calls] == ["metadata"]


def test_a_run_stopped_by_a_restart_is_tried_again(world, monkeypatch):
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import learner
    from supagent.models import Run

    now = dt.datetime.now()
    if now.hour == 0 and now.minute < 15:
        pytest.skip("too close to midnight for 'today'")
    real_get = settings.get
    fixed = {"learn.hour": 0, "learn.days": [], "learn.max_minutes": 1, "learn.enabled": True}
    monkeypatch.setattr(settings, "get", lambda k: fixed[k] if k in fixed else real_get(k))
    db.session.query(Run).delete()
    db.session.commit()
    assert learner.due_today()
    utc = dt.datetime.utcnow()
    run = Run(kind="learn", reason="schedule", status="running", started_at=utc - dt.timedelta(minutes=2))
    db.session.add(run)
    db.session.commit()
    assert not learner.due_today()                                   # still running
    run.started_at = utc - dt.timedelta(minutes=10)                  # older than 2 x max_minutes + 5: stopped
    db.session.commit()
    assert learner.due_today()
    db.session.refresh(run)
    assert run.status == "interrupted" and "restart" in run.error
    for _i in range(2):
        db.session.add(Run(kind="learn", reason="schedule", status="error", started_at=utc - dt.timedelta(minutes=9)))
    db.session.commit()
    assert not learner.due_today()                                   # three tries today: tomorrow then
    db.session.query(Run).delete()
    db.session.add(Run(kind="learn", reason="cli", status="done", started_at=utc - dt.timedelta(minutes=9)))
    db.session.commit()
    assert not learner.due_today()                                   # learned today already (by hand)


def test_a_metric_whose_data_stopped_costs_two_lookups(world, monkeypatch):
    """Data that stopped (a decommissioned exporter, a lab copy): the end found last time is
    checked (nothing newer? still there?) instead of bisecting the series index again."""
    from supagent.knowledge import learn_metrics as lm

    now, prev = 1_790_000_000_000, 1_789_700_000_000
    lookups = []

    class S:
        def __init__(self, labels=None, v=5.0):
            self.labels, self.points = labels or {}, [[0, v]]

    class Client:
        def query(self, expr, t, timeout=None):
            if expr.startswith("count({"):
                return []                                            # nothing live now
            if expr.startswith("count(last_over_time"):
                return [S(v=4)]
            return [S({"stat": "avg"}, 5)]

        def series(self, sels, a, b, limit=None):
            lookups.append((a, b))
            return [{"__name__": "x"}] if a <= prev else []           # no sample after prev

    class Conn:
        client = Client()
        zone = None

        def now_ms(self):
            return now

    monkeypatch.setattr(lm, "_iso", lambda ms, conn: str(ms))
    old = {"data_to_ms": prev, "data_from": "x", "history_checked_on": dt.date.today().isoformat()}
    stats = lm.profile_metric(Conn(), "node_temp", None, "gauge", 24, old)
    assert stats["data_to_ms"] == prev and stats["series"] == 4 and stats["avg"] == 5.0
    assert len(lookups) == 2                                          # no bisection
    lookups.clear()
    stats = lm.profile_metric(Conn(), "node_temp", None, "gauge", 24, {})
    assert len(lookups) > 8 and stats["data_to_ms"] <= prev + 3_600_000   # first time: bisected (and history)
