"""0.9: the System map's arrangement an admin saves: where a category was moved, and the groups of categories drawn
as frames (display only). A category is in one group; a renamed or removed category is followed."""

from __future__ import annotations

from test_dictionary_080 import _client


def _reset(app):
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import sysmap
    from supagent.models import Facet

    with app.app_context():
        sysmap.save_layout({}, "admin")
        db.session.query(Facet).filter(Facet.facet.in_(("server", "host", "pool"))).delete(synchronize_session=False)
        db.session.commit()
        settings.set_value("categories.custom", None)
        settings.set_value("categories.fields", None)


def test_the_layout_keeps_moved_categories_and_groups(app):
    try:
        with _client(app, "admin") as c:
            lay = c.post("/supagent/dictionary/api/map", json={"layout": {
                "positions": {"7": [10, 20]}, "folded": {"server": True},
                "columns": {"Server": [120, -30.26], "pool": [0, 0], "bad": "x"},
                "groups": [{"name": "  Infrastructure  ", "categories": ["server", "pool", "server"]},
                           {"name": "Other", "categories": ["pool", "network"]},          # pool: in the first one
                           {"name": "", "categories": ["x"]}, {"name": "Empty", "categories": []}, "junk"]}}).get_json()["layout"]
            assert lay["columns"] == {"server": [120.0, -30.3]}                       # not moved: not kept
            assert lay["groups"] == [{"name": "Infrastructure", "categories": ["server", "pool"]},
                                     {"name": "Other", "categories": ["network"]}]
            assert lay["positions"] == {"7": [10.0, 20.0]} and lay["folded"] == {"server": True}
            many = [{"name": f"g{i}", "categories": [f"c{i}"]} for i in range(30)]
            assert len(c.post("/supagent/dictionary/api/map", json={"layout": {"groups": many}}).get_json()["layout"]["groups"]) == 12
            c.post("/supagent/dictionary/api/map", json={"layout": {"columns": {"server": [5, 6]},
                                                                    "groups": [{"name": "Infra", "categories": ["server", "pool"]}]}})
        with _client(app, "alice") as c:                                              # everyone sees the arrangement
            seen = c.get("/supagent/dictionary/api/map").get_json()["layout"]
            assert seen["groups"] == [{"name": "Infra", "categories": ["server", "pool"]}] and seen["columns"] == {"server": [5.0, 6.0]}
            assert c.post("/supagent/dictionary/api/map", json={"layout": {"groups": []}}).status_code == 403
    finally:
        _reset(app)


def test_a_renamed_or_removed_category_is_followed(app):
    from supagent.knowledge import sysmap

    api = "/supagent/admin/api/facets/categories"
    try:
        with _client(app, "admin") as c:
            c.post(api, json={"name": "server"})
            c.post(api, json={"name": "pool"})
            c.post("/supagent/dictionary/api/map", json={"layout": {
                "columns": {"server": [40, 0], "pool": [0, 25]}, "folded": {"server": True},
                "groups": [{"name": "Infra", "categories": ["server", "pool"]}]}})
            assert c.post(api, json={"name": "server", "rename": "host"}).status_code == 200
        with app.app_context():
            lay = sysmap.layout()
            assert lay["columns"] == {"host": [40.0, 0.0], "pool": [0.0, 25.0]} and lay["folded"] == {"host": True}
            assert lay["groups"] == [{"name": "Infra", "categories": ["host", "pool"]}]
        with _client(app, "admin") as c:
            assert c.post(api, json={"name": "pool", "remove": True}).status_code == 200
        with app.app_context():
            lay = sysmap.layout()
            assert lay["columns"] == {"host": [40.0, 0.0]} and lay["groups"] == [{"name": "Infra", "categories": ["host"]}]
        with _client(app, "admin") as c:
            assert c.post(api, json={"name": "host", "remove": True}).status_code == 200
        with app.app_context():
            lay = sysmap.layout()
            assert lay["columns"] == {} and lay["groups"] == [] and lay["folded"] == {}
    finally:
        _reset(app)
