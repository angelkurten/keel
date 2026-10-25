"""SQLite store: the same protocol as MemoryStore, on one file.

Durable with no new dependency (stdlib `sqlite3`). One file per deployment, one subgraph per
tenant inside it, WAL mode so a `keel serve` process can read while an ingest process writes.

    store = SqliteStore("keel.db")
    Ingestor(cartridge, store).ingest(adapter.follow())

The SQL lives in `keel.store.sql.SqlStore`; this file is the dialect. `MysqlStore` is the other one.
A cartridge's rules compile to SQL (keel/sqlgen.py, SQLite dialect), so `search(rule=)` pushes a rule down
to one statement (`rule_subjects`) instead of evaluating every entity of its kind in Python.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from keel import sqlgen
from keel.store.sql import SqlStore, _ts


class SqliteStore(SqlStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS entities (
      tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, ref TEXT, attrs TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY (tenant, kind, id));
    CREATE TABLE IF NOT EXISTS relations (
      seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
      valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT, closed_at TEXT);
    CREATE INDEX IF NOT EXISTS relations_src ON relations (tenant, src);
    CREATE INDEX IF NOT EXISTS relations_dst ON relations (tenant, dst);
    CREATE TABLE IF NOT EXISTS facts (
      seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
      value TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT,
      scope TEXT NOT NULL, corrected_by TEXT, evidence TEXT, expires_at TEXT,
      task TEXT, idempotency_key TEXT, closed_at TEXT, status TEXT, trace TEXT, cartridge TEXT);
    CREATE INDEX IF NOT EXISTS facts_subject ON facts (tenant, subject);
    CREATE INDEX IF NOT EXISTS facts_recorded ON facts (tenant, recorded_at);
    CREATE TABLE IF NOT EXISTS ingested (tenant TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (tenant, id));
    CREATE TABLE IF NOT EXISTS forgotten (tenant TEXT NOT NULL PRIMARY KEY, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS api_keys (
      id TEXT NOT NULL PRIMARY KEY, hash TEXT NOT NULL UNIQUE, account TEXT NOT NULL, tenant TEXT, agent TEXT,
      glob INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, revoked_at TEXT);
    CREATE TABLE IF NOT EXISTS metering (
      tenant TEXT NOT NULL, day TEXT NOT NULL, item TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (tenant, day, item));
    CREATE TABLE IF NOT EXISTS cutoffs (tenant TEXT NOT NULL PRIMARY KEY, cut TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS accounts (id TEXT NOT NULL PRIMARY KEY, rps INTEGER, events_month INTEGER, store TEXT, retention TEXT)
    """
    UPSERT_ENTITY = (
        "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant, kind, id) DO UPDATE SET ref = excluded.ref, attrs = excluded.attrs"
    )
    REMEMBER = "INSERT OR IGNORE INTO ingested (tenant, id) VALUES (?, ?)"
    TOMBSTONE = "INSERT OR IGNORE INTO forgotten (tenant, at) VALUES (?, ?)"
    METER = "INSERT INTO metering (tenant, day, item, n) VALUES (?, ?, ?, ?) ON CONFLICT (tenant, day, item) DO UPDATE SET n = n + excluded.n"
    INDEX_IF_NOT_EXISTS = True
    ADDED_FACT_COLUMNS = (
        ("task", "TEXT", "CREATE INDEX IF NOT EXISTS facts_task ON facts (tenant, task)"),
        ("idempotency_key", "TEXT", None),
        ("status", "TEXT", None),
        ("trace", "TEXT", None),
        ("cartridge", "TEXT", None),
        ("closed_at", "TEXT", "CREATE INDEX IF NOT EXISTS facts_closed ON facts (tenant, closed_at)"),
    )

    def __init__(self, path: str | Path = ":memory:", prefix: str = "") -> None:
        def connect() -> sqlite3.Connection:
            # autocommit: one primitive, one transaction. check_same_thread off because the store
            # serializes statements itself and Python's sqlite3 is built serialized.
            conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

        self._compiled: tuple[str, sqlgen.SQLite] | None = None
        self._compile_lock = threading.Lock()
        super().__init__(connect, prefix)
        # the expanded rule SQL names the unprefixed tables; window functions, FILTER, IS DISTINCT FROM need 3.39
        if prefix or sqlite3.sqlite_version_info < sqlgen.SQLite.MIN_VERSION:
            self.rule_subjects = None  # type: ignore[assignment]

    def _prepare(self, conn: sqlite3.Connection) -> None:
        sqlgen.SQLite.register(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        # ponytail: fixed sizes; the defaults (2 MB, no mmap) send a large database to the OS per page read
        conn.execute("PRAGMA cache_size=-262144")  # 256 MB of pages, allocated as used
        conn.execute("PRAGMA mmap_size=2147483648")  # 2 GB mapped (SQLite's default cap)

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._rows(f"PRAGMA table_info({table})")}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one("SELECT 1 AS one FROM sqlite_master WHERE type = 'index' AND name = ?", (self.prefix + name,)) is not None

    # ---- the compiled cartridge -------------------------------------------------------

    def install(self, cartridge: Any) -> sqlgen.SQLite:
        """The cartridge's rules compiled for SQLite (keel/sqlgen.py), kept until the cartridge changes."""
        ident = f"{cartridge.ident}|{sorted(cartridge.predicates or {})}|{cartridge.rules!r}|{cartridge.relations!r}|{cartridge.calendar!r}"
        with self._compile_lock:
            if self._compiled is None or self._compiled[0] != ident:
                self._compiled = (ident, sqlgen.SQLite.compile(cartridge))
            return self._compiled[1]

    @property
    def compiled(self) -> sqlgen.SQLite:
        assert self._compiled is not None, "install(cartridge) first"
        return self._compiled[1]

    def rule_subjects(self, tenant: str, cartridge: Any, rule: str, as_of: datetime, known_at: datetime | None = None,
                      author: str | None = None, task: str | None = None) -> set[str] | None:
        """The entities `rule` holds for at `as_of` (as known at `known_at`), by its compiled SQL: what
        `Tools.search(rule=)` would find evaluating every candidate in Python (see DuckdbStore.rule_subjects).
        None (Tools evaluates it in Python) for a rule that does not compile to SQL."""
        from keel.envelope import utcnow

        compiled = self.install(cartridge)
        if rule in compiled.skipped or f"{rule}_at" not in compiled.defs:
            return None
        lit = compiled.lit
        sql = (f"SELECT subject FROM {sqlgen.rule_macro(rule)}_at({lit(tenant)}, {lit(_ts(as_of))}, k := {lit(_ts(known_at))}, "
               f"alive := {lit(_ts(known_at or utcnow()))}, as_author := {lit(author)}, as_task := {lit(task)})")
        return {r[0] for r in self._rows(compiled.expand(sql))}
