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

from keel.agents import resolve
from keel.cartridge import Cartridge
from keel.envelope import utcnow
from keel.rules import conditions, evaluate
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation, Store


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


def _kind(subject: str) -> str:
    return subject.split(":", 1)[0]


class Tools:
    def __init__(self, cartridge: Cartridge, store: Store, tenant: str, agent: str | None = None) -> None:
        self.cartridge = cartridge
        self.store = store
        self.tenant = tenant  # tools are always scoped to one end-customer
        self.agent_id = agent  # who is calling; the host decides, Keel records
        self.agent = resolve(cartridge.agents, agent)  # the spec to enforce; None when the cartridge declares no agents

    # ---- registry enforcement: the agent never polices itself -------------------------------

    def _readable(self, subject: str) -> None:
        if self.agent and not self.agent.sees_kind(_kind(subject)):
            raise PermissionError(f"agent '{self.agent.id}' may not read kind '{_kind(subject)}'")

    def _visible(self, r: Relation, subject: str) -> bool:
        if self.agent is None:
            return True
        other = r.dst if r.src == subject else r.src
        return self.agent.sees_relation(r.rel) and self.agent.sees_kind(_kind(other))

    def context(self, subject: str, as_of: datetime | None = None) -> dict[str, Any]:
        """Truth about a subject now (or at `as_of`): entity, open facts and relations, the rules
        that apply to its kind, and the evaluation of every rule that carries a `when`."""
        self._readable(subject)
        ent = self.store.entity(self.tenant, subject)
        now = as_of or utcnow()
        facts = self.store.facts(self.tenant, subject, at=now)
        rels = [r for r in self.store.relations(self.tenant, subject, at=now) if self._visible(r, subject)]
        kind = _kind(subject)
        rules = {k: v for k, v in self.cartridge.rules.items() if v.get("applies_to") in (None, kind)}
        return _ser(
            {
                "subject": subject,
                "entity": ent,
                "facts": facts,
                "relations": [asdict(r) for r in rels],
                "rules": rules,
                "evaluation": {k: evaluate(v, facts, now) for k, v in rules.items() if conditions(v)},
                "as_of": now,
            }
        )

    def history(
        self,
        subject: str,
        since: datetime | None = None,
        until: datetime | None = None,
        predicate: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time: valid_from/valid_to (world) and recorded_at (when we learned it).
        Optionally one predicate only, or one memory scope only."""
        self._readable(subject)
        facts = self.store.facts(self.tenant, subject, since=since, until=until)
        if predicate:
            facts = [f for f in facts if f.predicate == predicate]
        if scope:
            facts = [f for f in facts if f.scope == Scope(scope)]
        return _ser(facts)

    def search(self, text: str) -> list[dict[str, Any]]:
        hits = self.store.search(self.tenant, text)
        if self.agent:
            hits = [h for h in hits if self.agent.sees_kind(h.kind if isinstance(h, Entity) else _kind(h.subject))]
        return _ser(hits)

    def annotate(
        self,
        subject: str,
        predicate: str,
        value: Any,
        scope: str = "task",
        evidence: str | None = None,
        by: str | None = None,
    ) -> dict[str, Any]:
        """Write earned memory. The registry says who may write which scope; the scope policy says
        whether evidence is required and how long it lives. Both are enforced here."""
        sc = Scope(scope)
        self._readable(subject)
        if self.agent and not self.agent.can_write(sc):
            raise PermissionError(f"agent '{self.agent.id}' may not write scope '{sc.value}'")
        self.cartridge.policy.check_write(sc, evidence)
        ttl = self.cartridge.policy.ttl.get(sc)
        now = utcnow()
        f = self.store.open_fact(
            self.tenant,
            subject,
            predicate,
            value,
            valid_from=now,
            source=f"annotate:{self.agent_id or by or 'agent'}",
            scope=sc,
            corrected_by=by or self.agent_id,
            evidence=evidence,
            expires_at=(now + ttl) if ttl else None,
        )
        return _ser(f)

    def related(self, subject: str, relation: str | None = None) -> list[dict[str, Any]]:
        self._readable(subject)
        rels = self.store.relations(self.tenant, subject, rel=relation, at=utcnow())
        out = []
        for r in rels:
            if not self._visible(r, subject):
                continue
            other = r.dst if r.src == subject else r.src
            out.append({"relation": r.rel, "direction": "out" if r.src == subject else "in", "subject": other, "since": r.valid_from.isoformat()})
        return out

    # for frameworks that want a schema list
    def spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "context", "args": ["subject", "as_of?"], "doc": self.context.__doc__},
            {"name": "history", "args": ["subject", "since?", "until?", "predicate?", "scope?"], "doc": self.history.__doc__},
            {"name": "search", "args": ["text"], "doc": "Entities and facts matching text."},
            {"name": "annotate", "args": ["subject", "predicate", "value", "scope?", "evidence?", "by?"], "doc": self.annotate.__doc__},
            {"name": "related", "args": ["subject", "relation?"], "doc": "Neighbors by relation type."},
        ]
