"""0.9: compare_groups with every measure of a table in one call (measure "all"): the rows, each duration against
its usual, each numeric field, and for a business date the stages of its rows (the table's time fields in their
order): what had reached each by that time of day, how long after the one before, who was still waiting; the
rules that keep noise out (a count of 13 against 8, an average over two rows, 10 s instead of 60, a status, a
field the runs not started do not have yet); a new version read against the one it replaces; the field that
holds the change with the fewest values first; the records of the related tables about what stands out."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager

import pytest
from test_groups import _Conn
from test_knowledge import world  # noqa: F401  (the fixture)

from supagent.knowledge import groups as G


def _days(n, values):
    return [dict(values) for _ in range(n)]


def _has(x, key):
    """A key anywhere in a result."""
    if isinstance(x, dict):
        return key in x or any(_has(v, key) for v in x.values())
    return isinstance(x, list) and any(_has(v, key) for v in x)


def test_one_query_reads_every_measure():
    plan = G.Plan([G.Measure("count"), G.Measure("ratio", ("D", "U")), G.Measure("reached", ("ENDED",)),
                   G.Measure("avg", ("TRADES",)), G.Measure("lapse", ("READY", "STARTED")),
                   G.Measure("age", ("STARTED", "ENDED"))], as_of={1: "ENDED"})
    sql = plan.select("TIMESTAMP '2026-09-23 05:00:00'")
    assert sql.startswith('COUNT(*) AS n, SUM("D") FILTER (WHERE "U" IS NOT NULL AND "ENDED" < TIMESTAMP \'2026-09-23 05:00:00\') AS a1')
    assert 'COUNT(*) FILTER (WHERE "ENDED" < TIMESTAMP \'2026-09-23 05:00:00\') AS a2' in sql
    assert 'AVG("TRADES") AS a3, COUNT("TRADES") AS c3' in sql
    assert 'AVG("READY") FILTER (WHERE "READY" IS NOT NULL AND "STARTED" IS NOT NULL AND "STARTED" < TIMESTAMP' in sql
    assert 'AVG("STARTED") FILTER (WHERE "STARTED" < TIMESTAMP \'2026-09-23 05:00:00\' AND ("ENDED" IS NULL OR "ENDED" >= TIMESTAMP' in sql
    end = dt.datetime(2026, 9, 23, 5, 0)
    row = (40, 9000.0, 6000.0, 10, 28, 500.0, 40, dt.datetime(2026, 9, 23, 1, 0), dt.datetime(2026, 9, 23, 1, 2), 30,
           dt.datetime(2026, 9, 23, 4, 0), 12)
    assert plan.values(row, end) == [(40, 40.0), (10, 1.5), (28, 28.0), (40, 500.0), (30, 120.0), (12, 3600.0)]
    plain = G.Plan([G.Measure("ratio", ("D", "U")), G.Measure("reached", ("ENDED",))], filter_clause=False).select("X")
    assert 'SUM(CASE WHEN "U" IS NOT NULL THEN "D" END) AS a0' in plain and 'SUM(CASE WHEN "ENDED" < X THEN 1 ELSE 0 END) AS a1' in plain
    assert G.Measure("reached", ("ENDED",)).counting and G.Measure("between", ("A", "B")).counting and not G.Measure("lapse", ("A", "B")).counting
    assert G.seconds_between(1_758_000_000_000, 1_758_000_060_000) == 60.0 and G.seconds_between(None, end) is None


def test_noise_is_not_a_finding():
    # a count of 13 against 8 is what counts differ by from one day to the next; 0 against 20 is not
    earlier = [{"a": (n, float(n)), "b": (20, 20.0), "c": (200, 200.0)} for n in (8, 6, 9, 11, 7, 8)]
    s = G.summarize({"a": (13, 13.0), "b": (0, 0.0), "c": (203, 203.0)}, earlier, counting=True)
    flagged = {g["value"] for g in s["groups"] if g.get("unusual")}
    assert flagged == {"b"} and "b (0 against 20 usually)" in s["reading"]
    # ... a count that is the same every day (five a day, each day) has moved when it moves
    s = G.summarize({"a": (13, 13.0), "d": (0, 0.0), "c": (203, 203.0)},
                    _days(6, {"a": (8, 8.0), "d": (5, 5.0), "c": (200, 200.0)}), counting=True)
    assert {g["value"] for g in s["groups"] if g.get("unusual")} == {"a", "d"}
    # values off the other way are among the ones a change is not on
    both = G.summarize({"new": (30, 30.0), "x": (8, 8.0), "y": (8, 8.0)}, _days(6, {"x": (16, 16.0), "y": (16, 16.0)}), True)
    assert both["concentrated"] and both["reading"].startswith(
        "concentrated on new (new: 30, not seen on the earlier days) above usual, while the 2 other value(s) are not")
    # an average over two rows is not judged, nor 10 s instead of 60 where the usual of all is an hour
    usual = {"x": (20, 60.0), "y": (20, 3600.0), "z": (20, 3600.0)}
    now = {"x": (20, 10.0), "y": (2, 9000.0), "z": (20, 3700.0)}
    s = G.summarize(now, _days(6, usual), counting=False, scale=3600.0)
    assert not s["concentrated"] and s["reading"] == "no value stands out (each within its usual)"
    assert G.summarize(now, _days(6, usual), counting=False)["concentrated"]              # (without the scale: x0.17)
    # a wait shorter than usual is no finding; twice as long is
    waits = G.summarize({"p": (10, 300.0), "q": (10, 2500.0), "r": (10, 1000.0)},
                        _days(6, {"p": (10, 1000.0), "q": (10, 1000.0), "r": (10, 1000.0)}), False, high=2.0, above_only=True)
    assert [g["value"] for g in waits["groups"] if g.get("unusual")] == ["q"]
    # the rows without a value of the field are no place to look
    s = G.summarize({G.NONE: (100, 100.0), "E1": (4, 4.0)}, _days(6, {G.NONE: (300, 300.0), "E1": (4, 4.0)}), True)
    assert not s["concentrated"] and not s["flagged"]


def test_a_field_not_filled_yet_and_a_status_do_not_localize():
    # the server of the runs not started: counts by server are not comparable, nor an average every row has
    usual = {"srv-1": (20, 20.0), "srv-2": (20, 20.0)}
    now = {"srv-1": (8, 8.0), "srv-2": (9, 9.0), G.NONE: (23, 23.0)}
    s = G.summarize(now, _days(6, usual), counting=True)
    assert s["reading"].startswith("not comparable yet: 23 rows have no value of this field so far") and not s["concentrated"]
    counts_now, counts_days = {k: v[0] for k, v in now.items()}, _days(6, {"srv-1": 20, "srv-2": 20})
    assert G.unfilled(counts_now, counts_days) and not G.unfilled({"srv-1": 20, "srv-2": 20}, counts_days)
    assert "not comparable yet" in G.summarize(now, _days(6, usual), False, open_field=True)["reading"]
    # a status: every value's rows have all ended or none has, and the ones not ended are values the earlier days
    # (read as they ended) never had
    ended_days = _days(6, {"DONE": 39, "FAILED": 1})
    assert G.follows_the_stage({"DONE": (25, 25), "FAILED": (1, 1), "RUNNING": (8, 0), "QUEUED": (6, 0)}, ended_days)
    # ... while a value of the earlier days whose rows are all stuck is a finding
    assert not G.follows_the_stage({"RATES": (20, 0), "FX": (20, 20)}, _days(6, {"RATES": 20, "FX": 20}))
    assert not G.follows_the_stage({"LEDGER": (20, 12), "BILLING": (20, 3)}, _days(6, {"LEDGER": 20, "BILLING": 20}))
    assert G.state_like({"DONE": 25, "RUNNING": 9, "QUEUED": 6}, _days(6, {"DONE": 40}))


def test_rows_that_appear_where_there_are_none_usually():
    # nobody waits for a slot at that hour, on any pool; today 52 rows do, on one pool
    usual = {"pool-a": (0, 0.0), "pool-b": (0, 0.0), "pool-c": (0, 0.0)}
    s = G.summarize({"pool-a": (52, 52.0), "pool-b": (0, 0.0), "pool-c": (0, 0.0)}, _days(8, usual), True, change=52.0)
    assert s["concentrated"] and s["reading"] == "concentrated on pool-a (52 against 0 usually) above usual, while the 2 other value(s) are as usual"
    assert s["coverage"] == 1.0 and s["strength"] == G.STRONG
    o = G.overall((52, 52.0), [(0, 0.0)] * 8)
    assert o["verdict"] == "above usual" and "ratio" not in o
    # ... and three rows are not a finding
    assert not G.summarize({"pool-a": (3, 3.0), "pool-b": (0, 0.0)}, _days(8, {"pool-a": (0, 0.0), "pool-b": (0, 0.0)}), True)["concentrated"]


def test_a_new_version_is_read_against_the_one_it_replaces():
    days = _days(8, {"1.9": 20, "7.1": 20})
    assert G.replaced({"2.0": 20, "7.1": 20}, days) == {"2.0": "1.9"}
    assert G.replaced({"2.0": 9, "7.1": 20, G.NONE: 11}, days) == {"2.0": "1.9"}      # the runs not started have none yet
    assert G.replaced({"2.0": 3, "7.1": 20}, days) == {"2.0": "1.9"}                   # one came, one went
    assert G.replaced({"2.0": 3, "8.0": 20}, _days(8, {"1.9": 20, "7.1": 20})) == {"8.0": "1.9"}   # several: by their rows
    assert G.replaced({"1.9": 20, "7.1": 20}, days) == {}
    recent = [{"2.0": 20, "7.1": 20}] + _days(7, {"1.9": 20, "7.1": 20})               # released two days ago
    assert G.replaced({"2.0": 20, "7.1": 20}, recent) == {"2.0": "1.9"}
    usual = {"2.0": (20, 1.0), "7.1": (20, 1.0)}                                       # (its history: the old version's)
    s = G.summarize({"2.0": (20, 3.2), "7.1": (20, 1.02)}, _days(8, usual), False, renamed={"2.0": "1.9"})
    assert s["concentrated"] and "2.0 (new, in place of 1.9: x3.2 its usual)" in s["reading"]
    # as many rows in all, some under a value that is new: none is lost
    counts = G.summarize({"2.0": (20, 20.0), "7.1": (20, 20.0)}, _days(8, {"1.9": (20, 20.0), "7.1": (20, 20.0)}), True)
    assert not counts["concentrated"] and "not seen on the earlier days: 2.0" in counts["reading"]


def test_the_field_that_holds_the_change_with_the_fewest_rows_comes_first():
    """The runs of one region are released four hours late. By application, the one whose runs come first is off
    too (a third of its rows): the region holds the same change on fewer rows, all of them off."""
    region = G.summarize({"EU": (110, 12500.0), "US": (135, 2020.0), "AS": (109, 2400.0)},
                         _days(8, {"EU": (190, 2500.0), "US": (135, 1980.0), "AS": (109, 2400.0)}), False,
                         scale=2260.0, change=354 * 3140.0)
    app = G.summarize({"ORDERS": (278, 5100.0), "INVOICES": (76, 6480.0)},
                      _days(8, {"ORDERS": (302, 143.0), "INVOICES": (136, 7060.0)}), False, scale=2260.0, change=354 * 3140.0)
    assert region["concentrated"] and app["concentrated"] and app["strength"] > region["strength"]     # x35 against x5
    assert G.rank(region) > G.rank(app) and G.localized({"APP": app, "REGION": region}) == ["REGION", "APP"]
    text = G.conclusion({"APP": app, "REGION": region})
    assert text.startswith('The change is concentrated on "REGION": concentrated on EU (x5 its usual)') and 'Also seen on "APP"' in text
    # a field whose values hold a tenth of the change comes last, whatever its ratio
    version = G.summarize({"5.9": (175, 743.0), "4.1": (32, 6300.0)}, _days(8, {"5.9": (300, 130.0), "4.1": (136, 7060.0)}),
                          False, scale=2260.0, change=354 * 3140.0)
    assert version["coverage"] < 0.25 and G.localized({"VERSION": version, "REGION": region})[0] == "REGION"


def _judged(measure, now, usual, fields=None, rows=None):
    overall = G.overall(now, [usual] * 8, rows)
    return measure, overall, fields or {}


def test_the_stages_in_their_order_and_the_first_that_is_off():
    quiet = G.summarize({"EU": (20, 20.0), "US": (20, 20.0)}, _days(8, {"EU": (20, 20.0), "US": (20, 20.0)}), True)
    late = G.summarize({"EU": (0, 0.0), "US": (20, 20.0)}, _days(8, {"EU": (20, 20.0), "US": (20, 20.0)}), True, change=-20.0)
    stuck = G.summarize({"EU": (20, 20.0), "US": (0, 0.0)}, _days(8, {"EU": (0, 0.0), "US": (0, 0.0)}), True, change=20.0)
    slow = G.summarize({"EU": (10, 1.0), "US": (10, 3.0)}, _days(8, {"EU": (10, 1.0), "US": (10, 1.0)}), False, scale=1.0,
                       change=20 * 1.0)
    judged = [
        _judged(G.Measure("count"), (40, 40.0), (40, 40.0), {"REGION": quiet}),
        _judged(G.Measure("ratio", ("D", "U")), (20, 2.0), (38, 1.0), {"REGION": slow}),
        _judged(G.Measure("reached", ("READY",)), (20, 20.0), (40, 40.0), {"REGION": late}),
        _judged(G.Measure("reached", ("ENDED",)), (18, 18.0), (38, 38.0), {"REGION": late}),
        _judged(G.Measure("lapse", ("SCHED", "READY")), (20, 70.0), (40, 60.0), {"REGION": quiet}),
        _judged(G.Measure("lapse", ("READY", "ENDED")), (18, 900.0), (38, 880.0), {"REGION": quiet}),
        _judged(G.Measure("between", ("SCHED", "READY")), (20, 20.0), (0, 0.0), {"REGION": stuck}),
        _judged(G.Measure("age", ("SCHED", "READY")), (20, 14000.0), (0, None), {"REGION": quiet}),
        _judged(G.Measure("avg", ("TRADES",)), (40, 500.0), (40, 498.0), {"REGION": quiet}),
    ]
    out = G.survey(judged, stages=["SCHED", "READY", "ENDED"], what={"READY": "When what it waits for was done"},
                   every_row=["SCHED"])
    assert out["stages"][0] == "SCHED: every row had reached it, as usual"
    assert out["stages"][1].startswith("READY (When what it waits for was done): rows that had reached it: 20 against 40 usually "
                                       "(below usual); on average 70 s against 60 s usually")
    assert "rows past SCHED and not yet there: 20 against 0 usually (above usual), since 14,000 s on average" in out["stages"][1]
    text = out["conclusion"]
    assert text.startswith("In the order of the stages, READY is the first that is clearly off: what is late after it may only "
                           "be late in its wake. What stands out, that stage first: (1) rows that had reached READY by then "
                           '(below usual in total): on "REGION", concentrated on EU (0 against 20 usually)')
    assert text.count("rows that had reached ENDED by then (below usual in total)") == 1                # said once, with it
    assert "(the same values for: rows past SCHED and not yet at READY then (above usual in total); rows that had reached ENDED" in text
    assert '(2) sum of D / sum of U (above usual in total): on "REGION", concentrated on US (x3 its usual)' in text
    assert out["as_usual"] == ["number of rows (x1.0)", "avg of TRADES (x1.0)"]
    assert [m["measure"] for m in out["measures"]][:2] == ["rows that had reached READY by then", "sum of D / sum of U"]
    assert out["measures"][0]["values"] == [{"value": "EU", "now": 0.0, "usual": 20.0, "ratio": 0.0}]
    # nothing off: said so
    calm = G.survey([_judged(G.Measure("count"), (40, 40.0), (40, 40.0), {"REGION": quiet}),
                     _judged(G.Measure("avg", ("TRADES",)), (40, 500.0), (40, 498.0), {"REGION": quiet})])
    assert calm["conclusion"] == "Nothing stands out: every measure is as usual, in total and for each value of each field."
    # an average that fewer rows have than usually (the runs in progress) is over other rows: not compared
    o = G.overall((20, 300.0), [(40, 600.0)] * 8, rows=(40, [40] * 8))
    assert o["verdict"] == "not comparable yet" and o["why"].startswith("50% of the rows have it now, 100% usually")
    later = G.survey([(G.Measure("avg", ("WAIT",)), o, {"REGION": quiet})])
    assert later["not_comparable_yet"][0].startswith("avg of WAIT: 50% of the rows")


@pytest.fixture()
def night(world, monkeypatch):  # noqa: F811
    """The runs of six weekdays and of today (Wednesday 2026-09-23, asked at 00:50): forty a night, one a minute
    from midnight, released a minute later, started two minutes after, ten minutes long. Today BILLING runs its
    new version 2.0 (released the evening before), three times as long."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from supagent import tools as T

    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "runs" ("ts" TIMESTAMP, "SCHED" TIMESTAMP, "READY" TIMESTAMP, "STARTED" TIMESTAMP, "ENDED" TIMESTAMP, '
                '"APP" VARCHAR, "REGION" VARCHAR, "NODE" VARCHAR, "STATUS" VARCHAR, "VERSION" VARCHAR, "DURATION_S" DOUBLE, '
                '"USUAL_S" DOUBLE, "TRADES" DOUBLE, "POSITION_DATE" VARCHAR, "POSITION_LABEL" VARCHAR)')
    con.execute('CREATE TABLE "changes" ("ts" TIMESTAMP, "TARGET" VARCHAR, "WHAT" VARCHAR, "TICKET" VARCHAR)')
    con.execute("INSERT INTO \"changes\" VALUES ('2026-09-22 19:30:00', 'BILLING', 'Release of BILLING 2.0', 'CHG-7'), "
                "('2026-09-10 19:30:00', 'BILLING', 'Release of BILLING 1.9', 'CHG-3'), "
                "('2026-09-22 18:00:00', 'LEDGER', 'Certificate renewed', 'CHG-6')")
    today, now = dt.date(2026, 9, 23), dt.datetime(2026, 9, 23, 0, 50)
    labels = {"20260922": "D-1", "20260921": "D-2", "20260918": "D-3", "20260917": "D-4", "20260916": "D-5",
              "20260915": "W-1", "20260914": "W-1"}
    day, n = today, 0
    while n < 7:
        if day.weekday() < 5:
            pos = day - dt.timedelta(days=3 if day.weekday() == 0 else 1)
            for i in range(40):
                app = "BILLING" if i % 2 else "LEDGER"
                sched = dt.datetime.combine(day, dt.time(0, i))
                ready, start = sched + dt.timedelta(minutes=1), sched + dt.timedelta(minutes=3)
                seconds = 1800.0 if day == today and app == "BILLING" else 600.0
                end = start + dt.timedelta(seconds=seconds)
                over = day != today or end <= now
                started = day != today or start <= now
                con.execute('INSERT INTO "runs" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                    start if started else sched, sched, ready if day != today or ready <= now else None,
                    start if started else None, end if over else None, app, "EU" if i % 5 else "US",
                    f"srv-{i % 4 + 1}" if started else None, "DONE" if over else "RUNNING" if started else "QUEUED",
                    (("2.0" if day == today else "1.9") if app == "BILLING" else "7.1") if started else None,
                    seconds if over else None, 600.0, 500.0 + i, f"{pos:%Y%m%d}", labels[f"{pos:%Y%m%d}"]])
            n += 1
        day -= dt.timedelta(days=1)
    conn = _Conn(con, now)

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    cat = {"indices": {"runs": {"time_field": "ts", "fields": {
        "USUAL_S": {"usual_of": "DURATION_S"}, "READY": {"description": "When what it waits for was done"},
        "STARTED": {"description": "When it got a slot"}},
        "relationships": [{"to": "changes", "keys": {"APP": "TARGET"}, "description": "the changes made to the application"}]},
        "changes": {"time_field": "ts"}}}
    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "GROUP_THREADS", 1)
    monkeypatch.setattr(T, "_catalog", lambda: cat)
    monkeypatch.setattr(T, "_table_database", lambda table, ref: world["jobs"])
    monkeypatch.setattr(T, "_group_fields", lambda table, fixed, label: [f for f in ("APP", "REGION", "NODE", "STATUS", "VERSION") if f not in fixed])
    monkeypatch.setattr(T, "_measures", lambda table, tf: [
        G.Measure("count"), G.Measure("ratio", ("DURATION_S", "USUAL_S")), G.Measure("reached", ("SCHED",)),
        G.Measure("reached", ("READY",)), G.Measure("reached", ("STARTED",)), G.Measure("reached", ("ENDED",)),
        G.Measure("avg", ("TRADES",))])
    from superset.extensions import security_manager as sm

    from supagent import settings

    monkeypatch.setattr(sm, "raise_for_access", lambda **kw: None)
    settings.set_value("agent.now", "2026-09-23 00:50")
    yield conn
    settings.set_value("agent.now", None)


