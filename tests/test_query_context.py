"""A chart the agent saves through Superset's MCP service (which saves no query context) gets the one
Superset's front end would save, so that its data API, CSV and text reports work; a chart type it
cannot be written for keeps none, never a stale one."""

from __future__ import annotations

import json

from test_knowledge import world  # noqa: F401  (the fixture)


def test_a_saved_chart_gets_its_query_context(world):
    from superset.connectors.sqla.models import SqlaTable
    from superset.extensions import db
    from superset.models.slice import Slice

    from supagent.security import acting_as
    from supagent.tools import refresh_query_context

    ds = SqlaTable(table_name="jobs", database_id=world["jobs"].id)
    db.session.add(ds)
    db.session.flush()
    pie = Slice(slice_name="Failed jobs by line", viz_type="pie", datasource_id=ds.id, datasource_type="table",
                params=json.dumps({"viz_type": "pie", "groupby": ["LINE"], "row_limit": 100,
                                   "metric": {"aggregate": "SUM", "column": {"column_name": "n"}, "label": "SUM(n)"},
                                   "adhoc_filters": [{"expressionType": "SIMPLE", "clause": "WHERE", "subject": "STATUS",
                                                      "operator": "==", "comparator": "FAILED"}]}))
    mixed = Slice(slice_name="Failures and rate", viz_type="pivot_table_v2", datasource_id=ds.id,
                  datasource_type="table", params=json.dumps({"viz_type": "pivot_table_v2"}),
                  query_context='{"stale": true}')
    two = Slice(slice_name="Failures and rate per hour", viz_type="mixed_timeseries", datasource_id=ds.id,
                datasource_type="table", params=json.dumps({
                    "viz_type": "mixed_timeseries", "x_axis": "t",
                    "metrics": [{"aggregate": "SUM", "column": {"column_name": "failed"}, "label": "SUM(failed)"}],
                    "metrics_b": [{"aggregate": "AVG", "column": {"column_name": "rate"}, "label": "AVG(rate)"}]}))
    db.session.add_all([pie, mixed, two])
    db.session.commit()
    pie_id, mixed_id, ds_id, two_id = pie.id, mixed.id, ds.id, two.id
    try:
        with acting_as("admin"):
            assert refresh_query_context(pie_id) is True
            assert refresh_query_context(pie_id, keep_existing=True) is False           # one of its own: kept
            assert refresh_query_context(mixed_id) is False
            assert refresh_query_context(two_id) is True
        both = json.loads(db.session.get(Slice, two_id).query_context)["queries"]
        assert [q["metrics"][0]["label"] for q in both] == ["SUM(failed)", "AVG(rate)"] and both[1]["columns"] == ["t"]
        qc = json.loads(db.session.get(Slice, pie_id).query_context)
        q = qc["queries"][0]
        assert q["columns"] == ["LINE"] and q["metrics"][0]["label"] == "SUM(n)"
        assert q["filters"] == [{"col": "STATUS", "op": "==", "val": "FAILED"}] and qc["datasource"]["id"] == ds_id
        assert db.session.get(Slice, mixed_id).query_context is None                 # never a stale one
    finally:
        db.session.rollback()
        for obj in (db.session.get(Slice, pie_id), db.session.get(Slice, mixed_id), db.session.get(Slice, two_id),
                    db.session.get(SqlaTable, ds_id)):
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()
