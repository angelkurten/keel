"""MCP server exposing the five tools. Optional dependency: `pip install keel[mcp]`.

    keel serve cartridges/fleet --tenant heineken-mx --events examples/events/fleet.jsonl

Any MCP-capable agent (Claude, an OpenAI agent, LangGraph via an MCP client)
then sees context/history/search/annotate/related and nothing else.
"""

from __future__ import annotations

from typing import Any

from keel.tools import Tools


def build_server(tools: Tools):  # pragma: no cover - needs the optional mcp package
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as exc:
            raise SystemExit("MCP server needs the optional dependency: uv sync --extra mcp") from exc

    mcp = Server(f"keel:{tools.cartridge.name}:{tools.tenant}")

    @mcp.tool()
    def context(subject: str) -> dict[str, Any]:
        """What is true now about a subject ('<kind>:<id>'), with the rules that apply."""
        return tools.context(subject)

    @mcp.tool()
    def history(subject: str, since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
        """Facts over time for a subject. ISO dates."""
        from datetime import datetime

        s = datetime.fromisoformat(since) if since else None
        u = datetime.fromisoformat(until) if until else None
        return tools.history(subject, s, u)

    @mcp.tool()
    def search(text: str) -> list[dict[str, Any]]:
        """Entities and facts matching text."""
        return tools.search(text)

    @mcp.tool()
    def annotate(subject: str, predicate: str, value: str, scope: str = "task", evidence: str | None = None, by: str | None = None) -> dict[str, Any]:
        """Write what you learned. tenant/global scopes require evidence."""
        return tools.annotate(subject, predicate, value, scope, evidence, by)

    @mcp.tool()
    def related(subject: str, relation: str | None = None) -> list[dict[str, Any]]:
        """Neighbors of a subject, optionally by relation type."""
        return tools.related(subject, relation)

    return mcp
