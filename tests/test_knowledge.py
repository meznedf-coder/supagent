"""The learned dictionary: change detection, measured relations, catalog, LLM descriptions,
and what each user may see of it."""

from __future__ import annotations

import datetime as dt
import json

import pytest


def _database(name: str, uri: str):
    from superset.extensions import db
    from superset.models.core import Database

    d = db.session.query(Database).filter_by(database_name=name).one_or_none()
    if d is None:
        d = Database(database_name=name, sqlalchemy_uri=uri)
        db.session.add(d)
        db.session.commit()
    return d


@pytest.fixture()
def world(ctx):
    """An OpenSearch database (jobs index) and a metrics database (node_cpu metric), learned
    once; alice may query only the OpenSearch one."""
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.store import source_for, upsert
    from supagent.models import Change, KObject, Relation, Run, Source

    for model in (Change, Relation, KObject, Run, Source):
        db.session.query(model).delete()
    db.session.commit()
    jobs_db = _database("jobs", "osagg://127.0.0.1:9200/?timezone=Europe/Paris")
    prom_db = _database("metrics", "promagg://127.0.0.1:9009/prometheus")
    role = sm.find_role("jobs reader") or sm.add_role("jobs reader")
    pvm = sm.find_permission_view_menu("database_access", jobs_db.perm) or \
        sm.add_permission_view_menu("database_access", jobs_db.perm)
    sm.add_permission_role(role, pvm)
    alice = sm.find_user(username="alice")
    if role not in alice.roles:
        alice.roles.append(role)
    db.session.commit()
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    s_jobs, s_prom = source_for(jobs_db), source_for(prom_db)
    upsert(run, s_jobs, "index", "", "jobs", {"stats": {"docs": 1000, "time_field": "ts",
                                                          "time_range": ["2026-09-01 00:00", "2026-09-24 23:00"]}})
    upsert(run, s_jobs, "field", "jobs", "NODE", {"data_type": "keyword",
                                                   "stats": {"cardinality": 4, "values": ["srv-1", "srv-2", "srv-3", "srv-4"]}})
    upsert(run, s_jobs, "field", "jobs", "STATUS", {"data_type": "keyword",
                                                     "stats": {"cardinality": 2, "values": ["FAILED", "SUCCESS"]}})
    upsert(run, s_prom, "metric", "", "node_cpu_seconds_total", {"metric_type": "counter", "unit": "seconds",
                                                                  "backend_help": "Seconds the CPUs spent in each mode.",
                                                                  "stats": {"series": 32}})
    upsert(run, s_prom, "label", "node_cpu_seconds_total", "node",
           {"data_type": "string", "stats": {"cardinality": 4, "values": ["srv-1", "srv-2", "srv-3", "srv-9"]}})
    upsert(run, s_prom, "label", "node_cpu_seconds_total", "mode",
           {"data_type": "string", "stats": {"cardinality": 2, "values": ["idle", "user"]}})
    for s in (s_jobs, s_prom):
        s.last_learned_at = dt.datetime.utcnow()
    db.session.commit()
    return {"jobs": jobs_db, "metrics": prom_db, "run": run, "s_jobs": s_jobs, "s_prom": s_prom}


def test_first_run_records_no_new_then_changes(world):
    from superset.extensions import db

    from supagent.knowledge.store import mark_gone, upsert
    from supagent.models import Change, KObject, Run

    first = db.session.query(Change).filter_by(run_id=world["run"].id).all()
    assert first == []                                  # a first run finds everything new: nothing recorded
    run2 = Run(kind="learn", reason="test")
    db.session.add(run2)
    db.session.commit()
    s_prom = world["s_prom"]
    upsert(run2, s_prom, "metric", "", "node_cpu_seconds_total", {"metric_type": "gauge", "stats": {"series": 320}})
    upsert(run2, s_prom, "metric", "", "node_load1", {"metric_type": "gauge", "stats": {"series": 4}})
    gone = mark_gone(run2, s_prom, "metric", {("", "node_load1")})
    db.session.commit()
    kinds = sorted(c.change for c in db.session.query(Change).filter_by(run_id=run2.id))
    assert kinds == ["cardinality", "gone", "new", "type"]
    assert gone == 1
    cpu = db.session.query(KObject).filter_by(name="node_cpu_seconds_total").one()
    assert cpu.gone_at is not None
    assert cpu.description == "Seconds the CPUs spent in each mode." and cpu.description_source == "backend"
    run3 = Run(kind="learn", reason="test")
    db.session.add(run3)
    db.session.commit()
    upsert(run3, s_prom, "metric", "", "node_cpu_seconds_total", {"metric_type": "gauge", "stats": {"series": 320}})
    db.session.commit()
    assert [c.change for c in db.session.query(Change).filter_by(run_id=run3.id)] == ["back"]


