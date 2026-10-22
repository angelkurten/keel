"""In-memory bitemporal store. Reference implementation and test double.

Good enough to run cartridges, tests and demos. Not durable. `SqliteStore`
implements the same protocol on a file and reuses the filters below, so both
stores answer every query identically.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from keel.envelope import utcnow
from keel.scopes import Scope, is_global
from keel.store.base import Account, ApiKey, DuplicateKey, Entity, Fact, Relation

# ---- shared by every store: build a fact, filter rows the bitemporal way, search ---------


def new_fact(tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
    now = utcnow()
    return Fact(
        tenant=tenant,
        subject=subject,
        predicate=predicate,
        value=value,
        valid_from=valid_from,
        recorded_at=kw.get("recorded_at") or now,
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
        changed_at=now,
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
    at = succession(new)
    prev = max((f for f in same if f is not new and succession(f) <= at), key=succession, default=None)
    nxt = min((f for f in same if succession(f) > at), key=succession, default=None)
    closed = None
    if prev is not None and (prev.valid_to is None or prev.valid_to > new.valid_from):
        prev.valid_to = new.valid_from
        prev.closed_at = _later(prev.recorded_at, new.recorded_at)  # we learned it ended once we knew both
        prev.changed_at = new.changed_at
        closed = prev
    elif prev is not None and prev.valid_to == new.valid_from:
        # already closed there by a successor that was recorded later than this one (a correction that
        # arrived before the value it corrects): the end was known as soon as the earliest successor was
        learned = _later(prev.recorded_at, new.recorded_at)
        if learned is not None and (prev.closed_at is None or learned < prev.closed_at):
            prev.closed_at = learned
            prev.changed_at = new.changed_at
            closed = prev
    if nxt is not None:
        new.valid_to = nxt.valid_from
        new.closed_at = _later(new.recorded_at, nxt.recorded_at)  # the next one may have been recorded later
    return closed


def succession(f: Fact) -> tuple[Any, ...]:
    """The order of a predicate's history: by valid_from, and facts starting at the same instant (two
    changes in one second) by when they were recorded, then by event and value. A total order on what
    the facts carry, never on arrival, so any arrival order builds the same history."""
    return (f.valid_from, f.recorded_at or valid_never, f.evidence or "", json.dumps(f.value, sort_keys=True, default=str))


valid_never = datetime.min.replace(tzinfo=timezone.utc)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    """The later of two transaction times: arrival order then never changes when a close was learned."""
    return max((t for t in (a, b) if t is not None), default=None)


def as_known_at(facts: Iterable[Fact], known_at: datetime, multi: Iterable[str] = ()) -> list[Fact]:
    """The facts as Keel knew them at `known_at` (transaction time): only those recorded by then, each
    closed where the next fact of its slot known by then starts, or by a close already learned. Closes
    are rebuilt from what was known, not read off the stored row: a late fact that later moved a close
    must not reopen the fact before it. `multi` names multi-valued predicates (one slot per author, as
    proposals). Copies, so the stored facts are never touched."""
    multi = set(multi)
    slots: dict[tuple[Any, ...], list[Fact]] = defaultdict(list)
    for f in facts:
        if f.recorded_at is None or f.recorded_at <= known_at:
            slots[_slot(f, f.status is not None or f.predicate in multi)].append(f)
    out = []
    for group in slots.values():
        group.sort(key=succession)
        for f, nxt in zip(group, [*group[1:], None]):
            ends = [(nxt.valid_from, _later(f.recorded_at, nxt.recorded_at))] if nxt is not None else []
            if f.valid_to is not None and (f.closed_at is None or f.closed_at <= known_at):  # a close learned by then
                ends.append((f.valid_to, f.closed_at))
            valid_to, closed_at = min(ends, key=lambda e: e[0], default=(None, None))
            out.append(f if (valid_to, closed_at) == (f.valid_to, f.closed_at) else replace(f, valid_to=valid_to, closed_at=closed_at))
    return out


def _slot(f: Fact, per_source: bool) -> tuple[Any, ...]:
    """`same_slot` as a key: the facts that close each other."""
    scope = Scope(f.scope)
    return (f.tenant, f.subject, f.predicate, f.status, scope, f.task if scope == Scope.TASK else None,
            f.source if scope == Scope.AGENT else None, author(f) if per_source else None)


def changed_at(f: Fact | Relation) -> datetime | None:
    """When a fact or link was last written. Rows from before `changed_at` existed fall back to their transaction times."""
    return f.changed_at or max((t for t in (f.recorded_at, getattr(f, "closed_at", None)) if t is not None), default=None)


def changed_after(facts: Iterable[Fact], after: datetime) -> list[Fact]:
    """Facts written (opened or closed) after a watermark, oldest change first: an agent's "since my last run".
    By the ingest clock, not transaction time: an event declaring an old recorded_at still shows up as a change."""
    out = [f for f in facts if (changed_at(f) or after) > after]
    return sorted(out, key=changed_at)


def filter_facts(
    facts: Iterable[Fact],
    at: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    known_at: datetime | None = None,
    multi: Iterable[str] = (),
) -> list[Fact]:
    out = as_known_at(facts, known_at, multi) if known_at is not None else list(facts)
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
        self._tx: list[Callable[[], None]] | None = None  # inside transaction(): how to undo each write, in order
        self._watermark: dict[str, datetime] = {}  # tenant -> latest changed_at (ingest clock)
        self._forgotten: dict[str, datetime] = {}  # tenant -> when it was erased (Store protocol: forget)
        self._cutoffs: dict[str, datetime] = {}  # tenant -> retention cutoff (forget_before)
        self._keys: dict[str, ApiKey] = {}  # id -> API key (keel/keys.py)
        self._accounts: dict[str, Account] = {}  # id -> limits (keel/accounts.py)
        self._metering: dict[tuple[str, str, str], int] = defaultdict(int)  # (tenant, day, item) -> n (keel/usage.py)

    def _mark(self, tenant: str, t: datetime | None) -> None:
        if t is not None and (tenant not in self._watermark or t > self._watermark[tenant]):
            if self._tx is not None:
                old = self._watermark.get(tenant)
                self._tx.append(lambda: self._watermark.pop(tenant, None) if old is None else self._watermark.__setitem__(tenant, old))
            self._watermark[tenant] = t

    def _keep(self, obj: Entity | Relation | Fact) -> None:
        """Inside a transaction, remember an object's fields before a write changes them in place."""
        if self._tx is not None:
            saved = {k: dict(v) if isinstance(v, dict) else v for k, v in vars(obj).items()}
            self._tx.append(lambda: vars(obj).update(saved))

    def _added(self, *lists: list[Any], item: Any) -> None:
        """Inside a transaction, remember that `item` was appended to these lists."""
        if self._tx is not None:
            self._tx.append(lambda: [lst.remove(item) for lst in lists])

    # ---- writes (the primitives) -------------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity:
        key = f"{kind}:{id}"
        ent = self._entities[tenant].get(key)
        if ent is None:
            ent = Entity(tenant=tenant, kind=kind, id=id, ref=ref, attrs=dict(attrs))
            self._entities[tenant][key] = ent
            if self._tx is not None:
                self._tx.append(lambda: self._entities[tenant].pop(key, None))
        else:
            self._keep(ent)
            if ref:
                ent.ref = ref
            ent.attrs.update(attrs)
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None, recorded_at: datetime | None = None) -> Relation:
        for r in self._rels[tenant][src]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                return r  # already open; idempotent
        now = utcnow()
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=recorded_at or now, source=source, changed_at=now)
        self._mark(tenant, now)
        self._rels[tenant][src].append(r)
        if dst != src:
            self._rels[tenant][dst].append(r)
        self._all_rels[tenant].append(r)
        self._added(self._rels[tenant][src], *([self._rels[tenant][dst]] if dst != src else []), self._all_rels[tenant], item=r)
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        for r in self._rels[tenant][src]:
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                self._keep(r)
                r.valid_to, r.changed_at = valid_to, utcnow()
                self._mark(tenant, r.changed_at)

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        # the old value keeps its history, it is never overwritten; and the new fact takes its
        # place by valid_from even when it arrives late (see place_fact)
        key = kw.get("idempotency_key")
        if key is not None:  # ponytail: atomic under the GIL only; one process, as this store always is
            for x in self._facts[tenant][subject]:
                if x.idempotency_key == key:
                    raise DuplicateKey(x)
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        slot = same_slot(self._facts[tenant][subject], f, kw.get("per_source", False))
        for x in slot:  # place_fact may move a neighbour's close
            self._keep(x)
        place_fact(f, slot)
        self._facts[tenant][subject].append(f)
        self._all_facts[tenant].append(f)
        self._added(self._facts[tenant][subject], self._all_facts[tenant], item=f)
        self._mark(tenant, f.changed_at)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT,
                   recorded_at: datetime | None = None) -> None:
        # a close that predates a fact's start would invert its interval: that close is about an
        # earlier period and leaves this fact alone. `status` picks accepted facts (None) or proposals.
        # `scope` (tenant by default, what handlers write) keeps a handler from closing agent or task memory.
        now = utcnow()
        for f in self._facts[tenant][subject]:
            if f.predicate == predicate and f.status == status and f.valid_to is None and f.valid_from <= valid_to and in_scope(f, scope):
                self._keep(f)
                f.valid_to, f.closed_at, f.changed_at = valid_to, recorded_at or now, now
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
        multi: Iterable[str] = (),
    ) -> list[Fact]:
        return filter_facts(self._facts[tenant].get(subject, ()), at, since, until, known_at, multi)

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]:
        """Every entity of a kind (or all), ordered by key: the work list of a sweep."""
        return sorted((e for e in self._entities[tenant].values() if kind is None or e.kind == kind), key=lambda e: e.key)

    def changes(self, tenant: str, after: datetime, alive_at: datetime | None = None) -> list[Fact]:
        """Facts recorded or closed after a watermark, across subjects; memory expired by `alive_at` (now by default) is left out."""
        now = alive_at or utcnow()
        return [f for f in changed_after(self._all_facts[tenant], after) if f.expires_at is None or f.expires_at > now]

    def relation_changes(self, tenant: str, after: datetime) -> list[Relation]:
        """Relations linked or unlinked after a watermark (by the ingest clock, like changes)."""
        return [r for r in self._all_rels[tenant] if (changed_at(r) or after) > after]

    def watermark(self, tenant: str) -> datetime | None:
        """The latest write in the tenant (ingest clock): a cursor for `changes`."""
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
            self._tx.append(lambda: self._seen[tenant].discard(event_id))
        return True

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """A chunk that raises is rolled back whole, as on the SQL stores: every write it made is undone
        (newest first) and its event ids are forgotten, so the redelivery applies it cleanly.
        ponytail: an undo log of the primitives; forget/forget_before inside a failed chunk are not undone."""
        if self._tx is not None:
            yield
            return
        self._tx = []
        try:
            yield
        except BaseException:
            for undo in reversed(self._tx):
                undo()
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
        if is_global(tenant):
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

    def forget_before(self, tenant: str, before: datetime) -> dict[str, int]:
        """Delete what closed at or before `before` and keep the cutoff (Store protocol). Idempotent."""
        if is_global(tenant):
            raise ValueError("the global partition is every tenant's; retention runs per tenant")
        old = lambda x: x.valid_to is not None and x.valid_to <= before
        gone = [f for f in self._all_facts.get(tenant, []) if old(f)]
        ann = sum(1 for f in gone if is_annotation(f))
        rels = sum(1 for r in self._all_rels.get(tenant, []) if old(r))
        if gone:
            self._all_facts[tenant] = [f for f in self._all_facts[tenant] if not old(f)]
            for subject, facts in self._facts[tenant].items():
                self._facts[tenant][subject] = [f for f in facts if not old(f)]
        if rels:
            self._all_rels[tenant] = [r for r in self._all_rels[tenant] if not old(r)]
            for key, touching in self._rels[tenant].items():
                self._rels[tenant][key] = [r for r in touching if not old(r)]
        if tenant not in self._cutoffs or before > self._cutoffs[tenant]:
            self._cutoffs[tenant] = before
        return {"facts": len(gone) - ann, "annotations": ann, "relations": rels}

    def cutoff(self, tenant: str) -> datetime | None:
        return self._cutoffs.get(tenant)

    def tenants(self) -> list[str]:
        """Every tenant with something stored (not the global partition), for jobs that sweep them all."""
        return sorted(t for t in (set(self._entities) | set(self._all_facts) | set(self._all_rels)) - set(self._forgotten) if not is_global(t))

    def forgotten_tenants(self) -> dict[str, datetime]:
        return dict(self._forgotten)

    # ---- metering (keel/usage.py): counters, not tenant data, so forget and reset leave them ----

    def meter(self, rows: list[tuple[str, str, str, int]]) -> None:
        for tenant, day, item, n in rows:
            self._metering[(tenant, day, item)] += n

    def usage(self, since: str | None = None, until: str | None = None) -> list[tuple[str, str, str, int]]:
        return [(*k, n) for k, n in sorted(self._metering.items()) if (since or "") <= k[1] < (until or "9999")]

    # ---- API keys (keel/keys.py): not tenant data, so forget and reset leave them ----

    def put_key(self, key: ApiKey) -> None:
        self._keys[key.id] = key

    def put_account(self, account: Account) -> None:
        self._accounts[account.id] = account

    def account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def accounts(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.id)

    def api_key(self, digest: str) -> ApiKey | None:
        return next((k for k in self._keys.values() if k.hash == digest), None)

    def api_keys(self, account: str | None = None) -> list[ApiKey]:
        return sorted((k for k in self._keys.values() if account in (None, k.account)), key=lambda k: (k.account, k.created_at))

    def revoke_key(self, key_id: str, at: datetime) -> bool:
        k = self._keys.get(key_id)
        if k is None or k.revoked_at is not None:
            return False
        k.revoked_at = at
        return True

    def reset(self, tenant: str, keep_annotations: bool = False) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it. With
        `keep_annotations`, what agents and people wrote (and consolidated) stays, closed history included."""
        kept = [f for f in self._all_facts.get(tenant, []) if is_annotation(f)] if keep_annotations else []
        for d in (self._entities, self._rels, self._all_rels, self._facts, self._all_facts, self._seen, self._watermark):
            d.pop(tenant, None)
        for f in kept:
            self._facts[tenant][f.subject].append(f)
            self._all_facts[tenant].append(f)
            self._mark(tenant, changed_at(f))
