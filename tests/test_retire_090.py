"""0.9: parts that look retired are proposed to an admin, never retired by the learning. A value read in the data
that its category's fields and labels have not shown for two weeks (seen so by two checks on two days; never from
a list that may be cut, a failed query, or data that stopped as a whole); any value, the ones put by hand too,
that a text says was retired or decommissioned (the sentence is kept). Retire keeps the value, marked retired;
Keep declines that reason for good."""

from __future__ import annotations

import datetime as dt
import types
from contextlib import contextmanager

import pytest
from test_brief import system  # noqa: F401  (Billing = Invoicing + Payments, grid-a with srv-1 and srv-2, ledger db)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401


@pytest.fixture()
def parts(system):  # noqa: F811
    from superset.extensions import db

    from supagent.models import Doc, Facet

    f = {x.value: x for x in db.session.query(Facet)}
    f["srv-1"].source = f["srv-2"].source = "data"
    f["srv-1"].origins = ["field NODE of jobs (db)"]
    hand = Facet(facet="server", value="srv-hand", status="approved", source="admin", parents=[system["grid-a"]])
    db.session.add(hand)
    db.session.commit()
    yield {**system, "srv-hand": hand.id}
    db.session.rollback()
    db.session.query(Doc).delete()
    db.session.commit()


def _state(monkeypatch, retire, day, **cats):
    monkeypatch.setattr(retire, "_today", lambda: dt.date(2026, 10, day))
    monkeypatch.setattr(retire, "recent", lambda seconds=120.0: {
        c: {"values": set(v), "complete": True, "asked": 2} if isinstance(v, (set, list, tuple)) else v for c, v in cats.items()})