def test_measured_relations_with_evidence(world):
    from superset.extensions import db

    from supagent.knowledge.relations import learn_relations
    from supagent.models import KObject, Relation

    out = learn_relations()
    assert out["same_values"] == 1
    rel = db.session.query(Relation).filter_by(relation="same_values").one()
    ends = {db.session.get(KObject, rel.a_id).name, db.session.get(KObject, rel.b_id).name}
    assert ends == {"node", "NODE"}
    assert rel.evidence["common"] == 3 and rel.evidence["coverage"] == 0.75
    # the data changes: the relation is not supported any more and goes
    label = db.session.query(KObject).filter_by(kind="label", name="node").one()
    label.stats = {"values": ["a", "b", "c"]}
    db.session.commit()
    assert learn_relations() == {"same_values": 0, "removed": 1, "rejected": 0}


def test_a_wrong_relation_is_rejected_for_good(world, app):
    """An admin marks a measured relation wrong: the daily measuring keeps it rejected, the
    agent never sees it; Restore brings it back."""
    from flask import g
    from superset.extensions import db

    from conftest import login
    from supagent.knowledge.describe import _relations as described
    from supagent.knowledge.relations import learn_relations, relations_of
    from supagent.models import KObject, Relation

    learn_relations()
    rel = db.session.query(Relation).filter_by(relation="same_values").one()
    rid, node = rel.id, db.session.query(KObject).filter_by(kind="field", name="NODE").one()

    def post(user, body):
        for key in ("_login_user", "user"):
            g.pop(key, None)
        with app.test_client() as c:
            login(c, user)
            return c.post(f"/supagent/dictionary/api/relations/{rid}", json=body)

    assert post("alice", {"rejected": True}).status_code == 403
    assert post("admin", {"rejected": True}).get_json() == {"id": rid, "rejected": True}
    out = learn_relations()
    assert out["rejected"] == 1 and out["same_values"] == 0
    rel = db.session.get(Relation, rid)
    assert rel is not None and rel.rejected_at is not None and rel.rejected_by == "admin"
    assert relations_of([node.id]) == [] and not described([node.id], {world["s_jobs"].id, world["s_prom"].id})
    assert post("admin", {"rejected": False}).get_json() == {"id": rid, "rejected": False}
    assert learn_relations()["same_values"] == 1 and relations_of([node.id])


def test_catalog_wins_and_relates(world):
    from superset.extensions import db

    from supagent.knowledge.catalog import import_catalog
    from supagent.knowledge.curated import apply_catalog
    from supagent.models import KObject, Relation

    import_catalog("""
indices:
  jobs:
    description: One document per job run
    fields:
      NODE: {description: Server that ran the job, synonyms: [server, host]}
metrics:
  tables:
    node_cpu_seconds_total:
      description: CPU time per CPU and mode
      labels: {mode: "idle = unused"}
  label_relationships:
    - {label: node, index: jobs, field: NODE, description: server of the job}
""", by="test")
    out = apply_catalog()
    assert out["relations"] == 1
    node = db.session.query(KObject).filter_by(kind="field", name="NODE").one()
    assert (node.description, node.description_source, node.verified) == ("Server that ran the job", "curated", True)
    assert node.synonyms == ["server", "host"]
    cpu = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    assert cpu.description == "CPU time per CPU and mode"            # the catalog wins over the HELP text
    assert db.session.query(Relation).filter_by(relation="curated").count() == 1
    from supagent.knowledge.catalog import invalidate
    from supagent.models import Entry, EntryVersion

    db.session.query(EntryVersion).delete()                             # leave no catalog for the other tests
    db.session.query(Entry).delete()
    db.session.commit()
    invalidate()


