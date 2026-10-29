"""The per-tenant sandboxes SqliteStore and DuckdbStore answer `query` from (keel/store/query.py): what the security
review found in their lifecycle, each with its repro.

- forget, forget_before, expire and reset drop the tenant's sandbox: no copy of forgotten data stays in RAM or on disk;
- evicting a sandbox never closes it under a query another thread is running, and leaves no file behind;
- a query copies the tenant again only when its rows changed, never because another tenant wrote, never under the
  store's main lock, and refuses a tenant too large to copy instead of stalling.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

import pytest
from test_query import RIVAL, TENANT, cartridge, make_store, populate

from keel import Tools
from keel.store import query as q

KINDS = ["sqlite", "duckdb"]
AT = datetime(2026, 9, 3, tzinfo=timezone.utc)


@pytest.fixture(params=KINDS)
def kind(request: Any) -> str:
    return request.param


def tools(c: Any, store: Any, tenant: str = TENANT, **kw: Any) -> Tools:
    return Tools(c, store, tenant, query=True, **kw)


def files_with(folder: str | None, needle: bytes) -> list[str]:
    out = []
    for root, _, names in os.walk(folder or "/nonexistent"):
        for n in names:
            with open(os.path.join(root, n), "rb") as fh:
                if needle in fh.read():
                    out.append(n)
    return out


@pytest.mark.parametrize("how", ["forget", "forget_before", "expire", "reset"])
def test_forgetting_a_tenant_drops_its_sandbox(kind, tmp_path, how):
    """Review a4: after forget the DuckDB sandbox file (and SQLite's in-memory copy) still held the forgotten data."""
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    tools(c, store, RIVAL).query("SELECT count(*) AS n FROM facts")
    assert RIVAL in store._sandboxes.boxes
    {"forget": lambda: store.forget(RIVAL), "forget_before": lambda: store.forget_before(RIVAL, datetime(2030, 1, 1, tzinfo=timezone.utc)),
     "expire": lambda: store.expire(RIVAL), "reset": lambda: store.reset(RIVAL)}[how]()
    assert RIVAL not in store._sandboxes.boxes
    assert files_with(getattr(store, "_sandbox_dir", None), b"RIVAL-SECRET") == []


def test_expiring_the_global_partition_drops_every_sandbox(kind, tmp_path):
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    tools(c, store).query("SELECT 1 AS one")
    store.expire("__global__")
    assert store._sandboxes.boxes == {}


def test_eviction_never_closes_a_sandbox_in_use(kind, tmp_path, monkeypatch):
    """Review a4: a long query while other tenants evicted its sandbox got ResourceInUse/None errors (HTTP 500)."""
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    store._sandboxes.keep = 2
    for i in range(8):
        store.upsert_entity(f"t{i}", "tire", "x", ref="r")
    errors: list[str] = []

    def long() -> None:
        try:
            tools(c, store, query_timeout=3).query("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3000000) "
                                                   "SELECT count(*) AS n FROM r")
        except ValueError as exc:
            if "timed out" not in str(exc):
                errors.append(f"long: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"long: {type(exc).__name__}: {exc}")

    def other(i: int) -> None:
        try:
            assert tools(c, store, f"t{i}").query("SELECT 1 AS one") == [{"one": 1}]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"t{i}: {type(exc).__name__}: {exc}")

    th = threading.Thread(target=long)
    th.start()
    others = [threading.Thread(target=other, args=(i,)) for i in range(8)]
    for t in others:
        t.start()
    for t in others:
        t.join()
    th.join()
    assert errors == []
    assert tools(c, store).query("SELECT count(*) AS n FROM entities")[0]["n"] > 0
    assert len(store._sandboxes.boxes) <= 2
    if kind == "duckdb":
        left = [n for n in os.listdir(store._sandbox_dir) if n.endswith(".duckdb")] if os.path.isdir(store._sandbox_dir) else []
        assert len(left) <= 2


def test_a_write_elsewhere_does_not_copy_this_tenant_again(kind, tmp_path, monkeypatch):
    """Review a6: every query after any write fingerprinted and copied the whole tenant again."""
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    t = tools(c, store)
    t.query("SELECT 1 AS one")
    copies, prints = [], []
    real_visible, real_print = q.visible, q.fingerprint
    monkeypatch.setattr(q, "visible", lambda *a, **k: copies.append(1) or real_visible(*a, **k))
    monkeypatch.setattr(q, "fingerprint", lambda *a, **k: prints.append(1) or real_print(*a, **k))
    t.query("SELECT 1 AS one")
    assert prints == [] and copies == []  # nothing written anywhere: not even a fingerprint
    store.open_fact(RIVAL, "tire:T-9001", "depth_mm", 4.0, valid_from=AT, source="s", evidence="e")
    t.query("SELECT 1 AS one")
    assert copies == []  # another tenant wrote: this one is not copied again
    store.open_fact(TENANT, "tire:T-9001", "depth_mm", 4.0, valid_from=AT, source="s", evidence="e")
    assert t.query("SELECT count(*) AS n FROM facts WHERE value = '4.0'") == [{"n": 1}]
    assert copies == [1]


def test_a_copy_never_holds_the_stores_main_lock(kind, tmp_path, monkeypatch):
    """Review: DuckdbStore held its main lock for the whole copy, so every other read and write waited."""
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    held = []
    real_load = type(store._sandboxes.make()).load
    monkeypatch.setattr(type(store._sandboxes.make()), "load", lambda self, *a: held.append(store._lock._is_owned()) or real_load(self, *a))
    tools(c, store).query("SELECT 1 AS one")
    assert held == [False]


def test_a_tenant_too_large_to_copy_is_refused_clearly(kind, tmp_path, monkeypatch):
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    monkeypatch.setattr(q, "COPY_MAX", 5)
    with pytest.raises(ValueError, match="(?i)too large|rows"):
        tools(c, store).query("SELECT 1 AS one")


def test_the_duckdb_sandbox_never_spills_to_disk(tmp_path):
    """Review a5: a large sort spilled gigabytes into the sandbox folder; with no temp directory it fails in memory."""
    pytest.importorskip("duckdb")
    c = cartridge()
    store = populate(make_store("duckdb", tmp_path), c)
    out = tools(c, store, query_timeout=5)
    with pytest.raises(ValueError):
        out.query("WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 50000000) "
                  "SELECT count(*) AS n FROM (SELECT i, md5(i::VARCHAR) || md5((i + 1)::VARCHAR) AS h FROM r ORDER BY h)")
    assert sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(store._sandbox_dir) for n in ns) < 50 * 2**20
    assert out.query("SELECT current_setting('temp_directory') AS d") == [{"d": ""}]
