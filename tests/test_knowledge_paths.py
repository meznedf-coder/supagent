"""Every way knowledge is put in reaches the agent at once, and nothing reaches it twice: a
description a person writes in the Data dictionary (a field, a metric's label), a catalog entry
saved, deleted (its descriptions taken back), restored, imported; the caches of every process
made again (the knowledge stamp); the knowledge check finds nothing out of step after each; the
prompt gives each piece once (a rule of the instructions, a memory, a metric of the same name in
two databases, a learned answer are not repeated in the knowledge found), so that the room goes
to the catalog, the documents and the Context."""

from __future__ import annotations

import types

import pytest

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.fixture()
def fresh(world, monkeypatch):
    """The stamp read at every call (another process would read it within READ_S seconds)."""
    from supagent.knowledge import freshness

    monkeypatch.setattr(freshness, "READ_S", 0.0)
    from supagent.knowledge.index import sync

    sync()
    yield world
    from superset.extensions import db

    from supagent.models import Entry, EntryVersion

    db.session.query(EntryVersion).delete()
    db.session.query(Entry).delete()
    db.session.commit()
    from supagent.knowledge.catalog import invalidate

    invalidate()
    sync()


def _piece(title: str) -> str:
    from superset.extensions import db

    from supagent.models import Chunk

    c = db.session.query(Chunk).filter(Chunk.title == title).first()
    return c.text if c is not None else ""


def _out_of_step() -> int:
    from supagent.knowledge.audit import audit

    return audit()["pieces"]["out_of_step"]


def _admin(app):
    c = app.test_client()
    login(c, "admin")
    return c


def test_a_description_written_in_the_dictionary_is_used_by_the_next_answer(fresh, app):
    from superset.extensions import db

    from supagent.knowledge.resolve import where_block
    from supagent.models import KObject
    from supagent.security import acting_as

    with acting_as("admin"):
        assert "SQL" in where_block("failed jobs per node")            # the resolver's lists are cached now
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    mode = db.session.query(KObject).filter_by(kind="label", parent="node_cpu_seconds_total", name="mode").one()
    with app.app_context(), _admin(app) as c:
        assert c.post(f"/supagent/dictionary/api/objects/{node.id}",
                      json={"description": "The worker host that executed the batch"}).status_code == 200
        assert c.post(f"/supagent/dictionary/api/objects/{mode.id}",
                      json={"description": "CPU state: idle or busy modes"}).status_code == 200
    db.session.expire_all()
    with acting_as("admin"):
        block = where_block("which worker host executed the batch")
    assert "The worker host that executed the batch" in block           # not the cached lists of before
    assert "The worker host that executed the batch" in _piece("index jobs")         # the search, at once
    assert "CPU state: idle or busy modes" in _piece("metric node_cpu_seconds_total")  # the label's metric
    assert _out_of_step() == 0


def test_another_process_sees_the_change_at_its_next_question(fresh):
    from superset.extensions import db

    from supagent.knowledge import excluded, resolve
    from supagent.models import KObject, Meta

    def lists() -> list:
        return resolve._dictionary(tuple(sorted({world_s.id for world_s in (fresh["s_jobs"], fresh["s_prom"])})))

    before = lists()
    assert lists() is before                                            # cached
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    node.description, node.description_source = "not used anymore", "curated"
    row = db.session.query(Meta).filter(Meta.key == "knowledge_changed").first()
    if row is None:                                                     # another server's edit: only its stamp
        db.session.add(Meta(key="knowledge_changed", value="elsewhere"))
    else:
        row.value = "elsewhere"
    db.session.commit()
    after = lists()
    assert after is not before and any("anymore" in o["words"] for o in after if o["name"] == "NODE")
    assert any(x["name"] == "NODE" for x in excluded.excluded())        # not after its 60 s
    node.description, node.description_source = None, None
    db.session.commit()


