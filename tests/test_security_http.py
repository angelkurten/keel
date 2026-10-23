"""Adversarial tests of the HTTP API's isolation (keel/http.py, keel/keys.py, keel/accounts.py).

Each test is an attack. A passing test is one the server blocks; an `xfail(strict=True)` marked
SECURITY is a hole that is still open, with what goes wrong in its reason. Data lives in account
`zeta`; account `acme` is the attacker unless a test says otherwise.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from keel import Ingestor, MemoryStore, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter
from keel.keys import create_key, revoke_key

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from keel.http import build_http_app

FLEET, EVENTS = "examples/fleet", "examples/fleet/events.jsonl"
SECRET = "s3cret"
OPERATOR = {"Authorization": f"Bearer {SECRET}"}
TIRE = {"subject": "tire:T-9001"}
NOTE = {**TIRE, "predicate": "glossary", "value": "poisoned", "scope": "global", "evidence": "manual"}


def _lines(tenant: str) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in Path(EVENTS).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    return [{**r, "tenant": tenant} for r in rows]


def _bearer(key: str, tenant: str | None = None, agent: str | None = None) -> dict[str, Any]:
    """Header values as UTF-8 bytes, so look-alike tenants and agents reach the server as sent."""
    return {"Authorization": f"Bearer {key}", **({"X-Keel-Tenant": tenant.encode()} if tenant is not None else {}),
            **({"X-Keel-Agent": agent.encode()} if agent is not None else {})}


@pytest.fixture
def world():
    """zeta/c1 holds the fleet data; acme has a full key, a key narrowed to c1, and a global key."""
    c = load_cartridge(FLEET)
    store = MemoryStore()
    keys = {"acme": create_key(store, "acme")[0], "narrow": create_key(store, "acme", tenant="c1")[0],
            "admin": create_key(store, "acme", glob=True)[0], "zeta": create_key(store, "zeta")[0]}
    Ingestor(c, store).ingest([e.model_copy(update={"tenant": "zeta/c1"}) for e in JsonlAdapter(EVENTS)])
    return c, store, keys


def _client(c, store, **kw) -> TestClient:
    return TestClient(build_http_app(c, store, secret=SECRET, host="0.0.0.0", **kw))  # 0.0.0.0: TestClient's Host passes MCP


def _raw(app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]] = (), body: bytes = b"") -> tuple[int | None, bytes]:
    """One request straight into the ASGI app, with the path exactly as given (no client-side normalization).
    An exception escaping the app is (None, its name): what a server would turn into a 500."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode("utf-8"), "query_string": b"", "root_path": "", "headers": list(headers),
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    sent: list[dict[str, Any]] = []
    first = True

    async def receive() -> dict[str, Any]:
        nonlocal first
        if not first:
            await asyncio.sleep(3600)
        first = False
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m: dict[str, Any]) -> None:
        sent.append(m)

    try:
        asyncio.run(asyncio.wait_for(app(scope, receive, send), 5))
    except Exception as exc:  # noqa: BLE001 - the point is to see it
        return None, type(exc).__name__.encode()
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _mcp(client: TestClient, headers: dict[str, str], tool: str, args: dict[str, Any]) -> tuple[Any, str]:
    """A tools/call over streamable HTTP (json responses). Returns (isError, text), or (status, "") when refused."""
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **headers}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "attacker", "version": "1"}}}
    r = client.post("/mcp", json=init, headers=h)
    if r.status_code != 200:
        return r.status_code, ""
    h["mcp-session-id"] = r.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}, headers=h)
    if r.status_code != 200:
        return r.status_code, ""
    res = r.json()["result"]
    return res.get("isError", False), res["content"][0]["text"]


# --- tenant and account isolation over /v1 ---------------------------------------------------------------------


