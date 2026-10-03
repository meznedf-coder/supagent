"""The categories of the knowledge: the values that need no approval are seeded (aspects, the categories people
wrote, the values of application-like fields), the LLM classifies what changed (known values used at once when it
is sure, new values proposed to an admin, relations resolved to items and tables), an item is classified again only
when its text changes, metrics are classified by family, and only approved categories are read."""

from __future__ import annotations

import json
import re

import pytest

from test_agent_loop import ScriptedLLM
from test_decider import lab  # noqa: F401  (the fixture: jobs index with APPLICATION values, metrics, entries)


@pytest.fixture()
def sure_used_at_once(monkeypatch):
    """categories.review_all off (on by default since 0.7): what the LLM is sure of is used at once, as in 0.6."""
    from supagent import settings

    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: False if key == "categories.review_all" else real(key))



@pytest.fixture()
def world(lab):  # noqa: F811
    from superset.extensions import db

    from supagent.models import Classified, Entry, Facet, Link, Tag

    for model in (Tag, Link, Facet, Classified):
        db.session.query(model).delete()
    e = Entry(title="Payments note", classification="guide", category="Payments", enabled=True, version=1,
              content="Payments are settled by the BILLING batch every night.")
    db.session.add(e)
    db.session.commit()
    yield {**lab, "entry": e.id}
    db.session.rollback()
    for model in (Tag, Link, Facet, Classified):
        db.session.query(model).delete()
    db.session.query(Entry).filter(Entry.title == "Payments note").delete(synchronize_session=False)
    db.session.commit()


def classify_reply(messages):
    """What a model would say: the jobs index is technical, about BILLING, part of a new subject; the note is
    functional, about Payments, and explains the jobs index; the other items are left as they are."""
    text = messages[-1]["content"]
    refs = re.findall(r"^\[([a-z]+:[^\]]+)\]", text, re.M)
    items = []
    for ref in refs:
        line = next(ln for ln in text.splitlines() if ln.startswith(f"[{ref}]"))
        if "batch-jobs" in line and ref.startswith("object:"):
            items.append({"ref": ref, "aspect": "technical", "applications": ["BILLING"],
                          "subjects": ["Batch processing"], "components": ["scheduler"], "confidence": "high",
                          "links": [{"to": "http_requests_total", "kind": "about"}]})
        elif "Payments note" in line:
            items.append({"ref": ref, "aspect": "functional", "subjects": ["Payments"], "confidence": "high",
                          "links": [{"to": "batch-jobs", "kind": "explains"}]})
        elif ref.startswith("family:") and "node" in ref:
            items.append({"ref": ref, "aspect": "technical", "components": ["servers"], "confidence": "medium"})
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": "classify_items", "arguments": json.dumps({"items": items, "new_values": [
            {"facet": "subject", "value": "Batch processing", "description": "the nightly jobs"}]})}}]}


class Replies(ScriptedLLM):
    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append(messages)
        return classify_reply(messages)


def test_seeded_classified_and_read(world, sure_used_at_once):
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Entry, Facet, Link, Tag

    seeded = F.seed()
    vocab = F.vocabulary()
    assert {v["value"] for v in vocab["aspect"]} == {"functional", "technical"}
    assert {"BILLING", "PAYROLL", "ORDERS"} <= {v["value"] for v in vocab["application"]}     # from the data
    assert any(v["value"] == "Payments" and v["status"] == "approved" for v in vocab["subject"])
    assert seeded["application"] >= 3
    llm = Replies([])
    out = F.classify(llm, seconds=60)
    assert out["calls"] >= 1 and out["items"] >= 3 and not F.pending()            # everything classified once
    jobs = db.session.query(Tag).join(Facet, Facet.id == Tag.facet_id).filter(Tag.ref.like("object:%"))
    got = {(f.facet, f.value): t.status for t, f in
           ((t, db.session.get(Facet, t.facet_id)) for t in jobs)}
    assert got[("application", "BILLING")] == "approved" and got[("aspect", "technical")] == "approved"
    assert got[("subject", "Batch processing")] == "proposed"                      # a new value waits for an admin
    assert db.session.query(Facet).filter(Facet.value == "Batch processing").one().status == "proposed"
    links = {(x.kind, x.b_ref) for x in db.session.query(Link)}
    assert ("about", f"data:{world['metrics']}:http_requests_total") in links
    assert {("explains", f"data:{world[k]}:batch-jobs") for k in ("main", "replica")} & links   # a table's name
    jobs_ref = next(t.ref for t in db.session.query(Tag).filter(Tag.ref.like("object:%")))
    assert "application: BILLING" in F.facets_of([jobs_ref])[jobs_ref]
    assert "subject: Batch processing" not in F.facets_of([jobs_ref])[jobs_ref]     # proposed: not read
    assert F.review_counts()["values"] >= 1
    e = db.session.get(Entry, world["entry"])
    e.content += " Late payments are escalated."
    db.session.commit()
    assert [it["ref"] for it in F.pending()] == [f"entry:{world['entry']}"]        # only what changed, again


