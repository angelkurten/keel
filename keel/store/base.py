"""Store protocol: the primitives a handler is allowed to call.

A store holds, per tenant: entities, relations between them, and facts
about them. Facts are bitemporal: `valid_from`/`valid_to` say when the fact
was true in the world; `recorded_at` (and `closed_at` for its end) says when it
was learned: the event's own `recorded_at` when its source declared one, else
when Keel ingested it. `changed_at` is the ingest clock of the last write, the
cursor of `changes`; it is never a query time. Facts are closed, never overwritten.

The graph holds pointers (`ref`) to rows in the SaaS's own systems, the
semantics, and earned memory. It never holds copies of those rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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
class Account:
    """What a hosted account may use (keel/accounts.py). None means no limit, the shared store, no retention."""

    id: str
    rps: int | None = None  # requests per second through the HTTP API
    events_month: int | None = None  # events applied per calendar month (UTC)
    store: str | None = None  # its own database (a SQLite path or mysql:// URL) instead of the shared one
    retention: str | None = None  # e.g. "3y", "18m", "90d": what closed longer ago goes (keel forget --policy)


@dataclass
class ApiKey:
    """A credential for the HTTP API (keel/keys.py). Only the hash is stored; `id` is the public prefix."""

    id: str
    hash: str
    account: str
    created_at: datetime
    tenant: str | None = None  # only this tenant of the account
    agent: str | None = None  # calls are made as this agent
    glob: bool = False  # may annotate(scope="global") into account/__global__
    revoked_at: datetime | None = None


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
    changed_at: datetime | None = None  # ingest clock of the link or unlink: the cursor of relation_changes
    closed_at: datetime | None = None  # transaction time of the unlink: when Keel learned valid_to (as_known_at)


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
    changed_at: datetime | None = None  # ingest clock of the last write (open or close): the cursor of changes()


class DuplicateKey(ValueError):
    """open_fact with an idempotency_key the subject already has. `fact` is the one already written,
    so a caller that lost a race returns it instead of writing twice."""

    def __init__(self, fact: Fact) -> None:
        super().__init__(f"idempotency key '{fact.idempotency_key}' is already on {fact.subject}")
        self.fact = fact


class Store(Protocol):
    """Primitives. Handlers call these; nothing else writes to the graph."""

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity: ...

    # recorded_at: the transaction time to write (the Ingestor passes an event's declared one); None is now
    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None, recorded_at: datetime | None = None) -> Relation: ...

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime, recorded_at: datetime | None = None) -> None: ...  # recorded_at: closed_at, optional

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact: ...  # may raise DuplicateKey

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT,
                   recorded_at: datetime | None = None, value: Any = ...) -> None: ...  # scope None: every scope; value: only that value

    # reads
    def entity(self, tenant: str, key: str) -> Entity | None: ...

    def facts(self, tenant: str, subject: str, at: datetime | None = None, since: datetime | None = None, until: datetime | None = None,
              known_at: datetime | None = None, multi: Iterable[str] = ()) -> list[Fact]: ...  # known_at, multi: optional, feature-detected

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]: ...

    def search(self, tenant: str, text: str) -> list[Entity | Fact]: ...

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]: ...  # Tools lists kinds with it

    def changes(self, tenant: str, after: datetime, alive_at: datetime | None = None) -> list[Fact]: ...  # written (changed_at) after a watermark; alive_at optional

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

    # retention (optional; the Ingestor requires it when the cartridge declares an event that `forgets: before`)
    # forget_before: in one transaction, facts (annotations included) and relations that closed at or before
    # `before` (valid_to <= before) are deleted; open ones and entities stay. The cutoff is kept (the latest
    # wins), so replay applies it again. Idempotent. Returns {"facts", "annotations", "relations"}.
    # def forget_before(self, tenant: str, before: datetime) -> dict[str, int]: ...
    # def cutoff(self, tenant: str) -> datetime | None: ...

    # push-down (optional, feature-detected by Tools; without them Tools reads the tenant in Python, same answers):
    # facts_between: the tenant's facts (no close marks) with valid_from in [since, until], as stored, in write order;
    # optionally one predicate, only those recorded by `recorded_by`, memory expired by `alive_at` (now) left out.
    # subjects_where: the entities of `kinds` a `where` of ASCII texts may hold for at `as_of` (as known at `known_at`),
    # as Tools reads facts for (author, task); key -> True when certain, False when Tools must check; None to decline.
    # rule_subjects: the entities a cartridge rule holds for at `as_of` (as known at `known_at`), for (author, task).
    # def facts_between(self, tenant: str, since: datetime, until: datetime, predicate: str | None = None,
    #                   recorded_by: datetime | None = None, alive_at: datetime | None = None) -> list[Fact]: ...
    # def subjects_where(self, tenant: str, cartridge: Any, kinds: list[str], where: dict[str, Any], as_of: datetime,
    #                    known_at: datetime | None = None, author: str | None = None, task: str | None = None) -> dict[str, bool] | None: ...
    # def rule_subjects(self, tenant: str, cartridge: Any, rule: str, as_of: datetime, known_at: datetime | None = None,
    #                   author: str | None = None, task: str | None = None) -> set[str]: ...

    # optional: every tenant with something stored, never the global partition (`--all-tenants` in jobs)
    # def tenants(self) -> list[str]: ...

    # backup (optional; `keel export`, `keel import`, `keel replay --check`): backup returns everything a restore
    # needs, {"entities", "relations", "facts" (close marks included), "ingested" (event ids), "cutoff"}, rows as
    # stored; restore writes one as it was into an empty tenant (raises ValueError otherwise), nothing recomputed.
    # def backup(self, tenant: str) -> dict[str, Any]: ...
    # def restore(self, tenant: str, backup: dict[str, Any]) -> None: ...
