"""HTTP transport: the /v1 API for many tenants, ingest, refusals as 403, and MCP over HTTP."""

import inspect
import json
import socket
import threading
import time

import pytest

from keel import Ingestor, MemoryStore, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter

FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"
AUTH = {"Authorization": "Bearer s3cret"}


def _client(cartridge, store, **kw):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    return TestClient(build_http_app(cartridge, store, secret="s3cret", **kw))


def test_secret_tenant_header_and_ingest():
    c = load_cartridge(FLEET)
    client = _client(c, MemoryStore())
    assert client.get("/health").status_code == 200  # open
    assert client.post("/v1/context", json={"subject": "unit:MXK-4821"}).status_code == 401  # no secret
    assert client.post("/v1/context", json={"subject": "unit:MXK-4821"}, headers=AUTH).status_code == 400  # no tenant
    from pathlib import Path

    lines = [json.loads(line) for line in Path(EVENTS).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    res = client.post("/v1/events", json=lines, headers={**AUTH, "X-Keel-Tenant": "someone-else"})
    assert res.status_code == 400  # the header, when given, must match every envelope
    res = client.post("/v1/events", json=lines, headers={**AUTH, "X-Keel-Tenant": TENANT})
    assert res.status_code == 200 and res.json()["processed"] == 12
    retry = client.post("/v1/events", json=lines, headers={**AUTH, "X-Keel-Tenant": TENANT}).json()  # a client retry
    assert (retry["processed"], retry["duplicates"]) == (0, 12)
    h = {**AUTH, "X-Keel-Tenant": TENANT}
    ctx = client.post("/v1/context", json={"subject": "tire:T-9001"}, headers=h).json()
    assert ctx["evaluation"] == {"tire_critical": True}
    past = client.post("/v1/context", json={"subject": "tire:T-9001", "as_of": "2026-09-20"}, headers=h).json()
    assert past["evaluation"] == {"tire_critical": False}
    depths = client.post("/v1/history", json={"subject": "tire:T-9001", "predicate": "depth_mm"}, headers=h).json()
    assert [f["value"] for f in depths] == [5.2, 2.6]
    assert client.post("/v1/search", json={"text": "T-9001"}, headers=h).json()
    rel = client.post("/v1/related", json={"subject": "unit:MXK-4821", "relation": "mounted_on"}, headers=h).json()
    assert rel[0]["subject"] == "tire:T-9001"
    assert client.post("/v1/related", json={"subject": "unit:MXK-4821", "relation": "mounted_on", "as_of": "2026-09-05"}, headers=h).json() == []
    deep = client.post("/v1/context", json={"subject": "unit:MXK-4821", "depth": 1}, headers=h).json()
    assert any(n["subject"] == "tire:T-9001" and n["evaluation"] == {"tire_critical": True} for n in deep["neighbors"])
    assert client.post("/v1/context", json={"subject": "tire:T-9001"}, headers={**AUTH, "X-Keel-Tenant": "other"}).json()["entity"] is None
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/v1/events", "/v1/context", "/v1/history", "/v1/search", "/v1/annotate", "/v1/related"} <= paths


def test_registry_and_policy_refusals_are_403():
    c = load_cartridge(FLEET)
    c.agents = parse_agents({"tires-bot": {"writes": ["task"], "reads": {"kinds": ["unit", "tire"], "relations": ["mounted_on"]}}})
    store = MemoryStore()
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS))
    client = _client(c, store)
    h = {**AUTH, "X-Keel-Tenant": TENANT}
    assert client.post("/v1/context", json={"subject": "tire:T-9001"}, headers=h).status_code == 403  # no agent, registry exists
    h["X-Keel-Agent"] = "tires-bot"
    assert client.post("/v1/context", json={"subject": "ticket:TK-3608"}, headers=h).status_code == 403
    res = client.post("/v1/annotate", json={"subject": "tire:T-9001", "predicate": "assessment", "value": "critical", "scope": "tenant", "evidence": "e"}, headers=h)
    assert res.status_code == 403 and "may not write scope 'tenant'" in res.json()["detail"]
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    res = client.post("/v1/annotate", json={"subject": "tire:T-9001", "predicate": "assessment", "value": "critical"}, headers={**h, "traceparent": tp, "X-Keel-Tool-Call": "call_9"})
    assert res.status_code == 200 and res.json()["source"] == "annotate:tires-bot"
    assert res.json()["trace"] == f"{tp}; tool_call_id=call_9"  # joins the caller's trace to the audit export
    short = client.post("/v1/context", json={"subject": "tire:T-9001", "detail": "concise"}, headers=h).json()
    assert short["facts"]["depth_mm"] == 2.6 and "rules" not in short
    assert client.post("/v1/annotate", json={"subject": "tire:T-9001", "predicate": "x", "value": 1, "scope": "nope"}, headers=h).status_code == 400


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_over_http_for_one_tenant():
    pytest.importorskip("mcp")
    uvicorn = pytest.importorskip("uvicorn")
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client import streamable_http as sh

    from keel.http import build_http_app

    c = load_cartridge(FLEET)
    store = MemoryStore()
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_http_app(c, store, secret="s3cret", tenant=TENANT), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    connect = getattr(sh, "streamable_http_client", None) or sh.streamablehttp_client  # mcp 2.x / 1.x
    url = f"http://127.0.0.1:{port}/mcp"

    async def run():
        if "http_client" in inspect.signature(connect).parameters:
            cm = connect(url, http_client=httpx.AsyncClient(headers=AUTH))
        else:
            cm = connect(url, headers=AUTH)
        async with cm as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == {"context", "history", "search", "annotate", "related"}
            res = await session.call_tool("context", {"subject": "tire:T-9001"})
            assert json.loads(res.content[0].text)["evaluation"] == {"tire_critical": True}

    try:
        anyio.run(run)
        assert httpx.post(url, json={}).status_code == 401  # the secret guards /mcp too
    finally:
        server.should_exit = True


