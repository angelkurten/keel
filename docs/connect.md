# Connect your events

Keel ingests envelopes, never raw rows. Every source produces the same shape, and the cartridge's handlers are the only code that reads a payload. Pick the source that matches where your events already are.

## The envelope

One event, as the development adapter reads it from `examples/events/fleet.jsonl`:

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
uv run keel ingest cartridges/fleet examples/events/fleet.jsonl
```

The command prints `processed`, `duplicates`, `unhandled` and `errors` and carries on past a bad event. `--strict` stops at the first unhandled type or handler error instead; on a SQL store the batch in flight (500 events) is rolled back.

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

ingestor = Ingestor(load_cartridge("cartridges/fleet"), SqliteStore("keel.db"))  # durable; MemoryStore() for a throwaway graph
adapter = PostgresTableAdapter(lambda: psycopg.connect(DSN), table="keel_events", cursor_path=".keel/cursor")

ingestor.ingest(adapter)                      # one pass over everything after the cursor
ingestor.ingest(adapter.follow(interval=5))   # or poll forever
```

Delivery is at-least-once: the cursor advances after the consumer has taken an event, so a crash mid-event re-delivers it, and the store applies each event in one transaction, so a redelivery is skipped as a duplicate. The table name, the cursor column and the batch size are parameters (`table=`, `cursor_column="seq"`, `batch_size=500`). Any DB-API 2.0 connection works; pass `placeholder="?"` for sqlite3.

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

## A webhook

Keel receives envelopes over HTTP, validates them and ingests them in the request.

```bash
uv sync --extra webhook
```

```python
# app.py
from keel import Ingestor, MemoryStore, SqliteStore, load_cartridge
from keel.adapters.webhook import build_app

ingestor = Ingestor(load_cartridge("cartridges/fleet"), SqliteStore("keel.db"))  # durable; MemoryStore() for a throwaway graph
app = build_app(ingestor, token="shared-secret")
```

```bash
uv run uvicorn app:app --port 8000
curl -X POST localhost:8000/events \
  -H "Authorization: Bearer shared-secret" -H "Content-Type: application/json" \
  -d '[{"tenant": "acme-logistics", "type": "unit.registered", "subject": "unit:MXK-4821", "occurred_at": "2026-09-01T09:00:00Z", "source": "fleet.vehicles", "payload": {"id": "MXK-4821", "plate": "MXK-4821", "model": "Freightliner Cascadia"}}]'
```

The body is one envelope or a list. The response is the ingest report: `processed`, `duplicates` (ids the store had already applied), `unhandled` types and `errors`. `build_app(ingestor, token=..., strict=True)` fails the request at the first unhandled type or handler error instead of reporting it; on a SQL store the request's batch is rolled back, so the sender can retry it whole. A malformed envelope is a `422`; a wrong token is a `401`. Without a token the endpoint accepts any caller, so use that only on localhost.

## Consume a Kafka topic or an SQS queue

Your SaaS publishes envelopes as JSON messages; Keel consumes them. `uv sync --extra kafka` (confluent-kafka) or `--extra sqs` (boto3).

```python
from keel.adapters.bus import KafkaAdapter, SqsAdapter

adapter = KafkaAdapter.connect("broker:9092", topic="keel-events", group_id="keel")
adapter = SqsAdapter.connect("https://sqs.us-east-1.amazonaws.com/123456789012/keel-events")
ingestor.ingest(adapter)            # drain what is there now, then stop
ingestor.ingest(adapter.follow())   # consume forever
```

Delivery is at-least-once: the offset is committed, or the SQS message deleted, only after the handler ran, so a crash mid-event re-delivers it. A message that is not a valid envelope raises and stays on the bus; pass `dead_letter=lambda body, error: ...` to log it somewhere and move on. `KafkaAdapter.connect(..., **config)` merges extra keys into the confluent-kafka consumer config (dotted keys go through a dict: `**{"security.protocol": "SSL"}`); `SqsAdapter.connect(queue_url, **kwargs)` passes them to `boto3.client("sqs", ...)`. `connect` uses the defaults for the rest; to change them, or to pass `dead_letter`, build the adapter from your own client: `KafkaAdapter(consumer, poll_timeout=1.0, dead_letter=...)`, `SqsAdapter(client, queue_url, batch_size=10, wait_seconds=10, dead_letter=...)`. Both adapters only need two calls from their client (`poll`/`commit`, `receive_message`/`delete_message`), which is how the tests run them on fakes and how you would wrap another client.

## Serve the tools over MCP

```bash
uv sync --extra mcp
uv run keel serve cartridges/fleet --tenant acme-logistics --events examples/events/fleet.jsonl
```

With Claude Code, one command registers the server. The agent sees exactly five tools and nothing else:

```bash
claude mcp add keel -- uv run --directory $PWD keel serve cartridges/fleet --tenant acme-logistics --events examples/events/fleet.jsonl
```

With Codex CLI (OpenAI), declare the server in `~/.codex/config.toml`. Keel annotates `context`, `history`, `search` and `related` as read-only, so Codex runs them without asking even under `approval_policy = "never"`; `annotate` writes, so it prompts unless you set `default_tools_approval_mode = "auto"` on the server:

```toml
[mcp_servers.keel]
command = "uv"
args = ["run", "--directory", "/path/to/keel", "keel", "serve", "cartridges/fleet", "--tenant", "acme-logistics", "--events", "examples/events/fleet.jsonl"]
startup_timeout_sec = 60
```

A headless run, for a smoke test or CI:

```bash
codex exec --json "Which tires on unit:MXK-4821 are critical according to the cartridge's own rules? Use only the keel tools." < /dev/null
```

Checked on 2026-10-14 with Codex CLI 0.162 and `openai/gpt-5.2-codex` behind an OpenAI-compatible gateway: seven calls (`search`, `related`, `context`, `history` with `predicate=depth_mm`), the right tire, the right depth on 2026-09-20, and the threshold quoted from the cartridge rather than assumed. Any other MCP client works the same way: a stdio server, five tools, one tenant per server instance. If the cartridge declares `agents:`, add `--agent <id>`: the server then reads and writes only what that agent may, and signs its annotations. Agent-scope memory is read only by the agent that wrote it, so a server started without `--agent` reads none.

!!! note "Where is the graph stored?"
    In memory with `--events`: the server ingests the stream at start and serves it, which is enough for a demo. For a deployment, pass `--db keel.db` instead: a SQLite file (stdlib, WAL mode) that an ingest process fills while `keel serve` reads it. Or point `--db` at a MySQL or MariaDB you already run: `--db mysql://user:password@host:3306/keel` (`mariadb://` is accepted too; `uv sync --extra mysql`); the tables are created on first use and the same conformance suite runs against it in CI. Sharing the database with your application: `?prefix=keel_` names the tables `keel_entities`, `keel_facts`, and so on. In code, the same option is a constructor argument: `SqliteStore("keel.db", prefix="keel_")`, `MysqlStore.from_url(url, prefix="keel_")` or `MysqlStore(connect, prefix="keel_")`.

    ```bash
    uv run keel ingest cartridges/fleet examples/events/fleet.jsonl --db keel.db   # or adapter.follow() in your own process
    uv run keel serve cartridges/fleet --tenant acme-logistics --db keel.db
    uv run keel replay cartridges/fleet examples/events/fleet.jsonl acme-logistics --db keel.db   # wipe one tenant, rebuild it from the log
    ```

    The log is the truth and the file is derived: `replay` rebuilds one tenant from the external log (your outbox table, your bus, a `.jsonl`) and never from the file itself. The store is pluggable: `SqliteStore` is the default, and any store that passes `tests/test_store.py` (see [contributing](contributing.md)) can take its place.

## Serve the tools over HTTP

For a backend, or for many tenants from one process, serve the same five operations as a JSON API (`uv sync --extra http`):

```bash
KEEL_SECRET=change-me uv run keel serve cartridges/fleet --transport http --db keel.db --host 0.0.0.0 --port 8000
```

The secret is read from `KEEL_SECRET`; `--secret-env OTHER_VAR` reads it from another variable. If the variable is unset, the server prints a warning and starts anyway with the API open to any caller, so keep that on localhost.

| Call | Body (`?` optional, default in parentheses) | Returns |
| --- | --- | --- |
| `POST /v1/events` | one envelope or a list | the ingest report: `processed`, `duplicates`, `unhandled`, `errors` |
| `POST /v1/context` | `{"subject", "as_of"?, "depth"? (0), "as_known_at"?, "task"?, "detail"? ("full" or "concise")}` | what `context` returns |
| `POST /v1/history` | `{"subject"?, "since"?, "until"?, "predicate"?, "scope"?, "as_known_at"?, "task"?, "changed_since"?, "limit"?}` | facts |
| `POST /v1/search` | `{"text"? (""), "kind"?, "rule"?, "limit"?, "after"?, "mode"? ("text" or "semantic"), "min_score"?, "related_to"?, "via"?}` | entities and facts |
| `POST /v1/annotate` | `{"subject", "predicate", "value", "scope"? ("task"), "evidence"?, "by"?, "task"?, "key"?, "status"?}` | the fact |
| `POST /v1/related` | `{"subject", "relation"?, "as_of"?}` | neighbours |
| `GET /health` | | `{"ok", "cartridge", "mcp"}` |
| `GET /openapi.json` | | the contract, for generated clients |