def test_one_call_says_what_is_off_where_and_what_was_changed_there(night, app):
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-22 00:00", end="2026-09-23 00:50", group_by=["POSITION_LABEL", "APP"],
                           where='''"POSITION_LABEL" = 'D-1' ''')
    assert "error" not in r, r
    # the business date's rows as they were at 00:50 on each day, the weekend read once
    assert r["window"] == "the rows of the business date (D-1) as they were on 2026-09-23 at 00:50"
    assert "at 00:50 on 2026-09-22, 2026-09-21, 2026-09-18, 2026-09-17, 2026-09-16, 2026-09-15" in r["compared_with"]
    assert r["fields_compared"] == ["APP", "REGION", "NODE", "VERSION"]                 # the label is the scope's; no status
    assert "POSITION_LABEL (the scope pins it to one value)" in r["note"] and '"then": at 00:50' in r["note"]
    assert r["not_compared"]["STATUS"].startswith("not compared: its values change as a row advances (a status)")
    # the stages in their order, each against the same time of day
    assert [s.split(":")[0] for s in r["stages"]] == ["SCHED", "READY (When what it waits for was done)",
                                                      "STARTED (When it got a slot)", "ENDED"]
    assert "every row had reached it, as usual" in r["stages"][0]
    assert "on average 60 s against 60 s usually after SCHED" in r["stages"][1]
    assert r["stages"][3].startswith("ENDED: rows that had reached it: 27 against 37 usually (below usual); on average 956 s "
                                     "against 600 s usually (above usual) after STARTED; rows past STARTED and not yet there: "
                                     "13 against 3 usually (above usual), since 1,094 s on average against 540 usually")
    text = r["conclusion"]
    assert text.startswith("In the order of the stages, ENDED is the first that is clearly off")
    assert 'on "APP", concentrated on BILLING (x3 its usual)' in text
    ratio = next(m for m in r["measures"] if m["measure"] == "sum of DURATION_S / sum of USUAL_S")
    assert ratio["over"].startswith("the rows that had reached ENDED by 00:50, on each day alike (28 of 40 now)")
    assert ratio["values"][0] == {"value": "BILLING", "rows_now": 8, "now": 3.0, "usual": 1.0, "ratio": 3.0,
                                  "then": {"yesterday": 1.0, "1 week ago": 1.0},
                                  "since": "new on this day (as usual on the days before)"}
    assert any(line.endswith("APP = BILLING: new on this day (as usual on the days before)") for line in r["since_when"])
    assert "2.0 (new, in place of 1.9: x3 its usual)" in str(r["measures"]) + text      # the new version, against the old
    assert "avg of TRADES (x1.0)" in r["as_usual"]
    # what the catalog's join holds about the application that stands out: its change of the evening before
    related = r["related"][0]
    assert (related["table"], related["about"], related["records"]) == ("changes", '"TARGET" = BILLING', 1)
    assert related["what"] == "the changes made to the application" and related["latest"][0]["WHAT"] == "Release of BILLING 2.0"
    assert len(str(r)) < 6600
    assert list(r)[:8] == ["table", "database", "database_id", "window", "conclusion", "since_when",    # what it found first
                           "related", "against_reference_days"]
    # the team's reference days: what stands out, on each of them
    assert r["reference_days"] == {"yesterday": "2026-09-22", "1 week ago": "2026-09-16", "4 weeks ago": "no data that day"}
    assert any(line.startswith("sum of DURATION_S / sum of USUAL_S, APP = BILLING: 3 now, 1 yesterday, 1 1 week ago")
               for line in r["against_reference_days"])


