"""The 0.8 Data dictionary on the server side: a learned answer corrected and checked before it is confirmed, words
put on a table by hand, the system map (who sees what, the places of the boxes, the interactions), the Context and
the map exported, documents and sites without their secret."""

from __future__ import annotations

import io

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


class _As:
    """Requests as a user, each in an app context of its own (as a real server does: the test's own context would
    keep the first request's user in flask.g)."""

    def __init__(self, app, user):
        self.app, self.user = app, user

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _do(self, method, path, **kw):
        with self.app.app_context(), self.app.test_client() as c:
            login(c, self.user)
            r = c.open(path, method=method, **kw)
            r.get_data()
            return r

    def get(self, path, **kw):
        return self._do("GET", path, **kw)

    def post(self, path, **kw):
        return self._do("POST", path, **kw)


def _client(app, user):
    return _As(app, user)


def _post(app, user, path, body):
    r = _As(app, user).post(path, json=body)
    return r.status_code, r.get_json()


def test_a_learned_answer_is_corrected_and_checked_before_it_is_confirmed(world, app, monkeypatch):
    from superset.extensions import db

    jobs_id = world["jobs"].id

    from supagent import tools as T
    from supagent.models import Recipe

    sql = 'SELECT COUNT(*) AS n FROM "jobs" WHERE "STATUS" = \'FAILED\''
    r = Recipe(question="How many jobs failed yesterday?", words="fail job yesterday", tool="execute_sql",
               database_id=jobs_id, target="jobs", query=sql, args={"request": {"database_id": jobs_id, "sql": sql}},
               signature="old", status="helpful", uses=1)
    db.session.add(r)
    db.session.commit()
    rid = r.id
    ran = []

    def fake_run(database, query, max_rows, extract):
        ran.append(query)
        if "nope" in query:
            raise RuntimeError('Table "nope" does not exist')
        return ["n"], [(12,)], False

    monkeypatch.setattr(T, "_run", fake_run)
    monkeypatch.setattr(T, "unknown_tables", lambda database, query: "")
    try:
        with _client(app, "admin") as c:
            got = c.post(f"/supagent/dictionary/api/recipes/{rid}/check", json={"query": sql}).get_json()
            assert got["ok"] and got["rows"] == 1 and got["columns"] == ["n"] and got["sample"] == [[12]]
            bad = c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"query": 'SELECT 1 FROM "nope"',
                                                                         "status": "confirmed"})
            assert bad.status_code == 400 and "nope" in bad.get_json()["error"]
            with app.app_context():
                assert db.session.get(Recipe, rid).status == "helpful"          # a broken query is never confirmed
        new = sql.replace("FAILED", "ERROR")
        code, ok = _post(app, "admin", f"/supagent/dictionary/api/recipes/{rid}", {
            "question": "How many jobs ended in error on a day?", "query": new, "status": "confirmed"})
        assert code == 200 and ok.get("status") == "confirmed", ok
        with _client(app, "admin") as c:
            listed = c.get("/supagent/dictionary/api/recipes").get_json()["recipes"]
            row = next(x for x in listed if x["id"] == rid)
            assert row["edited_by"] == "admin" and row["previous"]["query"] == sql
        with _client(app, "alice") as c:
            assert c.post(f"/supagent/dictionary/api/recipes/{rid}", json={"question": "x"}).status_code == 403
            listed = c.get("/supagent/dictionary/api/recipes").get_json()["recipes"]
            assert all(x["previous"] is None for x in listed)                    # an admin's trail
        with app.app_context():
            r = db.session.get(Recipe, rid)
            assert r.query == sql.replace("FAILED", "ERROR") and r.args["request"]["sql"] == r.query
            assert "error" in r.words.split() and r.signature != "old" and r.target == "jobs"
    finally:
        with app.app_context():
            db.session.query(Recipe).filter(Recipe.id == rid).delete()
            db.session.commit()


