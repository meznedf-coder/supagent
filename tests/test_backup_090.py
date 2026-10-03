"""0.9: backups of the knowledge and their restore, whole or by part. One zip with a folder per part; no secret in
it; the last backup.keep are kept; a restore replaces the parts asked by the backup's (the others stay), after
saving the present state; a part alone can be put back; the daily one is made at backup.hour."""

from __future__ import annotations

import datetime as dt
import json
import os
import zipfile

import pytest
from test_brief import system  # noqa: F401  (categories, a pool with servers, interactions, a catalog entry)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401


@pytest.fixture()
def knowledge(system, tmp_path):  # noqa: F811
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import facets, sysmap
    from supagent.models import ContextPage, Doc, Memory, Note, Recipe

    settings.set_value("backup.dir", str(tmp_path))
    facets.set_about("pool", "a group of servers", "admin")
    sysmap.save_layout({"columns": {"pool": [40, 0]}, "groups": [{"name": "Infra", "categories": ["server", "pool"]}]}, "admin")
    doc = Doc(kind="url", title="Wiki", url="https://wiki.example/x", content="How the night goes.", enabled=True, status="ok")
    doc.secret = "s3cr3t-token-of-the-wiki"
    rows = [doc, Memory(scope="team", user_id=1, kind="rule", text="Sales are PAID orders.", status="active", source="manual"),
            Note(scope="team", user_id=1, title="Meeting", text="We agreed on the cut-off."),
            ContextPage(section="technical", slug="night", title="The night", content="The batch starts at midnight."),
            Recipe(question="How many orders?", tool="execute_sql", query="SELECT 1", signature="s1", status="confirmed")]
    db.session.add_all(rows)
    db.session.commit()
    yield {**system, "doc": doc.id, "dir": str(tmp_path)}
    db.session.rollback()
    for model in (Doc, Memory, Note, ContextPage, Recipe):
        db.session.query(model).delete()
    db.session.commit()
    sysmap.save_layout({}, "admin")
    for key in ("backup.dir", "backup.keep", "categories.about"):
        settings.set_value(key, None)


def test_a_backup_holds_every_part_and_no_secret(knowledge):
    from supagent.knowledge import backup

    out = backup.make(reason="test", by="admin")
    assert backup.NAME.match(out["name"]) and os.path.dirname(out["path"]) == knowledge["dir"]
    assert oct(os.stat(out["path"]).st_mode & 0o777) == "0o600"
    parts = out["parts"]
    assert set(parts) == {"categories", "catalog", "memory", "documents", "notes", "context", "learned", "dictionary", "settings"}
    assert parts["categories"]["supagent_facet"] == 8 and parts["categories"]["supagent_link"] == 4
    assert parts["documents"] == {"supagent_doc": 1} and parts["memory"] == {"supagent_memory": 1}
    with zipfile.ZipFile(out["path"]) as z:
        everything = b"".join(z.read(n) for n in z.namelist())
        manifest = json.loads(z.read("manifest.json"))
        assert json.loads(z.read("categories/settings.json"))["categories.about"] == {"pool": "a group of servers"}
        assert json.loads(json.loads(z.read("categories/meta.json"))["system_map"])["groups"][0]["name"] == "Infra"
    assert b"s3cr3t-token-of-the-wiki" not in everything and b"How the night goes." in everything
    assert manifest["format"] == 1 and manifest["reason"] == "test" and manifest["parts"] == parts
    listed = backup.listing()
    assert listed[0]["name"] == out["name"] and listed[0]["parts"]["categories"] >= 12 and listed[0]["by"] == "admin"
    with_vectors = backup.make(vectors=True, tag="x")
    assert "vectors" in with_vectors["parts"]


