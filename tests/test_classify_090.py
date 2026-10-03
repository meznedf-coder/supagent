"""0.9: the classification at the size of a platform. The values of the categories read from the data's fields are
read once each, whatever the number of metrics that carry the label, with where each comes from (which field of
which indices, which label of which metrics); such a category is not a list the LLM chooses in nor adds to; the
classification is a run of its own, listed in the settings with its steps and its LLM use."""

from __future__ import annotations

import json

import pytest
from test_decider import lab  # noqa: F401
from test_dictionary_080 import _client
from test_facets import classify_reply, world  # noqa: F401


@pytest.fixture()
def platform(world, monkeypatch):  # noqa: F811
    """Servers read from the label host of 40 metrics and from the field NODE of two indices; teams by hand."""
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Facet, KObject, Source

    conf = {"categories.custom": ["server", "team"],
            "categories.fields": {"application": r"^(application|app)$", "server": r"^(host|node)$"}}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    src = db.session.query(Source).first()
    hosts = [f"srv-{i:02d}" for i in range(30)]
    rows = []
    for m in range(40):
        rows.append(KObject(source_id=src.id, kind="metric", name=f"plat_metric_{m:02d}", parent=""))
        rows.append(KObject(source_id=src.id, kind="label", name="host", parent=f"plat_metric_{m:02d}", stats={"values": hosts}))
    for idx in ("plat-jobs", "plat-steps"):
        rows.append(KObject(source_id=src.id, kind="index", name=idx, parent=""))
        rows.append(KObject(source_id=src.id, kind="field", name="NODE", parent=idx, stats={"values": hosts[:3] + ["SRV-99"]}))
    rows.append(KObject(source_id=src.id, kind="field", name="NODE", parent="plat-huge", stats={"values": ["zz-1"], "partial": True}))
    db.session.add_all(rows)
    db.session.commit()
    yield {"db": src.database_name, "conf": conf}
    db.session.rollback()
    db.session.query(KObject).filter(KObject.name.like("plat%")).delete(synchronize_session=False)
    db.session.query(KObject).filter(KObject.parent.like("plat%")).delete(synchronize_session=False)
    db.session.query(Facet).filter(Facet.facet.in_(("server", "team"))).delete(synchronize_session=False)
    db.session.commit()


def test_each_value_is_read_once_and_says_where_it_comes_from(platform, monkeypatch):
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet

    by_hand = Facet(facet="server", value="srv-01", status="approved", source="admin", description="the first one",
                    origins=["added by admin", "field host of plat_metric_07 (old)"])
    retired = Facet(facet="server", value="srv-02", status="rejected", source="data", origins=["field NODE of old"])
    db.session.add_all([by_hand, retired])
    db.session.commit()
    asked = []
    real = F._ensure
    monkeypatch.setattr(F, "_ensure", lambda *a, **k: asked.append(a[:2]) or real(*a, **k))
    n = F.seed()
    import re

    mine = lambda rows: [f for f in rows if re.fullmatch(r"srv-\d\d", f.value.lower())]   # noqa: E731  (the fixture's own aside)
    assert n["server"] >= 31 and n["fields_read"] >= 42 and n["values_read"] >= 31        # 30 hosts and SRV-99, once each
    assert not [a for a in asked if a[0] == "server"]                                     # no query per value
    by = {f.value: f for f in db.session.query(Facet).filter(Facet.facet == "server")}
    assert len(mine(by.values())) == 31 and "zz-1" not in by                              # a partial list is not read
    db_name = platform["db"]
    assert by["srv-00"].origins == [
        f"field NODE of 2 indices such as plat-jobs, plat-steps ({db_name})",
        f"label host of 40 metrics such as plat_metric_00, plat_metric_01, plat_metric_02 ({db_name})"]
    assert by["srv-10"].origins == [f"label host of 40 metrics such as plat_metric_00, plat_metric_01, plat_metric_02 ({db_name})"]
    assert by["SRV-99"].origins == [f"field NODE of 2 indices such as plat-jobs, plat-steps ({db_name})"]
    assert (by["srv-00"].status, by["srv-00"].source) == ("proposed", "data")             # found: it waits (0.9)
    # a value put by hand keeps what was written and is told where it is in the data today
    hand = by["srv-01"]
    assert (hand.status, hand.source, hand.description) == ("approved", "admin", "the first one")
    assert hand.origins[0] == "added by admin" and hand.origins[1].startswith("field NODE of 2 indices")
    assert not any("(old)" in o for o in hand.origins)
    assert (by["srv-02"].status, by["srv-02"].origins) == ("rejected", ["field NODE of old"])   # retired: left alone
    again = F.seed()
    assert again["proposed"] == 0 and len(mine(db.session.query(Facet).filter(Facet.facet == "server"))) == 31