def test_a_call_stops_reading_fields_when_its_time_is_up(night, app, monkeypatch):
    from supagent import settings
    from supagent import tools as T
    from supagent.security import acting_as

    clock = iter(range(0, 100000, 20))                           # every look at the clock: 20 s later
    monkeypatch.setattr(T.time, "time", lambda: next(clock))
    with app.app_context(), acting_as("admin"):
        r = T.compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where='''"POSITION_LABEL" = 'D-1' ''')
        settings.set_value("agent.compare_seconds", 100000)
        try:
            whole = T.compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where='''"POSITION_LABEL" = 'D-1' ''')
        finally:
            settings.set_value("agent.compare_seconds", None)
    assert "error" not in r, r
    left = [f for f, why in r["not_compared"].items() if "agent.compare_seconds" in why]
    assert left and r["fields_compared"] and not set(left) & set(r["fields_compared"])       # one field at least, then no more
    assert "ask it alone in group_by" in r["not_compared"][left[0]]
    assert not [w for w in (whole.get("not_compared") or {}).values() if "compare_seconds" in w]


def test_one_measure_is_still_asked_for_its_values_in_full(night, app):
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="ratio(DURATION_S, USUAL_S)",
                           group_by=["NODE"], where='''"POSITION_LABEL" = 'D-1' ''')
        quiet = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where='''"APP" = 'LEDGER' AND "POSITION_LABEL" = 'D-1' ''')
    assert "error" not in r, r
    assert r["measure"] == "sum of DURATION_S / sum of USUAL_S" and r["total"]["verdict"] == "above usual"
    assert set(r["fields"]) == {"NODE", "APP", "REGION", "VERSION", "STATUS"}             # the one asked, then the table's own
    assert r["conclusion"].startswith('The change is concentrated on "APP": concentrated on BILLING (x3 its usual)')
    assert "groups" in r["fields"]["NODE"] and r["related"][0]["latest"][0]["TICKET"] == "CHG-7"
    assert quiet["conclusion"].startswith("Nothing stands out") or "BILLING" not in quiet["conclusion"]
    assert "warning" not in r
    # a window of another day says so (a date written one digit off reads another day's incident as today's)
    with app.app_context(), acting_as("admin"):
        old = compare_groups(table="runs", start="2026-09-18 00:00", end="2026-09-18 00:50", measure="count", group_by=["APP"])
    assert old["warning"] == ("this window ended 5 day(s) before now (now is Wednesday 2026-09-23 00:50): if the question "
                              "is about today, call again with end = now")
    assert list(old)[:6] == ["table", "database", "database_id", "window", "warning", "conclusion"]     # what it found first


