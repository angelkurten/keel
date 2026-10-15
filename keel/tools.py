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
from keel.envelope import parse_iso, utcnow
from keel.rules import Calendar, evaluate
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation, Store
from keel.store.memory import changed_after, filter_facts, search_in


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


Instant = datetime | str | None  # every time argument: a datetime or ISO 8601 text, as the tools return it


def _t(v: Instant) -> datetime | None:
    """Accept what the tools hand out (ISO text: `watermark`, `as_of`, `recorded_at`) as readily as a datetime."""
    return parse_iso(v) if isinstance(v, str) else v


def _kind(subject: str) -> str:
    return subject.split(":", 1)[0]


NEIGHBOR_CAP = 50  # `context(depth=1)` stops here and says so; ask `related` for the full list


class _Graph:
    """What rules may look at, read through the store at one instant. Rules are the product's
    semantics, so they see the whole tenant whatever the calling agent's read slice is."""

    def __init__(
        self, store: Store, tenant: str, as_of: datetime, calendar: Calendar, symmetric: set[str],
        known_at: datetime | None = None, task: str | None = None,
    ) -> None:
        self.store, self.tenant, self.as_of, self.calendar, self.symmetric = store, tenant, as_of, calendar, symmetric
        self.known_at, self.task = known_at, task
        self._facts: dict[str, list[Fact]] = {}

    def _read(self, subject: str, **kw: Any) -> list[Fact]:
        if self.known_at is not None:
            kw["known_at"] = self.known_at  # only passed when asked: a store from before 0.9 does not take it
        facts = self.store.facts(self.tenant, subject, **kw)
        if self.task is not None:  # one task's memory, plus everything that belongs to no task
            facts = [f for f in facts if f.task is None or f.task == self.task]
        return facts

    def facts(self, subject: str) -> list[Fact]:
        if subject not in self._facts:
            self._facts[subject] = self._read(subject, at=self.as_of)
        return self._facts[subject]

    def history(self, subject: str, predicate: str) -> list[Fact]:
        return [f for f in self._read(subject, until=self.as_of) if f.predicate == predicate]

    def relations(self, subject: str, rel: str | None = None) -> list[Relation]:
        rels = self.store.relations(self.tenant, subject, rel=rel, at=self.as_of)
        if self.known_at is not None:
            rels = [r for r in rels if r.recorded_at is None or r.recorded_at <= self.known_at]
        return rels

    def neighbors(self, subject: str, rel: str, direction: str) -> list[str]:
        out: list[str] = []
        for r in self.relations(subject, rel):
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

    def _graph(self, now: datetime, known_at: datetime | None = None, task: str | None = None) -> _Graph:
        return _Graph(self.store, self.tenant, now, self.cartridge.calendar, self.symmetric, known_at, task)

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

    def context(
        self,
        subject: str,
        as_of: Instant = None,
        depth: int = 0,
        as_known_at: Instant = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Truth about a subject now (or at `as_of`): entity, open facts and relations, the rules
        that apply to its kind, and the evaluation of every rule that carries a `when`. With
        `depth=1`, also each neighbour's entity, open facts and evaluation (up to NEIGHBOR_CAP).
        `as_known_at` answers what Keel knew then (transaction time; `as_of` defaults to it): what
        an agent saw when it decided. `task` keeps only that task's task-scope memory. `watermark`
        is the tenant's latest transaction time, the cursor for `history(changed_since=)`."""
        self._readable(subject)
        as_of, as_known_at = _t(as_of), _t(as_known_at)
        now = as_of or as_known_at or utcnow()
        graph = self._graph(now, as_known_at, task)
        rels = self._dedupe([r for r in graph.relations(subject) if self._visible(r, subject)], subject)
        watermark = getattr(self.store, "watermark", None)
        out: dict[str, Any] = {
            "subject": subject,
            "entity": self.store.entity(self.tenant, subject),
            "facts": graph.facts(subject),
            "relations": [asdict(r) for r in rels],
            "rules": self._rules_for(subject),
            "evaluation": self._evaluation(subject, graph),
            "as_of": now,
            "watermark": watermark(self.tenant) if watermark else None,
        }
        if as_known_at is not None:
            out["as_known_at"] = as_known_at
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
        subject: str | None = None,
        since: Instant = None,
        until: Instant = None,
        predicate: str | None = None,
        scope: str | None = None,
        as_known_at: Instant = None,
        task: str | None = None,
        changed_since: Instant = None,
    ) -> list[dict[str, Any]]:
        """Facts over time: valid_from/valid_to (world), recorded_at and closed_at (when we learned
        them). Filters: one predicate, one memory scope, one task. `as_known_at`: as Keel knew them
        then. `changed_since` (a watermark from `context`): only facts recorded or closed after it;
        with no `subject` it spans the whole tenant, oldest change first: an agent's "since my last run"."""
        since, until, as_known_at, changed_since = _t(since), _t(until), _t(as_known_at), _t(changed_since)
        if subject is None:
            if changed_since is None:
                raise ValueError("history needs a subject, or changed_since for the whole tenant")
            facts = self.store.changes(self.tenant, changed_since)
            if self.agent:
                facts = [f for f in facts if self.agent.sees_kind(_kind(f.subject))]
            if as_known_at is not None:
                facts = filter_facts(facts, known_at=as_known_at)
        else:
            self._readable(subject)
            kw: dict[str, Any] = {"since": since, "until": until}
            if as_known_at is not None:
                kw["known_at"] = as_known_at
            facts = self.store.facts(self.tenant, subject, **kw)
            if changed_since is not None:
                facts = changed_after(facts, changed_since)
        if predicate:
            facts = [f for f in facts if f.predicate == predicate]
        if scope:
            facts = [f for f in facts if f.scope == Scope(scope)]
        if task:
            facts = [f for f in facts if f.task == task]
        return _ser(facts)

    def search(
        self,
        text: str = "",
        kind: str | None = None,
        rule: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Entities and facts matching text. With `kind` or `rule` it enumerates instead: the entities
        of that kind (a rule's `applies_to` when only `rule` is given) whose key or attributes match
        `text`, where the rule fires now when `rule` is given, ordered by key; page with `limit` and
        `after` (the last key you got). The work list of a sweep, routing or orchestrator-workers."""
        if kind is None and rule is None:
            hits = self.store.search(self.tenant, text)
            if self.agent:
                hits = [h for h in hits if self.agent.sees_kind(h.kind if isinstance(h, Entity) else _kind(h.subject))]
            return _ser(hits[:limit] if limit else hits)
        spec = None
        if rule is not None:
            spec = self.cartridge.rules.get(rule)
            if spec is None or not spec.get("when"):
                raise ValueError(f"rule '{rule}' does not exist or has no `when` to evaluate")
            kind = kind or spec.get("applies_to")
            if kind is None:
                raise ValueError(f"rule '{rule}' applies to every kind: pass `kind` too")
        if self.agent and not self.agent.sees_kind(kind):
            raise PermissionError(f"agent '{self.agent.id}' may not read kind '{kind}'")
        found: list[Entity] = []
        graph = self._graph(utcnow()) if spec else None
        for e in self.store.entities(self.tenant, kind):
            if after is not None and e.key <= after:
                continue
            if text and not search_in([e], [], text):
                continue
            if spec and evaluate(spec, e.key, graph.as_of, graph) is not True:
                continue
            found.append(e)
            if limit and len(found) >= limit:
                break
        return _ser(found)

    def annotate(
        self,
        subject: str,
        predicate: str,
        value: Any,
        scope: str = "task",
        evidence: str | None = None,
        by: str | None = None,
        task: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Write earned memory. The registry says who may write which scope; the scope policy says
        whether evidence is required and how long it lives. Both are enforced here. `task` ties the
        fact to one job (`history(task=)`, `keel expire --task`). `key` makes the write idempotent:
        the same key again returns the fact it wrote; the same key with other arguments is an error."""
        sc = Scope(scope)
        self._readable(subject)
        if self.agent and not self.agent.can_write(sc):
            raise PermissionError(f"agent '{self.agent.id}' may not write scope '{sc.value}'")
        self.cartridge.policy.check_write(sc, evidence)
        if key is not None:
            for f in self.store.facts(self.tenant, subject):
                if f.idempotency_key == key:
                    if (f.predicate, f.value, f.task) != (predicate, value, task):
                        raise ValueError(f"idempotency key '{key}' was already used on {subject} with other arguments")
                    return _ser(f)
        ttl = self.cartridge.policy.ttl.get(sc)
        now = utcnow()
        author = self.agent_id or by or "agent"
        if self.agent and self.agent.kind == "human":
            author = f"human:{self.agent.id}"
        f = self.store.open_fact(
            self.tenant,
            subject,
            predicate,
            value,
            valid_from=now,
            source=f"annotate:{author}",
            scope=sc,
            corrected_by=by or (author if self.agent_id else None),
            evidence=evidence,
            expires_at=(now + ttl) if ttl else None,
            task=task,
            idempotency_key=key,
        )
        return _ser(f)

    def related(self, subject: str, relation: str | None = None, as_of: Instant = None) -> list[dict[str, Any]]:
        """Neighbours by relation type, now or at `as_of`. A symmetric relation shows once, direction `both`."""
        self._readable(subject)
        rels = self._dedupe([r for r in self.store.relations(self.tenant, subject, rel=relation, at=_t(as_of) or utcnow()) if self._visible(r, subject)], subject)
        out = []
        for r in rels:
            other = r.dst if r.src == subject else r.src
            direction = "both" if r.rel in self.symmetric else ("out" if r.src == subject else "in")
            out.append({"relation": r.rel, "direction": direction, "subject": other, "since": r.valid_from.isoformat()})
        return out

    # for frameworks that want a schema list
    def spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "context", "args": ["subject", "as_of?", "depth?", "as_known_at?", "task?"], "doc": self.context.__doc__},
            {"name": "history", "args": ["subject?", "since?", "until?", "predicate?", "scope?", "as_known_at?", "task?", "changed_since?"], "doc": self.history.__doc__},
            {"name": "search", "args": ["text?", "kind?", "rule?", "limit?", "after?"], "doc": self.search.__doc__},
            {"name": "annotate", "args": ["subject", "predicate", "value", "scope?", "evidence?", "by?", "task?", "key?"], "doc": self.annotate.__doc__},
            {"name": "related", "args": ["subject", "relation?", "as_of?"], "doc": self.related.__doc__},
        ]