def test_words_put_on_a_table_by_hand_never_fade(world, app):
    import datetime as dt

    from superset.extensions import db

    jobs_id = world["jobs"].id

    from supagent.knowledge.experience import record_associations
    from supagent.models import Association

    try:
        with _client(app, "admin") as c:
            got = c.post("/supagent/dictionary/api/where_data/add", json={
                "words": "rejected runs, the batches", "database_id": jobs_id, "name": "jobs"}).get_json()
            assert got["kind"] == "index" and set(got["words"]) == {"reject", "run", "batch"}
            bad = c.post("/supagent/dictionary/api/where_data/add", json={"words": "x y", "database_id": jobs_id,
                                                                         "name": "no-such"})
            assert bad.status_code == 400
            tables = c.get(f"/supagent/dictionary/api/where_data/tables?database={jobs_id}").get_json()["tables"]
            assert {"kind": "index", "name": "jobs"} in tables
            rows = c.get("/supagent/dictionary/api/where_data?q=reject").get_json()["tables"]
            words = {w["word"]: w for w in rows[0]["words"]}
            assert words["reject"]["manual"] and words["reject"]["added_by"] == "admin"
        with _client(app, "alice") as c:
            r = c.post("/supagent/dictionary/api/where_data/add", json={
                "words": "rejected", "database_id": jobs_id, "name": "jobs"})
            assert r.status_code in (403, 404), (r.status_code, r.get_json())
        with app.app_context():
            a = db.session.query(Association).filter_by(word="reject", name="jobs").one()
            assert a.source == "admin" and a.uses == 2
            a.updated_at = dt.datetime.utcnow() - dt.timedelta(days=400)     # long unused: an admin's stays
            db.session.commit()
            trace = [{"tool": "execute_sql", "called": "execute_sql", "status": "done", "args": {"request": {
                "database_id": jobs_id, "sql": 'SELECT 1 FROM "jobs"'}}, "result": '{"row_count": 0}'},
                {"tool": "execute_sql", "called": "execute_sql", "status": "done", "args": {"request": {
                    "database_id": jobs_id, "sql": 'SELECT 1 FROM "other"'}}, "result": '{"row_count": 3}'}]
            record_associations(999, "rejected runs", trace)                  # a miss on jobs: no delete
            a = db.session.query(Association).filter_by(word="reject", name="jobs").one()
            assert a.uses >= 1
            from supagent.knowledge.resolve import _associations

            found = {}
            from superset.models.core import Database

            _associations(["reject"], found, [db.session.get(Database, jobs_id)])
            assert any(k[3] == "jobs" for k in found)
    finally:
        with app.app_context():
            db.session.query(Association).delete()
            db.session.commit()


def _map_world():
    from superset.extensions import db

    from supagent.models import Facet, KObject, Tag

    orders = Facet(facet="subject", value="Orders", status="approved", source="admin")
    pay = Facet(facet="application", value="Payments", status="approved", source="admin")
    gw = Facet(facet="component", value="Payment gateway", status="approved", source="admin")
    hidden = Facet(facet="component", value="Metrics exporter", status="approved", source="admin")
    db.session.add_all([orders, pay, gw, hidden])
    db.session.flush()
    pay.parents, gw.parents, hidden.parents = [orders.id], [pay.id], [pay.id]
    jobs = db.session.query(KObject).filter_by(kind="index", name="jobs").one()
    cpu = db.session.query(KObject).filter_by(kind="metric", name="node_cpu_seconds_total").one()
    db.session.add_all([Tag(ref=f"object:{jobs.id}", facet_id=gw.id, status="approved"),
                        Tag(ref=f"object:{cpu.id}", facet_id=hidden.id, status="approved")])
    db.session.commit()
    return orders, pay, gw, hidden


