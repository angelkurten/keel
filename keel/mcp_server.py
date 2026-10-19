"""MCP server exposing the five tools. Optional dependency: `pip install keel[mcp]`.

    keel serve examples/fleet --tenant heineken-mx --events examples/fleet/events.jsonl

Any MCP-capable agent (Claude, an OpenAI agent, LangGraph via an MCP client)
then sees context/history/search/annotate/related and nothing else.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
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


def compact(schema: Any) -> Any:
    """A tool's JSON Schema without pydantic's noise: no `title` on every property, and an optional parameter as its
    own type instead of `anyOf [T, null]` with `default: null` (leaving it out means null). Same meaning, about half
    the tokens an agent pays on every call that lists the tools."""
    if isinstance(schema, list):
        return [compact(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: compact(v) for k, v in schema.items() if k != "title"}
    options = out.get("anyOf")
    if isinstance(options, list) and {"type": "null"} in options and "default" in out and out["default"] is None:
        rest = [o for o in options if o != {"type": "null"}]
        out.pop("anyOf")
        out.pop("default")
        out.update(rest[0] if len(rest) == 1 else {"anyOf": rest})
    return out


def build_server(tools: Tools | Callable[[Any], Tools], name: str | None = None):  # pragma: no cover - needs the optional mcp package
    """`tools` is bound to one tenant, or a function of the HTTP request that builds them per call (one server for
    many tenants: the tenant and agent come from the request's key and headers, keel/http.py)."""
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

    def bound(ctx: Any) -> Tools:
        if isinstance(tools, Tools):
            return tools
        return tools(getattr(getattr(ctx, "request_context", None), "request", None))

    if isinstance(tools, Tools):
        name = ":".join(filter(None, ["keel", tools.cartridge.name, tools.tenant, tools.agent_id]))
    mcp = Server(name or "keel")
    # Four tools only read. Clients that gate calls on annotations (Codex does) run them without asking.
    read_only = {"annotations": ToolAnnotations(readOnlyHint=True)}

    @mcp.tool(**read_only)
    @guarded
    def context(subject: str | list[str], as_of: str | None = None, depth: int = 0, as_known_at: str | None = None, task: str | None = None, detail: str = "full", exclude: list[str] | None = None, only: list[str] | None = None, ctx: Context | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        """What is true about a subject ('<kind>:<id>') now, or at an ISO datetime, with the rules that apply and their evaluation. A list of subjects returns their answers in order. depth=1 adds each neighbour. as_known_at: what Keel knew then. task: one task's memory. detail="concise" is about a third of the tokens. exclude: predicates to leave out; only: just these predicates (e.g. the evidence to check from many subjects). The result's watermark is the cursor for history(changed_since=)."""
        return bound(ctx).context(subject, _dt(as_of), depth, _dt(as_known_at), task, detail, exclude, only)

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
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time for a subject. ISO dates; filters by predicate, scope, task; as_known_at for what Keel knew then. Without a subject, changed_since (a watermark) lists the tenant's changes since then."""
        return bound(ctx).history(subject, _dt(since), _dt(until), predicate, scope, _dt(as_known_at), task, _dt(changed_since), limit)

    @mcp.tool(**read_only)
    @guarded
    def search(
        text: str | list[str] = "", kind: str | list[str] | None = None, rule: str | None = None, limit: int | None = None, after: str | None = None, mode: str = "text",
        min_score: float | None = None, related_to: str | None = None, via: str | None = None, detail: str = "entity", depth: int = 0,
        per_kind: int | None = None, exclude: list[str] | None = None, as_of: str | None = None,
        where: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> list[Any]:
        """Entities and facts matching text. With kind or rule: list the entities of a kind, or those where a rule fires now, ordered by key; page with limit and after. mode="semantic": subjects ranked by meaning, with a score. related_to (+ via): only that subject's neighbours, e.g. cards of a client where a rule holds. detail="concise" puts each result's current facts in it (no context call needed); depth=1 adds its neighbours. per_kind=n: the best n of every kind; with related_to and mode="semantic", the context for a task in one call. exclude: predicates to leave out. where={name: value}: only entities whose attribute or current fact has that value (a visual by its exact title). Each result says what matched and quotes it (matched, snippet). kind may be a list of kinds. A list of texts returns one result list per text."""
        return bound(ctx).search(text, kind, rule, limit, after, mode, min_score, related_to, via, detail, depth, per_kind, exclude, _dt(as_of), where)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    @guarded
    def annotate(
        # int stays apart from float so pydantic keeps 3 as 3, not 3.0
        subject: str, predicate: str, value: str | int | float | bool | list[Any] | dict[str, Any], scope: str = "task", evidence: str | None = None, by: str | None = None,  # noqa: PYI041
        task: str | None = None, key: str | None = None, status: str | None = None, ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Write what you learned; value is any JSON (text, number, boolean, list or object). tenant/global scopes require evidence. task ties it to one job; key makes a retried write return the first one. status="proposed" proposes it for a person or authorized agent to accept."""
        t = bound(ctx)
        t.trace = _trace(ctx)
        return t.annotate(subject, predicate, value, scope, evidence, by, task, key, status)

    @mcp.tool(**read_only)
    @guarded
    def related(subject: str, relation: str | list[str] | None = None, as_of: str | None = None, depth: int = 1, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Neighbors of a subject, optionally by relation type (one or a list), now or at an ISO datetime. depth 2 or 3 walks further through those relations; each entry says its hops and the subject it was reached via."""
        return bound(ctx).related(subject, relation, _dt(as_of), depth)

    manager = getattr(mcp, "_tool_manager", None)  # ponytail: the mcp package's own attribute; a public hook if it offers one
    for t in manager.list_tools() if manager else []:
        t.parameters = compact(t.parameters)
    return mcp
