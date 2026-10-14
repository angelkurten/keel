"""Ingestor: reads events from an adapter, dispatches each to its handler.

The event log is the truth. The graph is derived from it, one small
upsert per event, and can be rebuilt by replaying the log for a tenant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

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
    unhandled: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


class Ingestor:
    def __init__(self, cartridge: Cartridge, store: Store) -> None:
        self.cartridge = cartridge
        self.store = store
        self.log: list[Event] = []  # append-only; a real deployment keeps this in the bus

    def ingest(self, events: Iterable[Event], strict: bool = False) -> IngestReport:
        report = IngestReport()
        for ev in events:
            self.log.append(ev)
            try:
                self.cartridge.registry.dispatch(ev, self.store)
                report.processed += 1
            except UnhandledEvent:
                report.unhandled.append(ev.type)
                if strict:
                    raise
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the stream
                report.errors.append((ev.id, f"{type(exc).__name__}: {exc}"))
                if strict:
                    raise
        return report

    def replay(self, tenant: str) -> IngestReport:
        """Recovery path: wipe the tenant's subgraph and rebuild it from the log."""
        reset = getattr(self.store, "reset", None)
        if reset:
            reset(tenant)
        events = [e for e in self.log if e.tenant == tenant]
        self.log = [e for e in self.log if e.tenant != tenant]
        return self.ingest(events)
