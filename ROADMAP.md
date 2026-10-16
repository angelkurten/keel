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
- [x] MCP server tested end to end with one non-Anthropic client: Codex CLI with `openai/gpt-5.2-codex`, seven tool calls, right answers (see [docs/connect.md](https://github.com/angelkurten/keel/blob/master/docs/connect.md))
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
- [x] `keel serve --transport http --http2`: HTTP/2 on Hypercorn (`--extra http2`), h2 over TLS or cleartext h2c; `--certfile`/`--keyfile` serve https on either server (0.17.0)
- [x] `keel.client.Client` reuses one keep-alive connection, sends `User-Agent: keel-context/<version>` (Cloudflare blocked urllib's with 1010) and takes `headers=` and `http_client=` (any httpx-shaped client: HTTP/2, proxies, retries) (0.17.0)
- [x] Processes that open one database at once no longer race on the schema migration (`facts_key` and added columns; MySQL 1061/1060) (0.16.4)
- [x] `annotate(key=)` is one write even across processes: a unique `facts_key` index on (tenant, subject, idempotency_key); a racing call gets the winner's fact back (`DuplicateKey` in the store protocol) (0.16.3)
- [x] The table, Kafka and SQS adapters acknowledge only after the Ingestor commits: with `batch=N`, a batch that rolled back was skipped by the cursor and lost (0.16.2; from the kagent PoC)
- [x] Ingest no longer slows down as a tenant grows (from the 14M-event stress test, SQLite, same snapshot: 1.0k to 3.1k events/s; `context` 79 ms to 1.5 ms): relation lookups and `changes` are two indexed branches instead of an `OR` that scanned the tenant; the watermark takes one `MAX` per index (new index `relations_recorded`); a bulk batch prefetches only the facts it can still change (new index `facts_open`); SQLite keeps 256 MB of pages in process; `keel ingest` and `replay` commit every 5000 events (0.18.0)
- [x] From the kagent pilot's 41 CUs (more than half the cost was checking and correcting): `context(subjects, only=[...])` returns just those predicates, the evidence a reviewer needs from many subjects; a list of subjects is read in batches (facts, proposals, relations, entities: a constant number of statements, it was two or more per subject); `search` with a list of texts reads each subject's facts once for every text, so many names checked against one subject's neighbours (`search(names, kind=[...], related_to=s)`) is one call
- [x] From the kagent pilot's second round (semantic search had become the slow call: 17 to 20 s for `per_kind` with `related_to`): `kind` takes a list (listing, semantic ranking and `per_kind` over just those kinds); `per_kind` embeds the query once, not once per kind; the lexical leg's BM25 is built once per tenant and kinds and reads only the query's tokens (cached until the index changes; in `ChromaIndex` also for at most `bm25_ttl` seconds, since another process may re-index); `related_to` filters inside the index (`where={"subject": [...]}`; Chroma collections built before this need `keel index --full`), so its fused `score` is now normalized among the neighbours. On a synthetic tenant of 14,000 subjects in embedded Chroma, `per_kind=5, related_to=` went from 1.76 s to 0.09 s (0.21.0)
- [x] From the analysis of the kagent pilot's calls (three of four were searches, most for a visual by its title or page): `search(where={name: value})` finds entities by an attribute's or a current fact's exact value, in every mode; with `kind`, `text` also matches current facts' values; every ranked result says why it is there (`matched`, `snippet`); `semantic.split` makes a document per element of a list value. In the Python client, HTTP, MCP and the TypeScript, Go and PHP runtimes (0.20.0)
- [x] The 0.19 parameters in the TypeScript, Go and PHP runtimes: `detail`, `depth`, `perKind`, `exclude`, `asOf` on search, batches (`contexts`, `searches`; lists in PHP), `exclude` on context, `related` with a list of relations and `depth` (each runtime's end-to-end test exercises them against `keel serve`)
- [x] What the kagent pilot of technical instructions asked for, inside the five tools: `search(detail="concise")` puts each result's facts in it (one call, not one plus a `context` per result), `depth=1` its neighbours, and `per_kind=n` the best of every kind (with `related_to` and semantic mode, the context for a task in one call); `context` and `search` take a list; `related(depth=2|3)` with a list of relations; `exclude` and `search(as_of=)` hide data for evaluations; `semantic.split` indexes chosen facts as documents of their own. Every parameter reaches the Python client, HTTP and MCP (a test checks it) (0.19.0)
- [x] An official image, `ghcr.io/angelkurten/keel` (amd64, arm64; `-slim` without Chroma), published on every release: non-root, `/data` volume, its own health check, refuses to listen without `KEEL_SECRET`; `deploy/docker-compose.yml` with MySQL, a Chroma server and the indexer. Every command with `--db` now reads `KEEL_DB` (`index`, `expire`, `consolidate` and `export` required the flag, so a container configured by its environment failed) (0.18.1)
- [x] `keel index` for a production deploy (from kagent: 11,192 entities took 21 minutes): embedding batches go out four at a time with retries on 429/5xx; the cursor defaults to the index's own folder for `chroma:///path` (`KEEL_INDEX_CURSOR` otherwise) and a run without one says it is rebuilding; one run per tenant at a time, a second skips (two at once corrupted Chroma); documented that `chroma:///path` does not work across `keel serve` and `keel index` processes (0.18.0)
- [x] No N+1 reads: `context(depth=1)`, `search(rule=)` and rules that follow a relation read their subjects in batches (`facts_of`, `entities_of`, optional on a store). On the 14M-event MySQL graph `context(depth=1)` issued about 100 statements, 130 ms over Docker; now a fixed handful (0.18.0; from the stress test)
- [x] A store shared by the HTTP server's threads keeps a transaction and a batch in flight to the thread that opened them: an `annotate` arriving during an ingest batch wrote into that batch and was lost if it rolled back, and other threads read its uncommitted state. MySQL reads on a connection per thread: 32 clients went from 41 to 254 requests/s on the 14M-event graph. Reads of the state now fetch only open facts (`facts_open`), and rule sweeps that follow a relation read relations in batches (`relations_of`): `search(rule=unit_grounded)` 1.0 s to 0.22 s (0.18.0)
- [x] `Ingestor` keeps no in-memory copy of the log unless asked (`keep_log=True`): the HTTP server, the webhook and `follow()` grew by every event they ever ingested, and a 14M-event load needed about 32 GB (0.18.0; from the stress test)
- [x] `keel sdk python` writes rules as a Python literal: a rule with `true` or `null` no longer breaks the import (0.16.1)
- [x] `KEEL_TENANT`: the default tenant for every command that takes one, and the bound tenant of `serve`; a product with no end-customers sets one value and forgets it (0.16.1)
- [x] `replay` keeps annotations and consolidations; the events land around them (0.16.0)
- [x] Configuration in a `.env` file (`--env-file`, `KEEL_ENV_FILE`), and `KEEL_DB`, `KEEL_AGENT`, `KEEL_INDEX`, `KEEL_EMBEDDER` for the matching flags (0.16.0)
- [x] `keel serve --transport http` refuses an open API on a non-loopback host unless `--allow-open` (0.16.0)
- [x] Generated Python and PHP `client()` pass `timeout` and `traceparent`; Kafka and SQS `connect` take every adapter option; the unused `graphiti` extra is gone (0.16.0)
- [x] MCP `annotate` takes any JSON `value`, and `detail="concise"` reports symmetric relations as `both` (0.16.0)
- [x] A handler's `close_fact` closes tenant facts only; `scope=None` closes every scope (0.15.3)
- [x] Scopes never close each other, and consolidation keeps agent memory private and uses world time (0.15.2; from the kagent PoC)
- [x] Memory scopes enforced on read (0.15.0): agent memory reaches only its author, task memory only calls that pass its task, global memory every tenant from its own partition (`__global__`)
- [x] Consolidation (0.14.0): episodic memory into durable facts, declared in the cartridge; deterministic reducers and a Summarizer adapter; `keel consolidate`, `keel expire --consolidate`
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
- [x] Agent registry: identity, permitted scopes, kinds and relations per agent, enforced in `Tools` (`agents:` in the cartridge, `--agent` on `serve`; [design note](https://github.com/angelkurten/keel/blob/master/docs/design/agent-registry.md), SPEC §6.1)

## 0.3 — SDKs generated from the cartridge (0.3.0 with TypeScript and 0.4.0 with Python, Go and PHP, both 2026-10-14)

Decided 2026-10-14 ([design note](https://github.com/angelkurten/keel/blob/master/docs/design/sdk-generator.md)): the cartridge is the interface definition; Keel generates the client code.

- [x] Payload notation as a JSON Schema subset (enums, nested objects, arrays) and `keel schema <cartridge>`
- [x] HTTP transport: `/v1/*` JSON API with OpenAPI, tenant and agent per request, one process for many tenants; MCP over HTTP for one tenant (`--tenant`)
- [x] `keel sdk typescript`, runtime `@keel-context/sdk` (sdk/typescript), validated end to end from Bun against the HTTP server with the fleet and finance cartridges; Toche's own cartridge is the next real test
- [x] Python, Go and PHP templates and runtimes, each tested end to end against the HTTP server
- [x] `@keel-context/sdk` 0.1.0 on npm (2026-10-14)
- [x] Go module `github.com/angelkurten/keel/sdk/go` tagged `sdk/go/v0.1.0`; PHP runtime split to github.com/angelkurten/keel-sdk-php for Composer
- [x] Packagist lists `keel-context/sdk` from that repo, following its tags by webhook (2026-10-16, v0.1.0 to v0.8.0); `composer require keel-context/sdk` needs no repository entry

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
- [x] Bulk ingest: set-based batch flush for the SQL stores ([design note](https://github.com/angelkurten/keel/blob/master/docs/design/bulk-ingest.md)); 101 000 events in 8.4 s on MySQL, a million in under a minute and a half, against 45 minutes before

## 0.9 — what agentic workflows demand (0.9.0 shipped 2026-10-15)

From a survey of the state of the art and a complex multi-agent flow run on Keel (`examples/workflow_fleet_ops.py`); the reasoning is in the [design note](https://github.com/angelkurten/keel/blob/master/docs/design/agentic-workflows.md). All inside the five tools.

- [x] Transaction time: `context` / `history` with `as_known_at` (what the agent saw), with the close's own `recorded_at`
- [x] Changes since a cursor: tenant-wide `history(changed_since=)` with annotations, and the watermark in every `context`
- [x] Task ids on task memory, `history(task=)`, `keel expire --task`
- [x] Idempotency key on `annotate`
- [x] `search(kind=, rule=, limit=)`: enumerate subjects by kind or by rule state, with a cursor
- [x] People as principals in the registry (`kind: human`)
- [x] Detail levels and budgets (`context(detail="concise")`, `history(limit=)`), conflict policy per predicate (`predicates: {p: {multi: true}}`, `context.conflicts`), proposed vs accepted annotations (`annotate(status=)`, `context.proposals`), trace propagation into the audit log (`traceparent` / `X-Keel-Tool-Call` / MCP `_meta`) — 0.10.0

## 0.12 — semantic search, from the evals (0.12.0 shipped 2026-10-15: MRR 0.82 to 0.95, 1.00 with a reranker)

From research and a labeled eval bench run with real embeddings ([design note](https://github.com/angelkurten/keel/blob/master/docs/design/semantic-evals.md), `evals/semantic/`).

- [x] Exact-key shortcut in semantic search (id MRR 0.45 to 1.00)
- [x] Hybrid with convex fusion and a lexical leg (overall MRR 0.82 to 0.93)
- [x] `min_score`, model-specific, off by default
- [x] Compact relation lines; query/document roles in `Embedder`; `where` filters in `VectorIndex`; reranker adapter
- [ ] Real agent queries from kagent and Toche in the bench

## Later (2027)

Goal: the hosted product and the cartridge catalog.

- [ ] Postgres CDC adapter
- [ ] Cartridge catalog: a public index of cartridges with versioning and a verified tier
- [ ] Hosted Keel (Kubbo operates graph, bus and jobs; billed by events ingested per month)
- [ ] SSO and retention policies for enterprise (tenant-level data export shipped in 0.2.0: `keel export`)

## Not planned

- A UI for browsing the graph (use the tools or your graph database's own tooling)
- Document/RAG ingestion (plenty of good tools exist; Keel points at their output if needed)
- A sixth tool
