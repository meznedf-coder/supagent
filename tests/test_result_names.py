"""The chat shows the results an answer is based on, each named: a query run again in the same shape
(the same tool, database, tables, columns and time window: fixed, or with a rule applied) replaces
the earlier try; a result is named by its measures, dimensions and window; two results of the same
name say what differs."""

from __future__ import annotations

import json


def _step(sql: str, rows: list[dict], tool: str = "execute_sql", db_id: int = 1) -> dict:
    cols = list(rows[0]) if rows else ["n"]
    return {"tool": tool, "called": tool, "status": "done", "args": {"request": {"database_id": db_id, "sql": sql}},
            "full": json.dumps({"success": True, "database": "jobs db", "columns": [{"name": c} for c in cols],
                                "rows": rows})}


DAY = """"ts" >= '2026-09-23 00:00' AND "ts" < '2026-09-24 00:00'"""
ROWS = [{"APP": "BILLING", "FAILED": 12}, {"APP": "PAYROLL", "FAILED": 7}]


def test_a_query_run_again_replaces_its_try(ctx):
    from supagent.runner import _results_of

    first = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "STATUS" = 'FAILED' AND {DAY} GROUP BY 1""",
                  ROWS)
    again = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "STATUS" = 'FAILED' AND "ENV" <> 'UAT'
                      AND {DAY} GROUP BY 1""", ROWS)
    out = _results_of([first, again])
    assert len(out) == 1 and "ENV" in out[0]["sql"]
    assert out[0]["title"] == "FAILED by APP · 23 Sep"


def test_two_days_are_two_results_named_by_their_day(ctx):
    from supagent.runner import _results_of

    d22 = _step("""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ts" >= '2026-09-22 00:00'
                   AND "ts" < '2026-09-23 00:00' GROUP BY 1""", ROWS)
    d23 = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE {DAY} GROUP BY 1""", ROWS)
    night = _step("""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ts" >= '2026-09-23 00:00'
                     AND "ts" < '2026-09-23 06:00' GROUP BY 1""", ROWS)
    week = _step("""SELECT "t", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ts" >= '2026-09-20 00:00'
                    AND "ts" < '2026-09-25 00:00' GROUP BY 1""", [{"t": "2026-09-20", "FAILED": 3}])
    out = _results_of([d22, d23, night, week])
    assert [r["title"] for r in out] == ["FAILED by APP · 22 Sep", "FAILED by APP · 23 Sep",
                                         "FAILED by APP · 23 Sep 00:00-06:00", "FAILED over time · 20-24 Sep"]


def test_results_of_the_same_name_say_what_differs(ctx):
    from supagent.runner import _results_of

    prod = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ENV" = 'PROD' AND {DAY} GROUP BY 1""", ROWS)
    uat = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ENV" = 'UAT' AND {DAY} GROUP BY 1""", ROWS,
                db_id=2)
    titles = [r["title"] for r in _results_of([prod, uat])]
    assert titles == ["""FAILED by APP · 23 Sep · "ENV" = 'PROD'""", """FAILED by APP · 23 Sep · "ENV" = 'UAT'"""]


def test_a_comparison_on_the_same_table_keeps_both_results(ctx):
    from supagent.runner import _results_of

    prod = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ENV" = 'PROD' AND {DAY} GROUP BY 1""", ROWS)
    uat = _step(f"""SELECT "APP", COUNT(*) AS "FAILED" FROM "jobs" WHERE "ENV" = 'UAT' AND {DAY} GROUP BY 1""", ROWS)
    out = _results_of([prod, uat])                                   # the same database: not a try, a comparison
    assert [r["title"] for r in out] == ["""FAILED by APP · 23 Sep · "ENV" = 'PROD'""",
                                         """FAILED by APP · 23 Sep · "ENV" = 'UAT'"""]
