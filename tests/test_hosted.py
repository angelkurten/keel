"""Hosted control plane: sign-up by invitation, an isolated account born with the example, the console's API."""

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from hosted.app import COOKIE, ControlPlane, Settings, build_app, check_cartridge
from keel.store.sqlite import SqliteStore

ROOT = Path(__file__).resolve().parent.parent
PW = "correct horse battery"


class FakeDatabases:
    def __init__(self, root: Path) -> None:
        self.root, self.dropped = root, []

    def create(self, account: str) -> str:
        return str(self.root / f"{account}.db")

    def drop(self, account: str) -> None:
        self.dropped.append(account)


class FakeProvisioner:
    def __init__(self, fail: bool = False) -> None:
        self.created, self.restarted, self.updates, self.fail, self.keel = [], [], [], fail, None

    def create(self, account, files, env):
        if self.fail:
            raise RuntimeError("coolify is down")
        self.created.append((account, sorted(files), env))
        return f"app-{account}", f"https://{account}.keel.example"

    def restart(self, uuid):
        self.restarted.append(uuid)

    def volume(self, uuid, name, mount_path):
        self.updates.append((uuid, ["volume:" + mount_path], None, ()))

    def update(self, uuid, files=None, env=None, drop_env=()):
        from keel.cartridge import revision

        self.updates.append((uuid, sorted(files or {}), env, drop_env))
        if files and "cartridge.yaml" in files and "boom" not in files["handlers.py"]:  # a cartridge whose import fails never starts
            self.keel.revision = revision(files["cartridge.yaml"], files["handlers.py"])


class FakeKeel:
    """The account's container and the model, as httpx sees them."""

    def __init__(self, healthy_after: int = 0) -> None:
        self.calls, self.health_checks, self.healthy_after, self.events, self.revision = [], 0, healthy_after, [], None

    def handler(self, request):
        import httpx

        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), dict(request.headers), body))
        path = request.url.path
        if path == "/health":
            self.health_checks += 1
            return httpx.Response(200 if self.health_checks > self.healthy_after else 503, json={"ok": True, "revision": self.revision, "cdc": {"ok": True}})
        if path == "/v1/events":
            self.events += body
            return httpx.Response(200, json={"processed": len(body)})
        if path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"total_tokens": 7}})
        if path == "/v1/events/recent":
            return httpx.Response(200, json=[{"id": "e1", "status": "applied", "tenant_seen": request.headers["x-keel-tenant"]}])
        if path == "/v1/events/check":
            return httpx.Response(200, json=[{"index": 0, "problems": [f"tenant {body[0]['tenant']}"]}])
        if path.startswith("/v1/sdk/"):
            return httpx.Response(200, json={"language": path.rsplit("/", 1)[1], "filename": "keel-fleet.ts", "code": "// fleet", "sha256": "ab"})
        if path == "/v1/search":
            return httpx.Response(200, json=[{"key": "tire:T-9001", "tenant": request.headers["x-keel-tenant"]}])
        if path == "/v1/chat/completions" and "You write Keel cartridges" in body["messages"][0]["content"]:
            fixed = len(body["messages"]) > 2  # the second attempt, after the problems were sent back
            yaml_text = (ROOT / "examples/clinics/cartridge.yaml").read_text()
            handlers = (ROOT / "examples/clinics/handlers.py").read_text() if fixed else "from keel.handlers import HandlerRegistry\nregistry = HandlerRegistry()\n"
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": f"```yaml\n{yaml_text}```\n\n```python\n{handlers}```"}}]})
        if path == "/v1/chat/completions":
            if not any(m["role"] == "tool" for m in body["messages"]):
                call = {"id": "c1", "type": "function", "function": {"name": "search", "arguments": json.dumps({"rule": "tire_critical"})}}
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Replace tire:T-9001."}}]})
        return httpx.Response(404, json={"detail": "not found"})


def plane(tmp_path, provisioner=None, keel=None, **settings):
    import httpx

    keel = keel or FakeKeel()
    http = httpx.Client(transport=httpx.MockTransport(keel.handler))
    s = Settings(invite_codes={"pilot-1", "pilot-2", "pilot-3"}, examples_dir=str(ROOT / "examples"), ready_timeout=2, ofox_key="ofox-test", **settings)
    p = ControlPlane(SqliteStore(tmp_path / "control.db"), FakeDatabases(tmp_path), provisioner or FakeProvisioner(), s, http)
    p.keel = keel  # the fake's record of calls, for the tests
    p.provisioner.keel = keel
    return p


