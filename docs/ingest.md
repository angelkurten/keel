# Send your events

Keel ingests envelopes, never raw rows. Every source produces the same shape, and the cartridge's handlers are the only code that reads a payload. Pick the source that matches where your events already are; you can combine several for one tenant, because the event `id` dedupes.

| Source | Use it when | Guarantee |
| --- | --- | --- |
| [Postgres CDC](#postgres-cdc-your-own-tables-no-outbox) | You would rather not touch the product's code: Keel follows your own tables. | The replication slot moves only after the store commits. |
| [The typed SDK](#from-your-code-the-typed-sdk) | Your backend emits events from business code, with types generated from the cartridge. | Ids are a content hash when you send none: a retry is a duplicate. |
| [An outbox table](#a-postgres-outbox-table) | You write domain events to a table in the same transaction as the change. | At-least-once, with a cursor; restarts resume. |
| [A webhook](#a-webhook) | Something else pushes JSON envelopes over HTTP. | The response is the ingest report; the sender retries. |
| [SQS or Kafka](#consume-a-kafka-topic-or-an-sqs-queue) | You already have a queue or a topic. | At-least-once, acknowledged after each commit. |
| [A `.jsonl` file](#a-jsonl-file) | Development, history backfill, tests, replay. | Deterministic: the same log gives the same graph. |

Where this fits in the whole: [Architecture](architecture.md).

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

The command prints `processed`, `duplicates`, `unhandled`, `errors` and `skipped` (events for a forgotten tenant, see [Erase a tenant](operate.md#erase-a-tenant)) and carries on past a bad event. `--strict` stops at the first unhandled type or handler error instead; on a SQL store the batch in flight (5000 events) is rolled back.

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

`keel serve --transport http --cdc cdc.yaml` (with `KEEL_PG_DSN` set) follows the same tables in a thread of the server instead of a second process: it creates what is missing, retries after a failure with a growing pause, and `/health` shows `cdc` with `ok`, `events`, `last_read` and the last `error`. `--account` (or `KEEL_ACCOUNT`, as a hosted container has) puts every tenant under that account, `account/<tenant>`, for both commands.

## From your code: the typed SDK

When your product's backend is the one that knows an event happened, emit it from there. `keel sdk <language> <cartridge>` generates a typed client from your cartridge: an `emit` constructor per cataloged event, whose payload type comes from the catalog, so a wrong key or type fails in your editor or compiler instead of in a handler. The same client calls the five tools.

```ts
import { client, emit, subjects } from "./keel-fleet";   // keel sdk typescript examples/fleet -o src/keel-fleet.ts

const keel = client({ baseUrl: "http://keel.internal:8000", tenant: "acme-logistics", secret: process.env.KEEL_SECRET });

const report = await keel.events([
  emit.tireDepthMeasured({ tenant: "acme-logistics", subject: subjects.tire("T-9001"), occurredAt: new Date(), source: "tires.measurements", payload: { unit_id: "MXK-4821", position: "P3", depth_mm: 2.6 } }),
]);
report.processed;   // 1
```

`events()` posts one envelope or a list to `POST /v1/events` on `keel serve --transport http` and returns the ingest report (`processed`, `duplicates`, `unhandled`, `errors`). When an envelope has no `id`, the server derives one from its content, so retrying after a network error stores it once. Python, Go and PHP work the same way: see [SDKs](sdks.md).

Where you call it matters: right after the business transaction commits, an event can be lost if the process dies in between. When that is unacceptable, write the event to an [outbox table](#a-postgres-outbox-table) in the same transaction, or let [CDC](#postgres-cdc-your-own-tables-no-outbox) follow the table.

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
