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
    zone = types.SimpleNamespace(utc_ms=lambda d: int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000))
    week = 7 * 86_400_000

    def __init__(self) -> None:
        self.calls = []
        self.client = types.SimpleNamespace(query_range=self.query_range)

    def query_range(self, expr, start, end, step):
        k = round((_Conn.start - start) / self.week)
        self.calls.append(k)
        if k == 0:
            return [_Series({"node": "srv-1"}, 95.0), _Series({"node": "srv-2"}, 41.0)]
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
        out = tools.compare_to_usual("avg by (node) (cpu)", "2026-09-24 02:00", "2026-09-24 06:00")
    assert sorted(conn.calls) == [0, 1, 2, 3, 4] and out["unusual"] == 1 and out["series_count"] == 2
    first, second = out["series"]
    assert first["labels"] == {"node": "srv-1"} and first["verdict"] == "high" and first["median"] == 42.5
    assert second["verdict"] == "normal" and not math.isnan(second["now"])
    with acting_as("admin"):
        assert "at most 7 days" in tools.compare_to_usual("cpu", "2026-09-01 00:00", "2026-09-24 00:00")["error"]


def test_compare_to_usual_is_offered_when_a_question_asks_for_it():
    from supagent.agent import TOOLS_OF, intents

    assert "usual" in intents("Is the CPU of the grid unusual today?")
    assert "usual" in intents("La mémoire est-elle anormale ce matin ?")
    assert "usual" in intents("was the latency higher than usual last night")
    assert intents("CPU of the grid today") == set()
    assert "compare_to_usual" in TOOLS_OF["usual"] and "compare_to_usual" in TOOLS_OF["investigation"]
