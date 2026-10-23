"""Adversarial tests of the MCP server (keel/mcp_server.py) and how `keel serve` mounts it at /mcp.

What an MCP client must never do (CLAUDE.md rules 5, 7, 8; SPEC §6, §6.1, §7.7): read or write another
tenant, change the agent it calls as, write the global partition without the right, read data back through
errors or `instructions`, write through a tool annotated `readOnlyHint`, or exhaust the server past its caps.

A test that passes proves the guard holds. A test marked `xfail(strict=True, reason="SECURITY: ...")` shows
a hole that is still open: it fails today, and starts failing the suite (XPASS) the day the hole is closed,
so the marker is removed with the fix.
"""

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter

pytest.importorskip("mcp")

from keel.mcp_server import build_server

FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"
OTHER = "rival-freight"
SECRET = "RIVAL-SECRET-7f3a"  # a value only OTHER holds: it must never appear in an answer to TENANT
AGENTS = {
    "tires-bot": {"writes": ["task", "agent"],
                  "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]}},
    "fleet-copilot": {"writes": ["task", "agent", "tenant"]},
}
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


# ---- helpers ------------------------------------------------------------------------------------

def _events(tenant: str = TENANT):
    return [e.model_copy(update={"tenant": tenant}) for e in JsonlAdapter(EVENTS)]


def _fleet(store=None, registry: bool = False):
    c = load_cartridge(FLEET)
    if registry:
        c.agents = parse_agents(AGENTS)
    s = store if store is not None else MemoryStore()
    Ingestor(c, s).ingest(_events())
    return c, s


def _plant_other_tenant(s) -> None:
    """OTHER has the same subject keys as TENANT, with a value only it holds, in every place a read looks."""
    s.upsert_entity(OTHER, "unit", "MXK-4821", plate=SECRET)
    s.upsert_entity(OTHER, "driver", "D-666", name=SECRET)
    s.link(OTHER, "driver:D-666", "assigned_to", "unit:MXK-4821", valid_from=T0)
    s.open_fact(OTHER, "unit:MXK-4821", "secret_note", SECRET, valid_from=T0, source="rival.db", evidence="ev-rival")
    s.open_fact(OTHER, "tire:T-9001", "depth_mm", SECRET, valid_from=T0, source="rival.db", evidence="ev-rival")


def _call(srv, name: str, args: dict) -> tuple[bool, str, object]:
    """In-process tools/call: (is_error, the text block the model reads, structuredContent)."""
    try:
        res = asyncio.run(srv.call_tool(name, args))
    except Exception as exc:  # noqa: BLE001 - a refusal reaches the caller as a ToolError
        return True, str(exc), None
    if isinstance(res, tuple):  # pragma: no cover - mcp 1.x returns (content, structured)
        content, structured = res
        return False, content[0].text, structured
    err = getattr(res, "is_error", None) or getattr(res, "isError", False)
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    return bool(err), res.content[0].text, structured


def _everything(text: str, structured: object) -> str:
    return text + json.dumps(structured, default=str)


def _tool_fn(srv, name: str):
    """The registered function itself, to pass a request context the way the SDK would."""
    return srv._tool_manager.get_tool(name).fn


class _Ctx:
    """A request context with `_meta`, as the MCP SDK hands a tool."""

    def __init__(self, meta: dict) -> None:
        self.request_context = type("RC", (), {"meta": meta, "request": None})()


@contextlib.contextmanager
def _mcp_http(app):
    """MCP over the HTTP app's /mcp, driven with raw JSON-RPC through TestClient (json_response mode)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "redteam", "version": "1"}}}
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:

        def session(headers: dict) -> dict:
            r = client.post("/mcp", headers={**base, **headers}, json=init)
            if r.status_code != 200:
                return {"status": r.status_code, "detail": r.text}
            sid = r.headers.get("mcp-session-id")
            h = {**base, **headers, **({"mcp-session-id": sid} if sid else {})}
            client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            return {"status": 200, "headers": h, "sid": sid}

        def call(headers: dict, name: str, args: dict, meta: dict | None = None, sess: dict | None = None):
            """(http status, is_error, text) for one tools/call; `sess` reuses a session id, `headers` still apply."""
            s = sess or session(headers)
            if s["status"] != 200:
                return s["status"], True, s["detail"]
            h = {**s["headers"], **headers}
            params = {"name": name, "arguments": args, **({"_meta": meta} if meta else {})}
            r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params})
            if r.status_code != 200:
                return r.status_code, True, r.text
            body = r.json()
            if "error" in body:
                return 200, True, json.dumps(body["error"])
            res = body["result"]
            return 200, bool(res.get("isError")), res["content"][0]["text"] + json.dumps(res.get("structuredContent"))

        yield call, session, client


def _keyed_app(tenant: str | None = None, agent: str | None = None, registry: bool = False):
    """A hosted-style app: accounts `acme` and `zeta`, keys of each kind, data in acme/c1 and zeta/c1."""
    pytest.importorskip("fastapi")
    from keel.http import build_http_app
    from keel.keys import create_key

    c = load_cartridge(FLEET)
    if registry:
        c.agents = parse_agents(AGENTS)
    s = MemoryStore()
    keys = {
        "acme": create_key(s, "acme")[0],
        "acme_c2": create_key(s, "acme", tenant="c2")[0],
        "acme_glob": create_key(s, "acme", glob=True)[0],
        "zeta": create_key(s, "zeta")[0],
    }
    if registry:
        keys["acme_bot"] = create_key(s, "acme", agent="tires-bot")[0]
    Ingestor(c, s).ingest(_events("acme/c1"))
    s.open_fact("zeta/c1", "unit:MXK-4821", "secret_note", SECRET, valid_from=T0, source="zeta.db", evidence="ev-zeta")
    app = build_http_app(c, s, secret="op-secret", tenant=tenant, agent=agent)
    return app, s, keys


def _bearer(key: str, tenant: str | None = None, agent: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    if tenant is not None:
        h["X-Keel-Tenant"] = tenant
    if agent is not None:
        h["X-Keel-Agent"] = agent
    return h


# ---- tenant isolation: no argument reaches another tenant ---------------------------------------

def test_no_tool_argument_reaches_another_tenant():
    c, s = _fleet()
    _plant_other_tenant(s)
    srv = build_server(Tools(c, s, TENANT))
    attempts = [
        ("context", {"subject": "unit:MXK-4821", "tenant": OTHER}),  # an extra argument naming the tenant
        ("context", {"subject": "unit:MXK-4821", "detail": "full", "depth": 1}),
        ("context", {"subject": ["unit:MXK-4821", "tire:T-9001", "driver:D-666"], "detail": "full"}),
        ("context", {"subject": f"{OTHER}/unit:MXK-4821"}),
        ("context", {"subject": "__global__:x"}),
        ("context", {"subject": "unit:MXK-4821", "as_known_at": "2099-01-01T00:00:00Z"}),
        ("context", {"subject": "unit:MXK-4821", "detail": "text"}),
        ("history", {"subject": "unit:MXK-4821", "limit": 0, "detail": "full"}),
        ("history", {"changed_since": "2000-01-01T00:00:00Z", "limit": 0}),
        ("history", {"since": "2000-01-01T00:00:00Z", "limit": 0}),
        ("history", {"changed_since": "2000-01-01T00:00:00Z", "as_known_at": "2099-01-01T00:00:00Z", "limit": 0}),
        ("search", {"text": SECRET}),
        ("search", {"text": SECRET[:5].lower(), "detail": "full", "depth": 1}),
        ("search", {"text": [SECRET, "RIVAL"], "detail": "concise"}),
        ("search", {"kind": ["unit", "driver"], "detail": "full", "limit": 0}),
        ("search", {"kind": "unit", "where": {"plate": SECRET}}),
        ("search", {"text": "rival secret words", "detail": "concise"}),  # the word-ranking fallback
        ("search", {"text": "x", "related_to": "driver:D-666", "detail": "full"}),
        ("related", {"subject": "driver:D-666", "depth": 3, "limit": 0}),
        ("related", {"subject": "unit:MXK-4821", "depth": 3, "limit": 0}),
    ]
    for name, args in attempts:
        _err, text, structured = _call(srv, name, args)
        seen = _everything(text, structured)
        assert SECRET not in seen, (name, args)
        assert "D-666" not in seen or "D-666" in json.dumps(args), (name, args)  # only the key the caller gave, echoed


def test_a_write_lands_in_the_bound_tenant_whatever_the_arguments_say():
    c, s = _fleet()
    srv = build_server(Tools(c, s, TENANT))
    _call(srv, "annotate", {"subject": "unit:MXK-4821", "predicate": "p", "value": 1, "tenant": OTHER})
    _call(srv, "annotate", {"subject": f"{OTHER}/unit:MXK-4821", "predicate": "p", "value": 1})
    assert s.facts(OTHER, "unit:MXK-4821") == []
    assert [f.predicate for f in s.facts(TENANT, "unit:MXK-4821") if f.predicate == "p"] == ["p"]


@pytest.mark.parametrize("partition", ["__global__", "acme/__global__"])
def test_no_server_is_bound_to_the_global_partition(partition):
    c, s = _fleet()
    with pytest.raises(ValueError, match="reserved"):
        build_server(Tools(c, s, partition))


def test_per_request_mcp_takes_the_tenant_from_the_key_never_beyond_its_account():
    app, _, keys = _keyed_app()
    with _mcp_http(app) as (call, _, _client):
        ok = call(_bearer(keys["acme"], "c1"), "context", {"subject": "unit:MXK-4821"})
        assert ok[0] == 200 and not ok[1] and "MXK-4821" in ok[2]
        for tenant in ("zeta/c1", "../zeta/c1", "__global__", "c1/../../zeta/c1", ""):
            _, err, text = call(_bearer(keys["acme"], tenant), "context", {"subject": "unit:MXK-4821"})
            assert err and SECRET not in text, tenant
        _, err, text = call(_bearer(keys["acme_c2"], "c1"), "context", {"subject": "unit:MXK-4821"})
        assert err and "reaches only tenant 'c2'" in text
        _, err, text = call(_bearer(keys["zeta"], "c1"), "search", {"text": "MXK"})  # zeta's own c1, not acme's
        assert not err and "Freightliner" not in text
        # the operator's secret may name any tenant, but never the global partition itself
        _status, err, text = call(_bearer("op-secret", "__global__"), "context", {"subject": "unit:MXK-4821"})
        assert err and "reserved" in text


def test_a_session_id_carries_no_identity_to_another_key():
    """A session opened by acme, reused with zeta's key: every call is answered as the key of that call."""
    app, _, keys = _keyed_app()
    with _mcp_http(app) as (call, session, _client):
        acme = session(_bearer(keys["acme"], "c1"))
        _, _, text = call(_bearer(keys["zeta"], "c1"), "context", {"subject": "unit:MXK-4821", "detail": "full"}, sess=acme)
        assert "Freightliner" not in text  # acme's c1 is not reached through acme's session
        status, _err, text = call({"Authorization": "Bearer nope"}, "context", {"subject": "unit:MXK-4821"}, sess=acme)
        assert status == 401


def test_bound_mcp_ignores_the_tenant_header_and_refuses_foreign_keys():
    app, _, keys = _keyed_app(tenant="acme/c1")
    with _mcp_http(app) as (call, _, client):
        status, err, text = call(_bearer("op-secret", "zeta/c1"), "context", {"subject": "unit:MXK-4821", "detail": "full"})
        assert not err and SECRET not in text and "Freightliner" in text  # the bound tenant, not the header's
        for key in ("zeta", "acme_c2"):
            status, err, text = call(_bearer(keys[key], "c1"), "context", {"subject": "unit:MXK-4821"})
            assert status == 403 and "does not reach" in text, key
        for path in ("//mcp", "/mcp/", "/Mcp"):  # a path the binding check would not see must not reach MCP either
            r = client.post(path, headers={**_bearer(keys["zeta"]), "Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert r.status_code in (403, 404, 405, 307), path


# ---- agent identity: no argument, _meta or header changes who is calling -------------------------

def test_annotate_by_and_meta_do_not_change_the_author():
    c, s = _fleet(registry=True)
    bot = build_server(Tools(c, s, TENANT, "tires-bot"))
    _call(bot, "annotate", {"subject": "tire:T-9001", "predicate": "hunch", "value": "mine", "scope": "agent", "by": "fleet-copilot"})
    inner = _tool_fn(bot, "annotate")
    inner(subject="tire:T-9001", predicate="hunch2", value="mine", scope="agent", by="human:dispatcher",
          ctx=_Ctx({"agent": "fleet-copilot", "tenant": OTHER, "x-keel-agent": "fleet-copilot"}))
    written = [f for f in s.facts(TENANT, "tire:T-9001") if f.predicate in ("hunch", "hunch2")]
    assert [f.source for f in written] == ["annotate:tires-bot", "annotate:tires-bot"]
    copilot = build_server(Tools(c, s, TENANT, "fleet-copilot"))
    for name, args in [("context", {"subject": "tire:T-9001", "detail": "full"}), ("history", {"subject": "tire:T-9001", "scope": "agent"}),
                       ("history", {"changed_since": "2000-01-01T00:00:00Z"}), ("search", {"text": "mine"}),
                       ("context", {"subject": "tire:T-9001", "as_known_at": "2099-01-01T00:00:00Z"})]:
        _err, text, structured = _call(copilot, name, args)
        assert "hunch" not in _everything(text, structured), (name, args)


def test_the_registry_holds_over_mcp():
    c, s = _fleet(registry=True)
    bot = build_server(Tools(c, s, TENANT, "tires-bot"))
    for scope in ("tenant", "global"):
        err, text, _ = _call(bot, "annotate", {"subject": "tire:T-9001", "predicate": "p", "value": 1, "scope": scope, "evidence": "e"})
        assert err and "may not write scope" in text
    err, text, _ = _call(bot, "annotate", {"subject": "tire:T-9001", "predicate": "p", "value": 1, "scope": "global", "status": "proposed", "evidence": "e"})
    assert err and "global" in text
    for name, args in [("context", {"subject": "driver:D-118"}), ("history", {"subject": "ticket:TK-3608"}),
                       ("related", {"subject": "workshop:WS-02"}), ("search", {"kind": "driver"}),
                       ("search", {"text": "x", "related_to": "driver:D-118"}),
                       ("annotate", {"subject": "driver:D-118", "predicate": "p", "value": 1})]:
        err, text, _ = _call(bot, name, args)
        assert err and "may not read kind" in text, (name, args)
    # hidden kinds and relations are left out of what it may read
    for name, args in [("context", {"subject": "unit:MXK-4821", "detail": "full", "depth": 1}), ("related", {"subject": "unit:MXK-4821", "depth": 3, "limit": 0}),
                       ("search", {"text": "D-118", "detail": "full", "depth": 1}), ("history", {"changed_since": "2000-01-01T00:00:00Z", "limit": 0}),
                       ("search", {"text": "x", "related_to": "unit:MXK-4821", "via": "assigned_to"})]:
        err, text, structured = _call(bot, name, args)
        assert "driver:" not in _everything(text, structured) and "ticket:" not in _everything(text, structured), (name, args)


def test_an_agent_narrowed_key_cannot_switch_agent_over_mcp():
    app, s, keys = _keyed_app(registry=True)
    with _mcp_http(app) as (call, _, _client):
        _, err, text = call(_bearer(keys["acme_bot"], "c1", agent="fleet-copilot"), "context", {"subject": "unit:MXK-4821"})
        assert err and "calls as agent 'tires-bot'" in text
        _, err, text = call(_bearer(keys["acme_bot"], "c1"), "annotate",
                                 {"subject": "tire:T-9001", "predicate": "p", "value": 1, "scope": "tenant", "evidence": "e"},
                                 meta={"agent": "fleet-copilot", "x-keel-agent": "fleet-copilot"})
        assert err and "may not write scope 'tenant'" in text
        _status, err, text = call(_bearer(keys["acme"], "c1"), "context", {"subject": "unit:MXK-4821"})  # no agent: refused
        assert err and "not declared" in text
    assert [f for f in s.facts("acme/c1", "tire:T-9001") if f.predicate == "p"] == []


# ---- the global partition -------------------------------------------------------------------------

def test_only_a_global_key_writes_global_memory_over_per_request_mcp():
    app, s, keys = _keyed_app()
    args = {"subject": "unit:MXK-4821", "predicate": "poison", "value": "every tenant reads this", "scope": "global", "evidence": "e"}
    with _mcp_http(app) as (call, _, client):
        r = client.post("/v1/annotate", json=args, headers=_bearer(keys["acme"], "c1"))  # /v1 refuses a plain key
        assert r.status_code == 403 and "global key" in r.json()["detail"]
        _, err, text = call(_bearer(keys["acme_glob"], "c1"), "annotate", {**args, "predicate": "allowed"})
        assert not err, text  # the global key may
        call(_bearer(keys["acme"], "c1"), "annotate", args)
    assert [f.predicate for f in s.facts("acme/__global__", "unit:MXK-4821")] == ["allowed"]


def test_only_a_global_key_writes_global_memory_over_bound_mcp():
    app, s, keys = _keyed_app(tenant="acme/c1")
    with _mcp_http(app) as (call, _, _client):
        call(_bearer(keys["acme"]), "annotate",
             {"subject": "unit:MXK-4821", "predicate": "poison", "value": 1, "scope": "global", "evidence": "e"})
    assert s.facts("acme/__global__", "unit:MXK-4821") == []


def test_global_memory_is_written_by_the_privileged_process_and_read_by_every_tenant():
    c, s = _fleet()
    _plant_other_tenant(s)
    op = build_server(Tools(c, s, TENANT))
    err, text, _ = _call(op, "annotate", {"subject": "unit:MXK-4821", "predicate": "recall", "value": "brakes", "scope": "global"})
    assert err and "evidence" in text  # global needs evidence
    _call(op, "annotate", {"subject": "unit:MXK-4821", "predicate": "recall", "value": "brakes", "scope": "global", "evidence": "bulletin-7"})
    other = build_server(Tools(c, s, OTHER))
    err, text, _ = _call(other, "context", {"subject": "unit:MXK-4821"})
    assert "brakes" in text  # global is shared on purpose, and nothing else of TENANT's comes with it
    assert "Freightliner" not in text and "last_fuel_load" not in text


# ---- leaks through errors, instructions, structuredContent, detail="text" ----------------------

def test_the_did_you_mean_hint_does_not_reveal_hidden_kinds():
    """A bare id is probed only against kinds the agent reads: 'D-118' must not answer 'did you mean driver:D-118?'."""
    c, s = _fleet(registry=True)
    bot = build_server(Tools(c, s, TENANT, "tires-bot"))
    err, text, _ = _call(bot, "context", {"subject": "T-9001"})
    assert err and "tire:T-9001" in text  # a kind it reads: the hint helps
    err, text, _ = _call(bot, "context", {"subject": "D-118"})
    assert err and "driver" not in text


def test_instructions_name_only_what_the_agent_may_read():
    c, s = _fleet(registry=True)
    text = build_server(Tools(c, s, TENANT, "tires-bot")).instructions
    assert "driver" not in text and "ticket" not in text and "workshop" not in text


def test_refusals_carry_no_data():
    c, s = _fleet(registry=True)
    bot = build_server(Tools(c, s, TENANT, "tires-bot"))
    err, text, _ = _call(bot, "context", {"subject": "driver:D-118", "detail": "text"})
    assert err and "D-118" not in text.replace("driver:D-118", "") and "license" not in text.lower()
    err, text, _ = _call(bot, "context", {"subject": "ticket:TK-3608"})
    assert err and "urgent" not in text and "approved" not in text


def test_structured_content_and_text_carry_the_same_answer():
    c, s = _fleet()
    srv = build_server(Tools(c, s, TENANT, agent="a1"))
    _call(srv, "annotate", {"subject": "unit:MXK-4821", "predicate": "hunch", "value": "a1 only", "scope": "agent"})
    for name, args in [("context", {"subject": "unit:MXK-4821"}), ("history", {"subject": "unit:MXK-4821"}),
                       ("search", {"text": "MXK"}), ("related", {"subject": "unit:MXK-4821"})]:
        _, text, structured = _call(srv, name, args)
        data = json.loads(text)
        assert structured.get("result", structured) == data, name
    other = build_server(Tools(c, s, TENANT, agent="a2"))
    for name, args in [("context", {"subject": "unit:MXK-4821", "detail": "text"}), ("history", {"subject": "unit:MXK-4821", "detail": "text"})]:
        _, text, structured = _call(other, name, args)
        assert "a1 only" not in _everything(text, structured), name
    nobody = build_server(Tools(c, s, TENANT))  # bound to no agent: nobody's agent memory
    _err, text, structured = _call(nobody, "history", {"subject": "unit:MXK-4821", "detail": "text"})
    assert "a1 only" not in _everything(text, structured)


# ---- readOnlyHint: the four read tools never write ----------------------------------------------

def test_read_only_tools_do_not_write(tmp_path: Path):
    db = tmp_path / "k.db"
    c, s = _fleet(SqliteStore(db))
    tools = Tools(c, s, TENANT, agent="a1")
    tools.annotate("unit:MXK-4821", "hunch", "x", scope="agent")
    srv = build_server(tools)
    marks = {t.name: getattr(t.annotations, "read_only_hint", None) if hasattr(t.annotations, "read_only_hint") else t.annotations.readOnlyHint
             for t in srv._tool_manager.list_tools()}
    assert marks == {"context": True, "history": True, "search": True, "related": True, "annotate": False}

    def dump() -> str:
        con = sqlite3.connect(db)
        try:
            return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
        finally:
            con.close()

    before = dump()
    for name, args in [("context", {"subject": "unit:MXK-4821", "depth": 1, "detail": "full"}), ("context", {"subject": "MXK-4821"}),
                       ("context", {"subject": ["unit:MXK-4821", "tire:T-9001"], "as_known_at": "2026-10-11T00:00:00Z", "task": "t"}),
                       ("history", {"subject": "unit:MXK-4821", "detail": "text"}), ("history", {"changed_since": "2000-01-01T00:00:00Z"}),
                       ("history", {"since": "2000-01-01", "as_known_at": "2026-10-11"}),
                       ("search", {"text": "MXK", "detail": "full", "depth": 1}), ("search", {"kind": "unit", "rule": "unit_grounded"}),
                       ("search", {"text": "nothing holds these words"}), ("search", {"text": "x", "mode": "semantic"}),
                       ("related", {"subject": "unit:MXK-4821", "depth": 3})]:
        _call(srv, name, args)
    assert dump() == before


# ---- exhaustion: caps hold whatever the caller asks ----------------------------------------------

def _crowded(max_list: int):
    """One unit with 30 tires mounted, 30 facts on it, under a server whose ceiling is `max_list`."""
    c, s = _fleet()
    for i in range(30):
        s.upsert_entity(TENANT, "tire", f"X-{i:02}", position="P1")
        s.link(TENANT, f"tire:X-{i:02}", "mounted_on", "unit:MXK-4821", valid_from=T0)
        s.open_fact(TENANT, "unit:MXK-4821", f"p{i:02}", "abc", valid_from=T0, source="t", evidence=f"e{i}")
    return build_server(Tools(c, s, TENANT, max_list=max_list))


def _entries(text: str) -> list:
    return [x for x in json.loads(text) if not (isinstance(x, dict) and ("more" in x or "note" in x))]


def test_the_ceiling_holds_for_all_and_for_too_many():
    srv = _crowded(5)
    for name, args in [("history", {"subject": "unit:MXK-4821", "limit": 0}), ("history", {"subject": "unit:MXK-4821", "limit": 10**9}),
                       ("related", {"subject": "unit:MXK-4821", "limit": 0}), ("related", {"subject": "unit:MXK-4821", "limit": 10**9}),
                       ("search", {"kind": "tire", "limit": 0}), ("search", {"text": "X-", "limit": 10**9}),
                       ("search", {"text": "abc"})]:
        err, text, _ = _call(srv, name, args)
        assert not err and len(_entries(text)) <= 5, (name, args)


@pytest.mark.parametrize("name,args", [
    ("history", {"subject": "unit:MXK-4821", "limit": -1}),
    ("related", {"subject": "unit:MXK-4821", "limit": -1}),
    ("search", {"text": "X-", "limit": -1}),
])
def test_a_negative_limit_does_not_bypass_the_ceiling(name, args):
    err, text, _ = _call(_crowded(5), name, args)
    assert err or len(_entries(text)) <= 5


def test_per_kind_does_not_bypass_the_ceiling():
    err, text, _ = _call(_crowded(5), "search", {"text": "X-", "kind": ["tire", "unit"], "per_kind": 10**6})
    assert err or len(_entries(text)) <= 5


def test_a_giant_list_of_subjects_is_bounded():
    srv = _crowded(5)
    err, text, _ = _call(srv, "context", {"subject": ["unit:MXK-4821"] * 2000})
    assert err or len(json.loads(text)) <= 5
    err, text, _ = _call(srv, "search", {"text": ["X-"] * 2000})
    assert err or len(json.loads(text)) <= 5


def test_annotate_bounds_the_size_of_a_value():
    c, s = _fleet()
    srv = build_server(Tools(c, s, TENANT))
    err, _, _ = _call(srv, "annotate", {"subject": "unit:MXK-4821", "predicate": "blob", "value": "x" * 2_000_000})
    assert err


def test_depth_is_bounded_whatever_the_caller_asks():
    srv = _crowded(5)
    for name, args, message in [("related", {"subject": "unit:MXK-4821", "depth": 4}, "1, 2 or 3"),
                                ("related", {"subject": "unit:MXK-4821", "depth": 10**9}, "1, 2 or 3"),
                                ("search", {"text": "X-", "depth": 2}, "0 or 1")]:
        err, text, _ = _call(srv, name, args)
        assert err and message in text, (name, args)
    err, text, _ = _call(srv, "context", {"subject": "unit:MXK-4821", "depth": 10**18})  # any depth >= 1 is one hop, capped
    assert not err and len(json.loads(text)["neighbors"]) <= 50


# ---- shared state on a bound server --------------------------------------------------------------

def test_concurrent_annotates_keep_their_own_trace():
    c, s = _fleet()
    tools = Tools(c, s, TENANT)
    srv = build_server(tools)
    inner = _tool_fn(srv, "annotate")
    a_inside, b_done = threading.Event(), threading.Event()
    real = s.forgotten

    def forgotten(tenant):  # session A pauses inside annotate, after its trace was set, until B has written
        if threading.current_thread().name == "A":
            a_inside.set()
            b_done.wait(5)
        return real(tenant)

    s.forgotten = forgotten

    def run(name: str, trace: str) -> None:
        inner(subject="unit:MXK-4821", predicate=f"from_{name}", value=1, ctx=_Ctx({"traceparent": trace}))

    a = threading.Thread(target=run, args=("A", "trace-A"), name="A")
    a.start()
    a_inside.wait(5)
    b = threading.Thread(target=run, args=("B", "trace-B"), name="B")
    b.start()
    b.join(5)
    b_done.set()
    a.join(5)
    traces = {f.predicate: f.trace for f in s.facts(TENANT, "unit:MXK-4821") if f.predicate.startswith("from_")}
    assert traces == {"from_A": "trace-A", "from_B": "trace-B"}