@pytest.mark.parametrize("tenant", [
    "zeta/c1", "/c1", "c1/", "../zeta/c1", "zeta%2Fc1", "..", ".", "zeta", "__global__", "zeta/__global__", "acme/__global__",
    "C1", "c１", "ｃ1", "c1\u200b", "cı", "__GLOBAL__", "＿＿global＿＿",
])
def test_tenant_header_tricks_never_reach_another_account(world, tenant):
    c, store, keys = world
    client = _client(c, store)
    for path, body in (("/v1/context", TIRE), ("/v1/history", TIRE), ("/v1/related", {"subject": "unit:MXK-4821"}),
                       ("/v1/search", {"text": "T-9001"})):
        res = client.post(path, json=body, headers=_bearer(keys["acme"], tenant))
        assert res.status_code in (200, 400), (path, res.status_code)
        if res.status_code == 200:
            data = res.json()
            assert data == [] or (isinstance(data, dict) and data.get("found") is False) or "T-9001" not in json.dumps(data), (path, data)
    assert all(t.startswith(("zeta/", "acme/")) for t in store.tenants())  # nothing outside the two accounts


@pytest.mark.parametrize("tenant", ["C1", "c1 x", "c１", "c1\u200b", "c2", "zeta/c1", "acme/c1"])
def test_a_narrowed_key_takes_no_lookalike_of_its_tenant(world, tenant):
    c, store, keys = world
    res = _client(c, store).post("/v1/context", json=TIRE, headers=_bearer(keys["narrow"], tenant))
    assert res.status_code == 403 or res.status_code == 400


def test_operator_cannot_address_a_global_partition_as_a_tenant(world):
    c, store, _ = world
    client = _client(c, store)
    for t in ("__global__", "acme/__global__", "zeta/__global__"):
        assert client.post("/v1/context", json=TIRE, headers={**OPERATOR, "X-Keel-Tenant": t}).status_code == 400
        tenant_note = {**NOTE, "scope": "tenant"}
        assert client.post("/v1/annotate", json=tenant_note, headers={**OPERATOR, "X-Keel-Tenant": t}).status_code == 400
    assert store.facts("acme/__global__", "tire:T-9001") == [] and store.facts("__global__", "tire:T-9001") == []


def test_event_bodies_cannot_escape_the_account(world):
    c, store, keys = world
    client = _client(c, store)
    before = sorted(store.tenants())
    assert client.post("/v1/events", json=_lines("zeta/c1"), headers=_bearer(keys["acme"])).status_code == 400
    assert client.post("/v1/events", json=_lines("zeta/c1"), headers=_bearer(keys["acme"], "zeta/c1")).status_code == 400
    assert client.post("/v1/events", json=_lines("zeta/c1"), headers=_bearer(keys["acme"], "c1")).status_code == 400  # body != header
    for reserved in ("__global__", "acme/__global__", "zeta/__global__"):
        assert client.post("/v1/events", json=_lines(reserved), headers=_bearer(keys["acme"])).status_code in (400, 422)
    mixed = _lines("c1")[:3] + _lines("zeta/c1")[3:6]  # one good tenant, one foreign: the whole batch is refused
    assert client.post("/v1/events", json=mixed, headers=_bearer(keys["acme"])).status_code == 400
    narrow_mixed = _lines("c1")[:3] + _lines("c2")[3:6]
    assert client.post("/v1/events", json=narrow_mixed, headers=_bearer(keys["narrow"])).status_code == 403
    assert sorted(store.tenants()) == before  # nothing of any refused batch was applied
    assert not store.seen("zeta/c1", "never")


def test_event_ids_are_deduplicated_per_tenant_not_across_accounts(world):
    """An id another account already sent is no oracle: it is new here."""
    c, store, keys = world
    res = _client(c, store).post("/v1/events", json=_lines("c1"), headers=_bearer(keys["acme"]))
    assert (res.json()["processed"], res.json()["duplicates"]) == (12, 0)
    assert "zeta" not in res.text


