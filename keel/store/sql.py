"""One SQL store, several dialects: SQLite (stdlib) and MySQL (PyMySQL or mysql-connector).

Rows of one subject are filtered in Python with the same functions MemoryStore uses, so every
store answers identically; the SQL stays plain: inserts, updates by key, selects by (tenant,
subject). Timestamps are ISO 8601 strings in UTC, so string order is time order in any engine.
A dialect supplies the schema, the placeholder style, the two statements that differ (upsert,
insert-or-ignore) and how to open a connection.

Thread safety: MCP servers run tool calls in worker threads and DB-API connections are not
thread-safe, so every statement runs under one lock on one connection.
ponytail: one connection per store; a pool if a deployment ever shows contention.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from keel.envelope import utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation
from keel.store.memory import filter_facts, filter_relations, new_fact, place_fact, search_in


def _ts(d: datetime | None) -> str | None:
    """ISO 8601 in UTC, so string order is time order."""
    return None if d is None else d.astimezone(timezone.utc).isoformat()


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _json(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    return json.loads(v)


def _entity(row: Any) -> Entity:
    return Entity(tenant=row["tenant"], kind=row["kind"], id=row["id"], ref=row["ref"], attrs=_json(row["attrs"]))


def _relation(row: Any) -> Relation:
    return Relation(
        tenant=row["tenant"],
        src=row["src"],
        rel=row["rel"],
        dst=row["dst"],
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]),
        recorded_at=_dt(row["recorded_at"]),
        source=row["source"],
    )


def _fact(row: Any) -> Fact:
    return Fact(
        tenant=row["tenant"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=_json(row["value"]),
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]),
        recorded_at=_dt(row["recorded_at"]),
        source=row["source"],
        scope=Scope(row["scope"]),
        corrected_by=row["corrected_by"],
        evidence=row["evidence"],
        expires_at=_dt(row["expires_at"]),
    )


class SqlStore:
    PLACEHOLDER = "?"
    SCHEMA = ""  # statements separated by ';', run at start, all IF NOT EXISTS
    UPSERT_ENTITY = ""  # (tenant, kind, id, ref, attrs); on conflict update ref and attrs
    REMEMBER = ""  # (tenant, id); insert or ignore
    ENTITY_ORDER = "rowid"
    _NAMES = re.compile(r"\b(entities_key|relations_src|relations_dst|facts_subject|facts_predicate|entities|relations|facts|ingested)\b")

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        """`prefix` names the tables `<prefix>entities`, `<prefix>facts`... for a database shared with an application."""
        self.prefix = prefix
        self._lock = threading.RLock()  # re-entrant: a transaction holds it across its statements
        self._in_tx = False
        self.db = connect()
        self._prepare(self.db)
        for stmt in self.SCHEMA.split(";"):
            if stmt.strip():
                self._exec(stmt)

    # ---- dialect hooks ---------------------------------------------------------------

    def _prepare(self, conn: Any) -> None:
        """Pragmas, autocommit: whatever the connection needs once."""

    def _cursor(self, conn: Any) -> Any:
        """A cursor whose rows support row["column"]."""
        return conn.cursor()

    # ---- plumbing --------------------------------------------------------------------

    def _sql(self, sql: str) -> str:
        if self.prefix:
            sql = self._NAMES.sub(self.prefix + r"\1", sql)
        return sql if self.PLACEHOLDER == "?" else sql.replace("?", self.PLACEHOLDER)

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            return cur.rowcount

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            return list(cur.fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One commit for everything inside, rolled back whole on an exception. The Ingestor wraps
        each event (or each batch) in one: the handler's primitives and `remember` land together,
        and a database that syncs per statement does it once instead of once per primitive."""
        if self._in_tx:  # nested: the outer one owns it
            yield
            return
        with self._lock:
            self._exec("BEGIN")
            self._in_tx = True
            try:
                yield
            except BaseException:
                self._exec("ROLLBACK")
                raise
            else:
                self._exec("COMMIT")
            finally:
                self._in_tx = False

    # ---- writes (the primitives) -------------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity:
        ent = self.entity(tenant, f"{kind}:{id}") or Entity(tenant=tenant, kind=kind, id=id)
        if ref:
            ent.ref = ref
        ent.attrs.update(attrs)
        self._exec(self.UPSERT_ENTITY, (tenant, kind, id, ent.ref, json.dumps(ent.attrs)))
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation:
        row = self._one(
            "SELECT * FROM relations WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL", (tenant, src, rel, dst)
        )
        if row:
            return _relation(row)  # already open; idempotent
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self._exec(
            "INSERT INTO relations (tenant, src, rel, dst, valid_from, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant, src, rel, dst, _ts(r.valid_from), _ts(r.recorded_at), source),
        )
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        self._exec(
            "UPDATE relations SET valid_to = ? WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL",
            (_ts(valid_to), tenant, src, rel, dst),
        )

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        # never overwrite; the new fact takes its place by valid_from even when it arrives late (place_fact)
        rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? AND predicate = ?", (tenant, subject, predicate))
        same = [_fact(r) for r in rows]
        seq_of = {id(fact): r["seq"] for fact, r in zip(same, rows)}
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        closed = place_fact(f, same)
        if closed is not None:
            self._exec("UPDATE facts SET valid_to = ? WHERE seq = ?", (_ts(closed.valid_to), seq_of[id(closed)]))
        self._exec(
            "INSERT INTO facts (tenant, subject, predicate, value, valid_from, valid_to, recorded_at, source, scope, "
            "corrected_by, evidence, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant, subject, predicate, json.dumps(value), _ts(f.valid_from), _ts(f.valid_to), _ts(f.recorded_at), f.source,
             f.scope.value, f.corrected_by, f.evidence, _ts(f.expires_at)),
        )
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime) -> None:
        # a close that predates a fact's start is about an earlier period and leaves this fact alone
        self._exec(
            "UPDATE facts SET valid_to = ? WHERE tenant = ? AND subject = ? AND predicate = ? AND valid_to IS NULL AND valid_from <= ?",
            (_ts(valid_to), tenant, subject, predicate, _ts(valid_to)),
        )

    # ---- reads -------------------------------------------------------------------

    def entity(self, tenant: str, key: str) -> Entity | None:
        kind, _, id = key.partition(":")
        row = self._one("SELECT * FROM entities WHERE tenant = ? AND kind = ? AND id = ?", (tenant, kind, id))
        return None if row is None else _entity(row)

    def facts(
        self,
        tenant: str,
        subject: str,
        at: datetime | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Fact]:
        rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? ORDER BY seq", (tenant, subject))
        return filter_facts((_fact(r) for r in rows), at, since, until)

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        rows = self._rows("SELECT * FROM relations WHERE tenant = ? AND (src = ? OR dst = ?) ORDER BY seq", (tenant, key, key))
        return filter_relations((_relation(r) for r in rows), rel, at)

    def search(self, tenant: str, text: str) -> list[Entity | Fact]:
        # ponytail: scans one tenant in Python; switch to LIKE or full-text when a tenant outgrows memory
        ents = [_entity(r) for r in self._rows(f"SELECT * FROM entities WHERE tenant = ? ORDER BY {self.ENTITY_ORDER}", (tenant,))]
        facts = [_fact(r) for r in self._rows("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,))]
        return search_in(ents, facts, text)

    # ---- ingest bookkeeping -----------------------------------------------------------

    def seen(self, tenant: str, event_id: str) -> bool:
        return self._one("SELECT 1 AS one FROM ingested WHERE tenant = ? AND id = ?", (tenant, event_id)) is not None

    def remember(self, tenant: str, event_id: str) -> None:
        self._exec(self.REMEMBER, (tenant, event_id))

    # ---- maintenance --------------------------------------------------------------

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        """Drop expired memory. Called by the consolidation job; never by handlers."""
        return self._exec(
            "DELETE FROM facts WHERE tenant = ? AND expires_at IS NOT NULL AND expires_at <= ?", (tenant, _ts(now or utcnow()))
        )

    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]:
        """Everything about one tenant, closed facts included: the audit trail."""
        for row in self._rows(f"SELECT * FROM entities WHERE tenant = ? ORDER BY {self.ENTITY_ORDER}", (tenant,)):
            yield _entity(row)
        for row in self._rows("SELECT * FROM relations WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _relation(row)
        for row in self._rows("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _fact(row)

    def reset(self, tenant: str) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it."""
        for table in ("entities", "relations", "facts", "ingested"):
            self._exec(f"DELETE FROM {table} WHERE tenant = ?", (tenant,))
