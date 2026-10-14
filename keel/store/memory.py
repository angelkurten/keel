"""In-memory bitemporal store. Reference implementation and test double.

Good enough to run cartridges, tests and demos. Not durable. The Graphiti
and SQLite stores implement the same protocol.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from keel.envelope import utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation


class MemoryStore:
    def __init__(self) -> None:
        self._entities: dict[str, dict[str, Entity]] = defaultdict(dict)  # tenant -> key -> entity
        self._relations: dict[str, list[Relation]] = defaultdict(list)
        self._facts: dict[str, list[Fact]] = defaultdict(list)

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
        for r in self._relations[tenant]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                return r  # already open; idempotent
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self._relations[tenant].append(r)
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        for r in self._relations[tenant]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                r.valid_to = valid_to

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        # closing the previous open fact on the same predicate is what makes this bitemporal:
        # the old value keeps its history, it is not overwritten.
        self.close_fact(tenant, subject, predicate, valid_to=valid_from)
        f = Fact(
            tenant=tenant,
            subject=subject,
            predicate=predicate,
            value=value,
            valid_from=valid_from,
            recorded_at=kw.get("recorded_at") or utcnow(),
            source=kw.get("source"),
            scope=kw.get("scope", Scope.TENANT),
            corrected_by=kw.get("corrected_by"),
            evidence=kw.get("evidence"),
            expires_at=kw.get("expires_at"),
        )
        self._facts[tenant].append(f)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime) -> None:
        for f in self._facts[tenant]:
            if f.subject == subject and f.predicate == predicate and f.valid_to is None:
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
        out = [f for f in self._facts[tenant] if f.subject == subject]
        if at is not None:
            out = [f for f in out if f.valid_from <= at and (f.valid_to is None or f.valid_to > at)]
        if since is not None:
            out = [f for f in out if f.valid_to is None or f.valid_to >= since]
        if until is not None:
            out = [f for f in out if f.valid_from <= until]
        now = utcnow()
        out = [f for f in out if f.expires_at is None or f.expires_at > now]
        return sorted(out, key=lambda f: f.valid_from)

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        out = [r for r in self._relations[tenant] if r.src == key or r.dst == key]
        if rel:
            out = [r for r in out if r.rel == rel]
        if at is not None:
            out = [r for r in out if r.valid_from <= at and (r.valid_to is None or r.valid_to > at)]
        return out

    def search(self, tenant: str, text: str) -> list[Entity | Fact]:
        t = text.lower()
        hits: list[Entity | Fact] = []
        for e in self._entities[tenant].values():
            if t in e.key.lower() or any(t in str(v).lower() for v in e.attrs.values()):
                hits.append(e)
        for f in self._facts[tenant]:
            if t in f.predicate.lower() or t in str(f.value).lower():
                hits.append(f)
        return hits

    # ---- maintenance --------------------------------------------------------------

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        """Drop expired memory. Called by the consolidation job; never by handlers."""
        now = now or utcnow()
        before = len(self._facts[tenant])
        self._facts[tenant] = [f for f in self._facts[tenant] if f.expires_at is None or f.expires_at > now]
        return before - len(self._facts[tenant])

    def reset(self, tenant: str) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it."""
        self._entities.pop(tenant, None)
        self._relations.pop(tenant, None)
        self._facts.pop(tenant, None)
