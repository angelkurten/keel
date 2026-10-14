# Design note: agent registry

Status: implemented on 2026-10-14 as proposed, with both open questions decided the same day: read filtering is in v1, and an unknown or missing agent id is refused whenever the cartridge declares agents. The normative text is now [SPEC §6.1](../SPEC.md#61-agent-registry); this note stays as the reasoning.

## Problem

A `Tools` instance is bound to a cartridge and a tenant, and to nothing else. `annotate` takes a free-text `by`; any caller may write to any scope as long as it brings evidence; `context`, `related`, `search` and `history` return everything the tenant has. That is fine for one team's agents on one box. It is not fine when several agents from different owners share a tenant's graph, when one agent must never write tenant-scope memory, when an agent should only see part of the ontology (a billing agent has no business reading inspections), or when the audit trail must say which agent wrote what and be believed.

## Goals

1. **Identity.** Every tool call is made by a known agent id. `annotate` records it: `by` defaults to the agent id, `source` is `annotate:<agent>`.
2. **Write policy per agent.** Which scopes it may write. Default `task` and `agent`; `tenant` only when declared; `global` never from an agent.
3. **Read policy per agent.** Which entity kinds and relations it may see. Everything else is filtered out of the four read tools.
4. **Enforced in `Tools`**, never trusted to the agent (design rule 5). No sixth tool (rule 7).

## Non-goals

- Authentication. The host (your SaaS, the process that launches `keel serve`) decides who is calling; Keel takes the agent id as given. `keel serve --agent tires-bot` binds one server to one agent, the way `--tenant` binds it to one tenant.
- Per-user permissions. Keel's unit is the agent, not the human behind it.

## Proposal

Declare agents in the cartridge, next to the ontology they use:

```yaml
agents:
  tires-bot:
    description: Reads tire depth, writes task memory for the inspection audit.
    writes: [task, agent]
    reads: { kinds: [unit, tire, inspection], relations: [mounted_on, has_inspection] }
  fleet-copilot:
    description: Answers the customer's questions across the whole graph. Writes tenant memory with evidence.
    writes: [task, agent, tenant]
```

Absent `reads` means everything in the cartridge. Absent `agents:` means no registry: today's behaviour, any caller, any scope with evidence. Backwards compatible.

Core changes, about eighty lines:

- `Cartridge.agents: dict[str, AgentSpec]`; `validate()` checks that kinds, relations and scopes exist.
- `Tools(cartridge, store, tenant, agent=None)`. With an agent: `annotate` raises `PermissionError` outside `writes` and defaults `by` to the agent id; the read tools filter by `reads`.
- `keel serve --agent <id>`; the MCP server name becomes `keel:<cartridge>:<tenant>:<agent>`.
- `keel export` already carries `corrected_by` and `source`, so the audit trail gains trustworthy authorship with no change.

`MemoryPolicy` stays as it is (evidence, TTL): the registry says *who may*, the policy says *how*. Order of checks: registry first, then policy.

## Alternatives considered

- **A separate `agents.yaml`** outside the cartridge keeps a vendor's agents apart from the domain, but then two files must agree on kinds and relations. The cartridge already versions the ontology, so the registry belongs with it. A customer disabling an agent for its own tenant can come later as a tenant-scope fact, not a file.
- **Enforcing in the MCP layer only** leaves the plain-function path open. Rule 5 says the layer enforces, so it is `Tools`.

## Open questions

1. **Read filtering in v1, or writes only?** Writes and identity are about thirty lines and cover the audit need. Read filtering touches every tool and `search`. Recommendation: writes and identity first; reads when a pilot has two agents that must see different slices.
2. **Unknown agent id**: refuse with `PermissionError`, or allow with the default policy? Recommendation: refuse when a registry exists, allow when it does not.
