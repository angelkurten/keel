"""DuckDB store: the same protocol as MemoryStore, on one DuckDB file, with the cartridge compiled to SQL beside it.

Optional extra: `uv sync --extra duckdb`. The tables are the generated ones (keel/sqlgen.py), so a rule can be
evaluated by its compiled macro instead of fact by fact in Python: `rule_subjects` is what `Tools.search(rule=)`
pushes down to when the store has it. Everything else is `keel.store.sql.SqlStore`; this file is the dialect.

    store = DuckdbStore("keel.duckdb")
    Ingestor(cartridge, store).ingest(adapter.follow())
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from keel import sqlgen
from keel.store.sql import SqlStore, _ts

_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_VALUES = re.compile(r"^(.*?)\s+VALUES\s*\(([^)]*)\)(.*)$", re.IGNORECASE | re.DOTALL)  # INSERT ... (cols) VALUES (?, ...) [ON CONFLICT ...]
_INTO = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)", re.IGNORECASE)
# columns whose names DuckDB reserves: quoted wherever the shared SQL names them
_RESERVED = [(re.compile(r"(?<![\w\"])at(?![\w\"(])"), '"at"'), (re.compile(r"(?<![\w\"])glob(?![\w\"(])"), '"glob"')]


class _Cursor:
    """DB-API enough for SqlStore over one DuckDB connection: rows by column name, rowcount, lastrowid.
    Results are read at once (a DuckDB connection holds one result), under the store's lock."""

    def __init__(self, conn: Any) -> None:
        self.conn, self.rowcount, self.lastrowid, self._rows = conn, -1, None, []

    def execute(self, sql: str, params: Any = ()) -> None:
        if params:
            self.conn.execute(sql, list(params))
        else:
            self.conn.execute(sql)
        if _DML.match(sql):
            row = self.conn.fetchone()
            self.rowcount = row[0] if row else -1
            self._rows = []
            into = _INTO.match(sql)
            if into and into.group(1) in ("facts", "relations"):
                self.lastrowid = self.conn.execute(f"SELECT currval('{into.group(1)}_seq')").fetchone()[0]
            return
        desc = self.conn.description
        if desc is None:
            self._rows = []
            return
        cols = [d[0] for d in desc]
        self._rows = [dict(zip(cols, r)) for r in self.conn.fetchall()]

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self.conn.executemany(sql, [list(r) for r in rows])

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class DuckdbStore(SqlStore):
    TIME = "VARCHAR"
    INDEX_IF_NOT_EXISTS = True
    UPSERT_ENTITY = (
        "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant, kind, id) DO UPDATE SET ref = excluded.ref, attrs = excluded.attrs"
    )
    REMEMBER = "INSERT OR IGNORE INTO ingested (tenant, id) VALUES (?, ?)"
    TOMBSTONE = 'INSERT OR IGNORE INTO forgotten (tenant, "at") VALUES (?, ?)'
    METER = "INSERT INTO metering (tenant, day, item, n) VALUES (?, ?, ?, ?) ON CONFLICT (tenant, day, item) DO UPDATE SET n = n + excluded.n"

    def __init__(self, path: str | Path = ":memory:", prefix: str = "") -> None:
        import duckdb

        if prefix:
            raise ValueError("DuckdbStore keeps its own file: no table prefix")
        self.SCHEMA = sqlgen.DuckDB().schema()
        self._installed: str | None = None
        self._macro_lock = threading.Lock()
        super().__init__(lambda: duckdb.connect(str(path)), prefix)
        self.install(None)

    def _prepare(self, conn: Any) -> None:
        conn.execute("SET TimeZone = 'UTC'")

    def _cursor(self, conn: Any) -> _Cursor:
        return _Cursor(conn)

    def _sql(self, sql: str) -> str:
        if " at" in sql or "glob" in sql:
            for pattern, quoted in _RESERVED:
                sql = pattern.sub(quoted, sql)
        return sql

    def _execmany(self, sql: str, rows: list[tuple[Any, ...]], size: int = 0) -> None:
        """Multi-row writes (a bulk batch's flush) in one statement, column by column: DuckDB's executemany runs
        a statement per row (~900 events/s). `VALUES (?, ...)` becomes `SELECT UNNEST(?), ...` over one list per
        column; rows keep their order, so the sequence hands out seq as the batch wrote them. DuckDB's ON CONFLICT
        does not keep that order, so the entity upsert updates the rows that exist and inserts the others."""
        if not rows:
            return
        if sql == self.UPSERT_ENTITY:
            with self._lock:
                lists = [[r[i] for r in rows] for i in range(3)]
                have = set(self.db.execute("SELECT e.tenant, e.kind, e.id FROM entities e JOIN (SELECT UNNEST(?) AS tenant, UNNEST(?) AS kind, "
                                           "UNNEST(?) AS id) c USING (tenant, kind, id)", lists).fetchall())
                old = [r for r in rows if tuple(r[:3]) in have]
                if old:
                    self.db.execute("UPDATE entities SET ref = c.ref, attrs = c.attrs FROM (SELECT UNNEST(?) AS tenant, UNNEST(?) AS kind, "
                                    "UNNEST(?) AS id, UNNEST(?::VARCHAR[]) AS ref, UNNEST(?) AS attrs) c "
                                    "WHERE entities.tenant = c.tenant AND entities.kind = c.kind AND entities.id = c.id",
                                    [[r[i] for r in old] for i in range(5)])
                rows = [r for r in rows if tuple(r[:3]) not in have]
                sql = "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?)"
            if not rows:
                return
        head, values, tail = _VALUES.match(sql).groups()
        if "ON CONFLICT" in tail.upper():  # metering: a few rows; row by row keeps its order
            return super()._execmany(sql, rows)
        n = values.count("?")
        cols = [[r[i] for r in rows] for i in range(n)]
        with self._lock:
            self.db.execute(self._sql(f"{head} SELECT {', '.join('UNNEST(?)' for _ in range(n))}{tail}"), cols)

    def _close_rows(self, table: str, closes: dict[int, tuple[datetime | None, datetime | None, datetime | None]], size: int = 0) -> None:
        """A batch's closes in one UPDATE joined to its lists (seq, valid_to, closed_at, changed_at)."""
        if not closes:
            return
        seqs = list(closes)
        cols = [seqs] + [[_ts(closes[s][i]) for s in seqs] for i in range(3)]
        with self._lock:
            self.db.execute(f"UPDATE {table} SET valid_to = c.valid_to, closed_at = c.closed_at, changed_at = c.changed_at FROM "
                            "(SELECT UNNEST(?::BIGINT[]) AS seq, UNNEST(?::VARCHAR[]) AS valid_to, UNNEST(?::VARCHAR[]) AS closed_at, "
                            f"UNNEST(?::VARCHAR[]) AS changed_at) AS c WHERE {table}.seq = c.seq", cols)

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._rows("SELECT column_name AS name FROM information_schema.columns WHERE table_name = ?", (table,))}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one("SELECT 1 AS one FROM duckdb_indexes() WHERE index_name = ?", (name,)) is not None

    # ---- the compiled cartridge ---------------------------------------------------------

    def install(self, cartridge: Any) -> None:
        """(Re)create the point-in-time macros, and the cartridge's rule macros (None: none). Idempotent per cartridge."""
        ident = None if cartridge is None else f"{cartridge.ident}|{sorted(cartridge.predicates or {})}|{cartridge.entities!r}|{cartridge.relations!r}"
        if ident == self._installed and ident is not None:
            return
        with self._macro_lock, self._lock:
            empty = type("Cartridge", (), {"rules": {}, "predicates": {}, "relations": {}, "calendar": None})
            self.db.execute(sqlgen.macros(cartridge or empty))
            self._installed = ident

    # subjects_where on DuckDB's JSON: attrs read with its json functions, the state from the generated macro
    JSON_TYPE, JSON_STR, JT_TEXT, JT_ARRAY = "json_type({0}, {1})", "json_extract_string({0}, {1})", "'VARCHAR'", "'ARRAY'"
    JT_NULL, JT_NUMBER, JT_BOOL = "'NULL'", "'BIGINT', 'UBIGINT', 'HUGEINT', 'DOUBLE'", "'BOOLEAN'"
    ASCII = "(strlen({0}) = length({0}))"

    def _where_state(self, tenant: str, cartridge: Any, predicates: list[str], as_of: datetime, known_at: datetime | None,
                     author: str | None, task: str | None) -> tuple[str, tuple[Any, ...]]:
        """The current facts of some predicates by the generated `state_known_at` macro: what rules and agents read."""
        from keel.envelope import utcnow

        self.install(cartridge)
        lit = sqlgen.DuckDB().lit
        # literals, not parameters: DuckDB plans the macro with them, so the `k IS NULL` branch is pruned
        sql = (f"SELECT subject, predicate, value FROM state_known_at({lit(tenant)}, {lit(_ts(as_of))}, k := {lit(_ts(known_at))}, "
               f"alive := {lit(_ts(known_at or utcnow()))}, as_author := {lit(author)}, as_task := {lit(task)}) "
               f"WHERE predicate IN ({', '.join(lit(p) for p in predicates)})")
        return sql, ()

    def rule_subjects(self, tenant: str, cartridge: Any, rule: str, as_of: datetime, known_at: datetime | None = None,
                      author: str | None = None, task: str | None = None) -> set[str]:
        """The entities `rule` holds for at `as_of` (as known at `known_at`), by its compiled macro: what
        `Tools.search(rule=)` would find evaluating every candidate in Python. `author`/`task` are the caller's
        (agent memory only its author's, task memory only its task's), as rules read them."""
        from keel.envelope import utcnow

        self.install(cartridge)
        lit = sqlgen.DuckDB().lit
        alive = _ts(known_at or utcnow())
        # literals, not parameters: DuckDB plans the macro with them, so the `k IS NULL` branch is pruned
        sql = (f"SELECT subject FROM {sqlgen.rule_macro(rule)}_at({lit(tenant)}, {lit(_ts(as_of))}, k := {lit(_ts(known_at))}, "
               f"alive := {lit(alive)}, as_author := {lit(author)}, as_task := {lit(task)})")
        with self._lock:
            return {r[0] for r in self.db.execute(sql).fetchall()}
