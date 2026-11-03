"""Agent registry: who may call the tools, what they may write, what they may see.

Declared in the cartridge, next to the ontology the agents use:

    agents:
      tires-bot:
        description: Reads tire depth, writes task memory for the inspection audit.
        writes: [task, agent]
        reads: { kinds: [unit, tire, inspection], relations: [mounted_on, has_inspection] }
      fleet-copilot:
        writes: [task, agent, tenant]        # no `reads`: sees everything
      dispatcher:
        kind: human                          # a person; annotations are signed human:dispatcher
        writes: [task, tenant]

No `agents:` means no registry: any caller, any scope with evidence, as before. With a
registry, a Tools instance must be bound to a declared agent; a missing or unknown id is
refused. Enforcement lives in Tools (design rule 5): the agent never polices itself.
Authentication is the host's job; Keel takes the agent id as given.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from keel.scopes import Scope

DEFAULT_WRITES = frozenset({Scope.TASK.value, Scope.AGENT.value})


@dataclass(frozen=True)
class AgentSpec:
    id: str
    description: str = ""
    kind: str = "agent"  # agent | human: a person is a principal too, and signs as human:<id>
    writes: frozenset[str] = DEFAULT_WRITES  # scope values
    kinds: frozenset[str] | None = None  # None = every kind
    relations: frozenset[str] | None = None  # None = every relation
    query: bool = False  # may call the optional `query` tool (SQL reads past `kinds` and `relations`)

    def can_write(self, scope: Scope) -> bool:
        return scope != Scope.GLOBAL and scope.value in self.writes  # global is never an agent's, whatever its entry says

    def sees_kind(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds

    def sees_relation(self, rel: str) -> bool:
        return self.relations is None or rel in self.relations


def parse_agents(raw: dict[str, Any] | None) -> dict[str, AgentSpec]:
    out: dict[str, AgentSpec] = {}
    for agent_id, spec in (raw or {}).items():
        spec = spec or {}
        reads = spec.get("reads") or {}
        out[agent_id] = AgentSpec(
            id=agent_id,
            description=spec.get("description", ""),
            kind=str(spec.get("kind", "agent")),
            writes=frozenset(str(s) for s in spec.get("writes", DEFAULT_WRITES)),
            kinds=frozenset(reads["kinds"]) if "kinds" in reads else None,
            relations=frozenset(reads["relations"]) if "relations" in reads else None,
            query=spec.get("query") is True,
        )
    return out


def problems(agents: dict[str, AgentSpec], entities: dict[str, Any], relations: dict[str, Any]) -> list[str]:
    """Shape errors in the registry, reported by `keel validate`."""
    valid = {s.value for s in Scope}
    out = []
    for a in agents.values():
        if ":" in a.id:
            out.append(f"agent id '{a.id}' has a ':': ids sign annotations as annotate:<id> and a person as human:<id>, so it would sign as someone else")
        if a.kind not in ("agent", "human"):
            out.append(f"agent '{a.id}' has kind '{a.kind}' (agent or human)")
        for s in sorted(a.writes - valid):
            out.append(f"agent '{a.id}' writes unknown scope '{s}'")
        if Scope.GLOBAL.value in a.writes:
            out.append(f"agent '{a.id}' may not write scope 'global' (a privileged process does)")
        for k in sorted((a.kinds or set()) - set(entities)):
            out.append(f"agent '{a.id}' reads undeclared kind '{k}'")
        for r in sorted((a.relations or set()) - set(relations)):
            out.append(f"agent '{a.id}' reads undeclared relation '{r}'")
    return out


def resolve(agents: dict[str, AgentSpec], agent_id: str | None, store: Any = None, tenant: str | None = None) -> AgentSpec | None:
    """The spec a Tools instance is bound to. No registry: None, the open behaviour. With a `store` (the cartridge
    says `registry: store`), an id the cartridge does not declare is looked up among the tenant's stored agents."""
    if not agents:
        return None
    if agent_id in agents and ":" not in agent_id:  # 'human:ana' would sign as the person ana (problems() says so too)
        return agents[agent_id]
    spec = _stored(store, tenant, agent_id) if store is not None and agent_id and ":" not in agent_id else None
    if spec is not None and spec.get("extends") in agents:  # a profile the cartridge dropped since: refused, not guessed
        return from_stored(agents, agent_id, spec)
    stored = f" nor stored for tenant '{tenant}'" if store is not None else ""
    raise PermissionError(
        f"agent {agent_id!r} is not declared in this cartridge{stored} (declared: {', '.join(sorted(agents))}); pass a declared agent id"
    )


# ---- stored agents (`registry: store`): per tenant, each `extends` a cartridge entry (its profile) and only narrows it,
# so the cartridge, reviewed with the ontology, stays the boundary of what any agent may do.

STORED_KEYS = {"extends", "description", "writes", "reads", "query"}


def _narrow(profile: frozenset[str] | None, asked: Any) -> frozenset[str] | None:
    if asked is None:
        return profile
    return frozenset(asked) if profile is None else frozenset(asked) & profile