def test_a_catalog_entry_applied_then_taken_back_when_deleted(fresh, app):
    from superset.extensions import db

    from supagent.models import KObject

    yaml_v1 = "jobs:\n  description: Every run of the night batch\n  fields:\n    STATUS: {description: FAILED or SUCCESS}\n"
    with app.app_context(), _admin(app) as c:
        r = c.post("/supagent/admin/api/entries", json={"title": "Jobs", "classification": "index",
                                                        "content": yaml_v1})
        entry = r.get_json()["entry"]
        assert r.get_json()["applied"]["curated"] == 2
        assert "Every run of the night batch" in _piece("index jobs") and "FAILED or SUCCESS" in _piece("index jobs")
        assert _out_of_step() == 0
        c.put(f"/supagent/admin/api/entries/{entry['id']}", json={**entry, "version": 1, "content":
                                                                   "jobs:\n  description: Every run of the batch\n"})
        status = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="STATUS").one()
        db.session.refresh(status)
        assert status.description is None                                # no longer given: taken back
        index = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
        assert c.post(f"/supagent/dictionary/api/objects/{index.id}",
                      json={"description": "Our own words"}).status_code == 200      # a person's text since
        assert c.delete(f"/supagent/admin/api/entries/{entry['id']}?version=2").status_code == 200
        db.session.refresh(index)
        assert index.description == "Our own words"                      # kept: not the catalog's text
        assert "FAILED or SUCCESS" not in _piece("index jobs") and "Our own words" in _piece("index jobs")
        assert c.post(f"/supagent/admin/api/entries/{entry['id']}/restore", json={"version": 1}).status_code == 200
        db.session.refresh(status)
        assert status.description == "FAILED or SUCCESS" and "FAILED or SUCCESS" in _piece("index jobs")
        assert _out_of_step() == 0
    index.description, index.description_source = None, None
    db.session.commit()


def test_an_imported_catalog_is_applied_and_searchable_at_once(fresh, app):
    with app.app_context(), _admin(app) as c:
        r = c.post("/supagent/admin/api/catalog", json={"content": (
            "glossary:\n  SLA: the night batch ends before 06:00\n"
            "metrics:\n  tables:\n    node_cpu_seconds_total:\n      description: Seconds of CPU per mode\n"
            "      labels: {node: The server}\n")})
        assert r.status_code == 200 and r.get_json()["applied"]["curated"] == 2
    assert "Seconds of CPU per mode" in _piece("metric node_cpu_seconds_total")
    assert "The server" in _piece("metric node_cpu_seconds_total")
    assert _piece("SLA (Business)") == "SLA: the night batch ends before 06:00"      # a term, its own piece
    assert _out_of_step() == 0


def test_the_knowledge_check_names_what_reaches_nothing(fresh):
    from superset.extensions import db

    from supagent.knowledge.audit import audit
    from supagent.knowledge.catalog import invalidate, save_entry
    from supagent.models import Entry

    save_entry({"title": "Jobs", "classification": "index",
                "content": "jobs:\n  fields:\n    NOED: {description: typo of NODE}\n"}, by="admin")
    db.session.add(Entry(title="Broken", classification="index", fmt="yaml", content="jobs: [", version=1,
                         enabled=True, updated_by="admin"))                  # written before the checks existed
    db.session.commit()
    invalidate()
    out = audit()
    assert "field NOED of jobs" in out["catalog"]["unknown_names"]
    assert any("'Broken' is ignored" in p for p in out["problems"])
    assert any("NOED" in p for p in out["problems"])


def test_a_sync_of_memories_does_not_read_the_dictionary(fresh, monkeypatch):
    from supagent.knowledge import index

    def boom():
        raise AssertionError("the whole dictionary read for a memory")

    monkeypatch.setattr(index, "KINDS", tuple((p, boom if p == "object:" else f) for p, f in index.KINDS))
    assert index.sync(("memory:",))["removed"] == 0
    monkeypatch.undo()


