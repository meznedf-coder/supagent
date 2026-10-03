"""0.9: a measure compared with its usual, group by group (knowledge.groups) and the tool compare_groups on a
table: the measure's SQL, the scope with its business-date labels moved to each earlier day, what stands out and
whether it is concentrated on a few values or spread over all."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest
from test_knowledge import world  # noqa: F401  (the fixture)

from supagent.knowledge import groups as G


def test_the_measure_is_read_and_written_as_sql():
    assert G.Measure.parse("count").select() == "COUNT(*) AS n"
    assert G.Measure.parse("COUNT(*)").kind == "count"
    m = G.Measure.parse('avg("DURATION_S")')
    assert (m.kind, m.fields, m.select()) == ("avg", ("DURATION_S",), 'COUNT(*) AS n, AVG("DURATION_S") AS a')
    r = G.Measure.parse("ratio(DURATION_S, USUAL_S)")
    assert r.select() == 'COUNT(*) AS n, SUM("DURATION_S") AS a, SUM("USUAL_S") AS b'
    assert r.not_null() == '"DURATION_S" IS NOT NULL AND "USUAL_S" IS NOT NULL'
    assert r.value((4, 300.0, 200.0)) == 1.5 and r.value((4, None, 200.0)) is None and r.value((4, 3.0, 0)) is None
    assert G.Measure.parse("count").value((7,)) == 7.0
    for bad in ("median(X)", "avg(A, B)", "ratio(A)", "avg(a; DROP TABLE x)"):
        with pytest.raises(G.GroupsError):
            G.Measure.parse(bad)


def test_the_scope_keeps_its_conditions_and_moves_its_labels():
    cond, labels = G.scope('''"ENV" = 'PROD' AND "APP" IN ('A', 'B') AND "POSITION_LABEL" = 'D-1' ''', "POSITION_LABEL")
    assert labels == ["D-1"]
    assert G.fixed_fields(cond) == {"ENV", "POSITION_LABEL"}
    assert "POSITION_LABEL" in G.render(cond)
    earlier = G.render(cond, "POSITION_LABEL", '''"POSITION_DATE" IN ('20260920')''')
    assert "POSITION_LABEL" not in earlier and '''"POSITION_DATE" IN ('20260920')''' in earlier
    assert '''"APP" IN ('A', 'B')''' in earlier and '''"ENV" = 'PROD\'''' in earlier
    assert "POSITION_LABEL" not in G.render(cond, "POSITION_LABEL", "TRUE")
    cond, labels = G.scope('''WHERE "POSITION_LABEL" IN ('D-1', 'D-2')''', "POSITION_LABEL")
    assert labels == ["D-1", "D-2"]
    assert G.scope("", "POSITION_LABEL") == (None, [])
    for bad in ('"A" = 1; DROP TABLE t', '"A" IN (SELECT x FROM y)', '"A" = 1 ORDER BY 1'):
        with pytest.raises(G.GroupsError):
            G.scope(bad, None)


def _days(n, values):
    return [dict(values) for _ in range(n)]


def test_a_change_concentrated_on_a_few_values_is_said_so():
    usual = {f"srv-{i}": (20, 100.0) for i in range(1, 9)}
    now = dict(usual)
    now["srv-3"], now["srv-4"] = (20, 290.0), (18, 270.0)
    s = G.summarize(now, _days(5, usual), counting=False)
    assert s["concentrated"] is True
    assert [g["value"] for g in s["groups"][:2]] == ["srv-3", "srv-4"]
    assert s["groups"][0]["ratio"] == 2.9 and s["groups"][0]["unusual"] is True
    assert "srv-3 (x2.9 its usual)" in s["reading"] and "6 other value(s) are as usual" in s["reading"]
    text = G.conclusion({"SERVER": s, "APP": G.summarize({"a": (158, 135.0)}, _days(5, {"a": (160, 100.0)}), False)})
    assert text.startswith('The change is concentrated on "SERVER"')


def test_a_change_on_every_value_is_not_specific_to_the_field():
    usual = {"a": (50, 100.0), "b": (40, 200.0), "c": (30, 50.0)}
    now = {"a": (50, 150.0), "b": (40, 310.0), "c": (30, 76.0)}
    s = G.summarize(now, _days(4, usual), counting=False)
    assert s["concentrated"] is False and "not specific to this field" in s["reading"]
    assert "No field localizes the change" in G.conclusion({"APP": s})


def test_counts_a_value_missing_now_and_a_new_one():
    usual = {"NORTH": (200, None), "SOUTH": (150, None), "EAST": (210, None)}
    now = {"SOUTH": (152, None), "EAST": (214, None), "NEW": (40, None)}
    s = G.summarize({k: (n, float(n)) for k, (n, _v) in now.items()},
                    [{k: (n, float(n)) for k, (n, _v) in usual.items()} for _ in range(5)], counting=True)
    north = next(g for g in s["groups"] if g["value"] == "NORTH")
    assert north["now"] == 0.0 and north["usual"] == 200.0 and north["unusual"] is True
    assert s["concentrated"] is True and "NORTH (0 against 200 usually)" in s["reading"]
    assert "not seen on the earlier days: NEW" in s["reading"]


def test_nothing_unusual_says_so_and_noise_is_not_a_change():
    usual_days = [{"a": (20, v), "b": (20, 100.0)} for v in (60.0, 100.0, 140.0, 80.0, 120.0)]
    s = G.summarize({"a": (20, 135.0), "b": (20, 104.0)}, usual_days, counting=False)    # a moves that much every day
    assert s["concentrated"] is False and s["reading"] == "no value stands out (each within its usual)"
    o = G.overall((500, 0.98), [(500, 1.0), (490, 1.02), (505, 0.99)])
    assert o["verdict"] == "as usual" and o["ratio"] == 0.98
    o = G.overall((500, 1.9), [(500, 1.0), (490, 1.02), (505, 0.99)])
    assert o["verdict"] == "above usual"


class _Conn:
    """A DuckDB table behind the tool, with the business-date calendar of an osagg connection."""

    label_column, label_source, label_date_format = "POSITION_LABEL", "POSITION_DATE", "%Y%m%d"
    label_cutoff, label_years, label_tz = dt.time(14, 0), True, None

    def __init__(self, con, now):
        self.con, self.label_now, self.sql = con, now, []

    def cursor(self):
        outer = self

        class Cur:
            def execute(self, sql):
                outer.sql.append(sql)
                done = outer.con.execute(sql)
                self.description = done.description
                self.rows = done.fetchall()

            def fetchall(self):
                return self.rows

        return Cur()


@pytest.fixture()
def runs(world, monkeypatch):
    """Runs of five weekdays and of today (Wednesday 2026-09-23, asked at 05:00): each day's batch computes the
    business day before; today the runs on srv-3 take three times their usual."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from supagent import tools as T

    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "jobs" ("ts" TIMESTAMP, "APP" VARCHAR, "NODE" VARCHAR, "ENV" VARCHAR, "STATUS" VARCHAR, '
                '"DURATION_S" DOUBLE, "USUAL_S" DOUBLE, "POSITION_DATE" VARCHAR, "POSITION_LABEL" VARCHAR)')
    today = dt.date(2026, 9, 23)
    labels = {"20260922": "D-1", "20260921": "D-2", "20260918": "D-3", "20260917": "D-4", "20260916": "D-5",
              "20260915": "W-1"}
    day, n = today, 0
    while n < 6:
        if day.weekday() < 5:
            pos = day - dt.timedelta(days=3 if day.weekday() == 0 else 1)
            for i in range(40):
                node = f"srv-{i % 4 + 1}"
                slow = 3.0 if day == today and node == "srv-3" else 1.0
                status = "DONE" if not (day == today and i >= 28) else "RUNNING"
                con.execute('INSERT INTO "jobs" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                    dt.datetime.combine(day, dt.time(1, i)), "BILLING" if (i // 4) % 2 else "LEDGER", node, "PROD", status,
                    100.0 * slow if status == "DONE" else None, 100.0, f"{pos:%Y%m%d}", labels[f"{pos:%Y%m%d}"]])
            n += 1
        day -= dt.timedelta(days=1)
    conn = _Conn(con, dt.datetime(2026, 9, 23, 5, 0))

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "GROUP_THREADS", 1)                       # (one connection: the DuckDB table)
    monkeypatch.setattr(T, "_group_fields", lambda table, fixed, label: [f for f in ("NODE", "APP", "STATUS") if f not in fixed])
    monkeypatch.setattr(T, "_table_database", lambda table, ref: world["jobs"])
    from superset.extensions import security_manager as sm

    monkeypatch.setattr(sm, "raise_for_access", lambda **kw: None)
    return conn


