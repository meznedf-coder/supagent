"""The learned answers and query timings of the dictionary page (statuses an admin sets, old
automatic ones hidden, full queries), the upgrade of 0.2.1 learned answers, and the cap on
raw documents of the agent's OpenSearch queries."""

from __future__ import annotations

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


def _login(client, user):
    """The world fixture keeps an app context open: its g must not carry the previous user."""
    from flask import g

    for key in ("_login_user", "user"):
        g.pop(key, None)
    login(client, user)


def _learned(world, status, confirmations=None, question="Failed jobs of a given application"):
    from superset.extensions import db

    from supagent.models import Recipe

    r = Recipe(question=question, words="fail job applic", tool="execute_sql", database_id=world["jobs"].id,
               query="SELECT COUNT(*) FROM jobs WHERE APP = 'X'", signature=f"sig-{status}", status=status, uses=1,
               confirmations=confirmations, generic=True)
    db.session.add(r)
    db.session.commit()
    return r.id


def test_only_helpful_ones_are_listed_and_admins_decide(world, app):
    from superset.extensions import db

    from supagent.models import Recipe

    db.session.query(Recipe).delete()
    db.session.commit()
    helpful, old = _learned(world, "helpful", [41]), _learned(world, "auto")
    with app.test_client() as c:
        _login(c, "alice")
        listed = c.get("/supagent/dictionary/api/recipes").get_json()["recipes"]
        assert [(r["id"], r["status"], r["helpful"]) for r in listed] == [(helpful, "helpful", 1)]
        assert c.post(f"/supagent/dictionary/api/recipes/{helpful}", json={"status": "confirmed"}).status_code == 403
    with app.test_client() as c:
        _login(c, "admin")
        for status in ("confirmed", "rejected", "helpful"):
            assert c.post(f"/supagent/dictionary/api/recipes/{helpful}", json={"status": status}).get_json() == \
                {"id": helpful, "status": status}
        assert c.post(f"/supagent/dictionary/api/recipes/{helpful}", json={"status": "auto"}).status_code == 400
        olds = c.get("/supagent/dictionary/api/recipes?status=auto").get_json()["recipes"]
        assert [r["id"] for r in olds] == [old]                      # still there, when asked for


def test_the_upgrade_sends_user_confirmed_answers_to_review(world):
    from superset.extensions import db

    from supagent.models import SCHEMA_VERSION, Meta, Recipe, create_or_upgrade

    db.session.query(Recipe).delete()
    db.session.commit()
    by_users = _learned(world, "confirmed", [51, 52])               # 0.2.1: users marked it Helpful
    by_admin = _learned(world, "confirmed", None)                   # 0.2.1: an admin confirmed it
    auto = _learned(world, "auto")
    db.session.get(Meta, "schema_version").value = "2"
    db.session.commit()
    assert create_or_upgrade() == (2, SCHEMA_VERSION)
    assert [db.session.get(Recipe, i).status for i in (by_users, by_admin, auto)] == ["helpful", "confirmed", "auto"]
    assert create_or_upgrade() == (SCHEMA_VERSION, SCHEMA_VERSION)


def test_the_upgrade_stems_the_stored_words_again(world):
    """0.4.0 stems further (failed, failure -> fail): the words stored before are stemmed again
    and the associations that become the same are merged."""
    from superset.extensions import db

    from supagent.models import SCHEMA_VERSION, Association, Meta, Recipe, create_or_upgrade

    db.session.query(Association).delete()
    db.session.query(Recipe).delete()
    db.session.commit()
    rid = _learned(world, "helpful", question="Failed jobs by failure category")
    db.session.get(Recipe, rid).words = "failed failure category job"
    for word, uses, mids in (("failed", 2, [1, 2]), ("failure", 3, [3]), ("job", 1, [4])):
        db.session.add(Association(word=word, database_id=world["jobs"].id, kind="index", parent="", name="jobs",
                                   uses=uses, messages=mids))
    db.session.get(Meta, "schema_version").value = "3"
    db.session.commit()
    assert create_or_upgrade() == (3, SCHEMA_VERSION)
    assert db.session.get(Recipe, rid).words == "category fail job"
    rows = {a.word: (a.uses, sorted(a.messages)) for a in db.session.query(Association)}
    assert rows == {"fail": (5, [1, 2, 3]), "job": (1, [4])}


def test_timings_show_the_whole_query_and_admins_the_last_one(world, app):
    from superset.extensions import db

    from supagent.knowledge.experience import record_timings
    from supagent.models import QueryStat

    db.session.query(QueryStat).delete()
    db.session.commit()
    long_sql = ("SELECT \"APPLICATION\", COUNT(*) AS n FROM jobs WHERE \"STATUS\" = 'FAILED' AND day = '2026-09-23' "
                + " ".join(f"AND \"F{i}\" <> 'value {i}'" for i in range(30)) + " GROUP BY 1")
    record_timings([{"tool": "execute_sql", "status": "error", "seconds": 3.5, "result": "tool error: " + "x" * 900,
                     "args": {"request": {"database_id": world["jobs"].id, "sql": long_sql}}}])
    with app.test_client() as c:
        _login(c, "alice")
        (t,) = c.get("/supagent/dictionary/api/timings").get_json()["timings"]
        assert '"F29" <> ?' in t["pattern"] and t["pattern"].endswith("GROUP BY ?") and "value 7" not in t["pattern"]
        assert len(t["last_error"]) > 900 and t["last_query"] is None     # the values: admins only
    with app.test_client() as c:
        _login(c, "admin")
        (t,) = c.get("/supagent/dictionary/api/timings").get_json()["timings"]
        assert t["last_query"] == long_sql