def wait(client, status="ready"):
    for _ in range(200):
        me = client.get("/api/me").json()
        if me["status"] in (status, "failed"):
            return me
        time.sleep(0.02)
    raise AssertionError(me)


def test_signup_creates_an_account_born_with_the_example(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    assert client.get("/api/slug", params={"email": "angel@acme.com"}).json() == {"account": "acme", "available": True, "error": None}
    assert client.get("/api/slug", params={"email": "angel.k@gmail.com"}).json()["account"] == "angel-k"
    bad = client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "nope"})
    assert bad.status_code == 403 and "invite" in bad.json()["detail"]
    assert client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": "short", "invite": "pilot-1"}).status_code == 400
    assert client.get("/api/me").status_code == 401
    ok = client.post("/api/signup", json={"email": "A@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    assert ok.status_code == 200 and COOKIE in ok.cookies
    me = wait(client)
    assert me["status"] == "ready" and me["endpoint"] == "https://acme.keel.example" and [s["state"] for s in me["steps"]] == ["done"] * 4
    account, files, env = p.provisioner.created[0]
    assert (account, files, env["KEEL_ACCOUNT"]) == ("acme", ["cartridge.yaml", "handlers.py"], "acme") and "control" not in env["KEEL_DB"]
    assert SqliteStore(tmp_path / "acme.db").account("acme").events_month == 100_000  # the container enforces it
    events = p.keel.events
    assert events and {e["tenant"] for e in events} == {"acme/demo"}
    loaded = [c for c in p.keel.calls if c[1].endswith("/v1/events")]
    assert loaded[0][2]["authorization"] == f"Bearer {env['KEEL_SECRET']}"
    assert client.get("/api/cartridge").json()["name"] == "fleet" and client.get("/api/cartridge").json()["example"] is True
    again = TestClient(build_app(p)).post("/api/signup", json={"email": "b@x.co", "account": "acme", "password": PW, "invite": "pilot-1"})
    assert again.status_code == 400 and "taken" in again.json()["detail"]
    # sign out, sign in
    client.post("/api/signout")
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"email": "a@acme.com", "password": "wrong password"}).status_code == 403
    assert client.post("/api/login", json={"email": "a@acme.com", "password": PW}).status_code == 200
    assert client.get("/api/me").json()["account"] == "acme"


def test_a_slow_container_is_restarted_once_and_a_failed_provision_cleans_up(tmp_path):
    p = plane(tmp_path, keel=FakeKeel(healthy_after=10**9))
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    me = wait(client)
    assert me["status"] == "failed" and "did not start" in me["error"] and p.provisioner.restarted == ["app-acme"]
    (tmp_path / "x").mkdir()
    p = plane(tmp_path / "x", provisioner=FakeProvisioner(fail=True))
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "c@x.co", "account": "clinic-1", "password": PW, "invite": "pilot-1"})
    assert wait(client)["error"] == "coolify is down" and p.databases.dropped == ["clinic-1"]


