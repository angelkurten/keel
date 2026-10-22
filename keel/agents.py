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
        )
    return out


def problems(agents: dict[str, AgentSpec], entities: dict[str, Any], relations: dict[str, Any]) -> list[str]:
    """Shape errors in the registry, reported by `keel validate`."""
    valid = {s.value for s in Scope}
    out = []
    for a in agents.values():
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


def resolve(agents: dict[str, AgentSpec], agent_id: str | None) -> AgentSpec | None:
    """The spec a Tools instance is bound to. No registry: None, the open behaviour."""
    if not agents:
        return None
    if agent_id in agents:
        return agents[agent_id]
    raise PermissionError(
        f"agent {agent_id!r} is not declared in this cartridge (declared: {', '.join(sorted(agents))}); pass a declared agent id"
    )
