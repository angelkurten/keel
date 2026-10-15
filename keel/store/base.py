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


class Store(Protocol):
    """Primitives. Handlers call these; nothing else writes to the graph."""

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity: ...

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation: ...

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None: ...

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact: ...

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime) -> None: ...

    # reads
    def entity(self, tenant: str, key: str) -> Entity | None: ...

    def facts(self, tenant: str, subject: str, at: datetime | None = None, since: datetime | None = None, until: datetime | None = None) -> list[Fact]: ...

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]: ...

    def search(self, tenant: str, text: str) -> list[Entity | Fact]: ...

    # ingest bookkeeping (the Ingestor, never handlers): at-least-once delivery means duplicates arrive
    def seen(self, tenant: str, event_id: str) -> bool: ...  # was this event already applied?

    def remember(self, tenant: str, event_id: str) -> bool: ...  # mark it; False when it already was (a duplicate)

    # maintenance (jobs and operators, never handlers)
    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]: ...  # everything, for export and audit
