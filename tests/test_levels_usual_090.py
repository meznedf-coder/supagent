"""0.9: promql_query gives each series what it was over the same window of the previous days: a pool that is full
every night, a queue as long as every night are their usual, not a change; the series that differ are named. And
the step after a tool call the server could not read gets a quarter of the tokens."""

from __future__ import annotations

import datetime as dt
import types

from test_knowledge import world  # noqa: F401  (the fixture)

DAY = 86_400_000
T0 = int(dt.datetime(2026, 9, 24, 1, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
T1 = T0 + 3 * 3_600_000


class _Series:
    def __init__(self, pool: str, values: list[float], start: int, step: int) -> None:
        self.labels = {"__name__": "slots_running", "pool": pool}
        self.points = [(start + i * step, v) for i, v in enumerate(values)]


class _Conn:
    zone = types.SimpleNamespace(utc_ms=lambda d: int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                                 local=lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).replace(tzinfo=None))

    def __init__(self, days_with_data=7) -> None:
        self.calls, self.days_with_data = [], days_with_data
        self.client = types.SimpleNamespace(query_range=self.query_range, query=self.query)

    def _series(self, k: int, start: int, n: int, step: int) -> list[_Series]:
        if k > self.days_with_data:
            return []
        out = [_Series("pool-a", [40.0] * n, start, step),                              # full every night
               _Series("pool-b", [30.0 if k == 0 else 10.0] * n, start, step)]           # three times its usual tonight
        if k == 0:
            out.append(_Series("pool-new", [5.0] * n, start, step))                     # not there before
        return out

    def query_range(self, expr, start, end, step):
        k = round((T0 - start) / DAY)
        self.calls.append(("range", k))
        return self._series(k, start, (end - start) // step + 1, step)

    def query(self, expr, at):
        k = round((T1 - at) / DAY)
        self.calls.append(("instant", k))
        return self._series(k, at, 1, 1)

    def list_tables(self):
        return ["slots_running"]

    def close(self):
        pass


def _tools(world, monkeypatch, conn):  # noqa: F811
    from supagent import tools

    monkeypatch.setattr(tools, "_metrics_database", lambda ref: world["metrics"])
    monkeypatch.setattr(tools, "_promagg_connection", lambda database: conn)
    return tools


def test_a_level_comes_with_its_usual(world, monkeypatch):  # noqa: F811
    from supagent.security import acting_as

    conn = _Conn()
    tools = _tools(world, monkeypatch, conn)
    with acting_as("admin"):
        out = tools.promql_query("slots_running", start="2026-09-24 01:00", end="2026-09-24 04:00")
    by = {s["labels"]["pool"]: s for s in out["series"]}
    assert (by["pool-a"]["usual_avg"], by["pool-a"]["usual_max"], by["pool-a"]["against_usual"]) == (40.0, 40.0, "as on the previous days")
    assert by["pool-b"]["against_usual"] == "x3.00 its usual: above usual" and by["pool-b"]["usual_avg"] == 10.0
    assert by["pool-new"]["against_usual"] == "no usual: not there on the previous days"
    assert out["against_usual"] == ("1 of 2 series differ from the same window of the 7 previous days (the median of their "
                                    "averages: usual_avg, usual_max): pool=pool-b; the others are as usual")
    assert sorted(k for kind, k in conn.calls if kind == "range") == [0, 1, 2, 3, 4, 5, 6, 7]
    # one instant: the same instant of the previous days
    with acting_as("admin"):
        now = tools.promql_query('slots_running{pool="pool-a"}', end="2026-09-24 04:00")
    assert now["step"] == "instant" and sorted(k for kind, k in conn.calls if kind == "instant") == [0, 1, 2, 3, 4, 5, 6, 7]


def test_every_series_as_usual_is_said_and_too_little_history_is_not_judged(world, monkeypatch):  # noqa: F811
    from supagent import tools as T
    from supagent.security import acting_as

    conn = _Conn()
    conn._series = lambda k, start, n, step: [_Series("pool-a", [40.0] * n, start, step),
                                               _Series("pool-c", [28.0] * n, start, step)]
    tools = _tools(world, monkeypatch, conn)
    with acting_as("admin"):
        out = tools.promql_query("slots_running", start="2026-09-24 01:00", end="2026-09-24 04:00")
    assert out["against_usual"].startswith("every series is as on the same window of the 7 previous days")
    assert out["against_usual"].endswith("these levels are their usual, not a change")
    few = _Conn(days_with_data=2)                               # two earlier days: no usual to speak of
    tools = _tools(world, monkeypatch, few)
    with acting_as("admin"):
        out = tools.promql_query("slots_running", start="2026-09-24 01:00", end="2026-09-24 04:00")
    assert "against_usual" not in out and all("usual_avg" not in s for s in out["series"])
    # a counter read as it is, a long range, many series: asked as before, nothing more
    many = _Conn()
    many._series = lambda k, start, n, step: [_Series(f"pool-{i}", [1.0] * n, start, step) for i in range(T.USUAL_SERIES + 1)]
    tools = _tools(world, monkeypatch, many)
    with acting_as("admin"):
        out = tools.promql_query("slots_running", start="2026-09-24 01:00", end="2026-09-24 04:00")
        tools.promql_query("jobs_total", start="2026-09-24 01:00", end="2026-09-24 04:00")
    assert "against_usual" not in out and [k for _kind, k in many.calls] == [0, 0]


def test_the_step_after_an_unreadable_tool_call_is_short(app):
    from test_prompt import _agent

    from supagent import settings

    a = _agent()
    a.llm = types.SimpleNamespace(cfg=types.SimpleNamespace(thinking=False))
    with app.app_context():
        assert a._answer_tokens() == 8192 and a._answer_tokens(short=True) == 2048
        settings.set_value("llm.max_answer_tokens", 1000)
        try:
            assert a._answer_tokens() == 1000 and a._answer_tokens(short=True) == 1000    # never more than the limit
            settings.set_value("llm.max_answer_tokens", 0)
            assert a._answer_tokens() is None and a._answer_tokens(short=True) == 2048
            a.llm.cfg.thinking = True                                                     # the reasoning counts too
            settings.set_value("llm.max_answer_tokens", 8192)
            assert a._answer_tokens() == 32768 and a._answer_tokens(short=True) == 8192
        finally:
            settings.set_value("llm.max_answer_tokens", None)


def test_the_investigation_instructions_follow_the_stages_and_the_usual():
    from supagent.agent import SECTIONS

    text = " ".join(SECTIONS["investigation"].split())
    for phrase in ("compare_groups on the question's table with its scope",
                   "the team's reference days (yesterday, a week ago, weeks ago; against = [...]",
                   "Read it in the order of the stages: the first that is clearly off",
                   "Late before it could start: it waited for what it depends on",
                   "Ready but waiting: it waits for a capacity (a slot, a token, a queue)",
                   "far from an older reference day is a change older than those days",
                   'the records compare_groups gives under "related"',
                   "A level is a finding only against its usual",
                   "what is not affected must be free of it",
                   "A day the team documented, with the effect it documented, is the explanation"):
        assert phrase in text, phrase
    assert len(SECTIONS["investigation"]) < 4000
    for word in ("batch", " job", " feed ", "feeds they"):         # worded for any table, not for one kind of system
        assert word not in text, word