def test_serve_refuses_an_open_api_on_a_network_host(monkeypatch):
    from typer.testing import CliRunner

    from keel.cli import _loopback, app

    monkeypatch.delenv("KEEL_SECRET", raising=False)
    res = CliRunner().invoke(app, ["serve", "examples/fleet", "--transport", "http", "--host", "0.0.0.0"], env={"COLUMNS": "400"})
    assert res.exit_code != 0 and "reachable from the network" in res.output
    assert _loopback("127.0.0.1") and _loopback("::1") and _loopback("localhost") and not _loopback("0.0.0.0") and not _loopback("keel.internal")


def test_the_task_context_parameters_over_http():
    c, s = load_cartridge(FLEET), MemoryStore()
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    client, h = _client(c, s), {**AUTH, "X-Keel-Tenant": TENANT}
    both = client.post("/v1/context", json={"subject": ["tire:T-9001", "unit:MXK-4821"], "exclude": ["depth_mm"]}, headers=h).json()
    assert [x["subject"] for x in both] == ["tire:T-9001", "unit:MXK-4821"] and both[0]["facts"] == []
    [tire] = client.post("/v1/search", json={"text": "T-9001", "detail": "concise"}, headers=h).json()
    assert tire["facts"] == {"depth_mm": 2.6}
    lists = client.post("/v1/search", json={"text": ["T-9001", "MXK"], "per_kind": 1}, headers=h).json()
    assert len(lists) == 2 and all(isinstance(x, list) for x in lists)
    both_kinds = client.post("/v1/search", json={"kind": ["unit", "tire"], "where": {"depth_mm": 2.6}}, headers=h).json()
    assert [x["key"] for x in both_kinds] == ["tire:T-9001"]
    [unit] = client.post("/v1/search", json={"kind": "unit", "depth": 1, "as_of": "2026-09-05"}, headers=h).json()
    assert "tire:T-9001" not in {n["subject"] for n in unit["neighbors"]}  # mounted on 09-10
    deep = client.post("/v1/related", json={"subject": "tire:T-9001", "relation": ["mounted_on", "assigned_to"], "depth": 2}, headers=h).json()
    assert {(e["subject"], e["hops"]) for e in deep} >= {("unit:MXK-4821", 1), ("driver:D-118", 2)}


