"""The decider (governed pipeline): which tables and knowledge a question needs. Candidates come from the
dictionary directly (no fresh search pieces or vectors needed), a value the question names points to the
tables that hold it, the database a question names wins, the LLM chooses among refs it is given (unknown
refs dropped, the gate's choice when it gives nothing usable), the pack gives the chosen tables in full
with the team's rules on their fields, and the gate learns only from confirmed answers, within bounds."""

from __future__ import annotations

import json

import pytest

from test_knowledge import _database


@pytest.fixture()
def lab(ctx):
    """A jobs index in two databases (main and replica), a metrics database with look-alike metrics, a team
    rule, a glossary term and a team memory."""
    from superset.extensions import db

    from supagent.knowledge.store import source_for, upsert
    from supagent.models import Change, Chunk, Entry, KObject, Memory, Relation, Route, Run, Source

    for model in (Change, Relation, KObject, Run, Source, Chunk, Route):
        db.session.query(model).delete()
    db.session.query(Entry).filter(Entry.title.in_(["Decider rule", "Decider words"])).delete(synchronize_session=False)
    db.session.query(Memory).filter(Memory.text.like("Decider:%")).delete(synchronize_session=False)
    db.session.commit()
    main = _database("decider jobs", "osagg://127.0.0.1:9200/?timezone=Europe/Paris")
    replica = _database("decider jobs replica", "osagg://127.0.0.2:9200/?timezone=Europe/Paris")
    metrics = _database("decider metrics", "promagg://127.0.0.1:9009/prometheus")
    main, replica, metrics = (db.session.merge(d) for d in (main, replica, metrics))
    run = Run(kind="learn", reason="test")
    db.session.add(run)
    db.session.commit()
    fields = {"APPLICATION": ("keyword", ["BILLING", "PAYROLL", "ORDERS"]), "STATUS": ("keyword", ["FAILED", "SUCCESS"]),
              "ENV": ("keyword", ["PROD", "UAT"]), "NODE": ("keyword", ["srv-a-1", "srv-a-2"]),
              "DURATION_S": ("double", None)}
    for d in (main, replica):
        s = source_for(d)
        upsert(run, s, "index", "", "batch-jobs", {"description": "Batch job executions, one document per job run.",
                                                   "stats": {"docs": 1000, "time_field": "ts",
                                                             "time_range": ["2026-06-01", "2026-09-25"]}})
        for name, (dtype, values) in fields.items():
            upsert(run, s, "field", "batch-jobs", name, {"data_type": dtype, "stats": {"values": values or [],
                                                                                         "cardinality": len(values or [])}})
    s = source_for(metrics)
    for name, mtype, labels, help_ in (
            ("http_requests_total", "counter", {"application": ["BILLING", "ORDERS"], "code": ["200", "500"]},
             "HTTP requests served, by application and status code."),
            ("node_cpu_seconds_total", "counter", {"node": ["srv-a-1", "srv-a-2"], "mode": ["idle", "user"]},
             "Seconds the CPUs spent in each mode."),
            ("node_cpu_guest_seconds_total", "counter", {"node": ["srv-a-1"], "mode": ["user"]},
             "Seconds the CPUs spent running guests."),
            ("process_cpu_seconds_total", "counter", {"job": ["exporter"]}, "CPU time of the exporter process."),
            ("job_duration_seconds_bucket", "histogram", {"application": ["BILLING"], "le": ["60", "3600", "+Inf"]},
             "Job duration histogram.")):
        upsert(run, s, "metric", "", name, {"metric_type": mtype, "backend_help": help_,
                                            "stats": {"series": 4, "labels": list(labels)}})
        for label, values in labels.items():
            upsert(run, s, "label", name, label, {"data_type": "string", "stats": {"values": values,
                                                                                   "cardinality": len(values)}})
    db.session.add(Entry(title="Decider rule", classification="rule", content="Exclude the UAT environment "
                                                                              "(ENV = 'UAT') unless asked.",
                         enabled=True, version=1))
    db.session.add(Entry(title="Decider words", classification="glossary", content="night batch window: 00:00 "
                                                                                   "to 06:00 local time.",
                         enabled=True, version=1))
    db.session.add(Memory(text="Decider: the critical applications are BILLING and PAYROLL.", scope="team",
                          kind="fact", status="active"))
    db.session.commit()
    from supagent.knowledge.freshness import touch
    from supagent.knowledge.index import sync

    touch()
    sync()
    ids = [main.id, replica.id, metrics.id]
    yield {"main": main.id, "replica": replica.id, "metrics": metrics.id}
    db.session.rollback()                              # what this world made goes: the next tests start clean
    from superset.models.core import Database

    for model in (Change, Relation, KObject, Run, Source, Chunk, Route):
        db.session.query(model).delete()
    db.session.query(Entry).filter(Entry.title.in_(["Decider rule", "Decider words"])).delete(synchronize_session=False)
    db.session.query(Memory).filter(Memory.text.like("Decider:%")).delete(synchronize_session=False)
    db.session.query(Database).filter(Database.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    touch()


class Scripted:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append((messages, tools))
        return self.reply(messages) if callable(self.reply) else self.reply


def _call(args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "choose_knowledge", "arguments": json.dumps(args)}}]}


