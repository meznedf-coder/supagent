"""The plan (governed pipeline): a condition nobody gave is sent back, what the question says in other words is
accepted, the team's rule is added by code (not when the question asks for its value), values are written as
the data has them, the period must be the question's, a limit only for a top N, and the query is built by
code from the plan (osagg and promagg)."""

from __future__ import annotations

import datetime as dt

import pytest

from test_decider import lab  # noqa: F401  (the fixture)

TODAY = dt.date(2026, 9, 24)
DAY = {"start": "2026-09-23 00:00", "end": "2026-09-24 00:00", "source": "question: on 23 September"}


@pytest.fixture()
def pack(lab):
    from supagent.governed.decider import Selection, gather, pack as make_pack
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("How many BILLING jobs failed on 23 September, and the HTTP requests of BILLING?")
        jobs = f"data:{lab['main']}:batch-jobs"
        http = f"data:{lab['metrics']}:http_requests_total"
        sel = Selection(kind="data", needs=[{"what": "jobs", "tables": [jobs]}, {"what": "http", "tables": [http]}],
                        knowledge=[k.subject for k in g.items()])
        yield make_pack(g, sel)


def _plan(steps, kind="answer", **kw):
    from supagent.governed.plan import parse_plan

    plan, err = parse_plan({"kind": kind, "steps": steps, **kw})
    assert plan is not None, err
    return plan


def _jobs_step(where, **kw):
    return {"id": "q1", "table": "T1", "measures": [{"label": "failed jobs", "fn": "count"}], "where": where,
            "period": DAY, **kw}


def test_a_condition_nobody_gave_is_sent_back(pack):
    from supagent.governed.validate import check

    q = "How many BILLING jobs failed on 23 September?"
    ok = check(_plan([_jobs_step([{"field": "APPLICATION", "op": "=", "value": "billing", "source": "question: BILLING jobs"},
                                  {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}])]),
               pack, q, today=TODAY)
    assert ok.ok, ok.errors
    assert any("'billing' written as 'BILLING'" in f for f in ok.fixed)            # the data's own spelling
    assert any("ENV != 'UAT'" in a and "Decider rule" in a for a in ok.added)          # the team's rule, by code
    bad = check(_plan([_jobs_step([{"field": "STATUS", "op": "=", "value": "SUCCESS", "source": "question: jobs"}])]),
                pack, q, today=TODAY)
    assert any("do not say 'SUCCESS'" in e for e in bad.errors)
    none = check(_plan([_jobs_step([{"field": "NODE", "op": "=", "value": "srv-a-1", "source": ""}])]), pack, q,
                 today=TODAY)
    assert any("no source" in e for e in none.errors)


