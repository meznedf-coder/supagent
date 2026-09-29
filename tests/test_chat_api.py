"""The pages and the API inside Superset: permissions, ownership, stuck answers, results,
files, table upgrades, answer post-processing."""

from __future__ import annotations

import datetime as dt

from conftest import login


def _conversation(user: str, status: str = "done", age_minutes: int = 0):
    from superset.extensions import db, security_manager as sm

    from supagent.models import Conversation, File, Message

    u = sm.find_user(username=user)
    conv = Conversation(user_id=u.id, title=f"{user}'s question")
    db.session.add(conv)
    db.session.flush()
    db.session.add(Message(conversation_id=conv.id, role="user", content="How many jobs failed?", status="done"))
    when = dt.datetime.utcnow() - dt.timedelta(minutes=age_minutes)
    m = Message(conversation_id=conv.id, role="assistant", status=status, content="12 jobs failed." if status == "done" else "",
                steps=[{"tool": "execute_sql", "status": "done", "args": {"request": {"database_id": 1, "sql": "SELECT 1"}}}],
                results=[{"tool": "execute_sql", "sql": "SELECT app, n FROM t", "columns": ["app", "n"],
                          "rows": [["A", 7], ["B", 5]], "row_count": 2, "truncated": False}],
                created_at=when, updated_at=when)
    db.session.add(m)
    db.session.flush()
    f = File(message_id=m.id, name="chart.png", mime="image/png", size=8, data=b"\x89PNG\r\n\x1a\n")
    db.session.add(f)
    db.session.flush()
    m.files = [{"id": f.id, "name": "chart.png", "mime": "image/png", "size": 8}]
    db.session.commit()
    db.session.query(Message).filter_by(id=m.id).update({"updated_at": when, "created_at": when})   # its last progress
    db.session.commit()
    return conv.id, m.id, f.id


def test_pages_and_admin_permissions(app):
    with app.test_client() as c:
        assert c.get("/supagent/").status_code in (302, 401)             # not logged in
        login(c, "alice")
        assert c.get("/supagent/").status_code == 200
        assert c.get("/supagent/dictionary/").status_code == 200
        assert c.get("/supagent/admin/").status_code in (302, 403)        # admins only
        assert c.get("/supagent/admin/api/settings").status_code in (401, 403)
        assert c.post("/supagent/admin/api/learn", json={}).status_code in (401, 403)
    with app.test_client() as c:
        login(c, "nobody")                                                 # no role AI Agent
        assert c.get("/supagent/api/conversations").status_code in (401, 403)
    with app.test_client() as c:
        login(c, "admin")
        r = c.get("/supagent/admin/api/settings")
        assert r.status_code == 200
        keys = {s["key"]: s for s in r.get_json()["settings"]}
        assert keys["llm.middleware.consumer_secret"]["secret"] is True
        r = c.post("/supagent/admin/api/settings", json={"settings": {"llm.auth": "middleware",
                                                                      "llm.middleware.consumer_secret": "s3cret"}})
        assert r.status_code == 200
        keys = {s["key"]: s for s in r.get_json()["settings"]}
        assert keys["llm.auth"]["value"] == "middleware"
        assert keys["llm.middleware.consumer_secret"]["value"] is True    # never shown back
        c.post("/supagent/admin/api/settings", json={"settings": {"llm.auth": "none"}})


