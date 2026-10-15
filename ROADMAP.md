# Roadmap

Keel is built in the open at 10–20 hours a week. The order below is deliberate: the public story ships alongside the code, and the proof of the thesis (a second cartridge, a second agent, no second pipeline) comes before polish.

Continue/stop criterion at 90 days: open source traction. Before that, at 30 days: one issue, PR or question from someone we do not know.

## Now — 0.0.x (October–November 2026)

Goal: a repo a stranger can run in ten minutes and a post that names the problem.

- [x] Event envelope, handler registry, in-memory bitemporal store
- [x] Memory scopes with TTL and evidence policy
- [x] Five tools as plain functions
- [x] Two cartridges on one core: `fleet` (reference) and `crm` (proof of generality)
- [x] CLI: `init`, `validate`, `ingest`, `replay`, `context`, `serve`
- [x] Docs: *Your first cartridge in 20 minutes*
- [x] Docs site: MkDocs Material on GitHub Pages, built from the repo's markdown
- [x] MCP server tested end to end with Claude via MCP (`tests/test_adapters.py` drives it with the MCP client; `claude -p --mcp-config` answered from the three tool calls)
- [x] MCP server tested end to end with one non-Anthropic client: Codex CLI with `openai/gpt-5.2-codex`, seven tool calls, right answers (see [docs/connect.md](docs/connect.md))
- [x] Postgres adapter: table + polling, with a cursor so restarts do not re-ingest
- [x] Webhook adapter (FastAPI, optional extra)
- [x] Full Apache 2.0 LICENSE text, CONTRIBUTING.md, issue templates
- [x] Launch post: [*We built four agents and four copies of our customer context*](https://angelkurten.com/blog/keel-four-agents-four-copies-of-context)
- [ ] Share it: HN, r/LLMDevs, LinkedIn
- [x] Recorded demo: terminal recording with narration lines (`examples/demo.tape` renders `docs/demo.gif`); a narrated video with subtitles is still open

Exit: the post is published, the demo runs, and the fleet slice runs against a real event stream from the first design customer.

## Next — 0.1 (code shipped early as 0.1.0 on 2026-10-14; pilots through January 2027)

Goal: durable, replayable, and used by two SaaS products that are not ours.

- [x] Durable store: SQLite (`keel/store/sqlite.py`, stdlib, same filters as the reference store, `--db` in the CLI). Graphiti stays a candidate for graph-native queries once a pilot needs them
- [x] MySQL / MariaDB store (`keel/store/mysql.py`, requested 2026-10-14; `--db mysql://...`), a dialect of the shared `SqlStore`, same conformance suite in CI
- [x] Replay from an external log (`Ingestor.replay(tenant, events=adapter)`; `keel replay ... --db` wipes one tenant and rebuilds it from the stream). Event ids are content hashes when the source has none, so a replay yields the same provenance
- [x] Expire task/agent memory: `keel expire <tenant> --db` (reads already hide expired facts)
- [ ] Consolidation: summarize old episodic facts into durable ones (design open; see CLAUDE.md)
- [x] `history` filters by predicate and scope; `context` returns the rule evaluation, not only the rule (rules carry a `when`; `context` takes `as_of`)
- [x] Cartridge validation against event streams (`keel validate --events` reports unknown types, wrong subject kinds and bad payloads)
- [x] Third and fourth cartridges: `finance` (modeled on Toche) and `clinics` (the tutorial's result), zero core changes
- [ ] A cartridge written with a pilot that is not ours (logistics?)
- [ ] Two pilots outside the design customer, running the core for free

Exit: a pilot connects its second agent in under a day, measured.

## 0.2 — in parallel with the pilots (shipped as 0.2.0 on 2026-10-14)

Pulled forward from Later on 2026-10-14 and shipped the same day, while the post circulates and the pilots are found.

- [x] Exportable audit trail per tenant: `keel export <tenant> --db` (entities, relations and facts with provenance, corrections, who annotated what)
- [x] Kafka and SQS adapters as optional extras (`keel.adapters.bus`), tested against fake consumers until a pilot runs a real broker
- [x] Agent registry: identity, permitted scopes, kinds and relations per agent, enforced in `Tools` (`agents:` in the cartridge, `--agent` on `serve`; [design note](docs/design/agent-registry.md), SPEC §6.1)

## 0.3 — SDKs generated from the cartridge (0.3.0 with TypeScript and 0.4.0 with Python, Go and PHP, both 2026-10-14)

Decided 2026-10-14 ([design note](docs/design/sdk-generator.md)): the cartridge is the interface definition; Keel generates the client code.

- [x] Payload notation as a JSON Schema subset (enums, nested objects, arrays) and `keel schema <cartridge>`
- [x] HTTP transport: `/v1/*` JSON API with OpenAPI, tenant and agent per request, one process for many tenants; MCP over HTTP for one tenant (`--tenant`)
- [x] `keel sdk typescript`, runtime `@keel-context/sdk` (sdk/typescript), validated end to end from Bun against the HTTP server with the fleet and finance cartridges; Toche's own cartridge is the next real test
- [x] Python, Go and PHP templates and runtimes, each tested end to end against the HTTP server
- [x] `@keel-context/sdk` 0.1.0 on npm (2026-10-14)
- [x] Go module `github.com/angelkurten/keel/sdk/go` tagged `sdk/go/v0.1.0`; PHP runtime split to github.com/angelkurten/keel-sdk-php for Composer
- [ ] Packagist listing of `keel-context/sdk` from that repo (Angel's Packagist account)

## 0.6 — what the kagent proof of concept asked for (2026-10-14)

A second design customer: kagent, with 354 real Jira cards and 2831 events, wrote up what Keel answered well and what blocked production. Each item below was a row in that list.

- [x] MySQL / MariaDB store with a table prefix for a shared database (0.5.0, prefix in 0.6.0)
- [x] Rules that follow a relation (`via` with `any`/`all`), compose (`all`/`any`/`not`) and compare strings case-insensitively (`ci: true`)
- [x] Duration operators on the open fact: `open_for_days`, `open_for_business_days` with a cartridge `calendar`
- [x] Symmetric relations declared in the cartridge, shown once with direction `both`
- [x] `context(depth=1)` with each neighbour's facts and evaluation; `related(as_of=)`
- [x] Spec guidance: long text is a pointer, not a fact value
- [x] Second round (revalidated on the same 354 cards, 0 differences on the pause rules): `held_for_days` / `held_for_business_days`, the waiting clock that survives a change between waiting states; transactions per event or per batch in the SQL stores, for MySQL ingest speed
- [x] Optional enums in the payload notation: `[new, done, null]` (third round of the kagent report, 0.8.1)
- [x] Semantic `search` (0.11.0): `search(mode="semantic")` over pluggable `VectorIndex` and `Embedder` adapters (Chroma and any OpenAI-compatible embeddings bundled), built by `keel index` outside ingest
- [x] Bulk ingest: set-based batch flush for the SQL stores ([design note](docs/design/bulk-ingest.md)); 101 000 events in 8.4 s on MySQL, a million in under a minute and a half, against 45 minutes before

## 0.9 — what agentic workflows demand (0.9.0 shipped 2026-10-15)

From a survey of the state of the art and a complex multi-agent flow run on Keel (`examples/workflow_fleet_ops.py`); the reasoning is in the [design note](docs/design/agentic-workflows.md). All inside the five tools.

- [x] Transaction time: `context` / `history` with `as_known_at` (what the agent saw), with the close's own `recorded_at`
- [x] Changes since a cursor: tenant-wide `history(since=)` with annotations, and the watermark in every `context`
- [x] Task ids on task memory, `history(task=)`, `keel expire --task`
- [x] Idempotency key on `annotate`
- [x] `search(kind=, rule=, limit=)`: enumerate subjects by kind or by rule state, with a cursor
- [x] People as principals in the registry (`kind: human`)
- [x] Detail levels and budgets (`context(detail="concise")`, `history(limit=)`), conflict policy per predicate (`predicates: {p: {multi: true}}`, `context.conflicts`), proposed vs accepted annotations (`annotate(status=)`, `context.proposals`), trace propagation into the audit log (`traceparent` / `X-Keel-Tool-Call` / MCP `_meta`) — 0.10.0

## 0.12 — semantic search, from the evals (0.12.0 shipped 2026-10-15: MRR 0.82 to 0.95, 1.00 with a reranker)

From research and a labeled eval bench run with real embeddings ([design note](docs/design/semantic-evals.md), `evals/semantic/`).

- [x] Exact-key shortcut in semantic search (id MRR 0.45 to 1.00)
- [x] Hybrid with convex fusion and a lexical leg (overall MRR 0.82 to 0.93)
- [x] `min_score`, model-specific, off by default
- [x] Compact relation lines; query/document roles in `Embedder`; `where` filters in `VectorIndex`; reranker adapter
- [ ] Real agent queries from kagent and Toche in the bench

## Later — 0.4+ (2027)

Goal: the hosted product and the cartridge catalog.

- [ ] Postgres CDC adapter
- [ ] Cartridge catalog: a public index of cartridges with versioning and a verified tier
- [ ] Hosted Keel (Kubbo operates graph, bus and jobs; billed by events ingested per month)
- [ ] SSO, retention policies and tenant-level data export for enterprise

## Not planned

- A UI for browsing the graph (use the tools or your graph database's own tooling)
- Document/RAG ingestion (plenty of good tools exist; Keel points at their output if needed)
- A sixth tool