def test_records_that_mention_what_stands_out_when_nothing_joins_it(night, app, monkeypatch):
    """The region has no join in the catalog: the related tables' records whose text mentions it are given (the
    change that says it concerns that region), from the tables within two joins that have a text field."""
    from superset.extensions import db

    from supagent import tools as T
    from supagent.models import KObject, Source
    from supagent.security import acting_as

    night.con.execute("INSERT INTO \"changes\" VALUES ('2026-09-22 17:00:00', 'network', 'Firewall rules of the EU-WEST site replaced', 'CHG-9')")
    night.con.execute("UPDATE \"runs\" SET \"REGION\" = 'EU-WEST' WHERE \"REGION\" = 'EU'")
    night.con.execute("UPDATE \"runs\" SET \"ENDED\" = NULL, \"DURATION_S\" = NULL WHERE \"REGION\" = 'EU-WEST' AND \"POSITION_DATE\" = '20260922' "
                      "AND \"SCHED\" >= TIMESTAMP '2026-09-23 00:10:00'")
    with app.app_context():
        src = db.session.query(Source).first()
        db.session.add(KObject(source_id=src.id, kind="field", name="WHAT", parent="changes", data_type="text", stats={}))
        db.session.commit()
        try:
            assert T._neighbours("runs") == {"changes": "the changes made to the application"}
            with acting_as("admin"):
                r = T.compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count",
                                     time_field="ENDED", group_by=["REGION"], where='''"POSITION_LABEL" = 'D-1' ''')
        finally:
            db.session.query(KObject).filter(KObject.parent == "changes", KObject.name == "WHAT").delete()
            db.session.commit()
    assert "error" not in r, r
    assert r["conclusion"].startswith('The change is concentrated on "REGION": concentrated on EU-WEST')
    said = r["related"][0]
    assert said["about"] == "records whose WHAT mentions EU-WEST" and said["table"] == "changes" and said["records"] == 1
    assert said["latest"][0]["WHAT"] == "Firewall rules of the EU-WEST site replaced" and "LIKE '%EU-WEST%'" in said["sql"]


def test_a_field_of_thousands_of_values_is_not_compared(night, app):
    from superset.extensions import db

    from supagent import tools as T
    from supagent.models import KObject, Source
    from supagent.security import acting_as

    with app.app_context():
        src = db.session.query(Source).first()
        db.session.add(KObject(source_id=src.id, kind="field", name="JOB", parent="runs", data_type="keyword",
                               stats={"cardinality": 5000}))
        db.session.commit()
        try:
            with acting_as("admin"):
                r = T.compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", group_by=["JOB", "APP"],
                                     where='''"POSITION_LABEL" = 'D-1' ''')
        finally:
            db.session.query(KObject).filter(KObject.parent == "runs", KObject.name == "JOB").delete()
            db.session.commit()
    assert "error" not in r, r
    assert "not compared: JOB (5,000 values: too many to say where a change is)" in r["note"]
    assert "JOB" not in r["fields_compared"] and r["fields_compared"][0] == "APP"


# ----------------------------------------------------------------------------------------------------------------- #
# the reference days: yesterday, a week ago, three weeks ago, months ago (the user's own comparisons)
# ----------------------------------------------------------------------------------------------------------------- #
def test_reference_days_are_read_from_words():
    refs = G.references(["yesterday", "2 days ago", "1 week ago", "3 weeks ago", "3 months ago"])
    assert [(r.kind, r.days) for r in refs] == [("day", 1), ("day", 2), ("week", 7), ("week", 21), ("week", 91)]
    assert [(r.kind, r.days) for r in G.references("last week, last month, 1 year ago")] == [("week", 7), ("week", 28), ("week", 364)]
    assert [(r.kind, r.days) for r in G.references("hier, il y a 2 semaines, 6 mois")] == [("day", 1), ("week", 14), ("week", 182)]
    assert G.references("2026-06-15")[0].date == dt.date(2026, 6, 15) and G.references(None) == [] and G.references("none") == []
    assert len(G.references(["1 week ago", "7 days ago", "last week"])) == 1                  # the same day, once
    for bad in ("soon", "0 days ago", "5 years ago", "2026-13-45", ["1 day ago", "2 days ago", "3 days ago", "4 days ago",
                                                                    "5 days ago", "6 days ago"]):
        with pytest.raises(G.GroupsError):
            G.references(bad)


@pytest.fixture()
def weeks(world, monkeypatch):  # noqa: F811
    """Thirty weekdays of runs up to Monday 2026-09-21 (asked at 05:00, every run of the night ended): twenty a
    night, half of each application. INVOICES runs twice as long since Monday 2026-09-07 (ten working days: more
    than the days of the usual)."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from osagg import calendar

    from supagent import settings
    from supagent import tools as T

    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "runs" ("ts" TIMESTAMP, "APP" VARCHAR, "REGION" VARCHAR, "DURATION_S" DOUBLE, "USUAL_S" DOUBLE, '
                '"POSITION_DATE" VARCHAR, "POSITION_LABEL" VARCHAR, "TRADES" DOUBLE, "WAIT_S" DOUBLE)')
    now = dt.datetime(2026, 9, 21, 5, 0)
    conn = _Conn(con, now)
    key = calendar.asof_key(now, conn.label_cutoff, True)
    day, n = now.date(), 0
    while n < 30:
        if day.weekday() < 5:
            pos = day - dt.timedelta(days=3 if day.weekday() == 0 else 1)
            for i in range(20):
                app = "INVOICES" if i % 2 else "ORDERS"
                slow = 2.0 if app == "INVOICES" and day >= dt.date(2026, 9, 7) else 1.0
                region = "EU" if i % 4 else "US"
                con.execute('INSERT INTO "runs" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                    dt.datetime.combine(day, dt.time(1, i)), app, region, 600.0 * slow, 600.0,
                    f"{pos:%Y%m%d}", calendar.label(f"{pos:%Y%m%d}", key) or "old",
                    1000.0 if day.weekday() == 0 else 500.0,                        # every Monday: three days of trades
                    180.0 if region == "US" and day >= dt.date(2026, 9, 17) else 60.0])      # the US runs wait since the 17th
            n += 1
        day -= dt.timedelta(days=1)

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "GROUP_THREADS", 1)
    monkeypatch.setattr(T, "_catalog", lambda: {"indices": {"runs": {"time_field": "ts", "fields": {"USUAL_S": {"usual_of": "DURATION_S"}}}}})
    monkeypatch.setattr(T, "_table_database", lambda table, ref: world["jobs"])
    monkeypatch.setattr(T, "_group_fields", lambda table, fixed, label: [f for f in ("APP", "REGION") if f not in fixed])
    monkeypatch.setattr(T, "_measures", lambda table, tf: [G.Measure("count"), G.Measure("ratio", ("DURATION_S", "USUAL_S"))])
    from superset.extensions import security_manager as sm

    monkeypatch.setattr(sm, "raise_for_access", lambda **kw: None)
    settings.set_value("agent.now", "2026-09-21 05:00")
    yield conn
    settings.set_value("agent.now", None)


def test_a_change_older_than_the_usual_is_seen_against_an_older_day(weeks, app):
    """Twice as long for ten working days: every day of the usual is as slow, so nothing stands out against it.
    Against four weeks ago (the team's reference days) it does, and where is said."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''')
    assert "error" not in r, r
    assert r["conclusion"].startswith("Nothing stands out")
    # a Monday: "yesterday" is the Friday (the day before that has runs of its own business date), a week ago the Monday before
    assert r["reference_days"] == {"yesterday": "2026-09-18", "1 week ago": "2026-09-14", "4 weeks ago": "2026-08-24"}
    assert "2026-09-20" not in r["compared_with"] and "2026-09-19" not in r["compared_with"]      # the weekend: no day of its own
    old = r["against"]
    assert [x["reference"] for x in old] == ["4 weeks ago (2026-08-24)"]
    assert 'sum of DURATION_S / sum of USUAL_S (above then in total): on "APP", concentrated on INVOICES (x2 its value then)' in old[0]["what_differs"]
    assert r["against_note"].startswith("as usual against the last days, but not against these older days")
    assert list(r)[:5] == ["table", "database", "database_id", "window", "conclusion"] and list(r)[5] == "against"


