"""0.9: compare_logs, what the logs say that they do not usually. The lines of a log table grouped into patterns
(numbers, times, ids, names and quoted values left out; a list of names is one), each counted against the same
window of the earlier days that have data: the new patterns, the ones far above usual and the rare ones first,
with where their lines come from (the fields of few values, the names they hold) and when; a pattern as frequent
as usual whose numbers are far from their usual (a slow write of 40 s every night 8 s); what is gone; the patterns
of every night said as usual; reference days. The patterns said are counted line by line in the database. Nothing
here knows a kind of log."""

from __future__ import annotations

import datetime as dt
import json
import random
from contextlib import contextmanager

import pytest
from test_groups import _Conn
from test_knowledge import world  # noqa: F401  (the fixture)

from supagent.knowledge import logs as L


# --- patterns ---------------------------------------------------------------------------------------------------

def test_a_pattern_leaves_out_what_changes_from_one_line_to_the_next():
    a = L.shape("GC overhead on srv-105: heap 97% used, full GC took 14.2 s")
    b = L.shape("GC overhead on node107: heap 95% used, full GC took 3.9 s")
    assert a.key == b.key == "GC overhead on <name>: heap # used, full GC took # s"
    assert a.wording == "GC overhead on srv-105: heap # used, full GC took # s"     # the names kept
    assert (a.names, a.numbers) == (["srv-105"], [97.0, 14.2])                     # (105 is the server's name)
    # a list of names is one name, however many; a line cut in the middle of its list is the same pattern
    waits = [L.shape(m) for m in (
        "APP_A.RATES.EMEA.01 still waiting for its inputs after 20 min: FEED_X, APP_B.RATES.EMEA",
        "APP_C.FX.APAC.17 still waiting for its inputs after 40 min: FEED_Y, APP_D.FX and APP_E.FX.APAC.02",
        "APP_C.FX.APAC.17 still waiting for its inputs after 60 min: MD_EOD_RATES",
        "ATTRIB_APP.RATES.EMEA.01 still waiting for its inputs after 40 min: POSITIONS_APP.RATES.EMEA, PNLAPP",
        "ATTRIB_APP.RATES.EMEA.01 still waiting for its inputs after 40 min: POSITIONS_APP.RATES.EMEA, RISK_")]
    assert {w.key for w in waits} == {"<name> still waiting for its inputs after # min: <name>"}
    assert waits[1].names == ["APP_C.FX.APAC.17", "FEED_Y, APP_D.FX, APP_E.FX.APAC.02"]
    assert waits[3].names[1] == "POSITIONS_APP.RATES.EMEA, PNLAPP"
    # times, ids, quoted values; a number with its unit; words joined by dashes and paths are words
    assert L.shape("at 2026-09-23T01:22:13+02:00 request 0x7f3a9c12ab: done.").key == "at # request #: done"
    q = L.shape("configuration key 'pricing.legacy_mode' is deprecated")
    assert (q.key, q.names) == ("configuration key '<value>' is deprecated", ["pricing.legacy_mode"])
    k = L.shape("container killed by the out-of-memory killer after 120ms (exit 137) on srv-3")
    assert k.key == "container killed by the out-of-memory killer after #ms (exit #) on <name>" and k.numbers == [120.0, 137.0]
    assert L.shape("slow I/O on /nas/risk/positions: 41.5 s for 380 MB").key == "slow I/O on /nas/risk/positions: # s for # MB"
    assert L.shape("copied A_1, B_2 and C_3 to /data/out").names == ["A_1, B_2, C_3"]
    # a line of names only keeps them: two exceptions are not one pattern
    assert L.shape("java.lang.NullPointerException").key == "java.lang.NullPointerException"
    assert L.shape("java.lang.OutOfMemoryError: Java heap space").key == "<name>: Java heap space"
    assert L.shape("10.0.0.12 unreachable").key == "# unreachable"
    # the piece every line of a pattern holds, to count them in the database
    assert L.fragment("<name> still waiting for its inputs after # min: <name>") == "still waiting for its inputs after"
    assert L.fragment("<name>: # of #") is None                          # nothing long enough to count it by
    assert L.fragment("no <name> token free: waiting") == "token free: waiting"