def test_metrics_are_classified_by_family(world):
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import KObject

    fams = [it for it in F.items() if it["ref"].startswith("family:")]
    assert {it["title"] for it in fams} >= {"node_*", "http_*"}                    # never one item per metric
    F.seed()
    F.classify(Replies([]), seconds=60)
    cpu = db.session.query(KObject).filter(KObject.name == "node_cpu_seconds_total").first()
    assert "component: servers" not in (F.facets_of([f"object:{cpu.id}"]).get(f"object:{cpu.id}") or [])
    from supagent.models import Facet

    f = db.session.query(Facet).filter(Facet.value == "servers").one()
    f.status = "approved"                                                           # an admin approves the value
    db.session.commit()
    F._CACHE.update(at=0.0)
    from supagent.models import Tag

    for t in db.session.query(Tag).filter(Tag.facet_id == f.id):
        t.status = "approved"
    db.session.commit()
    F._CACHE.update(at=0.0)
    assert "component: servers" in F.facets_of([f"object:{cpu.id}"])[f"object:{cpu.id}"]


def test_only_what_needs_an_admin_waits(world, sure_used_at_once):
    """Medium or high tags of approved values are used at once; a "component" that names a table is a relation
    to it; the links saying two items are the same wait for an admin, and so do low ones; what an older rule left
    waiting is settled."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet, Link, Tag

    F.seed()
    ref = f"entry:{world['entry']}"
    batch = [{"ref": ref, "hash": "h"}]
    args = {"items": [{"ref": ref, "aspect": "functional", "applications": ["BILLING"], "confidence": "medium",
                       "components": ["batch-jobs", "scheduler"],
                       "links": [{"to": "batch-jobs", "kind": "same_as"}]}],
            "new_values": [{"facet": "component", "value": "batch-jobs"}]}
    F.apply(args, batch, F._table_refs())
    tags = {(db.session.get(Facet, t.facet_id).value, t.status) for t in db.session.query(Tag).filter(Tag.ref == ref)}
    assert ("BILLING", "approved") in tags and ("functional", "approved") in tags        # medium: used at once
    assert ("scheduler", "proposed") in tags                                              # a new value: waits
    assert not db.session.query(Facet).filter(Facet.value == "batch-jobs").count()        # a table: not a component
    links = {(x.kind, x.status) for x in db.session.query(Link).filter(Link.a_ref == ref)}
    assert ("about", "approved") in links and ("same_as", "proposed") in links
    t = db.session.query(Tag).join(Facet, Facet.id == Tag.facet_id).filter(Tag.ref == ref,
                                                                              Facet.value == "BILLING").one()
    t.status = "proposed"                                                                 # left by an older rule
    db.session.commit()
    assert F.settle()["tags"] == 1 and db.session.get(Tag, t.id).status == "approved"


def test_a_code_of_one_letter_is_not_an_application(world):
    """The values of application-like fields name the applications, but not a code like A or 42. One an older rule
    seeded stays as it is (0.9: no value in use is retired by the learning; its removal is proposed, test_retire)."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet, Tag

    assert F.named("BILLING") and F.named("R2") and not F.named("A") and not F.named("42")
    f = Facet(facet="application", value="Z", status="approved", source="data")
    db.session.add(f)
    db.session.flush()
    db.session.add(Tag(ref="entry:1", facet_id=f.id, confidence=0.9, source="llm", status="approved"))
    db.session.commit()
    F.settle()
    db.session.refresh(f)
    assert f.status == "approved" and db.session.query(Tag).filter(Tag.facet_id == f.id).count() == 1