def test_the_console_calls_the_account_keel_with_its_secret(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    r = client.post("/api/v1/search", json={"text": "T-9001"}, headers={"X-Keel-Tenant": "demo"})
    assert r.status_code == 200 and r.json()[0]["tenant"] == "acme/demo"
    assert client.post("/api/v1/search", json={}, headers={"X-Keel-Tenant": "zeta/demo"}).status_code == 400  # no reaching out of the account
    assert client.post("/api/v1/drop", json={}).status_code == 409
    key = client.post("/api/keys", json={"tenant": "demo"}).json()
    assert key["key"].startswith("kk_") and key["mcp"] == "https://acme.keel.example/mcp"
    own = SqliteStore(tmp_path / "acme.db").api_keys()
    assert [(k.account, k.tenant) for k in own] == [("acme", "demo")]  # the container checks this copy
    assert TestClient(build_app(p)).post("/api/v1/search", json={}).status_code == 401


def test_the_playground_agent_calls_tools_within_a_daily_allowance(tmp_path):
    p = plane(tmp_path, agent_messages_day=1)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    assert client.get("/api/agent/models").json()["allowance"] == {"used": 0, "limit": 1}
    body = {"model": "anthropic/claude-sonnet-5.5", "system": "You help a fleet.", "tools": ["search", "context"], "tenant": "demo",
            "messages": [{"role": "user", "content": "Which tires should we replace?"}]}
    out = client.post("/api/agent/chat", json=body).json()
    assert out["trace"][0]["tool"] == "search" and out["trace"][0]["ok"] and out["messages"][-1]["content"] == "Replace tire:T-9001."
    assert out["allowance"] == {"used": 1, "limit": 1}
    model = next(c for c in p.keel.calls if c[1].endswith("/chat/completions"))
    assert model[1].startswith("https://api.ofox.ai/") and {t["function"]["name"] for t in model[3]["tools"]} == {"search", "context"}
    assert client.post("/api/agent/chat", json=body).status_code == 429
    mine = client.post("/api/agent/chat", json={**body, "model": "anthropic/claude-haiku-5.5", "own_key": {"provider": "anthropic", "key": "sk-own"}})
    assert mine.status_code == 200  # the customer's own key has no allowance
    last = [c for c in p.keel.calls if c[1].endswith("/chat/completions")][-1]
    assert last[1].startswith("https://api.anthropic.com/") and last[3]["model"] == "claude-haiku-5-5" and last[2]["authorization"] == "Bearer sk-own"
    assert client.put("/api/agents/Fleet assistant", json={"config": body}).status_code == 200
    assert client.get("/api/agents").json() == [{"name": "Fleet assistant", "config": body}]
    client.delete("/api/agents/Fleet assistant")
    assert client.get("/api/agents").json() == []


def test_events_keys_usage_and_sdk_through_the_console(tmp_path):
    p = plane(tmp_path, events_month=13)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    h = {"X-Keel-Tenant": "c1"}
    assert client.get("/api/events/recent", headers=h).json()[0]["tenant_seen"] == "acme/c1"
    assert client.post("/api/events/check", json=[{"tenant": "other", "type": "x"}], headers=h).json()[0]["problems"] == ["tenant acme/c1"]
    sent = p.keel.events[:]
    one = {"id": "u1", "tenant": "elsewhere", "type": "unit.registered", "subject": "unit:U1", "occurred_at": "2026-10-01T00:00:00Z",
           "source": "upload", "payload": {}}
    assert client.post("/api/events", json=[one], headers=h).status_code == 200
    assert p.keel.events[len(sent):] == [{**one, "tenant": "acme/c1"}]  # every envelope lands in the chosen tenant
    assert client.post("/api/events", json=[one] * 501, headers=h).status_code == 400
    from keel.usage import today
    SqliteStore(tmp_path / "acme.db").meter([("acme/c1", today(), "events", 13)])  # the month is used up
    assert client.post("/api/events", json=[one], headers=h).status_code == 429
    usage = client.get("/api/usage").json()
    assert usage["limits"]["events_month"] == 13 and [(r["tenant"], r["events"]) for r in usage["rows"]] == [("acme/c1", 13)]
    made = client.post("/api/keys", json={"tenant": "c1", "global": False}).json()
    glob = client.post("/api/keys", json={"global": True}).json()
    listed = {k["id"]: k for k in client.get("/api/keys").json()}
    assert listed[made["id"]]["tenant"] == "c1" and listed[glob["id"]]["global"] is True and listed[made["id"]]["revoked_at"] is None
    assert client.delete(f"/api/keys/{made['id']}").status_code == 200
    assert {k["id"]: k for k in client.get("/api/keys").json()}[made["id"]]["revoked_at"] is not None
    assert next(k for k in SqliteStore(tmp_path / "acme.db").api_keys() if k.id == made["id"]).revoked_at is not None  # the container's copy too
    assert client.delete("/api/keys/kk_nope").status_code == 409
    assert client.get("/api/sdk/typescript").json()["filename"] == "keel-fleet.ts"


def test_replacing_the_cartridge_reverts_when_the_new_one_does_not_start(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    clinics = {n: (ROOT / "examples/clinics" / n).read_text() for n in ("cartridge.yaml", "handlers.py")}
    assert client.post("/api/cartridge/check", json={"cartridge_yaml": clinics["cartridge.yaml"], "handlers_py": ""}).json()["problems"]  # no handlers
    assert client.put("/api/cartridge", json={"cartridge_yaml": "name: x", "handlers_py": "def ("}).status_code == 400
    r = client.put("/api/cartridge", json={"cartridge_yaml": clinics["cartridge.yaml"], "handlers_py": clinics["handlers.py"]})
    assert r.status_code == 200 and r.json()["problems"] == []
    me = wait(client)
    assert me["status"] == "ready" and me["notice"] is None and client.get("/api/cartridge").json()["name"] == "clinics"
    assert client.get("/api/cartridge").json()["example"] is False
    # the example's tenant is forgotten when its cartridge goes
    assert "acme/demo" in SqliteStore(tmp_path / "acme.db").forgotten_tenants()
    broken = {**clinics, "handlers.py": clinics["handlers.py"] + "\nboom()\n"}
    client.put("/api/cartridge", json={"cartridge_yaml": broken["cartridge.yaml"].replace("clinics", "clinics2", 1), "handlers_py": broken["handlers.py"]})
    me = wait(client)
    assert me["status"] == "ready" and "previous one is back" in me["notice"] and client.get("/api/cartridge").json()["name"] == "clinics"
    client.put("/api/cartridge", json={"example": "fleet"})
    wait(client)
    assert client.get("/api/cartridge").json()["example"] is True


def test_a_cartridge_generated_from_events_is_checked_against_them(tmp_path):
    p = plane(tmp_path, generations_day=1)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    lines = [ln for ln in (ROOT / "examples/clinics/events.jsonl").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    events = [{k: v for k, v in json.loads(ln).items() if k != "tenant"} for ln in lines[:3]]
    out = client.post("/api/cartridge/generate", json={"events": events, "description": "clinics"}).json()
    assert out["attempts"] == 2 and out["problems"] == [] and "registry.on" in out["handlers_py"] and "name: clinics" in out["cartridge_yaml"]
    assert client.post("/api/cartridge/generate", json={"events": events}).status_code == 429


def test_cdc_is_set_on_the_account_container(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    assert client.get("/api/cdc").json()["configured"] is False
    bad = client.put("/api/cdc", json={"dsn": "postgresql://u:p@db:5432/app", "map": "tires: {kind: wheel, id: serial, tenant: fleet_id}"})
    assert bad.status_code == 400 and "wheel.created" in bad.json()["detail"]
    good = client.put("/api/cdc", json={"dsn": "postgresql://u:p@db:5432/app", "map": "tires: {kind: tire, id: serial, tenant: fleet_id}"})
    assert good.status_code == 400 and "tire.created" in good.json()["detail"]  # fleet has tire.depth_measured, not the CDC types
    tires = "name: tires\nentities: {tire: {}}\nevents: {tire.created: {subject: tire}, tire.updated: {subject: tire}, tire.deleted: {subject: tire}}\n"
    p.control._exec("UPDATE hosted_accounts SET cartridge = ? WHERE account = 'acme'", (tires,))  # as if deployed
    ok = client.put("/api/cdc", json={"dsn": "postgresql://u:p@db:5432/app", "map": "tires: {kind: tire, id: serial, tenant: fleet_id}"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["tables"] == ["tires"] and ok.json()["status"] == {"ok": True}
    _, files, env, _ = p.provisioner.updates[-1]
    assert files == ["cdc.yaml"] and env == {"KEEL_CDC_MAP": "/cartridge/cdc.yaml", "KEEL_PG_DSN": "postgresql://u:p@db:5432/app"}
    assert "postgresql" not in json.dumps(client.get("/api/cdc").json())  # the DSN stays in the container
    client.delete("/api/cdc")
    assert p.provisioner.updates[-1][3] == ("KEEL_CDC_MAP", "KEEL_PG_DSN") and client.get("/api/cdc").json()["configured"] is False


def test_semantic_search_points_the_container_at_the_embeddings_proxy(tmp_path):
    p = plane(tmp_path, embedding_tokens_day=10)
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    wait(client)
    assert client.put("/api/semantic").json() == {"semantic": True}
    secret = p._row("acme")["secret"]
    env = p.provisioner.updates[-1][2]
    assert p.provisioner.updates[-2][1] == ["volume:/data"] and env["KEEL_INDEX_EVERY"] == "600"
    assert env["KEEL_EMBEDDINGS_API_KEY"] == secret and env["KEEL_EMBEDDINGS_BASE_URL"].endswith("/embed/v1")
    assert "ofox" not in str(env).lower()  # the gateway's key never enters a customer's container
    anon = TestClient(build_app(p))
    assert anon.post("/embed/v1/embeddings", json={"input": ["x"]}, headers={"Authorization": "Bearer nope"}).status_code == 403
    ok = anon.post("/embed/v1/embeddings", json={"input": ["x"], "model": "openai/text-embedding-3-large"}, headers={"Authorization": f"Bearer {secret}"})
    assert ok.status_code == 200 and ok.json()["data"][0]["embedding"] == [0.1]
    sent = next(c for c in p.keel.calls if c[1].endswith("/v1/embeddings"))
    assert sent[3]["model"] == "openai/text-embedding-3-small" and sent[2]["authorization"] == "Bearer ofox-test"  # the model is fixed
    anon.post("/embed/v1/embeddings", json={"input": ["x"]}, headers={"Authorization": f"Bearer {secret}"})
    assert anon.post("/embed/v1/embeddings", json={"input": ["x"]}, headers={"Authorization": f"Bearer {secret}"}).status_code == 429  # 14 > 10 tokens today


def test_the_console_build_is_served_with_a_fallback(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = TestClient(build_app(plane(tmp_path, static_dir=str(dist))))
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/console/graph").text == "<div id=root></div>"
    assert client.get("/../control.db").text == "<div id=root></div>"
    assert client.get("/api/nope").status_code == 404


def test_accounts_from_before_the_console_are_completed_from_their_container(tmp_path):
    p = plane(tmp_path)
    p.provisioner.current = lambda app: ({"cartridge.yaml": "name: fleet", "handlers.py": "x"}, {"KEEL_SECRET": "s3", "KEEL_DB": "d"})
    p.control._exec("INSERT INTO hosted_accounts (account, email, app, endpoint, db, created_at) VALUES ('old', 'o@x.co', 'app-old', 'https://old', 'd', 'now')")
    p.heal()
    row = p._row("old")
    assert (row["secret"], row["cartridge"], row["status"]) == ("s3", "name: fleet", "ready")


def test_check_cartridge():
    assert check_cartridge({"cartridge.yaml": "name: x\nentities: {a: {}}\nevents: {a.b: {subject: a}}", "handlers.py": ""}) == "x"
    for bad in ({"cartridge.yaml": "name: x"}, {"cartridge.yaml": ": :", "handlers.py": ""}, {"cartridge.yaml": "name: x", "handlers.py": ""}):
        with pytest.raises(ValueError):
            check_cartridge(bad)
def test_coolify_provisioner_calls(tmp_path):
    import json

    import httpx

    from hosted.provision import CoolifyProvisioner

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.url.path.endswith("/applications/dockerimage"):
            return httpx.Response(201, json={"uuid": "app-1"})
        return httpx.Response(200, json={})

    http = httpx.Client(base_url="https://coolify.test/api/v1", transport=httpx.MockTransport(handler))
    p = CoolifyProvisioner("https://coolify.test", "t", "proj", "srv", "keel.example", "0.27.1", http=http)
    assert p.create("acme", {"cartridge.yaml": "name: x", "handlers.py": ""}, {"KEEL_ACCOUNT": "acme"}) == ("app-1", "https://acme.keel.example")
    paths = [(m, path) for m, path, _, _ in calls]
    assert paths == [("POST", "/api/v1/applications/dockerimage"), ("POST", "/api/v1/applications/app-1/storages"),
                     ("POST", "/api/v1/applications/app-1/storages"), ("POST", "/api/v1/applications/app-1/envs"), ("POST", "/api/v1/deploy")]
    create = calls[0][3]
    assert create["docker_registry_image_tag"] == "0.27.1" and create["domains"] == "https://acme.keel.example" and create["instant_deploy"] is False
    assert calls[1][3] == {"type": "file", "mount_path": "/cartridge/cartridge.yaml", "content": "name: x"}
    assert calls[-1][2] == {"uuid": "app-1"}


def test_coolify_update_replaces_mounted_files():
    """Coolify's PATCH changes its own copy of a file mount but not the host's file: update deletes and recreates it."""
    import httpx

    from hosted.provision import CoolifyProvisioner

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/storages") and request.method == "GET":
            return httpx.Response(200, json={"file_storages": [{"uuid": "s1", "mount_path": "/cartridge/cartridge.yaml"}], "persistent_storages": []})
        return httpx.Response(200, json={})

    http = httpx.Client(base_url="https://coolify.test/api/v1", transport=httpx.MockTransport(handler))
    CoolifyProvisioner("https://coolify.test", "t", "p", "s", "keel.example", "0.30.0", http=http).update("app-1", files={"cartridge.yaml": "name: x", "handlers.py": ""})
    assert calls == [("GET", "/api/v1/applications/app-1/storages"), ("DELETE", "/api/v1/applications/app-1/storages/s1"),
                     ("POST", "/api/v1/applications/app-1/storages"), ("POST", "/api/v1/applications/app-1/storages"), ("POST", "/api/v1/deploy")]
