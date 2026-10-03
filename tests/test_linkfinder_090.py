"""0.9: the interactions the logs show (knowledge.linkfinder), proposed for the System map: a line that names other
parts ties the part it comes from (its application) to them, the kind read in its pattern's words; a pair seen on
enough lines and days waits in To review with its evidence; what is drawn, proposed or rejected already, what one
is part of, and what the map's own interactions rule out are not proposed."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest
from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, grid-a, ledger db)
from test_groups import _Conn
from test_knowledge import world  # noqa: F401

from supagent.knowledge import linkfinder as F


def test_the_kind_is_in_the_words_and_a_name_is_the_part_it_starts_with():
    assert F.kind_of("<name> still waiting for its inputs after # min: <name>") == "depends_on"
    assert F.kind_of("request to <name> took # ms") == "calls"
    assert F.kind_of("<name> answered in # ms (# requests pending)") == "calls"
    assert F.kind_of("loading positions from <name>") == "reads_from"
    assert F.kind_of("commit to <name> took # s") == "sends_to"
    assert F.kind_of("GC overhead on <name>: heap # used") is None          # a state, no interaction
    assert F.kind_of("no free slot on <name> for <name>") is None
    names = {"app_a": [1], "app_a.core": [2], "md_eod_rates": [3]}
    assert F.resolve("APP_A.CORE.EU.02", names) == 2                       # the longest part it starts with
    assert F.resolve("APP_A.RATES.EU", names) == 1 and F.resolve("MD_EOD_RATES", names) == 3
    assert F.resolve("APP_AB.X", names) is None and F.resolve("", names) is None


@pytest.fixture()
def logs(system, world, app, monkeypatch):  # noqa: F811
    """Three days of the Billing applications' logs, as their scheduler and their clients write them."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("osagg")
    from superset.extensions import db

    from supagent import settings
    from supagent import tools as T
    from supagent.knowledge.store import upsert
    from supagent.models import Link

    settings.set_value("categories.fields", {"server": "^(NODE|node)$", "application": "^APP$"})
    for name, kind, stats in (("APP", "keyword", {"cardinality": 2}), ("NODE", "keyword", {"cardinality": 2}),
                              ("MESSAGE", "text", {"cardinality": 50000})):
        upsert(world["run"], world["s_jobs"], "field", "app_logs", name, {"data_type": kind, "stats": stats})
    upsert(world["run"], world["s_jobs"], "index", "", "app_logs", {"stats": {"docs": 500, "time_field": "ts"}})
    pay = db.session.query(Link).filter(Link.kind == "calls").one().a_ref
    ledger = db.session.query(Link).filter(Link.kind == "calls").one().b_ref
    db.session.add(Link(a_ref=pay, b_ref=ledger, kind="sends_to", source="llm", status="rejected"))
    db.session.commit()
    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE "app_logs" ("ts" TIMESTAMP, "APP" VARCHAR, "NODE" VARCHAR, "MESSAGE" VARCHAR)')
    rows = []
    for back in range(3):
        day = dt.datetime(2026, 9, 23) - dt.timedelta(days=back)
        for i in range(6):
            t = day + dt.timedelta(minutes=10 * i)
            rows += [(t, "Invoicing", "srv-1", f"Invoicing.EU.0{i} still waiting for its inputs after 20 min: Payments.EU"),
                     (t, "Invoicing", "srv-1", f"request to LEDGERDB from grid-a took {800 + i} ms"),
                     (t, "Payments", "srv-2", f"commit to LEDGERDB took {i}.2 s"),
                     (t, "Payments", "srv-2", f"GC overhead on srv-2: heap 9{i}% used")]
        if back < 2:
            rows += [(day + dt.timedelta(hours=2), "Payments", "srv-2", "loading the prices from LEDGERDB")] * 2
    con.executemany('INSERT INTO "app_logs" VALUES (?, ?, ?, ?)', rows)
    conn = _Conn(con, dt.datetime(2026, 9, 23, 4, 0))

    @contextmanager
    def fake(database, extract, max_rows=0):
        yield conn

    monkeypatch.setattr(T, "_db_connection", fake)
    monkeypatch.setattr(T, "_catalog", lambda: {"indices": {"app_logs": {"time_field": "ts"}}})
    settings.set_value("agent.now", "2026-09-23 23:00")
    yield {"pay": pay, "ledger": ledger, "conn": conn}
    settings.set_value("agent.now", None)
    settings.set_value("categories.fields", None)