def test_the_prompt_says_whose_each_piece_is(world, sure_used_at_once):
    """The knowledge given with a question names each piece's application and subject (approved ones)."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.knowledge.index import sync
    from supagent.knowledge.search import knowledge_block
    from supagent.models import Facet, Tag
    from supagent.security import acting_as

    sync()
    F.seed()
    f = db.session.query(Facet).filter(Facet.facet == "application", Facet.value == "BILLING").one()
    db.session.add(Tag(ref=f"entry:{world['entry']}", facet_id=f.id, confidence=0.9, source="llm", status="approved"))
    db.session.commit()
    F._CACHE.update(at=0.0)
    with acting_as("admin"):
        text = knowledge_block("How are payments settled by the batch?", set())
    line = next(ln for ln in text.splitlines() if "Payments note" in ln)
    assert "(BILLING" in line and "aspect" not in line


def test_the_learning_says_what_new_and_known_values_are_part_of(world):
    """The user: categories found while learning (from metrics, notes, the catalog, memories) can be part of one or
    more known ones (a jvm of two applications and four components), or the same as a known one (a merge)."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet

    known = {}
    for facet, value in (("application", "LEDGER"), ("application", "PAYMENTS"), ("component", "posting engine"),
                         ("component", "risk grid"), ("subject", "Settlement")):
        known[value] = F._ensure(facet, value, "approved", "admin")
    db.session.commit()
    batch = [{"ref": f"entry:{world['entry']}", "hash": "h"}]
    args = {"items": [], "new_values": [
        {"facet": "component", "value": "jvm", "description": "the Java virtual machines",
         "part_of": ["LEDGER", "PAYMENTS", "posting engine", "Risk Grid", "nothing known"]},
        {"facet": "subject", "value": "Settlements", "same_as": "Settlement"}],
        "known_value_parents": [{"value": "posting engine", "part_of": ["LEDGER"]},
                                {"value": "unknown thing", "part_of": ["LEDGER"]}]}
    F.apply(args, batch, {})
    jvm = db.session.query(Facet).filter(Facet.value == "jvm").one()
    assert jvm.status == "proposed" and set(jvm.parents) == {known[v].id for v in
                                                              ("LEDGER", "PAYMENTS", "posting engine", "risk grid")}
    dup = db.session.query(Facet).filter(Facet.value == "Settlements").one()
    assert dup.suggested == {"same_as": known["Settlement"].id}
    engine = db.session.get(Facet, known["posting engine"].id)
    assert engine.parents is None and engine.suggested == {"parents": [known["LEDGER"].id]}   # an admin decides


def test_the_system_map_counts_the_items_that_exist_now(world):
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.knowledge.index import sync
    from supagent.models import Facet, KObject, Tag

    app_ = F._ensure("application", "LEDGER", "approved", "admin")
    comp = F._ensure("component", "posting engine", "approved", "admin")
    db.session.flush()
    comp.parents = [app_.id]
    metric = db.session.query(KObject).filter(KObject.kind == "metric").first()
    db.session.add_all([Tag(ref=f"object:{metric.id}", facet_id=comp.id, confidence=0.9, source="admin", status="approved"),
                        Tag(ref=f"entry:{world['entry']}", facet_id=comp.id, confidence=0.9, source="admin", status="approved")])
    db.session.commit()
    sync()
    nodes = {n["value"]: n for n in F.system_map()}
    assert nodes["posting engine"]["parents"] == [app_.id]
    assert nodes["posting engine"]["kinds"] == {"metrics": 1, "catalog entries": 1}
    metric.gone_at = __import__("datetime").datetime.utcnow()                  # the metric is removed from the source
    db.session.commit()
    nodes = {n["value"]: n for n in F.system_map()}
    assert nodes["posting engine"]["kinds"] == {"catalog entries": 1} and nodes["posting engine"]["tagged"] == 2
    metric.gone_at = None
    db.session.commit()