def test_the_system_map(world, app):
    from PIL import Image
    from superset.extensions import db

    from supagent.models import Facet, Link, Meta, Tag

    with app.app_context():
        orders, pay, gw, hidden = _map_world()
        ids = {f.value: f.id for f in (orders, pay, gw, hidden)}
    try:
        with _client(app, "admin") as c:
            d = c.get("/supagent/dictionary/api/map").get_json()
            assert {v["value"] for v in d["values"]} >= set(ids) and d["is_admin"]
            assert [c_["name"] for c_ in d["categories"]][:3] == ["subject", "application", "component"]
            r = c.post("/supagent/dictionary/api/map", json={"interaction": {"a": ids["Payment gateway"],
                                                                            "b": ids["Metrics exporter"],
                                                                            "kind": "sends_to", "note": "latency"}})
            link = r.get_json()["interaction"]
            assert link["label"] == "sends data to"
            assert c.post("/supagent/dictionary/api/map", json={"interaction": {
                "a": ids["Orders"], "b": ids["Orders"], "kind": "calls"}}).status_code == 400
            assert c.post("/supagent/dictionary/api/map", json={"interaction": {
                "a": ids["Orders"], "b": ids["Payments"], "kind": "teleports"}}).status_code == 400
            lay = c.post("/supagent/dictionary/api/map", json={"layout": {"positions": {str(ids["Orders"]): [10, 20.5],
                                                                                       "x": [1, 2]},
                                                                         "hidden": [ids["Metrics exporter"]]}}).get_json()
            assert lay["layout"]["positions"] == {str(ids["Orders"]): [10.0, 20.5]}
            assert c.post("/supagent/dictionary/api/map", json={"describe": {"id": ids["Payments"],
                                                                              "description": "Takes the money."}}).status_code == 200
            d = c.get("/supagent/dictionary/api/map").get_json()
            assert d["layout"]["hidden"] == [ids["Metrics exporter"]]
            assert next(v for v in d["values"] if v["value"] == "Payments")["description"] == "Takes the money."
            assert any(x["kind"] == "sends_to" for x in d["links"])
            png = io.BytesIO()
            Image.new("RGB", (1200, 700), "white").save(png, "PNG")
            import base64

            pdf = c.post("/supagent/dictionary/api/map/export.pdf", json={
                "png": "data:image/png;base64," + base64.b64encode(png.getvalue()).decode(), "scale": 2})
            assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF") and pdf.mimetype == "application/pdf"
            assert c.post("/supagent/dictionary/api/map/export.pdf", json={"png": "bm90IGEgcG5n"}).status_code == 400
            assert c.post("/supagent/dictionary/api/map", json={"remove_interaction": link["id"]}).get_json()["removed"]
        with _client(app, "alice") as c:
            d = c.get("/supagent/dictionary/api/map").get_json()
            seen = {v["value"] for v in d["values"]}
            # alice may query the jobs database only: the exporter (a metric of the other database) is not shown,
            # the gateway (the jobs index) is, with what it is part of
            assert "Metrics exporter" not in seen and {"Payment gateway", "Payments", "Orders"} <= seen
            assert not d["is_admin"]
            assert c.post("/supagent/dictionary/api/map", json={"layout": {}}).status_code == 403
    finally:
        with app.app_context():
            db.session.query(Link).filter(Link.a_ref.like("facet:%")).delete(synchronize_session=False)
            db.session.query(Tag).delete()
            db.session.query(Facet).delete()
            row = db.session.get(Meta, "system_map")
            if row is not None:
                db.session.delete(row)
            db.session.commit()


def test_the_context_is_exported(world, app):
    from superset.extensions import db

    metrics_id = world["metrics"].id

    from supagent.models import ContextPage

    with app.app_context():
        pages = [ContextPage(section="functional", slug="overview", title="How the system works", kind="summary",
                             content="# How the system works\n\nThe **orders** come from the shop.\n\n| a | b |\n|---|---|\n"
                                     "| 1 | 2 |\n", database_ids=[], version=1),
                 ContextPage(section="technical", slug="inventory-metrics", title="Metrics inventory", kind="facts",
                             content="- node_cpu_seconds_total", database_ids=[metrics_id], version=1)]
        db.session.add_all(pages)
        db.session.commit()
        ids = [p.id for p in pages]
    try:
        with _client(app, "alice") as c:
            full = c.get("/supagent/dictionary/api/context?full=1").get_json()["pages"]
            assert [p["title"] for p in full] == ["How the system works"]       # not her other database's page
            assert "<strong>orders</strong>" in full[0]["html"] and full[0]["content"] is None
            docx = c.get("/supagent/dictionary/api/context/export.docx")
            assert docx.status_code == 200 and docx.data[:2] == b"PK"
            import zipfile

            body = zipfile.ZipFile(io.BytesIO(docx.data)).read("word/document.xml").decode()
            assert "How the system works" in body and "Metrics inventory" not in body
            pdf = c.get(f"/supagent/dictionary/api/context/export.pdf?page={ids[0]}")
            assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
            assert "attachment" in pdf.headers["Content-Disposition"]
            assert c.get(f"/supagent/dictionary/api/context/export.pdf?page={ids[1]}").status_code == 404
            assert c.get("/supagent/dictionary/api/context/export.txt").status_code == 404
    finally:
        with app.app_context():
            db.session.query(ContextPage).filter(ContextPage.id.in_(ids)).delete(synchronize_session=False)
            db.session.commit()


