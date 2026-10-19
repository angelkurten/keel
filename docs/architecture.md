# Architecture

Everything Keel is made of, on one page: where events come from, what happens to them, where they are kept, how agents read and write them, and what runs on a schedule. Read this first; every other page goes deeper into one box of this map.

![Keel architecture: sources, adapters, deterministic ingest, bitemporal store, cartridge, five tools, jobs, surfaces and agents](architecture.svg)

## What Keel is, and what it is not

Keel is the context layer for the AI agents inside a product. Domain events from your product go in. A bitemporal context graph per tenant comes out. Every agent reads and writes it through the same five tools. Your domain is declared once, in a **cartridge**.

| Keel is not | Because |
| --- | --- |
| An agent framework | Your agents live in LangGraph, the Claude Agent SDK, ADK or your backend. Keel is what they consult. |
| A document or RAG memory | It ingests structured events with time, not text. Ingestion is deterministic: no LLM reads your events. |
| A copy of your database | The graph holds pointers to your rows (`ref`), their semantics and what agents learned, never whole rows. |
| One flat global memory | Memory has scopes (`global`, `tenant`, `task`, `agent`), and nothing is promoted to a wider one by accident. |

## What happens to one event

1. **Your product emits a domain fact.** For example `tire.depth_measured` with `depth_mm: 2.4`. It can leave through an outbox table, CDC, the SDK, a webhook, a queue or a file.
2. **An adapter turns it into an envelope.** It checks the fields and that times are timezone-aware UTC. It never interprets the payload.
3. **The Ingestor dedupes and dispatches it.** An `id` already applied is skipped. Otherwise the handler registered for its `type` runs.
4. **The cartridge's handler writes primitives.** `upsert_entity`, `link`, `open_fact`. A new fact closes the one that was current at that instant, so arrival order does not change the result.
5. **An agent asks.** `context("tire:T-9001")` returns the open facts, the relations and every rule's verdict (`tire_critical: true`) next to its definition.
6. **The agent writes what it concluded.** `annotate(..., scope="task", evidence="inspection:4821")`. Keel enforces the memory policy; the agent cannot skip it.

## 1–2. Getting events in

Every source produces the same envelope, and you can combine several for one tenant: the event `id` dedupes. Details and options for each are in [Send your events](ingest.md).

