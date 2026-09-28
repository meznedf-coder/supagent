"""The agent writes catalog entries only on evidence it can check, marks them, takes them back
when the evidence breaks, and never touches what a person changed or deleted."""

from __future__ import annotations

import json

from test_knowledge import world  # noqa: F401  (the fixtures)
from test_search import clean_knowledge  # noqa: F401

RATE = ("SELECT NODE, ROUND(100.0 * COUNT(*) FILTER (WHERE STATUS = 'FAILED') / COUNT(*), 2) AS failure_rate, "
        "COUNT(*) AS n FROM jobs WHERE ts >= '{day}' GROUP BY NODE")
OTHER_RATE = "SELECT COUNT(*) FILTER (WHERE STATUS = 'FAILED') * 1.0 / COUNT(*) AS failure_rate FROM jobs"


def _recipe(world, sql, status="confirmed", uses=1, database="jobs", message_id=None):
    from superset.extensions import db

    from supagent.models import Recipe

    r = Recipe(question="failure rate per node", words="failure node rate", tool="execute_sql",
               database_id=world[database].id, target="jobs", query=sql, signature=str(abs(hash(sql)))[:60],
               status=status, uses=uses, message_id=message_id,
               confirmations=[message_id] if status in ("helpful", "confirmed") and message_id else None)
    db.session.add(r)
    db.session.commit()
    return r


def _entries(origin_prefix):
    from superset.extensions import db

    from supagent.models import Entry

    return db.session.query(Entry).filter(Entry.origin.like(origin_prefix + "%")).order_by(Entry.id).all()


def test_calculated_columns_only(clean_knowledge):
    from supagent.knowledge.autocatalog import named_expressions

    got = named_expressions(RATE.format(day="2026-09-01"))
    assert [(n, t) for n, _e, t in got] == [("failure_rate", ["jobs"])]          # not n = COUNT(*)
    assert "FILTER" in got[0][1].upper() and got[0][1].upper().startswith("ROUND(")
    plain = "SELECT COUNT(*) AS jobs_count, SUM(bytes) AS total_bytes, ROUND(AVG(duration), 2) AS avg_duration FROM jobs"
    assert named_expressions(plain) == []                                       # plain reads are no formula
    case = "SELECT SUM(CASE WHEN STATUS = 'FAILED' THEN 1 ELSE 0 END) AS failed_jobs FROM jobs"
    assert [n for n, _e, _t in named_expressions(case)] == ["failed_jobs"]
    cte = ("WITH t AS (SELECT COUNT(*) AS total_jobs, COUNT(*) FILTER (WHERE STATUS = 'FAILED') AS failed_jobs "
           "FROM jobs) SELECT failed_jobs * 100.0 / total_jobs AS pct_failed FROM t")
    assert [(n, t) for n, _e, t in named_expressions(cte)] == [("failed_jobs", ["jobs"])]   # not the CTE, not
    #                                                                     a formula built on other results
    assert [n for n, _e, _t in named_expressions("SELECT duration_ms / 1000.0 AS duration_s FROM jobs")] == \
        ["duration_s"]
    assert named_expressions("this is not SQL (") == []


def test_formula_needs_confirmed_answers_and_one_expression(clean_knowledge):
    from superset.extensions import db

    from supagent.knowledge.autocatalog import formulas
    from supagent.knowledge.catalog import AGENT

    world = clean_knowledge
    _recipe(world, RATE.format(day="2026-09-01"), message_id=11)
    assert formulas()["added"] == []                                  # one confirmed answer: not certain yet
    _recipe(world, RATE.format(day="2026-09-20"), message_id=12)       # another day, the same formula
    out = formulas()
    assert len(out["added"]) == 1
    (e,) = _entries("formula:")
    assert (e.classification, e.updated_by, e.created_by, e.category) == ("formula", AGENT, AGENT,
                                                                          "Formulas learned from answers")
    assert e.content.startswith("failure_rate = ROUND(") and "Table: jobs (database jobs)" in e.content
    assert e.evidence["confirmed"] == 2 and e.evidence["database_id"] == world["jobs"].id
    assert e.evidence["messages"] == [11, 12]
    assert formulas() == {"added": [], "updated": [], "withdrawn": []}  # nothing new: no new version
    assert e.version == 1

    other = _recipe(world, OTHER_RATE, status="helpful")               # the same name, another calculation
    assert len(formulas()["withdrawn"]) == 1
    db.session.refresh(e)
    assert e.deleted_at is not None and "another expression" in e.evidence["withdrawn"]
    db.session.delete(other)
    db.session.commit()
    assert len(formulas()["added"]) == 1                              # certain again: the same entry back
    db.session.refresh(e)
    assert e.deleted_at is None and len(_entries("formula:")) == 1

    _recipe(world, RATE.format(day="2026-09-21"), status="rejected")   # an answer marked Not helpful
    assert len(formulas()["withdrawn"]) == 1


