"""Other MCP servers as sources of the agent (setting mcp.servers), through LangChain's MCP adapters
(pip install "supagent[graph]"). Their tools are offered to the agent next to Superset's, named
<server>_<tool> so that two servers never clash, and indexed as knowledge ("tool" pieces) so that the
decider can route a question to them. Each call uses the server's own permissions (its headers; "{username}"
in a header is replaced by the Superset user's name). A tool that changes something (listed in the server's
"write") runs only when an admin allowed it (the server's "allow_write": true); "roles" limits a server to
the users with one of those Superset roles.

    superset supagent settings --set 'mcp.servers=[{"name": "tickets", "transport": "streamable_http",
        "url": "https://tickets.example/mcp", "headers": {"Authorization": "Bearer ..."},
        "description": "Support tickets of the teams", "write": ["close_ticket"]}]'
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Iterator

log = logging.getLogger(__name__)

TTL = 300.0
CALL_TIMEOUT = 120.0
TRANSPORTS = ("streamable_http", "sse", "stdio", "websocket")
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}


def servers() -> list[dict[str, Any]]:
    """The configured servers (bad entries left out, logged)."""
    from supagent import settings

    raw = settings.get("mcp.servers") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            log.warning("supagent mcp: mcp.servers is not JSON")
            return []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", name) or s.get("transport") not in TRANSPORTS:
            log.warning("supagent mcp: server %r left out (a name of a-z0-9_ and a transport of %s)", name, TRANSPORTS)
            continue
        out.append(s)
    return out


def available() -> bool:
    try:
        import langchain_mcp_adapters.client  # noqa: F401
    except Exception:  # pylint: disable=broad-except
        return False
    return True


def _allowed(server: dict[str, Any], username: str) -> bool:
    roles = set(server.get("roles") or [])
    if not roles:
        return True
    try:
        from superset.extensions import security_manager

        user = security_manager.find_user(username=username)
        return user is not None and bool(roles & {r.name for r in user.roles})
    except Exception:  # pylint: disable=broad-except
        return False


def _connection(server: dict[str, Any], username: str) -> dict[str, Any]:
    keys = ("url", "headers", "timeout", "command", "args", "env", "cwd")
    conn = {k: server[k] for k in keys if k in server}
    conn["transport"] = server["transport"]
    if isinstance(conn.get("headers"), dict):
        conn["headers"] = {k: str(v).replace("{username}", username) for k, v in conn["headers"].items()}
    return conn


def _run(coro: Any, timeout: float = CALL_TIMEOUT) -> Any:
    """An async call from a web or worker thread (a loop of its own, in a thread of its own)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout)


def tools(username: str) -> list[Any]:
    """The tools of every server this user may use (LangChain tools, names <server>_<tool>), cached TTL s."""
    mine = [s for s in servers() if _allowed(s, username)]
    if not mine or not available():
        return []
    key = hashlib.sha256(json.dumps([_connection(s, username) for s in mine], sort_keys=True, default=str)
                         .encode()).hexdigest()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    from langchain_mcp_adapters.client import MultiServerMCPClient

    out: list[Any] = []
    for s in mine:                                   # one server down never hides the others
        try:
            client = MultiServerMCPClient({s["name"]: _connection(s, username)}, tool_name_prefix=True)
            found = _run(client.get_tools(), timeout=30)
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent mcp: tools of %s: %s", s["name"], ex)
            continue
        wanted = set(s.get("tools") or [])
        for t in found:
            base = t.name[len(s["name"]) + 1:] if t.name.startswith(s["name"] + "_") else t.name
            if wanted and base not in wanted:
                continue
            t.metadata = {**(t.metadata or {}), "server": s["name"], "tool": base,
                          "write": base in set(s.get("write") or []), "allow_write": bool(s.get("allow_write"))}
            out.append(t)
    with _LOCK:
        _CACHE[key] = (time.time(), out)
    return out


def _schema(t: Any) -> dict[str, Any]:
    schema = t.args_schema
    if isinstance(schema, dict):
        return schema
    try:
        return schema.model_json_schema()
    except Exception:  # pylint: disable=broad-except
        return {"type": "object", "properties": {}}


def specs(username: str) -> list[dict[str, Any]]:
    """OpenAI-style specs of the tools (what the LLM is offered)."""
    out = []
    for t in tools(username):
        md = t.metadata or {}
        desc = (t.description or "")[:1200]
        if md.get("write"):
            desc = ("(changes something in " + md.get("server", "") + ": run it only when the user asked for "
                    "exactly this) " + desc)
        out.append({"type": "function", "function": {"name": t.name, "description": desc, "parameters": _schema(t)}})
    return out


def call(username: str, name: str, args: dict[str, Any]) -> str:
    """A tool's result as text; a write tool no admin allowed is refused."""
    t = next((x for x in tools(username) if x.name == name), None)
    if t is None:
        return f"tool error: unknown MCP tool {name}"
    md = t.metadata or {}
    if md.get("write") and not md.get("allow_write"):
        return (f"tool error: {md.get('tool')} changes something in {md.get('server')}, and an admin did not allow "
                "the agent to do that (allow_write): tell the user what it would do instead")
    try:
        out = _run(t.ainvoke(args or {}))
    except Exception as ex:  # pylint: disable=broad-except
        return f"tool error: {type(ex).__name__}: {str(ex)[:1500]}"
    if isinstance(out, tuple):                       # (content, artifact) of content_and_artifact tools
        out = out[0]
    if isinstance(out, list) and out and all(isinstance(b, dict) and b.get("type") == "text" for b in out):
        return "\n".join(str(b.get("text") or "") for b in out)      # MCP text content blocks: their text
    if isinstance(out, (dict, list)):
        return json.dumps(out, default=str)
    return out if isinstance(out, str) else json.dumps(out, default=str)


def pieces(username: str | None = None) -> Iterator[dict[str, Any]]:
    """One knowledge piece per tool (what it does, its arguments), for the decider and the search."""
    from supagent.knowledge.learner import learning_username

    user = username or learning_username()
    by_server = {s["name"]: s for s in servers()}
    for t in tools(user):
        md = t.metadata or {}
        s = by_server.get(md.get("server"), {})
        params = ", ".join(sorted((_schema(t).get("properties") or {}).keys()))
        text = (f"MCP tool {md.get('tool')} of the source {md.get('server')}"
                + (f" ({s.get('description')})" if s.get("description") else "") + f": {t.description or ''}"
                + (f" Arguments: {params}." if params else "") + (" It changes something." if md.get("write") else ""))
        yield {"ref": f"mcp:{md.get('server')}:{md.get('tool')}", "kind": "tool", "database_id": None,
               "title": f"{md.get('server')}: {md.get('tool')}", "text": text[:2000]}
