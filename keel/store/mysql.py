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
import unicodedata
import warnings
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from keel.store.sql import SqlStore


def _fold(key: str) -> str:
    """What utf8mb4_0900_ai_ci compares: no case, no accents."""
    return "".join(c for c in unicodedata.normalize("NFKD", key) if not unicodedata.combining(c)).casefold()


class MysqlStore(SqlStore):
    PLACEHOLDER = "%s"
    ENTITY_ORDER = "seq"
    PER_THREAD_READS = True
    # keys compare exactly: under the server default (`utf8mb4_0900_ai_ci`) `pagina/RENDIMIENTO` and
    # `pagina/Rendimiento` were one entity, and Número one with Numero
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS entities (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, kind VARCHAR(191) NOT NULL, id VARCHAR(191) NOT NULL,
      ref TEXT, attrs JSON NOT NULL, UNIQUE KEY entities_key (tenant, kind, id)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
    CREATE TABLE IF NOT EXISTS relations (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, src VARCHAR(191) NOT NULL, rel VARCHAR(191) NOT NULL,
      dst VARCHAR(191) NOT NULL, valid_from VARCHAR(40) NOT NULL, valid_to VARCHAR(40), recorded_at VARCHAR(40), source TEXT,
      KEY relations_src (tenant, src), KEY relations_dst (tenant, dst)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
    CREATE TABLE IF NOT EXISTS facts (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, tenant VARCHAR(191) NOT NULL, subject VARCHAR(191) NOT NULL, predicate VARCHAR(191) NOT NULL,
      value JSON NOT NULL, valid_from VARCHAR(40) NOT NULL, valid_to VARCHAR(40), recorded_at VARCHAR(40), source TEXT,
      scope VARCHAR(16) NOT NULL, corrected_by TEXT, evidence TEXT, expires_at VARCHAR(40),
      task VARCHAR(191), idempotency_key VARCHAR(191), closed_at VARCHAR(40), status VARCHAR(16), trace TEXT,
      KEY facts_subject (tenant, subject), KEY facts_predicate (tenant, subject, predicate),
      KEY facts_recorded (tenant, recorded_at), KEY facts_closed (tenant, closed_at), KEY facts_task (tenant, task)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
    CREATE TABLE IF NOT EXISTS ingested (tenant VARCHAR(191) NOT NULL, id VARCHAR(191) NOT NULL, PRIMARY KEY (tenant, id)) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
    CREATE TABLE IF NOT EXISTS forgotten (tenant VARCHAR(191) NOT NULL PRIMARY KEY, at VARCHAR(40) NOT NULL) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
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

    TABLES = ("entities", "relations", "facts", "ingested", "forgotten")

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        self._pings: dict[int, float] = {}  # per connection: the shared one and each thread's reader
        super().__init__(connect, prefix)
        self._exact_keys()

    def _collation(self, table: str) -> str | None:
        row = self._one("SELECT TABLE_COLLATION AS c FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?", (self.prefix + table,))
        return row and row["c"]

    def _exact_keys(self) -> None:
        """Tables created before 0.24.2 took the server's collation, which compares keys without case or
        accents: convert them to utf8mb4_bin (once; it rewrites the table). Entities that already collided
        stay one row under the first key; the warning names them so the source can publish them again."""
        old = [t for t in self.TABLES if self._collation(t) != "utf8mb4_bin"]
        for t in old:
            self._ddl(f"ALTER TABLE {t} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_bin", lambda t=t: self._collation(t) == "utf8mb4_bin")
        if "entities" in old or "facts" in old:
            lost = self.collided()
            if lost:
                sample = ", ".join(f"{t}/{s}" for t, s in lost[:5])
                warnings.warn(f"{len(lost)} subjects have facts but no entity, most likely merged with a key that differs only in case or accents "
                              f"before 0.24.2 (e.g. {sample}): publish their entities again (MysqlStore.collided() lists them)", stacklevel=2)

    def collided(self) -> list[tuple[str, str]]:
        """(tenant, subject) with facts and no entity of its own, but an entity whose key differs only in case
        or accents: what the old collation merged into that key."""
        # two index scans and set work in Python: a join on CONCAT(kind, ':', id) can use no index
        have = {(r["tenant"], f"{r['kind']}:{r['id']}") for r in self._rows("SELECT tenant, kind, id FROM entities")}
        alike = {(t, _fold(k)) for t, k in have}
        subjects = {(r["tenant"], r["subject"]) for r in self._rows("SELECT DISTINCT tenant, subject FROM facts")}
        return sorted((t, s) for t, s in subjects - have if (t, _fold(s)) in alike)

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
        idle = now - self._pings.setdefault(id(conn), now) > 10  # servers drop idle connections: check after a pause, not per statement
        if idle or (pymysql and not conn.open):  # PyMySQL closes it when a statement loses the server
            try:
                conn.ping()  # PyMySQL >= 1.1 no longer reconnects here: it raises "Already closed"
            except Exception as exc:  # gone: open a new session (a server that is down raises from here)
                if self._in_tx and conn is self.db:  # a new session would autocommit the rest of the transaction
                    raise ConnectionError("mysql: the connection dropped inside a transaction") from exc
                conn.connect() if pymysql else conn.reconnect(attempts=1, delay=0)
                self._prepare(conn)
        self._pings[id(conn)] = now
        if pymysql:
            import pymysql.cursors

            return conn.cursor(pymysql.cursors.DictCursor)
        return conn.cursor(dictionary=True, buffered=True)