def test_conversations_are_private(app):
    with app.app_context():
        cid, mid, fid = _conversation("alice")
    with app.test_client() as c:
        login(c, "bob")
        assert c.get(f"/supagent/api/conversations/{cid}").status_code == 404
        assert c.get(f"/supagent/api/messages/{mid}").status_code == 404
        assert c.get(f"/supagent/api/files/{fid}").status_code == 404
        assert c.get(f"/supagent/api/messages/{mid}/results/0.xlsx").status_code == 404
        assert c.delete(f"/supagent/api/conversations/{cid}").status_code == 404
    with app.test_client() as c:
        login(c, "alice")
        data = c.get(f"/supagent/api/conversations/{cid}").get_json()
        answer = data["messages"][-1]
        assert answer["results"][0]["rows"] == [["A", 7], ["B", 5]]
        assert answer["files"][0]["url"] == f"api/files/{fid}"
        img = c.get(f"/supagent/api/files/{fid}")
        assert img.status_code == 200 and img.mimetype == "image/png" and "inline" in img.headers["Content-Disposition"]
        dl = c.get(f"/supagent/api/files/{fid}?download=1")
        assert "attachment" in dl.headers["Content-Disposition"]
        xlsx = c.get(f"/supagent/api/messages/{mid}/results/0.xlsx")
        assert xlsx.status_code == 200 and xlsx.data[:2] == b"PK"


def test_stuck_answers_never_lock_a_conversation(app):
    with app.app_context():
        cid, mid, _ = _conversation("alice", status="running", age_minutes=60)
    with app.test_client() as c:
        login(c, "alice")
        m = c.get(f"/supagent/api/messages/{mid}").get_json()
        assert m["status"] == "error" and "no progress" in m["content"]
    with app.app_context():
        cid2, mid2, _ = _conversation("alice", status="running", age_minutes=1)
    with app.test_client() as c:
        login(c, "alice")
        r = c.post("/supagent/api/ask", json={"question": "and yesterday?", "conversation_id": cid2})
        assert r.status_code == 409                                          # a live answer: wait
        assert c.post(f"/supagent/api/messages/{mid2}/cancel").get_json()["status"] == "cancelled"


def test_feedback_keeps_the_sql_as_an_example(app):
    with app.app_context():
        _cid, mid, _ = _conversation("alice")
    with app.test_client() as c:
        login(c, "alice")
        r = c.post(f"/supagent/api/messages/{mid}/feedback", json={"value": 1}).get_json()
        assert r["feedback"] == 1 and r["example_kept"] is True and "recipes" in r
    with app.app_context():
        from superset.extensions import db

        from supagent.models import Example

        ex = db.session.query(Example).filter_by(message_id=mid).one()
        assert ex.sql == "SELECT 1" and ex.question == "How many jobs failed?"


def _background_done(seconds: float = 30.0) -> None:
    """The background threads of earlier tests (Helpful, memory) must not read a table changed here."""
    import threading
    import time

    end = time.time() + seconds
    for t in threading.enumerate():
        if t.name.startswith("supagent-") and t is not threading.current_thread():
            t.join(max(0.0, end - time.time()))


def test_missing_columns_are_added(ctx):
    import sqlalchemy as sa
    from superset.extensions import db

    from supagent.models import _add_missing_columns

    _background_done()
    with db.engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE supagent_message DROP COLUMN results"))
    assert "supagent_message.results" in _add_missing_columns(db.engine)
    cols = {c["name"] for c in sa.inspect(db.engine).get_columns("supagent_message")}
    assert "results" in cols


def test_long_answer_tables_are_cut_when_the_page_has_the_rows():
    from supagent.agent import trim_tables

    table = "| a | b |\n|---|---|\n" + "\n".join(f"| {i} | {i} |" for i in range(30))
    out = trim_tables("Top:\n\n" + table + "\n\nDone.")
    assert out.count("\n| ") == 11 and "First 10 of 30 rows" in out and out.endswith("Done.")
    short = "| a |\n|---|\n| 1 |\n| 2 |"
    assert trim_tables(short) == short


def test_the_answer_language_follows_the_question():
    from supagent.agent import question_language

    assert question_language("How many jobs failed per hour today?") == "English"
    assert question_language("Give me a report of the failing jobs today, by application.") == "English"
    assert question_language("Combien de jobs ont échoué aujourd'hui par application ?") == "French"
    assert question_language("Quels serveurs avaient le plus de CPU hier ?") == "French"
    assert question_language("CPU srv-amer-002") is None


