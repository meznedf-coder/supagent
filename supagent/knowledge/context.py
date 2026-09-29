"""Context: what the system is, functionally and technically, written every night (context.hour,
after the day's learning run) from the knowledge the team shares: the documents and sites, the
catalog, the team memory, the data dictionary (databases, categories, relations, the values of
labels and fields such as servers, services, applications, environments) and the answers marked
Helpful. Raw chats are not read: they were answered with each user's own permissions.

Facts pages are written without the LLM: each database's data sources and inventory, the glossary,
the rules and facts. Summary pages are written by the LLM from the evidence given only (marked
AI-written, citing their sources): how the system works, the technical overview, one page per main
application. A summary page is written again only when its evidence changed (input hash), at most
context.max_llm_calls LLM calls per build: a second build with nothing new asks the LLM nothing. A
page a person edited is never written over by the agent.

A page is shown (Data dictionary -> Context, and to the agent through the knowledge search, below
the catalog and the documents) only to the users who may query every database it draws from.
The build is a run of kind "context" (the runs list, its steps, Stop); it never runs next to a
learning run. Code read through MCP servers can be added later as one more source of evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from typing import Any

from superset import db

from supagent import settings
from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Recipe, Relation, Run, Source

log = logging.getLogger(__name__)
SECTIONS = ("functional", "technical")
INVENTORY = {   # what a label or field of these names holds (lower case, exact names)
    "Servers and hosts": ("node", "host", "hostname", "server", "server_name", "instance", "machine", "vm"),
    "Services and jobs": ("service", "service_name", "job", "job_name", "component", "task_name"),
    "Applications": ("application", "app", "app_name", "application_name", "system"),
    "Environments": ("env", "environment", "environment_type", "stage"),
    "Regions and sites": ("region", "datacenter", "dc", "site", "zone", "location"),
    "Clusters and pools": ("cluster", "namespace", "pool", "queue"),
    "Tenants (each usually a different application or subject)": ("__tenant_id__", "tenant_id", "tenant", "org_id",
                                                                   "x_scope_orgid"),
    "Teams": ("team", "owner", "squad"),
}
MAIN_ITEMS = 25            # the main metrics and indices of a data source on its page (a person's, used, catalog)
MAX_VALUES = 300           # values listed per inventory line
MAX_APPS = 8               # application pages at most
DOC_CHARS = 3000           # of each document in an LLM prompt
PROMPT = """You write one page of the internal documentation of an information system, in Markdown, from the
evidence given as JSON (documents, catalog entries, team memory, data dictionary, questions people asked).
Rules: use only this evidence, never invent a name, a number, a link or a dependency; when the evidence does
not say, write "not known yet". Cite the evidence you use as [E1], [E2]... Use ## headings, short paragraphs
and bullet lists, no HTML, no code fences unless quoting a query. Write in the language of most of the
evidence. The page: {title}. {brief}"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:100] or "page"


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# what is known
# --------------------------------------------------------------------------- #
def _importance() -> dict[tuple[int, str], float]:
    """(database id, metric or index name) -> how much it matters to the team: used by the answers
    (where the data was, learned answers), described by the catalog."""
    import math

    from supagent.knowledge.curated import catalog_texts
    from supagent.models import Association

    out: dict[tuple[int, str], float] = defaultdict(float)
    try:
        for dbid, name, uses in db.session.query(Association.database_id, Association.name, Association.uses):
            out[(dbid, name)] += 20 * math.log(1 + (uses or 1))
        for dbid, target in (db.session.query(Recipe.database_id, Recipe.target)
                             .filter(Recipe.status.in_(("helpful", "confirmed")))):
            if dbid and target:
                out[(dbid, target)] += 30
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    named = {name for (kind, _parent, name) in catalog_texts() if kind in ("metric", "index")}
    return {**out, **{("*", n): 50.0 for n in named}}