def _rows(n, msg, host="srv-1", level="WARN", minute=0):
    return [{"MSG": msg(i), "HOST": host if isinstance(host, str) else host(i), "LEVEL": level,
             "_t": dt.datetime(2026, 9, 23, 1, (minute + i) % 60)} for i in range(n)]


DAYS = 8
DATES = [f"2026-09-{d:02d}" for d in (22, 21, 18, 17, 16, 15, 14, 11)]


def _survey(now, earlier, level="WARN"):
    samples = [now] + earlier
    found = L.read(samples, [float(len(s)) for s in samples], "MSG", ["HOST"], level=level)
    return found, L.survey(found, DAYS, ["HOST"], DATES)


def test_new_more_rare_numbers_gone_and_every_day_are_told_apart():
    every = lambda i: f"full GC pause of {1 + i % 3}.2 s"                                            # noqa: E731
    slow = lambda k: (lambda i: f"slow write to /data: {k * (2 + i % 3):.1f} s for {100 + i} MB")    # noqa: E731
    queue = lambda i: f"queue of {30 + i % 7} runs on POOL_A for job_{i}"                            # noqa: E731
    now = (_rows(30, every, "srv-old") + _rows(20, slow(9.0)) + _rows(60, queue)
           + _rows(25, lambda i: "snapshot of /vol/risk took 40 s", host="nas-1"))
    earlier = []
    for d in range(DAYS):
        day = (_rows(31 - d % 3, every, "srv-old") + _rows(20, slow(1.0)) + _rows(20, queue)
               + _rows(15, lambda i: "cache warmed in 4 s") + _rows(6 if d % 2 else 0, lambda i: "late feed MD_X"))
        if d == 4:                                                       # a weekly line: there a week ago
            day += _rows(24, lambda i: "snapshot of /vol/risk took 6 s", host="nas-1")
        earlier.append(day)
    found, res = _survey(now, earlier)
    found.update(L.read([_rows(12, lambda i: "java.lang.OutOfMemoryError: Java heap space",
                                host=lambda i: f"srv-{5 + i % 2}")] + [[] for _ in range(DAYS)],
                         [12.0] + [0.0] * DAYS, "MSG", ["HOST"], level="ERROR"))
    res = L.survey(found, DAYS, ["HOST"], DATES)
    verdicts = {p["pattern"]: p["verdict"] for p in res["patterns"]}
    assert verdicts["java.lang.OutOfMemoryError: Java heap space"] == "new"         # 12 errors, none before
    assert verdicts["queue of # runs on POOL_A for <name>"] == "above usual"        # 60 against 20 (POOL_A: every line)
    assert verdicts["slow write to /data: # s for # MB"] == "numbers above usual"   # as many, nine times longer
    assert verdicts["snapshot of /vol/risk took # s"] == "rare"                     # there one day of eight
    assert verdicts["full GC pause of # s"] == "as usual"
    assert verdicts["cache warmed in # s"] == "gone"
    assert "late feed MD_X" not in str(res)            # there every other day, none now: not gone, said nowhere
    rare = next(p for p in res["patterns"] if p["verdict"] == "rare")
    assert rare["seen_on"] == {"2026-09-16": 24}
    text = res["conclusion"]
    # the errors first, then the most lines; as many lines with numbers far from usual after
    assert text.startswith('What the logs say that they do not usually (the errors first, then the most lines): (1) '
                           '"java.lang.OutOfMemoryError: Java heap space" (ERROR, 12 lines, none on the earlier days, '
                           'from 01:00 to 01:11): HOST: srv-5 (6), srv-6 (6). (2) "queue of # runs on POOL_A for <name>" '
                           '(WARN, 60 lines against 20 usually')
    assert ('(3) "snapshot of /vol/risk took # s" (WARN, 25 lines, there on only 1 of the earlier days (2026-09-16 with '
            '24)') in text
    assert '(4) "slow write to /data: # s for # MB" (WARN, 20 lines, as many as usually' in text
    assert 'its number "/data: [#] s for #" is 27 against 3 usually (x9)' in text
    assert L.number_label("no free slot on <name> for <name> after # min (# runs waiting, # of # slots taken)", 3) == \
        "waiting, [#] of # slots"
    assert L.number_label("no free slot on <name> after # min (# runs waiting)", 2) == "min ([#] runs waiting)"
    assert L.number_label("at #:#", 2) == "at #:[#]" and L.number_label("nothing", 1) == "#1"
    assert 'Gone (there every day, none now): "cache warmed in # s" (15 usually)' in text
    assert 'As every day: "full GC pause of # s" (30 lines)' in text


