"""MCP server exposing the five tools. Optional dependency: `pip install keel[mcp]`.

    keel serve cartridges/fleet --tenant heineken-mx --events examples/events/fleet.jsonl

Any MCP-capable agent (Claude, an OpenAI agent, LangGraph via an MCP client)
then sees context/history/search/annotate/related and nothing else.
"""

from __future__ import annotations

import functools
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

    try:
        from mcp.server.mcpserver.exceptions import ToolError  # mcp >= 2
    except ImportError:  # pragma: no cover
        from mcp.server.fastmcp.exceptions import ToolError  # mcp 1.x

    def guarded(fn):
        """A refusal (scope policy, agent registry) must reach the agent with its reason; the
        server would otherwise mask it as 'Error executing tool'."""

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except PermissionError as exc:
                raise ToolError(str(exc)) from exc

        return inner

    mcp = Server(":".join(filter(None, ["keel", tools.cartridge.name, tools.tenant, tools.agent_id])))
    # Four tools only read. Clients that gate calls on annotations (Codex does) run them without asking.
    read_only = {"annotations": ToolAnnotations(readOnlyHint=True)}

    @mcp.tool(**read_only)
    @guarded
    def context(subject: str, as_of: str | None = None, depth: int = 0, as_known_at: str | None = None, task: str | None = None) -> dict[str, Any]:
        """What is true about a subject ('<kind>:<id>') now, or at an ISO datetime, with the rules that apply and their evaluation. depth=1 adds each neighbour. as_known_at: what Keel knew then. task: one task's memory. The result's watermark is the cursor for history(changed_since=)."""
        return tools.context(subject, _dt(as_of), depth, _dt(as_known_at), task)

    @mcp.tool(**read_only)
    @guarded
    def history(
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        predicate: str | None = None,
        scope: str | None = None,
        as_known_at: str | None = None,
        task: str | None = None,
        changed_since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time for a subject. ISO dates; filters by predicate, scope, task; as_known_at for what Keel knew then. Without a subject, changed_since (a watermark) lists the tenant's changes since then."""
        return tools.history(subject, _dt(since), _dt(until), predicate, scope, _dt(as_known_at), task, _dt(changed_since))

    @mcp.tool(**read_only)
    @guarded
    def search(text: str = "", kind: str | None = None, rule: str | None = None, limit: int | None = None, after: str | None = None) -> list[dict[str, Any]]:
        """Entities and facts matching text. With kind or rule: list the entities of a kind, or those where a rule fires now, ordered by key; page with limit and after."""
        return tools.search(text, kind, rule, limit, after)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    @guarded
    def annotate(
        subject: str, predicate: str, value: str, scope: str = "task", evidence: str | None = None, by: str | None = None,
        task: str | None = None, key: str | None = None,
    ) -> dict[str, Any]:
        """Write what you learned. tenant/global scopes require evidence. task ties it to one job; key makes a retried write return the first one."""
        return tools.annotate(subject, predicate, value, scope, evidence, by, task, key)

    @mcp.tool(**read_only)
    @guarded
    def related(subject: str, relation: str | None = None, as_of: str | None = None) -> list[dict[str, Any]]:
        """Neighbors of a subject, optionally by relation type, now or at an ISO datetime."""
        return tools.related(subject, relation, _dt(as_of))

    return mcp
