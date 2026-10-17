"""MySQL / MariaDB store: the same protocol as MemoryStore, on a database you already run.

Optional extra for the driver: `uv sync --extra mysql` (PyMySQL). mysql-connector-python works
too: pass your own `connect` callable.

    store = MysqlStore.from_url("mysql://keel:secret@db.internal:3306/keel")
    store = MysqlStore(lambda: pymysql.connect(host=..., user=..., password=..., database=..., autocommit=True))
    keel serve examples/fleet --transport http --db mysql://keel:secret@db.internal:3306/keel?prefix=keel_

Tables are created on first use (CREATE TABLE IF NOT EXISTS). Timestamps are ISO 8601 strings in
UTC; JSON columns hold attrs and fact values. The SQL lives in `keel.store.sql.SqlStore`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from keel.store.sql import SqlStore


class MysqlStore(SqlStore):
    PLACEHOLDER = "%s"
    ENTITY_ORDER = "seq"
    PER_THREAD_READS = True
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS entities (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, kind VARCHAR(191) NOT NULL, id VARCHAR(191) NOT NULL,
      ref TEXT, attrs JSON NOT NULL, UNIQUE KEY entities_key (tenant, kind, id));
    CREATE TABLE IF NOT EXISTS relations (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, src VARCHAR(191) NOT NULL, rel VARCHAR(191) NOT NULL,
      dst VARCHAR(191) NOT NULL, valid_from VARCHAR(40) NOT NULL, valid_to VARCHAR(40), recorded_at VARCHAR(40), source TEXT,
      KEY relations_src (tenant, src), KEY relations_dst (tenant, dst));
    CREATE TABLE IF NOT EXISTS facts (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, subject VARCHAR(191) NOT NULL, predicate VARCHAR(191) NOT NULL,
      value JSON NOT NULL, valid_from VARCHAR(40) NOT NULL, valid_to VARCHAR(40), recorded_at VARCHAR(40), source TEXT,
      scope VARCHAR(16) NOT NULL, corrected_by TEXT, evidence TEXT, expires_at VARCHAR(40),
      task VARCHAR(191), idempotency_key VARCHAR(191), closed_at VARCHAR(40), status VARCHAR(16), trace TEXT,
      KEY facts_subject (tenant, subject), KEY facts_predicate (tenant, subject, predicate),
      KEY facts_recorded (tenant, recorded_at), KEY facts_closed (tenant, closed_at), KEY facts_task (tenant, task));
    CREATE TABLE IF NOT EXISTS ingested (tenant VARCHAR(191) NOT NULL, id VARCHAR(191) NOT NULL, PRIMARY KEY (tenant, id));
    CREATE TABLE IF NOT EXISTS forgotten (tenant VARCHAR(191) NOT NULL PRIMARY KEY, at VARCHAR(40) NOT NULL)
    """
    UPSERT_ENTITY = (
        "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE ref = VALUES(ref), attrs = VALUES(attrs)"
    )
    REMEMBER = "INSERT IGNORE INTO ingested (tenant, id) VALUES (?, ?)"
    TOMBSTONE = "INSERT IGNORE INTO forgotten (tenant, at) VALUES (?, ?)"
    ADDED_FACT_COLUMNS = (
        ("task", "VARCHAR(191)", "CREATE INDEX facts_task ON facts (tenant, task)"),
        ("idempotency_key", "VARCHAR(191)", None),
        ("status", "VARCHAR(16)", None),
        ("trace", "TEXT", None),
        ("closed_at", "VARCHAR(40)", "CREATE INDEX facts_closed ON facts (tenant, closed_at)"),
    )

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        self._pings: dict[int, float] = {}  # per connection: the shared one and each thread's reader
        super().__init__(connect, prefix)

    @classmethod
    def from_url(cls, url: str, prefix: str | None = None) -> MysqlStore:
        """mysql://user:password@host:3306/database[?prefix=keel_], with PyMySQL."""
        try:
            import pymysql
        except ImportError as exc:
            raise SystemExit("MysqlStore.from_url needs the optional dependency: uv sync --extra mysql") from exc
        u = urlparse(url)
        if prefix is None:
            prefix = parse_qs(u.query).get("prefix", [""])[0]
        return cls(
            lambda: pymysql.connect(
                host=u.hostname or "127.0.0.1",
                port=u.port or 3306,
                user=unquote(u.username or "root"),
                password=unquote(u.password or ""),
                database=u.path.lstrip("/"),
                autocommit=True,
                charset="utf8mb4",
            ),
            prefix,
        )

    def _prepare(self, conn: Any) -> None:
        try:
            conn.autocommit(True)  # PyMySQL: a method
        except TypeError:
            conn.autocommit = True  # mysql-connector: a property

    def _columns(self, table: str) -> set[str]:
        return {r["Field"] for r in self._rows(f"SHOW COLUMNS FROM {table}")}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one(f"SHOW INDEX FROM {table} WHERE Key_name = ?", (self.prefix + name,)) is not None

    def _cursor(self, conn: Any) -> Any:
        pymysql = type(conn).__module__.startswith("pymysql")
        now = time.monotonic()
        if now - self._pings.setdefault(id(conn), now) > 10:  # servers drop idle connections: reconnect after a pause, not per statement
            if pymysql:
                conn.ping()  # reconnects by default
            else:
                conn.ping(reconnect=True, attempts=1, delay=0)
        self._pings[id(conn)] = now
        if pymysql:
            import pymysql.cursors

            return conn.cursor(pymysql.cursors.DictCursor)
        return conn.cursor(dictionary=True, buffered=True)