def test_recent_events_stay_in_their_account(world):
    c, store, keys = world
    with _client(c, store) as client:
        assert client.post("/v1/events", json=_lines("c7"), headers=_bearer(keys["zeta"])).status_code == 200
        assert client.get("/v1/events/recent", headers=_bearer(keys["acme"], "c7")).json() == []
        assert client.get("/v1/events/recent", headers=_bearer(keys["acme"], "zeta/c7")).status_code == 400
        assert client.get("/v1/events/recent", headers=_bearer(keys["narrow"], "c7")).status_code == 403
        assert client.get("/v1/events/recent", headers={"X-Keel-Tenant": "zeta/c7"}).status_code == 401
        assert len(client.get("/v1/events/recent", headers=_bearer(keys["zeta"], "c7")).json()) == 12


# --- agents --------------------------------------------------------------------------------------------------------


def _registry(store):
    c = load_cartridge(FLEET)
    c.agents = parse_agents({"tires-bot": {"writes": ["task", "agent"], "reads": {"kinds": ["unit", "tire"], "relations": ["mounted_on"]}},
                             "other-bot": {"writes": ["task", "agent"]}})
    bot, _ = create_key(store, "acme", tenant="c1", agent="tires-bot")
    return c, bot


@pytest.mark.parametrize("agent", ["other-bot", "", "TIRES-BOT", "tires-bot,other-bot", "human:boss"])
def test_a_pinned_agent_cannot_be_swapped(agent):
    store = MemoryStore()
    c, bot = _registry(store)
    res = _client(c, store).post("/v1/context", json=TIRE, headers=_bearer(bot, agent=agent))
    assert res.status_code == 403


def test_a_pinned_agent_writes_and_reads_as_itself_only():
    store = MemoryStore()
    c, bot = _registry(store)
    client = _client(c, store)
    op = {**OPERATOR, "X-Keel-Tenant": "acme/c1", "X-Keel-Agent": "other-bot"}
    secret_note = {**TIRE, "predicate": "scratch", "value": "other-bot's private note", "scope": "agent"}
    assert client.post("/v1/annotate", json=secret_note, headers=op).status_code == 200
    mine = client.post("/v1/annotate", json={**secret_note, "value": "mine", "detail": "full"}, headers=_bearer(bot))
    assert mine.status_code == 200 and mine.json()["source"] == "annotate:tires-bot"  # no header: the key's agent
    seen = client.post("/v1/history", json={**TIRE, "detail": "full"}, headers=_bearer(bot)).text
    assert "private note" not in seen and "mine" in seen
    for scope in ("agent", None):
        body = {**TIRE, "predicate": "scratch", "detail": "full", **({"scope": scope} if scope else {})}
        assert "private note" not in client.post("/v1/history", json=body, headers=_bearer(bot)).text
    assert "private note" not in client.post("/v1/context", json={**TIRE, "detail": "full"}, headers=_bearer(bot)).text
    assert "private note" not in client.post("/v1/search", json={"text": "private", "detail": "full"}, headers=_bearer(bot)).text


# --- global memory ---------------------------------------------------------------------------------------------------


def test_global_memory_needs_the_global_key_over_v1(world):
    c, store, keys = world
    client = _client(c, store)
    for k in ("acme", "narrow"):
        for extra in ({}, {"status": "proposed"}, {"scope": "global", "key": "x"}):
            assert client.post("/v1/annotate", json={**NOTE, **extra}, headers=_bearer(keys[k], "c1")).status_code == 403
    assert store.facts("acme/__global__", "tire:T-9001") == []
    assert client.post("/v1/annotate", json=NOTE, headers=_bearer(keys["admin"], "c1")).status_code == 200
    assert [f.value for f in store.facts("acme/__global__", "tire:T-9001")] == ["poisoned"]
    assert store.facts("__global__", "tire:T-9001") == []  # an account's global key never writes the deployment's
    zeta_view = client.post("/v1/history", json={**TIRE, "predicate": "glossary"}, headers=_bearer(keys["zeta"], "c1")).json()
    assert zeta_view == []


