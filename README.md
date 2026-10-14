# Keel

**Stop rebuilding the pipeline for every agent.**

Keel is a context layer for AI agents inside vertical SaaS products. Domain events from your product go in; a bitemporal context graph per end-customer comes out; every agent you build reads and writes it through the same five MCP tools.

You describe your vertical once, in a *cartridge* (ontology, event catalog, semantic rules, memory policies). Keel's core knows nothing about your domain. Your second agent connects in a day instead of weeks, because there is no second pipeline to write.

> Status: pre-alpha. The core shape is here; the Graphiti-backed store, the webhook adapter and the MCP server are stubs you can run against the in-memory store today.

## The problem

Every new agent in a SaaS product repeats the same work: extract the customer's data, normalize it, decide what "critical" means, keep its own memory, expose its own tools to the model. Four agents, four copies of the customer's context, four definitions of "critical", four memories that never talk.

Existing memory layers (Zep, Mem0, AgentCore Memory, Neo4j's MKG) ingest text and documents and model memory per user or per agent. None of them start from **domain events**, and none model the **tenant of the tenant**: the end-customer of a B2B SaaS.

## How Keel is different

1. **Cartridges.** A vertical is declared in YAML plus a few ten-line handlers. The core stays generic. Write a cartridge for fleets, clinics, logistics, or your own product.
2. **Events in, not documents.** Keel ingests structured facts with time (Postgres tables, webhooks, Kafka later). Ingestion is deterministic and cheap. The graph is exact because it is born from events, not inferred from text.
3. **Every fact carries its provenance.** Source, `valid_from`, `valid_to`, `recorded_at`, and who corrected it. Audit-ready by construction.
4. **Tenant of the tenant.** One subgraph per end-customer, with its own permissions and expiry.

## Five tools, any agent

```
context(subject)                  -> what is true now about this subject
history(subject, since, until)    -> what was true, and when we learned it
search(text)                      -> entities and facts matching text
annotate(scope, fact, evidence)   -> an agent (or a human) writes what it learned
related(subject, relation)        -> neighbors by relation type
```

Expose them over MCP (`keel serve`) and point any agent framework at them.

## Quick start

```bash
uv sync --extra dev
uv run keel validate cartridges/fleet
uv run keel ingest cartridges/fleet examples/events/fleet.jsonl
uv run keel context cartridges/fleet examples/events/fleet.jsonl unit:MXK-4821
```

Then write your own cartridge: see [docs/first-cartridge.md](docs/first-cartridge.md) (20 minutes).

## Architecture

```
  your SaaS (Postgres)   webhooks   Kafka / SQS (later)
            \               |            /
             v              v           v
   +-------------------------------------------------+      +------------------+
   |  event envelope: tenant, type, subject, time     |      |  cartridge       |
   |  ingestor + handler registry                     | <--- |  ontology        |
   |  bitemporal context graph, one subgraph/tenant   |      |  event catalog   |
   |  memory scopes: global / tenant / task / agent   |      |  handlers        |
   |  five MCP tools                                   |      |  semantic rules  |
   +-------------------------------------------------+      |  memory policies |
            |              |             |                   +------------------+
         agent A        agent B       agent N (no pipeline)
```

Design rules the core enforces:

- **Events are the truth.** An append-only log; the graph is derived and can be replayed.
- **Pointers, not copies.** The graph holds references to your rows, the semantics, and earned memory. Never a copy of your tables.
- **Explicit time.** Facts close, they are not overwritten.
- **Scopes, not a flat store.** Nothing is promoted to a wider scope by accident.

## Repository layout

```
keel/            core: envelope, handlers, store, scopes, cartridge loader, ingest, tools, cli
cartridges/      fleet (reference), crm (proof that the core is domain-agnostic)
examples/        sample event streams and a minimal agent
docs/            first-cartridge.md
tests/
```

## Roadmap

- [x] Event envelope and handler registry
- [x] In-memory bitemporal store
- [x] Memory scopes with expiry
- [x] Five tools as plain functions
- [ ] MCP server (`keel serve`)
- [ ] Graphiti-backed store
- [ ] Postgres (table + polling) and webhook adapters
- [ ] Consolidation and decay jobs
- [ ] Cartridge catalog

## License

Apache 2.0. The event envelope, the cartridge format and the five tools are the standard; they will always be free.
