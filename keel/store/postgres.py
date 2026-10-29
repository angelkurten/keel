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

import hashlib
import hmac
import threading
import time
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
        self._query_conns: dict[str, tuple[Any, threading.Lock]] = {}  # query: one connection per tenant, as its own role
        self._query_ready: tuple[str, str] | None = None  # (group role, secret) once set up
        self._query_where: tuple[str, str, str] | None = None  # (database, schema, user)
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

    # ---- query: a login role per tenant that reads only views bound to it -------------------

    QUERY_DIALECT = "postgres"
    QUERY_CONNECTIONS = 8  # tenants whose query connection is kept open; the least recently queried is closed past it

    def query_rows(self, tenant: str, cartridge: Any, sql: str, cap: int, timeout: float) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        """An agent's read-only SQL over the generated functions, for `tenant` only (Tools.query).

        Each tenant has a login role of its own (`_query_role`), so sessions of different tenants can neither read
        each other's statements in pg_stat_activity nor cancel or terminate each other. The role may read nothing but
        three security-barrier views over facts, entities and relations, each showing only the rows of the tenant the
        role is bound to (by `current_user`, which a session logged in as that role cannot change), through a group
        role. The connection makes temporary views `facts`, `entities`, `relations` over them as it opens, so the
        generated functions read through them. Every query is one statement declared as a server-side cursor (a
        SELECT only), each row measured on the server (one over a row's budget is an error there, never sent), run
        in a transaction rolled back after it under `statement_timeout`, its session advisory locks released. At most
        `cap` rows and RESULT_MAX bytes; `more` when there were others."""
        import psycopg

        self.install(cartridge)
        conn, lock = self._query_connection(tenant)
        body = sql.rstrip().rstrip(";")
        wrapped = (f"SELECT keel_q.* FROM ({body}\n) AS keel_q WHERE CASE WHEN octet_length(CAST(keel_q AS text)) > {q.ROW_MAX} "
                   f"THEN {self._qs}.keel_query_too_large() ELSE true END")
        with lock:
            self._one_select(conn, body)  # parsed alone first: balanced, so the wrapper below cannot be closed early
            # one deadline for the whole query: statement_timeout restarts on every FETCH of the cursor, so a timer cancels
            # whatever is running when it ends, take() fetches nothing after it, and PG 17+ adds transaction_timeout
            deadline = time.monotonic() + timeout
            timer = threading.Timer(timeout, getattr(conn, "cancel_safe", conn.cancel))
            timer.start()
            try:
                ms = max(1, int(timeout * 1000))
                conn.execute(f"SET LOCAL statement_timeout = {ms}")
                if conn.info.server_version >= 170000:
                    conn.execute(f"SET LOCAL transaction_timeout = {ms + 1000}")
                cur = conn.cursor(name="keel_query")  # DECLARE ... CURSOR FOR: a SELECT, nothing else
                cur.execute(wrapped)
                cols = [d.name for d in cur.description or ()]
                got, more = q.take(cur.fetchmany, cap, deadline)
            except psycopg.errors.QueryCanceled as exc:
                raise ValueError(f"query timed out after {timeout:g} s: narrow it, or aggregate") from exc
            except ValueError as exc:
                if str(exc) == q.TIMED_OUT:
                    raise ValueError(f"query timed out after {timeout:g} s: narrow it, or aggregate") from exc
                raise
            except psycopg.Error as exc:
                raise ValueError(f"query failed: {q.clean_error(exc)}") from exc
            finally:
                timer.cancel()
                try:
                    conn.rollback()  # undoes set_config, temp tables, LISTEN, NOTIFY; not a session advisory lock
                    conn.execute("SELECT pg_advisory_unlock_all()")
                    conn.commit()
                except psycopg.Error:  # the connection is gone: the next query opens another
                    self._drop_query_connection(tenant)
            return cols, got, more

    @staticmethod
    def _one_select(conn: Any, body: str) -> None:
        """The agent's text, alone, must be exactly one statement that returns rows: the server parses it (a protocol
        prepare, never run) and it is deallocated at once. Text that parses alone is balanced (parentheses, quotes,
        comments), so pasted into the row-measuring wrapper it cannot close the wrapper and add a part of its own."""
        from psycopg import pq

        pg, name = conn.pgconn, b"keel_query_check"
        res = pg.prepare(name, body.encode())
        if res.status != pq.ExecStatus.COMMAND_OK:
            raise ValueError(f"query failed: {q.clean_error(Exception((res.error_message or b'').decode(errors='replace')))}")
        try:
            if pg.describe_prepared(name).nfields == 0:
                raise ValueError("query reads only: one SELECT (or WITH ... SELECT) statement over the generated views")
        finally:
            pg.exec_(b"DEALLOCATE keel_query_check")

    @property
    def _qs(self) -> str:
        return '"' + self._query_schema().replace('"', '""') + '"'

    def _query_schema(self) -> str:
        if self._query_where is None:
            here = self._one("SELECT current_database() AS db, current_schema() AS s, current_user AS u")
            self._query_where = (here["db"], here["s"], here["u"])
        return self._query_where[1]

    def _query_connection(self, tenant: str) -> tuple[Any, threading.Lock]:
        import psycopg
        from psycopg.conninfo import make_conninfo

        with self._query_lock:
            if tenant in self._query_conns:
                hit = self._query_conns.pop(tenant)
                if not hit[0].closed:
                    self._query_conns[tenant] = hit  # most recent last
                    return hit
            role, password = self._query_role(tenant)
            try:
                conn = psycopg.connect(make_conninfo(self._dsn, user=role, password=password), autocommit=False)
                s = self._qs
                for t in ("facts", "entities", "relations"):  # pg_temp comes first in every search_path
                    conn.execute(f"CREATE TEMP VIEW {t} AS SELECT * FROM {s}.keel_q_{t}")
                conn.commit()
            except psycopg.Error as exc:
                raise ValueError(f"query cannot open its connection on this Postgres: {q.clean_error(exc)}") from exc
            self._query_conns[tenant] = (conn, threading.Lock())
            while len(self._query_conns) > self.QUERY_CONNECTIONS:
                old, old_lock = self._query_conns.pop(next(iter(self._query_conns)))
                with old_lock:
                    old.close()
            return self._query_conns[tenant]

    def _drop_query_connection(self, tenant: str) -> None:
        with self._query_lock:
            hit = self._query_conns.pop(tenant, None)
        if hit is not None:
            hit[0].close()

    def _query_role(self, tenant: str) -> tuple[str, str]:
        """The tenant's login role, created on first use in one transaction (nothing is left behind if a step fails),
        with what CREATEROLE may set: LOGIN, a password, the store's schema as search_path, a statement timeout, and
        membership of the deployment's group role, which may read the three views. Its password is an HMAC of the
        deployment's own random secret (a table only the store's user can read) and the role's name: never derived
        from the DSN, unchanged when the store's credentials rotate."""
        try:
            group, secret = self._query_setup()
            db, schema, user = self._query_where  # type: ignore[misc]
            role = f"{group}_{hashlib.sha256(f'{db}|{schema}|{user}|{tenant}'.encode()).hexdigest()[:12]}"
            password = hmac.new(secret.encode(), role.encode(), hashlib.sha256).hexdigest()
            qr, s = f'"{role}"', self._qs
            with self.transaction():
                self._exec("SET LOCAL lock_timeout = '10s'")
                self._exec("SELECT pg_advisory_xact_lock(7414)")
                if self._one("SELECT 1 AS one FROM pg_roles WHERE rolname = ?", (role,)) is None:
                    self._exec(f"CREATE ROLE {qr} LOGIN CONNECTION LIMIT 16 PASSWORD '{password}'")
                else:
                    self._exec(f"ALTER ROLE {qr} LOGIN PASSWORD '{password}'")
                self._exec(f"ALTER ROLE {qr} SET search_path = {s}")
                self._exec(f"ALTER ROLE {qr} SET statement_timeout = '60s'")
                self._exec(f'GRANT "{group}" TO {qr}')
                self._exec(f"INSERT INTO {s}.keel_query_roles (role, tenant, glob) VALUES (?, ?, ?) "
                           "ON CONFLICT (role) DO UPDATE SET tenant = excluded.tenant, glob = excluded.glob", (role, tenant, global_of(tenant)))
            return role, password
        except ValueError:
            raise
        except Exception as exc:  # psycopg's, by message: a deployment that cannot isolate query is told so
            raise ValueError("query cannot isolate tenants on this Postgres (its user needs CREATEROLE and to own its schema): "
                             f"{q.clean_error(exc)}") from exc

    def _query_setup(self) -> tuple[str, str]:
        """Once per process: the deployment's random secret, the role binding table (neither readable by any query
        role), the three security-barrier views and the group role that may read them. Idempotent, in one transaction."""
        if self._query_ready is not None:
            return self._query_ready
        import secrets

        schema = self._query_schema()
        db, _, user = self._query_where  # type: ignore[misc]
        group = "keel_q_" + hashlib.sha256(f"{db}|{schema}|{user}".encode()).hexdigest()[:12]
        s, g = self._qs, f'"{group}"'
        rows = "(SELECT r.{} FROM " + s + ".keel_query_roles r WHERE r.role = CAST(current_user AS text))"
        t, gl = Scope.TENANT.value, Scope.GLOBAL.value
        with self.transaction():
            self._exec("SET LOCAL lock_timeout = '10s'")
            self._exec("SELECT pg_advisory_xact_lock(7414)")
            for stmt in (
                f"CREATE TABLE IF NOT EXISTS {s}.keel_query_secret (id integer PRIMARY KEY CHECK (id = 1), secret text NOT NULL)",
                f"REVOKE ALL ON {s}.keel_query_secret FROM PUBLIC",
                f"CREATE TABLE IF NOT EXISTS {s}.keel_query_roles (role {T} PRIMARY KEY, tenant {T} NOT NULL, glob {T} NOT NULL)",
                f"REVOKE ALL ON {s}.keel_query_roles FROM PUBLIC",
                (f"CREATE OR REPLACE VIEW {s}.keel_q_facts WITH (security_barrier = true) AS SELECT f.* FROM {s}.facts f "
                 f"WHERE f.status IS NULL AND (f.expires_at IS NULL OR f.expires_at > {s}.keel_now()) "
                 f"AND ((f.tenant = {rows.format('tenant')} AND f.scope = '{t}') OR (f.tenant = {rows.format('glob')} AND f.scope = '{gl}'))"),
                f"CREATE OR REPLACE VIEW {s}.keel_q_entities WITH (security_barrier = true) AS SELECT e.* FROM {s}.entities e WHERE e.tenant = {rows.format('tenant')}",
                f"CREATE OR REPLACE VIEW {s}.keel_q_relations WITH (security_barrier = true) AS SELECT r.* FROM {s}.relations r WHERE r.tenant = {rows.format('tenant')}",
                f"REVOKE ALL ON {s}.keel_q_facts, {s}.keel_q_entities, {s}.keel_q_relations FROM PUBLIC",
                (f"CREATE OR REPLACE FUNCTION {s}.keel_query_too_large() RETURNS boolean LANGUAGE plpgsql AS $keel$ "
                 f"BEGIN RAISE EXCEPTION '{q.TOO_LARGE}'; END $keel$"),
            ):
                self._exec(stmt)
            self._exec(f"INSERT INTO {s}.keel_query_secret (id, secret) VALUES (1, ?) ON CONFLICT (id) DO NOTHING", (secrets.token_hex(32),))
            secret = self._one(f"SELECT secret FROM {s}.keel_query_secret WHERE id = 1")["secret"]
            if self._one("SELECT 1 AS one FROM pg_roles WHERE rolname = ?", (group,)) is None:
                self._exec(f"CREATE ROLE {g} NOLOGIN")
            self._exec(f"GRANT USAGE ON SCHEMA {s} TO {g}")
            self._exec(f"GRANT SELECT ON {s}.keel_q_facts, {s}.keel_q_entities, {s}.keel_q_relations TO {g}")
        self._query_ready = (group, secret)
        return self._query_ready
