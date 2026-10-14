"""SQLite store: the same protocol as MemoryStore, on one file.

Durable with no new dependency (stdlib `sqlite3`). One file per deployment,
one subgraph per tenant inside it, WAL mode so a `keel serve` process can read
while an ingest process writes. Rows of one subject are filtered in Python with
the same functions MemoryStore uses, so both stores answer identically.

    store = SqliteStore("keel.db")
    Ingestor(cartridge, store).ingest(adapter.follow())

Graphiti stays the candidate for graph-native queries; this is the durable
intermediate step CLAUDE.md allows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keel.envelope import utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation
from keel.store.memory import filter_facts, filter_relations, new_fact, search_in

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
  tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, ref TEXT, attrs TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant, kind, id));
CREATE TABLE IF NOT EXISTS relations (
  seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS relations_src ON relations (tenant, src);
CREATE INDEX IF NOT EXISTS relations_dst ON relations (tenant, dst);
CREATE TABLE IF NOT EXISTS facts (
  seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
  value TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT,
  scope TEXT NOT NULL, corrected_by TEXT, evidence TEXT, expires_at TEXT);
CREATE INDEX IF NOT EXISTS facts_subject ON facts (tenant, subject);
"""


def _ts(d: datetime | None) -> str | None:
    """ISO 8601 in UTC, so string order is time order."""
    return None if d is None else d.astimezone(timezone.utc).isoformat()


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _entity(row: sqlite3.Row) -> Entity:
    return Entity(tenant=row["tenant"], kind=row["kind"], id=row["id"], ref=row["ref"], attrs=json.loads(row["attrs"]))


def _relation(row: sqlite3.Row) -> Relation:
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


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        tenant=row["tenant"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=json.loads(row["value"]),
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]),
        recorded_at=_dt(row["recorded_at"]),
        source=row["source"],
        scope=Scope(row["scope"]),
        corrected_by=row["corrected_by"],
        evidence=row["evidence"],
        expires_at=_dt(row["expires_at"]),
    )


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # autocommit: one primitive, one transaction. check_same_thread off because MCP servers run tool
        # calls in worker threads; Python's sqlite3 is built serialized, so one shared connection is safe.
        # ponytail: one connection per store; a connection per thread if contention ever shows.
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- writes (the primitives) -------------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity:
        ent = self.entity(tenant, f"{kind}:{id}") or Entity(tenant=tenant, kind=kind, id=id)
        if ref:
            ent.ref = ref
        ent.attrs.update(attrs)
        self.db.execute(
            "INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant, kind, id) DO UPDATE SET ref = excluded.ref, attrs = excluded.attrs",
            (tenant, kind, id, ent.ref, json.dumps(ent.attrs)),
        )
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation:
        row = self.db.execute(
            "SELECT * FROM relations WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL",
            (tenant, src, rel, dst),
        ).fetchone()
        if row:
            return _relation(row)  # already open; idempotent
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self.db.execute(
            "INSERT INTO relations (tenant, src, rel, dst, valid_from, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant, src, rel, dst, _ts(r.valid_from), _ts(r.recorded_at), source),
        )
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        self.db.execute(
            "UPDATE relations SET valid_to = ? WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL",
            (_ts(valid_to), tenant, src, rel, dst),
        )

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        self.close_fact(tenant, subject, predicate, valid_to=valid_from)  # never overwrite: close, then open
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        self.db.execute(
            "INSERT INTO facts (tenant, subject, predicate, value, valid_from, recorded_at, source, scope, "
            "corrected_by, evidence, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant, subject, predicate, json.dumps(value), _ts(f.valid_from), _ts(f.recorded_at), f.source,
             f.scope.value, f.corrected_by, f.evidence, _ts(f.expires_at)),
        )
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime) -> None:
        self.db.execute(
            "UPDATE facts SET valid_to = ? WHERE tenant = ? AND subject = ? AND predicate = ? AND valid_to IS NULL",
            (_ts(valid_to), tenant, subject, predicate),
        )

    # ---- reads -------------------------------------------------------------------

    def entity(self, tenant: str, key: str) -> Entity | None:
        kind, _, id = key.partition(":")
        row = self.db.execute("SELECT * FROM entities WHERE tenant = ? AND kind = ? AND id = ?", (tenant, kind, id)).fetchone()
        return None if row is None else _entity(row)

    def facts(
        self,
        tenant: str,
        subject: str,
        at: datetime | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Fact]:
        rows = self.db.execute("SELECT * FROM facts WHERE tenant = ? AND subject = ? ORDER BY seq", (tenant, subject))
        return filter_facts((_fact(r) for r in rows), at, since, until)

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        rows = self.db.execute("SELECT * FROM relations WHERE tenant = ? AND (src = ? OR dst = ?) ORDER BY seq", (tenant, key, key))
        return filter_relations((_relation(r) for r in rows), rel, at)

    def search(self, tenant: str, text: str) -> list[Entity | Fact]:
        # ponytail: scans one tenant in Python; switch to LIKE or FTS5 when a tenant outgrows memory
        ents = (_entity(r) for r in self.db.execute("SELECT * FROM entities WHERE tenant = ? ORDER BY rowid", (tenant,)))
        facts = (_fact(r) for r in self.db.execute("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,)))
        return search_in(ents, facts, text)

    # ---- maintenance --------------------------------------------------------------

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        """Drop expired memory. Called by the consolidation job; never by handlers."""
        cur = self.db.execute(
            "DELETE FROM facts WHERE tenant = ? AND expires_at IS NOT NULL AND expires_at <= ?", (tenant, _ts(now or utcnow()))
        )
        return cur.rowcount

    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]:
        """Everything about one tenant, closed facts included: the audit trail."""
        for row in self.db.execute("SELECT * FROM entities WHERE tenant = ? ORDER BY rowid", (tenant,)):
            yield _entity(row)
        for row in self.db.execute("SELECT * FROM relations WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _relation(row)
        for row in self.db.execute("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _fact(row)

    def reset(self, tenant: str) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it."""
        for table in ("entities", "relations", "facts"):
            self.db.execute(f"DELETE FROM {table} WHERE tenant = ?", (tenant,))
