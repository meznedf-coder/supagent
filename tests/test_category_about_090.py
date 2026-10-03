"""0.9: what each category is, written by an admin (in Categories, on the System map): shown with the category,
and given to the agent (the system picture, system_links) and to the router with the parts a question names."""

from __future__ import annotations

from test_brief import system  # noqa: F401  (the fixture: Billing = Invoicing + Payments, grid-a, ledger db)
from test_dictionary_080 import _client
from test_knowledge import world  # noqa: F401


def test_an_admin_writes_what_a_category_is(system, app):  # noqa: F811
    from supagent import settings
    from supagent.knowledge import facets as F

    api = "/supagent/admin/api/facets/categories"
    try:
        with _client(app, "admin") as c:
            r = c.post(api, json={"name": "pool", "about": "  a group of servers that share\n the same queue of slots "}).get_json()
            pool = next(x for x in r["categories"] if x["name"] == "pool")
            assert pool["about"] == "a group of servers that share the same queue of slots"
            # on the map: an admin writes it there too, everyone reads it
            m = c.post("/supagent/dictionary/api/map", json={"describe_category": {"name": "service", "about": "something the applications call"}})
            assert m.get_json() == {"name": "service", "about": "something the applications call"}
            assert c.post("/supagent/dictionary/api/map", json={"describe_category": {"name": "nothing", "about": "x"}}).status_code == 400
            cats = {x["name"]: x for x in c.get("/supagent/dictionary/api/map").get_json()["categories"]}
            assert cats["pool"]["about"].startswith("a group of servers") and cats["server"]["fields"] == "^(NODE|node)$"
        with _client(app, "alice") as c:                      # who is not an admin reads it with the categories they see
            seen = c.get("/supagent/dictionary/api/map").get_json()["categories"]
            assert all("about" in x for x in seen)
            assert c.post("/supagent/dictionary/api/map", json={"describe_category": {"name": "pool", "about": "x"}}).status_code == 403
        with app.app_context():
            assert F.about() == {"pool": "a group of servers that share the same queue of slots", "service": "something the applications call"}
            assert F.about(all_of_them=True)["application"] == "the applications, systems and services"   # the built-in ones say it
        with _client(app, "admin") as c:                      # renamed: its description follows; cleared; removed
            assert c.post(api, json={"name": "pool", "rename": "grid"}).status_code == 200
            assert c.post(api, json={"name": "service", "about": ""}).status_code == 200
        with app.app_context():
            assert F.about() == {"grid": "a group of servers that share the same queue of slots"}
        with _client(app, "admin") as c:
            assert c.post(api, json={"name": "grid", "rename": "pool"}).status_code == 200
    finally:
        with app.app_context():
            settings.set_value("categories.about", None)


def test_the_agent_and_the_router_are_given_what_the_categories_are(system, app):  # noqa: F811
    from supagent import router, settings
    from supagent.knowledge import facets as F
    from supagent.knowledge.brief import build, named_line
    from supagent.security import acting_as
    from supagent.tools import system_links

    try:
        with app.app_context():
            F.set_about("pool", "a group of servers that share the same queue of slots", "admin")
            F.set_about("application", "a program of the night batch", "admin")
            with acting_as("admin"):
                text = "\n".join(build("why are the jobs of Billing slow on grid-a?")["lines"])
                assert ("- What these categories are: application: a program of the night batch; pool: a group of servers "
                        "that share the same queue of slots.") in text
                assert system_links(["grid-a"])["categories"] == {"pool": "a group of servers that share the same queue of slots"}
            line = named_line("why are the jobs of Billing slow on grid-a?")
            assert line.startswith("The question names these parts of the system (the team's categories): Billing (subject: "
                                   "Invoices the customers); grid-a (pool: The compute pool).")
            assert line.endswith("Categories: pool = a group of servers that share the same queue of slots.")
            assert named_line("how many rows are there?") == ""
            msgs = router.messages("why is grid-a slow?", "", [], [], named_line("why is grid-a slow?"))
            assert msgs[1]["content"].startswith("The question names these parts of the system") and \
                msgs[1]["content"].endswith("Question: why is grid-a slow?")
            assert "The parts of the system the question names" in msgs[0]["content"]
            assert router.messages("hello", "", [], [])[1]["content"] == "Question: hello"       # nothing named: as before
    finally:
        with app.app_context():
            settings.set_value("categories.about", None)
