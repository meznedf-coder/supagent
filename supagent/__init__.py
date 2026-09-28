"""supagent: an AI agent inside Apache Superset.

* a chat page in Superset (tab "Chat" of the top bar), answers computed by Superset's Celery workers
  with the tools acting as the user who asks (Superset's own permissions);
* a data dictionary stored in Superset's database and learned every day: every metric
  (Prometheus / Mimir through promagg) and every index field (OpenSearch through osagg), its
  type, unit, meaning, typical values, and how metrics and fields relate (measured value
  overlap), with the changes from one day to the next;
* the same tools as an MCP service for other agents (`superset supagent mcp`).

Enable it with one line in superset_config.py (registration only, no logic):

    from supagent import init_app as FLASK_APP_MUTATOR

then `superset supagent init` (tables, role "AI Agent") and restart the web server, the Celery
workers and beat. With an existing FLASK_APP_MUTATOR: `FLASK_APP_MUTATOR = supagent.chain(old)`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

__version__ = "0.2.2"

log = logging.getLogger(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = "supagent_static"


MENU_ITEMS = {"chat": "AI agent chat", "dictionary": "AI agent dictionary", "admin": "AI agent settings"}
SETTINGS_CATEGORY = "Manage"            # Superset's Settings menu, section Manage


def init_app(app: Any) -> None:
    """Superset's FLASK_APP_MUTATOR: views, API, Celery tasks, the daily schedule."""
    from flask import Blueprint
    from superset.extensions import appbuilder, security_manager

    from supagent import tasks  # noqa: F401  (registers the Celery tasks)
    from supagent.tasks import add_beat_schedule
    from supagent.views import AdminView, ChatView, KnowledgeView

    from supagent.views import NotFound, _not_found

    app.register_error_handler(NotFound, _not_found)
    if _STATIC not in app.blueprints:
        app.register_blueprint(Blueprint(_STATIC, __name__, static_folder=os.path.join(HERE, "static", "supagent"),
                                         static_url_path="/supagent-static"))
    # `superset init` gives every new view to Gamma unless it is admin-only: the agent's views
    # are admin-only there, and `superset supagent init` gives the chat and the dictionary to
    # the role "AI Agent" (the settings stay with the admins)
    for name in (ChatView.class_permission_name, KnowledgeView.class_permission_name, AdminView.class_permission_name,
                 *MENU_ITEMS.values()):
        security_manager.ADMIN_ONLY_VIEW_MENUS.add(name)
    # "Chat": a tab of Superset's top bar, like Dashboards or SQL; the dictionary and the settings
    # are in Superset's Settings menu (and in the tabs of the chat page)
    appbuilder.add_view(ChatView, MENU_ITEMS["chat"], label="Chat", icon="fa-comments")
    appbuilder.add_view(KnowledgeView, MENU_ITEMS["dictionary"], label="Data dictionary", icon="fa-book",
                        category=SETTINGS_CATEGORY)
    appbuilder.add_view(AdminView, MENU_ITEMS["admin"], label="Chat settings", icon="fa-cog",
                        category=SETTINGS_CATEGORY)
    add_beat_schedule()
    log.info("supagent %s: chat, data dictionary and daily learning enabled", __version__)


def chain(previous: Callable[[Any], None] | None) -> Callable[[Any], None]:
    """FLASK_APP_MUTATOR that runs an existing mutator, then supagent's."""

    def mutator(app: Any) -> None:
        if previous is not None:
            previous(app)
        init_app(app)

    return mutator