def test_a_part_is_restored_alone_and_the_present_state_is_saved_first(knowledge):
    from superset.extensions import db

    from supagent.knowledge import backup, facets, sysmap
    from supagent.models import Doc, Entry, Facet, Link, Memory

    name = backup.make(by="admin")["name"]
    # after the backup: a pool goes with its links, the map's arrangement and a description change, an entry and
    # the memory change, the wiki's sign-in stays
    grid = knowledge["grid-a"]
    db.session.query(Link).filter(Link.b_ref == f"facet:{grid}").delete(synchronize_session=False)
    db.session.query(Facet).filter(Facet.id == grid).delete()
    db.session.query(Memory).delete()
    entry = db.session.query(Entry).first()
    entry_id, was = entry.id, entry.content
    entry.content = "changed after the backup"
    db.session.commit()
    sysmap.save_layout({}, "admin")
    facets.set_about("pool", "", "admin")
    out = backup.restore(name, ["categories"], by="admin")
    assert out["parts"]["categories"]["supagent_facet"] == 8 and "error" not in out
    assert db.session.get(Facet, grid).value == "grid-a"                              # back, with its id
    assert db.session.query(Link).filter(Link.b_ref == f"facet:{grid}").count() == 2
    assert sysmap.layout()["groups"] == [{"name": "Infra", "categories": ["server", "pool"]}]
    assert facets.about() == {"pool": "a group of servers"}
    assert db.session.get(Entry, entry_id).content == "changed after the backup"      # the catalog was not asked
    assert db.session.query(Memory).count() == 0
    saved = [f["name"] for f in backup.listing() if "before-restore" in f["name"]]
    assert out["saved_first"] in saved                                                # a restore can be undone
    out = backup.restore(name, ["catalog", "memory", "documents"], by="admin", safety=False)
    assert db.session.get(Entry, entry_id).content == was and db.session.query(Memory).one().text == "Sales are PAID orders."
    assert db.session.get(Doc, knowledge["doc"]).secret == "s3cr3t-token-of-the-wiki"    # kept: it is in no file
    new = Facet(facet="server", value="srv-new", status="approved", source="admin")       # ids go on after a restore
    db.session.add(new)
    db.session.commit()
    assert new.id > max(i for i in knowledge.values() if isinstance(i, int))
    db.session.delete(new)
    db.session.commit()