# ---------------------------------------------------------------------------------------------- #
# nothing twice in the prompt
# ---------------------------------------------------------------------------------------------- #
def test_the_knowledge_found_gives_what_the_other_blocks_do_not(fresh, monkeypatch):
    from superset.extensions import db, security_manager as sm

    from supagent import settings
    from supagent.agent import Agent, ChartGuard
    from supagent.knowledge.experience import words
    from supagent.knowledge.index import sync
    from supagent.knowledge.store import source_for, upsert
    from supagent.models import ContextPage, Entry, Memory, Recipe
    from supagent.security import acting_as
    from test_knowledge import _database

    other = _database("metrics copy", "promagg://127.0.0.1:9010/prometheus")
    s_other = source_for(other)
    upsert(fresh["run"], s_other, "metric", "", "node_cpu_seconds_total",
           {"metric_type": "counter", "backend_help": "Seconds the CPUs spent in each mode.", "stats": {}})
    admin = sm.find_user(username="admin").id
    q = "cpu seconds per mode of the servers"
    rows = [Entry(title="Busy CPU", classification="rule", content="Busy CPU means every mode but idle.",
                  updated_by="admin"),
            Memory(scope="team", user_id=admin, kind="fact", text="The cpu seconds reset when a server reboots",
                   status="active"),
            Recipe(question="cpu seconds per mode", words=" ".join(sorted(words("cpu seconds per mode"))),
                   tool="execute_sql", database_id=fresh["metrics"].id, target="node_cpu_seconds_total",
                   status="confirmed", query='SELECT mode, SUM(rate) FROM "node_cpu_seconds_total" GROUP BY 1'),
            ContextPage(section="technical", slug="servers", title="Servers and their CPU", kind="summary",
                        content="The cpu seconds per mode of the servers show how busy each server is.",
                        database_ids=[fresh["metrics"].id], author="agent")]
    db.session.add_all(rows)
    db.session.commit()
    sync()
    settings.set_value("search.top_k", 12)
    settings.set_value("search.prompt_chars", 8000)
    try:
        a = object.__new__(Agent)
        a.username, a.rich, a.wants_saved_chart = "admin", True, False
        a.superset = types.SimpleNamespace(available=True, error=None)
        a.guard = ChartGuard(a)
        with acting_as("admin"):
            shown: set[str] = set()
            system = a._system(q, shown)
            blocks = a._question_blocks(q, shown)
        background = blocks[blocks.find("Background that looks relevant"):]
        background = background[:background.find("\n\nWays that answered")] if "Ways that" in background else background
        assert "Busy CPU means every mode but idle." in system and "Busy CPU" not in background     # a rule once
        assert "(team, fact) The cpu seconds reset" in blocks and "[memory]" not in background      # a memory once
        assert "Ways that answered similar questions before" in blocks and "[recipe]" not in background
        assert background.count("metric node_cpu_seconds_total") <= 1                               # two databases
        if 'metric "node_cpu_seconds_total"' in blocks:                    # in "Where the data is" with details
            assert "metric node_cpu_seconds_total" not in background
        assert "[context] Context (AI-written overview): Servers and their CPU" in background       # room for it
    finally:
        settings.set_value("search.top_k", 6)
        settings.set_value("search.prompt_chars", 2500)
        for r in rows:
            db.session.delete(db.session.merge(r))
        db.session.delete(s_other)
        db.session.delete(other)
        db.session.commit()
        sync()


# ---------------------------------------------------------------------------------------------- #
# the columns the connector computes are in the dictionary
# ---------------------------------------------------------------------------------------------- #
def _field(name: str, os_type: str, sql_type: str, virtual: str | None = None):
    """A field as osagg describes it (osagg.metadata.Field: its attributes the learner reads)."""
    return types.SimpleNamespace(name=name, os_type=os_type, sql_type=sql_type, virtual=virtual,
                                 agg_field=None if virtual else name, is_date=sql_type == "TIMESTAMP",
                                 is_numeric=sql_type in ("BIGINT", "DOUBLE"))


