"""Postgres store: the same protocol as MemoryStore, on a database you already run, with the cartridge
compiled to SQL functions beside it.

Optional extra for the driver: `uv sync --extra postgres` (psycopg 3).

    store = PostgresStore.from_url("postgresql://keel:secret@db.internal:5432/keel")
    store = PostgresStore(lambda: psycopg.connect(dsn, autocommit=True, row_factory=dict_row))

Tables are created on first use (CREATE TABLE IF NOT EXISTS). Instants are ISO 8601 text in UTC and values
JSON text, exactly as SQLite stores them; every text column is `COLLATE "C"`, so ordering by key, subject or
value is code-point order, as in Python and SQLite. The SQL lives in `keel.store.sql.SqlStore`; the generated
functions (keel/sqlgen.py, Postgres dialect) let `search(rule=)` push a rule down, as in `DuckdbStore`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from keel import sqlgen
from keel.store.sql import SqlStore, _ts

T = 'TEXT COLLATE "C"'


class _Cursor:
    """psycopg's cursor, but a statement without parameters is sent as is (no `%` parsing)."""

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def execute(self, sql: str, params: Any = ()) -> None:
        self.cur.execute(sql, tuple(params)) if params else self.cur.execute(sql)

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self.cur.executemany(sql, [tuple(r) for r in rows])

    def fetchall(self) -> list[Any]:
        return self.cur.fetchall() if self.cur.description is not None else []

    @property
    def rowcount(self) -> int:
        return self.cur.rowcount

    @property
    def lastrowid(self) -> Any:
        return None


class PostgresStore(SqlStore):
    TIME = T
    PLACEHOLDER = "%s"
    ENTITY_ORDER = "seq"
    INDEX_IF_NOT_EXISTS = True
    PER_THREAD_READS = True
    SCHEMA = sqlgen.Postgres().schema()  # the generated tables: the compiled functions read them as the store writes them
    UPSERT_ENTITY = (
        "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant, kind, id) DO UPDATE SET ref = excluded.ref, attrs = excluded.attrs"
    )
    REMEMBER = "INSERT INTO ingested (tenant, id) VALUES (?, ?) ON CONFLICT DO NOTHING"
    TOMBSTONE = "INSERT INTO forgotten (tenant, at) VALUES (?, ?) ON CONFLICT DO NOTHING"
    METER = "INSERT INTO metering (tenant, day, item, n) VALUES (?, ?, ?, ?) ON CONFLICT (tenant, day, item) DO UPDATE SET n = metering.n + excluded.n"
    ADDED_FACT_COLUMNS = (
        ("task", T, "CREATE INDEX IF NOT EXISTS facts_task ON facts (tenant, task)"),
        ("idempotency_key", T, None),
        ("status", T, None),
        ("trace", T, None),
        ("cartridge", T, None),
        ("closed_at", T, "CREATE INDEX IF NOT EXISTS facts_closed ON facts (tenant, closed_at)"),
    )

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        self._installed: str | None = None
        self._macro_lock = threading.Lock()
        super().__init__(connect, prefix)
        if prefix:  # the generated functions read the unprefixed tables: Tools then evaluates rules in Python
            self.rule_subjects = None  # type: ignore[assignment]
        else:
            self.install(None)

    @classmethod
    def from_url(cls, url: str, prefix: str | None = None) -> PostgresStore:
        """postgresql://user:password@host:5432/database[?prefix=keel_], with psycopg 3."""
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise SystemExit("PostgresStore.from_url needs the optional dependency: uv sync --extra postgres") from exc
        u = urlparse(url)
        query = parse_qs(u.query)
        if prefix is None:
            prefix = query.pop("prefix", [""])[0]
        query.pop("prefix", None)
        dsn = urlunparse(u._replace(scheme="postgresql", query=urlencode(query, doseq=True)))
        return cls(lambda: psycopg.connect(dsn, autocommit=True, row_factory=dict_row), prefix)

    def _prepare(self, conn: Any) -> None:
        conn.autocommit = True
        conn.execute("SET TimeZone = 'UTC'")
        # Keel's statements are many and short: compiling one (JIT, on above a cost estimate) took 80-200 ms, more than running it
        conn.execute("SET jit = off")

    def _cursor(self, conn: Any) -> _Cursor:
        return _Cursor(conn.cursor())

    def _insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            n = self._exec(sql, params)
            cur = self._cursor(self.db)
            cur.execute("SELECT lastval() AS id")
            self._last_id = cur.fetchall()[0]["id"]
            return n

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._rows("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?",
                                              (self.prefix + table,))}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one("SELECT 1 AS one FROM pg_indexes WHERE schemaname = current_schema() AND indexname = ?", (self.prefix + name,)) is not None

    # ---- the compiled cartridge ---------------------------------------------------------

    def install(self, cartridge: Any) -> None:
        """(Re)create the point-in-time functions, and the cartridge's rule functions (None: none). Idempotent per cartridge."""
        ident = None if cartridge is None else f"{cartridge.ident}|{sorted(cartridge.predicates or {})}"
        if ident == self._installed and ident is not None:
            return
        with self._macro_lock, self._lock:
            empty = type("Cartridge", (), {"rules": {}, "predicates": {}, "relations": {}, "calendar": None})
            # one session-level lock: two processes installing at once would race on CREATE OR REPLACE
            self._exec("SELECT pg_advisory_lock(7413)")
            try:
                self._cursor(self.db).execute(sqlgen.macros(cartridge or empty, sqlgen.Postgres()))
            finally:
                self._exec("SELECT pg_advisory_unlock(7413)")
            self._installed = ident

    # subjects_where on Postgres: attrs read as jsonb by key
    JSON_TYPE, JSON_STR, JT_TEXT, JT_ARRAY = "jsonb_typeof(({0})::jsonb -> {1}::text)", "(({0})::jsonb ->> {1}::text)", "'string'", "'array'"
    ASCII = "(octet_length({0}) = length({0}))"
    JSON_PATH = "{}"
    INSTR = "strpos({0}, {1})"

    # the state itself is SqlStore's plain SQL: on Postgres it beats the generated state_known_at function, which
    # rebuilds every predicate's closes before filtering (10^6 events: 204 vs 426 ms stored, 469 vs 1,046 ms as known at)

    def rule_subjects(self, tenant: str, cartridge: Any, rule: str, as_of: datetime, known_at: datetime | None = None,
                      author: str | None = None, task: str | None = None) -> set[str]:
        """The entities `rule` holds for at `as_of` (as known at `known_at`), by its compiled function: what
        `Tools.search(rule=)` would find evaluating every candidate in Python (see DuckdbStore.rule_subjects)."""
        from keel.envelope import utcnow

        self.install(cartridge)
        lit = sqlgen.Postgres().lit
        alive = _ts(known_at or utcnow())
        sql = (f"SELECT subject FROM {sqlgen.rule_macro(rule)}_at({lit(tenant)}, {lit(_ts(as_of))}, k => {lit(_ts(known_at))}, "
               f"alive => {lit(alive)}, as_author => {lit(author)}, as_task => {lit(task)})")
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(sql)
            return {r["subject"] for r in cur.fetchall()}
