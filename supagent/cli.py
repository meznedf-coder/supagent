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
    click.echo("Next: give users the role (superset supagent grant <user>, or Superset's user list), set the LLM "
               "(Settings page or superset supagent settings --set ...), then superset supagent learn.")


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
@with_appcontext
def test_llm() -> None:
    from supagent.llm import LLM

    try:
        out = LLM().check()
    except Exception as ex:  # pylint: disable=broad-except
        raise click.ClickException(f"{type(ex).__name__}: {ex}") from ex
    for k, v in out.items():
        click.echo(f"{k}: {v}")


@supagent.command(help="Learn now: metrics (promagg) and indices (osagg), relations, catalog, LLM descriptions")
@click.option("--database", "databases", multiple=True, help="Database name or id (repeat); default: all")
@click.option("--no-llm", is_flag=True, help="No LLM descriptions this time")
@click.option("--minutes", type=int, default=None, help="Time limit (default: learn.max_minutes)")
@click.option("--plan", is_flag=True, help="Only estimate today's work (objects due, requests, minutes); learn nothing")
@with_appcontext
def learn(databases: tuple[str, ...], no_llm: bool, minutes: int | None, plan: bool) -> None:
    from supagent.knowledge.learner import learning_username, plan_learning, run_learning

    if plan:
        from supagent.security import acting_as

        with acting_as(learning_username()):
            rows = plan_learning(list(databases) or None)
        for r in rows:
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


@supagent.command("import-catalog", help="Split a catalog (YAML) into entries in Superset's database, and apply it")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--replace", is_flag=True, help="Also delete the structured entries the file does not have")
@with_appcontext
def import_catalog_cmd(path: str, replace: bool) -> None:
    from supagent.knowledge.catalog import import_catalog
    from supagent.knowledge.curated import apply_catalog

    with open(path, encoding="utf-8") as fh:
        counts = import_catalog(fh.read(), by="cli", mode="replace" if replace else "merge")
    click.echo(f"imported: {counts}; applied: {apply_catalog()}")


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
@with_appcontext
def ask(question: str, user: str, steps: bool) -> None:
    from supagent.agent import Agent
    from supagent.security import acting_as

    with acting_as(user):
        agent = Agent(user)
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
