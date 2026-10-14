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

## Next — 0.1 (December 2026–January 2027)

Goal: durable, replayable, and used by two SaaS products that are not ours.

- [x] Durable store: SQLite (`keel/store/sqlite.py`, stdlib, same filters as the reference store, `--db` in the CLI). Graphiti stays a candidate for graph-native queries once a pilot needs them
- [x] Replay from an external log (`Ingestor.replay(tenant, events=adapter)`; `keel replay ... --db` wipes one tenant and rebuilds it from the stream). Event ids are content hashes when the source has none, so a replay yields the same provenance
- [ ] Consolidation job: expire task/agent memory, summarize old episodic facts into durable ones
- [x] `history` filters by predicate and scope; `context` returns the rule evaluation, not only the rule (rules carry a `when`; `context` takes `as_of`)
- [x] Cartridge validation against event streams (`keel validate --events` reports unknown types, wrong subject kinds and bad payloads)
- [ ] Third and fourth cartridges contributed or written with pilots (clinics, logistics)
- [ ] Two pilots outside the design customer, running the core for free

Exit: a pilot connects its second agent in under a day, measured.

## Later — 0.2+ (2027)

Goal: the hosted product and the cartridge catalog.

- [ ] Kafka and SQS adapters; Postgres CDC
- [ ] Agent registry: identity, permitted scopes and relations per agent
- [ ] Exportable audit trail per tenant (facts with provenance, corrections, who annotated what)
- [ ] Cartridge catalog: a public index of cartridges with versioning and a verified tier
- [ ] Hosted Keel (Kubbo operates graph, bus and jobs; billed by events ingested per month)
- [ ] SSO, retention policies and tenant-level data export for enterprise

## Not planned

- A UI for browsing the graph (use the tools or your graph database's own tooling)
- Document/RAG ingestion (plenty of good tools exist; Keel points at their output if needed)
- A sixth tool