def collect() -> dict[str, Any]:
    """The shared knowledge, bounded: databases, inventories, relations, catalog, documents, team
    memory, Helpful answers."""
    from superset.models.core import Database

    from supagent.knowledge.catalog import AGENT
    from supagent.tools import agent_databases

    dbs = {d.id: d for d in agent_databases(list(db.session.query(Database).order_by(Database.id)))}
    sources = {s.id: s for s in db.session.query(Source).filter(Source.database_id.in_(list(dbs) or [-1]))}
    weight = _importance()
    out: dict[str, Any] = {"databases": {}, "inventory": defaultdict(dict), "relations": [], "entries": [],
                           "docs": [], "memory": [], "answers": []}
    for sid, s in sources.items():
        objs = db.session.query(KObject).filter(KObject.source_id == sid, KObject.gone_at.is_(None))
        counts: dict[str, int] = defaultdict(int)
        categories: dict[str, int] = defaultdict(int)
        main = []
        for o in objs:
            counts[o.kind] += 1
            if o.kind in ("metric", "index") and o.category:
                categories[o.category] += 1
            if o.kind in ("metric", "index", "family"):                # what matters first (not all of 10,000)
                score = weight.get((s.database_id, o.name), 0) + weight.get(("*", o.name), 0) \
                    + (100 if o.description_source == "curated" else 0) \
                    + (5 if o.description else 0) + (10 if o.kind in ("index", "family") else 0)
                main.append((-score, o.kind, o.name, (o.description or "").strip()[:200]))
            names = [dim for dim, keys in INVENTORY.items() if o.kind in ("label", "field") and o.name.lower() in keys]
            if names:
                st = o.stats or {}
                vals = st.get("values") or st.get("sample") or []
                slot = out["inventory"][names[0]].setdefault(s.database_id, {"holders": [], "values": set(),
                                                                            "partial": False})
                slot["holders"].append(f"{o.kind} {o.name} of {o.parent}")
                slot["values"].update(map(str, vals))
                slot["partial"] = slot["partial"] or bool(st.get("partial") or st.get("sample"))
        if not counts:                                   # nothing learned there (yet): no page
            continue
        main.sort()
        d = dbs.get(s.database_id)
        out["databases"][s.database_id] = {
            "name": s.database_name or (d.database_name if d else str(s.database_id)), "backend": s.backend,
            "counts": dict(counts), "categories": dict(sorted(categories.items(), key=lambda x: -x[1])[:20]),
            "main": [{"kind": k, "name": n, "about": t} for _s, k, n, t in main[:MAIN_ITEMS]],
            "others": max(0, len(main) - MAIN_ITEMS)}
    for dim, per_db in out["inventory"].items():
        for slot in per_db.values():
            slot["values"] = sorted(slot["values"])
    by_obj = {o.id: o for o in db.session.query(KObject).filter(
        KObject.id.in_(db.session.query(Relation.a_id).filter(Relation.rejected_at.is_(None))) |
        KObject.id.in_(db.session.query(Relation.b_id).filter(Relation.rejected_at.is_(None))))}
    for r in db.session.query(Relation).filter(Relation.rejected_at.is_(None)).limit(400):
        a, b = by_obj.get(r.a_id), by_obj.get(r.b_id)
        if a is None or b is None or a.source_id == b.source_id or a.source_id not in sources \
                or b.source_id not in sources:
            continue
        out["relations"].append({"a": f"{a.parent + '.' if a.parent else ''}{a.name}",
                                 "a_db": sources[a.source_id].database_id,
                                 "b": f"{b.parent + '.' if b.parent else ''}{b.name}",
                                 "b_db": sources[b.source_id].database_id, "origin": r.origin})
    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True)).order_by(Entry.id):
        database_id = (e.evidence or {}).get("database_id")
        out["entries"].append({"id": e.id, "title": e.title, "classification": e.classification,
                               "category": e.category, "content": (e.content or "")[:1500],
                               "agent": e.updated_by == AGENT, "database_id": database_id})
    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.status == "ok").order_by(Doc.id):
        out["docs"].append({"id": d.id, "title": d.title or d.url or f"document {d.id}", "url": d.url,
                            "content": d.content or ""})
    for m in db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "active").order_by(Memory.id):
        out["memory"].append({"id": m.id, "kind": m.kind, "text": m.text})
    for r in (db.session.query(Recipe).filter(Recipe.status.in_(("helpful", "confirmed")))
              .order_by(Recipe.id.desc()).limit(200)):
        out["answers"].append({"id": r.id, "question": r.question, "tool": r.tool, "database_id": r.database_id})
    db.session.commit()
    out["inventory"] = dict(out["inventory"])
    return out