def test_how_many_lines_make_a_finding():
    nothing = [[] for _ in range(DAYS)]
    # an error counts from two lines, a warning from five
    _f, two_errors = _survey(_rows(2, lambda i: "disk full on /data", level="ERROR"), nothing, level="ERROR")
    assert two_errors["patterns"][0]["verdict"] == "new"
    _f, two_warnings = _survey(_rows(2, lambda i: "disk almost full on /data"), nothing)
    assert two_warnings["new_or_more"] == 0 and two_warnings["conclusion"].startswith("Nothing new in the logs")
    # a table without a level: an error said in its words is an error
    found = L.read([_rows(3, lambda i: "Connection refused by db-2")] + nothing, [3.0] + [0.0] * DAYS, "MSG", ["HOST"])
    assert next(iter(found.values()))["level"] == "ERROR" and L.judge(next(iter(found.values())), DAYS)["verdict"] == "new"
    # what was read stands for more lines: scaled
    scaled = L.read([_rows(10, lambda i: "x happened")], [100.0], "MSG", [])
    assert next(iter(scaled.values()))["counts"] == [100.0]
    # counts that differ day to day: twice the usual of a pattern of 4 lines is no finding, of 400 it is
    small = {"counts": [9.0] + [4.0] * DAYS, "level": "WARN"}
    big = {"counts": [900.0] + [400.0] * DAYS, "level": "WARN"}
    assert L.judge(small, DAYS)["verdict"] == "as usual" and L.judge(big, DAYS)["verdict"] == "above usual"
    # gone: there on most of the earlier days; below usual: far under
    assert L.judge({"counts": [0.0] + [20.0] * 5 + [0.0] * 3, "level": "WARN"}, DAYS)["verdict"] == "below usual"
    assert L.judge({"counts": [0.0] + [20.0] * 6 + [0.0] * 2, "level": "WARN"}, DAYS)["verdict"] == "gone"
    assert L.judge({"counts": [40.0] + [200.0] * DAYS, "level": "WARN"}, DAYS)["verdict"] == "below usual"

    # numbers: known from half the days and more, twice their usual; from fewer, 2.5 times
    def numbered(now, usual, days_with):
        values = {0: [[now] * 6] + [[usual] * 4 if d < days_with else [] for d in range(DAYS)]}
        return {"counts": [6.0] + [4.0 if d < days_with else 0.0 for d in range(DAYS)], "level": "WARN", "values": values}
    assert L.numbers_moved(numbered(20, 9, 8), DAYS)[0]["ratio"] == pytest.approx(2.22, abs=0.01)
    assert L.numbers_moved(numbered(20, 9, 2), DAYS) == []                         # x2.2 known from two days only
    assert L.numbers_moved(numbered(30, 9, 2), DAYS)[0]["days"] == 2
    assert L.judge(numbered(3, 9, 8), DAYS)["verdict"] == "numbers below usual"


