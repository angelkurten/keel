"""MCP server exposing the five tools. Optional dependency: `pip install keel[mcp]`.

    keel serve examples/fleet --tenant heineken-mx --events examples/fleet/events.jsonl

Any MCP-capable agent (Claude, an OpenAI agent, LangGraph via an MCP client)
then sees context/history/search/annotate/related and nothing else.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from keel.envelope import parse_iso

try:  # module level: the tool signatures name it, and MCP injects it by that annotation
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover - mcp 1.x, or no mcp at all (build_server then refuses)
    try:
        from mcp.server.fastmcp import Context
    except ImportError:
        Context = Any
from keel.store.base import Scope
from keel.tools import DESCRIPTIONS, Tools, _lean


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
    out = {k: compact(v) for k, v in schema.items() if k != "title" and not (k == "additionalProperties" and v is True)}
    if isinstance(out.get("anyOf"), list) and {"type": "boolean"} in out["anyOf"] and {"type": "array", "items": {}} in out["anyOf"]:
        return {}  # every JSON type: an empty schema says it in two characters
    options = out.get("anyOf")
    if isinstance(options, list) and {"type": "null"} in options and "default" in out and out["default"] is None:
        rest = [o for o in options if o != {"type": "null"}]
        out.pop("anyOf")
        out.pop("default")
        out.update(rest[0] if len(rest) == 1 else {"anyOf": rest})
    return out


_compact_schema = compact  # build_server's `compact` flag shadows the name there

_UTC = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d)(:\d\d)?(\.\d+)?\+00:00")
MARKERS = ("more", "note", "initial")  # the elements a list leads with to say what it left out, never records
ESCAPE = "~"  # an object of data whose keys would read as a table (cols and rows) travels as {"~": object}


def _short(m: re.Match) -> str:
    sec = "" if m.group(2) == ":00" and not m.group(3) else (m.group(2) or "")
    return f"{m.group(1)}{sec}{m.group(3) or ''}Z"


def _table(items: list[Any]) -> Any:
    """A list of records as {all, cols, rows}, or the list as it is when no field is shared or it holds no records."""
    lead = 0
    while lead < len(items) and isinstance(items[lead], dict) and len(items[lead]) == 1 and next(iter(items[lead])) in MARKERS:
        lead += 1
    recs = items[lead:]
    if len(recs) < 2 or not all(isinstance(r, dict) for r in recs) or any("." in k for r in recs for k in r):
        return items
    flat = []
    for r in recs:  # a map every record has, with keys they all share, becomes `map.key` fields
        out: dict[str, Any] = {}
        for k, v in r.items():
            shared = isinstance(v, dict) and all(isinstance(o.get(k), dict) for o in recs) and set(v).intersection(*(o[k] for o in recs))
            out.update({f"{k}.{kk}": vv for kk, vv in v.items()} if shared else {k: v})
        flat.append(out)
    common = [k for k in flat[0] if all(k in f for f in flat)]
    if not common:
        return items
    same = {k: flat[0][k] for k in common if all(f[k] == flat[0][k] for f in flat)}
    cols = [k for k in common if k not in same]
    rows = [[f[k] for k in cols] + ([extra] if (extra := {k: v for k, v in f.items() if k not in common}) else []) for f in flat]
    out = {k: v for m in items[:lead] for k, v in m.items()}
    return out | ({"all": same} if same else {}) | {"cols": cols, "rows": rows}


def pack(value: Any) -> Any:
    """The compact text form a model reads (`build_server(compact=True)`): the same data in fewer tokens, lossless
    (`unpack` gives it back). UTC times are written `2026-10-03T08:00Z` (seconds when not zero, every fraction kept);
    a list of two or more records is one table, `{"all": {fields every record shares}, "cols": [...], "rows": [[...]]}`,
    a row in the order of cols ending with an object of that record's other fields when it has some, a map the
    records share written as `map.key` fields, and the list's leading markers (`more`, `note`, `initial`) as keys."""
    if isinstance(value, str):
        return _UTC.sub(_short, value) if _UTC.fullmatch(value) else value
    if isinstance(value, dict):
        out = {k: pack(v) for k, v in value.items()}
        return {ESCAPE: out} if ("cols" in out and "rows" in out) or list(out) == [ESCAPE] else out
    if isinstance(value, list):
        return _table([pack(v) for v in value])
    return value


