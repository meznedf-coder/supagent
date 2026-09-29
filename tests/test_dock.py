"""The chat panel docked on Superset's pages: added to Superset's own place for custom page
scripts (tail_js_custom_extra.html, whatever the deployment put there is kept), only for users who
may chat, never on embedded or standalone pages; the chat page's compact mode for the panel."""

from __future__ import annotations

from jinja2 import DictLoader, Environment

from conftest import login


def _render(loader, **context) -> str:
    import supagent

    env = Environment(loader=supagent._dock_loader(loader))
    env.globals.update(supagent_dock=lambda: "/supagent/", supagent_static=lambda f: f"/s/{f}")
    return env.get_template(supagent.DOCK_TEMPLATE).render(**context)


def test_the_panel_is_added_to_what_the_deployment_put_there():
    import supagent

    custom = DictLoader({supagent.DOCK_TEMPLATE: "<script>analytics()</script>", "other.html": "x"})
    out = _render(custom)
    assert 'src="/s/dock.js" data-chat="/supagent/"' in out and out.endswith("<script>analytics()</script>")
    assert 'href="/s/dock.css"' in out
    assert "dock.js" in _render(DictLoader({}))                      # no file there: the panel alone
    assert _render(custom, standalone_mode=True).strip() == "<script>analytics()</script>"   # standalone dashboard
    assert _render(custom, entry="embedded").strip() == "<script>analytics()</script>"       # embedded dashboard
    env = Environment(loader=supagent._dock_loader(custom))
    assert env.get_template("other.html").render() == "x"
    assert supagent._dock_loader(env.loader) is env.loader            # added once


def test_the_panel_is_on_superset_pages_of_users_who_may_chat(app):
    with app.test_client() as c:
        login(c, "alice")
        page = c.get("/superset/welcome/").get_data(as_text=True)
    assert "supagent-static/dock.js" in page and 'data-chat="/supagent/"' in page
    with app.test_client() as c:
        login(c, "nobody")                                            # no access to the chat
        page = c.get("/superset/welcome/").get_data(as_text=True)
    assert "dock.js" not in page
    with app.test_client() as c:
        page = c.get("/login/").get_data(as_text=True)                # not logged in
    assert "dock.js" not in page


def test_the_chat_page_in_the_panel_is_compact(app):
    with app.test_client() as c:
        login(c, "alice")
        docked = c.get("/supagent/?embed=1").get_data(as_text=True)
        full = c.get("/supagent/").get_data(as_text=True)
    assert '<base target="_top">' in docked and 'data-embed="yes"' in docked and 'class="topbar"' not in docked
    assert '<base target="_top">' not in full and 'class="topbar"' in full
