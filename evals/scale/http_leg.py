"""The transport's share of a tool call: the same ops in-process and over `keel serve --transport http`.

One SQLite database, filled once; the in-process Tools and the server read the same file. HTTP calls go
through one keep-alive httpx client on loopback, with `detail="full"` so both sides build the same answer.
"""

from __future__ import annotations

import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from evals.scale import harness
from keel import Ingestor, SqliteStore, Tools, load_cartridge
from keel.ingest import BATCH

HTTP_OPS = ("context_now", "context_as_of", "context_known_at", "history_subject", "related", "search_status_at")
ROUTE = {"context": "/v1/context", "history": "/v1/history", "related": "/v1/related", "search": "/v1/search"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _json(kw: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in kw.items()}


def bench(events: Path, out: Path, label: str, db: Path, per_op: int, seed: int = 1) -> None:
    import httpx

    cartridge = load_cartridge(harness.JIRA)
    for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
        p.unlink(missing_ok=True)
    store = SqliteStore(db)
    sampler = harness.SampleBuilder(seed)
    Ingestor(cartridge, store).ingest(sampler.tap(harness.read_events(events)), batch=BATCH)
    sample = sampler.sample()
    todo = [op for op in harness.ops(sample, random.Random(seed), per_op, per_op) if op.name in HTTP_OPS]
    random.Random(seed).shuffle(todo)
    tools = Tools(cartridge, store, sample.tenant)
    inproc = harness.run_ops(tools, store, todo, 60.0)
    port = _free_port()
    server = subprocess.Popen([sys.executable, "-m", "keel.cli", "serve", str(harness.JIRA), "--transport", "http", "--db", str(db),
                               "--port", str(port), "--tenant", sample.tenant], cwd=harness.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Keel-Tenant": sample.tenant}, timeout=120)
        for _ in range(100):
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)

        def call(op: harness.Op) -> Any:
            body = _json(op.kw)
            if op.name.startswith("context"):
                body["detail"] = "full"
            r = client.post(ROUTE[op.name.split("_")[0]], json=body)
            r.raise_for_status()
            return r.json()

        over_http = harness.run_ops(None, None, todo, 60.0, call_fn=call)  # type: ignore[arg-type]
    finally:
        server.terminate()
        server.wait(10)
    harness.emit(out, {"kind": "http", "label": label, "events": sample.count, "in_process": inproc, "http": over_http, "machine": harness.machine()})