def test_own_categories_read_from_the_fields_and_related_by_the_data(world, monkeypatch):
    """The user: categories come from the values in the indices too (applications, components, servers...), a
    category list of one's own, where each value comes from, and the relations the data shows, to approve."""
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import facets as F
    from supagent.knowledge.learn_indices import category_pairs
    from supagent.models import Facet, KObject, Source

    conf = {"categories.custom": ["server"], "categories.fields": {"application": r"^(application|app)$",
                                                                    "server": r"^(node|host)$"},
            "categories.relation_min_docs": 5}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    assert F.editable() == ("subject", "application", "component", "server")
    src = db.session.query(Source).first()
    idx = KObject(source_id=src.id, kind="index", name="ops-jobs", parent="")
    fld_app = KObject(source_id=src.id, kind="field", name="APPLICATION", parent="ops-jobs",
                      stats={"values": ["LEDGER", "PAYMENTS"]})
    fld_node = KObject(source_id=src.id, kind="field", name="NODE", parent="ops-jobs",
                       stats={"values": ["srv-01", "srv-02", "srv-03"]})
    db.session.add_all([idx, fld_app, fld_node])
    db.session.commit()

    class Conn:
        class transport:  # noqa: N801
            @staticmethod
            def search(index, body):
                assert body["aggs"]["p"]["terms"]["field"] == "APPLICATION"      # the wider category first
                return {"aggregations": {"p": {"buckets": [
                    {"key": "LEDGER", "c": {"buckets": [{"key": "srv-01", "doc_count": 40}, {"key": "srv-02", "doc_count": 2}]}},
                    {"key": "PAYMENTS", "c": {"buckets": [{"key": "srv-01", "doc_count": 9}, {"key": "srv-03", "doc_count": 30}]}}]}}}

    class Fld:
        def __init__(self, name):
            self.name, self.agg_field, self.sql_type = name, name, "VARCHAR"

    pairs = category_pairs(Conn(), "ops-jobs", [Fld("NODE"), Fld("APPLICATION"), Fld("STATUS")], 1000)
    assert pairs[0]["parent"] == ["application", "APPLICATION"] and pairs[0]["child"] == ["server", "NODE"]
    idx.stats = {"category_pairs": pairs}
    db.session.commit()
    n = F.seed()
    assert n["server"] >= 3 and n["relations"] == 3                       # srv-02 with LEDGER: 2 documents, below 5
    srv1 = db.session.query(Facet).filter(Facet.facet == "server", Facet.value == "srv-01").one()
    # 0.9: what the learning reads in the data waits for an admin, like what the LLM proposes
    assert srv1.status == "proposed" and srv1.source == "data" and n["proposed"] >= 5
    assert srv1.origins == [f"field NODE of ops-jobs ({src.database_name})"]
    names = {db.session.get(Facet, i).value for i in srv1.suggested["parents"]}
    assert names == {"LEDGER", "PAYMENTS"} and srv1.parents is None          # proposed, never applied alone
    assert "LEDGER with NODE srv-01 in 40 documents" in " ".join(srv1.suggested["from"].values())
    again = F.seed()
    assert again["proposed"] == 0 and db.session.get(Facet, srv1.id).status == "proposed"    # nothing new: nothing said
    conf["categories.review_all"] = False                                   # as before 0.9: used at once
    F.seed()
    assert db.session.get(Facet, srv1.id).status == "approved"
    db.session.query(KObject).filter(KObject.parent == "ops-jobs").delete(synchronize_session=False)
    db.session.delete(idx)
    db.session.commit()


