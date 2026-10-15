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

FLEET, TENANT, SECRET = "cartridges/fleet", "acme-logistics", "s3cret"


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
    assert report == {"processed": 3, "duplicates": 0, "unhandled": [], "errors": []}
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
    assert m.client(server, "someone-else", secret=SECRET).context("tire:T-9001")["entity"] is None
    with pytest.raises(KeelError) as err:
        m.client(server, TENANT, secret="wrong").search("x")
    assert err.value.status == 401
    assert m.RULES["tire_critical"]["threshold_mm"] == 3 and "tire" in m.KINDS


def test_generated_python_is_valid_for_every_cartridge(tmp_path):
    for cart in ("fleet", "crm", "finance", "clinics"):
        code = generate("python", load_cartridge(f"cartridges/{cart}"))
        compile(code, f"keel_{cart}.py", "exec")
        m = _load(tmp_path, code, f"keel_{cart}")
        assert m.CARTRIDGE == cart and len(m.KINDS) >= 3
    assert json.loads(json.dumps(m.RULES))  # rules survive as plain data
