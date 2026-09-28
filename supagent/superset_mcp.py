"""Superset's own MCP tools (charts, dashboards) called in-process, as the user who asks.

Superset's MCP service (Superset 6.1+, with fastmcp installed) reads the user from g.user when
a request context is active; the agent runs every question in a request context whose g.user
is the user who asks (supagent.security.acting_as), so the service checks that user's
permissions. Without a request context it would fall back on MCP_DEV_USERNAME: to fail
closed, the service is first asked who it acts as (get_instance_info), and its tools are not
offered for the question unless it names the user who asks.

Without Superset's MCP service the agent still answers (SQL, dictionary, files, e-mails,
reports); only saving charts and dashboards is not possible.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from supagent.registry import _inline_refs

log = logging.getLogger(__name__)
TOOLS = ("get_chart_type_schema", "generate_chart", "update_chart", "update_chart_preview", "list_charts",
         "get_chart_info", "generate_dashboard", "add_chart_to_existing_dashboard", "list_dashboards",
         "get_dashboard_info")


def result_text(result: Any) -> str:
    parts = [getattr(c, "text", None) or "" for c in getattr(result, "content", None) or []]
    text = "\n".join(p for p in parts if p)
    if text:
        return text
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    return json.dumps(data, default=str, ensure_ascii=False)


class SupersetMCP:
    """One in-process client for the tools of a question (open, call..., close)."""

    def __init__(self, username: str, names: tuple[str, ...] = TOOLS) -> None:
        self.username = username
        self.error: str | None = None
        self.specs: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        try:
            from fastmcp import Client
            from superset.mcp_service.app import mcp as server
        except Exception as ex:  # pylint: disable=broad-except
            self.error = f"Superset's MCP service is not available ({type(ex).__name__}: {str(ex)[:200]})"
            return
        self._loop = asyncio.new_event_loop()
        try:
            self._client = Client(server)
            self._run(self._client.__aenter__())
            listed = self._run(self._client.list_tools())
            who = self._whoami()
            if who != username:
                raise PermissionError(f"Superset's MCP service would act as {who!r}, not as {username!r}")
            for t in listed:
                if t.name in names:
                    self.specs[t.name] = {"type": "function", "function": {
                        "name": t.name, "description": (t.description or "")[:1500],
                        "parameters": _inline_refs(t.inputSchema or {"type": "object", "properties": {}})}}
        except Exception as ex:  # pylint: disable=broad-except
            self.error = f"Superset's MCP tools are off for this question: {type(ex).__name__}: {str(ex)[:300]}"
            log.warning("supagent: %s", self.error)
            self.specs = {}
            self.close()

    def _run(self, coro: Any) -> Any:
        assert self._loop is not None
        return self._loop.run_until_complete(coro)

    def _whoami(self) -> str | None:
        res = self._run(self._client.call_tool("get_instance_info", {"request": {}}, raise_on_error=False))
        try:
            data = json.loads(result_text(res))
        except ValueError:
            return None
        return ((data or {}).get("current_user") or {}).get("username")

    @property
    def available(self) -> bool:
        return self._client is not None and not self.error

    def schema(self, name: str) -> dict:
        return (self.specs.get(name) or {}).get("function", {}).get("parameters") or {}

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if not self.available:
            return f"error: {self.error or 'Superset MCP tools are not available'}"
        try:
            res = self._run(self._client.call_tool(name, arguments, raise_on_error=False))
        except Exception as ex:  # pylint: disable=broad-except
            return f"tool error: {type(ex).__name__}: {str(ex)[:1000]}"
        return result_text(res)

    def close(self) -> None:
        if self._client is not None and self._loop is not None:
            try:
                self._run(self._client.__aexit__(None, None, None))
            except Exception:  # pylint: disable=broad-except
                pass
        self._client = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None