# --------------------------------------------------------------------------- #
# facts pages (no LLM)
# --------------------------------------------------------------------------- #
def fact_pages(ev: dict[str, Any]) -> list[dict[str, Any]]:
    pages = []
    names = {i: d["name"] for i, d in ev["databases"].items()}
    for dbid, d in ev["databases"].items():
        lines = [f"# {d['name']}", "", f"Engine: {d['backend']}. Learned objects: " +
                 ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in sorted(d["counts"].items())) + "."]
        if d["categories"]:
            lines += ["", "## Domains", ""] + [f"- {c}: {n}" for c, n in d["categories"].items()]
        if d["main"]:
            lines += ["", "## Main data", "", "What the team described or uses most (the Data dictionary has every "
                      "object)."] + [f"- {m['kind']} `{m['name']}`" + (f": {m['about']}" if m["about"] else "")
                                     for m in d["main"]]
            if d.get("others"):
                lines.append(f"- ... and {d['others']:,} more, in the domains above")
        formulas = [e for e in ev["entries"] if e["database_id"] == dbid and e["classification"] == "formula"]
        if formulas:
            lines += ["", "## Formulas", ""] + [f"- {e['title']}: {e['content'][:300]}" for e in formulas]
        pages.append({"section": "technical", "slug": f"data-sources-{slugify(d['name'])}",
                      "title": f"Data source: {d['name']}", "content": "\n".join(lines), "database_ids": [dbid],
                      "sources": [{"ref": f"database:{dbid}", "title": d["name"]}]})
        inv = [(dim, per_db[dbid]) for dim, per_db in ev["inventory"].items() if dbid in per_db]
        if inv:
            lines = [f"# Inventory from {d['name']}", "",
                     "The values of the labels and fields that name servers, services, applications, environments, "
                     "regions, clusters and teams (learned from the data; a list may be a sample)."]
            for dim, slot in sorted(inv):
                vals = slot["values"]
                lines += ["", f"## {dim}", "", "Found in: " + "; ".join(sorted(set(slot["holders"]))[:12]) + ".", ""]
                shown = ", ".join(vals[:MAX_VALUES]) if vals else "(no values learned yet)"
                more = f" ... ({len(vals)} values)" if len(vals) > MAX_VALUES else f" ({len(vals)} values)"
                lines.append(shown + (more if vals else "") + (" - a sample of the values" if slot["partial"] else ""))
            pages.append({"section": "technical", "slug": f"inventory-{slugify(d['name'])}",
                          "title": f"Inventory: {d['name']}", "content": "\n".join(lines), "database_ids": [dbid],
                          "sources": [{"ref": f"database:{dbid}", "title": d["name"]}]})
    pairs: dict[tuple[int, int], list[dict]] = {}          # the links of two sources: a page of both (seen only
    for r in ev["relations"]:                                # by the users who may query both databases)
        if r["a_db"] != r["b_db"] and r["a_db"] in names and r["b_db"] in names:
            pairs.setdefault(tuple(sorted((r["a_db"], r["b_db"]))), []).append(r)
    for (a, b), links in pairs.items():
        lines = [f"# Links between {names[a]} and {names[b]}", "",
                 "Fields and labels that hold the same values in the two data sources (measured, or from the catalog)."]
        for r in links[:60]:
            lines.append(f"- `{r['a']}` ({names.get(r['a_db'], r['a_db'])}) holds the same values as `{r['b']}` "
                         f"({names.get(r['b_db'], r['b_db'])}){' (catalog)' if r['origin'] == 'curated' else ''}")
        pages.append({"section": "technical", "slug": f"links-{slugify(names[a])}-{slugify(names[b])}",
                      "title": f"Links: {names[a]} and {names[b]}", "content": "\n".join(lines),
                      "database_ids": [a, b], "sources": [{"ref": f"database:{a}", "title": names[a]},
                                                          {"ref": f"database:{b}", "title": names[b]}]})
    glossary = [e for e in ev["entries"] if e["classification"] == "glossary"]
    if glossary:
        lines = ["# Glossary", "", "Terms defined in the catalog (by the team, or quoted word for word from the documents "
                 "by the agent)."]
        for e in sorted(glossary, key=lambda x: x["title"].lower()):
            lines += ["", f"## {e['title']}", "", e["content"]]
        pages.append({"section": "functional", "slug": "glossary", "title": "Glossary", "content": "\n".join(lines),
                      "database_ids": [], "sources": [{"ref": f"entry:{e['id']}", "title": e["title"]} for e in glossary]})
    rules = [e for e in ev["entries"] if e["classification"] in ("rule", "note")]
    if rules or ev["memory"]:
        lines = ["# Rules and facts of the team", ""]
        for e in rules:
            lines += [f"## {e['title']}", "", e["content"], ""]
        if ev["memory"]:
            lines += ["## Team memory", ""] + [f"- ({m['kind']}) {m['text']}" for m in ev["memory"]]
        pages.append({"section": "functional", "slug": "rules-and-facts", "title": "Rules and facts",
                      "content": "\n".join(lines).strip(), "database_ids": [],
                      "sources": [{"ref": f"entry:{e['id']}", "title": e["title"]} for e in rules] +
                                 [{"ref": f"memory:{m['id']}", "title": m["text"][:60]} for m in ev["memory"]]})
    return pages


