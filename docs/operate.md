# Operate

Configuration, the container, and the jobs that run next to the server. Jobs run outside ingest, from the CLI or your scheduler, so unlike handlers they may call the network or an LLM.

| Command | What it does | How often |
| --- | --- | --- |
| [`keel expire --consolidate`](#consolidate-episodic-memory) | Consolidates episodic memory into durable facts, then drops expired `task` and `agent` memory. | Daily |
| [`keel index`](serve.md#semantic-search) | Incremental indexing for semantic search. | Every few minutes |
| `keel replay` | Rebuilds a tenant's graph from its log, keeping annotations. | After changing handlers |
| [`keel export`](#export-the-audit-trail) | One tenant's audit trail as JSONL. | On demand |
| [`keel forget`](#erase-a-tenant) | Erases a tenant, or with `--before`, what closed before a date. | On demand, or scheduled for retention |
| `keel usage` | Events ingested and calls per tool, read from the log. | For metering |

`keel index`, `keel consolidate` and `keel expire` take `--all-tenants` to run over every tenant in the store.

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

## Run it in a container

`ghcr.io/angelkurten/keel` serves the tools over HTTP (`/v1` and `/mcp`; `KEEL_TENANT` binds `/mcp` to one tenant) on port 8000, for amd64 and arm64. Tags follow the releases: `0.29.1`, `0.29`, `latest`, and `-slim` variants without the Chroma client (a third of the size, no semantic search on Chroma). The cartridge is not in the image: mount it at `/cartridge`. Both variants include the Postgres driver, so `KEEL_CDC_MAP` and `KEEL_PG_DSN` make the container follow Postgres too.

```bash
docker run -p 8000:8000 -e KEEL_SECRET=$(openssl rand -hex 32) \
  -e KEEL_DB=mysql://keel:secret@db.internal:3306/keel \
  -v ./examples/fleet:/cartridge:ro -v keel-data:/data ghcr.io/angelkurten/keel:0.29.1
```

- It refuses to start without `KEEL_SECRET` (or `--allow-open` behind your own auth proxy), since it listens on every interface.
- `/data` is the working directory and a volume: a SQLite graph (`KEEL_DB=/data/keel.db`), the `keel index` cursor (`/data/.keel/`) and a `.env` live there.
- It runs as an unprivileged user and checks its own `/health` (no `curl` needed in the image). Configuration is the environment ([Configuration](#configuration)); every command reads `KEEL_DB`, `KEEL_TENANT` and `KEEL_INDEX`.
- Any other command runs the same way: `docker run ... ghcr.io/angelkurten/keel:0.29.1 index /cartridge` (the entrypoint is `keel`).
- To ship your cartridge inside, two lines: `FROM ghcr.io/angelkurten/keel:0.29.1` and `COPY cartridges/mine /cartridge`.

Single sign-on for your team in front of `/ui`, with Caddy and oauth2-proxy and no code in Keel: [`deploy/sso`](https://github.com/angelkurten/keel/tree/master/deploy/sso).

`deploy/docker-compose.yml` runs Keel with its own MySQL, a Chroma server and `keel index` every 15 minutes: `KEEL_SECRET=... docker compose -f deploy/docker-compose.yml up -d`. `docker build -t keel .` builds this checkout; `--build-arg KEEL_VERSION=0.29.1` builds a release from PyPI. `deploy/smoke.sh <image>` checks an image end to end.

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
