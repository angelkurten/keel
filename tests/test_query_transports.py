"""`query` over every transport: off unless enabled (MCP lists five tools, /v1/query answers 404), on with
`build_server(query=True)`, `build_http_app(query=True)` or `keel serve --query`; the Python client calls it.
Over MCP its description is fixed text (prompt caches hold) and the generated schema rides in the instructions."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_query import TENANT, T, cartridge, populate

from keel import MemoryStore, SqliteStore, Tools
from keel.tools import DESCRIPTIONS

pytest.importorskip("mcp")


def _store(tmp_path):
    c = cartridge()
    return c, populate(SqliteStore(tmp_path / "k.db"), c)


def _names(srv) -> list[str]:
    return [t.name for t in asyncio.run(srv.list_tools())]


def test_mcp_lists_query_only_when_enabled_and_carries_the_schema_in_its_instructions(tmp_path):
    from keel.mcp_server import build_server

    c, store = _store(tmp_path)
    off = build_server(Tools(c, store, TENANT))
    assert _names(off) == ["context", "history", "search", "annotate", "related"]
    assert "tire_state_at" not in (off.instructions or "")
    on = build_server(Tools(c, store, TENANT), query=True)
    assert _names(on)[-1] == "query"
    tool = next(t for t in asyncio.run(on.list_tools()) if t.name == "query")
    assert tool.description == DESCRIPTIONS["query"]
    assert tool.model_dump(by_alias=True)["annotations"]["readOnlyHint"] is True
    schema = tool.model_dump(by_alias=True)["inputSchema"]
    assert set(schema["properties"]) == {"sql", "limit"} and schema["required"] == ["sql"]
    assert "tire_state_at" in on.instructions and TENANT in on.instructions
    res = asyncio.run(on.call_tool("query", {"sql": f"SELECT id, depth_mm FROM tire_state_at('{TENANT}', '{T}') WHERE id = 'T-9001'"}))
    assert "T-9001" in res.content[0].text


def test_mcp_does_not_list_query_on_a_store_without_sql_or_for_an_agent_without_the_flag(tmp_path):
    from keel.mcp_server import build_server

    c = cartridge()
    assert "query" not in _names(build_server(Tools(c, MemoryStore(), TENANT), query=True))
    ca = cartridge(agents={"bot": {"writes": ["task"]}})
    assert "query" not in _names(build_server(Tools(ca, SqliteStore(tmp_path / "a.db"), TENANT, "bot"), query=True))


def test_a_read_only_server_still_offers_query(tmp_path):
    from keel.mcp_server import build_server

    c, store = _store(tmp_path)
    assert _names(build_server(Tools(c, store, TENANT), query=True, read_only=True)) == ["context", "history", "search", "related", "query"]


def test_http_query_is_404_unless_enabled(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    c, store = _store(tmp_path)
    auth = {"Authorization": "Bearer s", "X-Keel-Tenant": TENANT}
    sql = {"sql": f"SELECT subject AS id FROM state_at('{TENANT}', '{T}') ORDER BY subject", "limit": 1}
    off = TestClient(build_http_app(c, store, secret="s"))
    r = off.post("/v1/query", json=sql, headers=auth)
    assert r.status_code == 404 and "--query" in r.json()["detail"]
    assert "/v1/query" not in off.get("/openapi.json").json()["paths"]
    on = TestClient(build_http_app(c, store, secret="s", query=True))
    r = on.post("/v1/query", json=sql, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "more" in body[0] and body[1] == {"id": body[1]["id"]}
    assert on.post("/v1/query", json={"sql": "SELECT * FROM api_keys"}, headers=auth).status_code == 400
    assert on.post("/v1/query", json=sql, headers={"X-Keel-Tenant": TENANT}).status_code == 401
    assert TestClient(build_http_app(c, store, secret="s", query=True, read_only=True)).post("/v1/query", json=sql, headers=auth).status_code == 200
    assert TestClient(build_http_app(c, MemoryStore(), secret="s", query=True)).post("/v1/query", json=sql, headers=auth).status_code == 400


def test_http_query_refuses_a_registry_agent_without_the_flag(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    c = cartridge(agents={"bot": {"writes": ["task"]}, "analyst": {"query": True}})
    store = populate(SqliteStore(tmp_path / "k.db"), cartridge())
    client = TestClient(build_http_app(c, store, secret="s", query=True))
    head = {"Authorization": "Bearer s", "X-Keel-Tenant": TENANT}
    assert client.post("/v1/query", json={"sql": "SELECT 1 AS one"}, headers={**head, "X-Keel-Agent": "bot"}).status_code == 403
    assert client.post("/v1/query", json={"sql": "SELECT 1 AS one"}, headers={**head, "X-Keel-Agent": "analyst"}).json() == [{"one": 1}]


def test_the_python_client_queries():
    httpx = pytest.importorskip("httpx")
    from keel.client import Client

    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[{"one": 1}])

    keel = Client("https://keel.example", TENANT, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert keel.query("SELECT 1 AS one", limit=5) == [{"one": 1}]
    assert seen == [("/v1/query", {"sql": "SELECT 1 AS one", "limit": 5})]


def test_serve_takes_query_flags():
    from typer.testing import CliRunner

    from keel.cli import app

    out = CliRunner().invoke(app, ["serve", "--help"], env={"COLUMNS": "200"}).output
    assert "--query" in out and "--query-timeout" in out
