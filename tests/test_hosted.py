"""Hosted control plane: sign-up creates an account, its own database, its key and its container."""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from hosted.app import ControlPlane, Settings, build_app, check_cartridge
from keel.store.sqlite import SqliteStore

ROOT = Path(__file__).resolve().parent.parent


class FakeDatabases:
    def __init__(self, root: Path) -> None:
        self.root, self.dropped = root, []

    def create(self, account: str) -> str:
        return str(self.root / f"{account}.db")

    def drop(self, account: str) -> None:
        self.dropped.append(account)


class FakeProvisioner:
    def __init__(self, fail: bool = False) -> None:
        self.created, self.fail = [], fail

    def create(self, account, files, env):
        if self.fail:
            raise RuntimeError("coolify is down")
        self.created.append((account, sorted(files), env))
        return f"app-{account}", f"https://{account}.keel.example"


def plane(tmp_path, provisioner=None):
    return ControlPlane(SqliteStore(tmp_path / "control.db"), FakeDatabases(tmp_path), provisioner or FakeProvisioner(),
                        Settings(invite_codes={"pilot-1"}, examples_dir=str(ROOT / "examples")))


def test_signup_creates_an_isolated_account(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    assert client.get("/").status_code == 200
    bad = client.post("/signup", data={"account": "acme", "email": "a@acme.co", "invite": "nope", "example": "fleet"})
    assert bad.status_code == 400 and "invitation" in bad.text
    ok = client.post("/signup", data={"account": "acme", "email": "a@acme.co", "invite": "pilot-1", "example": "fleet"})
    assert ok.status_code == 200 and "https://acme.keel.example" in ok.text
    account, files, env = p.provisioner.created[0]
    assert (account, files) == ("acme", ["cartridge.yaml", "handlers.py"])
    assert env["KEEL_ACCOUNT"] == "acme" and env["KEEL_DB"] == str(tmp_path / "acme.db") and "control" not in env["KEEL_DB"]
    own = SqliteStore(tmp_path / "acme.db")
    assert [k.account for k in own.api_keys()] == ["acme"] and own.account("acme").events_month == 100_000  # the container enforces them
    key = ok.text.split("<pre>")[1].split("</pre>")[0]
    assert key.startswith("kk_")
    panel = client.post("/panel", data={"key": key})
    assert panel.status_code == 200 and "acme" in panel.text and "Nothing yet" in panel.text
    assert client.post("/panel", data={"key": "kk_x_y"}).status_code == 403
    again = client.post("/signup", data={"account": "acme", "email": "b@x.co", "invite": "pilot-1", "example": "crm"})
    assert again.status_code == 400 and "taken" in again.text


def test_signup_with_an_uploaded_cartridge_and_a_failed_provision(tmp_path):
    p = plane(tmp_path, FakeProvisioner(fail=True))
    client = TestClient(build_app(p))
    files = {"cartridge_yaml": ("cartridge.yaml", (ROOT / "examples/clinics/cartridge.yaml").read_bytes()),
             "handlers_py": ("handlers.py", b"raise SystemExit('never imported by the control plane')")}
    with pytest.raises(RuntimeError):
        client.post("/signup", data={"account": "clinic-1", "email": "c@x.co", "invite": "pilot-1"}, files=files)
    assert p.databases.dropped == ["clinic-1"]  # nothing half-made stays behind


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
