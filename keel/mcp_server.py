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

try:  # module level: the tool signatures name it, and MCP injects it by that annotation
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover - mcp 1.x, or no mcp at all (build_server then refuses)
    try:
        from mcp.server.fastmcp import Context
    except ImportError:
        Context = Any
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

    def _trace(ctx: Any) -> str | None:
        """The caller's trace context from the request `_meta` (MCP carries W3C traceparent there)."""
        rc = getattr(ctx, "request_context", None)
        meta = getattr(rc, "meta", None)
        if meta is None:
            return None
        data = meta if isinstance(meta, dict) else (meta.model_dump(by_alias=True, exclude_none=True) if hasattr(meta, "model_dump") else {})
        parts = [data.get("traceparent")] + [f"{k}={data[k]}" for k in ("tool_call_id", "toolCallId") if data.get(k)]
        return "; ".join(p for p in parts if p) or None

    def guarded(fn):
        """A refusal (scope policy, agent registry) must reach the agent with its reason; the
        server would otherwise mask it as 'Error executing tool'. Bad input does too."""

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (PermissionError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        return inner

    mcp = Server(":".join(filter(None, ["keel", tools.cartridge.name, tools.tenant, tools.agent_id])))
    # Four tools only read. Clients that gate calls on annotations (Codex does) run them without asking.
    read_only = {"annotations": ToolAnnotations(readOnlyHint=True)}

    @mcp.tool(**read_only)
    @guarded
    def context(subject: str, as_of: str | None = None, depth: int = 0, as_known_at: str | None = None, task: str | None = None, detail: str = "full") -> dict[str, Any]:
        """What is true about a subject ('<kind>:<id>') now, or at an ISO datetime, with the rules that apply and their evaluation. depth=1 adds each neighbour. as_known_at: what Keel knew then. task: one task's memory. detail="concise" is about a third of the tokens. The result's watermark is the cursor for history(changed_since=)."""
        return tools.context(subject, _dt(as_of), depth, _dt(as_known_at), task, detail)

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
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time for a subject. ISO dates; filters by predicate, scope, task; as_known_at for what Keel knew then. Without a subject, changed_since (a watermark) lists the tenant's changes since then."""
        return tools.history(subject, _dt(since), _dt(until), predicate, scope, _dt(as_known_at), task, _dt(changed_since), limit)

    @mcp.tool(**read_only)
    @guarded
    def search(text: str = "", kind: str | None = None, rule: str | None = None, limit: int | None = None, after: str | None = None, mode: str = "text", min_score: float | None = None, related_to: str | None = None, via: str | None = None) -> list[dict[str, Any]]:
        """Entities and facts matching text. With kind or rule: list the entities of a kind, or those where a rule fires now, ordered by key; page with limit and after. mode="semantic": subjects ranked by meaning, with a score. related_to (+ via): only that subject's neighbours, e.g. cards of a client where a rule holds."""
        return tools.search(text, kind, rule, limit, after, mode, min_score, related_to, via)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    @guarded
    def annotate(
        subject: str, predicate: str, value: str, scope: str = "task", evidence: str | None = None, by: str | None = None,
        task: str | None = None, key: str | None = None, status: str | None = None, ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Write what you learned. tenant/global scopes require evidence. task ties it to one job; key makes a retried write return the first one. status="proposed" proposes it for a person or authorized agent to accept."""
        tools.trace = _trace(ctx)
        return tools.annotate(subject, predicate, value, scope, evidence, by, task, key, status)

    @mcp.tool(**read_only)
    @guarded
    def related(subject: str, relation: str | None = None, as_of: str | None = None) -> list[dict[str, Any]]:
        """Neighbors of a subject, optionally by relation type, now or at an ISO datetime."""
        return tools.related(subject, relation, _dt(as_of))

    return mcp