def test_the_interactions_the_logs_show_are_proposed_with_their_evidence(logs, app):
    from superset.extensions import db

    from supagent.knowledge import sysmap
    from supagent.models import Facet, Link

    with app.app_context():
        tables = F.log_tables()
        assert [(t["table"], t["message"], t["owners"]) for t in tables] == [
            ("app_logs", "MESSAGE", {"APP": "application", "NODE": "server"})]
        out = F.run()
    assert out["tables"] == 1 and out["lines"] == 74 and out["proposed"] == 1, out     # (a line written twice: once)
    x = db.session.query(Link).filter(Link.status == "proposed").one()
    inv = db.session.query(Facet).filter(Facet.value == "Invoicing").one()
    assert (x.a_ref, x.kind, x.b_ref, x.source) == (f"facet:{inv.id}", "calls", logs["ledger"], "logs")
    assert x.evidence.startswith('18 lines of app_logs on 3 of the last 14 days, such as "request to LEDGERDB from grid-a')
    # not proposed: drawn already (Invoicing waits for Payments), rejected before (Payments writes to the ledger),
    # too few lines (Payments reads from it: 2, on 2 days), no interaction in the words (GC overhead), what the
    # map's own kinds rule out (Invoicing "calls" grid-a, a pool: the map's calls go to services), the servers
    # (NODE: the applications act in the map, the line comes from its application)
    assert db.session.query(Link).filter(Link.kind == "depends_on").count() == 1
    assert not any(k["kind"] == "calls" and k["b"] == inv.id for k in sysmap.interactions())   # waiting: not drawn
    assert F.run()["proposed"] == 0                                           # proposed already: once
    # with the classification: a step of its own
    from supagent import settings
    from supagent.knowledge import learner

    assert settings.get("learn.interactions_logs") is True


def test_the_lines_of_a_table_that_cannot_be_read_leave_the_others(logs, app, monkeypatch):
    from supagent import tools as T

    @contextmanager
    def broken(database, extract, max_rows=0):
        raise RuntimeError("the cluster is down")
        yield None                                               # pragma: no cover

    monkeypatch.setattr(T, "_db_connection", broken)
    with app.app_context():
        out = F.run()
    assert out["tables"] == 0 and out["not_read"] == ["app_logs"] and out["proposed"] == 0


def test_the_step_keeps_to_its_time_query_by_query(logs, app, monkeypatch):
    """0.9.1: the time of the step (learn.interactions_logs_seconds) is checked before each query: on a big or
    shared cluster the queries left are not sent (the next night goes on), and the result says so, with the
    queries it sent."""
    import time as clock

    b = F.Budget(0)
    assert not b.left() and b.cut and b.queries == 0
    b = F.Budget(60)
    assert b.left() and b.left() and b.queries == 2 and not b.cut
    with app.app_context():
        nothing = F.run(seconds=0)
        full = F.run(propose=False)
    assert nothing["cut"].startswith("the step's time (0 s) ran out") and nothing["queries"] == 0 and nothing["proposed"] == 0
    assert "cut" not in full and full["queries"] > 24 and full["seen"]
    # cut in the middle of a table: what was read is used, the rest not sent
    real, calls = clock.time, {"n": 0}

    def later():
        calls["n"] += 1
        return real() + (3600 if calls["n"] > 40 else 0)
    monkeypatch.setattr(F.time, "time", later)
    with app.app_context():
        part = F.run(seconds=60, propose=False)
    monkeypatch.setattr(F.time, "time", real)
    assert part["cut"] and 0 < part["queries"] < full["queries"]
