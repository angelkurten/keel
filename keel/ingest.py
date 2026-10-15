"""Ingestor: reads events from an adapter, dispatches each to its handler.

The event log is the truth. The graph is derived from it, one small
upsert per event, and can be rebuilt by replaying the log for a tenant.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from keel.cartridge import Cartridge
from keel.envelope import Event
from keel.handlers import UnhandledEvent
from keel.store.base import Store


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
    def __init__(self, cartridge: Cartridge, store: Store) -> None:
        self.cartridge = cartridge
        self.store = store
        self.log: list[Event] = []  # append-only; a real deployment keeps this in the bus

    def ingest(self, events: Iterable[Event], strict: bool = False, batch: int = 1) -> IngestReport:
        """Dispatch every event. When the store has transactions, each event is one (its primitives and
        `remember` land together); a bulk caller (a file, an HTTP batch) passes `batch` to commit every
        `batch` events instead, which is what makes a networked database fast. A chunk that raises in
        strict mode is rolled back whole and will be redelivered whole; dedupe takes care of the rest."""
        report = IngestReport()
        tx = getattr(self.store, "transaction", None)
        for chunk in _chunks(events, max(1, batch)):
            with tx() if tx else nullcontext():
                for ev in chunk:
                    if self.store.seen(ev.tenant, ev.id):
                        report.duplicates += 1
                        continue
                    self.log.append(ev)
                    try:
                        self.cartridge.registry.dispatch(ev, self.store)
                        report.processed += 1
                        self.store.remember(ev.tenant, ev.id)  # after the handler ran: a failed event is not remembered
                    except UnhandledEvent:
                        report.unhandled.append(ev.type)
                        if strict:
                            raise
                    except Exception as exc:
                        report.errors.append((ev.id, f"{type(exc).__name__}: {exc}"))
                        if strict:
                            raise
        return report

    def replay(self, tenant: str, events: Iterable[Event] | None = None) -> IngestReport:
        """Recovery path: wipe the tenant's subgraph and rebuild it from the log.

        `events` is the external log (the bus, the outbox table, a .jsonl file): the truth a
        deployment replays from. Without it, the in-memory log of this process is used.
        """
        reset = getattr(self.store, "reset", None)
        if reset:
            reset(tenant)
        if events is None:
            events, self.log = [e for e in self.log if e.tenant == tenant], [e for e in self.log if e.tenant != tenant]
        return self.ingest((e for e in events if e.tenant == tenant), batch=500)