def test_dashboard_screenshots_lose_their_empty_bottom():
    import io

    from PIL import Image, ImageDraw

    from supagent.tools import _trim_bottom

    im = Image.new("RGB", (400, 1000), (245, 245, 245))
    ImageDraw.Draw(im).rectangle((20, 20, 380, 300), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = Image.open(io.BytesIO(_trim_bottom(buf.getvalue())))
    assert out.size == (400, 301 + 24)


def test_local_file_links_leave_the_answer():
    from supagent.agent import local_links

    text = ("Here is the screenshot:\n\n![Dashboard](/srv/superset-exports/dashboard-7.png)\n\n"
            "The file [extract.xlsx](/srv/superset-exports/extract.xlsx) is ready. "
            "See [Superset](https://superset.example/dashboard/7/).")
    out = local_links(text)
    assert "/home/" not in out and "extract.xlsx is ready" in out and "(https://superset.example/dashboard/7/)" in out


def test_a_saved_chart_request_is_recognised():
    from supagent.agent import CHART_CLAIM, SAVED_CHART_ASK, claims_check

    for q in ("Create a Superset line chart named 'CPU busy % by server - 24 Sep' from the metrics",
              "Save a bar chart called Failed jobs in Superset", "Crée un graphique Superset des jobs en échec",
              "Add a chart of the failures to the dashboard 'Ops'"):
        assert SAVED_CHART_ASK.search(q), q
    for q in ("Give me a report of the failing jobs today, by application.", "Show me a chart of the CPU today"):
        assert not SAVED_CHART_ASK.search(q), q
    assert CHART_CLAIM.search("The chart has been created. Here's a summary")
    trace = [{"tool": "chart_from_sql", "status": "done", "result": "{}"}]
    assert "no Superset chart was saved" in claims_check("The chart has been created.", trace)
    trace.append({"tool": "generate_chart", "status": "done", "result": "{}"})
    assert claims_check("The chart has been created.", trace) == ""


def test_an_answer_without_the_tools_is_sent_back_once():
    """The knowledge given with a question is a summary: an answer with no tool call, or
    showing results that no query returned, is sent back to the model once."""
    from supagent.agent import NO_QUERY_NUDGE, NO_TOOL_NUDGE, unsupported_answer

    made_up = '```json\n[{"application": "BILLING", "jobs": 215430}]\n```\n\nSQL run:\n```sql\nSELECT 1\n```'
    assert unsupported_answer(made_up, []) == NO_TOOL_NUDGE
    looked = [{"tool": "describe_data", "status": "done"}]
    assert unsupported_answer(made_up, looked) == NO_QUERY_NUDGE            # a dictionary is not a query
    ran = looked + [{"tool": "execute_sql", "status": "done"}]
    assert unsupported_answer(made_up, ran) is None
    failed = looked + [{"tool": "execute_sql", "status": "error"}]
    assert unsupported_answer(made_up, failed) == NO_QUERY_NUDGE            # the query failed: no results
    fields = "| field | fill rate |\n|---|---|\n| STATUS_INFO | 100% |\n| ERROR_CATEGORY | 12% |"
    assert unsupported_answer(fields, looked) is None                       # the dictionary's own figures
    assert unsupported_answer("The index batch-jobs holds the job runs.", looked) is None


def test_a_second_unsupported_answer_is_marked():
    from supagent.agent import UNSUPPORTED_NOTE, unsupported_note

    made_up = '```json\n[{"application": "BILLING", "jobs": 215430}]\n```'
    assert unsupported_note(made_up, []) == UNSUPPORTED_NOTE                 # still no query after the reminder
    assert unsupported_note(made_up, [{"tool": "describe_data", "status": "done"}]) == UNSUPPORTED_NOTE
    assert unsupported_note(made_up, [{"tool": "execute_sql", "status": "done"}]) == ""
    assert unsupported_note("Hello! Ask me about your data.", []) == ""     # no data claimed: fine
    doc = "The runbook says: escalate pool 7 incidents to the Night Ops desk, extension 4242."
    assert unsupported_note(doc, []) == ""                                    # a document quoted, no results


def test_check_health_says_when_its_entities_match_nothing(app, monkeypatch):
    """Lab acceptance: entities=["ops"] (not a server name) hid every breach behind 'no breach'."""
    import contextlib
    import datetime as dt

    from supagent import tools

    class Zone:
        def utc_ms(self, d):
            return int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

        def local(self, ms):
            return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)

    class S:
        def __init__(self, node, values, t0, step):
            self.labels = {"node": node}
            self.points = [(t0 + i * step, v) for i, v in enumerate(values)]

    class Conn:
        zone = Zone()

        class client:  # noqa: N801
            @staticmethod
            def query_range(expr, a, b, step):
                return [S("srv-amer-002", [95, 97, 96, 40], a, step), S("srv-emea-041", [50, 60, 55, 50], a, step)]

        def close(self):
            pass

    monkeypatch.setattr(tools, "_as_user", lambda: contextlib.nullcontext((None, None)))
    monkeypatch.setattr(tools, "_catalog", lambda: {"checks": {"cpu_saturation": {"promql": "cpu", "above": 90}}})
    monkeypatch.setattr(tools, "_metrics_database", lambda ref: type("D", (), {"database_name": "metrics"})())
    monkeypatch.setattr(tools, "_promagg_connection", lambda d: Conn())
    everyone = tools.check_health("2026-09-24 00:00", "2026-09-24 08:00")
    assert everyone["breach_count"] == 1 and everyone["breaches"][0]["labels"]["node"] == "srv-amer-002"
    wrong = tools.check_health("2026-09-24 00:00", "2026-09-24 08:00", entities=["ops"])
    assert wrong["breach_count"] == 0 and "nothing was checked" in wrong["note"] and "1 breach" in wrong["note"]
    other = tools.check_health("2026-09-24 00:00", "2026-09-24 08:00", entities=["srv-emea-041"])
    assert other["note"] == "no breach on ['srv-emea-041']; 1 breach(es) on other entities"


