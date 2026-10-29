# Serve the tools

Agents reach the five tools (and, when enabled, the optional sixth, `query`) through one of these surfaces. All of them enforce the same scopes, memory policy and agent registry, because the enforcement lives in the tools, not in the transport.

| Surface | For | Start it with |
| --- | --- | --- |
| Python, in process | An agent that is already Python; no network. | `Tools(cartridge, store, tenant="acme", agent="tires-bot")` |
| [MCP](#serve-the-tools-over-mcp) | Claude Code, Claude Desktop, Codex, any MCP client. | `keel serve <cartridge> --tenant acme` (stdio), or `/mcp` on the HTTP server |
| [HTTP JSON API](#serve-the-tools-over-http) | A backend in any language; many tenants from one process. | `keel serve <cartridge> --transport http --db ...` |
| [Typed SDKs](sdks.md) | TypeScript, Python, Go and PHP backends, over the HTTP API. | `keel sdk <language> <cartridge> -o <file>` |
| Console | People: browse the graph, try the tools, watch events. | `keel serve --transport http --ui`, at `/ui` |
| [Semantic search](#semantic-search) | Finding subjects by meaning; optional, on any of the above. | `keel index`, then `keel serve --index ... --embedder ...` |

Running the server in production (configuration, the container, jobs) is in [Operate](operate.md). Where this fits in the whole: [Architecture](architecture.md).

## Serve the tools over MCP

```bash
uv sync --extra mcp
uv run keel serve examples/fleet --tenant acme-logistics --events examples/fleet/events.jsonl
```

With Claude Code, one command registers the server. The agent sees exactly five tools and nothing else:

```bash
claude mcp add keel -- uv run --directory $PWD keel serve examples/fleet --tenant acme-logistics --events examples/fleet/events.jsonl
```

With Codex CLI (OpenAI), declare the server in `~/.codex/config.toml`. Keel annotates `context`, `history`, `search` and `related` as read-only, so Codex runs them without asking even under `approval_policy = "never"`; `annotate` writes, so it prompts unless you set `default_tools_approval_mode = "auto"` on the server:

```toml
[mcp_servers.keel]
command = "uv"
args = ["run", "--directory", "/path/to/keel", "keel", "serve", "examples/fleet", "--tenant", "acme-logistics", "--events", "examples/fleet/events.jsonl"]
startup_timeout_sec = 60
```

A headless run, for a smoke test or CI:

```bash
codex exec --json "Which tires on unit:MXK-4821 are critical according to the cartridge's own rules? Use only the keel tools." < /dev/null
```

Checked on 2026-10-14 with Codex CLI 0.162 and `openai/gpt-5.2-codex` behind an OpenAI-compatible gateway: seven calls (`search`, `related`, `context`, `history` with `predicate=depth_mm`), the right tire, the right depth on 2026-09-20, and the threshold quoted from the cartridge rather than assumed. Any other MCP client works the same way: a stdio server, five tools, one tenant per server instance. If the cartridge declares `agents:`, add `--agent <id>`: the server then reads and writes only what that agent may, and signs its annotations. Agent-scope memory is read only by the agent that wrote it, so a server started without `--agent` reads none.

What the model reads is kept small. Each result is one text block of compact JSON, with the same data in `structuredContent` for programmatic clients. `context` defaults to `detail="brief"`, `history` to `"concise"` and `annotate` to `"ack"` (ask for `"full"` when you need the transaction-time bookkeeping), `search` returns fact hits without that bookkeeping, and long lists are capped with a leading `{"more": ...}` that says how to read the rest. A server bound to one tenant (stdio, or `/mcp` with `--tenant`) states in its `instructions`, once per session, the cartridge's kinds (naming the subjects of a kind that has at most three, as the server starts), relations and rule definitions, so answers carry rule verdicts without repeating their definitions. `detail="text"` on `context` and `history` (experimental) renders lines instead of JSON for models that read prose better. A demo or a test pinned to a day runs with `--clock 2026-10-15T12:00:00Z` (or `KEEL_CLOCK`): reads without `as_of` and what agents write land on that day, so a note written during the demo is read back on the same day; ingest and retention keep real time. A deployment that serves agents may cap one listing with `--max-list 200` (or `KEEL_MAX_LIST`): a request for everything returns 200 (a `search` in `detail="full"`, an audit about 8x a brief entry, a tenth of it) and says how to page, and `search(detail="count")` answers "how many" without listing; unset, nothing is capped. What the model reads is written compact by default, in fewer tokens with nothing left out: lists of records as tables whose shared fields are said once, and UTC times as `2026-10-03T08:00Z` (SPEC §7.7); `structuredContent` and `/v1` keep the JSON. `--plain` (or `KEEL_COMPACT=0`) sends the JSON text instead, for a client that parses it. An agent that never writes can be served the four read tools only with `--read-only` (or `KEEL_READ_ONLY=1`): `annotate`'s definition is not sent with every request, and a call to it (over `/v1` too) is refused with the reason. An agent declared with `writes: []` in the cartridge's registry is served that way without the flag; the default lists all five. `--query` adds the optional sixth, read-only SQL ([below](#query-read-only-sql)).

From LangGraph (LangChain's `create_agent`), through `langchain-mcp-adapters`: `examples/fleet/agent_langgraph.py` starts the same stdio server, hands the five tools to the agent and asks about unit MXK-4821. Its dependencies are declared inline, so `uv run` installs them and nothing is added to Keel:

```bash
OPENAI_API_KEY=... uv run examples/fleet/agent_langgraph.py
# any OpenAI-compatible model: OPENAI_BASE_URL=https://your-gateway/v1 KEEL_DEMO_MODEL=some/model
```

Checked on 2026-10-18 with `openai/gpt-4.1-mini` behind an OpenAI-compatible gateway: `context` with `depth=1` on the unit, `history` of the tire's depth, then `annotate` with the depth fact's evidence; critical since 2026-10-03, 5.2 mm on 2026-09-20, the 3 mm threshold quoted from the rule. Without the system prompt's hint (keys are `<kind>:<id>`, find them with `search`) that small model guessed a key and found nothing.

!!! note "Where is the graph stored?"
    In memory with `--events`: the server ingests the stream at start and serves it, which is enough for a demo. For a deployment, pass `--db keel.db` instead: a SQLite file (stdlib, WAL mode) that an ingest process fills while `keel serve` reads it. Or point `--db` at a MySQL or MariaDB you already run: `--db mysql://user:password@host:3306/keel` (`mariadb://` is accepted too; `uv sync --extra mysql`); the tables are created on first use (with `utf8mb4_bin`, so keys compare exactly) and the same conformance suite runs against it in CI. Tables created before 0.24.2 took the server's collation, under which `page:RENDIMIENTO` and `page:Rendimiento` (or `Numero` and `Número`) were one entity; the first start of 0.24.2 or later converts them, which rewrites each table once, and warns with the subjects that had already collided (`MysqlStore.collided()` lists them): publish those entities again. Sharing the database with your application: `?prefix=keel_` names the tables `keel_entities`, `keel_facts`, and so on. In code, the same option is a constructor argument: `SqliteStore("keel.db", prefix="keel_")`, `MysqlStore.from_url(url, prefix="keel_")` or `MysqlStore(connect, prefix="keel_")`.

    ```bash
    uv run keel ingest examples/fleet examples/fleet/events.jsonl --db keel.db   # or adapter.follow() in your own process
    uv run keel serve examples/fleet --tenant acme-logistics --db keel.db
    uv run keel replay examples/fleet examples/fleet/events.jsonl acme-logistics --db keel.db   # wipe one tenant, rebuild it from the log
    ```

    The log is the truth and the file is derived: `replay` rebuilds one tenant from the external log (your outbox table, your bus, a `.jsonl`) and never from the file itself. What agents and people wrote through `annotate`, and consolidations of it, is not in the log, so `replay` keeps it and the events land around it. The store is pluggable: `SqliteStore` is the default, and any store that passes `tests/test_store.py` (see [contributing](contributing.md)) can take its place.

## Serve the tools over HTTP

For a backend, or for many tenants from one process, serve the same five operations as a JSON API (`uv sync --extra http`):

```bash
KEEL_SECRET=change-me uv run keel serve examples/fleet --transport http --db keel.db --host 0.0.0.0 --port 8000
```

The secret is read from `KEEL_SECRET`; `--secret-env OTHER_VAR` reads it from another variable. If the variable is unset and the store holds no API key (below), the server starts open only on a loopback host (`127.0.0.1`, the default, `::1` or `localhost`), with a warning. On any other `--host` it refuses to start unless you pass `--allow-open`, for when your own proxy authenticates every request.

**The console.** `--ui` also serves the console at `/ui`, the same one hosted Keel runs, in its self-hosted mode: the graph (search, a subject's context `as of` any instant with its rules, a predicate's history, neighbours as a force graph), the five tools by hand with the same call as curl and Python, an agent's prompt exported to Claude Agent SDK, LangGraph, OpenAI Agents SDK or Google ADK, per-client MCP snippets, the webhook with a live log of recent events, an `events.jsonl` upload checked against the catalog, and the SDK generated from your cartridge. It holds no data: it asks for the secret (or a key) and the tenant once, keeps them for the browser tab, and reads through the same `/v1` API, so every scope and registry rule applies. Off by default.

**TLS and HTTP/2.** `--certfile` and `--keyfile` (PEM; or `KEEL_CERTFILE`, `KEEL_KEYFILE`) serve https. `--http2` runs the server on Hypercorn instead of uvicorn (`uv sync --extra http2`) and speaks HTTP/1.1 and HTTP/2 on the same port: h2 negotiated by ALPN over TLS, or cleartext h2c (prior knowledge or `Upgrade`) without it, which is what a proxy in front that talks h2c to its origin uses. HTTP/1.1 clients keep working. On the client side, `keel.client.Client(..., http_client=httpx.Client(http2=True))` ([SDKs](sdks.md)).

```bash
KEEL_SECRET=change-me uv run keel serve examples/fleet --transport http --http2 --db keel.db \
  --host 0.0.0.0 --certfile cert.pem --keyfile key.pem
```

| Call | Body (`?` optional, default in parentheses) | Returns |
| --- | --- | --- |
| `POST /v1/events` | one envelope or a list | the ingest report: `processed`, `duplicates`, `unhandled`, `errors`, `forgotten`, `skipped` |
| `GET /v1/events/recent` | | the tenant's last 200 events received by this server, newest first, each with `status` (`applied`, `duplicate`, `unhandled`, `skipped`, `error`) and the `reason`; kept in memory, so a restart empties it |
| `POST /v1/events/check` | a list of envelopes | what the catalog would refuse in each (`index`, `problems`), the same checks as `keel validate --events`; nothing is ingested |
| `GET /v1/sdk/{language}` | | the typed client for this cartridge, as `keel sdk` generates it: `filename`, `code`, `sha256` |
| `POST /v1/context` | `{"subject", "as_of"?, "depth"? (0), "as_known_at"?, "task"?, "detail"? ("brief"; "concise" or "full"), "exclude"?, "only"?}` | what `context` returns; `{subject, found: false, as_of, watermark}` for a subject nothing is known about |
| `POST /v1/history` | `{"subject"?, "since"?, "until"?, "predicate"?, "scope"?, "as_known_at"?, "task"?, "changed_since"?, "limit"? (50; 0 for all), "detail"? ("concise" or "full")}` | facts, the most recent `limit`; when cut, led by `{"more": ...}` |
| `POST /v1/search` | `{"text"? (""), "kind"?, "rule"?, "limit"?, "after"?, "mode"? ("text" or "semantic"), "min_score"?, "related_to"?, "via"?, "detail"? ("entity"), "depth"?, "per_kind"?, "exclude"?, "as_of"?, "where"?}` | entities and facts; without `limit`, the first 20 matches or 50 of a listing, led by `{"more": ...}` when cut, or by `{"note": ...}` when ranked by the query's words |
| `POST /v1/annotate` | `{"subject", "predicate", "value", "scope"? ("task"), "evidence"?, "by"?, "task"?, "key"?, "status"?, "detail"? ("ack" or "full")}` | a receipt (`subject`, `predicate`, `scope`, `valid_from`, and `valid_to`, `expires_at`, `task`, `status` when set), or the fact with `"full"` |
| `POST /v1/related` | `{"subject", "relation"?, "as_of"?, "depth"? (1), "limit"? (50; 0 for all)}` | neighbours; when cut, led by `{"more": ...}` |
| `POST /v1/query` | `{"sql", "limit"? (200; 0: the ceiling)}` | rows keyed by column; only with `--query` (else 404); see [query](#query-read-only-sql) |
| `GET /health` | | `{"ok", "cartridge", "mcp"}` |
| `GET /openapi.json` | | the contract, for generated clients |

The defaults are for an agent calling directly: `context` answers `brief`, `history` `concise` (lean facts) and `annotate` a short receipt; pass `"detail": "full"` for the whole shape, as the SDKs and `keel.client` do. Lists are capped, and a cut list's first element says what was left out and how to read the rest ([SPEC §7](SPEC.md#7-the-tools-five-core-one-optional)). Times are ISO 8601 strings. Every request carries `Authorization: Bearer $KEEL_SECRET`, `X-Keel-Tenant` (optional when the server is bound with `--tenant` or `KEEL_TENANT`, which is then the default) and, when the cartridge declares agents, `X-Keel-Agent`. Your product authenticates its users and sets the two headers; Keel takes them as given and builds the tools for that tenant and agent per request. `/health`, `/openapi.json`, `/docs` and `/redoc` need no token. Two optional headers are stored with every write the request makes: `traceparent` (W3C trace context) and `X-Keel-Tool-Call` (the agent's tool call id).

On `/v1/events`, `X-Keel-Tenant` is optional: each envelope names its tenant, and when the header is present every envelope must match it. The agent registry does not apply to ingest.

| Status | Meaning |
| --- | --- |
| `400` | bad input: missing `X-Keel-Tenant`, an envelope of another tenant, a subject without `<kind>:`, an unknown kind, relation, rule or scope (the reason names the valid ones), a time that is not ISO 8601, an invalid `mode` or `detail` |
| `401` | bad or missing bearer token |
| `403` | a refusal (scope policy, agent registry), with its reason |
| `422` | a malformed body, such as an envelope that fails validation |
| `503` | the embeddings provider is rate limiting or down, still after Keel's retries (semantic search); retry after `Retry-After` seconds |

The same server also exposes `/mcp` (MCP over streamable HTTP), for agents that speak MCP and cannot run a local process. Each call takes its tenant from `X-Keel-Tenant` (and its agent from `X-Keel-Agent`), as `/v1` does, so one server answers MCP for every tenant; a key narrowed to a tenant needs no header. With `--tenant`, `/mcp` is bound to that tenant instead.

```bash
curl -s localhost:8000/v1/context -H "Authorization: Bearer change-me" -H "X-Keel-Tenant: acme-logistics" \
  -H "Content-Type: application/json" -d '{"subject": "tire:T-9001"}'
```

Reads follow the memory scopes. Agent-scope memory is read only by the agent that wrote it (send `X-Keel-Agent` to read your own). Task-scope memory tied to a task id is read only by calls that pass that `task` (`history`'s `task` is an exact filter); task-scope memory with no task id belongs to the tenant. Global-scope memory lives in a reserved partition, `__global__`, that every tenant reads and no event may write; neither a tool call nor an envelope may use that tenant name. A tenant of an account (`acme/t`, see below) reads `acme/__global__` instead.

### Several products on one deployment: accounts and API keys

One server can serve several products, each confined to its own data. An account is one product (a short slug: lowercase letters, digits and hyphens, at most 32 characters); its tenants are its end-customers. An API key belongs to one account, and the server puts the account in front of the tenant: `X-Keel-Tenant: cliente-42` with a key of `acme` reads and writes the tenant `acme/cliente-42`, and envelopes posted to `/v1/events` are prefixed the same way. A tenant sent with a `/` is refused, so a key cannot reach another account.

```bash
uv run keel keys create acme --db keel.db                         # any tenant of acme
uv run keel keys create acme --db keel.db --tenant cliente-42     # only that tenant (the header may be left out)
uv run keel keys create acme --db keel.db --agent tires-bot       # calls are made as that agent
uv run keel keys create acme --db keel.db --global                # the only key that may annotate(scope="global")
uv run keel keys list acme --db keel.db                           # JSON lines; never prints a key again
uv run keel keys revoke kk_3f9a1c2b7d4e --db keel.db              # refused by running servers within seconds
```

`create` prints the key once; only its SHA-256 is stored, in the `api_keys` table of the same database. Send it as `Authorization: Bearer kk_...`. The first key closes a server that had no secret. `KEEL_SECRET` keeps working as the operator's credential and reaches every tenant by its full name (`acme/cliente-42`). Each account has its own global memory, `acme/__global__`: its tenants read it, other accounts never do, and only its `--global` key writes it. `/mcp` follows the same rules per call; bound to one tenant with `--tenant acme/cliente-42`, it accepts only keys of that account that reach that tenant (and that agent, with `--agent`). The design: `docs/design/hosted.md`.

An account can also have limits, its own database and a retention policy:

```bash
uv run keel accounts set acme --db keel.db --rps 20 --events-month 1000000   # 0 removes a limit
uv run keel accounts set big --db keel.db --store mysql://user:pass@host:3306/big   # "" goes back to the shared database
uv run keel accounts set acme --db keel.db --retention 3y                    # 3y, 18m or 90d
uv run keel accounts list --db keel.db
uv run keel forget --policy --db keel.db --yes                               # schedule it: each account's retention, on its tenants
```

Over its requests per second (a token bucket in each server process) or its events for the month (from the metering counters), a key gets 429 with `Retry-After`. An account with no row is not limited. With `--store`, the server keeps that account's graph in its own database, opened on first use, while keys, accounts and usage stay in the shared one, so `keel usage` still sees it; jobs for that account (`keel index`, `keel expire`) run with `--db` pointing at its database. `--policy` applies each account's retention like `keel forget --before`, in its own database when it has one.

When accounts bring their own cartridges, run one server per account: a cartridge carries Python, and it should never run next to another customer's data. `keel serve <its cartridge> --transport http --db <shared> --account acme` accepts only `acme`'s keys (others get 403) and keeps its graph in the account's database (`keel accounts set acme --store ...`), while keys, limits and usage stay in the shared one. Hosted Keel runs this way, one container per account (`docs/design/hosted.md`).

What each tenant used, per month, one JSON line per account, tenant and month:

```bash
uv run keel usage --db keel.db --month 2027-01 --account acme
{"account": "acme", "tenant": "acme/cliente-42", "month": "2027-01", "events": 18342, "calls": {"context": 911, "search": 2380}}
```

`events` counts events applied for the first time, whatever path brought them (`/v1/events`, `keel ingest`, `keel consume`); duplicates, events for a forgotten tenant and `keel replay` are not counted, so a retry or a rebuild is never counted twice. `calls` counts tool calls through `keel serve --transport http` (`/v1` and `/mcp`), kept in memory and written every ten seconds and at shutdown. Both live as daily counters in the `metering` table; a tenant with no account prefix is reported with `"account": null`.

## Query: read-only SQL

The optional sixth tool. `keel serve --query` (or `KEEL_QUERY=1`; `build_server(query=True)`, `build_http_app(query=True)` in code) adds `query(sql, limit=None)` over MCP and `POST /v1/query` over HTTP. The agent writes one `SELECT` (or `WITH ... SELECT`) over the schema `keel/sqlgen.py` generates from the cartridge: `<kind>_state_at(tenant, t)`, `<kind>_state_known_at(tenant, t, k := k)` and `<kind>_changes(tenant, since, until)` for each kind that declares `facts:`, `state_at`, `keel_facts`, `relations_at`, `<rule>_at` and `<rule>_verdicts`, and the tables `facts`, `entities`, `relations`. Over MCP the tool's description is fixed text, so prompt caches hold, and the schema, generated for the tenant and the store's dialect (`sqlgen.describe`, about 450 to 700 tokens for the example cartridges), rides in the server's instructions; `Tools.spec()` appends it to the description. Rows come back as objects keyed by column, instants as ISO 8601 in UTC, at most 200 by default (`limit`; `0` or more than `--max-list` gives that ceiling, else 1000), a cut list led by `{"more": ...}`. A query runs at most `--query-timeout` seconds (5 by default, `KEEL_QUERY_TIMEOUT`). A `--read-only` server may still offer it: it is a read.

```bash
keel serve examples/fleet --db keel.db --tenant acme-logistics --query
```

**Isolation is the store's, not a check of the agent's SQL.** Whatever the agent writes (another tenant's id in a macro, a base table, `api_keys`, a catalog, `read_csv('/etc/passwd')`, `ATTACH`, `COPY`, `PRAGMA`, `SET`, two statements, a writing CTE), it reads one tenant's rows and changes nothing; `tests/test_query_security.py` runs about a hundred such attacks on every store, through `Tools` and straight to the store.

| Store | How a query is bound to the tenant |
| --- | --- |
| `SqliteStore` | An in-memory SQLite database per tenant holding only what the tenant may read, with the cartridge's macros inlined (under a 200,000-character budget, so nested macro calls cannot grow it exponentially); an authorizer allows reading those three tables and calling functions (no extension or file functions), nothing else (no `PRAGMA`, `ATTACH`, write or schema change); no string or blob over 1 MB (Python 3.11+); a progress handler stops it at the timeout. |
| `DuckdbStore` | A separate DuckDB file per tenant, in a folder of its own, holding only what the tenant may read and the cartridge's macros, opened read-only with external access (files, network), extension install and load, Python replacement scans, spilling to disk (no temp directory) and further configuration locked, 256 MB of memory; functions whose output DuckDB's memory limit does not bound (`repeat`, `lpad`, `rpad`, `printf`, `format`, `list_resize`, `array_resize`, `range`, `generate_series`, `repeat_row`) are refused; every row is measured in the database; interrupted at the timeout. |
| `PostgresStore` (from a URL) | A login role per tenant (created on first use, in one transaction; the store's user needs `CREATEROLE` and to own its schema, nothing more), so sessions of different tenants can neither read each other's statements in `pg_stat_activity` nor cancel or terminate each other. Through a group role it may only `SELECT` three security-barrier views in the store's schema over `facts`, `entities` and `relations`, each showing the rows of the tenant bound to `current_user` (which a session logged in as that role cannot change: no `SET ROLE` or `set_config` moves it); the query connection makes temporary views `facts`, `entities`, `relations` over them, so the generated functions read through them. Its password is an HMAC of a random secret stored in a table only the store's user reads, never derived from the DSN. Every query is one server-side cursor (a `SELECT` only), each row measured on the server, in a transaction rolled back after it, under one deadline for the whole query (a timer cancels it; `statement_timeout`, and `transaction_timeout` on PG 17+); session advisory locks are released after it. Role names carry the whole SHA-256 of deployment and tenant, and a role is bound once, never rebound. `forget` drops the tenant's role and binding. A superuser store also caps each role's temp files (`temp_file_limit`, 1 GB). |
| `MemoryStore`, `MysqlStore` | Not served: the tool is not listed and `Tools.query` says so. |

**What it reads.** The tenant's accepted (no `status`) tenant-scope facts, unexpired, the global partition's global facts (as every tenant's tools read them), its entities and relations. Never agent or task memory, never proposals: a store cannot apply each caller's per-agent and per-task visibility to arbitrary SQL, so they are left out. With an agent registry, an agent may call `query` only if its entry says `query: true`, since SQL reads past `reads.kinds` and `reads.relations`; otherwise the tool is not listed to it and a call is refused.

Sandboxes (SQLite, DuckDB) are copied again only when the tenant's rows changed (a change counter skips even the check while nothing was written), never under the store's main lock; the copy counts against the query's timeout; a tenant over 100,000 rows is refused there, from counts alone (serve it from Postgres); `forget`, `forget_before`, `expire` and `reset` drop the tenant's sandbox, so no copy of forgotten data stays in memory or on disk; on SQLite a forget by another process drops this one's copy at its next query. An answer carries at most 8 MB of values (it stops early, led by `{"more": ...}`); a row over 1 MB is refused.

**Accepted risks.** On Postgres 15 and earlier, `CREATEROLE` is close to superuser (it may alter any non-superuser role and grant itself their rights); give the store's user `CREATEROLE` there only on a database server dedicated to the deployment, or use 16+, where a role's creator administers only what it created. Without superuser, Keel cannot cap a query role's temp files. Two agents of the same tenant querying at once on Postgres share the tenant's role: each can see the other's running statement and cancel it (one tenant's data either way). Every query role can read Postgres's catalog statistics (`pg_stat_user_tables` row counts, the number of `keel_q_*` roles), not their contents. An agent may take the advisory locks Keel's install waits on, for at most its query's timeout. On Postgres a huge value (`repeat('x', 500000000)`) is built in the server's memory before the timeout or the row check stops it; the row check runs on the server over the agent's statement, which must parse alone as one SELECT (so it cannot close the check early), and a row over 1 MB is refused there, not sent to Keel. A DuckDB sandbox's own path is visible to its query (`duckdb_databases()`), with no file access to use it; error texts name `<sandbox>` instead. Without an API key bound to an agent, the `X-Keel-Agent` header is the host's claim, as for every tool (Keel takes it as given): with `query` on, a host that lets an end user set that header to an agent with `query: true` lets them read past that agent's `reads`; bind keys to agents (`keel keys create --agent`) where that matters.

## Semantic search

`search(text, mode="semantic")` finds subjects by meaning ("worn-out tyre" finds a tire at 2.6 mm), filtered by tenant, the agent's read slice, `kind` and `rule`. Two adapters, both replaceable: where vectors live and where embeddings come from.

```bash
uv sync --extra chroma        # only for the bundled Chroma adapter; Python 3.11+ (chromadb needs onnxruntime)
export KEEL_EMBEDDINGS_BASE_URL=https://api.openai.com/v1 KEEL_EMBEDDINGS_MODEL=text-embedding-3-small KEEL_EMBEDDINGS_API_KEY=...
uv run keel index examples/fleet --tenant acme-logistics --db keel.db --index chroma:///var/keel/vectors   # cron it: incremental
uv run keel serve examples/fleet --transport http --db keel.db --index chroma:///var/keel/vectors
```

`keel index` keeps the time of its last run in a cursor file and indexes only what changed since; `--full` rebuilds the tenant's index. The cursor sits next to a `chroma:///path` index (`keel-index-<tenant>.cursor`), else in `.keel/index-<tenant>.cursor` under the working directory; `--cursor` or `KEEL_INDEX_CURSOR` puts it elsewhere. In a container, keep it on the same volume as the index: a cursor lost on deploy makes the next run a full rebuild, and `keel index` says so when it starts without one. Embedding batches go out four at a time (`--workers`, `KEEL_INDEX_WORKERS`), and a rate limit or a 5xx from the embeddings API is retried with backoff. `--all-tenants` indexes every tenant in the store, each with its own cursor (`--cursor-dir` or `KEEL_INDEX_CURSOR_DIR` puts them all in one folder), and clears the documents of every tenant forgotten since its last run. One run per tenant at a time: a run that finds another holding the lock next to the cursor skips (two runs at once corrupt a Chroma index), so a cron that overlaps a slow run is safe. `--embedder` on `index` and `serve` picks where embeddings come from: `openai` (the default, configured by the `KEEL_EMBEDDINGS_*` variables), `hash` (deterministic, for tests) or your own `pkg.module:factory?k=v`. Use the same one for both.

Any OpenAI-compatible endpoint works for embeddings (OpenAI, OfoxAI, Azure, Ollama, vLLM).

`chroma:///path` opens the index inside the process. When `keel serve` and `keel index` run as separate processes (a server and a cron), the server does not see what the other process writes and searches fail with `Error finding id`. Either run the indexing inside the server, `keel serve ... --index chroma:///path --index-every 600` (every tenant every 10 minutes, cursors in `KEEL_INDEX_CURSOR_DIR` or next to the index; `/health` shows `index` with its last run or error), or run Chroma as a server and point both at it with `chroma://host:port`.

The Chroma adapter ranks the lexical leg itself: it pulls a kind's documents once, keeps their BM25 in memory and reuses it until the collection's count changes or 300 seconds pass (`ChromaIndex(bm25_ttl=...)`), so a re-index by another process shows in the lexical leg within that time (the dense leg sees it at once). Each document carries its subject as metadata, so `related_to` filters inside Chroma; a collection built before 0.21 lacks it and filters after retrieval, slower, until one `keel index --full`.

**Weaviate.** `uv sync --extra weaviate` and `--index weaviate://host:8080` (Weaviate 1.25 or later). Query parameters: `grpc_port` (default 50051), `grpc_host` (default the same host), `secure=true` for TLS on both, `collection` (default `Keel`); the API key comes from `KEEL_WEAVIATE_API_KEY`, never the URL. Keel uses one multi-tenant collection and makes each Keel tenant a Weaviate tenant, so the server isolates them. Vectors come from Keel's embedder (the collection has no vectorizer), and the lexical leg is Weaviate's own BM25, so nothing is pulled into Keel's memory and a re-index by another process shows at once. Its tokenizer splits `cus-1974` into two words where Keel's keeps one, so lexical scores differ slightly from Chroma's. Run the conformance tests against a server with `KEEL_WEAVIATE_URL=weaviate://localhost:8080 uv run pytest tests/test_semantic.py`.

### Your own vector store or embedding model

Another vector database (pgvector, Qdrant, Pinecone, OpenSearch...) or your own model is an adapter you write: a plain class, nothing to inherit or register. `examples/custom_adapters.py` is a complete, tested one, a vector index on SQLite and an embedder wrapping any encode function. Run it with `uv run python examples/custom_adapters.py`.

```python
class MyIndex:                                                     # keel.semantic.VectorIndex
    def upsert(self, tenant, items): ...                           # items: [(subject key, vector, document)]
    def delete(self, tenant, keys): ...
    def query(self, tenant, vector, k, where=None): ...            # [(key, cosine)], best first, this tenant only
    def clear(self, tenant): ...

class MyEmbedder:                                                  # keel.semantic.Embedder
    def embed(self, texts, role="document"): ...                   # one vector per text, same dimension
```

The rules an index must keep: every call is scoped by tenant and never returns another tenant's keys; `query` returns `(key, score)` with the score a cosine in [-1, 1], best first, at most `k`. Two capabilities are optional and detected from the signature: `where={"kind": [...]}` on `query` filters inside the index (without it Keel filters after retrieval, which an approximate index can miss), and a `text_query(tenant, text, k, where=None)` method adds the lexical leg for hybrid search. An embedder that takes `role` gets `"query"` for questions and `"document"` for what it indexes.

Name them by a spec, `module:factory?param=value` (parameters arrive as text), on `index` and `serve` alike, and check them with the same conformance suite as the bundled adapters:

```bash
uv run keel index examples/fleet --tenant acme-logistics --db keel.db \
  --index "examples.custom_adapters:make_index?path=vectors.db" --embedder examples.custom_adapters:make_embedder
KEEL_VECTOR_INDEX="examples.custom_adapters:make_index?path=/tmp/v.db" KEEL_EMBEDDER=examples.custom_adapters:make_embedder \
  uv run pytest tests/test_semantic.py
```

**Vectors you already compute.** Keel embeds the document it renders for each subject (key, attributes, open facts, the relations the cartridge names) and the query text, with the same embedder, so both sides live in one vector space. It does not take precomputed vectors. To use your own model, wrap it as the embedder: a function from texts to vectors is enough (`ModelEmbedder` in the example). Vectors you computed elsewhere, from other text, would not match the documents Keel searches.

Search is hybrid by default: ids in the query come first, then a dense and a lexical leg are fused (the lexical weight is `semantic.lexical_weight`, 0.3). Add `--reranker llm` (any OpenAI-compatible chat model, `KEEL_RERANK_BASE_URL` / `_MODEL` / `_API_KEY`) or `--reranker http` (the Cohere, Jina or Voyage `/rerank` API) to reorder the best candidates. For asymmetric embedders set `KEEL_EMBEDDINGS_QUERY_PREFIX` / `_DOCUMENT_PREFIX` (e5: `"query: "`, `"passage: "`). How much each piece buys, measured: `docs/design/semantic-evals.md`.

Indexing never runs in ingest, so ingest stays deterministic and offline. Only tenant and global knowledge is indexed: task and agent memory and pending proposals are not. The cartridge decides the rest:

```yaml
semantic:
  kinds: [card, cliente]
  exclude: [comentario]      # comments full of URLs and technical noise stay out
  relations: [de_cliente]    # "de_cliente: cliente:BAT (nombre: ...)" goes into the card's document
```
