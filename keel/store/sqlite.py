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
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from keel import sqlgen
from keel.scopes import is_global
from keel.store import query as q
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
        self._sandboxes = q.Sandboxes(_Sandbox, self.SANDBOXES)
        self._seen_version: int | None = None  # PRAGMA data_version when the sandboxes were last checked  # query: one per tenant, refreshed when its rows change
        super().__init__(connect, prefix)
        # the expanded rule SQL names the unprefixed tables; window functions, FILTER, IS DISTINCT FROM need 3.39
        if prefix or sqlite3.sqlite_version_info < sqlgen.SQLite.MIN_VERSION:
            self.rule_subjects = None  # type: ignore[assignment]
            self.query_rows = None  # type: ignore[assignment]

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
        ident = f"{cartridge.ident}|{sorted(cartridge.predicates or {})}|{cartridge.rules!r}|{cartridge.relations!r}|{cartridge.calendar!r}|{cartridge.entities!r}"
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

    # ---- query: the tenant's rows in a sandbox of their own -------------------------------

    QUERY_DIALECT = "sqlite"
    SANDBOXES = 8  # tenants whose sandbox is kept; the least recently queried is dropped past it
    EXPANDED_MAX = 200_000  # characters an agent's query may grow to with its macros inlined (the largest one alone: ~5,000)

    def query_rows(self, tenant: str, cartridge: Any, sql: str, cap: int, timeout: float) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        """An agent's read-only SQL over the generated views, for `tenant` only (Tools.query). It runs in an in-memory
        SQLite database that holds nothing but what the tenant may read (keel/store/query.py `visible`), with the
        cartridge's macros inlined (`SQLite.expand`), behind an authorizer that allows reading those three tables and
        calling functions (not extension or file ones), nothing else: no PRAGMA, ATTACH, write or schema change. A
        progress handler stops it at `timeout` seconds. At most `cap` rows; `more` when there were others."""
        from keel.envelope import utcnow

        compiled = self.install(cartridge)
        now = _ts(utcnow())
        self._sweep(now)
        box = self._sandboxes.acquire(tenant)
        try:
            deadline = time.monotonic() + timeout  # the copy counts against the query's timeout
            q.refresh(self, box, tenant, cartridge, self._changes(), now, deadline)
            try:
                expanded = compiled.expand(sql, budget=[self.EXPANDED_MAX])
            except (ValueError, IndexError) as exc:  # an unbalanced quote or parenthesis
                raise ValueError(f"query does not parse: {exc}") from exc
            return box.run(expanded, cap, max(0.05, deadline - time.monotonic()))
        finally:
            box.lock.release()

    def _sweep(self, now: str) -> None:
        """When another process has written to the file (a forget by another worker or the CLI), drop every held copy
        whose tenant changed, so no copy of forgotten data outlives the next query here, whichever tenant it is for."""
        version = self._changes()[1]
        if version == self._seen_version:
            return
        self._seen_version = version
        for tenant, box in list(self._sandboxes.boxes.items()):
            try:
                stale = box.mark is None or q.fingerprint(self, tenant, now) != box.mark[0]
            except ValueError:  # grew past the ceiling
                stale = True
            if stale:
                self._sandboxes.drop(tenant)

    def _changes(self) -> tuple[int, int]:
        """A counter of every write to the database: this connection's own (total_changes) and other processes'
        (data_version): while it stands still, no sandbox needs even a fingerprint."""
        with self._lock:
            return self.db.total_changes, self.db.execute("PRAGMA data_version").fetchone()[0]

    # what forgets a tenant's data forgets its sandbox's copy too (the global partition: every tenant's)
    def forget(self, tenant: str) -> dict[str, int]:
        out = super().forget(tenant)
        self._sandboxes.drop(None if is_global(tenant) else tenant)
        return out

    def forget_before(self, tenant: str, before: datetime) -> dict[str, int]:
        out = super().forget_before(tenant, before)
        self._sandboxes.drop(None if is_global(tenant) else tenant)
        return out

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        out = super().expire(tenant, now)
        self._sandboxes.drop(None if is_global(tenant) else tenant)
        return out

    def reset(self, tenant: str, keep_annotations: bool = False) -> None:
        super().reset(tenant, keep_annotations)
        self._sandboxes.drop(None if is_global(tenant) else tenant)


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_TABLES = {"facts", "entities", "relations"}
_NO_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "sqlite_dbpage", "zeroblob", "randomblob"}


def _authorize(action: int, a: str | None, b: str | None, db: str | None, source: str | None) -> int:
    """A query's sandbox: read the three tables and call functions; everything else is denied."""
    if action not in _ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and db is not None and (a not in _TABLES or db != "main"):  # db None: a CTE's rows
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (b or "").lower() in _NO_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class _Sandbox(q.Box):
    """One tenant's readable rows in a private in-memory SQLite database (SqliteStore.query_rows)."""

    def __init__(self) -> None:
        super().__init__()
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        if hasattr(self.conn, "setlimit"):  # Python 3.11+: no string or blob past a row's budget (printf('%500000000d'))
            self.conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, q.ROW_MAX)
        sqlgen.SQLite.register(self.conn)
        for table, cols in (("facts", sqlgen.FACT_COLUMNS), ("entities", q.ENTITY_COLUMNS), ("relations", q.RELATION_COLUMNS)):
            self.conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
        self.conn.execute("CREATE INDEX facts_subject ON facts (tenant, subject)")
        self.conn.execute("CREATE INDEX relations_src ON relations (tenant, src)")

    def close(self) -> None:
        self.conn.close()

    def load(self, tables: dict[str, tuple[tuple[str, ...], list[tuple[Any, ...]]]], cartridge: Any = None) -> None:
        self.conn.execute("BEGIN")
        for table, (cols, rows) in tables.items():
            self.conn.execute(f"DELETE FROM {table}")
            self.conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", rows)
        self.conn.execute("COMMIT")

    def run(self, sql: str, cap: int, timeout: float) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        deadline = time.monotonic() + timeout
        self.conn.set_authorizer(_authorize)
        self.conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            cur = self.conn.execute(sql)
            cols = [d[0] for d in cur.description or ()]
            got, more = q.take(cur.fetchmany, cap, deadline)
            cur.close()
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc):
                raise ValueError(f"query timed out after {timeout:g} s: narrow it, or aggregate") from exc
            raise ValueError(f"query failed: {q.clean_error(exc)}") from exc
        except (sqlite3.Error, sqlite3.Warning, OverflowError, MemoryError) as exc:
            raise ValueError(f"query failed: {q.clean_error(exc)}") from exc
        finally:
            self.conn.set_authorizer(None)
            self.conn.set_progress_handler(None, 0)
        return cols, got, more
