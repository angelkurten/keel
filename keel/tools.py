"""The five tools. Every agent, in every cartridge, talks to the graph through these.

They are plain functions first, so any framework can call them directly;
`keel.mcp_server` wraps the same object for MCP.

    context(subject)                   what is true now about this subject
    history(subject, since, until)     what was true, and when we learned it
    search(text)                       entities and facts matching text
    annotate(scope, fact, evidence)    an agent or a human writes what it learned
    related(subject, relation)         neighbors by relation type
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from keel.cartridge import Cartridge
from keel.envelope import utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Store


def _ser(o: Any) -> Any:
    if isinstance(o, (Entity, Fact)):
        d = asdict(o)
        return {k: _ser(v) for k, v in d.items()}
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Scope):
        return o.value
    if isinstance(o, dict):
        return {k: _ser(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_ser(v) for v in o]
    return o


class Tools:
    def __init__(self, cartridge: Cartridge, store: Store, tenant: str) -> None:
        self.cartridge = cartridge
        self.store = store
        self.tenant = tenant  # tools are always scoped to one end-customer

    def context(self, subject: str) -> dict[str, Any]:
        """Current truth about a subject: entity, open facts, open relations, applicable rules."""
        ent = self.store.entity(self.tenant, subject)
        now = utcnow()
        facts = self.store.facts(self.tenant, subject, at=now)
        rels = self.store.relations(self.tenant, subject, at=now)
        kind = subject.split(":", 1)[0]
        rules = {k: v for k, v in self.cartridge.rules.items() if v.get("applies_to") in (None, kind)}
        return _ser(
            {
                "subject": subject,
                "entity": ent,
                "facts": facts,
                "relations": [asdict(r) for r in rels],
                "rules": rules,
                "as_of": now,
            }
        )

    def history(self, subject: str, since: datetime | None = None, until: datetime | None = None) -> list[dict[str, Any]]:
        """Facts over time: valid_from/valid_to (world) and recorded_at (when we learned it)."""
        return _ser(self.store.facts(self.tenant, subject, since=since, until=until))

    def search(self, text: str) -> list[dict[str, Any]]:
        return _ser(self.store.search(self.tenant, text))

    def annotate(
        self,
        subject: str,
        predicate: str,
        value: Any,
        scope: str = "task",
        evidence: str | None = None,
        by: str | None = None,
    ) -> dict[str, Any]:
        """Write earned memory. Scope policy decides whether evidence is required and how long it lives."""
        sc = Scope(scope)
        self.cartridge.policy.check_write(sc, evidence)
        ttl = self.cartridge.policy.ttl.get(sc)
        now = utcnow()
        f = self.store.open_fact(
            self.tenant,
            subject,
            predicate,
            value,
            valid_from=now,
            source=f"annotate:{by or 'agent'}",
            scope=sc,
            corrected_by=by,
            evidence=evidence,
            expires_at=(now + ttl) if ttl else None,
        )
        return _ser(f)

    def related(self, subject: str, relation: str | None = None) -> list[dict[str, Any]]:
        rels = self.store.relations(self.tenant, subject, rel=relation, at=utcnow())
        out = []
        for r in rels:
            other = r.dst if r.src == subject else r.src
            out.append({"relation": r.rel, "direction": "out" if r.src == subject else "in", "subject": other, "since": r.valid_from.isoformat()})
        return out

    # for frameworks that want a schema list
    def spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "context", "args": ["subject"], "doc": self.context.__doc__},
            {"name": "history", "args": ["subject", "since?", "until?"], "doc": self.history.__doc__},
            {"name": "search", "args": ["text"], "doc": "Entities and facts matching text."},
            {"name": "annotate", "args": ["subject", "predicate", "value", "scope?", "evidence?", "by?"], "doc": self.annotate.__doc__},
            {"name": "related", "args": ["subject", "relation?"], "doc": "Neighbors by relation type."},
        ]
