"""No copies in the team's knowledge: a memory written again is the same memory (brought back if it was
disabled, when a person writes it; a disabled one learned again from a chat stays refused); admins and
owners really delete one; the copies made before are merged at init; the catalog refuses a second entry
of the same title, or of the same classification and content."""

from __future__ import annotations

import pytest

from conftest import login
from test_knowledge import world  # noqa: F401  (the fixture)


@pytest.fixture()
def clean(world):
    from superset.extensions import db

    from supagent.knowledge.catalog import invalidate
    from supagent.models import Entry, EntryVersion, Memory

    for model in (Memory, EntryVersion, Entry):
        db.session.query(model).delete()
    db.session.commit()
    invalidate()
    yield world
    for model in (Memory, EntryVersion, Entry):
        db.session.query(model).delete()
    db.session.commit()


def test_a_memory_written_again_is_the_same_memory(clean):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.memory import add
    from supagent.models import Memory

    admin = sm.find_user(username="admin").id
    first = add(admin, "The critical applications are BILLING and PAYROLL", scope="team", kind="fact",
                approved_by="admin")
    assert add(admin, "The critical applications are BILLING and PAYROLL.", scope="team") is None
    first.status = "disabled"
    db.session.commit()
    assert add(admin, "The critical applications are BILLING and PAYROLL", scope="team", source="chat") is None
    back = add(admin, "The critical applications are BILLING and PAYROLL", scope="team", approved_by="admin")
    assert back.id == first.id and back.status == "active"
    assert db.session.query(Memory).count() == 1


def test_admins_and_owners_delete_a_memory_for_good(clean, app):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.memory import add
    from supagent.models import Memory

    alice = sm.find_user(username="alice").id
    team = add(alice, "Team rule: exclude the test pool", scope="team", kind="rule")          # proposed
    mine = add(alice, "Durations in minutes")
    team_id, mine_id = team.id, mine.id
    with app.app_context(), app.test_client() as c:
        login(c, "alice")
        assert c.delete(f"/supagent/api/memory/{mine_id}").get_json() == {"deleted": mine_id}
    with app.app_context(), app.test_client() as c:
        login(c, "admin")
        assert c.delete(f"/supagent/api/memory/{team_id}").status_code == 200
    db.session.expire_all()
    assert db.session.query(Memory).filter(Memory.id.in_([team_id, mine_id])).count() == 0


def test_copies_made_before_are_merged_at_init(clean):
    from superset.extensions import db, security_manager as sm

    from supagent.knowledge.memory import merge_duplicates
    from supagent.models import Memory

    admin, alice = sm.find_user(username="admin").id, sm.find_user(username="alice").id
    text = "The critical applications are BILLING and PAYROLL."
    db.session.add_all([Memory(scope="team", user_id=admin, kind="fact", text=text, status="disabled"),
                        Memory(scope="team", user_id=admin, kind="fact", text=text, status="active"),
                        Memory(scope="team", user_id=alice, kind="fact", text=text.lower(), status="disabled"),
                        Memory(scope="user", user_id=alice, kind="preference", text="Minutes", status="active"),
                        Memory(scope="user", user_id=admin, kind="preference", text="Minutes", status="active")])
    db.session.commit()
    assert merge_duplicates() == 2
    left = db.session.query(Memory).filter(Memory.scope == "team").all()
    assert len(left) == 1 and left[0].status == "active"
    assert db.session.query(Memory).filter(Memory.scope == "user").count() == 2      # two users: no copy


def test_the_catalog_refuses_a_second_entry_of_the_same_title_or_content(clean):
    from supagent.knowledge.catalog import CatalogError, save_entry

    e = save_entry({"title": "No UAT", "classification": "rule", "content": "Exclude UAT unless asked."}, by="admin")
    with pytest.raises(CatalogError, match="already named 'No UAT'"):
        save_entry({"title": "no uat ", "classification": "rule", "content": "Something else."}, by="admin")
    with pytest.raises(CatalogError, match="already says exactly this"):
        save_entry({"title": "UAT rule", "classification": "rule", "content": "exclude  UAT unless asked."}, by="admin")
    other = save_entry({"title": "Minutes", "classification": "rule", "content": "Durations in minutes."}, by="admin")
    with pytest.raises(CatalogError, match="already named"):
        save_entry({"title": "No UAT", "classification": "rule", "content": "Durations in minutes."}, by="admin",
                   entry_id=other.id)                                          # renamed onto another one
    save_entry({"title": "No UAT", "classification": "rule", "content": "Exclude UAT unless the question asks."},
               by="admin", entry_id=e.id)                                      # its own title: fine