def test_the_tool_finds_where_a_slowdown_is(runs, app):
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00",
                           measure="ratio(DURATION_S, USUAL_S)", where='''"ENV" = 'PROD' AND "POSITION_LABEL" = 'D-1' ''',
                           group_by=["NODE", "APP"], time_field="ts")
    assert "error" not in r, r
    assert r["total"]["verdict"] == "above usual" and r["total"]["rows_now"] == 28       # the running ones have no duration
    assert r["compared_with"].startswith("the same window on 2026-09-22, 2026-09-21, 2026-09-18, 2026-09-17, 2026-09-16")
    assert r["conclusion"].startswith('The change is concentrated on "NODE": concentrated on srv-3 (x3 its usual)')
    assert "Not specific to: APP" in r["conclusion"]
    assert r["fields"]["NODE"]["groups"][0] == {"value": "srv-3", "rows_now": 7, "now": 3.0, "usual": 1.0,
                                                "rows_usual": 10, "ratio": 3.0, "unusual": True,
                                                "then": {"yesterday": 1.0, "1 week ago": 1.0},
                                                "since": "new on this day (as usual on the days before)"}
    assert r["since_when"] == ["NODE = srv-3: new on this day (as usual on the days before)"]
    # the team's reference days (agent.compare_against): the figure on each, the day each is
    assert r["reference_days"] == {"yesterday": "2026-09-22", "1 week ago": "2026-09-16", "4 weeks ago": "no data that day"}
    assert r["total"]["then"] == {"yesterday": 1.0, "1 week ago": 1.0}
    assert r["against_reference_days"] == ["sum of DURATION_S / sum of USUAL_S, NODE = srv-3: 3 now, 1 yesterday, 1 1 week ago"]
    # each earlier day was read with its own business date (the label D-1 is today's only)
    earlier = [q for q in runs.sql if "2026-09-22 00:00:00" in q]
    assert earlier and all("POSITION_LABEL" not in q and "'20260921'" in q for q in earlier)
    assert "the business date (D-1) was read for each earlier day as that day's own" in r["note"]
    # the same scope with the business date written as a date: read as its label, and moved the same way
    with app.app_context(), acting_as("admin"):
        again = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00",
                               measure="ratio(DURATION_S, USUAL_S)", group_by=["NODE", "APP"], time_field="ts",
                               where='''"ENV" = 'PROD' AND "POSITION_DATE" = '20260922\' ''')
    assert again["total"] == r["total"] and again["conclusion"] == r["conclusion"]