def test_llm_descriptions_are_marked_and_never_overwrite(world):
    from superset.extensions import db

    from supagent.knowledge.enrich import enrich
    from supagent.models import KObject

    node = db.session.query(KObject).filter_by(kind="field", name="NODE").one()
    node.description, node.description_source, node.verified = "Server of the run", "curated", True
    db.session.commit()

    class FakeLLM:
        asked: list = []

        def chat(self, messages, tools=None, max_tokens=None):
            items = json.loads(messages[1]["content"])
            self.asked += [i["name"] for i in items]
            return {"content": "Sure:\n" + json.dumps([{"id": i["id"], "description": f"About {i['name']}",
                                                       "category": "jobs"} for i in items])}

    llm = FakeLLM()
    out = enrich(None, deadline=9e18, llm=llm)
    assert "NODE" not in llm.asked and "le" not in llm.asked
    assert out["written"] == out["asked"] > 0
    status = db.session.query(KObject).filter_by(kind="field", name="STATUS").one()
    assert (status.description, status.description_source, status.verified) == ("About STATUS", "llm", False)
    assert db.session.query(KObject).filter_by(kind="field", name="NODE").one().description == "Server of the run"


def test_describe_shows_only_what_the_user_may_query(world):
    from supagent.knowledge.describe import describe
    from supagent.knowledge.relations import learn_relations
    from supagent.security import acting_as

    learn_relations()
    with acting_as("admin"):
        text = describe("which servers failed")
    assert "Index jobs" in text and "node_cpu_seconds_total" in text
    assert "label node" in text                        # the measured relation, both ends visible
    with acting_as("alice"):
        text = describe("which servers failed")
    assert "Index jobs" in text
    assert "node_cpu_seconds_total" not in text and "label node" not in text and "metrics" not in text.lower()
    with acting_as("nobody"):
        assert describe("servers") is None


def test_learning_gone_from_listing_even_when_profiling_stops(world, monkeypatch):
    """A time limit stops the profiling, not the knowledge of which metrics disappeared."""
    import time as _time

    from superset.extensions import db

    from supagent.knowledge import learn_metrics as lm
    from supagent.models import KObject, Run

    class Meta:
        kind, labels, unit, help, is_counter = "gauge", ["node"], "", "", False

    class Client:
        def metadata(self):
            return {}

    class Conn:
        client = Client()

        class schema:  # noqa: N801
            @staticmethod
            def meta(name):
                return Meta()

        def list_tables(self):
            return ["node_load1"]                      # node_cpu_seconds_total disappeared

        def close(self):
            pass

    monkeypatch.setattr("supagent.tools._promagg_connection", lambda database: Conn())
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    out = lm.learn_metrics(run, world["s_prom"], world["metrics"], deadline=_time.time() - 1)   # no time at all
    assert out["complete"] is False and out["gone"] >= 1
    cpu = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    assert cpu.gone_at is not None


def test_tools_only_pick_databases_the_user_may_query(world):
    from supagent.security import acting_as
    from supagent.tools import ToolError, _database

    with acting_as("alice"):
        assert _database(None).database_name == "jobs"
        with pytest.raises(ToolError) as err:
            _database("metrics")
        assert "metrics" not in str(err.value).split("databases:")[1]      # not even named
    with acting_as("admin"):
        assert _database("metrics").database_name == "metrics"


def test_admins_curate_the_dictionary(app):
    """Requests run outside any test context: each has its own g, as in production."""
    from conftest import login

    with app.app_context():
        from superset.extensions import db

        from supagent.knowledge.store import source_for, upsert
        from supagent.models import KObject

        src = source_for(_database("jobs", "osagg://127.0.0.1:9200/?timezone=Europe/Paris"))
        obj = upsert(None, src, "field", "jobs", "RESULT", {"data_type": "keyword", "stats": {}})
        db.session.commit()
        oid = obj.id
    with app.test_client() as c:
        login(c, "alice")                                   # may read the dictionary, not change it
        assert c.post(f"/supagent/dictionary/api/objects/{oid}", json={"description": "x"}).status_code in (401, 403)
    with app.test_client() as c:
        login(c, "admin")
        r = c.post(f"/supagent/dictionary/api/objects/{oid}", json={"description": "Final status of the run",
                                                                   "synonyms": "state, outcome"})
        row = r.get_json()
        assert r.status_code == 200, row
        assert (row["description"], row["description_source"], row["verified"]) == \
            ("Final status of the run", "curated", True)
    with app.app_context():
        from superset.extensions import db

        from supagent.models import KObject

        assert db.session.get(KObject, oid).synonyms == ["state", "outcome"]


def test_topic_words_match_plurals(ctx):
    from supagent.knowledge.describe import _bag, stem, topic_words

    ws = topic_words("Which metrics do we have about the servers that run the batch jobs?")
    assert {"server", "job", "batch", "metric"} <= ws and "the" not in ws
    assert "server" in _bag("1-minute load average of the server")
    assert [stem(w) for w in ("queries", "processes", "status", "cpus", "boxes")] == \
        ["query", "process", "status", "cpu", "box"]
