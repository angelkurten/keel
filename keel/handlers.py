"""Handler registry.

A handler is the only code that understands an event's payload. It maps
the payload to store primitives in a few lines. The core routes on
`event.type`; it never looks inside.

    registry = HandlerRegistry()

    @registry.on("inspection.created")
    def inspection_created(event, store):
        store.upsert_entity(event.tenant, "inspection", event.payload["id"], ref=event.source)
        store.link(event.tenant, event.subject, "has_inspection", f"inspection:{event.payload['id']}", event.occurred_at)
"""

from __future__ import annotations

from collections.abc import Callable

from keel.envelope import Event
from keel.store.base import Store

Handler = Callable[[Event, Store], None]


class UnhandledEvent(Exception):
    pass


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, event_type: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self._handlers.setdefault(event_type, []).append(fn)
            return fn

        return deco

    def register(self, event_type: str, fn: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(fn)

    def types(self) -> set[str]:
        return set(self._handlers)

    def dispatch(self, event: Event, store: Store) -> int:
        handlers = self._handlers.get(event.type)
        if not handlers:
            raise UnhandledEvent(f"no handler for event type '{event.type}'")
        for h in handlers:
            h(event, store)
        return len(handlers)

    def merge(self, other: HandlerRegistry) -> None:
        for t, hs in other._handlers.items():
            self._handlers.setdefault(t, []).extend(hs)
