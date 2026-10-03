"""compare_to_usual: is a metric unusual for this time (the same window on earlier weeks, median
and median absolute deviation, per series); offered only when a question asks for it."""

from __future__ import annotations

import datetime as dt
import math
import types

import pytest

from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.mark.parametrize("now, before, verdict", [
    (30.0, [10.0, 11.0, 9.0, 10.0], "high"),
    (10.5, [10.0, 11.0, 9.0, 10.0], "normal"),
    (2.0, [10.0, 11.0, 9.0, 10.0], "low"),
    (10.2, [10.0, 10.0, 10.0, 10.0], "normal"),          # a flat series barely moving is not unusual
    (30.0, [10.0, 11.0], "unknown"),                     # not enough history
    (None, [10.0, 11.0, 9.0], "unknown"),
])
def test_the_verdict_of_one_series(now, before, verdict):
    from supagent.tools import _usual

    assert _usual(now, before)["verdict"] == verdict


class _Series:
    def __init__(self, labels: dict, value: float) -> None:
        self.labels = {"__name__": "cpu", **labels}
        self.points = [(1, value), (2, value), (3, float("nan"))]


class _Conn:
    zone = types.SimpleNamespace(utc_ms=lambda d: int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                                 local=lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).replace(tzinfo=None))
    week = 7 * 86_400_000

    def __init__(self) -> None:
        self.calls = []
        self.client = types.SimpleNamespace(query_range=self.query_range)

    def query_range(self, expr, start, end, step):
        k = round((_Conn.start - start) / self.week)
        self.calls.append(k)
        if k == 0:
            return [_Series({"node": "srv-1"}, 95.0), _Series({"node": "srv-2"}, 41.0)]
        if k > 12:                                  # the metrics are kept twelve weeks
            return []
        return [_Series({"node": "srv-1"}, 40.0 + k), _Series({"node": "srv-2"}, 40.0 + k)]

    def close(self):
        pass


_Conn.start = _Conn.zone.utc_ms(dt.datetime(2026, 9, 24, 2, 0))


def test_compare_to_usual_gives_a_verdict_per_series(world, monkeypatch):
    from supagent import tools
    from supagent.security import acting_as

    conn = _Conn()
    monkeypatch.setattr(tools, "_metrics_database", lambda ref: world["metrics"])
    monkeypatch.setattr(tools, "_promagg_connection", lambda database: conn)
    with acting_as("admin"):
        out = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00", against=[])
    assert sorted(conn.calls) == [0, 1, 2, 3, 4] and out["unusual"] == 1 and out["series_count"] == 2
    first, second = out["series"]
    assert first["labels"] == {"node": "srv-1"} and first["verdict"] == "high" and first["median"] == 42.5
    assert second["verdict"] == "normal" and not math.isnan(second["now"])
    assert "reference_days" not in out and "then" not in first
    # 0.9: the reference days (the team's own when none is given: agent.compare_against; a question may name others)
    conn.calls.clear()
    with acting_as("admin"):
        out = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00")
        named = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00",
                                       against=["2 weeks ago", "2026-09-03"])
        bad = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00", against=["someday"])
    assert out["reference_days"] == {"yesterday": "2026-09-23", "1 week ago": "2026-09-17", "4 weeks ago": "2026-08-27"}
    assert out["series"][0]["then"] == {"yesterday": 95.0, "1 week ago": 41.0, "4 weeks ago": 44.0}       # (the fake: per week)
    assert named["reference_days"] == {"2 weeks ago": "2026-09-10", "2026-09-03": "2026-09-03"}
    assert named["series"][0]["then"] == {"2 weeks ago": 42.0, "2026-09-03": 43.0}
    assert bad["error"].startswith("against: not a reference day: 'someday'")
    # a reference day the metrics do not reach: said, with no figure of that day and no verdict from it
    with acting_as("admin"):
        far = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00",
                                     against=["1 week ago", "6 months ago"])
        none = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00", against=["1 year ago"])
    assert far["reference_days"] == {"1 week ago": "2026-09-17", "6 months ago": "no data that day"}
    assert far["series"][0]["then"] == {"1 week ago": 41.0} and far["series"][0]["verdict"] == "high"
    assert none["reference_days"] == {"1 year ago": "no data that day"} and all("then" not in x for x in none["series"])
    assert [x["verdict"] for x in none["series"]] == [x["verdict"] for x in far["series"]]
    with acting_as("admin"):
        assert "at most 7 days" in tools.compare_to_usual("cpu", "2026-09-01 00:00", "2026-09-24 00:00")["error"]


def test_compare_to_usual_is_offered_when_a_question_asks_for_it():
    from supagent.agent import TOOLS_OF, intents

    assert "usual" in intents("Is the CPU of the grid unusual today?")
    assert "usual" in intents("La mémoire est-elle anormale ce matin ?")
    assert "usual" in intents("was the latency higher than usual last night")
    assert intents("CPU of the grid today") == set()
    assert "compare_to_usual" in TOOLS_OF["usual"] and "compare_to_usual" in TOOLS_OF["investigation"]


def test_a_raw_counter_is_not_compared_and_a_far_normal_says_why(world, monkeypatch):
    """0.9: a counter read as it is only grows (every week differs by chance): refused, with what to compare; a
    series far from its median yet within what its earlier weeks differ by says so."""
    from supagent import tools
    from supagent.security import acting_as

    assert tools.raw_counter("avg(jobs_done_total{app='a'})") == "jobs_done_total"
    assert tools.raw_counter("sum(rate(jobs_done_total[5m]))") is None
    assert tools.raw_counter("histogram_quantile(0.95, sum by (le) (rate(req_seconds_bucket[5m])))") is None
    assert tools.raw_counter("avg(node_load1)") is None
    monkeypatch.setattr(tools, "_metrics_database", lambda ref: world["metrics"])
    monkeypatch.setattr(tools, "_promagg_connection", lambda database: _Conn())
    with acting_as("admin"):
        out = tools.compare_to_usual("avg(req_seconds_count{app='a'})", "2026-09-24 02:00", "2026-09-24 06:00")
    assert "is a counter" in out["error"] and "sum(rate(req_seconds_count[5m]))" in out["error"]
    far = tools._usual(199.0, [100.0, 40.0, 210.0, 160.0])
    assert far["verdict"] == "normal" and "the earlier weeks vary as much (40 to 210)" in far["note"]
    assert "note" not in tools._usual(10.5, [10.0, 11.0, 9.0, 10.0])


def test_now_in_a_tool_time_follows_the_pinned_now(ctx, monkeypatch):
    from supagent import settings, tools

    conn = _Conn()
    settings.set_value("agent.now", "2026-09-24 23:30")
    try:
        assert tools._now_ms(conn) == _Conn.zone.utc_ms(dt.datetime(2026, 9, 24, 23, 30))
    finally:
        settings.set_value("agent.now", None)
    assert abs(tools._now_ms(conn) - dt.datetime.now(dt.timezone.utc).timestamp() * 1000) < 5000
