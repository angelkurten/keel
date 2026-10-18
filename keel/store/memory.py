"""In-memory bitemporal store. Reference implementation and test double.

Good enough to run cartridges, tests and demos. Not durable. `SqliteStore`
implements the same protocol on a file and reuses the filters below, so both
stores answer every query identically.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

from keel.envelope import utcnow
from keel.scopes import GLOBAL_TENANT, Scope
from keel.store.base import DuplicateKey, Entity, Fact, Relation

# ---- shared by every store: build a fact, filter rows the bitemporal way, search ---------


def new_fact(tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
    return Fact(
        tenant=tenant,
        subject=subject,
        predicate=predicate,
        value=value,
        valid_from=valid_from,
        recorded_at=kw.get("recorded_at") or utcnow(),
        source=kw.get("source"),
        scope=Scope(kw.get("scope", Scope.TENANT)),
        corrected_by=kw.get("corrected_by"),
        evidence=kw.get("evidence"),
        expires_at=kw.get("expires_at"),
        task=kw.get("task"),
        idempotency_key=kw.get("idempotency_key"),
        status=kw.get("status"),
        trace=kw.get("trace"),
        cartridge=kw.get("cartridge"),
    )


def author(f: Fact) -> str | None:
    """Who said it: `corrected_by` (the `by` of an annotation, or the agent) before the source, so two
    people writing through one agent are two authors."""
    return f.corrected_by or f.source


def is_annotation(f: Fact) -> bool:
    """Memory written through the tools (annotate) or a job over it (consolidate): not derived from
    events, so a replay keeps it instead of wiping it."""
    return bool(f.source) and f.source.startswith(("annotate:", "consolidate:"))


def in_scope(f: Fact, scope: Scope | str | None) -> bool:
    """close_fact's scope filter: None is every scope."""
    return scope is None or Scope(f.scope) == Scope(scope)


def same_slot(facts: Iterable[Fact], new: Fact, per_source: bool = False) -> list[Fact]:
    """The facts `new` takes its place among: same predicate, status and memory scope, and with
    `per_source` (a multi-valued predicate, a proposal) the same author. One open fact per slot.
    Scopes never close each other: a private note does not end a shared diagnosis, and a promotion
    leaves the narrower fact to its own lifetime. Task memory is per task; agent memory per agent."""

    def same(x: Fact) -> bool:
        if x.predicate != new.predicate or x.status != new.status or Scope(x.scope) != Scope(new.scope):
            return False
        if Scope(new.scope) == Scope.TASK and x.task != new.task:
            return False
        if Scope(new.scope) == Scope.AGENT and x.source != new.source:  # agent memory is its writer's
            return False
        return not per_source or author(x) == author(new)

    return [x for x in facts if same(x)]


def place_fact(new: Fact, same: Iterable[Fact]) -> Fact | None:
    """Slot `new` into the history of its predicate by valid_from, whatever order events arrive in:
    the fact current at new.valid_from closes there, and `new` closes at the next fact's start.
    Returns the fact that was closed, if any, so a durable store can persist that change."""
    prev = max((f for f in same if f.valid_from <= new.valid_from), key=lambda f: f.valid_from, default=None)
    nxt = min((f for f in same if f.valid_from > new.valid_from), key=lambda f: f.valid_from, default=None)
    closed = None
    if prev is not None and (prev.valid_to is None or prev.valid_to > new.valid_from):
        prev.valid_to = new.valid_from
        prev.closed_at = new.recorded_at  # we learned it ended when we learned what replaced it
        closed = prev
    if nxt is not None:
        new.valid_to = nxt.valid_from
        new.closed_at = new.recorded_at
    return closed


def as_known_at(facts: Iterable[Fact], known_at: datetime) -> list[Fact]:
    """The facts as Keel knew them at `known_at` (transaction time): only those recorded by then, and
    a close learned later is not applied yet. Copies, so the stored facts are never touched."""
    out = []
    for f in facts:
        if f.recorded_at is not None and f.recorded_at > known_at:
            continue
        out.append(replace(f, valid_to=None) if f.closed_at is not None and f.closed_at > known_at else f)
    return out


def changed_after(facts: Iterable[Fact], after: datetime) -> list[Fact]:
    """Facts recorded or closed after a watermark, oldest change first: an agent's "since my last run"."""
    out = [f for f in facts if (f.recorded_at and f.recorded_at > after) or (f.closed_at and f.closed_at > after)]
    return sorted(out, key=lambda f: max(t for t in (f.recorded_at, f.closed_at) if t is not None))