def test_the_reference_days_a_question_names(weeks, app):
    from supagent import settings
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''',
                           against=["yesterday", "3 weeks ago", "2026-08-12", "1 year ago"], measure="ratio(DURATION_S, USUAL_S)",
                           group_by=["APP"])
        bad = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", against=["the other day"])
        settings.set_value("agent.compare_against", "")
        try:
            none = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''')
        finally:
            settings.set_value("agent.compare_against", None)
    assert "error" not in r, r
    assert r["reference_days"] == {"yesterday": "2026-09-18", "3 weeks ago": "2026-08-31", "2026-08-12": "2026-08-12",
                                   "1 year ago": "no data that day"}
    assert r["total"]["then"] == {"yesterday": 1.5, "3 weeks ago": 1.0, "2026-08-12": 1.0}
    assert r["against_reference_days"] == ["sum of DURATION_S / sum of USUAL_S: 1.5 now, 1.5 yesterday, 1 3 weeks ago, 1 2026-08-12",
                                           "no data on 1 year ago: nothing to compare with there"]
    assert [x["reference"] for x in r["against"]] == ["3 weeks ago (2026-08-31)", "2026-08-12 (2026-08-12)"]       # not yesterday: the same
    assert all("concentrated on INVOICES (x2 its value then)" in x["what_differs"] for x in r["against"]) and "against_note" not in r
    assert bad["error"].startswith("against: not a reference day: 'the other day'")
    assert "reference_days" not in none and "against" not in none                    # no reference day: the usual alone
    # nothing stands out and nothing older differs: the result says to answer that, not to keep looking
    assert none["conclusion"].startswith("Nothing stands out") and list(none)[4:6] == ["conclusion", "next"]
    assert none["next"].startswith("Nothing is unusual in these rows: answer that now")
    assert "next" not in r


# ----------------------------------------------------------------------------------------------------------------- #
# not only runs of a batch: any table with a time field (orders here, no business date)
# ----------------------------------------------------------------------------------------------------------------- #
@pytest.fixture()
def orders(world, monkeypatch):  # noqa: F811
    """Ten days of orders, sixty each morning (00:00 to 10:00), paid ten minutes later and shipped an hour after.
    Today (Thursday 2026-09-24, asked at 12:00) the orders of one country are paid and not shipped, and the baskets
    of one channel are twice as big."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from supagent import settings
    from supagent import tools as T

    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "orders" ("CREATED" TIMESTAMP, "PAID" TIMESTAMP, "SHIPPED" TIMESTAMP, "COUNTRY" VARCHAR, '
                '"CHANNEL" VARCHAR, "STATE" VARCHAR, "AMOUNT" DOUBLE)')
    con.execute('CREATE TABLE "notices" ("AT" TIMESTAMP, "TEXT" VARCHAR)')
    con.execute("INSERT INTO \"notices\" VALUES ('2026-09-24 01:30:00', 'Carrier strike: no pick-up in DEU until further notice'), "
                "('2026-09-20 09:00:00', 'New payment page in FRA')")
    today, now = dt.date(2026, 9, 24), dt.datetime(2026, 9, 24, 12, 0)
    for back in range(10):
        day = today - dt.timedelta(days=back)
        for i in range(60):
            created = dt.datetime.combine(day, dt.time(i // 6, (i % 6) * 10))
            country = ("DEU", "FRA", "ESP")[i % 3]
            channel = "app" if i % 4 == 0 else "web"
            shipped = None if day == today and country == "DEU" else created + dt.timedelta(minutes=70)
            con.execute('INSERT INTO "orders" VALUES (?, ?, ?, ?, ?, ?, ?)', [
                created, created + dt.timedelta(minutes=10), shipped, country, channel,
                "SHIPPED" if shipped else "PAID", (100.0 if channel == "web" else 80.0) * (2.0 if day == today and channel == "app" else 1.0)])
    conn = _Conn(con, now)

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    cat = {"indices": {"orders": {"time_field": "CREATED", "fields": {
        "PAID": {"description": "When the payment was accepted"}, "SHIPPED": {"description": "When the carrier picked it up"}},
        "relationships": [{"to": "notices", "keys": {"NOTICE": "ID"}, "description": "the notices of the operations"}]},
        "notices": {"time_field": "AT"}}}
    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "GROUP_THREADS", 1)
    monkeypatch.setattr(T, "_catalog", lambda: cat)
    monkeypatch.setattr(T, "_table_database", lambda table, ref: world["jobs"])
    monkeypatch.setattr(T, "_group_fields", lambda table, fixed, label: [f for f in ("COUNTRY", "CHANNEL", "STATE") if f not in fixed])
    monkeypatch.setattr(T, "_measures", lambda table, tf: [G.Measure("count"), G.Measure("reached", ("PAID",)),
                                                           G.Measure("reached", ("SHIPPED",)), G.Measure("avg", ("AMOUNT",))])
    from superset.extensions import db, security_manager as sm

    from supagent.models import KObject, Source

    monkeypatch.setattr(sm, "raise_for_access", lambda **kw: None)
    settings.set_value("agent.now", "2026-09-24 12:00")
    src = db.session.query(Source).first()
    db.session.add(KObject(source_id=src.id, kind="field", name="TEXT", parent="notices", data_type="text", stats={}))
    db.session.commit()
    yield conn
    db.session.query(KObject).filter(KObject.parent == "notices").delete()
    db.session.commit()
    settings.set_value("agent.now", None)


def test_any_table_with_a_time_field_is_compared_the_same_way(orders, app):
    """Orders, no business date: the same call says which stage is off and where (the orders of one country paid
    and not shipped), which measure moved (the baskets of one channel), leaves the state out (it follows the
    stage), and gives the notice that mentions the country."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="orders", start="2026-09-24 00:00", end="2026-09-24 12:00")
    assert "error" not in r, r
    assert r["window"] == "2026-09-24 00:00 to 2026-09-24 12:00 on CREATED"
    assert r["compared_with"].startswith("the same window on 2026-09-23, 2026-09-22, 2026-09-21")
    assert r["fields_compared"] == ["COUNTRY", "CHANNEL"] and "a status" in r["not_compared"]["STATE"]
    assert [s.split(":")[0] for s in r["stages"]] == ["PAID (When the payment was accepted)", "SHIPPED (When the carrier picked it up)"]
    assert "rows that had reached it: 40 against 60 usually (below usual)" in r["stages"][1]
    assert "rows past PAID and not yet there: 20 against 0 usually (above usual)" in r["stages"][1]
    text = r["conclusion"]
    assert text.startswith("In the order of the stages, SHIPPED is the first that is clearly off")
    assert 'on "COUNTRY", concentrated on DEU (0 against 20 usually) below usual' in text
    assert 'avg of AMOUNT (slightly above usual in total): on "CHANNEL", concentrated on app (x2 its usual)' in text
    said = r["related"][0]
    assert said["table"] == "notices" and said["about"] == "records whose TEXT mentions DEU"
    assert said["latest"][0]["TEXT"] == "Carrier strike: no pick-up in DEU until further notice"
    assert r["reference_days"] == {"yesterday": "2026-09-23", "1 week ago": "2026-09-17", "4 weeks ago": "no data that day"}
    assert any(line.startswith("rows that had reached SHIPPED by then, COUNTRY = DEU: 0 now, 20 yesterday, 20 1 week ago")
               for line in r["against_reference_days"])