def test_every_transport_takes_every_parameter_of_the_five_tools():
    """A parameter added to a tool must reach the Python client, the HTTP API and MCP."""
    pytest.importorskip("fastapi")
    import asyncio

    from keel import Tools
    from keel.client import Client

    c = load_cartridge(FLEET)
    tools = Tools(c, MemoryStore(), TENANT)
    names = ["context", "history", "search", "annotate", "related"]
    own = {n: [p for p in inspect.signature(getattr(Tools, n)).parameters if p != "self"] for n in names}
    for n in names:
        assert [p for p in inspect.signature(getattr(Client, n)).parameters if p != "self"] == own[n], n
    schema = _client(c, MemoryStore()).get("/openapi.json").json()
    for n in names:
        ref = schema["paths"][f"/v1/{n}"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
        assert list(schema["components"]["schemas"][ref]["properties"]) == own[n], n
    pytest.importorskip("mcp")
    from keel.mcp_server import build_server

    listed = {t.name: list((getattr(t, "input_schema", None) or t.inputSchema)["properties"]) for t in asyncio.run(build_server(tools).list_tools())}  # mcp 2.x / 1.x
    for n in names:
        assert listed[n] == own[n], n


def test_ui_is_opt_in_open_and_carries_no_data():
    c = load_cartridge(FLEET)
    assert _client(c, MemoryStore()).get("/ui").status_code == 404  # off unless asked
    page = _client(c, MemoryStore(), ui=True).get("/ui")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'fetch("v1/" + tool' in page.text  # the page reads through the same /v1 API, with the secret


def test_a_rate_limited_embedder_is_503_with_retry_after_and_keys_list_cheaply():
    from keel.semantic import MemoryIndex, Unavailable

    class Limited:
        def embed(self, texts):
            raise Unavailable("embeddings 429: slow down")

    c, store = load_cartridge(FLEET), MemoryStore()
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    client = _client(c, store, embedder=Limited(), index=MemoryIndex())
    h = {**AUTH, "X-Keel-Tenant": TENANT}
    res = client.post("/v1/search", json={"text": "worn tire", "mode": "semantic"}, headers=h)
    assert res.status_code == 503 and res.headers["retry-after"] == "5", res.text
    keys = client.post("/v1/search", json={"kind": "tire", "detail": "key"}, headers=h).json()
    assert keys and all(isinstance(k, str) and k.startswith("tire:") for k in keys)
    assert client.post("/v1/search", json={"kind": "tire", "detail": "key", "depth": 1}, headers=h).status_code == 400


def test_api_keys_confine_an_account():
    from keel.keys import create_key

    c = load_cartridge(FLEET)
    store = MemoryStore()
    acme, _ = create_key(store, "acme")
    narrow, _ = create_key(store, "acme", tenant="c1")
    admin, _ = create_key(store, "acme", glob=True)
    zeta, _ = create_key(store, "zeta")
    client = _client(c, store)
    bearer = lambda k, t=None: {"Authorization": f"Bearer {k}", **({"X-Keel-Tenant": t} if t else {})}
    from pathlib import Path

    lines = [json.loads(line) for line in Path(EVENTS).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    for e in lines:
        e["tenant"] = "c1"
    assert client.post("/v1/events", json=lines, headers=bearer(acme)).json()["processed"] == 12
    assert store.tenants() == ["acme/c1"]  # the server prefixed the account
    tire = {"subject": "tire:T-9001"}
    assert client.post("/v1/context", json=tire, headers=bearer(acme, "c1")).json()["evaluation"] == {"tire_critical": True}
    assert client.post("/v1/context", json=tire, headers=bearer(narrow)).json()["entity"] is not None  # its tenant is fixed
    assert client.post("/v1/context", json=tire, headers=bearer(narrow, "c2")).status_code == 403
    assert client.post("/v1/context", json=tire, headers=bearer(zeta, "c1")).json()["entity"] is None  # zeta/c1, empty
    assert client.post("/v1/context", json=tire, headers=bearer(acme, "zeta/c1")).status_code == 400  # no choosing the prefix
    assert client.post("/v1/events", json=lines, headers=bearer(narrow, "c1")).json()["duplicates"] == 12
    for e in lines:
        e["tenant"] = "c2"
    assert client.post("/v1/events", json=lines, headers=bearer(narrow)).status_code == 403
    assert client.post("/v1/context", json=tire, headers={**AUTH, "X-Keel-Tenant": "acme/c1"}).json()["entity"] is not None  # the operator
    assert client.post("/v1/context", json=tire, headers=bearer("kk_nope_x", "c1")).status_code == 401
    note = {**tire, "predicate": "glossary", "value": "a tire under 3 mm is critical", "scope": "global", "evidence": "manual"}
    assert client.post("/v1/annotate", json=note, headers=bearer(acme, "c1")).status_code == 403
    assert client.post("/v1/annotate", json=note, headers=bearer(admin, "c1")).status_code == 200
    assert [f.value for f in store.facts("acme/__global__", "tire:T-9001")] == ["a tire under 3 mm is critical"]
    store.upsert_entity("zeta/c1", "tire", "T-9001")
    seen = lambda k: [f["value"] for f in client.post("/v1/history", json={**tire, "predicate": "glossary"}, headers=bearer(k, "c1")).json()]
    assert seen(acme) == ["a tire under 3 mm is critical"] and seen(zeta) == []  # global memory stays in its account


def test_first_key_closes_an_open_server():
    from keel.http import _Auth
    from keel.keys import create_key

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    store = MemoryStore()
    client = TestClient(build_http_app(load_cartridge(FLEET), store))  # no secret: open
    h = {"X-Keel-Tenant": TENANT}
    assert client.post("/v1/context", json={"subject": "unit:X"}, headers=h).status_code == 200
    full, key = create_key(store, "acme")
    client = TestClient(build_http_app(load_cartridge(FLEET), store))
    assert client.post("/v1/context", json={"subject": "unit:X"}, headers=h).status_code == 401
    assert client.post("/v1/context", json={"subject": "unit:X"}, headers={**h, "Authorization": f"Bearer {full}"}).status_code == 200
    # /mcp is bound to one tenant: a key reaches it only when that tenant is its account's
    auth = lambda t, a=None: _Auth(None, None, store, t, a)
    assert auth("acme/c1")._binds(key) and not auth("zeta/c1")._binds(key) and not auth("c1")._binds(key)
    _, bot = create_key(store, "acme", tenant="c1", agent="bot")
    assert auth("acme/c1", "bot")._binds(bot) and not auth("acme/c2", "bot")._binds(bot) and not auth("acme/c1")._binds(bot)


def test_tool_calls_are_metered_per_tenant():
    from keel.keys import create_key
    from keel.usage import report

    store = MemoryStore()
    acme, _ = create_key(store, "acme")
    with _client(load_cartridge(FLEET), store) as client:  # the context manager runs the lifespan: flush on exit
        h = {"Authorization": f"Bearer {acme}", "X-Keel-Tenant": "c1"}
        for _ in range(3):
            client.post("/v1/search", json={"text": "T-9001"}, headers=h)
        client.post("/v1/context", json={"subject": "tire:T-9001"}, headers=h)
    (row,) = report(store, account="acme")
    assert (row["tenant"], row["events"], row["calls"]) == ("acme/c1", 0, {"search": 3, "context": 1})


def test_quotas_per_account_answer_429():
    from keel.accounts import set_account
    from keel.keys import create_key

    store = MemoryStore()
    acme, _ = create_key(store, "acme")
    free, _ = create_key(store, "free")
    set_account(store, "acme", rps=2, events_month=15)
    client = _client(load_cartridge(FLEET), store)
    h = lambda k: {"Authorization": f"Bearer {k}", "X-Keel-Tenant": "c1"}
    codes = [client.post("/v1/search", json={"text": "x"}, headers=h(acme)).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]
    limited = client.post("/v1/search", json={"text": "x"}, headers=h(acme))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert all(client.post("/v1/search", json={"text": "x"}, headers=h(free)).status_code == 200 for _ in range(5))  # no row: no limit
    from pathlib import Path

    lines = [json.loads(x) for x in Path(EVENTS).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    for e in lines:
        e["tenant"] = "c1"
    time.sleep(1.1)
    assert client.post("/v1/events", json=lines, headers=h(acme)).status_code == 200  # 12 of 15
    time.sleep(1.1)
    over = client.post("/v1/events", json=lines[:4], headers=h(acme))
    assert over.status_code == 429 and int(over.headers["Retry-After"]) > 0 and "this month" in over.json()["detail"]


def test_an_account_can_have_its_own_database(tmp_path):
    from keel.accounts import set_account
    from keel.keys import create_key
    from keel.store.sqlite import SqliteStore
    from keel.usage import report

    shared = SqliteStore(tmp_path / "shared.db")
    big, _ = create_key(shared, "big")
    set_account(shared, "big", store=str(tmp_path / "big.db"))
    client = _client(load_cartridge(FLEET), shared)
    from pathlib import Path

    lines = [json.loads(x) for x in Path(EVENTS).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    for e in lines:
        e["tenant"] = "c1"
    h = {"Authorization": f"Bearer {big}", "X-Keel-Tenant": "c1"}
    assert client.post("/v1/events", json=lines, headers=h).json()["processed"] == 12
    assert shared.tenants() == [] and SqliteStore(tmp_path / "big.db").tenants() == ["big/c1"]  # its data, its database
    assert client.post("/v1/context", json={"subject": "tire:T-9001"}, headers=h).json()["evaluation"] == {"tire_critical": True}
    assert report(shared, account="big")[0]["events"] == 12  # usage stays where keel usage reads it