def test_a_pattern_is_said_with_the_names_most_of_its_lines_hold():
    lines = [f"no free slot on GRID_A for JOB_{i} after 30 min" for i in range(9)] + ["no free slot on GRID_B for JOB_X after 30 min"]
    found = L.read([[{"MSG": m, "_t": dt.datetime(2026, 9, 23, 1, i)} for i, m in enumerate(lines)]], [10.0], "MSG", [])
    key, p = next(iter(found.items()))
    assert key == "no free slot on <name> for <name> after # min"
    assert L.said(key, p) == "no free slot on GRID_A for <name> after # min"        # 9 of 10: GRID_A
    assert L.where(p, []) == []                                                     # (the jobs: spread, says nothing)
    two = L.read([[{"MSG": f"no free slot on {pool} for J_{i} after 30 min", "_t": None}
                   for i, pool in enumerate(["GRID_A"] * 5 + ["GRID_B"] * 5)]], [10.0], "MSG", [])
    k2, p2 = next(iter(two.items()))
    assert L.said(k2, p2) == "no free slot on <name> for <name> after # min"
    assert L.where(p2, []) == ["name #1: GRID_A (5), GRID_B (5)"]
    # a name place whose values are those of a field already said (the pool in the line and in a field): once
    for i, pool in enumerate(["GRID_A"] * 5 + ["GRID_B"] * 5):
        p2["where"]["POOL"][pool] += 1
    p2["where"]["POOL"]["GRID_C"] += 3
    assert L.where(p2, ["POOL"]) == ["POOL: GRID_A (5), GRID_B (5), GRID_C (3)"]
    # a pattern of two levels: counted together, said at its worst
    into = L.read([_rows(5, lambda i: "lost the connection to db-1")], [5.0], "MSG", ["HOST"], level="WARN")
    L.merge(into, L.read([_rows(3, lambda i: "lost the connection to db-1")], [3.0], "MSG", ["HOST"], level="ERROR"))
    merged = into["lost the connection to <name>"]
    assert merged["counts"] == [8.0] and merged["level"] == "ERROR" and merged["read"] == 8


# --- the tool, on a database ------------------------------------------------------------------------------------

TONIGHT = dt.date(2026, 9, 23)