def test_candidates_come_from_the_dictionary_and_the_values_named(lab):
    from supagent.governed.decider import gather
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("How many BILLING jobs failed on 23 September?")
    top = g.ranked()[0]
    assert top.table == "batch-jobs" and top.database_id == lab["main"]        # the main database first
    assert top.values == {"BILLING": "APPLICATION"} and top.features["value"] == 1.0
    http = g.tables[f"data:{lab['metrics']}:http_requests_total"]                # BILLING is a label value there
    assert http.features.get("value") and http.score < top.score
    kinds = {k.kind for k in g.knowledge.values()}
    assert {"rule", "memory"} <= kinds and "glossary" not in kinds         # a glossary term only when it is used
    replica = g.tables[f"data:{lab['replica']}:batch-jobs"]
    assert replica.score < top.score                                                # the policy: the first one


def test_the_database_the_question_names_wins(lab):
    from supagent.governed.decider import gather
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("On the decider jobs replica, how many BILLING jobs failed on 23 September?")
    top = g.ranked()[0]
    assert top.database_id == lab["replica"] and top.features["named"] == 1.0


def test_the_llm_chooses_among_refs_and_the_pack_carries_the_rules(lab):
    from supagent.governed.decider import choose, gather, pack
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("How many BILLING jobs failed during the night batch window on 23 September?")

        def reply(messages):
            text = messages[-1]["content"]
            jobs = next(line.split(":")[0] for line in text.splitlines() if '"batch-jobs" in database '
                        f"{lab['main']} " in line)
            words = next(line.split(":")[0] for line in text.splitlines() if "night batch window" in line)
            return _call({"kind": "data", "needs": [{"what": "failed BILLING jobs", "tables": [jobs, "T99"]}],
                          "knowledge": [words, "K77"], "confidence": "high"})

        llm = Scripted(reply)
        sel = choose(g, llm)
        p = pack(g, sel)
    assert sel.by == "llm" and sel.tables() == [f"data:{lab['main']}:batch-jobs"]      # T99 dropped
    assert [k.kind for k in p.knowledge][:1] == ["glossary"] and "K77" not in sel.knowledge
    assert any(k.kind == "rule" for k in p.knowledge)            # the rule on ENV, a field of the chosen table
    jobs = p.tables[0]
    assert jobs.columns["STATUS"]["values"] == ["FAILED", "SUCCESS"] and jobs.time_field == "ts"
    assert list(jobs.columns)[:1] in (["APPLICATION"], ["STATUS"])         # the fields the question matched first
    text = p.text()
    assert "[T1] index \"batch-jobs\"" in text and "Exclude the UAT environment" in text


def test_an_unusable_choice_falls_back_to_the_gate(lab):
    from supagent.governed.decider import choose, gather
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("How many BILLING jobs failed on 23 September?")
        sel = choose(g, Scripted({"role": "assistant", "content": "I think the jobs."}))
    assert sel.by == "gate" and sel.tables()[0] == f"data:{lab['main']}:batch-jobs"


def test_ambiguity_needs_two_known_tables(lab):
    from supagent.governed.decider import choose, gather, parse_choice
    from supagent.security import acting_as

    with acting_as("admin"):
        g = gather("How many errors did BILLING have on 23 September?")
        choose(g, None)
        from supagent.governed.decider import choose_messages

        text = choose_messages(g)[-1]["content"]
    refs = {line.split(":")[0]: line for line in text.splitlines() if line.startswith("T")}
    jobs = next(r for r, line in refs.items() if '"batch-jobs" in database ' f"{lab['main']} " in line)
    http = next(r for r, line in refs.items() if "http_requests_total" in line)
    sel = parse_choice({"kind": "data", "needs": [], "knowledge": [], "confidence": "low",
                        "ambiguous": [{"what": "errors", "options": [jobs, http], "why": "failed jobs or 5xx"},
                                      {"what": "x", "options": [jobs, "T404"]}]}, g)
    assert [a["what"] for a in sel.ambiguous] == ["errors"]


def test_the_gate_learns_only_from_confirmed_answers_within_bounds(lab):
    from superset.extensions import db

    from supagent.governed import gate
    from supagent.models import Route

    good = {"subject": "data:3:http_requests_total", "features": {"search": 1.0, "resolver": 0.3}, "score": 0.9}
    bad = {"subject": "data:1:batch-jobs", "features": {"resolver": 1.0, "rank": 1.0}, "score": 1.5}
    for i in range(8):
        db.session.add(Route(question="errors of BILLING", terms="error billing", shown=[bad, good],
                             used=[good["subject"]], signal="helpful" if i < 6 else None))
    db.session.add(Route(question="x", terms="error", shown=[bad, good], used=[bad["subject"]], signal="not_helpful"))
    db.session.commit()
    out = gate.learn()
    assert out["routes"] == 6 and out["learned"] and out["ordered"] == 1.0
    w = out["weights"]
    assert w["search"] > gate.DEFAULTS["search"] and w["resolver"] < gate.DEFAULTS["resolver"]
    for k, v in w.items():
        lo, hi = gate._bounds(k)
        assert lo <= v <= hi
    prior = gate.priors(["error", "billing"])
    assert prior[good["subject"]] > 0.5 and bad["subject"] not in prior          # not_helpful teaches nothing


def test_the_route_is_recorded_and_confirmed(lab):
    from superset.extensions import db

    from supagent.governed import gate
    from supagent.governed.decider import decide
    from supagent.models import Route
    from supagent.security import acting_as

    with acting_as("admin"):
        d = decide("How many BILLING jobs failed on 23 September?", llm=None)
    gate.used(d.route_id, d.selection.tables(), message_id=4242)
    assert gate.confirm(4242, "helpful")
    r = db.session.get(Route, d.route_id)
    assert r.signal == "helpful" and r.used == [f"data:{lab['main']}:batch-jobs"] and r.shown[0]["features"]
