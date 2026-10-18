"""Postgres CDC: row changes in your tables become events, through logical replication.

Keel reads a logical replication slot with the built-in `pgoutput` plugin (Postgres 10+, also on RDS,
Cloud SQL, Azure and Supabase; no extension), through SQL functions, so plain psycopg works. Each
inserted, updated or deleted row of a mapped table becomes one envelope:

    type        <kind>.created | <kind>.updated | <kind>.deleted
    subject     <kind>:<the id column>
    tenant      the tenant column (or a fixed tenant)
    occurred_at the transaction's commit time
    id          pgcdc:<commit lsn>:<n>, stable, so a re-read is a duplicate
    payload     the new row (the old key columns for a delete)

    keel cdc cartridges/mine --dsn postgresql://... --map cdc.yaml --db keel.db --setup

`cdc.yaml` maps tables to kinds:

    inspections: {kind: inspection, id: id, tenant: tenant_id}
    tires:       {kind: tire, id: serial, tenant: fleet_id, columns: [serial, depth_mm, unit_id]}

The slot moves only after Keel commits (`ack()`), so a batch that rolls back is read again; dedupe
skips what was stored. The database needs `wal_level = logical`. A slot that nobody reads keeps WAL
on the server's disk: drop it (`SELECT pg_drop_replication_slot('keel')`) when you stop.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from keel.envelope import Event

PG_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
OPS = {"I": "created", "U": "updated", "D": "deleted"}


@dataclass
class TableMap:
    kind: str
    id: str
    tenant: str | None = None  # a column; or leave it out and give the adapter a fixed tenant
    columns: list[str] | None = None  # only these columns in the payload (pointers, not copies)


@dataclass
class Relation:
    schema: str
    name: str
    columns: list[tuple[str, int]] = field(default_factory=list)  # (name, type oid)


def _value(text: str | None, oid: int) -> Any:
    if text is None:
        return None
    if oid == 16:
        return text == "t"
    if oid in (20, 21, 23, 26):
        return int(text)
    if oid in (700, 701):
        return float(text)
    if oid == 1700:  # numeric: an int when it has no fraction
        return int(text) if text.lstrip("-").isdigit() else float(text)
    if oid in (114, 3802):
        return json.loads(text)
    return text


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.b, self.i = data, 0

    def take(self, fmt: str) -> Any:
        v = struct.unpack_from("!" + fmt, self.b, self.i)
        self.i += struct.calcsize("!" + fmt)
        return v[0]

    def string(self) -> str:
        end = self.b.index(b"\0", self.i)
        s, self.i = self.b[self.i:end].decode(), end + 1
        return s

    def tuple(self, rel: Relation) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, oid in rel.columns[: self.take("h")]:
            kind = self.take("c")
            if kind == b"t":
                n = self.take("i")
                out[name] = _value(self.b[self.i:self.i + n].decode(), oid)
                self.i += n
            elif kind == b"n":
                out[name] = None
            # b"u": an unchanged TOASTed value, not sent; left out of the payload
        return out


def _fmt(lsn: int) -> str:
    return f"{lsn >> 32:X}/{lsn & 0xFFFFFFFF:X}"


def decode(messages: list[bytes]) -> Iterator[tuple[str, datetime, str, Relation | None, dict[str, Any]]]:
    """pgoutput (protocol 1) messages to (commit lsn, commit time, op, relation, row) changes, plus one
    ("<end lsn>", time, "commit", None, {}) per transaction: the position to confirm once it is stored.
    A pure function over bytes, so it is tested without a server."""
    rels: dict[int, Relation] = {}
    lsn, at = "", PG_EPOCH
    for data in messages:
        r = _Reader(data)
        tag = r.take("c").decode()
        if tag == "B":
            final, micros = r.take("Q"), r.take("q")
            lsn, at = _fmt(final), PG_EPOCH + timedelta(microseconds=micros)
        elif tag == "R":
            oid, schema, name = r.take("I"), r.string(), r.string()
            r.take("b")  # replica identity
            rel = Relation(schema, name)
            for _ in range(r.take("h")):
                r.take("b")
                col = r.string()
                rel.columns.append((col, r.take("I")))
                r.take("i")
            rels[oid] = rel
        elif tag in OPS:
            rel = rels[r.take("I")]
            sub = r.take("c")
            row = r.tuple(rel)
            if tag == "U" and sub in (b"K", b"O"):  # the old key or row came first; the new row follows
                r.take("c")
                row = r.tuple(rel)
            yield lsn, at, OPS[tag], rel, row
        elif tag == "C":
            r.take("b")
            r.take("Q")  # the commit lsn, already known from B
            yield _fmt(r.take("Q")), at, "commit", None, {}
        # Y (type), O (origin), T (truncate), M (message): nothing to emit


class PostgresCdcAdapter:
    manual_ack = False  # Ingestor.ingest sets it: the slot then moves only on ack()

    def __init__(
        self,
        connect: Callable[[], Any],
        tables: dict[str, TableMap | dict[str, Any]],
        slot: str = "keel",
        publication: str = "keel",
        tenant: str | None = None,
        batch_size: int = 1000,
    ) -> None:
        self.connect, self.slot, self.publication, self.tenant, self.batch_size = connect, slot, publication, tenant, batch_size
        self.tables = {t: m if isinstance(m, TableMap) else TableMap(**m) for t, m in tables.items()}
        for t, m in self.tables.items():
            if m.tenant is None and tenant is None:
                raise ValueError(f"table '{t}': give it a tenant column, or give the adapter a fixed tenant")
        self._taken: str | None = None  # the last commit lsn whose changes were all handed out

    def setup(self) -> None:
        """Create the publication and the slot if they do not exist, and set REPLICA IDENTITY FULL on tables
        with a tenant column: by default a DELETE carries only the primary key, so its tenant would be unknown.
        Needs a role that may replicate and alter those tables."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            for t, m in self.tables.items():
                if m.tenant is not None:
                    cur.execute(f"ALTER TABLE {t} REPLICA IDENTITY FULL")
            cur.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (self.publication,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE PUBLICATION "{self.publication}" FOR TABLE ' + ", ".join(self.tables))
                conn.commit()  # a slot cannot be created in a transaction that has written
            cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (self.slot,))
            if cur.fetchone() is None:
                cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (self.slot,))
            conn.commit()
        finally:
            conn.close()

    def ack(self) -> None:
        """Everything taken so far is stored: let the server release that WAL."""
        if self._taken is None:
            return
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_replication_slot_advance(%s, %s::pg_lsn)", (self.slot, self._taken))
            conn.commit()
        finally:
            conn.close()

    def _peek(self) -> list[tuple[str, bytes]]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT lsn::text, data FROM pg_logical_slot_peek_binary_changes(%s, NULL, %s, "
                "'proto_version', '1', 'publication_names', %s)",
                (self.slot, self.batch_size, self.publication),
            )
            return [(lsn, bytes(data)) for lsn, data in cur.fetchall()]
        finally:
            conn.close()

    def _events(self, rows: list[tuple[str, bytes]]) -> Iterator[tuple[str | None, Event | None]]:
        """Envelopes for the mapped tables, and after each transaction (None, end lsn) to confirm once stored."""
        seen: dict[str, int] = {}
        for lsn, at, op, rel, row in decode([d for _, d in rows]):
            if rel is None:  # a commit: its end lsn
                yield lsn, None
                continue
            n = seen[lsn] = seen.get(lsn, -1) + 1  # counts every change of the transaction, so ids stay stable
            m = self.tables.get(rel.name) or self.tables.get(f"{rel.schema}.{rel.name}")
            if m is None:
                continue
            tenant = row.get(m.tenant) if m.tenant else self.tenant
            if tenant is None:
                raise ValueError(f"{rel.schema}.{rel.name}: a {op} row has no '{m.tenant}'; run setup() or "
                                 f"ALTER TABLE {rel.name} REPLICA IDENTITY FULL, so deletes carry the tenant")
            payload = {k: v for k, v in row.items() if m.columns is None or k in m.columns or k == m.id}
            yield None, Event(
                id=f"pgcdc:{lsn}:{n}", tenant=str(tenant), type=f"{m.kind}.{op}",
                subject=f"{m.kind}:{row[m.id]}", occurred_at=at, source=f"pgcdc:{rel.schema}.{rel.name}", payload=payload,
            )

    def __iter__(self) -> Iterator[Event]:
        """What the slot holds now, then stop. Peeking does not consume: ack() moves the slot past what was
        stored. `keel cdc` repeats this pass, so following a database is a loop of passes."""
        for end, event in self._events(self._peek()):
            if end is not None:
                self._taken = end  # every change of that transaction has been handed out
                if not self.manual_ack:
                    self.ack()
                continue
            yield event
