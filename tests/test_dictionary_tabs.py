"""The Data dictionary's Knowledge tab (catalog, documents and sites, team memory, read only) and
what the agent learned by itself (its catalog entries, where the data is from the answers, the AI
descriptions to verify, the relations measured), for the users of the AI Agent role: each one sees
only what concerns the databases they may query."""

from __future__ import annotations

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


def _get(app, user: str, path: str) -> dict:
    """A request as `user`, in its own app context (the fixture's context would keep the first
    user's roles in flask.g for the next requests: in a real server each request has its own)."""
    with app.app_context(), app.test_client() as c:
        login(c, user)
        return c.get(path).get_json()


def _setup(world):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.catalog import AGENT
    from supagent.models import Association, Doc, Entry, KObject, Memory

    jobs, metrics = world["jobs"], world["metrics"]
    alice = sm.find_user(username="alice").id
    rows = [
        Entry(title="Failure rate", classification="formula", content="failure_rate = failed / all",
              updated_by="admin", created_by="admin"),
        Entry(title="cpu_busy_pct (agent)", classification="formula", content="100 * busy / all", updated_by=AGENT,
              created_by=AGENT, origin="formula:cpu", evidence={"database_id": metrics.id, "answers": [1, 2]}),
        Entry(title="jobs_per_node (agent)", classification="formula", content="COUNT(*) BY NODE", updated_by=AGENT,
              created_by=AGENT, origin="formula:jobs", evidence={"database_id": jobs.id, "answers": [3, 4]}),
        Doc(kind="upload", title="Runbook", content="Step 1. " * 400, status="ok", enabled=True),
        Doc(kind="url", title="Old wiki", content="gone", status="ok", enabled=False),
        Memory(scope="team", user_id=alice, kind="rule", text="Durations in minutes", status="active"),
        Memory(scope="team", user_id=alice, kind="fact", text="Waiting for an admin", status="proposed"),
        Memory(scope="user", user_id=alice, kind="preference", text="Alice's own", status="active"),
        Association(word="cpu", database_id=metrics.id, kind="metric", name="node_cpu_seconds_total", uses=5),
        Association(word="fail", database_id=jobs.id, kind="index", name="jobs", uses=9),
    ]
    db.session.add_all(rows)
    node = db.session.query(KObject).filter_by(kind="field", parent="jobs", name="NODE").one()
    node.description, node.description_source, node.verified = "Server of the run", "llm", False
    db.session.commit()
    return rows


def _cleanup(rows):
    from superset.extensions import db

    for r in rows:
        db.session.delete(db.session.merge(r))
    db.session.commit()


def test_the_knowledge_tab_for_a_user_of_the_ai_agent_role(world, app):
    rows = _setup(world)
    try:
        d = _get(app, "alice", "/supagent/dictionary/api/knowledge")
        titles = [e["title"] for e in d["entries"]]
        assert "Failure rate" in titles and "jobs_per_node (agent)" in titles
        assert "cpu_busy_pct (agent)" not in titles                    # learned on a database alice may not query
        assert [x["title"] for x in d["docs"]] == ["Runbook"]            # the enabled ones
        assert len(d["docs"][0]["excerpt"]) == 1500 and d["docs"][0]["chars"] == 3200
        assert [m["text"] for m in d["team_memory"]] == ["Durations in minutes"]   # approved, team only
        titles = [e["title"] for e in _get(app, "admin", "/supagent/dictionary/api/knowledge")["entries"]]
        assert "cpu_busy_pct (agent)" in titles
    finally:
        _cleanup(rows)


def test_what_the_agent_learned_by_itself(world, app):
    rows = _setup(world)
    try:
        d = _get(app, "alice", "/supagent/dictionary/api/agent_knowledge")
        mine = {e["title"]: e for e in d["entries"]}
        assert "jobs_per_node (agent)" in mine and "cpu_busy_pct (agent)" not in mine
        assert mine["jobs_per_node (agent)"]["evidence"]["answers"] == [3, 4]
        assert [a["word"] for a in d["associations"]] == ["fail"]         # the metrics database: not hers
        assert [(v["database"], v["count"]) for v in d["to_verify"]] == [("jobs", 1)]
        d = _get(app, "bob", "/supagent/dictionary/api/agent_knowledge")   # the role, but no database at all
        assert all(e["evidence"].get("database_id") is None for e in d["entries"])
        assert d["associations"] == [] and d["to_verify"] == []
    finally:
        _cleanup(rows)
