"""A real Superset app on a temporary SQLite database, with supagent registered, users and
roles set up once per test session."""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="supagent-tests-")
_CONFIG = os.path.join(_TMP, "superset_config.py")
with open(_CONFIG, "w", encoding="utf-8") as fh:
    fh.write(f'''
SQLALCHEMY_DATABASE_URI = "sqlite:///{_TMP}/superset.db"
SECRET_KEY = "supagent-tests-" + "x" * 32
WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False
TESTING = True
SUPAGENT_AGENT_ROUTER = False          # the router's LLM call only in its own tests (scripted LLMs elsewhere)
SUPAGENT_KNOWLEDGE_APPLY_BACKGROUND = False   # saves apply at once (the background has its own test)
from supagent import init_app as FLASK_APP_MUTATOR
''')
os.environ["SUPERSET_CONFIG_PATH"] = _CONFIG
os.environ["SUPERSET_HOME"] = _TMP

PASSWORD = "test-password-1"


@pytest.fixture(scope="session")
def app():
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        from superset.extensions import appbuilder, db, security_manager as sm

        from supagent.cli import ROLE, _role_permissions
        from supagent.models import create_or_upgrade

        import superset
        from flask_migrate import upgrade

        upgrade(directory=os.path.join(os.path.dirname(superset.__file__), "migrations"))   # superset db upgrade
        create_or_upgrade()
        appbuilder.add_permissions(update_perms=True)
        sm.sync_role_definitions()
        role = sm.find_role(ROLE) or sm.add_role(ROLE)
        for perm, view in _role_permissions():
            pvm = sm.find_permission_view_menu(perm, view) or sm.add_permission_view_menu(perm, view)
            sm.add_permission_role(role, pvm)
        for name, roles in (("admin", ["Admin"]), ("alice", ["Gamma", ROLE]), ("bob", ["Gamma", ROLE]),
                            ("nobody", ["Gamma"])):
            if sm.find_user(username=name) is None:
                sm.add_user(name, name, "Test", f"{name}@example.com", [sm.find_role(r) for r in roles],
                            password=PASSWORD)
        db.session.commit()
        db.session.remove()
        import supagent.governed.pipeline  # noqa: F401  (imported once as the app does: before any test patches)
    yield app                    # no context held open: every request and test has its own g


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app
        from superset.extensions import db

        db.session.rollback()
        db.session.remove()


def login(client, user: str) -> None:
    r = client.post("/login/", data={"username": user, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code in (200, 302), r.status_code