def test_the_rule_is_lifted_when_the_question_asks_for_its_value(pack):
    from supagent.governed.validate import check

    q = "How many BILLING jobs ran in UAT on 23 September?"
    out = check(_plan([_jobs_step([{"field": "ENV", "op": "=", "value": "UAT", "source": "question: in UAT"},
                                   {"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}])]),
                pack, q, today=TODAY)
    assert out.ok and not out.added, out.errors


def test_periods_limits_and_knowledge_sources(pack):
    from supagent.governed.validate import check

    q = "Top 2 applications by failed jobs on 23 September?"
    wrong_day = _jobs_step([], period={"start": "2026-09-22 00:00", "end": "2026-09-23 00:00",
                                       "source": "question: on 23 September"})
    assert any("not within what the question names" in e for e in check(_plan([wrong_day]), pack, q, today=TODAY).errors)
    top = _jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}], by=["APPLICATION"],
                     order=[{"by": "failed jobs", "desc": True}], limit=2, limit_source="question: Top 2")
    assert check(_plan([top]), pack, q, today=TODAY).plan.steps[0].limit == 2
    no_top = dict(top, limit=5, limit_source="question: applications")
    out = check(_plan([no_top]), pack, "Failed jobs per application on 23 September?", today=TODAY)
    assert out.plan.steps[0].limit is None and any("limit 5 removed" in f for f in out.fixed)
    rule = next(k for k in pack.knowledge if k.kind == "rule")
    by_rule = _jobs_step([{"field": "ENV", "op": "!=", "value": "UAT", "source": rule.ref},
                          {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: Failed"}])
    assert check(_plan([by_rule]), pack, "Failed jobs on 23 September?", today=TODAY).ok


def test_what_the_question_names_is_counted(pack):
    """A value the question names, a glossary term it uses, a unit it asks for: in the plan, else sent back;
    a time bucket nobody asked for: removed."""
    from supagent.governed.validate import check

    q = "How many BILLING jobs ran in UAT on 23 September?"
    only_env = check(_plan([_jobs_step([{"field": "ENV", "op": "=", "value": "UAT", "source": "question: in UAT"}])]),
                     pack, q, today=TODAY)
    assert any("names 'BILLING', a value of APPLICATION" in e for e in only_env.errors)
    everything = check(_plan([_jobs_step([])]), pack, q, today=TODAY)
    assert any("'UAT'" in e for e in everything.errors) and any("'BILLING'" in e for e in everything.errors)
    jobs = pack.table("T1")
    jobs.columns["TIER"] = {"type": "keyword", "values": ["CRITICAL", "LOW"], "cardinality": 2}
    words = check(_plan([_jobs_step([{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}])]),
                  pack, "How many BILLING jobs of the critical applications on 23 September?", today=TODAY)
    assert not any("CRITICAL" in e for e in words.errors)       # an ordinary word, not the value as the data writes it
    typed = check(_plan([_jobs_step([{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}])]),
                  pack, "How many BILLING jobs with CRITICAL on 23 September?", today=TODAY)
    assert any("'CRITICAL', a value of TIER" in e for e in typed.errors)
    del jobs.columns["TIER"]
    grouped = _jobs_step([], by=["APPLICATION"])          # a value of a field the plan groups by: counted per value
    assert not any("BILLING" in e for e in check(_plan([grouped]), pack, "Jobs per application, BILLING first, on "
                                                 "23 September?", today=TODAY).errors)
    per_minute = _jobs_step([{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}],
                            bucket="minute")
    out = check(_plan([per_minute]), pack, "BILLING jobs on 23 September?", today=TODAY)
    assert out.ok and out.plan.steps[0].bucket is None and any("bucket minute removed" in f for f in out.fixed)
    hourly = check(_plan([dict(per_minute, bucket="hour")]), pack, "BILLING jobs per hour on 23 September?",
                   today=TODAY)
    assert hourly.plan.steps[0].bucket == "hour"
    duration = {"id": "q1", "table": "T1", "period": DAY, "where": [
        {"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}],
        "measures": [{"label": "avg duration", "fn": "avg", "field": "DURATION_S"}]}
    q = "Average duration of the BILLING jobs on 23 September, in minutes?"
    assert any("set unit 'minutes'" in e for e in check(_plan([duration]), pack, q, today=TODAY).errors)
    duration["measures"][0].update(unit="minutes", source_unit="seconds")
    assert check(_plan([duration]), pack, q, today=TODAY).ok


def test_a_glossary_term_is_counted_as_defined(pack):
    from supagent.governed.decider import Candidate
    from supagent.governed.validate import check

    pack.knowledge.append(Candidate(subject="entry:99", kind="glossary", title="late job (Business)",
                                    text="late job: STATUS = 'LATE' or a job that ended after 06:00.", ref="K9"))
    q = "How many late jobs on 23 September?"
    assert any("defines it as STATUS = 'LATE'" in e for e in check(_plan([_jobs_step([])]), pack, q, today=TODAY).errors)
    late = _jobs_step([{"field": "STATUS", "op": "=", "value": "LATE", "source": "K9"}])
    assert check(_plan([late]), pack, q, today=TODAY).ok
    pack.knowledge.pop()


def test_the_query_is_built_by_code(pack):
    from supagent.governed.compile import compile_step
    from supagent.governed.validate import check

    q = "How many BILLING jobs failed per hour on 23 September, their average duration in minutes, and the share failed?"
    step = _jobs_step([{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}],
                      bucket="hour")
    step["measures"] = [
        {"label": "failed", "fn": "count", "where": [{"field": "STATUS", "op": "=", "value": "FAILED",
                                                       "source": "question: failed"}]},
        {"label": "avg duration", "fn": "avg", "field": "DURATION_S", "unit": "minutes", "source_unit": "seconds",
         "unit_source": "data: the field's name"},
        {"label": "failure rate", "fn": "share", "where": [{"field": "STATUS", "op": "=", "value": "FAILED",
                                                            "source": "question: failed"}]}]
    out = check(_plan([step]), pack, q, today=TODAY)
    assert out.ok, out.errors
    t = pack.table("T1")
    sql = compile_step(out.plan.steps[0], t)
    assert sql.startswith('SELECT DATE_TRUNC(\'hour\', "ts") AS "time", COUNT(*) FILTER (WHERE "STATUS" = \'FAILED\') '
                          'AS "failed", (AVG("DURATION_S")) / 60 AS "avg duration", 100.0 * COUNT(*) FILTER')
    assert "\"ts\" >= '2026-09-23 00:00' AND \"ts\" < '2026-09-24 00:00'" in sql and "\"ENV\" <> 'UAT'" in sql
    assert sql.endswith("GROUP BY DATE_TRUNC('hour', \"ts\") ORDER BY \"time\" ASC LIMIT 1000")


def test_a_metric_step_and_values_found_by_an_earlier_step(pack):
    from supagent.governed.compile import CompileError, compile_step
    from supagent.governed.validate import check

    q = "HTTP requests of the applications with failed jobs on 23 September"
    steps = [_jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed jobs"}],
                        by=["APPLICATION"]),
             {"id": "q2", "table": "T2", "measures": [{"label": "requests", "fn": "increase"}], "by": ["application"],
              "where": [{"field": "application", "op": "in", "value": "q1.APPLICATION", "source": "q1"}],
              "period": DAY}]
    out = check(_plan(steps), pack, q, today=TODAY)
    assert out.ok, out.errors
    t2 = pack.table("T2")
    with pytest.raises(CompileError):
        compile_step(out.plan.steps[1], t2)                     # the values of q1 are not known yet
    sql = compile_step(out.plan.steps[1], t2, {"q1.APPLICATION": ["BILLING", "ORDERS"]})
    assert sql == ("SELECT application, SUM(increase) AS \"requests\" FROM \"http_requests_total\" WHERE ts >= TIMESTAMP "
                   "'2026-09-23 00:00' AND ts < TIMESTAMP '2026-09-24 00:00' AND application IN ('BILLING', 'ORDERS') "
                   "GROUP BY application LIMIT 1000")
    counter_avg = [dict(steps[1], measures=[{"label": "requests", "fn": "avg"}], where=[])]
    assert any("is a counter" in e for e in check(_plan([steps[0]] + counter_avg), pack, q, today=TODAY).errors)


def test_other_kinds_and_bad_plans(pack):
    from supagent.governed.plan import parse_plan, source_of
    from supagent.governed.validate import check

    plan, err = parse_plan({"kind": "answer", "steps": [{"id": "q1", "table": "T1", "measures": [{"label": "x",
                                                                                                    "fn": "median"}]}]})
    assert plan is None and "fn must be one of" in err
    assert check(_plan([], kind="clarify"), pack, "x", today=TODAY).errors
    assert check(_plan([], kind="clarify", question_back="Job failures or HTTP errors?",
                       options=["job failures", "HTTP 5xx"]), pack, "x", today=TODAY).ok
    glossary_like = next(k for k in pack.knowledge if k.kind == "memory")
    assert check(_plan([], kind="explain", knowledge=[glossary_like.ref]), pack, "Which applications are critical?",
                 today=TODAY).ok
    assert source_of("question: 'on 23 September'") == ("question", "on 23 September")
    assert source_of("K3") == ("knowledge", "K3") and source_of("q2") == ("step", "q2") and source_of("") == ("", "")


def test_refs_are_named_and_a_useless_order_is_dropped(pack):
    from supagent.governed.compose import named
    from supagent.governed.validate import check

    rule = next(k for k in pack.knowledge if k.kind == "rule")
    assert named(f"The table T1 has no ZEPHYR; see {rule.ref}.", pack) == \
        f'The table batch-jobs has no ZEPHYR; see "{rule.title}".'
    step = _jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}],
                      order=[{"by": "APPLICATION", "desc": True}])
    out = check(_plan([step]), pack, "How many jobs failed on 23 September?", today=TODAY)
    assert out.ok and out.plan.steps[0].order == [] and any("dropped" in f for f in out.fixed)


def test_what_the_question_asks_per_is_grouped(pack):
    from supagent.governed.validate import check

    q = "How many jobs failed per application on 23 September?"
    flat = _jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}])
    out = check(_plan([flat]), pack, q, today=TODAY)
    assert any("group by APPLICATION" in e for e in out.errors)
    grouped = dict(flat, by=["APPLICATION"])
    assert check(_plan([grouped]), pack, q, today=TODAY).ok
    hourly = "How many jobs failed per hour on 23 September?"
    assert any("set bucket 'hour'" in e for e in check(_plan([flat]), pack, hourly, today=TODAY).errors)
    raw = dict(flat, bucket="hour", by=["ts"])
    out = check(_plan([raw]), pack, hourly, today=TODAY)
    assert out.ok and out.plan.steps[0].by == [] and any("raw time field" in f for f in out.fixed)
    servers = "Which 2 servers had the most failed jobs on 23 September?"          # server: the NODE field
    assert any("group by NODE" in e for e in check(_plan([flat]), pack, servers, today=TODAY).errors)