def test_agent_queries_read_few_raw_documents(world, monkeypatch):
    import osagg

    from supagent import settings, tools

    seen = []
    monkeypatch.setattr(osagg, "connect", lambda **kw: seen.append(kw) or object())
    tools._connection(world["jobs"], extract=False, agent_query=True)
    tools._connection(world["jobs"], extract=False)                   # the learner: the connection's own cap
    tools._connection(world["jobs"], extract=True, max_rows=100_000)  # extracts: as many as they need
    assert [kw.get("max_scan_rows") for kw in seen] == [20000, None, 500_000]
    settings.set_value("agent.osagg_max_scan_rows", 0)
    try:
        tools._connection(world["jobs"], extract=False, agent_query=True)
        assert seen[-1].get("max_scan_rows") is None
    finally:
        settings.set_value("agent.osagg_max_scan_rows", 20000)


def test_the_agent_knows_the_limits_of_osagg_and_promagg(world, monkeypatch):
    """Next to each database, and with each refusal: what the connector can run, and how to
    rewrite a refused query (in steps), so that the agent does not try variants one by one."""
    from supagent import tools_superset
    from supagent.security import acting_as

    with acting_as("admin"):
        dbs = {d["backend"]: d for d in tools_superset.list_databases()["databases"]}
    assert "One index per query" in dbs["osagg"]["sql"] and "promql_query" in dbs["promagg"]["sql"]

    class Refused(Exception):
        pass

    def refuse(database, sql, limit, extract):
        raise Refused("This query needs 200,099 raw documents from OpenSearch, above the safety cap of 20,000 rows. "
                      "It could not be fully pushed down (raw rows requested).")

    monkeypatch.setattr(tools_superset, "_run", refuse)
    with acting_as("admin"):
        out = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(
            database_id=world["jobs"].id, sql="SELECT * FROM jobs"))
    assert out["success"] is False and "safety cap" in out["error"] and "rewrite it in steps" in out["error"]


def test_an_extract_cut_by_its_own_limit_says_so(world, monkeypatch, tmp_path):
    """The agent copied a LIMIT into an extract and told the user it held every row: the tool
    now says when the SQL's own LIMIT was reached, so the agent runs it again without it."""
    from supagent import tools

    rows = [(i, "FAILED") for i in range(50)]
    monkeypatch.setattr(tools, "_export_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tools, "_database", lambda ref, backend=None, sql=None: world["jobs"])
    monkeypatch.setattr(tools, "_run", lambda d, sql, cap, extract: (["ID", "STATUS"], rows, False))
    cut = tools._export('SELECT "ID", "STATUS" FROM "batch-jobs" ORDER BY 1 LIMIT 50', None, "x", None, None, None)
    assert cut["limited_by_sql"] == 50 and "without the LIMIT" in cut["note"] and cut["rows"] == 50
    whole = tools._export('SELECT "ID", "STATUS" FROM "batch-jobs" ORDER BY 1', None, "y", None, None, None)
    first = tools._export('SELECT "ID", "STATUS" FROM "batch-jobs" ORDER BY 1 LIMIT 80', None, "z", None, None, None)
    assert "note" not in whole and "note" not in first            # no LIMIT / a LIMIT not reached


class _Tables:
    """An osagg / promagg connection that knows a few tables."""

    names = ["batch-jobs", "batch-apps", "node_cpu_seconds_total"]

    def table_meta(self, name):
        return object() if name in self.names else None

    def list_tables(self):
        return list(self.names)

    def close(self):
        pass


def test_a_name_that_is_no_index_is_named_with_the_closest_ones(world, monkeypatch):
    from supagent import tools, tools_superset
    from supagent.security import acting_as

    monkeypatch.setattr(tools, "_connection", lambda database, extract, max_rows=0, agent_query=False: _Tables())
    sql = 'SELECT a."ID", b."TEAM" FROM "batch-jobs" a JOIN "apps" b ON a."APP" = b."APP"'
    hint = tools.unknown_tables(world["jobs"], sql)
    assert '"apps" (closest: "batch-apps")' in hint and "batch-jobs" not in hint.split("closest")[0]
    assert tools.unknown_tables(world["jobs"], 'SELECT COUNT(*) FROM "batch-jobs"') == ""

    class Refused(Exception):
        pass

    def refuse(database, sql, limit, extract):
        raise Refused("JOIN on an OpenSearch table is not supported: aggregate or filter each side in a subquery")

    monkeypatch.setattr(tools_superset, "_run", refuse)
    with acting_as("admin"):
        out = tools_superset.execute_sql(tools_superset.ExecuteSqlRequest(database_id=world["jobs"].id, sql=sql))
    assert out["success"] is False and out["error"].startswith('No table "apps" (closest: "batch-apps")')
