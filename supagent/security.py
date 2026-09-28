"""Who the agent acts as.

Inside a question, everything runs in a request context whose g.user is the user who asks,
loaded with roles and groups: Superset's permission checks (raise_for_access,
can_access_database...) and Superset's MCP tools read g.user, so the agent can do exactly what
that user can do in Superset. Outside a question (CLI, MCP server mode) the tools act as a
service user (setting mcp.user, else the config's MCP_DEV_USERNAME).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_APP: Any = None           # the app of a long-running process (MCP server mode)


class NotAllowed(Exception):
    pass


def load_user(username: str) -> Any:
    """The user with roles and groups loaded (they must survive the tools' own sessions)."""
    from sqlalchemy.orm import joinedload
    from superset.extensions import db, security_manager

    user_model = security_manager.user_model
    query = db.session.query(user_model).options(joinedload(user_model.roles))
    if hasattr(user_model, "groups"):
        group_model = user_model.groups.property.mapper.class_
        query = query.options(joinedload(user_model.groups).joinedload(group_model.roles))
    return query.filter(user_model.username == username).one_or_none()


@contextmanager
def acting_as(username: str) -> Iterator[Any]:
    """A request context in which the agent acts as `username` (checked to exist and be active).
    It has an application context of its own: its g (the user) never leaks into the caller's."""
    from flask import current_app, g

    app = current_app._get_current_object()
    with app.app_context(), app.test_request_context("/supagent/agent"):
        user = load_user(username)
        if user is None or not getattr(user, "active", True):
            raise NotAllowed(f"user {username!r} not found or inactive")
        g.user = user
        g.supagent_acting = True
        yield user


def service_username(app: Any) -> str:
    """The user of the MCP server mode and of the CLI tools: setting mcp.user, else
    SUPAGENT_MCP_USER / TOOLS_USER (environment), else MCP_DEV_USERNAME of the config."""
    from supagent import settings

    try:
        name = settings.get("mcp.user")
    except Exception:  # pylint: disable=broad-except
        name = ""
    return (name or os.environ.get("TOOLS_USER") or app.config.get("MCP_DEV_USERNAME") or "")


@contextmanager
def current_actor() -> Iterator[tuple[Any, Any]]:
    """(app, user) the tools act as: the user who asks inside a question, else the service user."""
    from flask import current_app, g, has_app_context

    if has_app_context() and getattr(g, "supagent_acting", False) and getattr(g, "user", None) is not None:
        yield current_app._get_current_object(), g.user
        return
    app = current_app._get_current_object() if has_app_context() else _APP
    if app is None:
        raise NotAllowed("no Superset application (run inside Superset: superset supagent ...)")
    with app.app_context():
        from superset.extensions import db

        name = service_username(app)
        if not name:
            raise NotAllowed("no service user: set SUPAGENT_MCP_USER (or MCP_DEV_USERNAME) in superset_config.py")
        try:
            with acting_as(name) as user:
                yield app, user
        finally:
            db.session.remove()


def can_use_database(database: Any) -> bool:
    from superset.extensions import security_manager

    try:
        return bool(security_manager.can_access_database(database))
    except Exception:  # pylint: disable=broad-except
        return False


def visible_databases() -> set[int]:
    """Ids of the Superset databases the current user may query."""
    from superset.extensions import db
    from superset.models.core import Database

    return {d.id for d in db.session.query(Database) if can_use_database(d)}