# --------------------------------------------------------------------------- #
# summary pages (LLM, only when their evidence changed)
# --------------------------------------------------------------------------- #
def _numbered(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evidence items as E1, E2...: (for the prompt, the page's sources)."""
    prompt, sources = [], []
    for i, it in enumerate(items, 1):
        prompt.append({"id": f"E{i}", **{k: v for k, v in it.items() if k != "ref"}})
        sources.append({"ref": it["ref"], "title": it.get("title") or it.get("name") or it["ref"]})
    return prompt, sources


def _apps(ev: dict[str, Any]) -> list[str]:
    """The main applications: the values of the application labels and fields, most mentioned first."""
    found: set[str] = set()
    for slot in (ev["inventory"].get("Applications") or {}).values():
        found.update(v for v in slot["values"] if v and 2 <= len(v) <= 60 and not v.isdigit())   # not "A", not "7"
    text = " ".join([d["content"][:20000] for d in ev["docs"]] + [a["question"] or "" for a in ev["answers"]] +
                    [e["content"] for e in ev["entries"]])

    def mentions(app: str) -> int:
        return len(re.findall(r"(?<![\w-])" + re.escape(app) + r"(?![\w-])", text, re.I))

    ranked = sorted(found, key=lambda a: (-mentions(a), a))
    return [a for a in ranked if mentions(a)][:MAX_APPS]


def _excerpts(text: str, name: str, limit: int = 20) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])",
                                                                        ln, re.I)]
    return [ln[:300] for ln in lines[:limit]]


def summary_specs(ev: dict[str, Any]) -> list[dict[str, Any]]:
    """The summary pages to write, each with its evidence (the hash of which decides whether to write it)."""
    all_dbs = sorted(ev["databases"])
    databases = [{"ref": f"database:{i}", "name": d["name"], "engine": d["backend"], "objects": d["counts"],
                  "domains": list(d["categories"])[:12]} for i, d in ev["databases"].items()]
    inventory = [{"ref": f"inventory:{dim}", "title": dim, "what": dim,
                  "examples": sorted({v for slot in per_db.values() for v in slot["values"]})[:25],
                  "count": len({v for slot in per_db.values() for v in slot["values"]})}
                 for dim, per_db in ev["inventory"].items()]
    docs = [{"ref": f"doc:{d['id']}", "title": d["title"], "text": d["content"][:DOC_CHARS]} for d in ev["docs"][:6]]
    entries = [{"ref": f"entry:{e['id']}", "title": e["title"], "classification": e["classification"],
                "text": e["content"][:600]} for e in ev["entries"] if e["classification"] != "formula"][:40]
    questions = [{"ref": f"recipe:{a['id']}", "title": (a["question"] or "")[:80], "question": a["question"]}
                 for a in ev["answers"]][:40]
    relations = [{"ref": "relations", "title": "relations between the data sources",
                  "links": [f"{r['a']} = {r['b']}" for r in ev["relations"][:40]]}] if ev["relations"] else []
    specs = [
        {"section": "functional", "slug": "overview", "title": "How the system works", "database_ids": all_dbs,
         "brief": "Explain what the system does for its users, its applications and their roles, the main processes "
                  "and chains between applications, and where the data of each one is (## What it does, ## Applications, "
                  "## Processes and chains, ## Where the data is).",
         "evidence": docs + entries + questions + databases + inventory},
        {"section": "technical", "slug": "architecture", "title": "Technical overview", "database_ids": all_dbs,
         "brief": "Describe the data sources, the infrastructure (servers, services, clusters, environments, regions), "
                  "how the data sources relate and what is not known (## Data sources, ## Infrastructure, ## Links, "
                  "## Not known yet).",
         "evidence": databases + inventory + relations + docs},
    ]
    for app in _apps(ev):
        ev_app = []
        for d in ev["docs"]:
            ex = _excerpts(d["content"], app)
            if ex:
                ev_app.append({"ref": f"doc:{d['id']}", "title": d["title"], "lines": ex})
        for e in ev["entries"]:
            if re.search(r"(?<![\w-])" + re.escape(app) + r"(?![\w-])", e["title"] + " " + e["content"], re.I):
                ev_app.append({"ref": f"entry:{e['id']}", "title": e["title"], "text": e["content"][:600]})
        for a in ev["answers"]:
            if re.search(r"(?<![\w-])" + re.escape(app) + r"(?![\w-])", a["question"] or "", re.I):
                ev_app.append({"ref": f"recipe:{a['id']}", "title": (a["question"] or "")[:80],
                               "question": a["question"]})
        holders = [{"ref": f"database:{i}", "title": ev["databases"][i]["name"], "holders": slot["holders"][:10]}
                   for i, slot in (ev["inventory"].get("Applications") or {}).items() if app in slot["values"]]
        specs.append({"section": "functional", "slug": f"application-{slugify(app)}", "title": f"Application {app}",
                      "database_ids": sorted({int(h["ref"].split(":")[1]) for h in holders}),
                      "brief": f"Explain what the application {app} is and does, what it depends on and what depends "
                               "on it (the chain), where its data is, and what people ask about it (## What it is, "
                               "## Chain, ## Its data, ## Questions people ask).",
                      "evidence": ev_app[:30] + holders})
    return specs