def test_the_columns_osagg_computes_are_learned_with_what_they_are(ctx, monkeypatch):
    from supagent.knowledge import learn_indices as LI

    fields = {"POSITION_DATE": _field("POSITION_DATE", "keyword", "VARCHAR"),
              "ts": _field("ts", "date", "TIMESTAMP"),
              "POSITION_LABEL": _field("POSITION_LABEL", "virtual", "VARCHAR", "label:POSITION_DATE"),
              "POSITION_TIME": _field("POSITION_TIME", "virtual", "TIMESTAMP", "shift:POSITION_DATE:ts")}
    meta = types.SimpleNamespace(name="jobs", indices=["jobs"], fields=fields)
    asked: list[dict] = []

    class Transport:
        def search(self, index, body):
            asked.append(body)
            return {"aggregations": {"_seen": {"doc_count": 10}}, "hits": {"total": {"value": 10}}}

    conn = types.SimpleNamespace(transport=Transport(), tz=None)
    monkeypatch.setattr(LI.settings, "get", lambda key: {"learn.sample_docs": 1000, "learn.fields_per_request": 20,
                                                          "learn.request_timeout": 30}.get(key, 30))
    _info, fstats = LI.profile_index(conn, "jobs", meta)
    assert fstats["POSITION_LABEL"] == {"type": "varchar", "computed": "computed by the connector: the business-day "
                                        "label of POSITION_DATE (D, D-1, W-1, Y-1...)"}
    assert "D-1 position date of POSITION_DATE" in fstats["POSITION_TIME"]["computed"]
    assert not any("POSITION_LABEL" in str(b) for b in asked)          # never asked to OpenSearch (not stored)


# ---------------------------------------------------------------------------------------------- #
# the team's words
# ---------------------------------------------------------------------------------------------- #
GLOSSARY = """position date / business date: POSITION_DATE (yyyymmdd), the business day a job computes, not the day it runs.
POSITION_LABEL: D, D-1, D-2... (business days before the session date), W-1, W-2... (same weekday 1, 2... weeks
  before), Y-1. Filter with POSITION_LABEL IN ('D-1', 'W-1').
POSITION_TIME: execution time moved onto the D-1 position date, to overlay several position dates on one time axis
  (X axis POSITION_TIME, one line per POSITION_LABEL).
UAT: the acceptance environment, excluded unless asked.
SLA: the night batch ends before 06:00.
"""


def test_the_terms_of_a_question_are_given_in_full(fresh):
    from supagent.knowledge.catalog import save_entry
    from supagent.knowledge.glossary import found, glossary_block

    save_entry({"title": "Glossary", "classification": "glossary", "category": "Business", "content": GLOSSARY},
               by="admin")
    question = "Jobs of BILLING for the business day D compared with the average of W-1 to W-4, as a timeline"
    terms = [t["term"] for t in found(question)]
    assert terms[0] == "POSITION_LABEL"                                      # W-1, W-4: codes of its definition
    assert "POSITION_TIME" in terms                                          # names POSITION_LABEL
    assert "position date / business date" in terms                          # "business", its rare word
    assert "UAT" not in terms and "SLA" not in terms
    shown: set[str] = set()
    block = glossary_block(question, shown)
    assert "- POSITION_TIME: execution time moved onto the D-1 position date" in block
    assert len(shown) == len(terms)
    assert [t["term"] for t in found("failed jobs in UAT yesterday")] == ["UAT"]
    assert found("how many jobs failed yesterday") == []                     # nothing of the glossary


def test_the_prompt_gives_the_terms_once_and_before_the_data(fresh, monkeypatch):
    from supagent import settings
    from supagent.agent import Agent, ChartGuard
    from supagent.knowledge.catalog import save_entry
    from supagent.security import acting_as

    save_entry({"title": "Glossary", "classification": "glossary", "category": "Business", "content": GLOSSARY},
               by="admin")
    from supagent.knowledge.index import sync

    sync()
    settings.set_value("search.top_k", 12)
    settings.set_value("search.prompt_chars", 8000)
    try:
        a = object.__new__(Agent)
        a.username, a.rich, a.wants_saved_chart = "admin", True, False
        a.superset = types.SimpleNamespace(available=True, error=None)
        a.guard = ChartGuard(a)
        with acting_as("admin"):
            shown: set[str] = set()
            blocks = a._question_blocks("jobs per POSITION_LABEL for W-1 and W-2", shown)
    finally:
        settings.set_value("search.top_k", 6)
        settings.set_value("search.prompt_chars", 2500)
    words = blocks.find("The team's words in this question")
    assert 0 <= words and ("Where the data is" not in blocks or words < blocks.find("Where the data is"))
    background = blocks[blocks.find("Background that looks relevant"):] if "Background" in blocks else ""
    assert "POSITION_LABEL: D, D-1" not in background                        # given once