def test_no_total_of_cut_rows_and_explanations_say_the_knowledge(pack):
    import json

    from supagent.governed.compose import explained, sheet

    plan = _plan([_jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}],
                             by=["APPLICATION"])])
    rows = [{"APPLICATION": f"APP{i}", "failed jobs": 1} for i in range(1000)]
    cut = sheet(plan, {"q1": json.dumps({"columns": ["APPLICATION", "failed jobs"], "rows": rows, "row_count": 1000})},
                pack)
    assert "totals of these rows" not in cut and "no sum of these rows is a total" in cut
    whole = sheet(plan, {"q1": json.dumps({"columns": ["APPLICATION", "failed jobs"], "rows": rows[:3],
                                           "row_count": 3})}, pack)
    assert "totals of these rows: failed jobs 3" in whole
    top = _plan([_jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}],
                            by=["APPLICATION"], limit=1, limit_source="question: which application had the most",
                            order=[{"by": "failed jobs", "desc": True}])])
    first = sheet(top, {"q1": json.dumps({"columns": ["APPLICATION", "failed jobs"], "rows": rows[:1], "row_count": 1,
                                          "truncated": True})}, pack)
    assert "cut:" not in first                       # the top 1 asked is the whole answer (the retail lab: "the
    item = next(k for k in pack.knowledge if k.kind == "rule")    # results only show the first rows")
    assert explained("[K2] rule", [item]).startswith(f"- **{item.title}**: {item.text}")
    good = f"The team counts without UAT: {item.text}"
    assert explained(good, [item]) == good


