"""`superset supagent ...`: installation, settings, learning, tests from the command line.

    superset supagent init                      tables, permissions, role "AI Agent"
    superset supagent settings                  show the settings (secrets: only whether set)
    superset supagent settings --set llm.auth=middleware --set llm.middleware.token_url=https://...
    superset supagent test-llm                  token (middleware), model, one short answer
    superset supagent learn [--database NAME]   learn now (what the daily run does)
    superset supagent import-catalog catalog.yaml
    superset supagent knowledge [--changes 7]   what is learned, per database
    superset supagent describe "failed jobs"    what the agent reads for these words
    superset supagent ask "question" --user alice
    superset supagent grant alice bob           give users the role "AI Agent"
    superset supagent mcp --port 5009           the tools as an MCP service for other agents
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click
from flask.cli import with_appcontext

ROLE = "AI Agent"


@click.group(help="supagent: the AI agent inside Superset")
def supagent() -> None:
    pass


def _role_permissions() -> list[tuple[str, str]]:
    from supagent import MENU_ITEMS, SETTINGS_CATEGORY
    from supagent.views import ChatView, KnowledgeView

    return [("can_read", ChatView.class_permission_name), ("can_write", ChatView.class_permission_name),
            ("can_read", KnowledgeView.class_permission_name), ("menu_access", MENU_ITEMS["chat"]),
            ("menu_access", SETTINGS_CATEGORY), ("menu_access", MENU_ITEMS["dictionary"])]


@supagent.command(help="Create or upgrade the tables, the permissions and the role 'AI Agent'")
@with_appcontext
def init() -> None:
    from superset.extensions import appbuilder, db, security_manager

    from supagent.models import create_or_upgrade

    before, after = create_or_upgrade()
    click.echo(f"tables: schema version {before} -> {after}")
    from supagent.knowledge.memory import merge_duplicates

    merged = merge_duplicates()
    if merged:
        click.echo(f"memory: {merged} copies of the same memory removed (one of each kept)")
    from supagent.models import Recipe

    auto = db.session.query(Recipe).filter(Recipe.status == "auto").count()
    if auto:
        click.echo(f"learned answers: {auto} were saved automatically by an older version; they are no longer "
                   "listed nor used (learned answers now come from Helpful). To delete them: "
                   "superset supagent remove-auto-learned")
    from supagent.knowledge.catalog import migrate_document

    moved = migrate_document()
    if moved:
        click.echo(f"catalog: the single document split into entries ({moved}); the document is kept as a backup")
    appbuilder.add_permissions(update_perms=True)        # the views' permissions (FAB)
    security_manager.sync_role_definitions()             # Admin gets them; Gamma does not (admin-only)
    role = security_manager.find_role(ROLE) or security_manager.add_role(ROLE)
    added = []
    for perm, view in _role_permissions():
        pvm = security_manager.find_permission_view_menu(perm, view)
        if pvm is None:
            pvm = security_manager.add_permission_view_menu(perm, view)
        if pvm not in role.permissions:
            security_manager.add_permission_role(role, pvm)
            added.append(f"{perm} on {view}")
    db.session.commit()
    click.echo(f"role {ROLE!r}: " + (", ".join(added) if added else "up to date"))
    _init_store()
    click.echo("Next: give users the role (superset supagent grant <user>, or Superset's user list), set the LLM "
               "(Settings page or superset supagent settings --set ...), then superset supagent learn.")


def _init_store() -> None:
    """The knowledge store built at the first init that finds its extensions (a rebuild later: store rebuild)."""
    from supagent import settings as S
    from supagent.knowledge import pgstore

    if (S.get("search.store") or "auto") == "off":
        return
    try:
        e = pgstore.engine()
        if e is None:
            click.echo("knowledge store: not used (Superset's database is not PostgreSQL; search.store_uri to use one)")
            return
        st = pgstore.status()
        if st.get("error"):
            click.echo(f"knowledge store: not built: {st['error'][:300]}")
            return
        caps = st.get("extensions") or {}
        if not (caps.get("vector") or caps.get("pg_textsearch") or caps.get("pg_trgm")):
            click.echo("knowledge store: not used (no pgvector, pg_textsearch or pg_trgm in that database)")
            return
        if (st.get("state") or {}).get("version"):
            click.echo(f"knowledge store: version {st['state']['version']} ({st.get('rows')})")
        else:
            out = pgstore.rebuild()
            if out.get("skipped"):
                click.echo(f"knowledge store: not built now ({out['skipped']})")
            else:
                click.echo(f"knowledge store: built ({out['docs']} pieces, {out['names']} names, {out['bm25']}, "
                           f"vectors {out['dims'] or 'none'}) in {out['seconds']} s")
        for w in pgstore.status().get("warnings") or []:
            click.echo(f"knowledge store: {w}")
    except Exception as ex:  # pylint: disable=broad-except   (the search of 0.5 still answers)
        click.echo(f"knowledge store: not built: {type(ex).__name__}: {str(ex)[:300]}")


def _print_settings() -> None:
    from supagent import settings

    width = max(len(s.key) for s in settings.SPECS)
    for row in settings.describe():
        value = ("(set)" if row["value"] else "(not set)") if row["secret"] else json.dumps(row["value"])
        click.echo(f"{row['key']:<{width}}  {value}")


@supagent.command("remove-auto-learned",
                  help="Delete the learned answers that versions 0.2.1 and before saved by themselves")
@with_appcontext
def remove_auto_learned() -> None:
    from superset.extensions import db

    from supagent.knowledge.index import sync
    from supagent.models import Recipe

    n = db.session.query(Recipe).filter(Recipe.status == "auto").delete(synchronize_session=False)
    db.session.commit()
    sync(("recipe:",))
    click.echo(f"{n} learned answers saved automatically deleted (the ones marked Helpful stay)")


@supagent.command("forget-learned",
                  help="Forget what the learning learned (dictionary, AI descriptions, measured relations, changes) "
                       "to learn it again from scratch; without --yes, only show what would go")
@click.option("--database", "databases", multiple=True, help="Only this database (name or id; repeat); default: all")
@click.option("--everything", is_flag=True, help="Also what people did in the Data dictionary page "
                                                  "(written or approved descriptions, synonyms, relations marked Wrong)")
@click.option("--yes", is_flag=True, help="Do it (otherwise only show what would be forgotten)")
@with_appcontext
def forget_learned(databases: tuple[str, ...], everything: bool, yes: bool) -> None:
    from supagent.knowledge.forget import forget

    rows = forget(list(databases) or None, everything=everything, apply=yes)
    if not rows:
        click.echo("nothing learned yet" + (f" for {', '.join(databases)}" if databases else ""))
        return
    for r in rows:
        plural = {"index": "indices", "family": "families"}
        objs = ", ".join(f"{n} {plural.get(k, k + 's')}" for k, n in sorted(r["objects"].items())) or "no object"
        click.echo(f"{r['database']}: {objs}, {r['relations']} relations, {r['changes']} changes"
                   + (f"; kept (people's work, learned facts cleared): {r['kept']} objects" if r["kept"] else ""))
    if yes:
        click.echo("Forgotten. The next run learns these databases again from scratch: superset supagent learn "
                   "(large databases: --minutes 240 once), or the daily run. The catalog entries, learned answers, "
                   "memory, documents and chats are kept.")
    else:
        click.echo("Nothing was changed. Add --yes to forget it (--everything: also people's work in the dictionary).")


@supagent.command("settings", help="Show or change settings: --set key=value (repeat), --unset key")
@click.option("--set", "pairs", multiple=True, metavar="KEY=VALUE")
@click.option("--unset", "unset", multiple=True, metavar="KEY")
@with_appcontext
def settings_cmd(pairs: tuple[str, ...], unset: tuple[str, ...]) -> None:
    from supagent import settings

    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"{pair!r}: KEY=VALUE")
        key, value = pair.split("=", 1)
        try:
            settings.set_value(key.strip(), value, by="cli")
        except (KeyError, ValueError) as ex:
            raise click.ClickException(str(ex)) from ex
    for key in unset:
        settings.set_value(key.strip(), None, by="cli")
    _print_settings()


@supagent.command("test-llm", help="Get a token (middleware), find the model, ask for one word")
@click.option("--profile", is_flag=True, help="Also: thinking on and off, tool calls, prompt cache (a minute)")
@with_appcontext
def test_llm(profile: bool) -> None:
    from supagent.llm import LLM

    from supagent.llm import llm_task

    try:
        llm = LLM()
        with llm_task("test"):
            out = llm.check()
            if profile:
                out["profile"] = llm.profile()
    except Exception as ex:  # pylint: disable=broad-except
        raise click.ClickException(f"{type(ex).__name__}: {ex}") from ex
    for k, v in out.items():
        click.echo(f"{k}: {json.dumps(v) if isinstance(v, dict) else v}")


@supagent.command(help="Learn now: metrics (promagg) and indices (osagg), relations, catalog, LLM descriptions")
@click.option("--database", "databases", multiple=True, help="Database name or id (repeat); default: all")
@click.option("--no-llm", is_flag=True, help="No LLM descriptions this time")
@click.option("--minutes", type=int, default=None, help="Time limit (default: learn.max_minutes)")
@click.option("--plan", is_flag=True, help="Only estimate today's work (objects due, requests, minutes); learn nothing")
@click.option("--stop", is_flag=True, help="Stop the running learning run (it keeps what it learned)")
@with_appcontext
def learn(databases: tuple[str, ...], no_llm: bool, minutes: int | None, plan: bool, stop: bool) -> None:
    from supagent.knowledge.learner import learning_username, plan_learning, run_learning

    if stop:
        from supagent.knowledge.stopping import request_stop

        run_id = request_stop()
        click.echo(f"learning run {run_id} stopped: it keeps what it learned (its request in progress ends in "
                   "the background); a new run can start" if run_id else "no learning run is running")
        return
    if plan:
        from supagent.security import acting_as

        with acting_as(learning_username()):
            rows = plan_learning(list(databases) or None)
        for r in rows:
            if r.get("skipped"):
                click.echo(f"{r['database']}: not learned: {r['skipped']}")
                continue
            if r.get("error"):
                click.echo(f"{r['database']}: {r['error']}")
                continue
            what = "metrics" if r["backend"] == "promagg" else "indices / families"
            over = "  (more than the time limit: the next runs continue)" if r["minutes"] > r["limit_minutes"] else ""
            later = (f"; then the depth of the history, about {r['history_requests']} lookups with the time left"
                     if r.get("history_requests") else "")
            click.echo(f"{r['database']}: {r['objects']} {what}, {r['new']} new, {r['due']} to profile today, "
                       f"about {r['requests']} requests = {r['minutes']} min at the rate limit{over}{later}")
        return
    out = run_learning(reason="cli", databases=list(databases) or None, llm=not no_llm, max_minutes=minutes)
    click.echo(json.dumps(out, indent=2, default=str))
    if out.get("status") == "error":
        sys.exit(1)


@supagent.command("context", help="The Context (the system's functional and technical documentation): list its pages, "
                                   "or build it now")
@click.option("--build", is_flag=True, help="Build it now: the facts pages, and the summary pages whose sources changed")
@click.option("--no-llm", is_flag=True, help="Only the facts pages (no LLM call)")
@click.option("--force", is_flag=True, help="Write the summary pages again even when their sources did not change")
@with_appcontext
def context(build: bool, no_llm: bool, force: bool) -> None:
    from superset.extensions import db

    from supagent.models import ContextPage

    if build:
        from supagent.knowledge.context import build_context

        out = build_context(reason="cli", llm=not no_llm, force=force)
        click.echo(json.dumps(out, indent=2, default=str))
        if out.get("status") == "error":
            sys.exit(1)
        return
    pages = db.session.query(ContextPage).order_by(ContextPage.section, ContextPage.title).all()
    for p in pages:
        who = "AI-written" if p.kind == "summary" and (p.author or "agent") == "agent" else \
            ("facts" if (p.author or "agent") == "agent" else f"edited by {p.author}")
        click.echo(f"{p.section:<10} {p.title} ({who}, version {p.version}, {p.updated_at:%Y-%m-%d %H:%M})")
    if not pages:
        click.echo("no Context page yet: superset supagent context --build (or wait for the nightly build)")


@supagent.command("prompt", help="What the agent gives the LLM for a question, without asking the LLM: the "
                                  "instructions and the team's rules, the memory, where the data is, the knowledge "
                                  "found (dictionary, catalog, documents, Context) and the learned answers")
@click.argument("question")
@click.option("--user", "username", required=True, help="As this Superset user (what they may see)")
@click.option("--full", is_flag=True, help="Also print the whole instructions (else their size and the team's rules)")
@with_appcontext
def prompt(question: str, username: str, full: bool) -> None:
    from supagent.agent import Agent
    from supagent.security import acting_as

    import types

    with acting_as(username):
        agent = Agent(username, rich_results=True, llm=types.SimpleNamespace())   # the LLM is not asked
        try:
            messages = agent.prompt(question)
        finally:
            agent.close()
    system, asked = messages[0]["content"], messages[-1]["content"]
    if full:
        click.echo("=== instructions\n" + system)
    else:
        rules = system[system.find("Rules of the team"):] if "Rules of the team" in system else "(no team rule)"
        click.echo(f"=== instructions: {len(system)} characters; the team's rules:\n{rules}")
    click.echo("\n=== with the question\n" + asked)


@supagent.command("import-catalog", help="Split a catalog (YAML) into entries in Superset's database, and apply it")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--replace", is_flag=True, help="Also delete the structured entries the file does not have")
@with_appcontext
def import_catalog_cmd(path: str, replace: bool) -> None:
    from supagent.knowledge.catalog import import_catalog
    from supagent.knowledge.curated import after_change, catalog_texts

    before = catalog_texts()
    with open(path, encoding="utf-8") as fh:
        counts = import_catalog(fh.read(), by="cli", mode="replace" if replace else "merge")
    click.echo(f"imported: {counts}; applied: {after_change(before)}")


@supagent.command("export-catalog", help="Print the catalog: every enabled entry merged into one YAML")
@with_appcontext
def export_catalog() -> None:
    from supagent.knowledge.catalog import conflicts, export_catalog as export

    click.echo(export())
    found = conflicts()
    for c in found["conflicts"]:
        click.echo(f"# conflict: {c['kind']} {c['name']} in {c['entries']}, kept {c['kept']}", err=True)
    for e in found["errors"]:
        click.echo(f"# skipped entry {e['entry']!r}: {e['error']}", err=True)


@supagent.command(help="What is learned, per database (and the changes of the last days)")
@click.option("--changes", "days", type=int, default=0, help="Also list the changes of the last N days")
@click.option("--user", default=None, help="As this user (default: the learning user)")
@with_appcontext
def knowledge(days: int, user: str | None) -> None:
    from sqlalchemy import func
    from superset import db

    from supagent.knowledge.describe import changes
    from supagent.knowledge.learner import learning_username
    from supagent.models import KObject, Relation, Run, Source
    from supagent.security import acting_as

    with acting_as(user or learning_username()):
        for src in db.session.query(Source).order_by(Source.id):
            counts = dict(db.session.query(KObject.kind, func.count(KObject.id))
                          .filter(KObject.source_id == src.id, KObject.gone_at.is_(None)).group_by(KObject.kind).all())
            llm = (db.session.query(func.count(KObject.id)).filter(
                KObject.source_id == src.id, KObject.description_source == "llm").scalar())
            click.echo(f"{src.database_name} ({src.backend}), learned {src.last_learned_at}: {counts}, "
                       f"AI-written descriptions {llm}")
        rels = dict(db.session.query(Relation.relation, func.count(Relation.id)).group_by(Relation.relation).all())
        click.echo(f"relations: {rels}")
        for r in db.session.query(Run).order_by(Run.id.desc()).limit(5):
            click.echo(f"run {r.id} {r.reason} {r.started_at:%Y-%m-%d %H:%M} {r.status} "
                       f"{(r.stats or {}).get('seconds', '')} s")
        if days:
            for c in changes(days, limit=500):
                click.echo(f"{c['at']} {c['change']:<11} {c['kind']:<6} {c['parent'] + '.' if c['parent'] else ''}"
                           f"{c['name']} {json.dumps(c['detail']) if c['detail'] else ''}")


@supagent.command(help="Print what describe_data gives the agent for these words")
@click.argument("topic", required=False)
@click.option("--name", default=None, help="One index or metric")
@click.option("--user", default=None, help="As this user (default: the learning user)")
@with_appcontext
def describe(topic: str | None, name: str | None, user: str | None) -> None:
    from supagent.knowledge.learner import learning_username
    from supagent.security import acting_as
    from supagent.tools import describe_data

    with acting_as(user or learning_username()):
        click.echo(describe_data(topic=topic, index=name))


@supagent.command(help="Ask the agent a question in this process, as a user (no chat page needed)")
@click.argument("question")
@click.option("--user", required=True, help="Superset user the agent acts as")
@click.option("--steps/--no-steps", default=True, help="Print the tool calls")
@click.option("--pipeline", type=click.Choice(["classic", "governed"]), default=None,
              help="This question with this pipeline (default: agent.pipeline)")
@with_appcontext
def ask(question: str, user: str, steps: bool, pipeline: str | None) -> None:
    from supagent.agent import Agent
    from supagent.governed.pipeline import GovernedAgent, make_agent
    from supagent.security import acting_as

    with acting_as(user):
        agent = {"classic": Agent, "governed": GovernedAgent}[pipeline](user) if pipeline else make_agent(user)
        try:
            if agent.superset.error:
                click.echo(f"(note: {agent.superset.error})", err=True)
            answer, trace = agent.ask(question)
        finally:
            agent.close()
    if steps:
        for t in trace:
            click.echo(f"  -> {t.get('called') or t['tool']} {json.dumps(t['args'], ensure_ascii=False)[:300]} "
                       f"[{t.get('status')}, {t.get('seconds')} s]", err=True)
    click.echo(answer)


@supagent.command(help="Update the searchable knowledge: pieces that changed, then their vectors")
@click.option("--refresh-docs", is_flag=True, help="Also fetch the sites that are due")
@with_appcontext
def index(refresh_docs: bool) -> None:
    from supagent.knowledge.docs import refresh_due
    from supagent.knowledge.index import index_knowledge

    if refresh_docs:
        for r in refresh_due():
            click.echo(f"document {r['id']}: {r['status']}, {r['pages']} page(s) {r.get('error') or ''}")
    click.echo(json.dumps(index_knowledge(), indent=2, default=str))


@supagent.command(help="Classify the knowledge that changed (categories and relations, with the LLM), now")
@click.option("--minutes", default=15, show_default=True, type=int)
@click.option("--limit", default=400, show_default=True, type=int)
@with_appcontext
def classify(minutes: int, limit: int) -> None:
    from supagent.knowledge.facets import classify as run, review_counts
    from supagent.llm import LLM, llm_task

    with llm_task("classify"):
        out = run(LLM(), seconds=minutes * 60.0, limit=limit)
    out["waiting_for_review"] = review_counts()
    click.echo(json.dumps(out, indent=2, default=str))


@supagent.command(help="The team's Superset charts: look at their last full day against the 4 weeks before "
                  "(--scan), write what they show (--understand, LLM), then print what was found")
@click.option("--scan", "do_scan", is_flag=True, help="Look at the charts now (as the learning user)")
@click.option("--understand", "do_understand", is_flag=True, help="Write what the charts show (LLM; only changed ones)")
@click.option("--chart", "chart_ids", type=int, multiple=True, help="Only this chart (repeat)")
@click.option("--minutes", type=int, default=None, help="At most (default charts.minutes)")
@with_appcontext
def charts(do_scan: bool, do_understand: bool, chart_ids: tuple[int, ...], minutes: int | None) -> None:
    from superset.extensions import db

    from supagent.knowledge import charts as K
    from supagent.models import ChartScan

    if do_scan:
        click.echo(json.dumps(K.scan(seconds=minutes * 60 if minutes else None, chart_ids=list(chart_ids) or None),
                              default=str))
    if do_understand:
        from supagent.llm import LLM, background, llm_task

        with background(), llm_task("charts"):
            click.echo(json.dumps(K.understand(LLM(), force=bool(chart_ids)), default=str))
    q = db.session.query(ChartScan).order_by(ChartScan.chart_id)
    if chart_ids:
        q = q.filter(ChartScan.chart_id.in_(list(chart_ids)))
    for r in q:
        click.echo(f"chart {r.chart_id}: {r.status} {r.day or ''} {r.checked or 0} series"
                   + (f" ({r.reason})" if r.reason else ""))
        for f in r.findings or []:
            click.echo(f"    {K.describe_finding(f)}")
        if r.understanding:
            click.echo(f"    shows: {' '.join(r.understanding.split())[:300]}")


@supagent.group(help="The knowledge store in PostgreSQL (BM25, near spellings, vectors, chats)")
def store() -> None:
    pass


@store.command("status", help="Where it is, its extensions and versions, its size, the warnings to read")
@with_appcontext
def store_status() -> None:
    from supagent.knowledge import pgstore

    click.echo(json.dumps(pgstore.status(), indent=2, default=str))


@store.command("rebuild", help="Build it again from Superset's database (beside the one in use, then switched)")
@with_appcontext
def store_rebuild() -> None:
    from supagent.knowledge import pgstore

    click.echo(json.dumps(pgstore.rebuild(), indent=2, default=str))


@store.command("sync", help="Bring it in step now (it is, hourly and after each change)")
@with_appcontext
def store_sync() -> None:
    from supagent.knowledge import pgstore

    click.echo(json.dumps(pgstore.maintain(), indent=2, default=str))


@store.command("wipe", help="Drop it (the search of 0.5 answers until the next rebuild)")
@click.option("--yes", is_flag=True, help="Do not ask")
@with_appcontext
def store_wipe(yes: bool) -> None:
    from supagent.knowledge import pgstore

    if not yes and not click.confirm(f"Drop the schema {pgstore.schema()} and everything in it?"):
        return
    click.echo("dropped" if pgstore.wipe() else "no store")


@store.command("search", help="Search it as a user would (each piece with how it was found)")
@click.argument("query")
@click.option("--user", default=None, help="As this user (default: the learning user)")
@click.option("--limit", default=8, show_default=True, type=int)
@click.option("--chats", is_flag=True, help="The user's own chats instead")
@with_appcontext
def store_search(query: str, user: str | None, limit: int, chats: bool) -> None:
    from flask import g

    from supagent.knowledge import pgstore
    from supagent.knowledge.learner import learning_username
    from supagent.security import acting_as

    with acting_as(user or learning_username()):
        if chats:
            for c in pgstore.chats(query, g.user.id, k=limit):
                click.echo(f"{c['at']}  {c['question'][:120]}\n        tables {c['tables']}  {c['answer'][:160]!r}")
            return
        for f in pgstore.search(query, limit):
            click.echo(f"{f['score']:.4f}  [{f['kind']}] {f['title'][:100]}  via {f['via']} {f['ranks']}"
                       + (f"  spelled {[(x['word'], x['name']) for x in f['spelled'][:3]]}" if f["spelled"] else ""))


@supagent.command("check-knowledge", help="Is everything the team put in the knowledge given to the agent? "
                  "(searchable pieces in step, vectors, catalog errors and unknown names, rules, what waits "
                  "for an admin); exit code 1 when there is a problem")
@click.option("--json", "as_json", is_flag=True, help="The whole report as JSON")
@with_appcontext
def check_knowledge(as_json: bool) -> None:
    from supagent.knowledge.audit import audit

    out = audit()
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
    else:
        p = out["pieces"]
        click.echo(f"searchable pieces: {p['total']} ({', '.join(f'{k} {v}' for k, v in sorted(p['by_kind'].items()))})"
                   f"; out of step: {p['out_of_step']}")
        if out["vectors"]["model"]:
            click.echo(f"without a vector of {out['vectors']['model']}: {out['vectors']['missing']}")
        d, r = out["dictionary"], out["rules"]
        click.echo(f"dictionary: {d['objects']} objects, {d['curated']} described by people, {d['to_verify']} AI "
                   f"descriptions to verify; rules: {r['enabled']} ({r['in_instructions']} in the instructions); "
                   f"Context pages: {out['context_pages']}")
        click.echo("problems:" if out["problems"] else "no problem: everything the team put in is given to the agent")
        for line in out["problems"]:
            click.echo(f"- {line}")
    if out["problems"]:
        raise SystemExit(1)


@supagent.command("agent-catalog", help="Let the agent write the catalog entries it is certain of now (formulas "
                  "of confirmed answers, approved team rules and facts, definitions quoted from documents)")
@click.option("--no-docs", is_flag=True, help="Not the documents (no LLM call)")
@with_appcontext
def agent_catalog(no_docs: bool) -> None:
    from supagent.knowledge.autocatalog import run

    click.echo(json.dumps(run(llm_docs=not no_docs), indent=2, default=str))


@supagent.command("tidy-learned", help="Rewrite the questions of the learned answers and the chat names that "
                  "are not generic yet (the LLM; the daily learning does it too), and merge the duplicates")
@click.option("--limit", default=200, show_default=True, type=int)
@with_appcontext
def tidy_learned_cmd(limit: int) -> None:
    from supagent.knowledge.generic import tidy_learned

    click.echo(json.dumps(tidy_learned(limit=limit), indent=2))


@supagent.command(help="Where the time of the answers goes: LLM and tools, prompt sizes, prompt cache, slowest")
@click.option("--days", default=7, show_default=True, type=int)
@with_appcontext
def stats(days: int) -> None:
    from supagent.knowledge.quality import usage_stats

    click.echo(json.dumps(usage_stats(days), indent=2, ensure_ascii=False))


@supagent.command(help="Does 'Where the data is' find the data of the Helpful answers? (hit@1, hit@3, MRR)")
@click.option("--limit", default=200, show_default=True, type=int)
@click.option("--user", default=None, help="As this user (default: the learning user)")
@with_appcontext
def evaluate(limit: int, user: str | None) -> None:
    from supagent.knowledge.learner import learning_username
    from supagent.knowledge.quality import evaluate_resolver
    from supagent.security import acting_as

    with acting_as(user or learning_username()):
        click.echo(json.dumps(evaluate_resolver(limit=limit, seconds=600), indent=2, ensure_ascii=False))


@supagent.command(help="Questions not answered well (Not helpful, failed, no data found for their words) and "
                       "learned answers about data that no longer exists: what to add to the dictionary")
@click.option("--days", default=30, show_default=True, type=int)
@click.option("--user", default=None, help="As this user (default: the learning user)")
@with_appcontext
def gaps(days: int, user: str | None) -> None:
    from supagent.knowledge.learner import learning_username
    from supagent.knowledge.quality import gaps as find_gaps
    from supagent.security import acting_as

    with acting_as(user or learning_username()):
        for part, lines in find_gaps(days).items():
            click.echo(f"{part.replace('_', ' ')} ({len(lines)}):")
            for line in lines:
                click.echo(f"  {line}")


@supagent.command(help="Search the knowledge as a user would (what the agent is given)")
@click.argument("query")
@click.option("--user", default=None, help="As this user (default: the learning user)")
@click.option("--limit", default=8, show_default=True, type=int)
@with_appcontext
def search(query: str, user: str | None, limit: int) -> None:
    from supagent.knowledge.learner import learning_username
    from supagent.knowledge.search import search as do_search
    from supagent.security import acting_as

    with acting_as(user or learning_username()):
        for f in do_search(query, k=limit):
            click.echo(f"{f['score']:.4f}  [{f['kind']}] {f['title']}")
            click.echo("        " + " ".join((f["text"] or "").split())[:200])


@supagent.command(help=f"Give users the role {ROLE!r} (chat and data dictionary)")
@click.argument("usernames", nargs=-1, required=True)
@with_appcontext
def grant(usernames: tuple[str, ...]) -> None:
    from superset.extensions import db, security_manager

    role = security_manager.find_role(ROLE)
    if role is None:
        raise click.ClickException(f"no role {ROLE!r}: run superset supagent init first")
    for name in usernames:
        user = security_manager.find_user(username=name)
        if user is None:
            click.echo(f"{name}: no such user")
            continue
        if role not in user.roles:
            user.roles.append(role)
        click.echo(f"{name}: {', '.join(r.name for r in user.roles)}")
    db.session.commit()


@supagent.command("push-descriptions", help="Catalog descriptions -> descriptions of the dataset columns")
@click.option("--labels", is_flag=True, help="Also the verbose names (they relabel existing charts)")
@with_appcontext
def push_descriptions_cmd(labels: bool) -> None:
    from supagent.tools import push_descriptions

    push_descriptions(labels=labels)


@supagent.command("push-metrics", help="Catalog metrics -> Superset datasets with their saved metrics")
@with_appcontext
def push_metrics_cmd() -> None:
    from supagent.tools import push_metrics

    push_metrics()


@supagent.command(help="Serve the tools over MCP (streamable HTTP) for other agents, as the service user")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=5009, show_default=True, type=int)
@with_appcontext
def mcp(host: str, port: int) -> None:
    from flask import current_app

    from supagent import security, tools, tools_superset  # noqa: F401
    from supagent.security import service_username

    try:
        from fastmcp import FastMCP
    except ImportError as ex:
        raise click.ClickException("fastmcp is not installed (pip install fastmcp)") from ex
    security._APP = current_app._get_current_object()
    name = service_username(security._APP)
    if not name:
        raise click.ClickException("set mcp.user (superset supagent settings --set mcp.user=<user>)")
    server: Any = FastMCP("supagent tools")
    for tool in tools.mcp.tools.values():
        server.tool(tool.fn, name=tool.name, description=tool.description)
    click.echo(f"MCP tools on http://{host}:{port}/mcp as Superset user {name!r}: {', '.join(tools.mcp.tools)}")
    server.run(transport="http", host=host, port=port)