def test_a_name_filed_under_another_category_is_that_value_not_a_new_one(world, sure_used_at_once):
    """0.9: the LLM files parts as "components"; when the team already has that name as a value of another
    category (a service of theirs), the item is about that value and nothing new waits for an admin. A subject
    is a topic: it may share its name with a part. What an older rule left waiting says which approved value has
    its name: nothing is merged without an admin."""
    from superset.extensions import db

    from supagent import settings
    from supagent.knowledge import facets as F
    from supagent.models import Facet, Tag

    settings.set_value("categories.custom", ["service"])
    try:
        F.seed()
        svc = Facet(facet="service", value="ledger db", status="approved", source="admin", synonyms=["LEDGERDB"])
        old = Facet(facet="component", value="Ledger DB", status="proposed", source="llm")       # from before 0.9
        db.session.add_all([svc, old])
        db.session.commit()
        assert db.session.query(Facet).filter(Facet.facet == "subject", Facet.value == "Payments").one().status == \
            "approved"                                                   # seeded from the category people wrote
        ref = f"entry:{world['entry']}"
        db.session.add(Tag(ref="doc:1", facet_id=old.id, confidence=0.9, source="llm", status="proposed"))
        db.session.commit()
        assert F.settle()["values"] == 1                                 # the old duplicate says what it looks like
        assert db.session.get(Facet, old.id).suggested == {"same_as": svc.id}        # an admin merges it, in one click
        assert db.session.query(Tag).filter(Tag.ref == "doc:1").one().facet_id == old.id and svc.synonyms == ["LEDGERDB"]
        assert F.settle()["values"] == 0                                 # said once
        db.session.delete(db.session.query(Tag).filter(Tag.ref == "doc:1").one())
        db.session.delete(old)
        db.session.commit()
        args = {"items": [{"ref": ref, "aspect": "technical", "confidence": "high", "applications": ["Payments"],
                           "components": ["ledger db", "LEDGERDB", "scheduler"]}],
                "new_values": [{"facet": "component", "value": "ledger db", "description": "the ledger"},
                               {"facet": "application", "value": "Payments"}]}
        n = F.apply(args, [{"ref": ref, "hash": "h"}], F._table_refs())
        assert db.session.query(Facet).filter(Facet.facet == "component", Facet.value.ilike("ledger db")).count() == 0
        tags = {(f.facet, f.value, t.status) for t, f in db.session.query(Tag, Facet).join(
            Facet, Facet.id == Tag.facet_id).filter(Tag.ref == ref)}
        assert ("service", "ledger db", "approved") in tags              # by its name and by its other name: once
        assert ("component", "scheduler", "proposed") in tags            # a name nobody has: proposed as before
        assert ("application", "Payments", "proposed") in tags           # the subject Payments is another thing
        assert n["proposed"] == 1                                        # only the application
    finally:
        settings.set_value("categories.custom", None)


def test_a_proposed_interaction_waits_whatever_its_confidence(world, sure_used_at_once):
    """0.9: an interaction between two parts read in a text is drawn and followed only once an admin approved
    it: the rule that uses the confident links at once leaves it waiting."""
    from superset.extensions import db

    from supagent.knowledge import facets as F
    from supagent.models import Facet, Link

    a = Facet(facet="application", value="Invoicing", status="approved", source="admin")
    b = Facet(facet="application", value="Payments", status="approved", source="admin")
    db.session.add_all([a, b])
    db.session.flush()
    db.session.add(Link(a_ref=f"facet:{a.id}", b_ref=f"facet:{b.id}", kind="depends_on", confidence=0.7, source="llm",
                        status="proposed", note="Invoicing waits for Payments. (a document)"))
    db.session.add(Link(a_ref="entry:1", b_ref="entry:2", kind="about", confidence=0.9, source="llm", status="proposed"))
    db.session.commit()
    assert F.settle()["links"] == 1                                      # the relation between two items: used
    assert db.session.query(Link).filter(Link.kind == "depends_on").one().status == "proposed"
