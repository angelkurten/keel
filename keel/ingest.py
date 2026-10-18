"""Ingestor: reads events from an adapter, dispatches each to its handler.

The event log is the truth. The graph is derived from it, one small
upsert per event, and can be rebuilt by replaying the log for a tenant.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keel.cartridge import Cartridge
from keel.envelope import Event, utcnow
from keel.handlers import UnhandledEvent
from keel.store.base import Store


def _accepts(fn: object, name: str) -> bool:
    import inspect

    try:
        return name in inspect.signature(fn).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


class Adapter(Iterable[Event]):
    """Anything that yields Events. See keel.adapters for Postgres, webhook, Kafka."""


class JsonlAdapter:
    """Development adapter: one event per line in a .jsonl file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __iter__(self) -> Iterator[Event]:
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    yield Event.model_validate(json.loads(line))


@dataclass
class IngestReport:
    processed: int = 0
    duplicates: int = 0  # ids already applied: skipped, so at-least-once delivery and client retries are safe
    unhandled: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    forgotten: list[tuple[str, dict[str, int]]] = field(default_factory=list)  # (tenant, what forget deleted), for an audit log
    skipped: int = 0  # events for a tenant already forgotten: never applied, so a late redelivery cannot revive it
    retained: list[tuple[str, str, dict[str, int]]] = field(default_factory=list)  # (tenant, cutoff, what forget_before deleted)


BATCH = 5000  # events per commit for a bulk load (a file, a replay): fewer prefetch statements per event


def _chunks(events: Iterable[Event], size: int) -> Iterator[list[Event]]:
    """Lists of up to `size` events. With size 1 a streaming adapter is never buffered."""
    buf: list[Event] = []
    for ev in events:
        buf.append(ev)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


class Ingestor:
    def __init__(self, cartridge: Cartridge, store: Store, keep_log: bool = False) -> None:
        self.cartridge = cartridge
        self.store = store
        # opt-in copy of every event, for replay(tenant) without an external log: a development
        # convenience that grows with every event, so a server or a bulk load leaves it off
        self.keep_log = keep_log
        self.log: list[Event] = []
        # event types the cartridge declares `forgets: tenant`: Keel erases the tenant, no handler runs
        self.forgets = {t for t, spec in cartridge.events.items() if (spec or {}).get("forgets") == "tenant"}
        # event types declared `forgets: before`: Keel deletes what closed before the event's occurred_at (retention)
        self.retains = {t for t, spec in cartridge.events.items() if (spec or {}).get("forgets") == "before"}
        self._gone: set[str] = set()
        if self.forgets and not callable(getattr(store, "forget", None)):
            raise TypeError(f"the cartridge declares events that forget a tenant ({', '.join(sorted(self.forgets))}); {type(store).__name__} has no forget()")
        if self.retains and not callable(getattr(store, "forget_before", None)):
            raise TypeError(f"the cartridge declares retention events ({', '.join(sorted(self.retains))}); {type(store).__name__} has no forget_before()")

    def ingest(self, events: Iterable[Event], strict: bool = False, batch: int = 1) -> IngestReport:
        """Dispatch every event. When the store has transactions, each event is one (its primitives and
        `remember` land together); a bulk caller (a file, an HTTP batch) passes `batch` to commit every
        `batch` events instead, which is what makes a networked database fast. A chunk that raises in
        strict mode is rolled back whole and will be redelivered whole; dedupe takes care of the rest.

        An adapter with `ack()` (the table and bus adapters) is acknowledged after each commit, never
        before: its cursor or offsets cover only what is stored."""
        return self._ingest(events, strict, batch, source=events)

    def _ingest(self, events: Iterable[Event], strict: bool, batch: int, source: Any, metered: bool = True) -> IngestReport:
        ack = getattr(source, "ack", None)
        if ack:
            source.manual_ack = True
        report = IngestReport()
        tx = getattr(self.store, "transaction", None)
        bulk = getattr(self.store, "bulk", None)  # SQL stores: a batch is read and written in a few statements
        forgotten = getattr(self.store, "forgotten", None)
        meter = getattr(self.store, "meter", None) if metered else None  # keel/usage.py; a replay is not new usage
        for chunk in _chunks(events, max(1, batch)):
            applied: Counter[str] = Counter()
            erases = any(ev.type in self.forgets or ev.type in self.retains for ev in chunk)  # a batch overlay would write back what forget deleted
            scope = bulk(chunk) if bulk and len(chunk) > 1 and not erases else (tx() if tx else nullcontext())
            # a tombstone is permanent, so only tenants not yet known to be forgotten are asked, once per chunk
            self._gone |= {t for t in {ev.tenant for ev in chunk} - self._gone if forgotten and forgotten(t)}
            with scope:
                for ev in chunk:
                    if ev.tenant in self._gone:
                        report.skipped += 1
                        continue
                    if ev.type in self.forgets:
                        report.forgotten.append((ev.tenant, self.store.forget(ev.tenant)))
                        self._gone.add(ev.tenant)
                        report.processed += 1
                        applied[ev.tenant] += 1
                        continue
                    if not self.store.remember(ev.tenant, ev.id):  # one statement; the transaction forgets it if the handler fails
                        report.duplicates += 1
                        continue
                    applied[ev.tenant] += 1
                    if ev.type in self.retains:
                        counts = self.store.forget_before(ev.tenant, ev.occurred_at)
                        report.retained.append((ev.tenant, ev.occurred_at.isoformat(), counts))
                        report.processed += 1
                        continue
                    if self.keep_log:
                        self.log.append(ev)
                    try:
                        self.cartridge.registry.dispatch(ev, self.store)
                        report.processed += 1
                    except UnhandledEvent:
                        report.unhandled.append(ev.type)
                        if strict:
                            raise
                    except Exception as exc:
                        report.errors.append((ev.id, f"{type(exc).__name__}: {exc}"))
                        if strict:
                            raise
            if meter and applied:  # after the commit: a crash in between undercounts, never bills twice
                day = utcnow().date().isoformat()
                meter([(t, day, "events", n) for t, n in applied.items()])
            if ack:
                ack()
        return report

    def replay(self, tenant: str, events: Iterable[Event] | None = None) -> IngestReport:
        """Recovery path: wipe the tenant's event-derived subgraph and rebuild it from the log. What
        agents and people wrote through `annotate` (and consolidations of it) is kept and the events
        land around it, as they did when they first arrived.

        `events` is the external log (the bus, the outbox table, a .jsonl file): the truth a
        deployment replays from. Without it, the in-memory log of this process is used (keep_log=True).
        """
        if events is None and not self.keep_log:
            raise ValueError("replay needs events (the bus, the outbox table, a .jsonl file), or an Ingestor(keep_log=True)")
        reset = getattr(self.store, "reset", None)
        if reset:
            # annotations are earned memory, not derived from events: the log cannot rebuild them, so they stay
            reset(tenant, keep_annotations=True) if _accepts(reset, "keep_annotations") else reset(tenant)
        if events is None:
            events, self.log = [e for e in self.log if e.tenant == tenant], [e for e in self.log if e.tenant != tenant]
        report = self._ingest((e for e in events if e.tenant == tenant), False, BATCH, source=events, metered=False)
        cutoff = getattr(self.store, "cutoff", None)
        if cutoff and (cut := cutoff(tenant)) is not None:  # retention outlives a rebuild: what the log brought back goes again
            report.retained.append((tenant, cut.isoformat(), self.store.forget_before(tenant, cut)))
        return report
