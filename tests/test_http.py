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

FLEET, EVENTS, TENANT = "cartridges/fleet", "examples/events/fleet.jsonl", "acme-logistics"
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
    res = client.post("/v1/annotate", json={"subject": "tire:T-9001", "predicate": "assessment", "value": "critical"}, headers=h)
    assert res.status_code == 200 and res.json()["source"] == "annotate:tires-bot"
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
