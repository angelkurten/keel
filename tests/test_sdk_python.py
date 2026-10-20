"""The generated Python client, end to end against `keel serve --transport http`."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from keel import load_cartridge
from keel.sdk import generate

FLEET, TENANT, SECRET = "examples/fleet", "acme-logistics", "s3cret"


@pytest.fixture
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "keel.cli", "serve", FLEET, "--transport", "http", "--port", str(port)],
        env={**os.environ, "KEEL_SECRET": SECRET}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("keel serve did not come up")
    yield base
    proc.kill()


def _load(tmp_path, code, name="keel_fleet"):
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_python_client(tmp_path, server):
    code = generate("python", load_cartridge(FLEET))
    assert "class TireDepthMeasuredPayload(TypedDict):\n    unit_id: str\n    position: str\n    depth_mm: float" in code
    assert "class _TicketOpenedPayloadRequired(TypedDict):" in code and "class TicketOpenedPayload(_TicketOpenedPayloadRequired, total=False):\n    inspection_id: str | None" in code
    m = _load(tmp_path, code)
    keel = m.client(server, TENANT, secret=SECRET)
    report = keel.events([
        m.emit.unit_registered(TENANT, m.subjects.unit("MXK-4821"), "2026-09-01T09:00:00Z", "fleet.vehicles", {"id": "MXK-4821", "plate": "MXK-4821", "model": "Cascadia"}),
        m.emit.tire_depth_measured(TENANT, m.subjects.tire("T-9001"), "2026-09-10T08:00:00Z", "tires", {"unit_id": "MXK-4821", "position": "P3", "depth_mm": 5.2}),
        m.emit.tire_depth_measured(TENANT, m.subjects.tire("T-9001"), "2026-10-03T08:00:00Z", "tires", {"unit_id": "MXK-4821", "position": "P3", "depth_mm": 2.6}),
    ])
    assert report == {"processed": 3, "duplicates": 0, "unhandled": [], "errors": [], "forgotten": [], "skipped": 0, "retained": []}
    assert keel.context(m.subjects.tire("T-9001"))["evaluation"] == {"tire_critical": True}
    assert keel.context(m.subjects.tire("T-9001"), as_of="2026-09-20")["evaluation"] == {"tire_critical": False}
    assert [f["value"] for f in keel.history(m.subjects.tire("T-9001"), predicate="depth_mm")] == [5.2, 2.6]
    assert keel.related(m.subjects.unit("MXK-4821"), "mounted_on")[0]["subject"] == "tire:T-9001"
    assert keel.search("T-9001")
    assert keel.annotate(m.subjects.tire("T-9001"), "assessment", "replace", scope="task", by="agent:py")["scope"] == "task"
    from keel.client import KeelError

    with pytest.raises(KeelError) as err:
        keel.annotate(m.subjects.tire("T-9001"), "assessment", "x", scope="tenant")
    assert err.value.status == 403 and "requires evidence" in err.value.detail
    first = keel.annotate("tire:T-9001", "plan", "swap", task="run-1", key="k1")
    assert keel.annotate("tire:T-9001", "plan", "swap", task="run-1", key="k1") == first
    assert [f["predicate"] for f in keel.history(changed_since="2000-01-01T00:00:00Z", task="run-1")] == ["plan"]
    assert [e["id"] for e in keel.search(kind="tire")] == ["T-9001"]
    traced = m.client(server, TENANT, secret=SECRET, timeout=5, traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert traced.annotate("tire:T-9001", "swap", True, status="proposed")["trace"] == traced.traceparent
    assert keel.context("tire:T-9001", detail="concise")["facts"]["depth_mm"] == 2.6
    assert m.client(server, "someone-else", secret=SECRET).context("tire:T-9001")["found"] is False
    with pytest.raises(KeelError) as err:
        m.client(server, TENANT, secret="wrong").search("x")
    assert err.value.status == 401
    assert m.RULES["tire_critical"]["threshold_mm"] == 3 and "tire" in m.KINDS


def test_generated_python_is_valid_for_every_cartridge(tmp_path):
    for cart in ("fleet", "crm", "finance", "clinics"):
        code = generate("python", load_cartridge(f"examples/{cart}"))
        compile(code, f"keel_{cart}.py", "exec")
        m = _load(tmp_path, code, f"keel_{cart}")
        assert m.CARTRIDGE == cart and len(m.KINDS) >= 3
    assert json.loads(json.dumps(m.RULES))  # rules survive as plain data


def test_generated_python_rules_with_true_and_null(tmp_path):
    from keel.sdk.python import render

    doc = load_cartridge(FLEET).schema()
    doc["rules"] = {**doc["rules"], "flags": {"when": {"active": True, "closed_at": None}}}
    m = _load(tmp_path, render(doc), "keel_flags")
    assert m.RULES["flags"] == {"when": {"active": True, "closed_at": None}}


def test_client_reuses_its_connection_and_sends_its_user_agent():
    import http.server
    import threading

    from keel import __version__
    from keel.client import Client, KeelError

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.client_address[1], self.path, self.headers["User-Agent"], self.headers.get("X-Extra")))
            status, body = (403, b'{"detail": "no"}') if self.path.endswith("/annotate") else (200, b"[]")
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if self.path.endswith("/search"):
                self.close_connection = True  # an idle timeout: closed without telling the client

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        keel = Client(f"http://127.0.0.1:{srv.server_port}/keel", TENANT, headers={"X-Extra": "1"})
        keel.related("tire:T-1")
        keel.history("tire:T-1")
        with pytest.raises(KeelError) as err:
            keel.annotate("tire:T-1", "p", 1)
        assert err.value.status == 403 and err.value.detail == "no"
        keel.related("tire:T-1")  # an error answer does not cost the connection
        assert len({port for port, *_ in seen}) == 1  # one TCP connection for every call
        assert {(path, ua, extra) for _, path, ua, extra in seen} >= {("/keel/v1/related", f"keel-context/{__version__}", "1")}
        keel.search("x")  # the server drops the connection after this answer
        keel.related("tire:T-1")  # the stale connection fails; retried once on a new one
        assert len({port for port, *_ in seen}) == 2
        ua = Client(f"http://127.0.0.1:{srv.server_port}", TENANT, headers={"User-Agent": "mine"})
        ua.related("x")
        assert seen[-1][2] == "mine"
    finally:
        srv.shutdown()


def test_client_takes_your_own_http_client():
    httpx = pytest.importorskip("httpx")
    from keel.client import Client, KeelError

    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["user-agent"], request.headers["x-keel-tenant"], json.loads(request.content)))
        if request.url.path.endswith("/annotate"):
            return httpx.Response(403, json={"detail": "not yours"})
        return httpx.Response(200, json=[{"rel": "mounted_on"}])

    keel = Client("https://keel.example/k", TENANT, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert keel.related("tire:T-1") == [{"rel": "mounted_on"}]
    with pytest.raises(KeelError) as err:
        keel.annotate("tire:T-1", "p", 1)
    assert (err.value.status, err.value.detail) == (403, "not yours")
    url, ua, tenant, body = seen[0]
    assert url == "https://keel.example/k/v1/related" and ua.startswith("keel-context/") and tenant == TENANT
    assert body["subject"] == "tire:T-1"


def _serve(*args, scheme="http"):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "keel.cli", "serve", FLEET, "--transport", "http", "--port", str(port), "--events", "examples/fleet/events.jsonl", *args],
        env={**os.environ, "KEEL_SECRET": SECRET}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc, f"{scheme}://127.0.0.1:{port}"


def _wait(client, base, proc):
    for _ in range(200):
        try:
            return client.get(base + "/health")
        except Exception:  # noqa: BLE001 - not up yet
            if proc.poll() is not None:
                raise RuntimeError("keel serve exited") from None
            time.sleep(0.05)
    raise RuntimeError("keel serve did not come up")


@pytest.mark.parametrize("tls", [False, True])
def test_serve_speaks_http2(tls, tmp_path):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("hypercorn")
    pytest.importorskip("h2")
    from keel.client import Client

    args = ["--http2"]
    if tls:
        import shutil

        if not shutil.which("openssl"):
            pytest.skip("openssl not found")
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        args += ["--certfile", str(cert), "--keyfile", str(key)]
    proc, base = _serve(*args, scheme="https" if tls else "http")
    try:
        # over TLS, h2 is negotiated by ALPN; in cleartext the client speaks h2c with prior knowledge
        h2 = httpx.Client(http2=True, http1=tls, verify=False)
        assert _wait(h2, base, proc).http_version == "HTTP/2"
        keel = Client(base, TENANT, secret=SECRET, http_client=h2)
        assert keel.context("tire:T-9001")["evaluation"]
        assert h2.post(base + "/v1/related", json={"subject": "unit:MXK-4821"}, headers={"Authorization": f"Bearer {SECRET}", "X-Keel-Tenant": TENANT}).http_version == "HTTP/2"
        if not tls:  # an HTTP/1.1 client still works against the same server
            assert Client(base, TENANT, secret=SECRET).related("unit:MXK-4821")
    finally:
        proc.kill()