def test_since_when_and_what_a_weekday_always_is(weeks, app, monkeypatch):
    """Two more readings from the days already read. Since when a value is off: the runs of one region wait three
    times as long since Thursday (today is Monday). And what a weekday always is: twice the trades every Monday
    is far from the last days and the same as the Monday before and four Mondays ago: not a change."""
    from supagent import tools as T
    from supagent.security import acting_as

    monkeypatch.setattr(T, "_measures", lambda table, tf: [G.Measure("count"), G.Measure("avg", ("WAIT_S",)),
                                                           G.Measure("avg", ("TRADES",))])
    with app.app_context(), acting_as("admin"):
        r = T.compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''')
    assert "error" not in r, r
    assert 'avg of WAIT_S' in r["conclusion"] and 'on "REGION", concentrated on US (x3 its usual)' in r["conclusion"]
    assert "avg of WAIT_S, REGION = US: off since 2026-09-17 (the 2 day(s) before too)" in r["since_when"]
    wait = next(m for m in r["measures"] if m["measure"] == "avg of WAIT_S")
    assert wait["values"][0]["then"] == {"yesterday": 180.0, "1 week ago": 60.0, "4 weeks ago": 60.0}
    trades = next(line for line in r["against_reference_days"] if line.startswith("avg of TRADES"))
    assert trades == ("avg of TRADES: 1,000 now, 500 yesterday, 1,000 1 week ago, 1,000 4 weeks ago (as on the same weekday of "
                      "the earlier weeks: what this weekday is, not a change)")
    assert not any("WAIT_S" in line and "what this weekday is" in line for line in r["against_reference_days"])


def test_a_value_never_seen_in_a_count_and_a_state_written_as_business_date(night, app):
    """Rows under a value the earlier days never had (another kind of run on the same servers) stand out when
    there are more rows in all; as many rows in all are rows that moved. A state written as a business-date
    label is refused with what belongs where."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    for i in range(30):                                        # thirty runs of a test campaign on the same servers, tonight
        night.con.execute('INSERT INTO "runs" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            dt.datetime(2026, 9, 23, 0, i), dt.datetime(2026, 9, 23, 0, i), dt.datetime(2026, 9, 23, 0, i),
            dt.datetime(2026, 9, 23, 0, i), dt.datetime(2026, 9, 23, 0, i + 5), "CAMPAIGN", "EU", "srv-1", "DONE", "0.1",
            300.0, 300.0, 10.0, "20260922", "D-1"])
    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count", group_by=["APP"],
                           where='''"REGION" = 'EU' ''')
        bad = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50",
                             where='''"POSITION_LABEL" IN ('RUNNING', 'QUEUED')''')
    assert "error" not in r, r
    assert r["total"]["verdict"] == "above usual" and r["total"]["now"] == 62.0 and r["total"]["usual"] == 32.0
    assert r["conclusion"].startswith('The change is concentrated on "APP": concentrated on CAMPAIGN (new: 30, not seen on the '
                                      'earlier days) above usual, while the 2 other value(s) are as usual')
    assert bad["error"].startswith('where: "POSITION_LABEL" = RUNNING, QUEUED: not a business-date label')
    # as many rows in all: rows that moved to a new value are not lost, and the new value does not stand out
    usual = {"a": (20, 20.0), "b": (20, 20.0)}
    s = G.summarize({"a": (20, 20.0), "c": (20, 20.0)}, _days(8, usual), counting=True)
    assert not s["concentrated"] and "not seen on the earlier days: c" in s["reading"]
    # nothing of it usually (nobody waits at that hour): the rows of a value never seen are all there is, and stand out
    s = G.summarize({"a": (0, 0.0), "new": (20, 20.0)}, _days(8, {"a": (0, 0.0)}), counting=True)
    assert s["concentrated"] and s["reading"].startswith("concentrated on new (new: 20, not seen on the earlier days) above usual")


def test_an_instant_in_the_scope_moves_with_each_earlier_day(night, app):
    """A scope written with a time (the rows since 00:23 today) would leave every earlier day empty: a date moves
    with each earlier day, on any time field, cast or cut to its day; what is no date (since now minus six
    hours) is left out; a condition between two fields or on the hour holds on every day and stays."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    times = {"ts", "STARTED", "READY"}
    cond, _labels = G.scope("""
        "A" = 1 AND "ts" >= TIMESTAMP '2026-08-06 01:00' AND DATE '2026-08-07' > "STARTED"
        AND CAST("ts" AS DATE) = '2026-08-06' AND "READY" > "ts" AND EXTRACT(HOUR FROM "ts") < 6
        AND "STARTED" >= CURRENT_DATE AND "ts" BETWEEN '2026-08-06 00:00:00' AND '2026-08-06T06:10'
        AND "ts" IN ('2026-08-06', '2026-08-07') AND "NAME" = '2026-08-06'""", None)
    kept, moving, removed = G.timed(cond, times)
    assert moving == ["ts", "STARTED"] and removed == ["STARTED"]
    sql = G.render(G.moved_back(kept, times, 2))
    for said in ("\"ts\" >= CAST('2026-08-04 01:00' AS TIMESTAMP)", "CAST('2026-08-05' AS DATE) > \"STARTED\"",
                 "CAST(\"ts\" AS DATE) = '2026-08-04'", '"READY" > "ts"', 'EXTRACT(HOUR FROM "ts") < 6',
                 "\"ts\" BETWEEN '2026-08-04 00:00:00' AND '2026-08-04T06:10'", "\"ts\" IN ('2026-08-04', '2026-08-05')",
                 "\"NAME\" = '2026-08-06'"):                   # (a text that looks like a date, on a field that is no time)
        assert said in sql, (said, sql)
    assert "CURRENT_DATE" not in sql and G.render(kept) != sql and G.render(G.moved_back(kept, times, 0)) == G.render(kept)
    assert G.timed(G.scope('"A" = 1', None)[0], times)[1:] == ([], []) and G.timed(None, times) == (None, [], [])
    assert G.conditioned(cond, times) == ["A", "NAME"]
    # a field the dictionary does not know as a time is one when the scope compares it with an instant
    written = G.scope(""""X" >= TIMESTAMP '2026-01-01' AND "Y" < NOW() - INTERVAL '6' HOUR AND "Z" = '2026-01-01'
                         AND "W" = CURRENT_DATE AND DATE_TRUNC('day', "V") = DATE '2026-01-01' AND "N" > 5""", None)[0]
    assert G.time_columns(written, {"ts"}) == {"ts", "X", "Y", "W", "V"}
    with app.app_context(), acting_as("admin"):
        plain = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count", group_by=["APP"],
                               where='''"REGION" = 'EU' ''')
        timed = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count", group_by=["APP"],
                               where='''"REGION" = 'EU' AND "ts" >= TIMESTAMP '2026-09-23 00:00:00' AND "STARTED" >= NOW() - INTERVAL '6' HOUR''')
        later = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count", group_by=["APP"],
                               where='''"REGION" = 'EU' AND "ts" >= '2026-09-23 00:23' ''')
    assert "error" not in timed and "error" not in later, (timed, later)
    assert timed["total"] == plain["total"] and timed["compared_with"] == plain["compared_with"]
    assert "the date the scope compares ts with was moved back with each earlier day (the same time of day)" in timed["note"]
    assert "the condition on STARTED in where was left out: start and end give the time" in timed["note"]
    assert (later["total"]["now"], later["total"]["usual"]) == (16.0, 16.0)        # the rows since 00:23, on each day alike


def test_who_else_is_on_what_stands_out(night, app):
    """The question's rows wait at a stage where they did not, on one value of a field (a region here; a pool, a
    queue): the tool follows the lead once by itself. Who else is there: the rows outside the question's scope
    that share that value during the window (by their life, not by their own time, which moves as a row
    advances), against the same window of the earlier days and by the scope's own fields. And the same comparison
    over every row there, whatever the scope's other conditions: what holds it."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    scope = '''"APP" IN ('BILLING', 'LEDGER') AND "POSITION_LABEL" = 'D-1' '''
    with app.app_context(), acting_as("admin"):
        quiet = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where=scope)
    assert "outside_the_scope" not in quiet and "the_rows_there" not in quiet      # (what stands out is the scope's own field)
    # tonight the runs of one region released after 00:20 have not started
    night.con.execute("""UPDATE "runs" SET "STARTED" = NULL, "ENDED" = NULL, "NODE" = NULL, "STATUS" = 'QUEUED', "VERSION" = NULL,
                         "DURATION_S" = NULL, "ts" = "SCHED"
                         WHERE "POSITION_DATE" = '20260922' AND "REGION" = 'EU' AND "SCHED" >= TIMESTAMP '2026-09-23 00:20:00'""")
    with app.app_context(), acting_as("admin"):
        alone = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where=scope)
    assert 'on "REGION", concentrated on EU (16 against 0 usually) above usual' in alone["conclusion"]
    assert alone["outside_the_scope"] == ['on "REGION" = EU: no row outside the scope during the window, as on the earlier days']
    # ... and a campaign runs there: thirty runs where there are none usually
    for i in range(30):
        night.con.execute('INSERT INTO "runs" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            dt.datetime(2026, 9, 23, 0, i), dt.datetime(2026, 9, 23, 0, i), dt.datetime(2026, 9, 23, 0, i),
            dt.datetime(2026, 9, 23, 0, i), None, "CAMPAIGN", "EU", "srv-1", "RUNNING", "0.1",
            None, 300.0, 10.0, "20260922", "D-1"])
    del night.sql[:]
    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", where=scope)
    assert "error" not in r, r
    text = r["conclusion"]
    assert text.startswith("In the order of the stages, STARTED is the first that is clearly off")
    assert 'on "REGION", concentrated on EU (16 against 0 usually) above usual' in text
    # a run that lasts longer once started is no wake of a late start: said as a finding of its own
    assert ("Off on its own too, whatever came before: ENDED (1,080 s against 600 s usually after STARTED): a late start "
            "does not explain it, look at it separately.") in text
    assert r["outside_the_scope"] == ['on "REGION" = EU, the rows outside the scope during the window: 30 against 0 usually '
                                      '(above usual). Who they are: "APP" = CAMPAIGN (30 rows, none on the earlier days)']
    held = r["the_rows_there"]
    assert held["rows"].startswith('every row with "REGION" = EU of the business date, whatever the scope\'s other conditions '
                                   "(the scope's rows wait there at STARTED)")
    assert held["stages"].startswith("first clearly off there: STARTED")
    assert any('on "APP", concentrated on CAMPAIGN (new: 30, not seen on the earlier days) above usual' in line
               for line in held["what_stands_out"])
    assert len(held["what_stands_out"]) <= 3 and "conclusion" not in held
    assert list(r)[4:7] == ["conclusion", "outside_the_scope", "the_rows_there"]    # said with what it found
    assert len(json.dumps(r, default=str)) <= 6500
    # who is there: the rows whose life overlaps the window, outside the scope of each day
    counted = [q for q in night.sql if q.startswith('SELECT "APP", COUNT(*) AS n FROM "runs" WHERE "REGION" IN (\'EU\') AND ')]
    assert len(counted) == 7 and all("AND NOT (" in q and "POSITION_DATE" in q for q in counted)
    assert ('"SCHED" >= TIMESTAMP \'2026-09-22 00:00:00\' AND "SCHED" < TIMESTAMP \'2026-09-23 00:50:00\' AND '
            '("ENDED" IS NULL OR "ENDED" >= TIMESTAMP \'2026-09-23 00:00:00\')') in counted[0]
    # the lead is followed once: the comparison made for it follows none, and reads no reference day
    assert "the_rows_there" not in held and "against_reference_days" not in held
    # as many others as on the earlier days: said for what the rows wait on, and nothing more
    same = G.others("POOL", ["p1"], ["ENV"], {("TEST",): 41}, [{("TEST",): 40}] * 8)
    assert not same["more"] and same["line"] == ('on "POOL" = p1, the rows outside the scope during the window: 41 against 40 '
                                                 'usually (as usual): nothing else has more rows there than on the earlier days')
    more = G.others("POOL", ["p1", "p2"], ["ENV", "KIND"], {("TEST", "nightly"): 40, ("TEST", "campaign"): 300, ("DEV", "nightly"): 12},
                    [{("TEST", "nightly"): 40, ("DEV", "nightly"): 10}] * 8)
    assert more["more"] and more["line"] == (
        'on "POOL" in (p1, p2), the rows outside the scope during the window: 352 against 50 usually (above usual). Who they '
        'are: "KIND" = campaign (300 rows, none on the earlier days); "ENV" = TEST (340 rows against 40 usually)')
    # every value above alike (twice the rows everywhere) names nobody
    alike = G.others("POOL", ["p1"], ["ENV"], {("TEST",): 80, ("DEV",): 20}, [{("TEST",): 40, ("DEV",): 10}] * 8)
    assert alike["more"] and alike["line"].endswith("100 against 50 usually (above usual)")
    # no scope: nothing is outside it
    with app.app_context(), acting_as("admin"):
        whole = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50", measure="count")
    assert "outside_the_scope" not in whole and "the_rows_there" not in whole


def test_a_reference_day_without_rows_is_said_and_never_compared(weeks, app):
    """A reference day the data does not reach (months ago, before the first index; a day off): said as such; no
    figure of that day, and nothing is called a change against it."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''',
                           against=["3 months ago", "2026-01-15", "1 year ago"])
        only = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", measure="count",
                              against=["6 months ago"])
        mixed = compare_groups(table="runs", start="2026-09-21 00:00", end="2026-09-21 05:00", where='''"POSITION_LABEL" = 'D-1' ''',
                               against=["yesterday", "6 months ago"], measure="ratio(DURATION_S, USUAL_S)")
    assert "error" not in r and "error" not in only and "error" not in mixed, (r, only, mixed)
    assert r["reference_days"] == {"3 months ago": "no data that day", "2026-01-15": "no data that day",
                                   "1 year ago": "no data that day"}
    assert only["reference_days"] == {"6 months ago": "no data that day"}
    for res in (r, only):
        assert "against" not in res and "against_note" not in res
        assert res["against_reference_days"] == ["no data on " + ", ".join(res["reference_days"]) + ": nothing to compare with there"]
        assert not _has(res, "then")
    assert mixed["reference_days"] == {"yesterday": "2026-09-18", "6 months ago": "no data that day"}
    assert mixed["total"]["then"] == {"yesterday": 1.5}
    assert mixed["against_reference_days"][-1] == "no data on 6 months ago: nothing to compare with there"