def test_global_memory_needs_the_global_key_over_mcp_per_request(world):
    pytest.importorskip("mcp")
    c, store, keys = world
    with _client(c, store) as client:
        err, text = _mcp(client, _bearer(keys["narrow"]), "annotate", NOTE)
    assert store.facts("acme/__global__", "tire:T-9001") == [], text
    assert err


def test_global_memory_needs_the_global_key_over_mcp_bound(world):
    pytest.importorskip("mcp")
    c, store, keys = world
    with _client(c, store, tenant="acme/c1") as client:
        _mcp(client, _bearer(keys["acme"]), "annotate", NOTE)
    assert store.facts("acme/__global__", "tire:T-9001") == []


# --- /mcp ------------------------------------------------------------------------------------------------------------


def test_mcp_per_request_stays_in_the_account(world):
    pytest.importorskip("mcp")
    c, store, keys = world
    with _client(c, store) as client:
        err, text = _mcp(client, _bearer(keys["acme"], "c1"), "context", TIRE)
        assert not err and json.loads(text)["found"] is False
        err, text = _mcp(client, _bearer(keys["acme"], "zeta/c1"), "context", TIRE)
        assert err is True and "T-9001" not in text.replace("tire:T-9001", "")
        err, text = _mcp(client, _bearer(keys["narrow"], "c2"), "history", TIRE)
        assert err is True
        assert _mcp(client, {"X-Keel-Tenant": "zeta/c1"}, "context", TIRE)[0] == 401
        assert _mcp(client, _bearer("kk_forged_token", "c1"), "context", TIRE)[0] == 401
        err, text = _mcp(client, {**OPERATOR, "X-Keel-Tenant": "zeta/c1"}, "context", TIRE)  # the operator reaches it
        assert not err and json.loads(text)["evaluation"] == {"tire_critical": True}


def test_mcp_bound_to_a_tenant_refuses_keys_that_do_not_own_it(world):
    pytest.importorskip("mcp")
    c, store, keys = world
    wrong_agent, _ = create_key(store, "zeta", tenant="c1", agent="tires-bot")  # the bound tools carry no agent
    other, _ = create_key(store, "zeta", tenant="c2")
    with _client(c, store, tenant="zeta/c1") as client:
        for k in (keys["acme"], keys["narrow"], keys["admin"], other, wrong_agent):
            assert _mcp(client, _bearer(k, "c1"), "context", TIRE)[0] == 403
        err, text = _mcp(client, _bearer(keys["zeta"]), "context", TIRE)
        assert not err and json.loads(text)["evaluation"] == {"tire_critical": True}
        app = client.app
        h = [(b"authorization", f"Bearer {keys['acme']}".encode()), (b"accept", b"application/json, text/event-stream"),
             (b"content-type", b"application/json")]
        for path in ("//mcp", "/mcp/", "/MCP", "/ui/../mcp", "/./mcp", "/mcp%00"):
            status, body = _raw(app, "POST", path, h, b"{}")
            assert status not in (200, 202) and b"T-9001" not in body, (path, status)


# --- authentication ------------------------------------------------------------------------------------------------


GUARDED = [("POST", "/v1/context"), ("POST", "/v1/history"), ("POST", "/v1/search"), ("POST", "/v1/annotate"), ("POST", "/v1/related"),
           ("POST", "/v1/events"), ("POST", "/v1/events/check"), ("GET", "/v1/events/recent"), ("GET", "/v1/cartridge"),
           ("GET", "/v1/sdk/python"), ("POST", "/mcp"), ("GET", "/mcp")]


