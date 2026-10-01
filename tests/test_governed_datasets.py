"""The governed pipeline's queries really run: a SQLite table of jobs (a Superset database and dataset), a plan
checked and built by code, run as execute_sql and through Superset's chart data API on the dataset: the same
numbers; and the dataset's row-level security applies on the dataset path (a user who may only see BILLING)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

ROWS = [("BILLING", "FAILED", "PROD", "2026-09-23 01:00:00", 3000.0), ("BILLING", "SUCCESS", "PROD", "2026-09-23 02:00:00", 60.0),
        ("BILLING", "FAILED", "UAT", "2026-09-23 03:00:00", 120.0), ("PAYROLL", "FAILED", "PROD", "2026-09-23 04:00:00", 4000.0),
        ("PAYROLL", "FAILED", "PROD", "2026-09-22 23:00:00", 50.0), ("ORDERS", "SUCCESS", "PROD", "2026-09-23 05:00:00", 30.0)]


@pytest.fixture()
def jobs(ctx, monkeypatch):
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.core import Database

    from supagent.knowledge.freshness import touch
    from supagent.knowledge.store import source_for, upsert
    from supagent.models import KObject, Run, Source

    path = os.path.join(tempfile.mkdtemp(prefix="supagent-jobs-"), "jobs.db")
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE batch_jobs ("APPLICATION" TEXT, "STATUS" TEXT, "ENV" TEXT, ts TEXT, "DURATION_S" REAL)')
    con.executemany("INSERT INTO batch_jobs VALUES (?, ?, ?, ?, ?)", ROWS)
    con.commit()
    con.close()
    for model in (KObject, Source):
        db.session.query(model).delete()
    from supagent import settings

    real = settings.get                                   # the agent may use this database (agent.databases)
    monkeypatch.setattr(settings, "get", lambda key: ["jobs sqlite"] if key == "agent.databases" else real(key))
    d = Database(database_name="jobs sqlite", sqlalchemy_uri=f"sqlite:///{path}")
    db.session.add(d)
    db.session.commit()
    ds = SqlaTable(table_name="batch_jobs", database_id=d.id)
    db.session.add(ds)
    db.session.commit()
    ds.fetch_metadata()
    for c in ds.columns:
        if c.column_name == "ts":
            c.is_dttm = True
    db.session.commit()
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    s = source_for(d)
    upsert(run, s, "index", "", "batch_jobs", {"stats": {"time_field": "ts"}})
    for name, dtype, values in (("APPLICATION", "keyword", ["BILLING", "PAYROLL", "ORDERS"]),
                                ("STATUS", "keyword", ["FAILED", "SUCCESS"]), ("ENV", "keyword", ["PROD", "UAT"]),
                                ("DURATION_S", "double", [])):
        upsert(run, s, "field", "batch_jobs", name, {"data_type": dtype, "stats": {"values": values,
                                                                                     "cardinality": len(values)}})
    db.session.commit()
    touch()
    ids = {"db": d.id, "ds": ds.id}
    yield ids
    db.session.rollback()
    from superset.connectors.sqla.models import RowLevelSecurityFilter

    for f in db.session.query(RowLevelSecurityFilter).filter(RowLevelSecurityFilter.name == "billing only"):
        db.session.delete(f)
    db.session.query(SqlaTable).filter(SqlaTable.id == ids["ds"]).delete(synchronize_session=False)
    for model in (KObject, Source):
        db.session.query(model).delete()
    db.session.query(Database).filter(Database.id == ids["db"]).delete(synchronize_session=False)
    db.session.commit()
    touch()


def _pack(db_id):
    from supagent.governed.decider import Candidate, Pack, table_info

    c = Candidate(subject=f"data:{db_id}:batch_jobs", kind="index", title="batch_jobs", database_id=db_id,
                  database="jobs sqlite", backend="sqlite", table="batch_jobs", extra={"time_field": "ts"})
    return Pack(tables=[table_info(c, "T1")], knowledge=[], ambiguous=[], missing=[], kind="data", confidence="high")


def _step():
    from supagent.governed.plan import parse_plan

    plan, err = parse_plan({"kind": "answer", "steps": [{
        "id": "q1", "table": "T1", "by": ["APPLICATION"], "order": [{"by": "failed jobs", "desc": True}],
        "measures": [{"label": "failed jobs", "fn": "count", "where": [
            {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}]},
            {"label": "avg minutes", "fn": "avg", "field": "DURATION_S", "unit": "minutes", "source_unit": "seconds"}],
        "where": [{"field": "ENV", "op": "!=", "value": "UAT", "source": "question: without UAT"}],
        "period": {"start": "2026-09-23 00:00", "end": "2026-09-24 00:00", "source": "question: on 23 September"}}]})
    assert plan is not None, err
    return plan


def test_the_query_built_by_code_runs_the_same_both_ways(jobs):
    from supagent.governed.compile import compile_step, dataset_for, run_on_dataset
    from supagent.governed.validate import check
    from supagent.security import acting_as
    from supagent.tools_superset import execute_sql

    pack = _pack(jobs["db"])
    plan = _step()
    checked = check(plan, pack, "Failed jobs per application on 23 September, without UAT, and their average "
                                "duration in minutes?")
    assert checked.ok, checked.errors
    t = pack.table("T1")
    sql = compile_step(plan.steps[0], t)
    with acting_as("admin"):
        direct = execute_sql(type("R", (), {"database_id": jobs["db"], "sql": sql, "limit": 1000})())
        via = run_on_dataset(plan.steps[0], t, dataset_for(t))
    assert direct["success"], direct
    got = {r["APPLICATION"]: (r["failed jobs"], round(r["avg minutes"], 3)) for r in direct["rows"]}
    assert got == {"BILLING": (1, round((3000 + 60) / 2 / 60, 3)), "PAYROLL": (1, round(4000 / 60, 3)),
                   "ORDERS": (0, 0.5)}                                 # UAT and 22 September left out
    assert {r["APPLICATION"]: (r["failed jobs"], round(r["avg minutes"], 3)) for r in via["rows"]} == got
    assert "batch_jobs" in via["sql"] and via["via"].startswith("the dataset batch_jobs")


def test_row_level_security_applies_on_the_dataset_path(jobs):
    from superset.connectors.sqla.models import RowLevelSecurityFilter, SqlaTable
    from superset.extensions import db, security_manager as sm
    from superset.utils.core import RowLevelSecurityFilterType

    from supagent.governed.compile import dataset_for, run_on_dataset
    from supagent.security import acting_as

    role = sm.find_role("billing readers") or sm.add_role("billing readers")
    for perm, view in (("database_access", db.session.get(SqlaTable, jobs["ds"]).database.perm),
                       ("datasource_access", db.session.get(SqlaTable, jobs["ds"]).perm)):
        pvm = sm.find_permission_view_menu(perm, view) or sm.add_permission_view_menu(perm, view)
        sm.add_permission_role(role, pvm)
    bob = sm.find_user(username="bob")
    if role not in bob.roles:
        bob.roles.append(role)
    db.session.add(RowLevelSecurityFilter(name="billing only", filter_type=RowLevelSecurityFilterType.REGULAR,
                                          clause="\"APPLICATION\" = 'BILLING'", tables=[db.session.get(SqlaTable, jobs["ds"])],
                                          roles=[role]))
    db.session.commit()
    pack = _pack(jobs["db"])
    plan = _step()
    t = pack.table("T1")
    with acting_as("bob"):
        rows = run_on_dataset(plan.steps[0], t, dataset_for(t))["rows"]
    assert [r["APPLICATION"] for r in rows] == ["BILLING"]
    with acting_as("admin"):                              # the same query for a user without the rule: every row
        assert len(run_on_dataset(plan.steps[0], t, dataset_for(t))["rows"]) == 3
    bob = sm.find_user(username="bob")
    bob.roles = [r for r in bob.roles if r.name != "billing readers"]
    db.session.commit()