def write_summary(spec: dict[str, Any], llm: Any) -> dict[str, Any]:
    prompt_items, sources = _numbered(spec["evidence"])
    msg = llm.chat([{"role": "system", "content": PROMPT.format(title=spec["title"], brief=spec["brief"])},
                    {"role": "user", "content": json.dumps(prompt_items, ensure_ascii=False, default=str)}],
                   max_tokens=1800)
    usage = getattr(llm, "last_usage", None) or {}
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    return {"content": text, "sources": sources,
            "tokens": int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)}


# --------------------------------------------------------------------------- #
# saving
# --------------------------------------------------------------------------- #
def save_page(page: dict[str, Any], kind: str, input_hash: str, llm_calls: int = 0, tokens: int = 0) -> str:
    """written | unchanged | kept (a person's edit)."""
    p = (db.session.query(ContextPage).filter(ContextPage.section == page["section"], ContextPage.slug == page["slug"])
         .one_or_none())
    if p is not None and (p.author or "agent") != "agent":
        return "kept"
    if p is not None and p.input_hash == input_hash and p.content:
        return "unchanged"
    if p is None:
        p = ContextPage(section=page["section"], slug=page["slug"], version=0)
        db.session.add(p)
    p.title, p.kind, p.content = page["title"][:255], kind, page["content"]
    p.sources, p.database_ids = page.get("sources") or [], sorted(page.get("database_ids") or [])
    p.input_hash, p.author, p.version = input_hash, "agent", (p.version or 0) + 1
    p.llm_calls, p.tokens, p.updated_at = llm_calls, tokens, dt.datetime.utcnow()
    db.session.commit()
    return "written"


def drop_pages(keep: set[tuple[str, str]]) -> int:
    """Pages of the agent whose subject is gone (a database or an application no longer found)."""
    n = 0
    for p in db.session.query(ContextPage).filter(ContextPage.author == "agent"):
        if (p.section, p.slug) not in keep:
            db.session.delete(p)
            n += 1
    db.session.commit()
    return n


