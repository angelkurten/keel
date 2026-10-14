# Keel — project spec for coding agents

Read this before changing anything. It says what Keel is, what it is not, and the rules that keep the core generic. `ROADMAP.md` says what comes next.

## What Keel is

A context layer for AI agents inside vertical SaaS products. Domain events from the SaaS go in; a bitemporal context graph **per end-customer** (the tenant of the tenant) comes out; every agent reads and writes it through the same five tools.

The domain is declared once in a **cartridge** (YAML + a few ten-line handlers). The core knows nothing about any domain.

Positioning, in one line: *stop rebuilding the pipeline for every agent.* "Shared memory for agents" is the consequence, never the headline.

## What Keel is not

- Not an agent framework. Agents live in LangGraph, ADK, the Claude SDK, wherever. Keel is what they consult.
- Not a document/RAG memory. We ingest structured events with time, not text. Ingestion is deterministic. No LLM in the ingest path (see Open decisions).
- Not a copy of the SaaS's database. The graph holds pointers, semantics and earned memory.
- Not a single global memory. Memory has scopes and nothing is promoted by accident.

## Architecture

```
sources (Postgres table/CDC, webhooks, Kafka later)
   -> Event envelope (keel/envelope.py)        tenant, type, subject, occurred_at, recorded_at, source, payload
   -> Ingestor (keel/ingest.py)                 reads an adapter, dispatches by event.type
   -> HandlerRegistry (keel/handlers.py)        cartridge handlers map payload -> store primitives
   -> Store (keel/store/)                       bitemporal: entities, relations, facts; one subgraph per tenant (memory or sqlite)
   -> Tools (keel/tools.py)                     context / history / search / annotate / related
   -> MCP server (keel/mcp_server.py)           optional; exposes the same five tools
cartridges/<name>/cartridge.yaml + handlers.py  ontology, event catalog, rules, memory policy, handlers
```

Store primitives are the only write path: `upsert_entity`, `link`, `unlink`, `open_fact`, `close_fact`. `open_fact` closes the previous open fact on the same predicate; nothing is overwritten.

## Design rules (enforce these in review)

1. **Events are the truth.** The log is append-only; the graph is derived and must be rebuildable with `Ingestor.replay(tenant)`. Any state that cannot be rebuilt from events is a bug.
2. **Pointers, not copies.** An entity carries `ref` to the row in the system of record and the few attributes a tool needs to label it. If a PR copies a whole row or table into the graph, reject it.
3. **Explicit time, bitemporal.** `valid_from`/`valid_to` = world time. `recorded_at` = when Keel learned it. Facts close; they are never mutated or deleted by handlers. "The tire was critical on Oct 3" and "the tire is critical" are different queries and both must work.
4. **Provenance on every fact.** `source`, `evidence` (event id, row ref, photo), and `corrected_by` when a human or agent corrected it.
5. **Scopes, not a flat store.** `global` > `tenant` > `task` > `agent`. Policy (TTL, evidence requirement) is enforced in `Tools.annotate` via `MemoryPolicy`, never trusted to the calling agent. Promotion to a wider scope is an explicit `annotate` with evidence.
6. **The core is domain-agnostic.** No entity kind, relation name, predicate, rule or event type appears anywhere under `keel/`. The acceptance test for any core change: both `cartridges/fleet` and `cartridges/crm` still pass without edits. Adding a third cartridge must require zero changes under `keel/`.
7. **Five tools, and only five.** New capabilities go into the semantics of these five (richer `context`, filters on `history`) before anyone adds a sixth. The tool names and signatures are part of the public standard.
8. **One subgraph per tenant.** Every store call takes `tenant` first. Cross-tenant reads do not exist. A tool instance is bound to one tenant.
9. **Handlers are small.** A handler maps one payload to primitives in ~10 lines. Business logic (what "critical" means) lives in `rules`, read by tools, not in handlers and not in agents.
10. **Deterministic ingest.** No network calls, no LLM calls, no randomness in the ingest path. Replay must produce an identical graph.

## Conventions

- Python 3.10+, `uv`, `pytest`, `ruff` (line length 100). Type hints everywhere; pydantic for the envelope only, dataclasses in the store.
- Subject keys are `<kind>:<id>` strings. Event types are dotted lowercase: `inspection.created`.
- Datetimes are timezone-aware UTC. Naive datetimes are rejected at the envelope.
- Public surface: `keel/__init__.py` exports. Anything else is internal and may change.
- All user-facing text (README, docs, CLI help, errors) is in English. Commit messages in English.
- Tests: every core change adds or updates a test in `tests/`; cartridge changes add events to `examples/events/*.jsonl` and assertions in `tests/test_core.py`.

## How to verify a change

```bash
uv sync --extra dev
uv run pytest
uv run keel validate cartridges/fleet && uv run keel validate cartridges/crm
uv run python examples/agent_minimal.py
```

If `examples/agent_minimal.py` output changes, say why in the PR.

## Open decisions (do not resolve silently)

- **Durable store.** Decided 2026-10-14: pluggable by design, never a core change. `Store` protocol (`keel/store/base.py`), `MemoryStore` as reference, `SqliteStore` as the default durable one, and a conformance suite any store can run (`KEEL_STORE=pkg.module:factory uv run pytest tests/test_store.py`). Graphiti or Postgres would be third implementations; if Graphiti forces an LLM in the ingest path, use only its graph store and keep our deterministic ingest.
- **Consolidation/decay.** Expiry is shipped (`expires_at`, `store.expire`, `keel expire`). Summarizing old episodic facts into durable ones is deliberately left open (2026-10-14) until a pilot brings a real case; the leading candidate is a deterministic job, never an LLM in the core.
- **Multi-cartridge tenants.** One tenant, one cartridge for now. Composing cartridges is a later decision.
- **Pricing unit** (hosted): events ingested per month. Not a code concern yet, but metering must be possible from the log.

## Things to never do

- Never add a dependency to the core for one cartridge's convenience.
- Never let a tool return facts from another tenant.
- Never delete or mutate a fact from a handler. Close it.
- Never read `event.payload` in `keel/`. Only handlers do. One exception: `Cartridge.check_event` (behind `keel validate --events`) compares payload keys and types with the catalog; it never reads values.
- Never write generic placeholders (`TBD`, `n/a`) into cartridge YAML; leave the key out.