| Source | Use it when | Start it with | Guarantee |
| --- | --- | --- | --- |
| [Postgres CDC](ingest.md#postgres-cdc-your-own-tables-no-outbox) | You would rather not touch the product's code. Each row change becomes `<kind>.created\|updated\|deleted`. | `keel cdc <cartridge> --dsn ... --map cdc.yaml --setup`, or `keel serve --cdc cdc.yaml` | The slot moves only after the store commits. Needs `wal_level = logical`. |
| [Typed SDK](ingest.md#from-your-code-the-typed-sdk) | Your backend (TypeScript, Python, Go, PHP) emits events from business code, with payload types generated from the cartridge. | `keel sdk typescript <cartridge> -o src/keel.ts`, then `keel.events([emit.tireDepthMeasured({...})])` | `POST /v1/events`. Ids are a content hash when you send none, so a retry is a duplicate, not a second event. |
| [Outbox table](ingest.md#a-postgres-outbox-table) | You already write domain events to a table in the same transaction as the business change. | `PostgresTableAdapter(connect, cursor_path=".keel/cursor")` | At-least-once, a `seq` cursor; restarts resume. |
| [Webhook](ingest.md#a-webhook) | Something else pushes JSON envelopes over HTTP. | `POST /v1/events` on `keel serve --transport http`, or `build_app(ingestor, token=...)` | One envelope or a list per request; the sender retries. |
| [SQS](ingest.md#consume-a-kafka-topic-or-an-sqs-queue) | You have a queue, or want producer and Keel decoupled. | `keel consume <cartridge> --queue <url> --db ...` | At-least-once; a failing message goes to the queue's dead-letter queue. |
| [Kafka](ingest.md#consume-a-kafka-topic-or-an-sqs-queue) | Your platform already publishes to a topic. | `KafkaAdapter.connect(...)` from Python | At-least-once. No CLI command yet. |
| [JSONL file](ingest.md#a-jsonl-file) | Development, history backfill, tests, rebuilding the graph. | `keel ingest <cartridge> events.jsonl --db ...`, `keel replay` | Deterministic: the same log gives the same graph. |
| Your own adapter | Any other source. | Any iterable that yields `Event`s ([example](https://github.com/angelkurten/keel/blob/master/examples/custom_adapters.py)) | What you implement; `follow()` and `ack()` are optional. |

## 3. The ingest path and the cartridge

The ingest path is **deterministic**: no network calls, no LLM, no randomness. Replaying a tenant's log rebuilds the same graph, which is why the event log, not the graph, is the truth.

The **cartridge** is the only domain code you write: a folder with `cartridge.yaml` and `handlers.py`. Keel's core knows no entity kind, relation, predicate or event type; all of them come from here.

| Section | What it declares |
| --- | --- |
| `entities` | The kinds of things, each with `ref` to the table it points to. |
| `relations` | Typed edges between kinds (`mounted_on: {from: tire, to: unit}`). |
| `events` | The event catalog: each type, its subject kind and its payload shape. `keel validate --events` checks a stream against it. |
| `handlers.py` | One small function per event type: payload in, store primitives out. |
| `rules` | What "critical", "overdue" or "at risk" means, as a `definition` and an optional `when` the tools evaluate. |
| `memory` | TTL per scope and which scopes need evidence. |
| `agents` | The agent registry: who may write which scope and see which kinds and relations. |
| `consolidate` | How episodic memory is summarized into durable facts. |

Business logic lives in `rules`, not in handlers and not in agents. Rules compose (`all`, `any`, `not`), follow relations (`via`) and measure durations; counts and aggregates over time stay a `definition` the agent applies. Build one in [Your first cartridge](first-cartridge.md).

## 4. The bitemporal store

Every fact has two time axes. **World time** (`valid_from`, `valid_to`): when it was true. **Knowledge time** (`recorded_at`, `closed_at`): when Keel learned it began and ended. So Keel answers both "is the tire critical?" and "was it critical on October 3, as far as we knew that day?" (`as_of`, `as_known_at`). Facts are closed, never overwritten or deleted by a handler, and each carries `source`, `evidence` and, when someone corrected it, `corrected_by`.

The graph is partitioned **one subgraph per tenant**. No read crosses tenants; the only shared partition is `__global__`, which every tenant reads and only a privileged `annotate(scope="global")` writes.

| Backend | For | How |
| --- | --- | --- |
| `MemoryStore` | Tests, demos, a process that rebuilds from JSONL at start. | `MemoryStore()`, `keel serve --events events.jsonl` |
| `SqliteStore` | The default durable store. One file, nothing to run. | `--db keel.db` or `KEEL_DB=keel.db` |
| `MysqlStore` | Several processes, production. | `--db mysql://user:pass@host:3306/keel` |
| Your own | Postgres, a graph database, anything else. | Implement the protocol in `keel/store/base.py` and pass the conformance suite: `KEEL_STORE=pkg.module:factory uv run pytest tests/test_store.py` |

## 5. The five tools

The public standard. Every agent, through any surface, uses these five and no other. A `Tools` instance is bound to one tenant (and, with a registry, one agent). Full signatures are in the [specification](SPEC.md#7-the-five-tools).

| Tool | The question it answers |
| --- | --- |
| `context` | What do I know about this subject now, or at `as_of`? Open facts, relations and rule verdicts. |
| `history` | How did it change over time? Without a subject: what changed since a watermark. |
| `search` | Which subjects match? By text, kind, rule, attribute value or meaning (semantic). |
| `related` | What is it connected to, up to three hops? |
| `annotate` | Remember what the agent concluded: earned memory, with evidence and a scope. |

### Memory scopes and the agent registry

| Scope | Who reads it | Who writes it | Typical life |
| --- | --- | --- | --- |
| `global` | Every tenant | A privileged caller only, with evidence | Permanent |
| `tenant` | That tenant's agents | Any agent allowed to, with evidence | Permanent |
| `task` | Calls that pass that `task` id | Agents working on the task | A TTL, say 14 days |
| `agent` | Only its author | The agent itself | A short TTL, say 1 day |

The policy is enforced in the tools, never trusted to the agent. With `agents:` in the cartridge, every call names a declared agent; a missing or unknown id is refused, and each agent sees only the kinds and relations it declares. Authenticating the agent is your host's job: Keel takes the id as given.

## 6. How agents reach the tools

Details in [Serve the tools](serve.md) and [SDKs](sdks.md).

| Surface | For | Start it with |
| --- | --- | --- |
| Python, in process | An agent that is already Python; no network. | `Tools(cartridge, store, tenant="acme", agent="tires-bot")` |
| HTTP JSON API | A backend in any language, many tenants from one process. | `keel serve --transport http --db ...`: `POST /v1/{context,history,search,related,annotate}`, `POST /v1/events`, `GET /health`; headers `Authorization: Bearer`, `X-Keel-Tenant`, `X-Keel-Agent` |
| MCP | Claude Code, Claude Desktop, Codex, any MCP client. | `keel serve --tenant acme` (stdio), or `/mcp` on the HTTP server |
| Typed SDKs | TypeScript, Python, Go and PHP backends: the five tools and `events()` with types from your cartridge. | `keel sdk <language> <cartridge> -o <file>` |
| Console | People: browse the graph, try the tools, watch events, download SDKs. | `keel serve --transport http --ui`, at `/ui` |
| Semantic search | Finding subjects by meaning; optional. | `keel index` fills a vector index (Chroma, Weaviate or yours); `keel serve --index ... --embedder ...` |

## Jobs

They run outside ingest, from the CLI or cron, so they may call the network or an LLM. Details in [Operate](operate.md).

| Command | What it does | How often |
| --- | --- | --- |
| `keel expire --consolidate` | Consolidates episodic memory into durable facts, then drops expired `task` and `agent` memory. | Daily |
| `keel index` | Incremental indexing for semantic search. | Every few minutes |
| `keel replay` | Rebuilds a tenant's graph from its log, keeping annotations. | After changing handlers |
| `keel export` | One tenant's audit trail as JSONL, closed facts and provenance included. | On demand |
| `keel forget` | Erases a tenant for good, or with `--before`, what closed before a date (retention). | On demand, or scheduled |
| `keel usage` | Events ingested and calls per tool, read from the log. | For metering |

## Three ways to run it

| Mode | What it looks like | Pick it when |
| --- | --- | --- |
| Embedded | `pip install keel-context`; the Ingestor and the Tools inside your Python service, on SQLite or MySQL. | Your agents are Python and you want the fewest moving parts. |
| Self-hosted service | The `ghcr.io/angelkurten/keel` container: `keel serve --transport http --db mysql://... --cdc cdc.yaml --ui`, plus `keel consume` if you use SQS, plus cron for the jobs. | Agents in several languages, or several products sharing one deployment ([run it in a container](operate.md#run-it-in-a-container)). |
| Hosted | One Keel container and one database per account, with a console to upload or generate the cartridge, set up CDC, issue keys per tenant and try an agent. | You would rather not run it. During the pilots, by invitation: [pilot request](pilot.md#start-one). |

## Rules that do not bend

- **Events are the truth.** The graph is derived and can be rebuilt with `replay`. Only annotations live outside the log, and a replay keeps them.
- **Pointers, not copies.** An entity carries `ref` to its row and the few attributes a tool needs to label it.
- **Deterministic ingest.** No network, no LLM, no randomness in handlers. LLMs belong in jobs or in the agent.
- **Facts close.** Handlers never delete or mutate a fact.
- **One tenant per `Tools` instance.** Cross-tenant reads do not exist.
- **Five tools.** A new capability goes into the semantics of these five before anyone adds a sixth.
- **Provenance on every fact.** `source`, `evidence`, and `corrected_by` when someone corrected it.

## Your first day

1. **Run the example.** `uv sync --extra dev`, then `uv run python examples/fleet/agent_minimal.py`. Read `examples/fleet/`: a complete cartridge with its events and its test.
2. **Pick the tenant and five to ten events.** The facts your agents need today, not your whole database. Write twenty real ones to an `events.jsonl`.
3. **Write the cartridge.** `keel init cartridges/mine`, entities with `ref`, the event catalog and one handler per type. Check it: `keel validate cartridges/mine --events events.jsonl`.
4. **Move business logic into `rules`.** If an agent computes "overdue", "critical" or "at risk", that rule belongs in the cartridge.
5. **Connect the real source.** CDC if you would rather not touch the product; the SDK, an outbox or a webhook for explicit domain events.
6. **Connect an agent.** MCP to try it with Claude; HTTP or the generated SDK for production. Declare it under `agents:` with the least it needs.
7. **Schedule the jobs.** `keel expire --consolidate` daily and, with semantic search, `keel index`.

The same path with a time budget per step, and our help: [your first pilot in a day](pilot.md).