@pytest.fixture()
def logs(world, monkeypatch):  # noqa: F811
    """Three weeks of nightly logs (weekdays; asked on Wednesday 2026-09-23 at 04:00, the window 00:00-04:00):
    every night the runs write their starts, an old server its GC pauses, the scheduler its waits for a slot of
    POOL_A, srv-1 its cache warm-up; on Wednesdays the storage writes its weekly snapshot. Tonight two servers run
    out of memory (new warnings and errors), the waits for a slot are three times as long, the snapshot is five
    times slower, the cache warm-up is gone, and a burst of retries at the end of the window crowds what is read
    of its last third. A second table has no level field."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from supagent import settings
    from supagent import tools as T

    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "app_logs" ("ts" TIMESTAMP, "LEVEL" VARCHAR, "HOST" VARCHAR, "APP" VARCHAR, '
                '"LOGGER" VARCHAR, "POSITION_DATE" VARCHAR, "POSITION_LABEL" VARCHAR, "MESSAGE" VARCHAR)')
    con.execute('CREATE TABLE "plain_logs" ("ts" TIMESTAMP, "HOST" VARCHAR, "MESSAGE" VARCHAR)')
    day, rows, plain = TONIGHT, [], []
    while day >= dt.date(2026, 9, 1):
        if day.weekday() < 5:
            r = random.Random(str(day))
            pos = f"{day - dt.timedelta(days=3 if day.weekday() == 0 else 1):%Y%m%d}"
            label = "D-1" if day == TONIGHT else "old"
            tonight = day == TONIGHT
            midnight = dt.datetime.combine(day, dt.time(0, 0))
            for i in range(120):
                t = midnight + dt.timedelta(minutes=2 * i)
                app = ("BILLING", "LEDGER", "ORDERS")[i % 3]
                host = f"srv-{1 + i % 6}"
                rows.append((t, "INFO", host, app, "scheduler", pos, label, f"{app}.{i:03d} started on {host} (attempt 1)"))
                if i % 4 == 0:
                    rows.append((t + dt.timedelta(seconds=30), "WARN", "srv-6", app, "jvm", pos, label,
                                 f"full GC pause of {r.uniform(1, 2.5):.1f} s"))
                if i % 5 == 0:
                    waited = 90 if tonight else 30
                    rows.append((t + dt.timedelta(seconds=40), "WARN", "pool-a-scheduler", app, "scheduler", pos, label,
                                 f"no free slot on POOL_A for {app}.{i:03d} after {waited} min ({r.randint(20, 30)} runs waiting)"))
                if i % 12 == 0 and not tonight:
                    rows.append((t + dt.timedelta(seconds=45), "INFO" if i % 24 else "WARN", "srv-1", app, "cache", pos,
                                 label, f"cache warmed in {r.randint(3, 6)} s"))
                if day.weekday() == 2 and i % 6 == 0:
                    took = r.uniform(25, 35) if tonight else r.uniform(4, 7)
                    rows.append((t + dt.timedelta(seconds=20), "WARN", "nas-1", app, "storage", pos, label,
                                 f"weekly snapshot of /vol/risk: {took:.1f} s for {r.randint(200, 900)} MB"))
                if tonight and host in ("srv-2", "srv-3") and i >= 30:
                    rows.append((t + dt.timedelta(seconds=50), "WARN", host, app, "jvm", pos, label,
                                 f"GC overhead on {host}: heap {r.randint(94, 99)}% used, full GC took {r.uniform(4, 19):.1f} s"))
                    if i % 4 == 1:
                        rows.append((t + dt.timedelta(seconds=55), "ERROR", host, app, "jvm", pos, label,
                                     "java.lang.OutOfMemoryError: Java heap space"))
                plain.append((t, host, f"{app}.{i:03d} done in {r.randint(50, 90)} s"))
            if tonight:
                for k in range(60):                   # 03:00-03:30: srv-5 retries every 30 s ...
                    rows.append((midnight + dt.timedelta(hours=3, seconds=30 * k), "WARN", "srv-5", "ORDERS",
                                 "db", pos, label, f"retrying the connection to db-1 (attempt {k + 1})"))
                for k in range(300):                  # ... 03:45-04:00: srv-4, every 3 s
                    rows.append((midnight + dt.timedelta(hours=3, minutes=45, seconds=3 * k), "WARN", "srv-4", "ORDERS",
                                 "db", pos, label, f"retrying the connection to db-1 (attempt {k + 1})"))
                for k in range(4):
                    plain.append((midnight + dt.timedelta(hours=2, minutes=k), "srv-2", "Connection refused by db-2: giving up"))
        day -= dt.timedelta(days=1)
    con.executemany('INSERT INTO "app_logs" VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
    con.executemany('INSERT INTO "plain_logs" VALUES (?, ?, ?)', plain)
    conn = _Conn(con, dt.datetime(2026, 9, 23, 4, 0))

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "_catalog", lambda: {"indices": {"app_logs": {"time_field": "ts"}, "plain_logs": {"time_field": "ts"}}})
    monkeypatch.setattr(T, "_table_database", lambda table, ref: world["jobs"])
    monkeypatch.setattr(T, "_group_fields", lambda table, fixed, label: [
        f for f in (("HOST", "APP", "LOGGER") if table == "app_logs" else ("HOST",)) if f not in fixed])
    monkeypatch.setattr(T, "_message_field", lambda table: "MESSAGE")
    monkeypatch.setattr(T, "_level_field", lambda table: "LEVEL" if table == "app_logs" else None)
    from superset.extensions import security_manager as sm

    monkeypatch.setattr(sm, "raise_for_access", lambda **kw: None)
    settings.set_value("agent.now", "2026-09-23 04:00")
    yield conn
    settings.set_value("agent.now", None)


def _exact(conn, like, day=TONIGHT, levels=("WARN", "ERROR")):
    a = dt.datetime.combine(day, dt.time(0, 0))
    return conn.con.execute(f"""SELECT COUNT(*) FROM "app_logs" WHERE "MESSAGE" LIKE '{like}' AND "ts" >= ? AND "ts" < ?
                                AND "LEVEL" IN ({", ".join(f"'{x}'" for x in levels)})""",
                            [a, a + dt.timedelta(hours=4)]).fetchone()[0]


def test_one_call_says_what_the_logs_say_that_they_do_not_usually(logs, app):
    from supagent.security import acting_as
    from supagent.tools import compare_logs

    with app.app_context(), acting_as("admin"):
        r = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00")
    assert "error" not in r, r
    assert r["compared_with"].startswith("the same window on 2026-09-22, 2026-09-21, 2026-09-18, 2026-09-17, 2026-09-16")
    assert r["read"] in ("the lines of the levels WARN, ERROR", "the lines of the levels ERROR, WARN")
    by = {p["pattern"]: p for p in r["patterns"]}
    # new: the retries (the most lines), the GC overhead of srv-2 and srv-3, the out-of-memory errors
    retry = by["retrying the connection to db-1 (attempt #)"]
    assert (retry["verdict"], retry["now"], retry["counted"]) == ("new", 360, "line by line")
    # what is read of the last third is srv-4's burst only: where the lines come from is counted, not read
    assert retry["where"][0] == "HOST: srv-4 (300), srv-5 (60)" and (retry["from"], retry["to"]) == \
        ("2026-09-23 03:00", "2026-09-23 03:59")
    overhead = by["GC overhead on <name>: heap # used, full GC took # s"]
    assert overhead["verdict"] == "new" and overhead["now"] == _exact(logs, "GC overhead on %") == 30
    # where its lines come from, the lines read scaled to the lines counted (its servers: not said twice)
    assert overhead["where"][0] in ("HOST: srv-2 (15), srv-3 (15)", "HOST: srv-3 (15), srv-2 (15)")
    assert not any(w.startswith("name #") for w in overhead["where"])
    oom = by["java.lang.OutOfMemoryError: Java heap space"]
    assert oom["verdict"] == "new" and oom["now"] == _exact(logs, "%Java heap space%")
    # as many waits as usual, three times as long; the weekly snapshot, five times slower
    waits = by["no free slot on POOL_A for <name> after # min (# runs waiting)"]
    assert waits["verdict"] == "numbers above usual" and waits["numbers"][0]["ratio"] == 3.0
    snap = by["weekly snapshot of /vol/risk: # s for # MB"]
    assert snap["verdict"] == "rare" and list(snap["seen_on"]) == ["2026-09-16", "2026-09-09"] or \
        list(snap["seen_on"]) == ["2026-09-16"]
    assert snap["numbers"][0]["ratio"] > 4
    # the GC pauses of the old server: as usual, though the retries crowd what is read of the last third (counted
    # line by line, they are as many as every night)
    gc = by["full GC pause of # s"]
    assert (gc["verdict"], gc["now"], gc["counted"]) == ("as usual", 30, "line by line")
    # gone: the cache warm-up of srv-1 (its warnings: the levels read)
    assert by["cache warmed in # s"]["verdict"] == "gone"
    text = r["conclusion"]
    assert text.startswith('What the logs say that they do not usually (the errors first, then the most lines): (1) '
                           '"java.lang.OutOfMemoryError: Java heap space" (ERROR, 7 lines, none on the earlier days, '
                           'from 01:14 to 03:38): HOST: srv-2 (7); APP: LEDGER (7); LOGGER: jvm (7). (2) "retrying the '
                           'connection to db-1 (attempt #)" (WARN, 360 lines, none on the earlier days, from 03:00 to '
                           '03:59): HOST: srv-4 (300), srv-5 (60)')
    assert "1 week ago" not in text                     # (the team's reference days: in the patterns, not said)
    assert r["levels"]["ERROR"]["usual"] == 0 and r["levels"]["WARN"]["now"] > 2 * r["levels"]["WARN"]["usual"]
    assert len(json.dumps(r, default=str)) < 6600


def test_scopes_business_dates_reference_days_and_levels(logs, app):
    from supagent.security import acting_as
    from supagent.tools import compare_logs

    with app.app_context(), acting_as("admin"):
        scoped = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00",
                              where='''"APP" = 'BILLING' AND "POSITION_LABEL" = 'D-1' ''')
        sql_scoped = list(logs.sql)
        logs.sql.clear()
        dated = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00",
                             where='''"POSITION_DATE" = '20260922' AND "HOST" IN ('srv-2', 'srv-3')''')
        sql_dated = list(logs.sql)
        week = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00", against=["1 week ago"])
        info = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00", levels=["INFO"])
        wrong_label = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00",
                                   where='''"POSITION_LABEL" = 'QUEUED' ''')
        nothing = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00", where='''"APP" = 'NOPE' ''')
        backwards = compare_logs(table="app_logs", start="2026-09-23 04:00", end="2026-09-23 00:00")
        bad_ref = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00", against=["last blue moon"])
    # the scope of one application, its business date moved to each earlier day's own
    assert "error" not in scoped, scoped
    assert any("BILLING" in q and "\"POSITION_DATE\" IN ('20260922')" in q for q in sql_scoped)
    assert any("BILLING" in q and "'20260921')" in q and "TIMESTAMP '2026-09-22 00:00:00'" in q for q in sql_scoped)
    assert not any("'20260922'" in q and "TIMESTAMP '2026-09-22 00:00:00'" in q for q in sql_scoped)
    waits = next(p for p in scoped["patterns"] if p["pattern"].startswith("no free slot on POOL_A"))
    assert waits["now"] == 8 and waits["counted"] == "line by line"              # BILLING's 8 of the 24
    # a business date written as a date is moved too: the servers' scope finds their new lines
    assert "error" not in dated, dated
    assert any("'20260921'" in q for q in sql_dated)
    assert [p["verdict"] for p in dated["patterns"] if p["pattern"].startswith("GC overhead on")] == ["new"]
    # a reference day: each pattern counted on it too
    assert week["against"] == ["1 week ago (2026-09-16)"]
    snap = next(p for p in week["patterns"] if p["pattern"].startswith("weekly snapshot"))
    assert snap["on"] == {"1 week ago (2026-09-16)": 20}
    assert "1 week ago (2026-09-16): 20" in week["conclusion"]
    # the quiet levels when asked
    assert info["read"] == "the lines of the levels INFO"
    assert "Gone (there every day, none now): \"cache warmed in # s\"" in info["conclusion"]
    assert wrong_label["error"].startswith('where: "POSITION_LABEL" = QUEUED: not a business-date label')
    assert nothing["error"].startswith("no earlier day with lines to compare with")
    assert backwards["error"] == "end must be after start"
    assert bad_ref["error"].startswith("against: not a reference day: 'last blue moon'")


def test_a_table_without_levels_a_big_table_and_the_fields_found(logs, app, monkeypatch, world):  # noqa: F811
    from supagent import tools as T
    from supagent.security import acting_as
    from supagent.tools import compare_logs

    from superset.extensions import db

    from supagent.knowledge.store import upsert

    for name, kind, stats in (("MESSAGE", "text", {"cardinality": 90000}), ("LEVEL", "keyword", {"cardinality": 4}),
                              ("HOST", "keyword", {"cardinality": 7}), ("LOGGER", "keyword", {"cardinality": 6})):
        upsert(world["run"], world["s_jobs"], "field", "app_logs", name, {"data_type": kind, "stats": stats})
    db.session.commit()
    with app.app_context(), acting_as("admin"):
        plain = compare_logs(table="plain_logs", start="2026-09-23 00:00", end="2026-09-23 04:00")
        monkeypatch.setattr(T, "LOG_SECONDS", 0.0)                 # a big table: no time to count line by line
        quick = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00")
    assert "error" not in plain, plain
    assert plain["read"] == "every line" and not plain["levels"]
    refused = next(p for p in plain["patterns"] if p["pattern"].startswith("Connection refused by"))
    assert refused["verdict"] == "new" and refused["level"] == "ERROR" and refused["now"] == 4   # (an error by its words)
    assert "stopped after 0 s" in quick["note"] and not any(p.get("counted") for p in quick["patterns"])
    # a message field the database cannot filter by a piece of text (an analysed text field): the estimates, said
    real = logs.cursor

    def no_like():
        cur = real()
        execute = cur.execute

        def run(sql):
            if " LIKE " in sql:
                raise RuntimeError("LIKE on an analysed text field")
            return execute(sql)
        cur.execute = run
        return cur
    monkeypatch.setattr(T, "LOG_SECONDS", 40.0)
    monkeypatch.setattr(logs, "cursor", no_like)
    with app.app_context(), acting_as("admin"):
        estimated = compare_logs(table="app_logs", start="2026-09-23 00:00", end="2026-09-23 04:00")
    monkeypatch.setattr(logs, "cursor", real)
    assert "error" not in estimated and estimated["patterns"] and not any(p.get("counted") for p in estimated["patterns"])
    assert "no counting line by line: the database cannot filter MESSAGE by a piece of text" in estimated["note"]
    # the message and level fields, found in the dictionary of the table
    monkeypatch.undo()
    with app.app_context():
        assert (T._message_field("app_logs"), T._level_field("app_logs")) == ("MESSAGE", "LEVEL")
        assert (T._message_field("jobs"), T._level_field("jobs")) == (None, None)


# --- the agent --------------------------------------------------------------------------------------------------

def test_the_agent_knows_the_logs_and_learns_them_in_its_paths(app, monkeypatch):
    from supagent import agent
    from supagent import tools as T
    from supagent.knowledge import brief, paths

    text = " ".join(agent.SECTIONS["investigation"].split())
    assert "and their logs (compare_logs: what they say that they do not usually)" in text
    assert len(agent.SECTIONS["investigation"]) < 4000
    assert {"compare_logs", "compare_groups"} <= agent.TOOLS_OF["investigation"] and "compare_logs" in agent.TOOLS_OF["status"]
    assert "compare_logs" in agent.QUERY_TOOLS           # (its figures come from the data)
    # the picture of a question says which of its tables hold lines of text, and their fields
    monkeypatch.setattr(T, "_message_field", lambda t: {"app_logs": "MESSAGE", "jobs": "ERROR_TEXT"}.get(t))
    monkeypatch.setattr(T, "_level_field", lambda t: "LEVEL" if t == "app_logs" else None)
    assert brief._log_tables({"jobs", "app_logs", "hosts"}) == ['app_logs (its text: "MESSAGE", its level: "LEVEL")',
                                                               'jobs (its text: "ERROR_TEXT")']
    # a path keeps the logs it read (the table, the fields of its scope)
    used = paths.used([{"tool": "compare_logs", "status": "done",
                        "args": {"table": "app_logs", "where": "\"HOST\" IN ('srv-2') AND \"APP\" = 'X'"}}])
    assert used["tables"]["app_logs"] == ["HOST", "APP"]
    assert paths._steps_text([{"tool": "compare_logs", "status": "done", "args": {"table": "app_logs",
                                                                               "where": "\"HOST\" = 'srv-2'"},
                               "result": json.dumps({"conclusion": "What the logs say that they do not usually"})}])