def test_a_sites_secret_never_leaves_the_server(world, app, monkeypatch):
    from superset.extensions import db

    from supagent import tasks
    from supagent.models import Doc

    monkeypatch.setattr(tasks, "dispatch_doc", lambda doc_id: None)
    monkeypatch.setattr("supagent.knowledge.docs.check_url", lambda url: None)
    try:
        with _client(app, "admin") as c:
            r = c.post("/supagent/admin/api/docs", json={"url": "https://wiki.example.com/display/OPS/Home",
                                                         "auth": {"type": "bearer"}, "secret": "s3cr3t-token",
                                                         "max_pages": 30})
            doc = r.get_json()["doc"]
            assert "s3cr3t" not in r.get_data(as_text=True)
            assert doc["auth"] == {"type": "bearer", "user": None, "header": None, "secret_set": True} or \
                doc["auth"]["secret_set"] is True
            assert doc["reads_as"] == "confluence"
            listed = c.get("/supagent/admin/api/docs").get_data(as_text=True)
            assert "s3cr3t" not in listed
            e = c.post(f"/supagent/admin/api/docs/{doc['id']}", json={"category": "Wiki", "auth": {"type": "bearer"}})
            assert e.status_code == 200 and e.get_json()["doc"]["auth"]["secret_set"]      # an empty secret keeps it
            moved = c.post(f"/supagent/admin/api/docs/{doc['id']}", json={"url": "https://other.example.org/wiki/"})
            assert moved.status_code == 400 and "write the token" in moved.get_json()["error"]
            none = c.post(f"/supagent/admin/api/docs/{doc['id']}", json={"auth": {"type": ""}}).get_json()["doc"]
            assert not none["auth"]["secret_set"]
        with app.app_context():
            d = db.session.get(Doc, doc["id"])
            assert d.secret is None and d.category == "Wiki"
    finally:
        with app.app_context():
            db.session.query(Doc).delete()
            db.session.commit()


def test_a_parts_explanation_comes_from_the_sentence_about_it(ctx):
    """The map explains a part with no description by the sentence of a document, a guide or a Context page that
    says what it is: the part as the sentence's subject, a document before the AI-written Context; never a list of
    parts, a heading, or the citations of the Context."""
    from types import SimpleNamespace

    from superset.extensions import db

    from supagent.knowledge import sysmap
    from supagent.models import Chunk

    rows = [Chunk(ref="context:901#0", kind="context", title="Overview",
                  text="Customers order on the shop; the checkout asks the Payment gateway. "
                       "Known parts: Payment gateway, Checkout API, Refund batch, Ticketing."),
            Chunk(ref="doc:901#0", kind="doc", title="Payments runbook",
                  text="# Payment gateway\n\nThe Payment gateway authorizes the cards and retries a declined card once [E1]."),
            Chunk(ref="context:902#0", kind="context", title="Apps", text="Ticketing is the helpdesk's tool [E2, E3].")]
    db.session.add_all(rows)
    db.session.commit()
    sysmap._HINTS.update(state=None, at=0.0, names={}, partial=False)
    try:
        values = {1: SimpleNamespace(value="Payment gateway", description=None),
                  2: SimpleNamespace(value="Ticketing", description=None),
                  3: SimpleNamespace(value="Refund batch", description=None)}
        got = sysmap.hints(values)
        assert got[1][0][2] == "The Payment gateway authorizes the cards and retries a declined card once."
        assert all("Known parts" not in h[2] for h in got[1])            # a list of parts says nothing of one
        assert all(h[2] != "Payment gateway" for h in got[1])            # nor its heading
        assert got[2][0][2] == "Ticketing is the helpdesk's tool."       # without the citations
        assert 3 not in got                                              # named in a list only
        # a value approved or added later: its name alone is read (the others are kept while the texts stay)
        read: list[str] = []
        real = sysmap._sentences
        sysmap._sentences = lambda text: read.append(text) or real(text)
        try:
            values[4] = SimpleNamespace(value="Checkout", description=None)
            again = sysmap.hints(values)
            assert again[1] == got[1] and again[2] == got[2] and len(read) == 1      # the one text that names it
            assert again[4][0][2].startswith("Customers order on the shop; the checkout asks")
            read.clear()
            assert sysmap.hints(values) == again and read == []                       # nothing new: nothing read
            # the texts changed: everything is read again (a runbook now says what the Refund batch is)
            more = Chunk(ref="doc:902#0", kind="doc", title="Refunds", text="The Refund batch pays the refunds every night.")
            db.session.add(more)
            db.session.commit()
            rows.append(more)
            assert sysmap.hints(values)[3][0][2] == "The Refund batch pays the refunds every night."
        finally:
            sysmap._sentences = real
    finally:
        db.session.query(Chunk).filter(Chunk.ref.in_([c.ref for c in rows])).delete(synchronize_session=False)
        db.session.commit()