def test_pages_follow_supersets_theme(app, monkeypatch):
    """Dark mode as Superset has it: available only when Superset has a dark theme; the mode
    itself is chosen in the browser (theme.js reads Superset's saved choice)."""
    from conftest import login

    from supagent.theme import superset_theme

    with app.app_context():
        t = superset_theme()
        assert t["dark"] is True and t["primary"] in (None, "#2893B3")  # 6.1's default color; 6.0 sets none
        monkeypatch.setitem(app.config, "THEME_DARK", None)
        monkeypatch.setitem(app.config, "ENABLE_UI_THEME_ADMINISTRATION", False)
        assert superset_theme()["dark"] is False                         # dark mode turned off by the admin
    with app.test_client() as c:
        login(c, "alice")
        page = c.get("/supagent/").get_data(as_text=True)
    assert 'data-superset-dark="no"' in page and "theme.js" in page
    assert page.index("theme.js") < page.index("supagent.css")          # before the styles: no flash


def test_page_files_carry_their_content_hash(app):
    """Superset lets browsers keep static files for a year: after an upgrade, only a new URL makes
    them load the new CSS and JavaScript (a stale stylesheet gave half-dark pages)."""
    import re

    from supagent import _file_hash

    with app.test_client() as c:
        login(c, "admin")
        for page in ("/supagent/", "/supagent/dictionary/", "/supagent/admin/"):
            html = c.get(page).get_data(as_text=True)
            urls = re.findall(r'(?:src|href)="(/supagent-static/[^"]+)"', html)
            assert urls, page
            for url in urls:
                name = url.split("/supagent-static/")[1].split("?")[0]
                assert url.endswith(f"?v={_file_hash(name)}"), url
