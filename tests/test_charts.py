"""The nightly look at the team's Superset charts (0.7): each chart's metrics per day over five weeks with its own
dimensions and conditions (a chart data query), the last full day against the same weekday of the four weeks before
per series (high, low, normal; data stopped: stale, never "low"); charts left out with their reason; the agent's
tool gives the findings to the users who may see the chart (a look now, as them, with row-level security, written
nowhere); the knowledge pieces and the dashboard pages carry no figure; what a chart shows is written once per input."""

from __future__ import annotations

import datetime as dt
import json

import pytest

NOW = dt.datetime(2026, 9, 24, 23, 30)
DAY = dt.date(2026, 9, 23)                          # the last full day before NOW


def _ms(d: dt.date) -> int:
    return int(dt.datetime.combine(d, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _rows(spike: float = 100.0, gone: str | None = None, last: dt.date = DAY) -> list[dict]:
    """Five weeks of daily rows of two applications: BILLING about 10 a day, PAYROLL about 50; BILLING `spike` on
    DAY; `gone`: an application without rows on DAY."""
    rows = []
    d = DAY - dt.timedelta(days=28)
    while d <= last:
        for app, usual in (("BILLING", 10.0), ("PAYROLL", 50.0)):
            v = usual + (d.day % 3)
            if d == DAY and app == "BILLING":
                v = spike
            if d == DAY and app == gone:
                continue
            rows.append({"ts": _ms(d), "APPLICATION": app, "failed": v})
        d += dt.timedelta(days=1)
    return rows


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """No wait between the queries of a look (the real rate: charts.max_requests_per_minute)."""
    from supagent import settings

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k: 60_000 if k == "charts.max_requests_per_minute" else real(k))


def fake(rows: list[dict], first: dt.date = DAY - dt.timedelta(days=28), last: dt.date = DAY):
    """run_query for a look: the dataset's first and last day (the extent query), else the chart's rows."""
    def run(body):
        if body["queries"][0]["metrics"][0].get("label") == "first":
            return [{"first": _ms(first), "last": _ms(last)}] if last is not None else []
        return rows
    return run


@pytest.fixture()
def lab(app):
    with app.app_context():
        from superset.connectors.sqla.models import SqlaTable, SqlMetric, TableColumn
        from superset.extensions import db, security_manager as sm
        from superset.models.core import Database
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        base = Database(database_name="charts lab", sqlalchemy_uri="sqlite://")
        db.session.add(base)
        db.session.flush()
        jobs = SqlaTable(table_name="chart_jobs", database_id=base.id, main_dttm_col="ts",
                         columns=[TableColumn(column_name="ts", is_dttm=True, type="TIMESTAMP"),
                                  TableColumn(column_name="APPLICATION", type="VARCHAR", description="the application"),
                                  TableColumn(column_name="STATUS", type="VARCHAR")],
                         metrics=[SqlMetric(metric_name="count", expression="COUNT(*)", description="jobs")])
        flat = SqlaTable(table_name="chart_flat", database_id=base.id,
                         columns=[TableColumn(column_name="APPLICATION", type="VARCHAR")])
        db.session.add_all([jobs, flat])
        db.session.flush()

        def chart(name, viz, ds, params):
            return Slice(slice_name=name, viz_type=viz, datasource_id=ds.id, datasource_type="table",
                         params=json.dumps(params))
        failed = {"label": "failed", "expressionType": "SQL", "sqlExpression": "COUNT(*)"}
        line = chart("Failed jobs per application", "echarts_timeseries_line", jobs, {
            "x_axis": "ts", "metrics": [failed], "groupby": ["APPLICATION"], "time_range": "Last week",
            "adhoc_filters": [{"expressionType": "SIMPLE", "clause": "WHERE", "subject": "STATUS", "operator": "==",
                               "comparator": "FAILED"},
                              {"expressionType": "SIMPLE", "clause": "WHERE", "subject": "ts",
                               "operator": "TEMPORAL_RANGE", "comparator": "Last week"},
                              {"expressionType": "SQL", "clause": "WHERE", "sqlExpression": "ENV <> 'UAT'"}]})
        raw = chart("Last failures", "table", jobs, {"query_mode": "raw", "all_columns": ["ts", "STATUS"]})
        pie = chart("Jobs per app (no time)", "pie", flat, {"metric": "count", "groupby": ["APPLICATION"]})
        board = Dashboard(dashboard_title="Night batch health", slices=[line, raw, pie])
        db.session.add_all([line, raw, pie, board])
        db.session.commit()
        role = sm.find_role("chart readers") or sm.add_role("chart readers")
        pvm = sm.find_permission_view_menu("datasource_access", jobs.perm) or \
            sm.add_permission_view_menu("datasource_access", jobs.perm)
        sm.add_permission_role(role, pvm)
        alice = sm.find_user(username="alice")
        alice.roles.append(role)
        db.session.commit()
        ids = {"line": line.id, "raw": raw.id, "pie": pie.id, "board": board.id, "jobs": jobs.id, "flat": flat.id,
               "base": base.id}
        db.session.remove()
    yield ids
    with app.app_context():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db, security_manager as sm
        from superset.models.core import Database
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        from supagent.models import ChartScan

        alice = sm.find_user(username="alice")
        alice.roles = [r for r in alice.roles if r.name != "chart readers"]
        db.session.query(ChartScan).delete()
        for model, key in ((Dashboard, "board"), (Slice, "line"), (Slice, "raw"), (Slice, "pie"),
                           (SqlaTable, "jobs"), (SqlaTable, "flat"), (Database, "base")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()
        db.session.remove()


def test_a_chart_is_read_as_its_metrics_per_day_with_its_own_conditions(app, lab):
    with app.app_context():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db
        from superset.models.slice import Slice

        from supagent.knowledge import charts as K

        line, jobs = db.session.get(Slice, lab["line"]), db.session.get(SqlaTable, lab["jobs"])
        start, end = dt.datetime(2026, 8, 26), dt.datetime(2026, 9, 24)
        body, tc, dims, metrics = K.query_body(line, jobs, start, end)
        q = body["queries"][0]
        assert (tc, dims, metrics) == ("ts", ["APPLICATION"], ["failed"])
        assert q["columns"][0] == {"timeGrain": "P1D", "columnType": "BASE_AXIS", "sqlExpression": "ts", "label": "ts",
                                   "expressionType": "SQL"} and q["columns"][1:] == ["APPLICATION"]
        assert {"col": "STATUS", "op": "==", "val": "FAILED"} in q["filters"]
        assert {"col": "ts", "op": "TEMPORAL_RANGE", "val": "2026-08-26T00:00:00 : 2026-09-24T00:00:00"} in q["filters"]
        assert len([f for f in q["filters"] if f["op"] == "TEMPORAL_RANGE"]) == 1      # its own range left out
        assert q["extras"] == {"where": "(ENV <> 'UAT')"} and body["datasource"] == {"id": jobs.id, "type": "table"}
        from superset.charts.schemas import ChartDataQueryContextSchema

        ctx = ChartDataQueryContextSchema().load(body)                  # what Superset's chart data API accepts
        (qo,) = ctx.queries
        assert qo.metrics == [q["metrics"][0]] and qo.columns[0]["timeGrain"] == "P1D" and qo.row_limit == K.ROW_LIMIT
        params = json.loads(line.params)
        line.params = json.dumps({**params, "adhoc_filters": params["adhoc_filters"] + [
            {"expressionType": "SIMPLE", "clause": "WHERE", "subject": "ts", "operator": ">=", "comparator": "2026-09-01"}]})
        q = K.query_body(line, jobs, start, end)[0]["queries"][0]                       # open-ended: compared
        assert not [f for f in q["filters"] if f["col"] == "ts" and f["op"] != "TEMPORAL_RANGE"]
        line.params = json.dumps({**params, "adhoc_filters": params["adhoc_filters"] + [
            {"expressionType": "SIMPLE", "clause": "WHERE", "subject": "ts", "operator": "<", "comparator": "2026-09-01"}]})
        with pytest.raises(K.ChartSkip, match="fixed period that ended on 2026-08-31"):
            K.query_body(line, jobs, start, end)
        line.params = json.dumps({**params, "time_range": "2026-09-01 : 2026-09-24"})
        assert K.query_body(line, jobs, start, end)                                      # its period has the day
        line.params = json.dumps({**params, "time_range": "2026-08-01T00:00:00 : 2026-09-01T00:00:00"})
        with pytest.raises(K.ChartSkip, match="ended on 2026-08-31"):
            K.query_body(line, jobs, start, end)
        db.session.rollback()
        for key, why in (("raw", "raw records"), ("pie", "no time column")):
            slc = db.session.get(Slice, lab[key])
            with pytest.raises(K.ChartSkip, match=why):
                K.query_body(slc, slc.datasource, start, end)


def test_the_last_day_against_the_same_weekday_of_the_weeks_before():
    from supagent.knowledge import charts as K

    res = K.analyze(_rows(spike=100.0), "ts", ["APPLICATION"], ["failed"], DAY)
    assert res["status"] == "ok" and res["checked"] == 2
    (f,) = res["findings"]
    assert f["series"] == {"APPLICATION": "BILLING"} and f["verdict"] == "high" and f["now"] == 100.0
    assert len(f["previous_weeks"]) == 4 and f["change_pct"] > 500
    assert K.analyze(_rows(spike=11.0), "ts", ["APPLICATION"], ["failed"], DAY)["findings"] == []   # normal
    low = K.analyze(_rows(spike=11.0, gone="PAYROLL"), "ts", ["APPLICATION"], ["failed"], DAY)
    assert [(x["series"]["APPLICATION"], x["verdict"], x["now"]) for x in low["findings"]] == [("PAYROLL", "low", 0.0)]
    stale = K.analyze(_rows(last=DAY - dt.timedelta(days=2)), "ts", ["APPLICATION"], ["failed"], DAY)
    assert stale["status"] == "stale" and stale["findings"] == [] and "stops on 2026-09-21" in stale["reason"]
    assert K.analyze([], "ts", [], ["failed"], DAY)["status"] == "stale"
    iso = [{**r, "ts": dt.datetime.utcfromtimestamp(r["ts"] / 1000).isoformat()} for r in _rows()]
    assert K.analyze(iso, "ts", ["APPLICATION"], ["failed"], DAY)["findings"][0]["verdict"] == "high"
    # no row on a day the dataset has (BREACH IS TRUE: no breach) is no event, not data that stopped
    quiet = K.analyze([], "ts", ["BOOK"], ["breaches"], DAY, first=DAY - dt.timedelta(days=28), last=DAY)
    assert quiet["status"] == "ok" and "no event" in quiet["reason"]
    gone = K.analyze(_rows(), "ts", ["APPLICATION"], ["failed"], DAY, last=DAY - dt.timedelta(days=1))
    assert gone["status"] == "stale" and "stops on 2026-09-22" in gone["reason"]          # the dataset's own last day
    few = [{"ts": _ms(DAY - dt.timedelta(days=7 * k)), "APPLICATION": "X", "failed": 1} for k in range(1, 5)]
    assert K.analyze(few, "ts", ["APPLICATION"], ["failed"], DAY, last=DAY)["findings"] == []   # 0 against 1: a few
    young = [r for r in _rows() if _ms(DAY - dt.timedelta(days=22)) <= r["ts"]]               # data from 1 Sep only
    res = K.analyze(young, "ts", ["APPLICATION"], ["failed"], DAY, first=DAY - dt.timedelta(days=22), last=DAY)
    assert len(res["findings"][0]["previous_weeks"]) == 3                                  # no week before the data
    avg = [{"ts": r["ts"], "APPLICATION": r["APPLICATION"], "latency": r["failed"] + 0.5} for r in _rows()
           if not (r["APPLICATION"] == "BILLING" and r["ts"] == _ms(DAY))]
    res = K.analyze(avg, "ts", ["APPLICATION"], ["latency"], DAY, last=DAY)
    assert res["checked"] == 1 and res["findings"] == []          # a measure without a value that day: not compared


def test_the_night_writes_each_chart_and_its_reason_within_bounds(app, lab, monkeypatch):
    from supagent.knowledge import charts as K

    monkeypatch.setattr(K, "run_query", fake(_rows(spike=100.0)))
    with app.app_context():
        from superset.extensions import db

        from supagent.models import ChartScan

        out = K.scan(now=NOW, chart_ids=[lab["line"], lab["raw"], lab["pie"]])
        assert out["ok"] == 1 and out["skipped"] == 2 and out["anomalies"] == 1
        rows = {r.chart_id: r for r in db.session.query(ChartScan)}
        assert rows[lab["line"]].day == DAY and rows[lab["line"]].findings[0]["verdict"] == "high"
        assert rows[lab["line"]].dashboard_ids == [lab["board"]] and rows[lab["line"]].datasource_id == lab["jobs"]
        assert "raw records" in rows[lab["raw"]].reason and "no time column" in rows[lab["pie"]].reason
        assert K.scan(now=NOW, seconds=0, chart_ids=[lab["line"]])["left"] == 1          # out of time
        monkeypatch.setattr(K, "run_query", lambda body: (_ for _ in ()).throw(RuntimeError("osagg: index gone")))
        assert K.scan(now=NOW, chart_ids=[lab["line"]])["error"] == 1      # the extent not known, then the chart fails
        assert "osagg: index gone" in db.session.query(ChartScan).filter_by(chart_id=lab["line"]).one().reason
        reads = []
        monkeypatch.setattr(K, "run_query", lambda body: reads.append(body) or fake([], last=DAY - dt.timedelta(days=3))(body))
        assert K.scan(now=NOW, chart_ids=[lab["line"]])["stale"] == 1 and len(reads) == 1   # stopped: chart not read
        from supagent import settings

        real = settings.get
        monkeypatch.setattr(settings, "get", lambda k: 1 if k == "learn.stop_after_errors" else real(k))   # (on quick)
        monkeypatch.setattr(K, "run_query", lambda body: (_ for _ in ()).throw(RuntimeError(
            "OpenSearch error: circuit_breaking_exception: [parent] Data too large")))
        out = K.scan(now=NOW, chart_ids=[lab["line"], lab["raw"], lab["pie"]])
        assert out["error"] == 1 and out["left_overloaded"] == 2      # its database said it is overloaded: tomorrow
        db.session.remove()


def test_the_tool_gives_the_findings_to_who_may_see_the_chart(app, lab, monkeypatch):
    from supagent import tools
    from supagent.knowledge import charts as K

    monkeypatch.setattr(K, "run_query", fake(_rows(spike=100.0)))
    with app.app_context():
        from supagent.security import acting_as

        K.scan(now=NOW, chart_ids=[lab["line"], lab["raw"], lab["pie"]])
        monkeypatch.setattr("supagent.agent.now", lambda: NOW)
        with acting_as("alice"):                                         # the jobs dataset only
            r = tools.chart_anomalies(dashboard="night batch health")
            assert r["with_anomalies"] == 1 and r["not_shown"] == 1      # the pie's dataset: not hers
            top = r["charts"][0]
            assert top["chart"] == "Failed jobs per application" and "nightly look" in top["looked"]
            assert top["unusual"][0].startswith("failed for APPLICATION BILLING: 100.0 against usually")
        with acting_as("bob"):
            r = tools.chart_anomalies(dashboard=lab["board"])
            assert r["charts"] == [] and r["not_shown"] == 3
        calls = []
        monkeypatch.setattr(K, "run_query", lambda body: calls.append(1) or fake(_rows(spike=11.0))(body))
        monkeypatch.setattr(K, "may_see", lambda slc: (True, False))     # row-level security: looked at as them
        with acting_as("alice"):
            r = tools.chart_anomalies(chart=lab["line"])
        assert calls and r["charts"][0]["looked"] == "now, as you" and r["charts"][0]["unusual"] == []
        from superset.extensions import db

        from supagent.models import ChartScan

        kept = db.session.query(ChartScan).filter_by(chart_id=lab["line"]).one()
        assert kept.findings[0]["now"] == 100.0                          # the night's row: not written by a user
        assert "error" in tools.chart_anomalies(dashboard="no such board")


def test_what_a_chart_shows_is_written_once_and_no_figure_leaves_the_tool(app, lab, monkeypatch):
    from supagent.knowledge import charts as K

    monkeypatch.setattr(K, "run_query", fake(_rows(spike=100.0)))

    class LLM:
        calls = 0
        read: dict = {}

        def chat(self, messages, max_tokens=None):
            LLM.calls += 1
            facts = json.loads(messages[1]["content"])
            LLM.read[facts["chart"]] = facts
            return {"content": "<think>x</think>Failed jobs per day for each application, last week."}
    with app.app_context():
        from superset.extensions import db
        from superset.models.slice import Slice

        from supagent.knowledge.index import sync
        from supagent.models import Chunk

        K.scan(now=NOW, chart_ids=[lab["line"], lab["raw"], lab["pie"]])
        assert K.understand(LLM())["written"] == 3                       # every chart on a dataset
        facts = LLM.read["Failed jobs per application"]
        assert facts["metrics"][0]["metric"] == "failed" and facts["by"][0]["about"].startswith("the application")
        assert "STATUS == FAILED" in facts["conditions"] and facts["dashboards"] == ["Night batch health"]
        assert K.understand(LLM()).get("llm_calls", 0) == 0 and LLM.calls == 3      # nothing changed: no call
        line = db.session.get(Slice, lab["line"])
        line.slice_name = "Failed jobs per application (prod)"
        db.session.commit()
        assert K.understand(LLM())["written"] == 1                       # its input changed
        sync(("superset:",))
        text = db.session.query(Chunk).filter_by(ref=f"superset:chart:{lab['line']}").one().text
        assert "what it shows (AI-written): Failed jobs per day for each application" in text
        assert "1 series unusual (high): chart_anomalies gives them" in text and "100.0" not in text
        (page,) = [p for p in K.dashboard_pages() if p["title"] == "Dashboard Night batch health"]
        assert "Failed jobs per day for each application" in page["content"] and "100.0" not in page["content"]
        assert "series unusual (high)" in page["content"] and page["database_ids"] == [lab["base"]]
        db.session.remove()
