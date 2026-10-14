"""MCP server exposing the five tools. Optional dependency: `pip install keel[mcp]`.

    keel serve cartridges/fleet --tenant heineken-mx --events examples/events/fleet.jsonl

Any MCP-capable agent (Claude, an OpenAI agent, LangGraph via an MCP client)
then sees context/history/search/annotate/related and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from keel.envelope import parse_iso
from keel.tools import Tools


def _dt(s: str | None) -> datetime | None:
    return parse_iso(s) if s else None


def build_server(tools: Tools):  # pragma: no cover - needs the optional mcp package
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as exc:
            raise SystemExit("MCP server needs the optional dependency: uv sync --extra mcp") from exc
    from mcp.types import ToolAnnotations

    mcp = Server(f"keel:{tools.cartridge.name}:{tools.tenant}")
    # Four tools only read. Clients that gate calls on annotations (Codex does) run them without asking.
    read_only = {"annotations": ToolAnnotations(readOnlyHint=True)}

    @mcp.tool(**read_only)
    def context(subject: str, as_of: str | None = None) -> dict[str, Any]:
        """What is true about a subject ('<kind>:<id>') now, or at an ISO datetime, with the rules that apply and their evaluation."""
        return tools.context(subject, _dt(as_of))

    @mcp.tool(**read_only)
    def history(
        subject: str,
        since: str | None = None,
        until: str | None = None,
        predicate: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time for a subject. ISO dates; optional filters by predicate and memory scope."""
        return tools.history(subject, _dt(since), _dt(until), predicate, scope)

    @mcp.tool(**read_only)
    def search(text: str) -> list[dict[str, Any]]:
        """Entities and facts matching text."""
        return tools.search(text)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    def annotate(subject: str, predicate: str, value: str, scope: str = "task", evidence: str | None = None, by: str | None = None) -> dict[str, Any]:
        """Write what you learned. tenant/global scopes require evidence."""
        return tools.annotate(subject, predicate, value, scope, evidence, by)

    @mcp.tool(**read_only)
    def related(subject: str, relation: str | None = None) -> list[dict[str, Any]]:
        """Neighbors of a subject, optionally by relation type."""
        return tools.related(subject, relation)

    return mcp