def test_the_tool_counts_against_the_same_clock_time_and_says_its_errors(runs, app):
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00", measure="count",
                           where='''"STATUS" = 'DONE' AND "POSITION_LABEL" = 'D-1' ''', group_by=["APP"], time_field="ts")
        assert r["total"]["now"] == 28.0 and r["total"]["usual"] == 40.0 and r["total"]["verdict"] == "below usual"
        assert "note" not in r or "ends now" not in r["note"]            # a window of the past (the real clock)
        bad = compare_groups(table="jobs", start="2026-09-23 05:00", end="2026-09-23 00:00", time_field="ts",
                             group_by=["APP"])
        assert bad == {"error": "end must be after start"}
        bad = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00", time_field="ts",
                             group_by=["APP"], where='"APP" IN (SELECT 1)')
        assert "no subquery" in bad["error"]
        none = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00", time_field="ts",
                              group_by=["APP"], where='''"ENV" = 'NOWHERE\'''')
        assert none["error"].startswith("no row matches this scope, in the window asked or in the 31 windows before it")


def test_a_window_that_ends_now_says_that_the_day_is_not_over(runs, app):
    """Today's rows are as they are now (some still running), the earlier days as they ended: the tool says so
    when the window reaches now (the agent's now, when an admin pinned it)."""
    from supagent import settings
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    settings.set_value("agent.now", "2026-09-23 05:20")
    try:
        with app.app_context(), acting_as("admin"):
            r = compare_groups(table="jobs", start="2026-09-23 00:00", end="2026-09-23 05:00", measure="count",
                               where='''"POSITION_LABEL" = 'D-1' ''', group_by=["STATUS"], time_field="ts")
            past = compare_groups(table="jobs", start="2026-09-22 00:00", end="2026-09-22 05:00", measure="count",
                                  group_by=["STATUS"], time_field="ts")
    finally:
        settings.set_value("agent.now", None)
    assert "not seen on the earlier days: RUNNING" in r["fields"]["STATUS"]["reading"]
    assert "this window ends now" in r["note"] and "the business date (D-1) was read" in r["note"]
    assert "time_field = the field of the end time" in r["note"]
    assert "ends now" not in past.get("note", "")