def test_what_a_restore_refuses(knowledge):
    from supagent.knowledge import backup

    name = backup.make()["name"]
    for bad in ("../../etc/passwd", "supagent-knowledge-20260101-000000.zip", "x.zip", ""):
        with pytest.raises(ValueError):
            backup.path_of(bad)
    with pytest.raises(ValueError, match="not in this backup: vectors"):
        backup.restore(name, ["vectors"], safety=False)
    path = backup.path_of(name)
    later = os.path.join(knowledge["dir"], "supagent-knowledge-20991231-000000.zip")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(later, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n == "manifest.json":
                data = json.dumps({**json.loads(data), "schema": 99}).encode()
            dst.writestr(n, data)
    with pytest.raises(ValueError, match="later version"):
        backup.restore(os.path.basename(later), safety=False)


def test_the_descriptions_of_the_data_go_back_on_the_objects_of_the_same_name(knowledge):
    from superset.extensions import db

    from supagent.knowledge import backup
    from supagent.models import KObject

    o = db.session.query(KObject).filter(KObject.kind == "field", KObject.name == "NODE").first()
    o.description, o.description_source, o.verified = "the server a run ran on", "curated", True
    db.session.commit()
    oid = o.id
    name = backup.make()["name"]
    o = db.session.get(KObject, oid)
    o.description, o.description_source, o.verified = "wrong", "llm", False
    db.session.commit()
    out = backup.restore(name, ["dictionary"], safety=False)
    o = db.session.get(KObject, oid)
    assert (o.description, o.description_source, o.verified) == ("the server a run ran on", "curated", True)
    assert out["parts"]["dictionary"]["objects"] >= 1
    o.description, o.description_source, o.verified = None, None, False
    db.session.commit()


def test_the_last_ones_are_kept_and_the_daily_one_is_due_once(knowledge):
    from supagent import settings
    from supagent.knowledge import backup

    settings.set_value("backup.keep", 2)
    for stamp in ("20260101-010000", "20260102-010000", "20260103-010000"):
        with zipfile.ZipFile(os.path.join(knowledge["dir"], f"supagent-knowledge-{stamp}.zip"), "w") as z:
            z.writestr("manifest.json", json.dumps({"format": 1, "parts": {}, "created_at": stamp}))
    assert backup.prune() == ["supagent-knowledge-20260101-010000.zip"]
    assert [f["name"] for f in backup.listing()] == ["supagent-knowledge-20260103-010000.zip", "supagent-knowledge-20260102-010000.zip"]
    now = dt.datetime(2026, 10, 5, 3, 0)                                             # a Monday, 03:00
    assert backup.due(now)                                                           # none today: due (the hour is 1)
    assert not backup.due(now.replace(hour=0))                                       # not yet the hour
    settings.set_value("backup.days", ["tue"])
    assert not backup.due(now)
    settings.set_value("backup.days", None)
    settings.set_value("backup.enabled", False)
    assert not backup.due(now)
    settings.set_value("backup.enabled", None)
    made = backup.make()["name"]
    today = dt.datetime.strptime(made[len("supagent-knowledge-"):][:15], "%Y%m%d-%H%M%S").replace(hour=23)
    assert not backup.due(today)                                                     # made today: not again


def test_backups_are_runs_and_the_page_lists_downloads_and_restores_them(knowledge, app, monkeypatch):
    from superset.extensions import db

    from supagent import tasks
    from supagent.knowledge import backup
    from supagent.models import Memory, Run

    db.session.query(Run).filter(Run.status == "running").update({"status": "done"})
    db.session.commit()
    out = backup.run_backup(reason="cli", by="admin")
    run = db.session.get(Run, out["run"])
    assert (run.kind, run.status) == ("backup", "done") and run.stats["name"] == out["name"]
    assert [s["step"] for s in run.stats["steps"]][:3] == ["categories", "catalog", "memory"] and run.stats["bytes"] > 0
    name = out["name"]
    calls = []
    monkeypatch.setattr(tasks, "dispatch_restore", lambda n, parts, by="": calls.append((n, parts, by)) or "thread")
    monkeypatch.setattr(tasks, "dispatch_backup", lambda by="": calls.append(("backup", by)) or "thread")
    with _client(app, "admin") as c:
        d = c.get("/supagent/admin/api/backups").get_json()
        assert d["backups"][0]["name"] == name and d["directory"] == knowledge["dir"] and "categories" in d["parts"]
        f = c.get(f"/supagent/admin/api/backups/{name}")
        assert f.status_code == 200 and f.data[:2] == b"PK" and name in f.headers["Content-Disposition"]
        assert c.get("/supagent/admin/api/backups/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert c.post(f"/supagent/admin/api/backups/{name}/restore", json={"parts": ["nothing"]}).status_code == 400
        r = c.post(f"/supagent/admin/api/backups/{name}/restore", json={"parts": ["memory"]}).get_json()
        assert r == {"started": "thread", "parts": ["memory"]} and calls[-1] == (name, ["memory"], "admin")
        assert c.post("/supagent/admin/api/backups", json={}).get_json() == {"started": "thread"} and calls[-1] == ("backup", "admin")
    with _client(app, "alice") as c:
        assert c.get("/supagent/admin/api/backups").status_code in (302, 401, 403)
        assert c.get(f"/supagent/admin/api/backups/{name}").status_code in (302, 401, 403)
    db.session.query(Memory).delete()
    db.session.commit()
    done = backup.run_restore(name, ["memory"], by="admin")
    run = db.session.get(Run, done["run"])
    assert (run.kind, run.status) == ("restore", "done") and db.session.query(Memory).count() == 1
    assert [s["step"] for s in run.stats["steps"]] == ["the present state, saved first", "memory"]
    assert run.stats["saved_first"] == run.stats["steps"][0]["file"] and "before-restore" in run.stats["saved_first"]
    db.session.query(Run).filter(Run.kind.in_(("backup", "restore"))).delete(synchronize_session=False)
    db.session.commit()