def test_a_category_read_from_the_data_is_not_the_llms_to_choose_in(platform):
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet, Tag

    F.seed()
    db.session.add(Facet(facet="team", value="Platform team", status="approved", source="admin"))
    db.session.query(Facet).filter(Facet.value == "srv-05").update({"status": "approved"})
    db.session.commit()
    assert F.llm_categories() == ("subject", "application", "component", "team")
    tool = F.classify_tool()["function"]["parameters"]["properties"]
    assert tool["new_values"]["items"]["properties"]["facet"]["enum"] == ["subject", "application", "component", "team"]
    assert tool["items"]["items"]["properties"]["others"]["items"]["properties"]["category"]["enum"] == ["team"]
    text = F.classify_messages([{"ref": "entry:1", "kind": "guide", "title": "t", "text": "x"}], F.vocabulary(), [])[1]["content"]
    assert "- team: Platform team" in text and "- server:" not in text and "srv-0" not in text
    # what the LLM says of such a category anyway is not taken; a part it names as a component is that server
    ref = "entry:1"
    n = F.apply({"items": [{"ref": ref, "aspect": "technical", "confidence": "high", "components": ["srv-05", "scheduler"],
                            "others": [{"category": "server", "value": "srv-new"}, {"category": "team", "value": "Platform team"}]}],
                 "new_values": [{"facet": "server", "value": "srv-new"}]}, [{"ref": ref, "hash": "h"}], F._table_refs())
    assert db.session.query(Facet).filter(Facet.value == "srv-new").count() == 0
    tags = {(f.facet, f.value) for t, f in db.session.query(Tag, Facet).join(Facet, Facet.id == Tag.facet_id).filter(Tag.ref == ref)}
    assert ("server", "srv-05") in tags and ("team", "Platform team") in tags and ("component", "scheduler") in tags
    assert n["proposed"] == 0
    db.session.query(Tag).filter(Tag.ref == ref).delete()
    db.session.query(Facet).filter(Facet.value == "scheduler").delete()
    db.session.commit()


class _LLM:
    seen: list = []

    def __init__(self, *a, **k) -> None:
        pass

    def chat(self, messages, tools=None, max_tokens=None):
        _LLM.seen.append(messages)
        return classify_reply(messages)


def test_a_classification_is_a_run_with_its_steps(platform, app, monkeypatch):
    from superset.extensions import db

    import supagent.llm as L
    from supagent import settings
    from supagent.knowledge import learner
    from supagent.models import Classified, Run

    from supagent.knowledge import retire

    platform["conf"]["learn.interactions"] = False
    platform["conf"]["learn.interactions_logs"] = False                 # (its own test: test_linkfinder_090)
    monkeypatch.setattr(retire, "recent", lambda seconds=120.0: {})      # (no database is asked in this test)
    monkeypatch.setattr(L, "LLM", _LLM)
    _LLM.seen = []
    db.session.query(Classified).delete()
    db.session.query(Run).filter(Run.status == "running").update({"status": "done"})     # the fixture's own run
    db.session.commit()
    before = db.session.query(Run).count()
    out = learner.run_classification(reason="cli", minutes=5, limit=16)
    run_id = out["run"]
    try:
        run = db.session.get(Run, run_id)
        assert (run.kind, run.reason, run.status) == ("classify", "cli", "done") and db.session.query(Run).count() == before + 1
        steps = run.stats["steps"]
        assert [s["step"] for s in steps] == [
            "categories: the values read in the data's fields", "categories: what waits, by the rules",
            "categories: the knowledge items that changed", "categories: given to the items by the LLM",
            "categories: parts that look retired", "update the search"]
        assert steps[0]["values_read"] >= 31 and steps[0]["server"] >= 31 and "seconds" in steps[0]
        todo = steps[2]["to_classify"]
        assert 8 < todo <= 16 and steps[3]["calls"] == 2 and steps[3]["items"] == todo      # 8 items per call
        assert run.stats["classified"]["calls"] == 2 and run.stats["seconds"] >= 0 and len(_LLM.seen) == 2
        # another run cannot start while one is running, whatever its kind
        busy = Run(kind="learn", reason="manual", status="running", stats={})
        db.session.add(busy)
        db.session.commit()
        busy_id = busy.id
        skipped = learner.run_classification(reason="cli")
        assert skipped["status"] == "skipped" and f"run {busy_id} is still running" in skipped["reason"]
        with _client(app, "admin") as c:
            r = c.post("/supagent/admin/api/classify", json={})
            assert r.status_code == 409 and "learning run" in r.get_json()["error"]
        db.session.query(Run).filter(Run.id == busy_id).update({"status": "done"})
        db.session.commit()
        with _client(app, "admin") as c:
            listed = c.get("/supagent/admin/api/runs").get_json()["runs"]
            mine = next(x for x in listed if x["id"] == run_id)
            assert mine["kind"] == "classify" and len(mine["stats"]["steps"]) == 6
        with _client(app, "alice") as c:
            assert c.post("/supagent/admin/api/classify", json={}).status_code in (302, 401, 403)
    finally:
        db.session.rollback()
        db.session.query(Run).filter(Run.id > 0, Run.kind.in_(("classify",))).delete(synchronize_session=False)
        db.session.query(Run).filter(Run.reason == "manual", Run.kind == "learn", Run.stats == {}).delete(synchronize_session=False)
        db.session.query(Classified).delete()
        db.session.commit()
