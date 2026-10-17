"""SQLite store: the same protocol as MemoryStore, on one file.

Durable with no new dependency (stdlib `sqlite3`). One file per deployment, one subgraph per
tenant inside it, WAL mode so a `keel serve` process can read while an ingest process writes.

    store = SqliteStore("keel.db")
    Ingestor(cartridge, store).ingest(adapter.follow())

The SQL lives in `keel.store.sql.SqlStore`; this file is the dialect. `MysqlStore` is the other one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from keel.store.sql import SqlStore


class SqliteStore(SqlStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS entities (
      tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, ref TEXT, attrs TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY (tenant, kind, id));
    CREATE TABLE IF NOT EXISTS relations (
      seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
      valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT);
    CREATE INDEX IF NOT EXISTS relations_src ON relations (tenant, src);
    CREATE INDEX IF NOT EXISTS relations_dst ON relations (tenant, dst);
    CREATE TABLE IF NOT EXISTS facts (
      seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
      value TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT,
      scope TEXT NOT NULL, corrected_by TEXT, evidence TEXT, expires_at TEXT,
      task TEXT, idempotency_key TEXT, closed_at TEXT, status TEXT, trace TEXT);
    CREATE INDEX IF NOT EXISTS facts_subject ON facts (tenant, subject);
    CREATE INDEX IF NOT EXISTS facts_recorded ON facts (tenant, recorded_at);
    CREATE TABLE IF NOT EXISTS ingested (tenant TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (tenant, id));
    CREATE TABLE IF NOT EXISTS forgotten (tenant TEXT NOT NULL PRIMARY KEY, at TEXT NOT NULL)
    """
    UPSERT_ENTITY = (
        "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant, kind, id) DO UPDATE SET ref = excluded.ref, attrs = excluded.attrs"
    )
    REMEMBER = "INSERT OR IGNORE INTO ingested (tenant, id) VALUES (?, ?)"
    TOMBSTONE = "INSERT OR IGNORE INTO forgotten (tenant, at) VALUES (?, ?)"
    INDEX_IF_NOT_EXISTS = True
    ADDED_FACT_COLUMNS = (
        ("task", "TEXT", "CREATE INDEX IF NOT EXISTS facts_task ON facts (tenant, task)"),
        ("idempotency_key", "TEXT", None),
        ("status", "TEXT", None),
        ("trace", "TEXT", None),
        ("closed_at", "TEXT", "CREATE INDEX IF NOT EXISTS facts_closed ON facts (tenant, closed_at)"),
    )

    def __init__(self, path: str | Path = ":memory:", prefix: str = "") -> None:
        def connect() -> sqlite3.Connection:
            # autocommit: one primitive, one transaction. check_same_thread off because the store
            # serializes statements itself and Python's sqlite3 is built serialized.
            conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

        super().__init__(connect, prefix)

    def _prepare(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        # ponytail: fixed sizes; the defaults (2 MB, no mmap) send a large database to the OS per page read
        conn.execute("PRAGMA cache_size=-262144")  # 256 MB of pages, allocated as used
        conn.execute("PRAGMA mmap_size=2147483648")  # 2 GB mapped (SQLite's default cap)

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._rows(f"PRAGMA table_info({table})")}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one("SELECT 1 AS one FROM sqlite_master WHERE type = 'index' AND name = ?", (self.prefix + name,)) is not None
