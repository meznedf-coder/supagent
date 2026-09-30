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

import functools
import hashlib
import logging
import os
from typing import Any, Callable

__version__ = "0.5.1"

log = logging.getLogger(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = "supagent_static"


MENU_ITEMS = {"chat": "AI agent chat", "dictionary": "AI agent dictionary", "admin": "AI agent settings"}
SETTINGS_CATEGORY = "Manage"            # Superset's Settings menu, section Manage


@functools.lru_cache(maxsize=None)
def _file_hash(filename: str) -> str:
    try:
        with open(os.path.join(HERE, "static", "supagent", filename), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:10]
    except OSError:
        return __version__


def static_url(filename: str) -> str:
    """URL of a page file with its content hash: Superset lets browsers keep static files for a
    year (SEND_FILE_MAX_AGE_DEFAULT), so after an upgrade they must see a new URL, or they keep
    the former CSS and JavaScript (half-dark pages, former fixes missing)."""
    from flask import url_for

    return url_for(f"{_STATIC}.static", filename=filename, v=_file_hash(filename))


# Superset's pages include tail_js_custom_extra.html, the place meant for custom page scripts:
# the chat panel is added in front of what the deployment may have put there (kept as it is)
DOCK_TEMPLATE = "tail_js_custom_extra.html"
DOCK_SNIPPET = (
    "{% if supagent_dock is defined and entry != 'embedded' and not standalone_mode %}"
    "{% set supagent_chat = supagent_dock() %}{% if supagent_chat %}"
    '<link rel="stylesheet" href="{{ supagent_static(\'dock.css\') }}">'
    '<script src="{{ supagent_static(\'dock.js\') }}" data-chat="{{ supagent_chat }}"'
    '{% if csp_nonce is defined %} nonce="{{ csp_nonce() }}"{% endif %}></script>'
    "{% endif %}{% endif %}\n")


def dock_url() -> str:
    """The chat page's URL when the Superset page being drawn may show the chat panel: a user
    who is logged in and may use the chat; else ""."""
    try:
        from flask import g, url_for
        from superset.extensions import security_manager

        user = getattr(g, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return ""
        if not security_manager.can_access("can_read", "AIAgent"):
            return ""
        return url_for("ChatView.index")
    except Exception:  # pylint: disable=broad-except   (a page is never broken by the panel)
        return ""


def _dock_loader(inner: Any) -> Any:
    """The app's template loader, with the chat panel added to tail_js_custom_extra.html."""
    from jinja2 import BaseLoader, TemplateNotFound

    class DockLoader(BaseLoader):
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        def get_source(self, environment: Any, template: str) -> Any:
            if template != DOCK_TEMPLATE:
                return self.wrapped.get_source(environment, template)
            try:
                source, filename, uptodate = self.wrapped.get_source(environment, template)
            except TemplateNotFound:
                source, filename, uptodate = "", None, None
            return DOCK_SNIPPET + source, filename, uptodate

        def list_templates(self) -> list[str]:
            return self.wrapped.list_templates()

    if type(inner).__name__ == "DockLoader":
        return inner
    return DockLoader(inner)


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
    app.jinja_env.globals["supagent_static"] = static_url
    app.jinja_env.globals["supagent_dock"] = dock_url
    app.jinja_env.loader = _dock_loader(app.jinja_env.loader)      # the chat panel on Superset's pages
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
    from supagent import workers

    workers.register(app)                  # the workers' heartbeat (in a `celery worker` only)
    log.info("supagent %s: chat, data dictionary and daily learning enabled", __version__)


def chain(previous: Callable[[Any], None] | None) -> Callable[[Any], None]:
    """FLASK_APP_MUTATOR that runs an existing mutator, then supagent's."""

    def mutator(app: Any) -> None:
        if previous is not None:
            previous(app)
        init_app(app)

    return mutator