@pytest.mark.parametrize("ui", [False, True])
def test_auth_cannot_be_bypassed_by_method_or_path(world, ui):
    c, store, _ = world
    client = _client(c, store, ui=ui)
    h = {"X-Keel-Tenant": "zeta/c1"}
    for method, path in GUARDED:
        for m in (method, "HEAD", "OPTIONS", "PUT", "DELETE"):
            for p in (path, path + "/", path.upper(), "/" + path):
                assert client.request(m, p, headers=h).status_code == 401, (m, p)
    for given in ("Bearer", "Bearer ", f"bearer {SECRET}", f"Bearer {SECRET} ", f"Bearer {SECRET[:-1]}", f"Basic {SECRET}",
                  f"Bearer  {SECRET}", "Bearer kk_", "Bearer kk_x_y"):
        assert client.post("/v1/context", json=TIRE, headers={**h, "Authorization": given}).status_code == 401, given
    body = json.dumps(TIRE).encode()
    raw = [(b"x-keel-tenant", b"zeta/c1"), (b"content-type", b"application/json")]
    for path in ("/ui/../v1/context", "/ui/../../v1/context", "/ui//v1/context", "/ui/..//v1/context", "/ui/../mcp",
                 "/health/../v1/context", "/docs/../v1/context", "/openapi.json/../v1/context", "/v1/./context", "//v1/context"):
        for m in ("GET", "POST"):
            status, out = _raw(client.app, m, path, raw, body)
            assert status in (401, 404, 405) or (ui and m == "GET" and out.lstrip().startswith(b"<!doctype html>")), (m, path, status)
            assert b"depth_mm" not in out and b"T-9001" not in out, (m, path)


def test_the_console_serves_only_its_own_files(world):
    c, store, _ = world
    app = _client(c, store, ui=True).app
    for path in ("/ui/../../keel/http.py", "/ui/../http.py", "/ui/../../pyproject.toml", "/ui/%2e%2e/http.py", "/ui//etc/passwd",
                 "/ui/../../../../../../etc/passwd"):
        _, out = _raw(app, "GET", path)
        assert b"build_http_app" not in out and b"[project]" not in out and b"root:" not in out, path


def test_revoking_every_key_keeps_a_secretless_server_closed():
    c = load_cartridge(FLEET)
    store = MemoryStore()
    full, key = create_key(store, "acme")
    revoke_key(store, key.id)
    client = TestClient(build_http_app(c, store))  # no secret: only the keys close it
    h = {"X-Keel-Tenant": "acme/c1"}
    assert client.post("/v1/context", json=TIRE, headers=h).status_code == 401
    assert client.post("/v1/context", json=TIRE, headers={**h, "Authorization": f"Bearer {full}"}).status_code == 401


def test_a_hosted_server_takes_only_its_accounts_keys(world):
    c, store, keys = world
    client = _client(c, store, account="acme")
    res = client.post("/v1/context", json=TIRE, headers=_bearer(keys["zeta"], "c1"))
    assert res.status_code == 403 and "T-9001" not in res.text
    assert client.post("/v1/events", json=_lines("c1"), headers=_bearer(keys["zeta"])).status_code == 403
    assert not any(t.startswith("acme/") for t in store.tenants())


@pytest.mark.parametrize("given", ["Bearer é".encode(), b"Bearer \xff\xfe", f"Bearer {SECRET}é".encode()])
def test_a_garbled_authorization_header_is_a_401(world, given):
    c, store, _ = world
    app = build_http_app(c, store, secret=SECRET)
    status, _ = _raw(app, "POST", "/v1/context", [(b"authorization", given), (b"x-keel-tenant", b"zeta/c1"),
                                                  (b"content-type", b"application/json")], json.dumps(TIRE).encode())
    assert status == 401


# --- what leaks without a credential, or to the wrong one --------------------------------------------------------


