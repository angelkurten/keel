"""The public contract (SPEC §11), frozen in tests/contract.json.

Every surface a caller builds on is extracted here: the package exports, the event envelope, the cartridge
schema, the five tools (and the optional sixth, query, as a server serves it when enabled) in Python, over MCP and over HTTP (names, parameters, defaults, enums), the store
protocol and the Python client. The test fails on any difference from the snapshot and says which changes
break a caller. An additive change (a new optional parameter, a new tool field) is accepted by refreshing
the snapshot; a breaking one needs a deprecation first (SPEC §11) and `--allow-breaking`:

    uv run python tests/test_contract.py --update [--allow-breaking]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "contract.json"
TOOLS = ("context", "history", "search", "annotate", "related", "query")  # query: the optional sixth, as served when enabled
PROSE = {"description", "title", "doc"}  # wording, not contract: a caller never parses it


def _signature(fn: Any) -> dict[str, Any]:
    out = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name == "self":
            continue
        out[name] = {"kind": p.kind.name, "required": p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
        if p.default is not inspect.Parameter.empty:
            out[name]["default"] = repr(p.default)
    return out


def _protocol(cls: Any) -> dict[str, Any]:
    return {n: _signature(f) for n, f in sorted(vars(cls).items()) if callable(f) and not n.startswith("_")}


def contract() -> dict[str, Any]:
    import keel
    from keel import Event, MemoryStore, SqliteStore, Tools, load_cartridge
    from keel.client import Client
    from keel.http import build_http_app
    from keel.mcp_server import build_server
    from keel.store.base import Store

    fleet = load_cartridge(ROOT / "examples" / "fleet")
    tools = Tools(fleet, SqliteStore(), "t")  # a store with SQL, so the optional query tool is listed when enabled
    mcp = asyncio.run(build_server(tools, query=True).list_tools())
    api = build_http_app(fleet, MemoryStore(), secret="s", tenant="t", query=True).app.openapi()
    return {
        "exports": sorted(keel.__all__),
        "envelope": Event.model_json_schema(),
        "cartridge": json.loads((ROOT / "docs" / "cartridge.schema.json").read_text(encoding="utf-8")),
        "tools": {n: _signature(getattr(Tools, n)) for n in TOOLS},
        "mcp": {w["name"]: {k: w.get(k) for k in ("inputSchema", "annotations")} for w in (t.model_dump(by_alias=True, exclude_none=True) for t in mcp)},  # as on the wire
        "http": {"paths": {p: sorted(ops) for p, ops in sorted(api["paths"].items())}, "schemas": api["components"]["schemas"]},
        "store": _protocol(Store),
        "client": {n: _signature(getattr(Client, n)) for n in TOOLS},
    }


def diff(old: Any, new: Any, path: str = "") -> list[tuple[bool, str]]:
    """(breaking, what) for every difference. A removal or a changed value breaks a caller; an addition does
    not, unless it is a required parameter or a new entry in a JSON Schema `required` list."""
    if path.rsplit("/", 1)[-1] in PROSE:
        return [] if old == new else [(False, f"{path}: wording changed")]
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in old.keys() - new.keys():
            out.append((True, f"{path}/{k}: removed"))
        for k in new.keys() - old.keys():
            required = isinstance(new[k], dict) and new[k].get("required") is True
            out.append((required, f"{path}/{k}: added{' (required)' if required else ''}"))
        for k in old.keys() & new.keys():
            out += diff(old[k], new[k], f"{path}/{k}")
        return out
    if isinstance(old, list) and isinstance(new, list) and all(isinstance(x, (str, int, float, bool, type(None))) for x in old + new):
        gone, came = [x for x in old if x not in new], [x for x in new if x not in old]
        out = [(True, f"{path}: {x!r} removed") for x in gone]
        out += [(path.endswith("/required"), f"{path}: {x!r} added") for x in came]
        return out
    return [] if old == new else [(True, f"{path}: {json.dumps(old, default=str)[:80]} -> {json.dumps(new, default=str)[:80]}")]


def test_the_public_contract_matches_its_snapshot():
    changes = diff(json.loads(SNAPSHOT.read_text(encoding="utf-8")), json.loads(json.dumps(contract(), default=str)))
    breaking = [what for b, what in changes if b]
    assert not breaking, "breaking change to the public contract (SPEC §11: deprecate first):\n  " + "\n  ".join(breaking)
    assert not changes, "the contract grew; refresh the snapshot: uv run python tests/test_contract.py --update\n  " + "\n  ".join(w for _, w in changes)


def test_every_transport_takes_the_same_parameters():
    """One tool, one set of parameters: Python, MCP, HTTP, the Python client and Tools.spec() agree."""
    from keel import SqliteStore, Tools, load_cartridge

    c = contract()
    spec = {t["name"]: {a.rstrip("?") for a in t["args"]} for t in Tools(load_cartridge(ROOT / "examples" / "fleet"), SqliteStore(), "t", query=True).spec()}
    for n in TOOLS:
        py = set(c["tools"][n])
        assert set(c["client"][n]) == py, n
        assert set(c["mcp"][n]["inputSchema"]["properties"]) == py, n
        assert set(c["http"]["schemas"][f"{n.capitalize()}In"]["properties"]) == py, n
        assert spec[n] == py, n


def test_the_diff_tells_additions_from_breaks():
    old = {"tools": {"search": {"text": {"required": False, "default": "None"}}}, "required": ["subject"], "enum": ["a", "b"], "description": "x"}
    assert diff(old, old) == []
    assert diff(old, {**old, "description": "y"}) == [(False, "/description: wording changed")]
    assert diff(old, {**old, "tools": {"search": {"text": {"required": False, "default": "None"}, "k": {"required": False}}}}) == [(False, "/tools/search/k: added")]
    assert diff(old, {**old, "tools": {"search": {"text": {"required": False, "default": "None"}, "k": {"required": True}}}})[0][0] is True
    assert diff(old, {**old, "tools": {"search": {"text": {"required": False, "default": "'x'"}}}})[0][0] is True  # a default changed
    assert diff(old, {**old, "tools": {"search": {}}})[0] == (True, "/tools/search/text: removed")
    assert diff(old, {**old, "enum": ["a", "b", "c"]}) == [(False, "/enum: 'c' added")]
    assert diff(old, {**old, "enum": ["a"]}) == [(True, "/enum: 'b' removed")]
    assert diff(old, {**old, "required": ["subject", "value"]}) == [(True, "/required: 'value' added")]


if __name__ == "__main__":
    new = json.loads(json.dumps(contract(), default=str))
    changes = diff(json.loads(SNAPSHOT.read_text(encoding="utf-8")), new) if SNAPSHOT.exists() else []
    for b, what in changes:
        print(("BREAKING " if b else "added    ") + what)
    if "--update" in sys.argv:
        if any(b for b, _ in changes) and "--allow-breaking" not in sys.argv:
            sys.exit("refusing: breaking changes need a deprecation first (SPEC §11); pass --allow-breaking once it is done")
        SNAPSHOT.write_text(json.dumps(new, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