def test_the_period_the_ranking_the_share_and_the_grain_asked(pack):
    """The period the question names on every step, a top N ordered by what it ranks, a share asked for as a
    measure of its own, a bucket of the grain asked (a weekly review is not per day)."""
    from supagent.governed.validate import check

    billing = {"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING"}
    no_period = {"id": "q1", "table": "T1", "measures": [{"label": "jobs", "fn": "count"}], "where": [billing]}
    assert any("set period on this step" in e for e in check(_plan([no_period]), pack, "BILLING jobs on 23 September?",
                                                             today=TODAY).errors)
    assert not any("set period" in e for e in check(_plan([no_period]), pack, "BILLING jobs?", today=TODAY).errors)
    by_time = _jobs_step([{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}],
                         by=["APPLICATION"], order=[{"by": "APPLICATION", "desc": False}], limit=1,
                         limit_source="question: the most")
    q = "Which application had the most failed jobs on 23 September?"
    assert any("order by the measure it ranks" in e for e in check(_plan([by_time]), pack, q, today=TODAY).errors)
    ranked = dict(by_time, order=[{"by": "failed jobs", "desc": True}])
    assert check(_plan([ranked]), pack, q, today=TODAY).ok
    counts = _jobs_step([billing], measures=[{"label": "jobs", "fn": "count"}, {"label": "failed", "fn": "count", "where": [
        {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failure"}]}])
    q = "The failure rate of the BILLING jobs on 23 September?"
    assert any("the answer never divides two counts" in e for e in check(_plan([counts]), pack, q, today=TODAY).errors)
    share = _jobs_step([billing], measures=[{"label": "failure rate", "fn": "share", "where": [
        {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failure rate"}]}])
    assert check(_plan([share]), pack, q, today=TODAY).ok
    daily = _jobs_step([billing], bucket="day", period={"start": "2026-09-14 00:00", "end": "2026-09-21 00:00",
                                                        "source": "question: the week of 14 to 20 September"})
    out = check(_plan([daily]), pack, "The weekly review of the BILLING jobs, the week of 14 to 20 September?",
                today=TODAY)
    assert out.plan.steps[0].bucket is None and any("it asks per week" in f for f in out.fixed)
    out = check(_plan([dict(daily, bucket="hour")]), pack, "BILLING jobs over time, the week of 14 to 20 September?",
                today=TODAY)
    assert out.plan.steps[0].bucket == "hour"                     # over time: any grain


def test_a_unit_nobody_asked_for_is_not_said(pack):
    """The model put "percent" on a sum and "seconds" on a count: removed by code (the answer's line said a PnL
    was in percent); a unit the question asks for stays."""
    from supagent.governed.validate import check

    q = "How many BILLING jobs failed on 23 September?"
    where = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"},
             {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}]
    plan = _plan([{"id": "q1", "table": "T1", "where": where, "period": DAY,
                   "measures": [{"label": "failed jobs", "fn": "count", "unit": "seconds"}]}])
    ok = check(plan, pack, q, today=TODAY)
    assert plan.steps[0].measures[0].unit is None
    assert any("unit 'seconds' of 'failed jobs' removed" in f for f in ok.fixed)
    same = _plan([{"id": "q1", "table": "T1", "where": where, "period": DAY,     # the retail lab: "count of rows,
                   "measures": [{"label": "failed jobs", "fn": "count", "unit": "seconds",   # in seconds"
                                 "source_unit": "seconds"}]}])
    check(same, pack, q, today=TODAY)
    assert same.steps[0].measures[0].unit is None and same.steps[0].measures[0].source_unit is None
    summed = _plan([{"id": "q1", "table": "T1", "where": where, "period": DAY,        # the domain lab's PnL summed
                     "measures": [{"label": "time", "fn": "sum", "field": "DURATION_S", "unit": "percent",  # "in
                                   "source_unit": "percent"}]}])                                     # percent"
    check(summed, pack, q, today=TODAY)
    assert summed.steps[0].measures[0].unit is None
    seconds = _plan([{"id": "q1", "table": "T1", "where": where, "period": DAY,
                      "measures": [{"label": "time", "fn": "sum", "field": "DURATION_S", "unit": "seconds",
                                    "source_unit": "seconds"}]}])
    pack.table("T1").columns["DURATION_S"]["unit"] = "seconds"           # the dictionary says it: kept
    try:
        check(seconds, pack, q, today=TODAY)
        assert seconds.steps[0].measures[0].unit == "seconds"
    finally:
        pack.table("T1").columns["DURATION_S"].pop("unit", None)


def test_a_day_said_as_an_equality_on_a_date_field_becomes_the_period(pack):
    """COB_DATE = 2026-09-23 finds nothing where dates are stored with a time (the lab: 02:00): on the time field
    it is the period (or goes when the period says that day), on another date field the day's range."""
    from supagent.governed.validate import check

    q = "How many BILLING jobs failed on 23 September?"
    t = pack.table("T1")
    tf = t.time_field
    where = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"},
             {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"},
             {"field": tf, "op": "=", "value": "2026-09-23", "source": "question: on 23 September"}]
    plan = _plan([{"id": "q1", "table": "T1", "measures": [{"label": "failed jobs", "fn": "count"}], "where": where}])
    ok = check(plan, pack, q, today=TODAY)
    step = plan.steps[0]
    assert step.period is not None and step.period.start == "2026-09-23 00:00" and step.period.end == "2026-09-24 00:00"
    assert not any(c.field == tf for c in step.where) and any("made the period" in f for f in ok.fixed)
    plan = _plan([_jobs_step(where)])                        # the period already says that day: the equality goes
    ok = check(plan, pack, q, today=TODAY)
    assert not any(c.field == tf for c in plan.steps[0].where) and any("removed (the period says" in f for f in ok.fixed)


def test_a_code_is_not_said_by_a_part_of_another_code():
    """"FX_OPT_G10 EURUSD delta" names a book: it does not say the desk FX_SPOT (the lab's plan took FX from the
    book's code); "VaR" says the data's VAR_1D_99, "the RATES_EUR desk" says RATES_EUR."""
    from supagent.knowledge.conditions import Support, _value_said

    def said(value, text):
        s = Support()
        s.add(text, people=True)
        return _value_said(value, "DESK", s)

    assert not said("FX_SPOT", "FX_OPT_G10 EURUSD delta, COB 23 September?")
    assert said("FX_OPT_G10", "FX_OPT_G10 EURUSD delta, COB 23 September?")
    assert said("VAR_1D_99", "What was the VaR of the CREDIT_HY desk?")
    assert said("RATES_EUR", "official PnL of the RATES_EUR desk")
    assert said("FX_SPOT", "the fx spot desk")


def test_a_condition_the_model_took_from_a_rule_follows_the_rules_limits(pack):
    """The model copied "BOOK = 'ALL'" from the VaR rule into a step per BOOK (the lab's R2): such a condition goes
    on a step grouped by its field, and where its value cannot be, like the ones code adds."""
    from supagent.governed.validate import check

    rule = next(k for k in pack.knowledge if k.kind == "rule")
    q = "How many BILLING jobs failed on 23 September, per environment?"
    where = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"},
             {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"},
             {"field": "ENV", "op": "=", "value": "PROD", "source": rule.ref}]
    plan = _plan([_jobs_step(where, by=["ENV"])])
    ok = check(plan, pack, q, today=TODAY)
    assert not any(c.field == "ENV" and c.op == "=" for c in plan.steps[0].where)
    assert any("removed: the step is per ENV" in f for f in ok.fixed)


def test_a_count_the_question_qualifies_needs_that_condition(pack):
    """"How many BILLING jobs failed" counted without a condition on the field that says FAILED counts every job
    (the lab, its glossary term missing: all 73,769 jobs said "failed"): sent back."""
    from supagent.governed.validate import check

    q = "How many BILLING jobs failed on 23 September?"
    only_app = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"}]
    bad = check(_plan([_jobs_step(only_app)]), pack, q, today=TODAY)
    assert any("counts 'fail' ones" in e and "FAILED" in e for e in bad.errors)
    failed = only_app + [{"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}]
    good = check(_plan([_jobs_step(failed)]), pack, q, today=TODAY)
    assert not any("counts 'fail' ones" in e for e in good.errors)
    plain = check(_plan([_jobs_step(only_app)]), pack, "How many BILLING jobs ran on 23 September?", today=TODAY)
    assert not any("counts 'fail' ones" in e for e in plain.errors)


def test_a_period_on_the_date_the_question_names(pack):
    """"jobs finished on 23 September": the period on FINISHED_TIME, not on the table's time field; a period field
    that is no date of the table is refused."""
    from supagent.governed.compile import compile_step
    from supagent.governed.validate import check

    t = pack.table("T1")
    t.columns["FINISHED_TIME"] = {"type": "date"}
    try:
        where = [{"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: BILLING jobs"}]
        q = "How many BILLING jobs finished on 23 September?"
        out = check(_plan([_jobs_step(where)]), pack, q, today=TODAY)
        assert out.ok, out.errors
        assert out.plan.steps[0].period.field == "FINISHED_TIME" and any("the period on FINISHED_TIME" in f
                                                                          for f in out.fixed)
        sql = compile_step(out.plan.steps[0], t)
        assert "\"FINISHED_TIME\" >= '2026-09-23 00:00' AND \"FINISHED_TIME\" < '2026-09-24 00:00'" in sql
        plain = check(_plan([_jobs_step(where)]), pack, "How many BILLING jobs failed on 23 September?", today=TODAY)
        assert plain.plan.steps[0].period.field is None                 # no date named: the time field
        t.columns["QUEUED_TIME"] = {"type": "date"}                       # two named: the one next to the period's day
        two = "How many BILLING jobs queued on 22 September finished on 23 September or later?"
        later = where + [{"field": "FINISHED_TIME", "op": ">=", "value": "2026-09-23 00:00",
                          "source": "question: finished on 23 September or later"}]
        step = _jobs_step(later)
        step["period"] = {"start": "2026-09-22 00:00", "end": "2026-09-23 00:00", "source": "question: on 22 September"}
        out = check(_plan([step]), pack, two, today=TODAY)
        assert out.plan.steps[0].period.field == "QUEUED_TIME", out.fixed
        bad = _jobs_step(where)
        bad["period"] = {**DAY, "field": "APPLICATION"}
        out = check(_plan([bad]), pack, q, today=TODAY)
        assert any("period field APPLICATION is not a date field" in e for e in out.errors)
    finally:
        t.columns.pop("FINISHED_TIME", None)
        t.columns.pop("QUEUED_TIME", None)


def test_a_rule_code_adds_is_its_way_round_and_never_the_questions_condition(pack, monkeypatch):
    """The retail lab: "Orders of the channel TEST (CHANNEL = 'TEST') ... never count them (CHANNEL <> 'TEST')"
    was added as CHANNEL = 'TEST' (the words before the field said nothing, the operator was not read), and a
    rule's condition on STATUS hid that the plan had no condition saying the failed ones (36 failed payments
    answered 2)."""
    from supagent.governed.validate import check
    from supagent.knowledge import rulecheck

    rules = [{"title": "QA application", "text": "Jobs of the application ORDERS (APPLICATION = 'ORDERS') are the "
                                                 "QA team's: never count them (APPLICATION <> 'ORDERS')."},
             {"title": "Successes", "text": "Exclude the successful runs (STATUS = 'SUCCESS') from the reruns."}]
    monkeypatch.setattr(rulecheck, "team_rules", lambda: rules)
    rulecheck._CONCERNS.clear()
    out = check(_plan([_jobs_step([])]), pack, "How many jobs failed on 23 September?", today=TODAY)
    assert "q1: APPLICATION != 'ORDERS' (the team's rule \"QA application\")" in out.added
    assert any("STATUS != 'SUCCESS'" in a for a in out.added)
    assert any("counts 'fail' ones" in e and "FAILED" in e for e in out.errors)
    rulecheck._CONCERNS.clear()
    monkeypatch.undo()
    rule = next(k for k in pack.knowledge if k.kind == "rule")              # the model's, from a rule: the same
    taken = check(_plan([_jobs_step([{"field": "STATUS", "op": "=", "value": "SUCCESS", "source": rule.ref}])]), pack,
                  "How many jobs failed on 23 September?", today=TODAY)
    assert any("counts 'fail' ones" in e and "from a team rule does not say it" in e for e in taken.errors)


def test_a_value_a_rule_leaves_out_is_never_the_one_kept(pack):
    """The model's condition from a rule that leaves ENV = 'UAT' out, written ENV = 'UAT': turned round, unless
    the question asks for UAT."""
    from supagent.governed.validate import check

    rule = next(k for k in pack.knowledge if k.kind == "rule")
    failed = {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}
    plan = _plan([_jobs_step([failed, {"field": "ENV", "op": "=", "value": "UAT", "source": rule.ref}])])
    out = check(plan, pack, "How many jobs failed on 23 September?", today=TODAY)
    assert [(c.op, c.value) for c in plan.steps[0].where if c.field == "ENV"] == [("!=", "UAT")]
    assert any("written !=" in f for f in out.fixed)
    asked = _plan([_jobs_step([failed, {"field": "ENV", "op": "=", "value": "UAT", "source": rule.ref}])])
    check(asked, pack, "How many jobs failed in UAT on 23 September?", today=TODAY)
    assert [(c.op, c.value) for c in asked.steps[0].where if c.field == "ENV"] == [("=", "UAT")]


def test_a_value_said_before_the_tables_subject_is_named(pack):
    """"How many test orders were placed in September?": the governed plan counted every order (10,579; the
    retail lab's dev half): a value in any case just before the table's subject is named like "TEST" is; an
    ordinary word elsewhere is not; a rule's condition with that value says it."""
    from supagent.governed.validate import check

    failed = {"field": "STATUS", "op": "=", "value": "FAILED", "source": "question: failed"}
    out = check(_plan([_jobs_step([failed])]), pack, "How many billing jobs failed on 23 September?", today=TODAY)
    assert any("names 'BILLING'" in e for e in out.errors), out.errors
    billing = {"field": "APPLICATION", "op": "=", "value": "BILLING", "source": "question: billing jobs"}
    out = check(_plan([_jobs_step([failed, billing])]), pack, "How many billing jobs failed on 23 September?",
                today=TODAY)
    assert not any("names 'BILLING'" in e for e in out.errors), out.errors
    field = check(_plan([_jobs_step([failed])]), pack, "Billing application: jobs failed on 23 September?",
                  today=TODAY)
    assert any("names 'BILLING'" in e for e in field.errors)             # before its field's name: named too
    side = check(_plan([_jobs_step([failed])]), pack, "How many jobs failed on the billing side on 23 September?",
                 today=TODAY)
    assert not any("names 'BILLING'" in e for e in side.errors)          # not before the subject: not named
    rule = next(k for k in pack.knowledge if k.kind == "rule")
    prod = {"field": "ENV", "op": "=", "value": "PROD", "source": rule.ref}
    out = check(_plan([_jobs_step([failed, prod])]), pack, "How many prod jobs failed on 23 September?", today=TODAY)
    assert not any("names 'PROD'" in e for e in out.errors), out.errors   # a rule's condition with it says it