def test_two_answers_confirming_the_same_query_count_twice(clean_knowledge):
    """Lab acceptance Q: two similar questions answered with the same query shape are one recipe
    (its uses grow); each Helpful counts."""
    from supagent.knowledge.autocatalog import formulas
    from supagent.knowledge.experience import feedback

    from superset.extensions import db

    r = _recipe(clean_knowledge, RATE.format(day="2026-09-23"), status="helpful", message_id=31)
    assert formulas()["added"] == []                                  # marked Helpful once, used once
    r.uses, r.confirmations = 2, [31, 32]                             # a second answer marked Helpful joined it
    db.session.commit()
    assert len(formulas()["added"]) == 1
    feedback(32, 0)                                                    # the second Helpful taken back
    assert r.status == "helpful" and r.confirmations == [31]
    assert len(formulas()["withdrawn"]) == 1


def test_confirmed_once_and_used_three_times(clean_knowledge):
    from supagent.knowledge.autocatalog import formulas

    _recipe(clean_knowledge, RATE.format(day="2026-09-01"), uses=3)
    assert len(formulas()["added"]) == 1


def test_people_win(clean_knowledge):
    from superset.extensions import db

    from supagent.knowledge.autocatalog import formulas
    from supagent.knowledge.catalog import delete_entry, save_entry

    world = clean_knowledge
    _recipe(world, RATE.format(day="2026-09-01"))
    _recipe(world, RATE.format(day="2026-09-02"))
    case = "SELECT SUM(CASE WHEN STATUS = 'FAILED' THEN 1 ELSE 0 END) AS failed_jobs FROM jobs"
    _recipe(world, case)
    _recipe(world, case)
    assert len(formulas()["added"]) == 2
    rate, failed = _entries("formula:")
    save_entry({"title": rate.title, "classification": "formula", "category": "Ops",
                "content": "failure_rate = failed jobs / all jobs, in percent"}, by="admin", entry_id=rate.id)
    delete_entry(failed.id, by="admin")
    _recipe(world, RATE.format(day="2026-09-03"), status="rejected")     # its evidence breaks...
    assert formulas() == {"added": [], "updated": [], "withdrawn": []}  # ...but it is the admin's now
    db.session.refresh(rate)
    db.session.refresh(failed)
    assert rate.deleted_at is None and rate.updated_by == "admin" and rate.content.startswith("failure_rate = failed")
    assert failed.deleted_at is not None and len(_entries("formula:")) == 2    # deleted by a person: never again


def test_a_persons_term_wins_over_the_agents(clean_knowledge):
    from supagent.knowledge.catalog import AGENT, conflicts, invalidate, load_catalog, save_entry

    save_entry({"title": "Ops terms", "classification": "glossary", "content": "MTTR: time to repair (people)"},
               by="admin")
    save_entry({"title": "Glossary from runbook", "classification": "glossary",
                "content": "MTTR: something else (agent)\nSLO: the objective (agent)"}, by=AGENT, origin="doc:1",
               evidence={"source": "document"})                       # newer, yet the person's term is kept
    invalidate()
    glossary = load_catalog()["glossary"]
    assert glossary["MTTR"] == "time to repair (people)" and glossary["SLO"] == "the objective (agent)"
    assert conflicts()["conflicts"][0]["kept"] == "Ops terms"


DOC = """Operations glossary
MTTR: the mean time to repair an incident, in minutes.
The pool saturation is monitored daily.
| SLO | The objective of availability per month |
Restart the pool when it is full.
"""


class FakeLLM:
    def __init__(self, items):
        self.items, self.calls = items, 0

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls += 1
        return {"content": "<think>looking</think>" + json.dumps(self.items)}


