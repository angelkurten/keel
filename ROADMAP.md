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
