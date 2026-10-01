"""Other MCP servers (mcp.servers), against a real local server (fastmcp, streamable HTTP): their tools are named
<server>_<tool>, a read tool runs, a write tool runs only when an admin allowed it, a server limited to some roles
is not offered to other users, the tools become knowledge pieces, and a server that is down hides nothing else."""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("langchain_mcp_adapters")
pytest.importorskip("fastmcp")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def tickets_server():
    from fastmcp import FastMCP

    mcp = FastMCP("tickets")

    @mcp.tool
    def count_tickets(team: str) -> int:
        """How many open tickets a team has."""
        return {"billing": 3, "payroll": 5}.get(team.lower(), 0)

    @mcp.tool
    def close_ticket(ticket_id: int) -> str:
        """Close a ticket."""
        return f"ticket {ticket_id} closed"

    port = _free_port()
    t = threading.Thread(target=lambda: mcp.run(transport="http", host="127.0.0.1", port=port, show_banner=False),
                         daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"


@pytest.fixture()
def configured(ctx, tickets_server, monkeypatch):
    from supagent import mcp_sources, settings

    conf = {"mcp.servers": [{"name": "tickets", "transport": "streamable_http", "url": tickets_server,
                             "description": "Support tickets of the teams", "write": ["close_ticket"]},
                            {"name": "down", "transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"}]}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    mcp_sources._CACHE.clear()
    yield conf
    mcp_sources._CACHE.clear()


def test_tools_are_named_by_server_and_run(configured):
    from supagent import mcp_sources

    names = sorted(s["function"]["name"] for s in mcp_sources.specs("admin"))
    assert names == ["tickets_close_ticket", "tickets_count_tickets"]          # the server down hides nothing
    spec = next(s for s in mcp_sources.specs("admin") if s["function"]["name"] == "tickets_count_tickets")
    assert "team" in spec["function"]["parameters"]["properties"]
    assert mcp_sources.call("admin", "tickets_count_tickets", {"team": "billing"}) == "3"


def test_a_write_tool_needs_an_admin(configured):
    from supagent import mcp_sources

    refused = mcp_sources.call("admin", "tickets_close_ticket", {"ticket_id": 7})
    assert refused.startswith("tool error: close_ticket changes something in tickets")
    configured["mcp.servers"][0]["allow_write"] = True
    mcp_sources._CACHE.clear()
    assert "ticket 7 closed" in mcp_sources.call("admin", "tickets_close_ticket", {"ticket_id": 7})


def test_roles_and_pieces(configured):
    from supagent import mcp_sources

    pieces = list(mcp_sources.pieces("admin"))
    assert {p["ref"] for p in pieces} == {"mcp:tickets:count_tickets", "mcp:tickets:close_ticket"}
    assert "Support tickets of the teams" in pieces[0]["text"] and all(p["kind"] == "tool" for p in pieces)
    configured["mcp.servers"][0]["roles"] = ["Admin"]
    mcp_sources._CACHE.clear()
    assert mcp_sources.specs("alice") == [] and len(mcp_sources.specs("admin")) == 2