def unpack(value: Any) -> Any:
    """`pack`'s inverse: tables back to lists of records (times stay short: the same instants)."""
    if isinstance(value, list):
        return [unpack(v) for v in value]
    if not isinstance(value, dict):
        return value
    if list(value) == [ESCAPE]:
        inner = value[ESCAPE]
        return {k: unpack(v) for k, v in inner.items()} if isinstance(inner, dict) else unpack(inner)
    if "cols" not in value or "rows" not in value:
        return {k: unpack(v) for k, v in value.items()}
    out: list[Any] = [{k: value[k]} for k in value if k in MARKERS]
    cols = value["cols"]
    for row in value["rows"]:
        rec: dict[str, Any] = {}
        extra = row[len(cols)] if len(row) > len(cols) else {}
        for name, v in [*value.get("all", {}).items(), *zip(cols, row), *extra.items()]:
            if "." in name:
                parent, key = name.split(".", 1)
                rec.setdefault(parent, {})[key] = v
            else:
                rec[name] = v
        out.append(unpack(rec))
    return out


def _v(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def as_text(result: Any) -> str:
    """`detail="text"`: a brief context or a concise history as lines (one per fact, with its time), for models that
    read prose better than JSON. An experiment (reports/Eficiencia de herramientas en Keel.md, R11): the eval decides."""
    if isinstance(result, list) and result and "facts" in result[0]:
        return "\n\n".join(as_text(r) for r in result)
    if isinstance(result, list):  # history: one fact per line, oldest first
        lines = []
        for f in result:
            if "predicate" not in f:
                lines.append(_v(f))  # the {"more": ...} marker
                continue
            rest = {k: v for k, v in f.items() if k not in ("subject", "predicate", "value", "valid_from", "valid_to", "source", "evidence")}
            prov = "; ".join(_v(f[k]) for k in ("source", "evidence") if f.get(k))
            lines.append(f"{f['valid_from']} -> {f.get('valid_to') or 'now'}  {f.get('subject', '') + ' ' if 'subject' in f else ''}{f['predicate']} = {_v(f['value'])}"
                         + (f"  [{prov}]" if prov else "") + (f"  {_v(rest)}" if rest else ""))
        return "\n".join(lines)
    if "facts" not in result:  # {found: false}
        return _v(result)
    since = result.get("since", {})
    lines = [f"{result['subject']} {_v(result['attrs']) if result.get('attrs') else ''}".rstrip(), f"as_of {result['as_of']}"]
    lines += [f"{p} = {_v(v)}" + (f" (since {since[p]})" if p in since else "") for p, v in result["facts"].items()]
    lines += [f"{r}: {', '.join(subjects)}" for r, subjects in result["relations"].items()]
    lines += [f"rule {r}: {_v(v)}" for r, v in result["evaluation"].items()]
    for n in result.get("neighbors", []):
        facts = ", ".join(f"{p}={_v(v)}" for p, v in n["facts"].items())
        rules = ", ".join(f"{r}={_v(v)}" for r, v in n["evaluation"].items())
        lines.append(f"neighbor {n['relation'] if n['direction'] != 'in' else '<-' + n['relation']} {n['subject']}: {facts}" + (f"; {rules}" if rules else ""))
    lines += [f"{k}: {_v(result[k])}" for k in ("watermark", "conflicts", "proposals", "relations_more", "neighbors_more", "neighbors_truncated", "as_known_at") if k in result]
    return "\n".join(lines)


TABLES = ('Lists of records come as tables: {"all": {fields every record shares}, "cols": [...], "rows": [[...]]}, a row holding '
          "a record's values in the order of cols, and a last object with its other fields when it has some; `a.b` is field b of a. "
          "Times are UTC.")


FEW = 3  # a kind with at most this many subjects is named with them in a bound server's instructions


def few_subjects(tools: Tools, n: int = FEW) -> dict[str, list[str]]:
    """The subjects of each kind the bound agent may read that has at most `n` of them (a project, a site, an account):
    what an agent would otherwise look up before filtering by it."""
    out = {}
    for k in sorted(tools.cartridge.entities):
        if tools.agent is not None and not tools.agent.sees_kind(k):
            continue
        keys = [x for x in tools.search(kind=k, detail="key", limit=n + 1) if isinstance(x, str)]
        if 0 < len(keys) <= n:
            out[k] = keys
    return out


def instructions(cartridge: Any, agent: Any = None, compact: bool = True, few: dict[str, list[str]] | None = None) -> str:
    """What every call would otherwise repeat, said once per session: the kinds, the relations and what each rule means
    (context's `evaluation` names them), as far as `agent` (a registry entry) may read; `few` (a bound server's
    `few_subjects`) names the subjects of the kinds that have only a few. Sorted, so the text, and the prompt cache
    behind it, never changes for a cartridge and agent (and those few subjects)."""
    kind = (lambda k: True) if agent is None else agent.sees_kind
    seen = {r: s for r, s in cartridge.relations.items() if agent is None or (agent.sees_relation(r) and (not isinstance(s, dict) or (kind(s.get("from")) and kind(s.get("to")))))}
    rels = ", ".join(f"{r} ({s.get('from')}->{s.get('to')})" if isinstance(s, dict) else r for r, s in sorted(seen.items()))
    rules = "\n".join(f"- {r} ({s.get('applies_to', 'any')}): {s.get('definition', '').strip()}" for r, s in sorted(cartridge.rules.items())
                      if s.get("applies_to") is None or kind(s.get("applies_to")))
    kinds = ", ".join(f"{k} ({', '.join(few[k])})" if few and few.get(k) else k for k in sorted(cartridge.entities) if kind(k))
    return (f"Subjects are '<kind>:<id>'. Kinds: {kinds}.\nRelations: {rels}.\n"
            f"Rules (context's `evaluation`; search(rule=) lists where one holds; trust them):\n{rules}" + (f"\n{TABLES}" if compact else ""))


READ_ONLY = "this server is read-only: it serves context, history, search and related; annotate is not served"


def writes_nothing(agent: Any) -> bool:
    """An agent whose registry entry may write no scope (`writes: []`): it is served the read tools only."""
    return agent is not None and not any(agent.can_write(s) for s in Scope)


def build_server(tools: Tools | Callable[[Any], Tools], name: str | None = None, cartridge: Any = None, compact: bool = True,  # pragma: no cover - needs the optional mcp package
                 read_only: bool = False):
    """`tools` is bound to one tenant, or a function of the HTTP request that builds them per call (one server for
    many tenants: the tenant and agent come from the request's key and headers, keel/http.py). `compact` (default): the text a
    model reads is `pack`ed (tables, short times; the instructions say how to read it), structuredContent unchanged; `compact=False` sends the JSON text.
    `read_only`: only the four read tools are listed (annotate is refused), for agents that never write; a bound agent
    whose registry entry may write no scope is served so without the flag."""
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as exc:
            raise SystemExit("MCP server needs the optional dependency: uv sync --extra mcp") from exc
    from mcp.types import CallToolResult, ToolAnnotations

    try:
        from mcp.server.mcpserver.exceptions import ToolError  # mcp >= 2
    except ImportError:  # pragma: no cover
        from mcp.server.fastmcp.exceptions import ToolError  # mcp 1.x

    def _trace(ctx: Any) -> str | None:
        """The caller's trace context from the request `_meta` (MCP carries W3C traceparent there)."""
        try:
            rc = getattr(ctx, "request_context", None)
        except ValueError:  # a call made in-process, outside a request, has no request context and no trace
            return None
        meta = getattr(rc, "meta", None)
        if meta is None:
            return None
        data = meta if isinstance(meta, dict) else (meta.model_dump(by_alias=True, exclude_none=True) if hasattr(meta, "model_dump") else {})
        parts = [data.get("traceparent")] + [f"{k}={data[k]}" for k in ("tool_call_id", "toolCallId") if data.get(k)]
        return "; ".join(p for p in parts if p) or None

    def guarded(fn):
        """A refusal (scope policy, agent registry) must reach the agent with its reason; the
        server would otherwise mask it as 'Error executing tool'. Bad input does too.
        The result is one text block of compact JSON, what the model reads: the SDK would indent it and split a list
        into a block per item (20-40% more tokens). structuredContent keeps the data for programmatic clients."""
        wrap = fn.__annotations__.get("return") != "dict[str, Any]"  # the SDK wraps non-object results in {"result": ...}

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except (PermissionError, ValueError) as exc:
                raise ToolError(str(exc)) from exc
            if compact:  # through JSON first: the same values the plain text would carry (datetimes as ISO text)
                result_text = pack(json.loads(json.dumps(result, ensure_ascii=False, default=str)))
            text = json.dumps(result_text if compact else result, ensure_ascii=False, separators=(",", ":"), default=str)
            if kwargs.get("detail") == "text":  # the same data as lines for the model; structuredContent keeps the JSON
                text = as_text(result)
            return CallToolResult.model_validate({"content": [{"type": "text", "text": text}], "structuredContent": {"result": result} if wrap else result})

        return inner

    def bound(ctx: Any) -> Tools:
        if isinstance(tools, Tools):
            return tools
        return tools(getattr(getattr(ctx, "request_context", None), "request", None))

    if isinstance(tools, Tools):
        name = ":".join(filter(None, ["keel", tools.cartridge.name, tools.tenant, tools.agent_id]))
        read_only = read_only or writes_nothing(tools.agent)
    known = tools.cartridge if isinstance(tools, Tools) else cartridge  # a server for many tenants may still have one cartridge
    mcp = Server(name or "keel", instructions=instructions(known, tools.agent if isinstance(tools, Tools) else None, compact,
                                                                few_subjects(tools) if isinstance(tools, Tools) else None) if known is not None else None)
    # Four tools only read. Clients that gate calls on annotations (Codex does) run them without asking.
    # no outputSchema: it would only say "an object" or "a list", in tokens a client may hand the model on every call;
    # structuredContent still carries the data
    reader = {"annotations": ToolAnnotations(readOnlyHint=True), "structured_output": False}

    @mcp.tool(**reader, description=DESCRIPTIONS["context"])
    @guarded
    def context(subject: str | list[str], as_of: str | None = None, depth: int = 0, as_known_at: str | None = None, task: str | None = None, detail: Literal["brief", "concise", "full", "text"] = "brief", exclude: list[str] | None = None, only: list[str] | None = None, ctx: Context | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        return bound(ctx).context(subject, _dt(as_of), depth, _dt(as_known_at), task, "brief" if detail == "text" else detail, exclude, only)

    @mcp.tool(**reader, description=DESCRIPTIONS["history"])
    @guarded
    def history(
        subject: str | None = None,
        since: str | None = None,
        until: str | None = None,
        predicate: str | None = None,
        scope: Literal["agent", "task", "tenant", "global"] | None = None,
        as_known_at: str | None = None,
        task: str | None = None,
        changed_since: str | None = None,
        limit: int = 50,
        detail: Literal["concise", "full", "text"] = "concise",
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        return bound(ctx).history(subject, _dt(since), _dt(until), predicate, scope, _dt(as_known_at), task, _dt(changed_since), limit, "concise" if detail == "text" else detail)

    @mcp.tool(**reader, description=DESCRIPTIONS["search"])
    @guarded
    def search(
        text: str | list[str] = "", kind: str | list[str] | None = None, rule: str | None = None, limit: int | None = None, after: str | None = None, mode: Literal["text", "semantic"] = "text",
        min_score: float | None = None, related_to: str | None = None, via: str | None = None,
        detail: Literal["key", "entity", "brief", "concise", "full", "count"] = "entity", depth: int = 0,
        per_kind: int | None = None, exclude: list[str] | None = None, as_of: str | None = None,
        where: dict[str, Any] | None = None, as_known_at: str | None = None,
        ctx: Context | None = None,
    ) -> list[Any]:
        out = bound(ctx).search(text, kind, rule, limit, after, mode, min_score, related_to, via, detail, depth, per_kind, exclude, _dt(as_of), where, _dt(as_known_at))
        if detail != "entity":
            return out
        lean = lambda hits: [_lean(h) if isinstance(h, dict) and "predicate" in h else h for h in hits]  # an agent reads fact hits without bookkeeping
        return [lean(x) for x in out] if isinstance(text, list) else lean(out)

    def annotate(
        # int stays apart from float so pydantic keeps 3 as 3, not 3.0
        subject: str, predicate: str, value: str | int | float | bool | list[Any] | dict[str, Any] | None, scope: Literal["agent", "task", "tenant", "global"] = "task", evidence: str | None = None, by: str | None = None,  # noqa: PYI041
        task: str | None = None, key: str | None = None, status: Literal["proposed", "rejected"] | None = None, detail: Literal["ack", "full"] = "ack", ctx: Context | None = None,
    ) -> dict[str, Any]:
        t = copy.copy(bound(ctx))  # a bound server shares one Tools across sessions: each call keeps its own trace
        t.trace = _trace(ctx)
        return t.annotate(subject, predicate, value, scope, evidence, by, task, key, status, detail)

    if not read_only:
        mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False), structured_output=False,
                 description=DESCRIPTIONS["annotate"])(guarded(annotate))

    @mcp.tool(**reader, description=DESCRIPTIONS["related"])
    @guarded
    def related(subject: str, relation: str | list[str] | None = None, as_of: str | None = None, depth: int = 1, limit: int = 50, ctx: Context | None = None) -> list[dict[str, Any]]:
        return bound(ctx).related(subject, relation, _dt(as_of), depth, limit)

    manager = getattr(mcp, "_tool_manager", None)  # ponytail: the mcp package's own attribute; a public hook if it offers one
    for t in manager.list_tools() if manager else []:
        t.parameters = _compact_schema(t.parameters)
    if read_only and manager:  # "Unknown tool: annotate" would leave the agent guessing why
        call = manager.call_tool

        async def refusing(name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            if name == "annotate":
                raise ToolError(READ_ONLY)
            return await call(name, arguments, *args, **kwargs)

        manager.call_tool = refusing
    return mcp