Times are ISO 8601 strings. Every request carries `Authorization: Bearer $KEEL_SECRET`, `X-Keel-Tenant` and, when the cartridge declares agents, `X-Keel-Agent`. Your product authenticates its users and sets the two headers; Keel takes them as given and builds the tools for that tenant and agent per request. `/health`, `/openapi.json`, `/docs` and `/redoc` need no token. Two optional headers are stored with every write the request makes: `traceparent` (W3C trace context) and `X-Keel-Tool-Call` (the agent's tool call id).

On `/v1/events`, `X-Keel-Tenant` is optional: each envelope names its tenant, and when the header is present every envelope must match it. The agent registry does not apply to ingest.

| Status | Meaning |
| --- | --- |
| `400` | bad input: missing `X-Keel-Tenant`, an envelope of another tenant, an unknown rule, an invalid `mode` or `detail` |
| `401` | bad or missing bearer token |
| `403` | a refusal (scope policy, agent registry), with its reason |
| `422` | a malformed body, such as an envelope that fails validation |

With `--tenant`, the same server also exposes `/mcp` (MCP over streamable HTTP) bound to that tenant, for agents that speak MCP and cannot run a local process.

```bash
curl -s localhost:8000/v1/context -H "Authorization: Bearer change-me" -H "X-Keel-Tenant: acme-logistics" \
  -H "Content-Type: application/json" -d '{"subject": "tire:T-9001"}'
```

Reads follow the memory scopes. Agent-scope memory is read only by the agent that wrote it (send `X-Keel-Agent` to read your own). Task-scope memory tied to a task id is read only by calls that pass that `task` (`history`'s `task` is an exact filter); task-scope memory with no task id belongs to the tenant. Global-scope memory lives in a reserved partition, `__global__`, that every tenant reads and no event may write; neither a tool call nor an envelope may use that tenant name.

## Semantic search

`search(text, mode="semantic")` finds subjects by meaning ("worn-out tyre" finds a tire at 2.6 mm), filtered by tenant, the agent's read slice, `kind` and `rule`. Two adapters, both replaceable: where vectors live and where embeddings come from.

```bash
uv sync --extra chroma        # only for the bundled Chroma adapter; Python 3.11+ (chromadb needs onnxruntime)
export KEEL_EMBEDDINGS_BASE_URL=https://api.openai.com/v1 KEEL_EMBEDDINGS_MODEL=text-embedding-3-small KEEL_EMBEDDINGS_API_KEY=...
uv run keel index cartridges/fleet --tenant acme-logistics --db keel.db --index chroma:///var/keel/vectors   # cron it: incremental
uv run keel serve cartridges/fleet --transport http --db keel.db --index chroma:///var/keel/vectors
```

`keel index` keeps the time of its last run in a cursor file, `.keel/index-<tenant>.cursor` unless `--cursor` names another, and indexes only what changed since; `--full` rebuilds the tenant's index. `--embedder` on `index` and `serve` picks where embeddings come from: `openai` (the default, configured by the `KEEL_EMBEDDINGS_*` variables), `hash` (deterministic, for tests) or your own `pkg.module:factory?k=v`. Use the same one for both.

Any OpenAI-compatible endpoint works for embeddings (OpenAI, OfoxAI, Azure, Ollama, vLLM).

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
uv run keel index cartridges/fleet --tenant acme-logistics --db keel.db \
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
uv run keel consolidate cartridges/fleet --tenant acme-logistics --db keel.db
uv run keel expire acme-logistics --db keel.db --consolidate cartridges/fleet   # consolidate, then drop expired memory
```

Each run prints `written` and `unchanged` per consolidation and is idempotent: the same inputs never write twice. The originals stay until their TTL. `reduce: summary` needs `--summarizer`: `llm` (any OpenAI-compatible chat model, from `KEEL_SUMMARIZER_BASE_URL`, `KEEL_SUMMARIZER_MODEL` and `KEEL_SUMMARIZER_API_KEY`), `join` (the distinct values joined, deterministic) or your own `pkg.module:factory`. The other reducers are deterministic and need none.

## Export the audit trail

One tenant, everything Keel knows about it, as JSONL: entities, relations and facts, closed ones included, each with `source`, `evidence`, `scope`, `recorded_at` and `corrected_by`. Hand it to the customer, an auditor, or a diff.

```bash
uv run keel export acme-logistics --db keel.db --out acme-logistics.jsonl
```

Each line is one record: `{"record": "fact", "subject": "tire:T-9001", "predicate": "depth_mm", "value": 5.2, "valid_from": ..., "valid_to": ..., "source": "tires.measurements", "evidence": "<event id>", ...}`. Another tenant's export never contains this one's rows.