def test_who_else_is_there_on_a_table_that_is_no_batch(orders, app):
    """Orders, no business date: the web orders of one country are paid and not shipped. The same call says
    whether the other orders of that country (outside the question's scope) are more than usual, and makes the
    comparison over every order of that country: all of them are held, not only the web ones."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="orders", start="2026-09-24 00:00", end="2026-09-24 12:00", where='''"CHANNEL" = 'web' ''')
    assert "error" not in r, r
    assert 'on "COUNTRY", concentrated on DEU (0 against 15 usually) below usual' in r["conclusion"]
    assert "rows past PAID and not yet at SHIPPED then (above usual in total)" in r["conclusion"]
    # the other orders of that country: as many as on the earlier days (said, since the scope's rows wait there)
    assert r["outside_the_scope"] == ['on "COUNTRY" = DEU, the rows outside the scope during the window: 5 against 5 usually '
                                      '(as usual): nothing else has more rows there than on the earlier days']
    held = r["the_rows_there"]
    assert held["rows"] == ('every row with "COUNTRY" = DEU, whatever the scope\'s other conditions (the scope\'s rows wait '
                            'there at SHIPPED): what holds it')
    assert held["stages"] == "first clearly off there: SHIPPED"
    # every order of that country is held, whatever its channel: no field says where among them
    assert held["what_stands_out"][0] == ("rows that had reached SHIPPED by then: 0 against 20 usually (below usual) in total, "
                                          "on no field in particular")
    # the rows that are there: from their first stage to their last, a day before the window at most (the usual
    # way through the stages is an hour)
    counted = [q for q in orders.sql if q.startswith('SELECT "CHANNEL", COUNT(*) AS n FROM "orders" WHERE "COUNTRY" IN (\'DEU\') AND ')]
    assert len(counted) == 9 and ('"PAID" >= TIMESTAMP \'2026-09-23 00:00:00\' AND "PAID" < TIMESTAMP \'2026-09-24 12:00:00\' AND '
                                  '("SHIPPED" IS NULL OR "SHIPPED" >= TIMESTAMP \'2026-09-24 00:00:00\')') in counted[0]
    # today a partner sends thirty orders of that country: who else is there, and how many
    for i in range(30):
        created = dt.datetime(2026, 9, 24, 1, i)
        orders.con.execute('INSERT INTO "orders" VALUES (?, ?, ?, ?, ?, ?, ?)', [
            created, created + dt.timedelta(minutes=10), None, "DEU", "partner", "PAID", 90.0])
    with app.app_context(), acting_as("admin"):
        busy = compare_groups(table="orders", start="2026-09-24 00:00", end="2026-09-24 12:00", where='''"CHANNEL" = 'web' ''')
    assert busy["outside_the_scope"] == ['on "COUNTRY" = DEU, the rows outside the scope during the window: 35 against 5 usually '
                                         '(above usual). Who they are: "CHANNEL" = partner (30 rows, none on the earlier days)']
    # a lead is followed only while the call has time left, and not at all when the setting is off
    from supagent import settings

    settings.set_value("agent.compare_follow", False)
    try:
        with app.app_context(), acting_as("admin"):
            off = compare_groups(table="orders", start="2026-09-24 00:00", end="2026-09-24 12:00", where='''"CHANNEL" = 'web' ''')
    finally:
        settings.set_value("agent.compare_follow", None)
    assert "the_rows_there" not in off and off["outside_the_scope"] == busy["outside_the_scope"]


def test_a_business_date_is_of_the_day_the_window_ends(weeks, app):
    """Asked today about last Monday's batch (a window that ended a week ago, the scope's label D-1): the business
    date is that Monday's own (the Friday before it), and each earlier day's own from there; not today's."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    with app.app_context(), acting_as("admin"):
        past = compare_groups(table="runs", start="2026-09-14 00:00", end="2026-09-14 05:00", where='''"POSITION_LABEL" = 'D-1' ''')
        dated = compare_groups(table="runs", start="2026-09-14 00:00", end="2026-09-14 05:00", measure="count",
                               where='''"POSITION_DATE" = '20260911' ''')
    assert "error" not in past and "error" not in dated, (past, dated)
    assert past["window"] == "the rows of the business date (D-1) as they were on 2026-09-14 at 05:00"
    assert "at 05:00 on 2026-09-11, 2026-09-10, 2026-09-09" in past["compared_with"]
    assert past["warning"].startswith("this window ended 7 day(s) before now")
    assert "number of rows (x1.0)" in past["as_usual"]                       # the twenty runs of that Monday, as every day
    assert (dated["total"]["now"], dated["total"]["usual"]) == (20.0, 20.0)   # the same batch, by its date
    assert any('"POSITION_DATE" IN (\'20260911\')' in q and "2026-09-14" in q for q in weeks.sql)