def test_health_says_nothing_about_the_database(world):
    c, store, _ = world
    cdc = {"ok": False, "error": 'OperationalError: connection to server at "10.0.0.5", port 5432 failed: FATAL: password '
                                 'authentication failed for user "keel_prod"', "tables": ["public.patients"], "events": 9}
    text = _client(c, store, health_extra={"cdc": cdc}).get("/health").text
    assert "10.0.0.5" not in text and "keel_prod" not in text and "patients" not in text


def test_refusals_name_no_other_account(world):
    c, store, keys = world
    client = _client(c, store)
    texts = [client.post("/v1/context", json=TIRE, headers=_bearer(keys["acme"], t)).text for t in ("zeta/c1", "__global__", "")]
    texts += [client.post("/v1/context", json=TIRE, headers=_bearer(keys["narrow"], "c2")).text,
              client.post("/v1/events", json=_lines("zeta/c1"), headers=_bearer(keys["acme"])).text,
              client.post("/v1/annotate", json=NOTE, headers=_bearer(keys["acme"], "c1")).text]
    assert all("zeta/c1" not in t.replace('"zeta/c1"', "") or "give it without '/'" in t for t in texts)
    assert not any("depth_mm" in t or "MXK-4821" in t for t in texts)


def test_events_check_ingests_nothing_and_echoes_no_tenant_data(world):
    c, store, keys = world
    client = _client(c, store)
    before = {t: len(store.facts(t, "tire:T-9001")) for t in store.tenants()}
    out = client.post("/v1/events/check", json=_lines("zeta/c1"), headers=_bearer(keys["acme"])).json()
    assert out == [] and {t: len(store.facts(t, "tire:T-9001")) for t in store.tenants()} == before
    assert client.post("/v1/events/check", json=_lines("c1"), headers={"X-Keel-Tenant": "c1"}).status_code == 401


# --- quotas ------------------------------------------------------------------------------------------------------------


def test_requests_per_second_cover_every_keyed_path_and_every_key_of_the_account(world):
    from keel.accounts import set_account

    c, store, keys = world
    set_account(store, "acme", rps=1)
    with _client(c, store) as client:
        assert client.post("/v1/search", json={"text": "x"}, headers=_bearer(keys["acme"], "c1")).status_code == 200
        later = [client.post("/v1/events/check", json=[], headers=_bearer(keys["narrow"])),
                 client.get("/v1/sdk/python", headers=_bearer(keys["admin"])),
                 client.get("/v1/cartridge", headers=_bearer(keys["acme"])),
                 client.get("/v1/events/recent", headers=_bearer(keys["narrow"])),
                 client.head("/v1/context", headers=_bearer(keys["acme"])),
                 client.post("/mcp", json={}, headers=_bearer(keys["acme"], "c1"))]
        assert [r.status_code for r in later] == [429] * len(later)
        assert client.post("/v1/search", json={"text": "x"}, headers=_bearer(keys["zeta"], "c1")).status_code == 200  # its own bucket


def test_the_monthly_event_quota_is_atomic_per_batch(world):
    from keel.accounts import set_account

    c, store, keys = world
    set_account(store, "acme", events_month=5)
    client = _client(c, store)
    over = client.post("/v1/events", json=_lines("c1"), headers=_bearer(keys["acme"]))
    assert over.status_code == 429 and "acme/c1" not in store.tenants()
    split = [client.post("/v1/events", json=_lines(t)[:3], headers=_bearer(k)) for t, k in (("c1", keys["narrow"]), ("c2", keys["acme"]))]
    assert [r.status_code for r in split] == [200, 429]  # one month's count for every key and tenant of the account


def test_a_bound_mcp_server_still_states_its_ontology(world):
    """Binding /mcp builds its tools per key (global rights or not), so the server gets a function; its
    instructions (the ontology, stated once per session) come from the cartridge it is given."""
    from keel.mcp_server import build_server

    c, _, _ = world
    assert "Kinds: document, driver" in (build_server(lambda request: None, cartridge=c).instructions or "")