def test_a_value_gone_from_the_data_is_proposed_after_two_checks_never_one_put_by_hand(parts, app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import retire
    from supagent.models import Facet

    one, two, hand = parts["srv-1"], parts["srv-2"], parts["srv-hand"]
    _state(monkeypatch, retire, 1, server={"srv-1"})                     # srv-2 and srv-hand: not in the data
    out = retire.check()
    assert (out["absent"], out["proposed"], out["categories_checked"]) == (1, 0, 1)
    assert db.session.get(Facet, two).suggested["absent"] == {"since": "2026-10-01", "checks": 1, "last": "2026-10-01"}
    assert retire.check()["absent"] == 0 and db.session.get(Facet, two).suggested["absent"]["checks"] == 1   # the same day
    _state(monkeypatch, retire, 2, server={"srv-1"})
    out = retire.check()
    assert out["proposed"] == 1
    r = db.session.get(Facet, two).suggested["retire"]
    assert r["kind"] == "absent" and "not in the data of the last 14 days" in r["why"] and "2026-10-01" in r["why"]
    assert not (db.session.get(Facet, hand).suggested or {}).get("absent")            # put by hand: never from an absence
    assert db.session.get(Facet, two).status == "approved"                           # proposed: nothing retired
    listed, n = retire.waiting()
    assert n == 1 and listed[0]["value"] == "srv-2" and listed[0]["source"] == "data"
    with _client(app, "admin") as c:
        d = c.get("/supagent/admin/api/review").get_json()
        assert d["counts"]["retire"] == 1 and d["retire"][0]["id"] == two and d["waiting"] >= 1
        assert c.post(f"/supagent/admin/api/facets/{two}", json={"retire": False}).get_json()["status"] == "approved"
    _state(monkeypatch, retire, 3, server={"srv-1"})
    assert retire.check()["proposed"] == 0 and retire.waiting()[1] == 0            # kept: not proposed again
    _state(monkeypatch, retire, 4, server={"srv-1", "srv-2"})                      # seen again
    assert retire.check()["seen_again"] == 1 and not db.session.get(Facet, two).suggested
    _state(monkeypatch, retire, 5, server={"srv-1"})
    retire.check()
    _state(monkeypatch, retire, 6, server={"srv-1"})
    assert retire.check()["proposed"] == 1                                         # another absence: proposed again
    with _client(app, "admin") as c:
        assert c.post(f"/supagent/admin/api/facets/{two}", json={"retire": True}).get_json()["status"] == "rejected"
    with app.app_context():
        f = db.session.get(Facet, two)
        assert f.status == "rejected" and f.reviewed_by == "admin" and not (f.suggested or {}).get("retire")
        f.status, f.suggested = "approved", None
        db.session.commit()


def test_nothing_is_concluded_from_a_cut_list_or_from_data_that_stopped(parts, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import retire
    from supagent.models import Facet

    _state(monkeypatch, retire, 1, server={"values": {"srv-1"}, "complete": False, "asked": 2})
    out = retire.check()
    assert out["absent"] == 0 and "could not be read" in out["not_checked"]["server"]
    _state(monkeypatch, retire, 2, server=set())                                   # nothing at all: the data is late
    out = retire.check()
    assert out["absent"] == 0 and "the data is late" in out["not_checked"]["server"]
    assert not (db.session.get(Facet, parts["srv-2"]).suggested or {}).get("absent")
    monkeypatch.setattr(retire, "days", lambda: 0)                                 # the check turned off
    monkeypatch.undo()


def test_a_text_that_says_a_part_was_retired_proposes_it_with_its_sentence(parts, app, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import retire
    from supagent.knowledge.brief import _graph
    from supagent.knowledge.freshness import touch
    from supagent.models import Doc, Facet

    touch()
    db.session.commit()
    g = _graph()
    name = lambda ids: sorted(g["values"][i]["name"] for i in ids)   # noqa: E731
    assert name(retire.said_in("srv-hand was decommissioned in March.", g)) == ["srv-hand"]
    assert name(retire.said_in("srv-1 replaced the retired srv-2 last year.", g)) == ["srv-2"]
    assert name(retire.said_in("Decommissioned: srv-1, srv-2 and srv-hand.", g)) == ["srv-1", "srv-2", "srv-hand"]
    assert name(retire.said_in("Payments runs on grid-a since srv-2 has been taken out of service.", g)) == ["srv-2"]
    assert retire.said_in("We never retired anything this year.", g) == []
    assert retire.said_in("srv-1 is the busiest server of grid-a.", g) == []
    db.session.add(Doc(kind="upload", title="Infra notes", enabled=True, status="ok",
                       content="The pool grid-a was renewed.\n\nsrv-hand was decommissioned in March 2026. srv-1 stays."))
    db.session.commit()
    _state(monkeypatch, retire, 1)
    out = retire.check()
    assert (out["said"], out["proposed"]) == (1, 1)
    hand = db.session.get(Facet, parts["srv-hand"])
    r = hand.suggested["retire"]
    assert r["kind"] == "said" and r["evidence"] == "srv-hand was decommissioned in March 2026. (Infra notes)"
    assert hand.status == "approved" and retire.check()["said"] == 0                # once
    with _client(app, "admin") as c:
        card = c.get("/supagent/admin/api/review").get_json()["retire"][0]
        assert card["source"] == "admin" and card["kind"] == "said" and "decommissioned in March" in card["evidence"]
        c.post(f"/supagent/admin/api/facets/{parts['srv-hand']}", json={"retire": False})
    assert retire.check()["said"] == 0                                             # kept: that sentence, never again
    db.session.add(Doc(kind="upload", title="Change log", enabled=True, status="ok",
                       content="2026-09-30: the server srv-hand is out of service, removed from the racks."))
    db.session.commit()
    assert retire.check()["said"] == 1                                             # another sentence: proposed
    db.session.get(Facet, parts["srv-hand"]).suggested = None
    db.session.commit()


def test_a_code_that_names_nothing_is_proposed_not_retired(parts, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import retire
    from supagent.models import Facet

    z = Facet(facet="application", value="Z", status="approved", source="data")
    db.session.add(z)
    db.session.commit()
    _state(monkeypatch, retire, 1)
    out = retire.check()
    assert out["proposed"] == 1 and db.session.get(Facet, z.id).status == "approved"
    assert db.session.get(Facet, z.id).suggested["retire"]["kind"] == "unnamed"
    retire.decide(db.session.get(Facet, z.id), False, "admin")
    db.session.commit()
    assert retire.check()["proposed"] == 0
    db.session.delete(db.session.get(Facet, z.id))
    db.session.commit()


def test_the_data_is_asked_for_the_period_each_field_and_label_once(parts, monkeypatch):
    """Where a category is looked up, and what is asked: a field's values over the period (its index's time field),
    a label's values on any metric over the period; no time field, a failure or a cut list: not told."""
    from supagent import settings, tools
    from supagent.knowledge import retire

    monkeypatch.setattr(tools, "_catalog", lambda: {"indices": {"jobs": {"time_field": "ts"}}})
    where = retire.places()["server"]
    fields = [p for p in where if p["kind"] == "field"]
    labels = [p for p in where if p["kind"] == "label"]
    assert fields and all(p["name"] == "NODE" for p in fields) and next(p for p in fields if p["index"] == "jobs")["time"] == "ts"
    assert len(labels) == len({(p["source"], p["name"]) for p in labels})          # a label once per metrics database
    asked = {}

    class Cur:
        def execute(self, sql):
            asked["sql"] = sql

        def fetchall(self):
            return [("srv-1", 12), ("SRV-2", 3), (None, 1)]

    @contextmanager
    def sql_conn(database, extract, max_rows=0):
        yield types.SimpleNamespace(cursor=lambda: Cur(), tz=None)

    monkeypatch.setattr(tools, "_db_connection", sql_conn)
    settings.set_value("agent.now", "2026-10-02 12:00")
    try:
        got = retire._recent_values(next(p for p in fields if p["index"] == "jobs"), 14)
        assert got == {"srv-1", "srv-2"}
        assert asked["sql"] == ('SELECT "NODE" AS v, COUNT(*) AS n FROM "jobs" WHERE "ts" >= TIMESTAMP \'2026-09-18 12:00:00\' '
                                'GROUP BY "NODE" LIMIT 20001')
        assert retire._recent_values({**fields[0], "time": None}, 14) is None       # no time field: not told
        calls = []
        client = types.SimpleNamespace(label_values=lambda name, match, start, end, limit=None: calls.append(
            (name, match, end - start, limit)) or ["srv-1", "srv-9"])
        zone = types.SimpleNamespace(utc_ms=lambda d: int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000))
        monkeypatch.setattr(tools, "_promagg_connection", lambda database: types.SimpleNamespace(
            client=client, zone=zone, close=lambda: None))
        assert retire._recent_values(labels[0], 14) == {"srv-1", "srv-9"}
        assert calls == [(labels[0]["name"], None, 14 * 86_400_000, 20001)]
        boom = types.SimpleNamespace(label_values=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        monkeypatch.setattr(tools, "_promagg_connection", lambda database: types.SimpleNamespace(
            client=boom, zone=zone, close=lambda: None))
        assert retire._recent_values(labels[0], 14) is None                         # a failure: not told
    finally:
        settings.set_value("agent.now", None)
