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
from keel.rules import Calendar, evaluate
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


NEIGHBOR_CAP = 50  # `context(depth=1)` stops here and says so; ask `related` for the full list


class _Graph:
    """What rules may look at, read through the store at one instant. Rules are the product's
    semantics, so they see the whole tenant whatever the calling agent's read slice is."""

    def __init__(self, store: Store, tenant: str, as_of: datetime, calendar: Calendar, symmetric: set[str]) -> None:
        self.store, self.tenant, self.as_of, self.calendar, self.symmetric = store, tenant, as_of, calendar, symmetric
        self._facts: dict[str, list[Fact]] = {}

    def facts(self, subject: str) -> list[Fact]:
        if subject not in self._facts:
            self._facts[subject] = self.store.facts(self.tenant, subject, at=self.as_of)
        return self._facts[subject]

    def history(self, subject: str, predicate: str) -> list[Fact]:
        return [f for f in self.store.facts(self.tenant, subject, until=self.as_of) if f.predicate == predicate]

    def neighbors(self, subject: str, rel: str, direction: str) -> list[str]:
        out: list[str] = []
        for r in self.store.relations(self.tenant, subject, rel=rel, at=self.as_of):
            if rel not in self.symmetric and direction == "out" and r.src != subject:
                continue
            if rel not in self.symmetric and direction == "in" and r.dst != subject:
                continue
            other = r.dst if r.src == subject else r.src
            if other not in out:
                out.append(other)
        return out


class Tools:
    def __init__(self, cartridge: Cartridge, store: Store, tenant: str, agent: str | None = None) -> None:
        self.cartridge = cartridge
        self.store = store
        self.tenant = tenant  # tools are always scoped to one end-customer
        self.agent_id = agent  # who is calling; the host decides, Keel records
        self.agent = resolve(cartridge.agents, agent)  # the spec to enforce; None when the cartridge declares no agents
        self.symmetric = {rel for rel, spec in cartridge.relations.items() if isinstance(spec, dict) and spec.get("symmetric")}

    # ---- registry enforcement: the agent never polices itself -------------------------------

    def _readable(self, subject: str) -> None:
        if self.agent and not self.agent.sees_kind(_kind(subject)):
            raise PermissionError(f"agent '{self.agent.id}' may not read kind '{_kind(subject)}'")

    def _visible(self, r: Relation, subject: str) -> bool:
        if self.agent is None:
            return True
        other = r.dst if r.src == subject else r.src
        return self.agent.sees_relation(r.rel) and self.agent.sees_kind(_kind(other))

    def _graph(self, now: datetime) -> _Graph:
        return _Graph(self.store, self.tenant, now, self.cartridge.calendar, self.symmetric)

    def _rules_for(self, subject: str) -> dict[str, Any]:
        kind = _kind(subject)
        return {k: v for k, v in self.cartridge.rules.items() if v.get("applies_to") in (None, kind)}

    def _evaluation(self, subject: str, graph: _Graph) -> dict[str, bool | None]:
        return {k: evaluate(v, subject, graph.as_of, graph) for k, v in self._rules_for(subject).items() if v.get("when")}

    def _dedupe(self, rels: list[Relation], subject: str) -> list[Relation]:
        """A symmetric relation recorded from both ends is one edge."""
        seen: set[tuple[str, frozenset[str]]] = set()
        out = []
        for r in rels:
            key = (r.rel, frozenset({r.src, r.dst}))
            if r.rel in self.symmetric:
                if key in seen:
                    continue
                seen.add(key)
            out.append(r)
        return out

    def context(self, subject: str, as_of: datetime | None = None, depth: int = 0) -> dict[str, Any]:
        """Truth about a subject now (or at `as_of`): entity, open facts and relations, the rules
        that apply to its kind, and the evaluation of every rule that carries a `when`. With
        `depth=1`, also each neighbour's entity, open facts and evaluation (up to NEIGHBOR_CAP)."""
        self._readable(subject)
        now = as_of or utcnow()
        graph = self._graph(now)
        rels = self._dedupe([r for r in self.store.relations(self.tenant, subject, at=now) if self._visible(r, subject)], subject)
        out: dict[str, Any] = {
            "subject": subject,
            "entity": self.store.entity(self.tenant, subject),
            "facts": graph.facts(subject),
            "relations": [asdict(r) for r in rels],
            "rules": self._rules_for(subject),
            "evaluation": self._evaluation(subject, graph),
            "as_of": now,
        }
        if depth >= 1:
            neighbors = []
            for r in rels[:NEIGHBOR_CAP]:
                other = r.dst if r.src == subject else r.src
                neighbors.append(
                    {
                        "relation": r.rel,
                        "direction": "both" if r.rel in self.symmetric else ("out" if r.src == subject else "in"),
                        "subject": other,
                        "entity": self.store.entity(self.tenant, other),
                        "facts": graph.facts(other),
                        "evaluation": self._evaluation(other, graph),
                    }
                )
            out["neighbors"] = neighbors
            if len(rels) > NEIGHBOR_CAP:
                out["neighbors_truncated"] = True
        return _ser(out)

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

    def related(self, subject: str, relation: str | None = None, as_of: datetime | None = None) -> list[dict[str, Any]]:
        """Neighbours by relation type, now or at `as_of`. A symmetric relation shows once, direction `both`."""
        self._readable(subject)
        rels = self._dedupe([r for r in self.store.relations(self.tenant, subject, rel=relation, at=as_of or utcnow()) if self._visible(r, subject)], subject)
        out = []
        for r in rels:
            other = r.dst if r.src == subject else r.src
            direction = "both" if r.rel in self.symmetric else ("out" if r.src == subject else "in")
            out.append({"relation": r.rel, "direction": direction, "subject": other, "since": r.valid_from.isoformat()})
        return out

    # for frameworks that want a schema list
    def spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "context", "args": ["subject", "as_of?", "depth?"], "doc": self.context.__doc__},
            {"name": "history", "args": ["subject", "since?", "until?", "predicate?", "scope?"], "doc": self.history.__doc__},
            {"name": "search", "args": ["text"], "doc": "Entities and facts matching text."},
            {"name": "annotate", "args": ["subject", "predicate", "value", "scope?", "evidence?", "by?"], "doc": self.annotate.__doc__},
            {"name": "related", "args": ["subject", "relation?", "as_of?"], "doc": self.related.__doc__},
        ]