def filter_facts(
    facts: Iterable[Fact],
    at: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    known_at: datetime | None = None,
) -> list[Fact]:
    out = as_known_at(facts, known_at) if known_at is not None else list(facts)
    if at is not None:
        out = [f for f in out if f.valid_from <= at and (f.valid_to is None or f.valid_to > at)]
    if since is not None:
        out = [f for f in out if f.valid_to is None or f.valid_to >= since]
    if until is not None:
        out = [f for f in out if f.valid_from <= until]
    now = known_at or utcnow()  # memory alive then counts when asking what was known then
    out = [f for f in out if f.expires_at is None or f.expires_at > now]
    return sorted(out, key=lambda f: f.valid_from)


def filter_relations(rels: Iterable[Relation], rel: str | None = None, at: datetime | None = None) -> list[Relation]:
    out = list(rels)
    if rel:
        out = [r for r in out if r.rel == rel]
    if at is not None:
        out = [r for r in out if r.valid_from <= at and (r.valid_to is None or r.valid_to > at)]
    return out


def search_in(entities: Iterable[Entity], facts: Iterable[Fact], text: str) -> list[Entity | Fact]:
    t = text.lower()
    hits: list[Entity | Fact] = []
    for e in entities:
        if t in e.key.lower() or any(t in str(v).lower() for v in e.attrs.values()):
            hits.append(e)
    for f in facts:
        if t in f.predicate.lower() or t in str(f.value).lower():
            hits.append(f)
    return hits


