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


def test_a_large_tenant_is_refused_from_counts_alone(kind, tmp_path, monkeypatch):
    """Second review a6: 40,000 entities and 200,000 facts took 2.2-2.5 s and 520 MB per query after any write. Past
    the ceiling (now 100,000 rows) a tenant is refused from three counts, before any row is read."""
    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    assert q.COPY_MAX == 100_000
    monkeypatch.setattr(q, "COPY_MAX", 5)
    read: list[str] = []
    real = store._rows
    monkeypatch.setattr(store, "_rows", lambda sql, params=(): read.append(sql) or real(sql, params))
    with pytest.raises(ValueError, match="too large"):
        tools(c, store).query("SELECT 1 AS one")
    assert not any("FROM entities" in s and "count(" not in s for s in read)


def test_the_copy_counts_against_the_querys_timeout(kind, tmp_path, monkeypatch):
    """A copy that runs past the timeout ends the query as timed out (the copy is kept: the next query is quick)."""
    import time as _time

    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    box_type = type(store._sandboxes.make())
    real_load = box_type.load
    monkeypatch.setattr(box_type, "load", lambda self, *a: _time.sleep(1.2) or real_load(self, *a))
    t = tools(c, store, query_timeout=1)
    start = _time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        t.query("SELECT 1 AS one")
    assert _time.monotonic() - start < 2.5
    monkeypatch.setattr(box_type, "load", real_load)
    assert t.query("SELECT 1 AS one") == [{"one": 1}]


def test_a_forget_in_another_process_drops_this_ones_copy(tmp_path):
    """Second review s1: forget run by another worker (or the CLI) on the same SQLite file left this process's copy of
    the forgotten tenant in memory. When another process has written, the next query here (for any tenant) drops
    every held copy whose tenant changed."""
    from keel import SqliteStore

    c = cartridge()
    a = populate(SqliteStore(tmp_path / "keel.db"), c)
    b = SqliteStore(tmp_path / "keel.db")
    tools(c, a, RIVAL).query("SELECT count(*) AS n FROM facts")
    tools(c, a).query("SELECT 1 AS one")
    box = a._sandboxes.boxes[RIVAL]
    b.forget(RIVAL)
    tools(c, a).query("SELECT 1 AS one")
    assert RIVAL not in a._sandboxes.boxes and box.closed
    assert TENANT in a._sandboxes.boxes  # unchanged: kept
    assert tools(c, a, RIVAL).query("SELECT count(*) AS n FROM facts") == [{"n": 1}]  # its own global memory: GLOBAL-OK only
