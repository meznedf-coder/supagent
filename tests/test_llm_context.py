"""What the LLM is given with a question, end to end (the real instructions and blocks, a scripted
LLM that records what it receives): the team's rules from the catalog, the user's memory and the
team's, where the data is with the descriptions of the fields and metrics, the learned answers,
and the knowledge found for the question (documents, catalog glossary and formulas, the Context
pages). And nothing the user may not see: another user's memory, a Context page of a database the
user may not query."""

from __future__ import annotations

import types

import pytest

from test_agent_loop import ScriptedLLM, say
from test_knowledge import world  # noqa: F401  (the fixture)

QUESTION = "How many jobs failed per node yesterday, and when does the SLA window of the night end?"


@pytest.fixture()
def known(world):
    """Everything the team shares, as it would be after a few weeks."""
    from superset.extensions import db, security_manager as sm

    from supagent import settings
    from supagent.knowledge.context import build_context
    from supagent.knowledge.experience import words
    from supagent.knowledge.index import sync
    from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Recipe, Run

    alice, bob = sm.find_user(username="alice").id, sm.find_user(username="bob").id
    learned_q = "How many jobs failed per node on a given day?"
    rows = [
        Entry(title="Durations", classification="rule", content="Always give durations in minutes.", updated_by="admin"),
        Entry(title="SLA", classification="glossary", content="SLA window: the night batch must end before 06:00.",
              updated_by="admin"),
        Entry(title="failure_rate_pct", classification="formula", updated_by="admin",
              content="failure_rate_pct = 100 * failed jobs / all jobs, per node or per application"),
        Memory(scope="user", user_id=alice, kind="preference", text="Sort the tables by count, largest first",
               status="active"),
        Memory(scope="team", user_id=alice, kind="fact", text="The failed jobs restart at 02:00", status="active"),
        Memory(scope="user", user_id=bob, kind="preference", text="Bob likes pie charts", status="active"),
        Recipe(question=learned_q, words=" ".join(sorted(words(learned_q))), tool="execute_sql",
               database_id=world["jobs"].id, target="jobs", status="confirmed",
               query='SELECT "NODE", COUNT(*) AS failed FROM "jobs" WHERE "STATUS" = \'FAILED\' GROUP BY 1'),
        Doc(kind="upload", title="Night runbook", status="ok", enabled=True,
            content="When jobs failed on a node during the night, the on-call engineer restarts the node. "
                    "The SLA window of the night ends at 06:00."),
    ]
    db.session.add_all(rows)
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    saved_node = (node.description, node.description_source)
    node.description, node.description_source = "The server that ran the job", "curated"
    from supagent.knowledge.freshness import touch

    touch()                                                      # as a person's edit does: every cache made again
    db.session.query(Run).filter(Run.status == "running").update({"status": "done"})
    db.session.commit()
    build_context(reason="test", llm=False)                      # the Context's facts pages
    sync()                                                       # everything searchable
    settings.set_value("search.top_k", 12)
    settings.set_value("search.prompt_chars", 8000)
    yield world
    settings.set_value("search.top_k", 6)
    settings.set_value("search.prompt_chars", 2500)
    db.session.query(ContextPage).delete()
    db.session.query(Run).filter(Run.kind == "context").delete()
    for r in rows:
        db.session.delete(db.session.merge(r))
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    node.description, node.description_source = saved_node
    touch()
    db.session.commit()
    sync()


def _agent():
    from supagent.agent import Agent, ChartGuard

    a = object.__new__(Agent)
    a.username, a.on_step, a.should_stop, a.rich, a.max_steps = "alice", None, None, True, 5
    a.llm = ScriptedLLM([say("Done."), say("Done."), say("Done.")])
    a.names, a.local, a.specs = set(), {}, []
    a.superset = types.SimpleNamespace(schema=lambda name: {}, available=True, error=None, call=lambda n, a: "{}")
    a.guard = ChartGuard(a)
    return a


def _given(user: str, question: str) -> tuple[str, str]:
    """(instructions, what comes with the question) as the LLM received them."""
    from supagent.security import acting_as

    agent = _agent()
    with acting_as(user):
        agent.ask(question)
    sent = agent.llm.seen[0]
    return sent[0]["content"], sent[-1]["content"]


def test_everything_the_team_shares_reaches_the_llm(known):
    system, asked = _given("alice", QUESTION)
    assert "Always give durations in minutes." in system                     # the catalog's rules
    assert "(this user, preference) Sort the tables by count" in asked       # her memory
    assert "(team, fact) The failed jobs restart at 02:00" in asked          # the team's
    assert 'field "NODE"' in asked and "The server that ran the job" in asked   # where the data is, described
    assert "Ways that answered similar questions before" in asked            # the learned answer, with its query
    assert 'SELECT "NODE", COUNT(*) AS failed FROM "jobs"' in asked
    assert "[doc] Night runbook" in asked                                    # the documents
    assert "SLA window: the night batch must end before 06:00." in asked     # the catalog's glossary
    assert "[context] Context:" in asked                                     # the Context pages
    assert asked.rstrip().endswith(QUESTION)                                  # then the question


def test_nothing_the_user_may_not_see_reaches_the_llm(known):
    _system, asked = _given("alice", QUESTION + " And the CPU of the servers?")
    assert "Bob likes pie charts" not in asked                                # another user's memory
    assert "Data source: metrics" not in asked and "node_cpu_seconds_total" not in asked   # not her database
    _system, admin = _given("admin", "What was the CPU usage of each server yesterday?")
    assert 'metric "node_cpu_seconds_total"' in admin and "Seconds the CPUs spent in each mode" in admin


def test_the_prompt_command_shows_it(known, app):
    from supagent.cli import supagent

    with app.app_context():
        out = app.test_cli_runner().invoke(supagent, ["prompt", QUESTION, "--user", "alice"])
    assert out.exit_code == 0, out.output
    assert "Always give durations in minutes." in out.output and "[doc] Night runbook" in out.output
    assert "Ways that answered similar questions before" in out.output