class MemoryStore:
    """Indexed by subject: every read a handler makes is a dict lookup, not a scan of the tenant."""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, Entity]] = defaultdict(dict)  # tenant -> key -> entity
        self._rels: dict[str, dict[str, list[Relation]]] = defaultdict(lambda: defaultdict(list))  # tenant -> key -> touching
        self._all_rels: dict[str, list[Relation]] = defaultdict(list)  # tenant -> insertion order, for dump
        self._facts: dict[str, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))  # tenant -> subject -> facts
        self._all_facts: dict[str, list[Fact]] = defaultdict(list)
        self._seen: dict[str, set[str]] = defaultdict(set)  # applied event ids, per tenant
        self._tx: list[tuple[str, str]] | None = None
        self._watermark: dict[str, datetime] = {}  # tenant -> latest recorded_at or closed_at
        self._forgotten: dict[str, datetime] = {}  # tenant -> when it was erased (Store protocol: forget)

    def _mark(self, tenant: str, t: datetime | None) -> None:
        if t is not None and (tenant not in self._watermark or t > self._watermark[tenant]):
            self._watermark[tenant] = t

    # ---- writes (the primitives) -------------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity:
        key = f"{kind}:{id}"
        ent = self._entities[tenant].get(key)
        if ent is None:
            ent = Entity(tenant=tenant, kind=kind, id=id, ref=ref, attrs=dict(attrs))
            self._entities[tenant][key] = ent
        else:
            if ref:
                ent.ref = ref
            ent.attrs.update(attrs)
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation:
        for r in self._rels[tenant][src]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                return r  # already open; idempotent
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self._mark(tenant, r.recorded_at)
        self._rels[tenant][src].append(r)
        if dst != src:
            self._rels[tenant][dst].append(r)
        self._all_rels[tenant].append(r)
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        for r in self._rels[tenant][src]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                r.valid_to = valid_to

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        # the old value keeps its history, it is never overwritten; and the new fact takes its
        # place by valid_from even when it arrives late (see place_fact)
        key = kw.get("idempotency_key")
        if key is not None:  # ponytail: atomic under the GIL only; one process, as this store always is
            for x in self._facts[tenant][subject]:
                if x.idempotency_key == key:
                    raise DuplicateKey(x)
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        place_fact(f, same_slot(self._facts[tenant][subject], f, kw.get("per_source", False)))
        self._facts[tenant][subject].append(f)
        self._all_facts[tenant].append(f)
        self._mark(tenant, f.recorded_at)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT) -> None:
        # a close that predates a fact's start would invert its interval: that close is about an
        # earlier period and leaves this fact alone. `status` picks accepted facts (None) or proposals.
        # `scope` (tenant by default, what handlers write) keeps a handler from closing agent or task memory.
        now = utcnow()
        for f in self._facts[tenant][subject]:
            if f.predicate == predicate and f.status == status and f.valid_to is None and f.valid_from <= valid_to and in_scope(f, scope):
                f.valid_to, f.closed_at = valid_to, now
                self._mark(tenant, now)

    # ---- reads -------------------------------------------------------------------

    def entity(self, tenant: str, key: str) -> Entity | None:
        return self._entities[tenant].get(key)

    def facts(
        self,
        tenant: str,
        subject: str,
        at: datetime | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        known_at: datetime | None = None,
    ) -> list[Fact]:
        return filter_facts(self._facts[tenant].get(subject, ()), at, since, until, known_at)

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]:
        """Every entity of a kind (or all), ordered by key: the work list of a sweep."""
        return sorted((e for e in self._entities[tenant].values() if kind is None or e.kind == kind), key=lambda e: e.key)

    def changes(self, tenant: str, after: datetime) -> list[Fact]:
        """Facts recorded or closed after a watermark, across subjects."""
        now = utcnow()
        return [f for f in changed_after(self._all_facts[tenant], after) if f.expires_at is None or f.expires_at > now]

    def relation_changes(self, tenant: str, after: datetime) -> list[Relation]:
        """Relations recorded after a watermark (links; an unlink carries no transaction time yet)."""
        return [r for r in self._all_rels[tenant] if r.recorded_at and r.recorded_at > after]

    def watermark(self, tenant: str) -> datetime | None:
        """The latest transaction time in the tenant: a cursor for `changes`."""
        return self._watermark.get(tenant)

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        return filter_relations(self._rels[tenant].get(key, ()), rel, at)

    def search(self, tenant: str, text: str) -> list[Entity | Fact]:
        return search_in(self._entities[tenant].values(), self._all_facts[tenant], text)

    # ---- ingest bookkeeping -----------------------------------------------------------

    def seen(self, tenant: str, event_id: str) -> bool:
        return event_id in self._seen[tenant]

    def remember(self, tenant: str, event_id: str) -> bool:
        if event_id in self._seen[tenant]:
            return False
        self._seen[tenant].add(event_id)
        if self._tx is not None:
            self._tx.append((tenant, event_id))
        return True

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """No durability to protect here; this only forgets the ids remembered inside a chunk that
        raised, so the redelivery is not skipped as a duplicate."""
        if self._tx is not None:
            yield
            return
        self._tx = []
        try:
            yield
        except BaseException:
            for tenant, event_id in self._tx:
                self._seen[tenant].discard(event_id)
            raise
        finally:
            self._tx = None

    # ---- maintenance --------------------------------------------------------------

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        """Drop expired memory. Called by the consolidation job; never by handlers."""
        now = now or utcnow()
        before = len(self._all_facts[tenant])
        self._all_facts[tenant] = [f for f in self._all_facts[tenant] if f.expires_at is None or f.expires_at > now]
        for subject, facts in self._facts[tenant].items():
            self._facts[tenant][subject] = [f for f in facts if f.expires_at is None or f.expires_at > now]
        return before - len(self._all_facts[tenant])

    def close_task(self, tenant: str, task: str, now: datetime | None = None) -> int:
        """End a task: its memory expires now (reads stop seeing it; `expire` removes it later)."""
        now = now or utcnow()
        n = 0
        for f in self._all_facts[tenant]:
            if f.task == task and (f.expires_at is None or f.expires_at > now):
                f.expires_at = now
                n += 1
        return n

    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]:
        """Everything about one tenant, closed facts included: the audit trail."""
        yield from self._entities[tenant].values()
        yield from self._all_rels[tenant]
        yield from self._all_facts[tenant]

    def forget(self, tenant: str) -> dict[str, int]:
        """Erase the tenant and leave a tombstone (Store protocol). Idempotent."""
        if tenant == GLOBAL_TENANT:
            raise ValueError("the global partition is every tenant's; it cannot be forgotten")
        facts = self._all_facts.get(tenant, [])
        ann = sum(1 for f in facts if is_annotation(f))
        counts = {"entities": len(self._entities.get(tenant, {})), "relations": len(self._all_rels.get(tenant, [])),
                  "facts": len(facts) - ann, "annotations": ann, "ingested": len(self._seen.get(tenant, ()))}
        self.reset(tenant)
        self._forgotten.setdefault(tenant, utcnow())
        return counts

    def forgotten(self, tenant: str) -> datetime | None:
        return self._forgotten.get(tenant)

    def tenants(self) -> list[str]:
        """Every tenant with something stored (not the global partition), for jobs that sweep them all."""
        return sorted((set(self._entities) | set(self._all_facts) | set(self._all_rels)) - {GLOBAL_TENANT} - set(self._forgotten))

    def forgotten_tenants(self) -> dict[str, datetime]:
        return dict(self._forgotten)

    def reset(self, tenant: str, keep_annotations: bool = False) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it. With
        `keep_annotations`, what agents and people wrote (and consolidated) stays, closed history included."""
        kept = [f for f in self._all_facts.get(tenant, []) if is_annotation(f)] if keep_annotations else []
        for d in (self._entities, self._rels, self._all_rels, self._facts, self._all_facts, self._seen, self._watermark):
            d.pop(tenant, None)
        for f in kept:
            self._facts[tenant][f.subject].append(f)
            self._all_facts[tenant].append(f)
            self._mark(tenant, f.recorded_at)
            self._mark(tenant, f.closed_at)