# --------------------------------------------------------------------------- #
# the build
# --------------------------------------------------------------------------- #
def build_context(reason: str = "manual", llm: bool = True, force: bool = False) -> dict[str, Any]:
    """One Context build (a run of kind "context"); returns its summary."""
    from supagent.knowledge.learner import Steps, _start_run, running_run
    from supagent.knowledge.stopping import LearningStopped, check, watching
    from supagent.llm import background, llm_task

    busy = running_run()
    run_id = _start_run(reason, kind="context") if busy is None else None
    if run_id is None:
        busy = busy or running_run()
        return {"run": busy.id if busy else None, "status": "skipped",
                "reason": f"run {busy.id} is still {busy.status}" if busy else "another run started at the same time"}
    stats: dict[str, Any] = {}
    steps = Steps(run_id, stats)
    status, error = "done", None
    t0 = time.time()
    try:
        with background(), watching(run_id), llm_task("context", run_id=run_id):
            steps.begin("collect what the team shares")
            ev = collect()
            steps.end(databases=len(ev["databases"]), documents=len(ev["docs"]), catalog_entries=len(ev["entries"]),
                      team_memory=len(ev["memory"]), helpful_answers=len(ev["answers"]),
                      inventory=len(ev["inventory"]), relations=len(ev["relations"]))
            keep: set[tuple[str, str]] = set()
            steps.begin("facts pages (no LLM)")
            done: dict[str, int] = defaultdict(int)
            for page in fact_pages(ev):
                check()
                keep.add((page["section"], page["slug"]))
                done[save_page(page, "facts", _hash([page["title"], page["content"], page["database_ids"]]))] += 1
            steps.end(**done)
            specs = summary_specs(ev)
            keep |= {(s["section"], s["slug"]) for s in specs}
            if llm:
                budget = int(settings.get("context.max_llm_calls"))
                steps.begin("summary pages (LLM)")
                res: dict[str, int] = defaultdict(int)
                client = None
                for spec in specs:
                    check()
                    h = _hash([PROMPT, spec["title"], spec["brief"], spec["evidence"]])
                    p = (db.session.query(ContextPage).filter(ContextPage.section == spec["section"],
                                                              ContextPage.slug == spec["slug"]).one_or_none())
                    if p is not None and (p.author or "agent") != "agent":
                        res["kept"] += 1
                        continue
                    if p is not None and p.input_hash == h and p.content and not force:
                        res["unchanged"] += 1
                        continue
                    if not spec["evidence"]:
                        continue
                    if res["llm_calls"] >= budget:
                        res["left"] += 1
                        continue
                    if client is None:
                        from supagent.llm import LLM

                        client = LLM()
                    db.session.commit()                       # no connection held during the LLM call
                    out = write_summary(spec, client)
                    res["llm_calls"] += 1
                    res["tokens"] += out["tokens"]
                    if not out["content"]:
                        res["empty"] += 1
                        continue
                    res[save_page({**spec, "content": out["content"], "sources": out["sources"]}, "summary", h,
                                  1, out["tokens"])] += 1
                    steps.update(**res)
                steps.end(**res)
                if res.get("left"):
                    status = "partial"
            steps.begin("pages of subjects gone")
            steps.end(dropped=drop_pages(keep))
            from supagent.knowledge.index import embed_pending, sync

            steps.begin("search index")
            written = sync(("context:",))
            if written["added"] + written["changed"]:
                try:                                  # found by meaning from the next question on
                    written["embedded"] = embed_pending().get("embedded", 0)
                except Exception as ex:  # pylint: disable=broad-except   (words meanwhile; hourly)
                    db.session.rollback()
                    log.warning("supagent context: vectors: %s", ex)
            steps.end(**written)
    except LearningStopped:
        db.session.rollback()
        status, error = "stopped", "stopped by an admin"
        steps.interrupted("stopped by an admin")
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent context: build %s failed", run_id)
        status, error = "error", f"{type(ex).__name__}: {str(ex)[:1500]}"
        steps.interrupted(error)
    stats["seconds"] = round(time.time() - t0, 1)
    run = db.session.get(Run, run_id)
    run.status, run.error, run.stats, run.finished_at = status, error, stats, dt.datetime.utcnow()
    db.session.commit()
    return {"run": run_id, "status": status, "error": error, **stats}


def context_due(now: dt.datetime | None = None) -> bool:
    """The nightly build is due: enabled, its hour has come, none done today, none running."""
    from supagent.knowledge.learner import running_run

    if not settings.get("context.enabled"):
        return False
    now = now or dt.datetime.now()
    if now.hour < int(settings.get("context.hour")):
        return False
    if running_run() is not None:
        return False
    start_of_day_utc = dt.datetime.utcnow() - dt.timedelta(hours=now.hour, minutes=now.minute)
    today = db.session.query(Run).filter(Run.kind == "context", Run.started_at >= start_of_day_utc).all()
    return not any(r.status in ("done", "partial") for r in today) and len(today) < 3


# --------------------------------------------------------------------------- #
# who sees what
# --------------------------------------------------------------------------- #
def visible_pages() -> list[ContextPage]:
    """The pages the current user may read: every database of a page must be one they may query."""
    from supagent.security import visible_databases

    dbs = visible_databases()
    return [p for p in db.session.query(ContextPage).order_by(ContextPage.section, ContextPage.title)
            if set(p.database_ids or []) <= dbs]