def test_definitions_are_quoted_from_the_document(clean_knowledge):
    import yaml
    from superset.extensions import db

    from supagent.knowledge.autocatalog import candidate_lines, definitions_from_docs
    from supagent.models import Doc

    lines = candidate_lines(DOC)
    assert "MTTR:" in lines and "| SLO |" in lines and "monitored" not in lines and "Restart" not in lines
    d = Doc(kind="upload", title="Ops glossary", content=DOC, status="ok", content_hash="h1", enabled=True)
    db.session.add(d)
    db.session.commit()
    llm = FakeLLM([{"term": "MTTR", "quote": "MTTR: the mean time to repair an incident, in minutes."},
                   {"term": "pool saturation", "quote": "The pool saturation is monitored daily."},   # no definition
                   {"term": "RPO", "quote": "RPO is the recovery point objective of the backups."},   # not in the text
                   {"term": "SLO", "quote": "SLO | The objective of availability per month"}])
    out = definitions_from_docs(llm=llm)
    assert out["refused"] == 2 and out["added"] == [f"doc:{d.id}"] and llm.calls == 1
    (e,) = _entries("doc:")
    assert (e.classification, e.title, e.fmt) == ("glossary", "Glossary from Ops glossary", "yaml")
    assert yaml.safe_load(e.content) == {"MTTR": "the mean time to repair an incident, in minutes.",
                                         "SLO": "The objective of availability per month"}   # the document's words
    assert e.evidence["doc_id"] == d.id and d.learned_hash == "h1"
    definitions_from_docs(llm=llm)
    assert llm.calls == 1                                              # read once per content
    quiet = FakeLLM([{"term": "The MTTR (mean time to repair)", "quote": "- MTTR: the mean time to repair an "
                                                                          "incident, in minutes."}])
    d.content, d.content_hash = DOC + "Call the on-call engineer: extension 4242.\n", "h2"
    db.session.commit()
    definitions_from_docs(llm=quiet)                                   # the LLM's decorations are removed
    db.session.refresh(e)
    assert quiet.calls == 1 and set(yaml.safe_load(e.content)) == {"MTTR", "SLO"}   # still word for word there
    d.content, d.content_hash = DOC.replace("MTTR: the mean", "MTTR, roughly the mean"), "h3"
    db.session.commit()
    definitions_from_docs(llm=FakeLLM([]))
    db.session.refresh(e)
    assert set(yaml.safe_load(e.content)) == {"SLO"}                   # gone from the document: gone here
    d.enabled = False
    db.session.commit()
    assert definitions_from_docs(llm=llm)["withdrawn"] == [f"doc:{d.id}"]


def test_approved_team_memory_moves_into_the_catalog(clean_knowledge):
    from superset.extensions import db, security_manager as sm

    from supagent.agent import rule_line
    from supagent.knowledge.autocatalog import team_memory
    from supagent.knowledge.catalog import rules
    from supagent.knowledge.memory import add, prompt_block
    from supagent.models import Memory

    bob = sm.find_user(username="bob").id
    rule = add(bob, "Exclude the UAT environment unless the user asks for it", scope="team", kind="rule",
               category="environments", approved_by="admin")
    pref = add(bob, "Show durations in minutes", scope="team", kind="preference", approved_by="admin")
    waiting = add(bob, "Servers named tst-* are test servers", scope="team", kind="fact")    # not approved
    out = team_memory()
    assert out["added"] == [f"memory:{rule.id}"]
    for m in (rule, pref, waiting):
        db.session.refresh(m)
    assert (rule.status, pref.status, waiting.status) == ("catalog", "active", "proposed")
    (e,) = _entries("memory:")
    assert (e.classification, e.category, e.content) == ("rule", "environments", rule.text)
    assert e.evidence["approved_by"] == "admin"
    mine = [r for r in rules() if "UAT" in r["text"]]
    assert len(mine) == 1 and "UAT" not in prompt_block(bob)           # in the prompt once: as a catalog rule
    assert rule_line(mine[0]) == rule.text
    assert team_memory()["added"] == [] and db.session.query(Memory).filter_by(status="catalog").count() == 1


def test_a_formula_is_found_only_by_who_may_query_its_database(clean_knowledge):
    from supagent.knowledge.autocatalog import formulas
    from supagent.knowledge.describe import describe
    from supagent.knowledge.index import sync
    from supagent.knowledge.search import search
    from supagent.security import acting_as

    sql = ("SELECT node, AVG(value) * 100 AS cpu_busy_pct FROM node_cpu_seconds_total WHERE mode <> 'idle' "
           "GROUP BY node")
    _recipe(clean_knowledge, sql, database="metrics")
    _recipe(clean_knowledge, sql, database="metrics")
    assert len(formulas()["added"]) == 1
    sync()
    with acting_as("admin"):
        assert any("cpu_busy_pct" in f["text"] for f in search("cpu busy pct", k=10))
        assert "cpu_busy_pct = AVG(value) * 100" in describe("cpu busy")
    with acting_as("alice"):                                         # may not query the metrics database
        assert not any("cpu_busy_pct" in f["text"] for f in search("cpu busy pct", k=10))
        assert "cpu_busy_pct" not in (describe("cpu busy") or "")


def test_entries_api_marks_the_agents(clean_knowledge, app):
    from conftest import login

    from supagent.knowledge.autocatalog import formulas

    _recipe(clean_knowledge, RATE.format(day="2026-09-01"))
    _recipe(clean_knowledge, RATE.format(day="2026-09-02"))
    formulas()
    with app.test_client() as c:
        login(c, "admin")
        entries = c.get("/supagent/admin/api/entries").get_json()
    (e,) = [x for x in entries["entries"] if x["origin"]]
    assert e["agent"] is True and e["evidence"]["confirmed"] == 2 and "formula" in entries["classifications"]
