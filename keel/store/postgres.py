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

import contextlib
import hashlib
import hmac
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from keel import sqlgen
from keel.scopes import Scope, global_of
from keel.store import query as q
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

    def __init__(self, connect: Callable[[], Any], prefix: str = "", dsn: str | None = None) -> None:
        self._installed: str | None = None
        self._macro_lock = threading.Lock()
        self._dsn = dsn  # query: the query role logs in with it (from_url passes it)
        self._query_conn: Any = None
        self._query_role: tuple[str, str] | None = None
        self._query_lock = threading.Lock()
        super().__init__(connect, prefix)
        if prefix or dsn is None:  # query logs a role of its own in, to views over the unprefixed tables
            self.query_rows = None  # type: ignore[assignment]
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
        return cls(lambda: psycopg.connect(dsn, autocommit=True, row_factory=dict_row), prefix, dsn=dsn)

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
        ident = None if cartridge is None else f"{cartridge.ident}|{sorted(cartridge.predicates or {})}|{cartridge.entities!r}|{cartridge.relations!r}"
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
    JT_NULL, JT_NUMBER, JT_BOOL = "'null'", "'number'", "'boolean'"
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

    # ---- query: a login role that reads only views bound to its session ------------------

    QUERY_DIALECT = "postgres"

    def query_rows(self, tenant: str, cartridge: Any, sql: str, cap: int, timeout: float) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        """An agent's read-only SQL over the generated functions, for `tenant` only (Tools.query). It runs on a
        connection of its own, logged in as a role (`_query_setup`) that may read nothing but three security-barrier
        views over facts, entities and relations, each showing only the rows the tenant may read, bound to that
        connection's backend in a table the role cannot read. Its search_path puts the views where the generated
        functions look for the tables, so every macro reads through them. Since it logs in as that role, no
        `SET ROLE` or `set_config('role', ...)` leaves it; it writes nothing (no privilege), and every query runs in a
        transaction rolled back after it, under `statement_timeout`. One statement only (a prepared statement).
        At most `cap` rows; `more` when there were others."""
        import psycopg

        self.install(cartridge)
        with self._query_lock:
            conn = self._query_connection()
            pid = conn.info.backend_pid
            self._exec("INSERT INTO keel_query_bound (pid, tenant, glob) VALUES (?, ?, ?) "
                       "ON CONFLICT (pid) DO UPDATE SET tenant = excluded.tenant, glob = excluded.glob", (pid, tenant, global_of(tenant)))
            try:
                cur = conn.cursor()
                cur.execute(f"SET LOCAL statement_timeout = {max(1, int(timeout * 1000))}")
                cur.execute(sql, prepare=True)
                if cur.description is None:
                    raise ValueError("query reads only: one SELECT (or WITH ... SELECT) statement over the generated views")
                got = cur.fetchmany(cap + 1)
                cols = [d.name for d in cur.description]
            except psycopg.errors.QueryCanceled as exc:
                raise ValueError(f"query timed out after {timeout:g} s: narrow it, or aggregate") from exc
            except psycopg.Error as exc:
                raise ValueError(f"query failed: {q.clean_error(exc)}") from exc
            finally:
                try:
                    conn.rollback()  # undoes set_config, temp tables, LISTEN, NOTIFY; not a session advisory lock
                    conn.execute("SELECT pg_advisory_unlock_all()")
                    conn.execute("RESET ALL")
                    conn.commit()
                except psycopg.Error:  # the connection is gone: the next query opens another
                    conn.close()
                    self._query_conn = None
                self._exec("DELETE FROM keel_query_bound WHERE pid = ?", (pid,))
            return cols, [tuple(r) for r in got[:cap]], len(got) > cap

    def _query_connection(self) -> Any:
        import psycopg
        from psycopg.conninfo import make_conninfo

        if self._query_conn is not None and not self._query_conn.closed:
            return self._query_conn
        role, password = self._query_setup()
        self._query_conn = psycopg.connect(make_conninfo(self._dsn, user=role, password=password), autocommit=False)
        return self._query_conn

    def _query_setup(self) -> tuple[str, str]:
        """The query role and its views, created or brought up to date (idempotent; the store's user needs CREATEROLE
        once). The role's name derives from the database, schema and user, its password from the store's own
        credentials (an HMAC), so every process of one deployment agrees on both and none needs a stored secret."""
        if self._query_role is not None:
            return self._query_role
        from psycopg.conninfo import conninfo_to_dict

        here = self._one("SELECT current_database() AS db, current_schema() AS s, current_user AS u")
        schema = here["s"]
        role = "keel_q_" + hashlib.sha256(f"{here['db']}|{schema}|{here['u']}".encode()).hexdigest()[:12]
        secret = str(conninfo_to_dict(self._dsn).get("password") or self._dsn)
        password = hmac.new(secret.encode(), role.encode(), hashlib.sha256).hexdigest()
        qs, s = f'"{role}"', f'"{schema}"'
        bound = f"(SELECT {{}} FROM {s}.keel_query_bound WHERE pid = pg_backend_pid())"
        t, g = Scope.TENANT.value, Scope.GLOBAL.value
        with self._lock:
            self._exec("SELECT pg_advisory_lock(7414)")
            try:
                if self._one("SELECT 1 AS one FROM pg_roles WHERE rolname = ?", (role,)) is None:
                    self._exec(f"CREATE ROLE {qs} LOGIN")
                for stmt in (
                    f"ALTER ROLE {qs} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '{password}'",
                    f"ALTER ROLE {qs} SET search_path = {qs}, {s}",
                    f"ALTER ROLE {qs} SET statement_timeout = '60s'",
                    f'CREATE TABLE IF NOT EXISTS {s}.keel_query_bound (pid integer PRIMARY KEY, tenant {T} NOT NULL, glob {T} NOT NULL)',
                    f"REVOKE ALL ON {s}.keel_query_bound FROM PUBLIC",
                    f"DELETE FROM {s}.keel_query_bound WHERE pid NOT IN (SELECT pid FROM pg_stat_activity)",
                    f"CREATE SCHEMA IF NOT EXISTS {qs}",
                    f"REVOKE ALL ON SCHEMA {qs} FROM PUBLIC",
                    f"GRANT USAGE ON SCHEMA {qs} TO {qs}",
                    f"DROP VIEW IF EXISTS {qs}.facts, {qs}.entities, {qs}.relations",
                    (f"CREATE VIEW {qs}.facts WITH (security_barrier = true) AS SELECT f.* FROM {s}.facts f "
                     f"WHERE f.status IS NULL AND (f.expires_at IS NULL OR f.expires_at > {s}.keel_now()) "
                     f"AND ((f.tenant = {bound.format('tenant')} AND f.scope = '{t}') OR (f.tenant = {bound.format('glob')} AND f.scope = '{g}'))"),
                    f"CREATE VIEW {qs}.entities WITH (security_barrier = true) AS SELECT e.* FROM {s}.entities e WHERE e.tenant = {bound.format('tenant')}",
                    f"CREATE VIEW {qs}.relations WITH (security_barrier = true) AS SELECT r.* FROM {s}.relations r WHERE r.tenant = {bound.format('tenant')}",
                    f"GRANT SELECT ON {qs}.facts, {qs}.entities, {qs}.relations TO {qs}",
                    f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {qs}",
                ):
                    self._exec(stmt)
                # another session of the role must not read this one's statements in pg_stat_activity (a superuser's right;
                # otherwise, documented: a statement shows there only while it runs)
                with contextlib.suppress(Exception):
                    self._exec(f"ALTER ROLE {qs} SET track_activities = off")
            finally:
                self._exec("SELECT pg_advisory_unlock(7414)")
        self._query_role = (role, password)
        return self._query_role
