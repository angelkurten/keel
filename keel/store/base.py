"""Store protocol: the primitives a handler is allowed to call.

A store holds, per tenant: entities, relations between them, and facts
about them. Facts are bitemporal: `valid_from`/`valid_to` say when the fact
was true in the world; `recorded_at` says when Keel learned it. Facts are
closed, never overwritten.

The graph holds pointers (`ref`) to rows in the SaaS's own systems, the
semantics, and earned memory. It never holds copies of those rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from keel.scopes import Scope


@dataclass
class Entity:
    tenant: str
    kind: str
    id: str
    ref: str | None = None  # pointer to the row in the system of record
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Relation:
    tenant: str
    src: str  # entity key
    rel: str  # relation type from the cartridge ontology
    dst: str  # entity key
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime | None = None
    source: str | None = None


@dataclass
class Fact:
    tenant: str
    subject: str  # entity key
    predicate: str  # e.g. "tire_depth_mm", "status", "doc_expiry"
    value: Any
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime | None = None
    source: str | None = None
    scope: Scope = Scope.TENANT
    corrected_by: str | None = None  # a person or agent id when this fact is a correction
    evidence: str | None = None  # pointer: event id, row ref, photo url
    expires_at: datetime | None = None  # memory policy, not world time
    task: str | None = None  # the job this memory belongs to (task scope); see history(task=)
    idempotency_key: str | None = None  # annotate(key=): a replayed write returns this fact
    closed_at: datetime | None = None  # transaction time of the close: when Keel learned valid_to
    status: str | None = None  # None = accepted; "proposed" waits for a decision; "rejected" records one
    trace: str | None = None  # the caller's W3C traceparent / tool call id, joining traces to the audit
    cartridge: str | None = None  # annotations: the cartridge (name@version#rules) the decision was made under


class DuplicateKey(ValueError):
    """open_fact with an idempotency_key the subject already has. `fact` is the one already written,
    so a caller that lost a race returns it instead of writing twice."""

    def __init__(self, fact: Fact) -> None:
        super().__init__(f"idempotency key '{fact.idempotency_key}' is already on {fact.subject}")
        self.fact = fact


class Store(Protocol):
    """Primitives. Handlers call these; nothing else writes to the graph."""

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity: ...

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation: ...

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None: ...

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact: ...  # may raise DuplicateKey

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT) -> None: ...  # scope None: every scope

    # reads
    def entity(self, tenant: str, key: str) -> Entity | None: ...

    def facts(self, tenant: str, subject: str, at: datetime | None = None, since: datetime | None = None, until: datetime | None = None) -> list[Fact]: ...

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]: ...

    def search(self, tenant: str, text: str) -> list[Entity | Fact]: ...

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]: ...  # Tools lists kinds with it

    def changes(self, tenant: str, after: datetime) -> list[Fact]: ...  # recorded or closed after a watermark

    # ingest bookkeeping (the Ingestor, never handlers): at-least-once delivery means duplicates arrive
    def seen(self, tenant: str, event_id: str) -> bool: ...  # was this event already applied?

    def remember(self, tenant: str, event_id: str) -> bool: ...  # mark it; False when it already was (a duplicate)

    # maintenance (jobs and operators, never handlers)
    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]: ...  # everything, for export and audit

    def expire(self, tenant: str, now: datetime | None = None) -> int: ...  # drop facts past expires_at

    def close_task(self, tenant: str, task: str, now: datetime | None = None) -> int: ...  # expire a task's memory

    # erasure (optional; the Ingestor requires it when the cartridge declares an event that `forgets: tenant`)
    # forget: in one transaction, everything about the tenant (entities, relations, facts of every scope,
    # annotations, applied event ids) is deleted and a tombstone (tenant, time) is left, so nothing revives it.
    # Idempotent: a second call deletes whatever slipped in since and keeps the first tombstone's time.
    # Returns what it deleted: {"entities", "relations", "facts", "annotations", "ingested"}.
    # def forget(self, tenant: str) -> dict[str, int]: ...
    # def forgotten(self, tenant: str) -> datetime | None: ...   # the tombstone's time
    # def forgotten_tenants(self) -> dict[str, datetime]: ...      # every tombstone, for index sweeps

    # optional: every tenant with something stored, never the global partition (`--all-tenants` in jobs)
    # def tenants(self) -> list[str]: ...
