"""In-memory bitemporal store. Reference implementation and test double.

Good enough to run cartridges, tests and demos. Not durable. `SqliteStore`
implements the same protocol on a file and reuses the filters below, so both
stores answer every query identically.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from keel.envelope import utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation

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
    )


def place_fact(new: Fact, same: Iterable[Fact]) -> Fact | None:
    """Slot `new` into the history of its predicate by valid_from, whatever order events arrive in:
    the fact current at new.valid_from closes there, and `new` closes at the next fact's start.
    Returns the fact that was closed, if any, so a durable store can persist that change."""
    prev = max((f for f in same if f.valid_from <= new.valid_from), key=lambda f: f.valid_from, default=None)
    nxt = min((f for f in same if f.valid_from > new.valid_from), key=lambda f: f.valid_from, default=None)
    closed = None
    if prev is not None and (prev.valid_to is None or prev.valid_to > new.valid_from):
        prev.valid_to = new.valid_from
        closed = prev
    if nxt is not None:
        new.valid_to = nxt.valid_from
    return closed


def filter_facts(
    facts: Iterable[Fact],
    at: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Fact]:
    out = list(facts)
    if at is not None:
        out = [f for f in out if f.valid_from <= at and (f.valid_to is None or f.valid_to > at)]
    if since is not None:
        out = [f for f in out if f.valid_to is None or f.valid_to >= since]
    if until is not None:
        out = [f for f in out if f.valid_from <= until]
    now = utcnow()
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
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        place_fact(f, [x for x in self._facts[tenant][subject] if x.predicate == predicate])
        self._facts[tenant][subject].append(f)
        self._all_facts[tenant].append(f)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime) -> None:
        # a close that predates a fact's start would invert its interval: that close is about an
        # earlier period and leaves this fact alone
        for f in self._facts[tenant][subject]:
            if f.predicate == predicate and f.valid_to is None and f.valid_from <= valid_to:
                f.valid_to = valid_to

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
    ) -> list[Fact]:
        return filter_facts(self._facts[tenant].get(subject, ()), at, since, until)

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

    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]:
        """Everything about one tenant, closed facts included: the audit trail."""
        yield from self._entities[tenant].values()
        yield from self._all_rels[tenant]
        yield from self._all_facts[tenant]

    def reset(self, tenant: str) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it."""
        for d in (self._entities, self._rels, self._all_rels, self._facts, self._all_facts, self._seen):
            d.pop(tenant, None)