def test_an_unverified_ai_description_is_marked_where_the_data_is(fresh):
    from superset.extensions import db

    from supagent.knowledge.freshness import touch
    from supagent.knowledge.resolve import where_block
    from supagent.models import KObject
    from supagent.security import acting_as

    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    node.description, node.description_source, node.verified = "The server of the job, in days", "llm", False
    touch()
    db.session.commit()
    try:
        with acting_as("admin"):
            block = where_block("jobs per server of the job")
        assert "The server of the job, in days (AI-written, unverified)" in block
    finally:
        node.description, node.description_source = None, None
        touch()
        db.session.commit()


def test_tenants_are_named_where_the_data_is(fresh):
    from superset.extensions import db

    from supagent.agent import PROMAGG_RULES
    from supagent.knowledge.resolve import where_block
    from supagent.knowledge.store import upsert
    from supagent.models import KObject
    from supagent.security import acting_as

    assert "each tenant is usually a\ndifferent application or subject" in PROMAGG_RULES
    upsert(fresh["run"], fresh["s_prom"], "label", "node_cpu_seconds_total", "__tenant_id__",
           {"data_type": "string", "stats": {"values": ["billing-prod", "payroll-prod"]}})
    m = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    m.stats = {**(m.stats or {}), "labels": ["node", "mode", "__tenant_id__"]}
    from supagent.knowledge.freshness import touch

    touch()
    db.session.commit()
    with acting_as("admin"):
        block = where_block("cpu seconds per node")
    assert "tenants (__tenant_id__, each usually a different application or subject): billing-prod, payroll-prod" in block


def test_superset_charts_and_dashboards_are_knowledge(fresh, app):
    import json

    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from supagent.knowledge.index import sync
    from supagent.knowledge.search import search
    from supagent.security import acting_as

    jobs = fresh["jobs"]
    ds = SqlaTable(table_name="jobs", database_id=jobs.id)
    db.session.add(ds)
    db.session.flush()
    ds_id = ds.id
    chart = Slice(slice_name="Failed jobs by node", viz_type="echarts_timeseries_bar", datasource_id=ds.id,
                  datasource_type="table", params=json.dumps({
                      "metrics": [{"label": "failed", "sqlExpression": "COUNT(*)"}], "groupby": ["NODE"],
                      "adhoc_filters": [{"expressionType": "SIMPLE", "subject": "STATUS", "operator": "==",
                                         "comparator": "FAILED"}], "time_range": "Last day"}))
    board = Dashboard(dashboard_title="Night gateway health", slices=[chart])
    db.session.add_all([chart, board])
    db.session.commit()
    chart_id, board_id, jobs_id = chart.id, board.id, jobs.id
    try:
        sync(("superset:",))
        with acting_as("alice"):                                         # jobs: her database
            found = {f["ref"]: f for f in search("gateway health failed jobs by node", k=10)}
        c = found[f"superset:chart:{chart_id}"]
        assert "on the dataset jobs" in c["text"] and "STATUS == FAILED" in c["text"] and "Night gateway health" in c["text"]
        assert f"superset:dashboard:{board_id}:{jobs_id}" in found
        with acting_as("bob"):                                           # no database: nothing of it
            assert not [r for r in search("gateway health failed jobs by node", k=10) if r["ref"].startswith("superset:")]
    finally:
        db.session.rollback()
        for model, ident in ((Dashboard, board_id), (Slice, chart_id), (SqlaTable, ds_id)):
            obj = db.session.get(model, ident)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()
        sync(("superset:",))


def test_charts_are_in_the_knowledge_found_only_for_questions_about_them(fresh, monkeypatch):
    from supagent.knowledge import search as S

    seen: list = []
    monkeypatch.setattr(S, "search", lambda q, k=None, kinds=None, lower=None, skip=(): seen.append(skip) or [])
    S.knowledge_block("How many jobs failed yesterday?")
    S.knowledge_block("Is everything normal on the gateway dashboard?", with_charts=True)
    assert seen == [("chart", "dashboard"), ()]