def from_stored(agents: dict[str, AgentSpec], agent_id: str, spec: dict[str, Any]) -> AgentSpec:
    """The spec a stored agent binds as: its profile, narrowed by what it sets. Intersected again here, so a profile
    narrowed in the cartridge after the agent was stored narrows the agent too."""
    p, reads = agents[spec["extends"]], spec.get("reads") or {}
    return AgentSpec(
        id=agent_id,
        description=spec.get("description", p.description),
        kind=p.kind,
        writes=frozenset(spec["writes"]) & p.writes if "writes" in spec else p.writes,
        kinds=_narrow(p.kinds, reads.get("kinds")),
        relations=_narrow(p.relations, reads.get("relations")),
        query=p.query and spec.get("query", True) is not False,
    )


def stored_problems(cartridge: Any, agent_id: str, spec: Any) -> list[str]:
    """Why a stored spec may not be put: checked before anything is stored, so nothing invalid ever is."""
    agents = cartridge.agents
    if not isinstance(spec, dict):
        return ["an agent spec is an object"]
    out = []
    if not agent_id or ":" in agent_id or "/" in agent_id:
        out.append(f"agent id '{agent_id}': give one without ':' or '/' (it signs annotations as annotate:<id>)")
    if agent_id in agents:
        out.append(f"agent id '{agent_id}' is a cartridge entry, which would shadow it; pick another id")
    if "kind" in spec:
        out.append("a stored agent's kind comes from its profile (extends a `kind: human` entry for a person)")
    out += [f"unknown key '{k}' (allowed: {', '.join(sorted(STORED_KEYS))})" for k in sorted(set(spec) - STORED_KEYS - {"kind"})]
    if "extends" not in spec:
        return out + [f"a stored agent extends a cartridge entry (one of: {', '.join(sorted(agents))})"]
    p = agents.get(spec["extends"])
    if p is None:
        return out + [f"profile '{spec['extends']}' is not declared in the cartridge (declared: {', '.join(sorted(agents))})"]
    reads = spec.get("reads") or {}
    lists = {"writes": spec.get("writes", []), "reads.kinds": reads.get("kinds", []), "reads.relations": reads.get("relations", [])}
    if not isinstance(reads, dict) or not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in lists.values()):
        return out + ["writes, reads.kinds and reads.relations are lists of names"]
    out += [f"writes '{s}' widens profile '{p.id}' (it writes {', '.join(sorted(p.writes)) or 'nothing'})" for s in lists["writes"] if s not in p.writes]
    for name, asked, declared, allowed in (("kind", lists["reads.kinds"], cartridge.entities, p.kinds),
                                           ("relation", lists["reads.relations"], cartridge.relations, p.relations)):
        for x in asked:
            if x not in declared:
                out.append(f"reads undeclared {name} '{x}'")
            elif allowed is not None and x not in allowed:
                out.append(f"reads {name} '{x}', which widens profile '{p.id}'")
    if spec.get("query") is True and not p.query:
        out.append(f"query: true widens profile '{p.id}', which may not query")
    if "description" in spec and not isinstance(spec["description"], str):
        out.append("description is a string")
    return out


_cache: dict[tuple[int, str, str], tuple[float, dict[str, Any] | None]] = {}


def _stored(store: Any, tenant: str | None, agent_id: str) -> dict[str, Any] | None:
    """The tenant's live stored spec, cached a few seconds per process like API keys: an edit applies without a restart."""
    from keel.keys import CACHE_SECONDS

    if getattr(store, "agent", None) is None or tenant is None:
        return None
    k, now = (id(store), tenant, agent_id), time.monotonic()
    hit = _cache.get(k)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    if len(_cache) > 10_000:  # ponytail: cleared wholesale, an LRU if many tenants churn through one process
        _cache.clear()
    spec = store.agent(tenant, agent_id)
    _cache[k] = (now, spec)
    return spec


def put_agent(cartridge: Any, store: Any, tenant: str, agent_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Create or replace a tenant's stored agent. Raises ValueError with every problem; nothing invalid is stored."""
    from keel.envelope import utcnow

    if cartridge.agent_registry != "store":
        raise ValueError("this cartridge does not store agents: it declares no `registry: store`")
    if getattr(store, "put_agent", None) is None:
        raise ValueError(f"{type(store).__name__} does not keep agents (put_agent, agent, agents, remove_agent)")
    problems_ = stored_problems(cartridge, agent_id, spec)
    if problems_:
        raise ValueError("; ".join(problems_))
    store.put_agent(tenant, agent_id, spec, utcnow())
    _cache.clear()  # this process applies it now; others within CACHE_SECONDS
    return spec


def remove_agent(store: Any, tenant: str, agent_id: str) -> bool:
    """Close a tenant's stored agent (never delete it: the audit still says who annotate:<id> was)."""
    from keel.envelope import utcnow

    ok = store.remove_agent(tenant, agent_id, utcnow())
    _cache.clear()
    return ok
