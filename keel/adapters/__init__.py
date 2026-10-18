"""Event adapters: anything that yields `Event`s.

Shipped: JsonlAdapter (keel.ingest) for development, PostgresTableAdapter
(table + polling with a cursor), webhook `build_app` (FastAPI, push),
KafkaAdapter and SqsAdapter (keel.adapters.bus, at-least-once), PostgresCdcAdapter
(keel.adapters.pgcdc, logical replication of your own tables).
Each adapter only produces envelopes; it never understands payloads.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class Following:
    """What `adapter.follow()` returns: the endless iterator, plus the adapter's `ack` so
    `Ingestor.ingest` can acknowledge after each commit (a generator cannot carry it)."""

    def __init__(self, adapter: Any, events: Iterator[Any]) -> None:
        self.adapter, self.events = adapter, events

    def __iter__(self) -> Iterator[Any]:
        return self.events

    @property
    def manual_ack(self) -> bool:
        return self.adapter.manual_ack

    @manual_ack.setter
    def manual_ack(self, value: bool) -> None:
        self.adapter.manual_ack = value

    def ack(self) -> None:
        self.adapter.ack()
