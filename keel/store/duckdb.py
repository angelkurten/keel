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

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._rows("SELECT column_name AS name FROM information_schema.columns WHERE table_name = ?", (table,))}

    def _has_index(self, table: str, name: str) -> bool:
        return self._one("SELECT 1 AS one FROM duckdb_indexes() WHERE index_name = ?", (name,)) is not None

    # ---- the compiled cartridge ---------------------------------------------------------

    def install(self, cartridge: Any) -> None:
        """(Re)create the point-in-time macros, and the cartridge's rule macros (None: none). Idempotent per cartridge."""
        ident = None if cartridge is None else f"{cartridge.ident}|{sorted(cartridge.predicates or {})}"
        if ident == self._installed and ident is not None:
            return
        with self._macro_lock, self._lock:
            empty = type("Cartridge", (), {"rules": {}, "predicates": {}, "relations": {}, "calendar": None})
            self.db.execute(sqlgen.macros(cartridge or empty))
            self._installed = ident

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
