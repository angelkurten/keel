# Connect your events

Keel ingests envelopes, never raw rows. Every source produces the same shape, and the cartridge's handlers are the only code that reads a payload. Pick the source that matches where your events already are.

## Configuration

Every setting the CLI reads from the environment can live in a `.env` file: `keel` loads `./.env` when it exists, or the file named by `--env-file` (or `KEEL_ENV_FILE`). Variables already set in the environment win, so a deployment can override the file. `KEY=VALUE` lines, `#` comments, optional `export ` and quotes; no interpolation. Copy [`.env.example`](https://github.com/angelkurten/keel/blob/master/.env.example) and fill in what you use; keep `.env` out of git (it holds secrets).

| Variable | Used by | What |
| --- | --- | --- |
| `KEEL_DB` | every command with `--db` | the durable graph: a SQLite file or `mysql://user:pass@host:3306/db` |
| `KEEL_TENANT` | every command that takes a tenant; `serve` (`--tenant`) | the default tenant; with `serve --transport http` it is used when `X-Keel-Tenant` is absent |
| `KEEL_AGENT` | `serve`, `context`... (`--agent`) | the declared agent acting here |
| `KEEL_CERTFILE`, `KEEL_KEYFILE` | `serve --transport http` | TLS certificate and key (PEM) to serve https (`--certfile`, `--keyfile`) |
| `KEEL_SECRET` | `serve --transport http` | the shared bearer secret, the operator's credential (`--secret-env` names another variable); per-account API keys: `keel keys` |
| `KEEL_INDEX`, `KEEL_EMBEDDER` | `index`, `serve` | the vector index and embedder specs (`--index`, `--embedder`) |
| `KEEL_INDEX_CURSOR`, `KEEL_INDEX_WORKERS` | `index` | where the cursor of the last run is kept (on a volume) and how many embedding batches go out at once (`--cursor`, `--workers`) |
| `KEEL_EMBEDDINGS_BASE_URL`, `_MODEL`, `_API_KEY`, `_QUERY_PREFIX`, `_DOCUMENT_PREFIX` | `--embedder openai` | any OpenAI-compatible `/v1/embeddings` endpoint |
| `KEEL_RERANK_BASE_URL`, `_MODEL`, `_API_KEY` | `serve --reranker http\|llm` | the reranker endpoint |
| `KEEL_SUMMARIZER_BASE_URL`, `_MODEL`, `_API_KEY` | `consolidate --summarizer llm` | the chat model that writes summaries |

A flag on the command line beats both.

**No end-customers?** Keel keys everything on a tenant, but nothing forces you to have many. Pick one fixed value, put it in every envelope's `tenant` and in `KEEL_TENANT=default`, and stop passing `--tenant`: every command, `keel serve` and the HTTP API (no `X-Keel-Tenant` needed) use it. The `tenant` scope then simply means "shared by every agent".

The Python API reads nothing from `.env`: a program that embeds Keel passes its settings itself.

## The envelope

One event, as the development adapter reads it from `examples/fleet/events.jsonl`:

```json
{"tenant": "acme-logistics", "type": "unit.registered", "subject": "unit:MXK-4821",
 "occurred_at": "2026-09-01T09:00:00Z", "source": "fleet.vehicles",
 "payload": {"id": "MXK-4821", "plate": "MXK-4821", "model": "Freightliner Cascadia"}}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `tenant` | yes | The end-customer this fact belongs to. Opaque to Keel. |
| `type` | yes | Dotted lowercase type from the cartridge catalog. |
| `subject` | yes | `<kind>:<id>` of the entity the fact is about. |
| `occurred_at` | yes | When it happened in the world. Timezone-aware. |
| `source` | yes | System of record or adapter name. |
| `id` | no | Generated when absent. Becomes the `evidence` of derived facts. |
| `recorded_at` | no | When Keel learned it. Defaults to now. |
| `payload` | no | Event data. Only handlers read it. |

The full rules are in the [specification](SPEC.md#2-event-envelope).

## A `.jsonl` file

For development and tests. One envelope per line; blank lines and lines starting with `#` are ignored.

```bash
uv run keel ingest examples/fleet examples/fleet/events.jsonl
```

The command prints `processed`, `duplicates`, `unhandled`, `errors` and `skipped` (events for a forgotten tenant, see [Erase a tenant](#erase-a-tenant)) and carries on past a bad event. `--strict` stops at the first unhandled type or handler error instead; on a SQL store the batch in flight (5000 events) is rolled back.

## A Postgres outbox table

Your SaaS inserts envelopes into a table. Keel polls it in order and remembers the last row it took in a cursor file, so a restart resumes instead of re-ingesting.

```sql
CREATE TABLE keel_events (
    seq          bigserial PRIMARY KEY,
    id           text NOT NULL,
    tenant       text NOT NULL,
    type         text NOT NULL,
    subject      text NOT NULL,
    occurred_at  timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}'
);
```

```bash
uv sync --extra postgres
```

```python
import psycopg

from keel import Ingestor, MemoryStore, SqliteStore, load_cartridge
from keel.adapters.postgres import PostgresTableAdapter

ingestor = Ingestor(load_cartridge("examples/fleet"), SqliteStore("keel.db"))  # durable; MemoryStore() for a throwaway graph
adapter = PostgresTableAdapter(lambda: psycopg.connect(DSN), table="keel_events", cursor_path=".keel/cursor")

ingestor.ingest(adapter)                      # one pass over everything after the cursor
ingestor.ingest(adapter.follow(interval=5))   # or poll forever
```

Delivery is at-least-once: `Ingestor.ingest` moves the cursor only after each commit (it calls the adapter's `ack()`), so with `batch=N` a batch that rolls back, or a crash mid-batch, is read again whole; the store skips what it already applied as duplicates. Iterating the adapter by hand, the cursor moves when you ask for the next event; call `adapter.ack()` after storing instead if you batch your own writes. The table name, the cursor column and the batch size are parameters (`table=`, `cursor_column="seq"`, `batch_size=500`). Any DB-API 2.0 connection works; pass `placeholder="?"` for sqlite3.

```mermaid
sequenceDiagram
    participant S as Your SaaS
    participant T as keel_events
    participant A as PostgresTableAdapter
    participant C as cursor file
    participant I as Ingestor
    S->>T: INSERT envelope (seq 41)
    A->>C: read position
    C-->>A: 40
    A->>T: SELECT * WHERE seq > 40 ORDER BY seq LIMIT 500
    T-->>A: row 41
    A->>I: Event 41
    I->>I: dispatch to the handler
    I->>A: next event?
    A->>C: write 41
    Note over A,I: a crash before "write 41" re-delivers event 41 on restart
```

!!! warning "Concurrent writers"
    A serial cursor can skip a row that commits late while a later row was already read. Use one writer per table, or a `recorded_at` cursor with a lag, if that bites.

## Postgres CDC: your own tables, no outbox

If you would rather not write an outbox, Keel can follow row changes in the tables you already have, through logical replication. It uses the built-in `pgoutput` plugin (Postgres 10 or later, also RDS, Cloud SQL, Azure and Supabase), read through SQL functions, so there is no extension to install. The database needs `wal_level = logical`.

Map each table to a kind in a small YAML file:

```yaml
# cdc.yaml
inspections: {kind: inspection, id: id, tenant: tenant_id}
tires:       {kind: tire, id: serial, tenant: fleet_id, columns: [depth_mm, unit_id]}
```

Each inserted, updated or deleted row becomes an event `<kind>.created`, `<kind>.updated` or `<kind>.deleted`, with the subject `<kind>:<id column>`, the transaction's commit time as `occurred_at`, and the row (only `columns`, plus the id, when you list them: pointers, not copies) as payload. Declare those event types in your cartridge; `keel cdc` refuses to start while one is missing, because a read would stop at it.

```bash
uv sync --extra postgres
uv run keel cdc cartridges/mine --dsn postgresql://user:pass@host:5432/app --map cdc.yaml --db keel.db --setup
```

`--setup` creates the publication and the replication slot (`keel` by default) and sets `REPLICA IDENTITY FULL` on tables with a tenant column, since by default a delete carries only the primary key. The slot moves only after Keel has stored what it read, so a crash reads it again and dedupe skips what was already stored (event ids are `pgcdc:<commit lsn>:<n>`). `--once` reads what is there and stops; without it, `keel cdc` follows. A slot nobody reads keeps WAL on the database server's disk: drop it with `SELECT pg_drop_replication_slot('keel')` when you stop for good.

## A webhook

Keel receives envelopes over HTTP, validates them and ingests them in the request.

```bash
uv sync --extra webhook
```

```python
# app.py
from keel import Ingestor, MemoryStore, SqliteStore, load_cartridge
from keel.adapters.webhook import build_app

ingestor = Ingestor(load_cartridge("examples/fleet"), SqliteStore("keel.db"))  # durable; MemoryStore() for a throwaway graph
app = build_app(ingestor, token="shared-secret")
```

```bash
uv run uvicorn app:app --port 8000
curl -X POST localhost:8000/events \
  -H "Authorization: Bearer shared-secret" -H "Content-Type: application/json" \
  -d '[{"tenant": "acme-logistics", "type": "unit.registered", "subject": "unit:MXK-4821", "occurred_at": "2026-09-01T09:00:00Z", "source": "fleet.vehicles", "payload": {"id": "MXK-4821", "plate": "MXK-4821", "model": "Freightliner Cascadia"}}]'
```

The body is one envelope or a list. The response is the ingest report: `processed`, `duplicates` (ids the store had already applied), `unhandled` types, `errors`, `forgotten` (the tenants this batch erased, with what was deleted) and `skipped` (events for a forgotten tenant). `build_app(ingestor, token=..., strict=True)` fails the request at the first unhandled type or handler error instead of reporting it; on a SQL store the request's batch is rolled back, so the sender can retry it whole. A malformed envelope is a `422`; a wrong token is a `401`. Without a token the endpoint accepts any caller, so use that only on localhost.

## Consume a Kafka topic or an SQS queue

Your SaaS publishes envelopes as JSON messages; Keel consumes them. `uv sync --extra kafka` (confluent-kafka) or `--extra sqs` (boto3).

For SQS there is a command; run it as a long-lived service:

```bash
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1 \
  uv run keel consume cartridges/mine --queue https://sqs.us-east-1.amazonaws.com/123456789012/keel-events --db keel.db
uv run keel consume cartridges/mine --queue http://elasticmq:9324/000000000000/keel-events --db keel.db   # SQS-compatible: the queue's host is the endpoint
```

Messages are stored a batch per transaction (`--batch`, default 100: one long poll, topped up with what is already queued) and deleted, ten per call, only once stored. When a batch fails it is retried one message at a time, and a message that still fails (not an envelope, a type with no handler, a handler error) is logged and left on the queue, so SQS redelivers it after the visibility timeout and, after the queue's `maxReceiveCount`, moves it to its dead-letter queue: give the queue one. If the store itself stops answering (MySQL down), the command exits with code 2 instead of dead-lettering every message: run it under a supervisor that restarts it. A connection the server dropped while idle (`wait_timeout`) is reopened. `--once` drains what is queued and stops (exit code 1 if anything failed). An erasure prints one JSON line (`{"forgot": <tenant>, "event": ..., "entities": ...}`) on stdout for your audit log; everything else goes to stderr. `KEEL_QUEUE` and `KEEL_DB` stand in for the flags. Kafka has no per-message redelivery (committing a later offset skips a failed one), so it has no command yet; use `KafkaAdapter` from Python:

```python
from keel.adapters.bus import KafkaAdapter, SqsAdapter

adapter = KafkaAdapter.connect("broker:9092", topic="keel-events", group_id="keel")
adapter = SqsAdapter.connect("https://sqs.us-east-1.amazonaws.com/123456789012/keel-events")
ingestor.ingest(adapter)            # drain what is there now, then stop
ingestor.ingest(adapter.follow())   # consume forever
```

Delivery is at-least-once: the offset is committed, or the SQS message deleted, only after the batch that holds it is committed (`Ingestor.ingest` calls the adapter's `ack()`), so a crash mid-batch redelivers the whole batch. With `batch=N` on SQS, keep the visibility timeout above the time a batch takes. A message that is not a valid envelope raises and stays on the bus; pass `dead_letter=lambda body, error: ...` to log it somewhere and move on. `KafkaAdapter.connect(..., **config)` merges extra keys into the confluent-kafka consumer config (dotted keys go through a dict: `**{"security.protocol": "SSL"}`); `SqsAdapter.connect(queue_url, **kwargs)` passes them to `boto3.client("sqs", ...)`. `connect` also takes the adapter's own options as keywords: `KafkaAdapter.connect(..., poll_timeout=1.0, dead_letter=...)`, `SqsAdapter.connect(queue_url, batch_size=10, wait_seconds=10, dead_letter=...)`; or build the adapter from your own client with the same keywords: `KafkaAdapter(consumer, ...)`, `SqsAdapter(client, queue_url, ...)`. Both adapters only need two calls from their client (`poll`/`commit`, `receive_message`/`delete_message`), which is how the tests run them on fakes and how you would wrap another client.

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

**Browse the graph.** `--ui` also serves a read-only page at `/ui`: search a tenant, open a subject's context (optionally `as of` a past instant, with its rules evaluated), click a predicate for its history, and explore the graph: a force layout of the subject's neighbours up to 3 hops, where node size follows links, labels fade in as you zoom, hovering highlights a node's neighbours and a click opens a node and adds its own neighbours (wheel to zoom, drag to pan or move a node, double click to fit; up to 3,000 nodes). The page holds no data; it asks for the secret, the tenant and, when the cartridge declares agents, the agent, keeps them for the browser tab only, and reads through the same `/v1` API, so every scope and registry rule applies. Off by default.

**TLS and HTTP/2.** `--certfile` and `--keyfile` (PEM; or `KEEL_CERTFILE`, `KEEL_KEYFILE`) serve https. `--http2` runs the server on Hypercorn instead of uvicorn (`uv sync --extra http2`) and speaks HTTP/1.1 and HTTP/2 on the same port: h2 negotiated by ALPN over TLS, or cleartext h2c (prior knowledge or `Upgrade`) without it, which is what a proxy in front that talks h2c to its origin uses. HTTP/1.1 clients keep working. On the client side, `keel.client.Client(..., http_client=httpx.Client(http2=True))` ([SDKs](sdks.md)).

```bash
KEEL_SECRET=change-me uv run keel serve examples/fleet --transport http --http2 --db keel.db \
  --host 0.0.0.0 --certfile cert.pem --keyfile key.pem
```

| Call | Body (`?` optional, default in parentheses) | Returns |
| --- | --- | --- |
| `POST /v1/events` | one envelope or a list | the ingest report: `processed`, `duplicates`, `unhandled`, `errors`, `forgotten`, `skipped` |
| `POST /v1/context` | `{"subject", "as_of"?, "depth"? (0), "as_known_at"?, "task"?, "detail"? ("full" or "concise")}` | what `context` returns |
| `POST /v1/history` | `{"subject"?, "since"?, "until"?, "predicate"?, "scope"?, "as_known_at"?, "task"?, "changed_since"?, "limit"?}` | facts |
| `POST /v1/search` | `{"text"? (""), "kind"?, "rule"?, "limit"?, "after"?, "mode"? ("text" or "semantic"), "min_score"?, "related_to"?, "via"?}` | entities and facts |
| `POST /v1/annotate` | `{"subject", "predicate", "value", "scope"? ("task"), "evidence"?, "by"?, "task"?, "key"?, "status"?}` | the fact |
| `POST /v1/related` | `{"subject", "relation"?, "as_of"?}` | neighbours |
| `GET /health` | | `{"ok", "cartridge", "mcp"}` |
| `GET /openapi.json` | | the contract, for generated clients |

Times are ISO 8601 strings. Every request carries `Authorization: Bearer $KEEL_SECRET`, `X-Keel-Tenant` (optional when the server is bound with `--tenant` or `KEEL_TENANT`, which is then the default) and, when the cartridge declares agents, `X-Keel-Agent`. Your product authenticates its users and sets the two headers; Keel takes them as given and builds the tools for that tenant and agent per request. `/health`, `/openapi.json`, `/docs` and `/redoc` need no token. Two optional headers are stored with every write the request makes: `traceparent` (W3C trace context) and `X-Keel-Tool-Call` (the agent's tool call id).

On `/v1/events`, `X-Keel-Tenant` is optional: each envelope names its tenant, and when the header is present every envelope must match it. The agent registry does not apply to ingest.

| Status | Meaning |
| --- | --- |
| `400` | bad input: missing `X-Keel-Tenant`, an envelope of another tenant, an unknown rule, an invalid `mode` or `detail` |
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

## Run it in a container

`ghcr.io/angelkurten/keel` serves the tools over HTTP (`/v1` and `/mcp`; `KEEL_TENANT` binds `/mcp` to one tenant) on port 8000, for amd64 and arm64. Tags follow the releases: `0.28.0`, `0.28`, `latest`, and `-slim` variants without the Chroma client (a third of the size, no semantic search on Chroma). The cartridge is not in the image: mount it at `/cartridge`.

```bash
docker run -p 8000:8000 -e KEEL_SECRET=$(openssl rand -hex 32) \
  -e KEEL_DB=mysql://keel:secret@db.internal:3306/keel \
  -v ./examples/fleet:/cartridge:ro -v keel-data:/data ghcr.io/angelkurten/keel:0.28.0
```

- It refuses to start without `KEEL_SECRET` (or `--allow-open` behind your own auth proxy), since it listens on every interface.
- `/data` is the working directory and a volume: a SQLite graph (`KEEL_DB=/data/keel.db`), the `keel index` cursor (`/data/.keel/`) and a `.env` live there.
- It runs as an unprivileged user and checks its own `/health` (no `curl` needed in the image). Configuration is the environment (§ Configuration); every command reads `KEEL_DB`, `KEEL_TENANT` and `KEEL_INDEX`.
- Any other command runs the same way: `docker run ... ghcr.io/angelkurten/keel:0.28.0 index /cartridge` (the entrypoint is `keel`).
- To ship your cartridge inside, two lines: `FROM ghcr.io/angelkurten/keel:0.28.0` and `COPY cartridges/mine /cartridge`.

Single sign-on for your team in front of `/ui`, with Caddy and oauth2-proxy and no code in Keel: [`deploy/sso`](https://github.com/angelkurten/keel/tree/master/deploy/sso).

`deploy/docker-compose.yml` runs Keel with its own MySQL, a Chroma server and `keel index` every 15 minutes: `KEEL_SECRET=... docker compose -f deploy/docker-compose.yml up -d`. `docker build -t keel .` builds this checkout; `--build-arg KEEL_VERSION=0.28.0` builds a release from PyPI. `deploy/smoke.sh <image>` checks an image end to end.

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

`chroma:///path` opens the index inside the process. When `keel serve` and `keel index` run as separate processes (a server and a cron), the server does not see what the other process writes and searches fail with `Error finding id`. Run Chroma as a server and point both at it with `chroma://host:port`; keep `chroma:///path` for a single process or for tests.

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

## Consolidate episodic memory

Agents write task and agent memory that expires. A cartridge's `consolidate:` section declares which of it becomes a durable fact: the source predicate and scopes (task memory by default; agent memory is private and is read only when `from.scopes` lists it), an optional `within:` window on world time (`valid_from`, so a reingested history keeps its dates), how many observations make a pattern, a reducer (`mode`, `latest`, `count`, `min`, `max`, `avg`, `distinct` or `summary`) and the predicate and scope to write. It runs as a job, never in ingest:

```bash
uv run keel consolidate examples/fleet --tenant acme-logistics --db keel.db
uv run keel expire acme-logistics --db keel.db --consolidate examples/fleet   # consolidate, then drop expired memory
uv run keel expire --all-tenants --db keel.db --consolidate examples/fleet     # every tenant in the store, one after another
```

Each run prints `written` and `unchanged` per consolidation and is idempotent: the same inputs never write twice. The originals stay until their TTL. `reduce: summary` needs `--summarizer`: `llm` (any OpenAI-compatible chat model, from `KEEL_SUMMARIZER_BASE_URL`, `KEEL_SUMMARIZER_MODEL` and `KEEL_SUMMARIZER_API_KEY`), `join` (the distinct values joined, deterministic) or your own `pkg.module:factory`. The other reducers are deterministic and need none.

## Export the audit trail

One tenant, everything Keel knows about it, as JSONL: entities, relations and facts, closed ones included, each with `source`, `evidence`, `scope`, `recorded_at` and `corrected_by`. Hand it to the customer, an auditor, or a diff.

```bash
uv run keel export acme-logistics --db keel.db --out acme-logistics.jsonl
```

Each line is one record: `{"record": "fact", "subject": "tire:T-9001", "predicate": "depth_mm", "value": 5.2, "valid_from": ..., "valid_to": ..., "source": "tires.measurements", "evidence": "<event id>", ...}`. Another tenant's export never contains this one's rows.

## Erase a tenant

When an end-customer leaves and the law (Colombia's Ley 1581, GDPR's right to erasure) or your privacy notice says their data goes, Keel deletes everything it stores for that tenant: entities, relations, facts of every scope, what agents and people annotated, the ids of the events it applied and, through the index, its semantic documents. Declare the event your SaaS sends when the customer is deleted:

```yaml
events:
  household.deleted:
    subject: household
    forgets: tenant      # no handler: Keel erases the event's tenant
    payload: { id: string }
```

It arrives through the same stream, in order. The erasure is one transaction, and a tombstone (the tenant and the time) stays: any later event for that tenant, an old redelivery included, is skipped (`skipped` in the report) and `annotate` is refused, so nothing revives it. The ingest report lists what was deleted (`forgotten`), and `keel consume` prints it as a JSON line for your audit log. A second erasure deletes nothing and keeps the first time. The semantic documents go on the next `keel index --all-tenants` (or `keel index --tenant <t>`).

From the command line, for one tenant:

```bash
uv run keel forget household-42 --db keel.db --index chroma://chroma:8000 --yes
# {"tenant": "household-42", "forgotten_at": "...", "entities": 31, "relations": 40, "facts": 212, "annotations": 3, "ingested": 120, "index": true}
```

Keel erases only what it stores. The external log is yours: delete or expire the tenant's rows in your outbox table, and remember that consumed queue messages are already gone. Global memory is every tenant's and is kept, so never promote a customer's personal data to it. The tombstone holds the tenant id and the time; pick tenant ids that are not personal data on their own.

### Retention: forget what closed before a date

A contract or a privacy policy may ask for history older than N years to go while the customer stays. Retention deletes facts and relations that closed at or before a cutoff (`valid_to <= cutoff`), annotations included; open facts, current relations and entities stay. Declare the event your SaaS sends, or run it from the command line:

```yaml
events:
  retention.applied:
    subject: household
    forgets: before      # no handler: Keel deletes what closed before the event's occurred_at
    payload: {}
```

```bash
uv run keel forget household-42 --before 2024-01-01 --db keel.db --yes
uv run keel forget --all-tenants --before 2024-01-01 --db keel.db --yes      # a retention policy, one JSON line per tenant
# {"tenant": "household-42", "before": "2024-01-01T00:00:00+00:00", "facts": 180, "annotations": 4, "relations": 12}
```

The ingest report lists each run (`retained`) and `keel consume` prints it for your audit log. The cutoff is kept per tenant (the latest wins), and `replay` applies it again after rebuilding, so what the log brings back goes again. An event that arrives later with a valid time before the cutoff is kept until the next run: schedule the policy (cron, a Kubernetes CronJob) rather than running it once. Semantic documents follow on the next full `keel index`.