def test_a_scope_written_with_or_before_and_is_read_as_it_was_meant(night, app):
    """`"APP" = 'a' OR "APP" = 'b' AND "REGION" = 'EU' AND the business date`: in SQL the AND binds first, and 'a'
    would be read over every region and every day. The alternatives of one field are taken together; two
    alternatives of a field do not pin it; an OR that leaves alternatives without the business date is refused."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    def said(where, label="POSITION_LABEL"):
        cond, labels = G.scope(where, label)
        return G.render(cond), labels

    assert said("APP = 'a' OR APP = 'b' AND ENV = 'P' AND POSITION_LABEL = 'D-1'") == (
        '("APP" = \'a\' OR "APP" = \'b\') AND "ENV" = \'P\' AND "POSITION_LABEL" = \'D-1\'', ["D-1"])
    assert said('"ENV" = \'P\' AND "APP" = \'a\' OR "APP" = \'b\' OR "APP" = \'c\'')[0] == \
        '("APP" = \'a\' OR "APP" = \'b\' OR "APP" = \'c\') AND "ENV" = \'P\''
    assert said('("APP" = \'a\' OR "APP" = \'b\') AND "ENV" = \'P\'')[0] == '("APP" = \'a\' OR "APP" = \'b\') AND "ENV" = \'P\''
    assert said('"APP" = \'a\' OR "ENV" = \'P\' AND "X" = 1')[0] == '"APP" = \'a\' OR "ENV" = \'P\' AND "X" = 1'      # as written
    with pytest.raises(G.GroupsError, match="an OR at the top leaves some of its alternatives without the business date"):
        G.scope('"APP" = \'a\' OR "REGION" = \'EU\' AND "POSITION_LABEL" = \'D-1\'', "POSITION_LABEL")
    cond, _labels = G.scope("""("APP" = 'a' OR "APP" = 'b') AND "ENV" = 'P' AND "KIND" IN ('x')""", None)
    assert G.fixed_fields(cond) == {"ENV", "KIND"}                         # two alternatives do not pin a field
    with app.app_context(), acting_as("admin"):
        loose = compare_groups(table="runs", start="2026-09-22 00:00", end="2026-09-23 00:50",
                               where="""APP = 'BILLING' OR APP = 'LEDGER' AND POSITION_LABEL = 'D-1'""")
        tight = compare_groups(table="runs", start="2026-09-22 00:00", end="2026-09-23 00:50",
                               where='''"APP" IN ('BILLING', 'LEDGER') AND "POSITION_LABEL" = 'D-1' ''')
    assert "error" not in loose and "error" not in tight, (loose, tight)
    assert loose["conclusion"] == tight["conclusion"] and loose["stages"] == tight["stages"]
    assert "APP" in loose["fields_compared"]


def test_a_condition_that_holds_only_in_progress_is_left_out(night, app):
    """A scope with a state that exists only while a row is in progress (`"STATUS" = 'QUEUED'`): no row of the
    earlier days has it, and the comparison had no day to compare with. The scope is read without it, and said;
    a scope that matches nothing now is said as such; a subquery is refused with the table's own fields."""
    from supagent.security import acting_as
    from supagent.tools import compare_groups

    night.con.execute("""UPDATE "runs" SET "STARTED" = NULL, "ENDED" = NULL, "NODE" = NULL, "STATUS" = 'QUEUED', "VERSION" = NULL,
                         "DURATION_S" = NULL, "ts" = "SCHED"
                         WHERE "POSITION_DATE" = '20260922' AND "REGION" = 'EU' AND "SCHED" >= TIMESTAMP '2026-09-23 00:20:00'""")
    with app.app_context(), acting_as("admin"):
        r = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50",
                           where='''"POSITION_LABEL" = 'D-1' AND "REGION" = 'EU' AND "STATUS" = 'QUEUED' ''')
        plain = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50",
                               where='''"POSITION_LABEL" = 'D-1' AND "REGION" = 'EU' ''')
        nothing = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50",
                                 where='''"POSITION_LABEL" = 'D-1' AND "REGION" = 'MARS' ''')
        nested = compare_groups(table="runs", start="2026-09-23 00:00", end="2026-09-23 00:50",
                                where='''"APP" IN (SELECT "TARGET" FROM "changes")''')
    assert "error" not in r, r
    assert r["conclusion"] == plain["conclusion"] and r["stages"] == plain["stages"]
    assert ("the condition \"STATUS\" = 'QUEUED' was left out: no row of the earlier days has it (a state that exists only "
            "while a row is in progress, or a value that is new)") in r["note"]
    assert "left out" not in plain.get("note", "")
    assert nothing["error"].startswith("no row matches this scope, in the window asked or in the")
    assert nested["error"].startswith("where: only conditions on this table's own fields (no subquery")


def test_what_the_rows_wait_on_is_looked_for_outside_the_scope_s_own_fields():
    """The waiting rows of the application the question asked about are concentrated on it and on one pool: what
    they wait on is the pool (a field the scope does not fix: outside the scope on the application are only its
    other rows; on the pool, who else holds it). Running longer at the last stage: the best field, as before."""
    def groups(*flagged):
        return {"groups": [{"value": v, "unusual": True} for v in flagged] + [{"value": "other", "unusual": False}]}

    summaries = {"APPLICATION": groups("PNL_APP"), "POOL": groups("GRID_EU"), "NODE": groups("n1", "n2", "n3")}
    fields = ["APPLICATION", "POOL", "NODE"]                                  # (the best first)
    assert G.waited_on(summaries, fields, {"APPLICATION", "ENV"}, True) == ("POOL", ["GRID_EU"])
    assert G.waited_on(summaries, fields, set(), True) == ("APPLICATION", ["PNL_APP"])          # no scope: the best
    assert G.waited_on(summaries, fields, {"APPLICATION"}, False) == ("APPLICATION", ["PNL_APP"])  # the last stage
    assert G.waited_on(summaries, ["APPLICATION", "NODE"], {"APPLICATION"}, True) == ("APPLICATION", ["PNL_APP"])
    assert G.waited_on(summaries, ["NODE"], set(), True) is None                                # three values: none
