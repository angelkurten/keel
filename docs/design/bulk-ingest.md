# Design note: ingesting millions of events

Status: researched, measured and implemented on 2026-10-14 (design A, `SqlStore.bulk`, shipped in 0.8.0). The numbers after implementation are at the end.

## Where the time goes

Measured in this repo (Keel 0.7.1, fleet cartridge, synthetic streams):

| Measure | Value |
| --- | --- |
| Round trip per SQL statement, MySQL 8.4 in Docker on macOS (PyMySQL) | 0.64 ms |
| Statements per event after 0.7.1 (remember, upsert, link, read facts, close, insert) | about 4 |
| 3030 events on MySQL, batch of 500 | 8.9 s (30.3 s on 0.6.0) |
| 101 000 events on SQLite, batch of 500 | 4.0 s (25 000 events/s) |
| Handlers against `MemoryStore` (indexed by subject since `daad802`) | 160 000 events/s |
| Parsing and validating envelopes | 157 000 events/s |
| Multi-row `INSERT` of fact rows into MySQL (`executemany`, 1000 rows per statement) | 60 000 rows/s |
| Batched lookup of 500 `(subject, predicate)` pairs with a row constructor `IN` | 20 ms |

So the library's own work is a few seconds per million events; on MySQL 85 to 90 percent of the wall time is network round trips, one per statement. Extrapolated, one million events today would take about 45 minutes on MySQL and 40 seconds on SQLite. Indexes are not the issue: every query uses one and touches one to ten rows.

## What the literature says

Ranked by expected gain for this workload (sources in the research below):

1. **Set-based batches**: one statement per table per batch instead of four per event removes the round trips. Multi-row `INSERT` is 10 to 30 times faster than row-by-row on MySQL (Karwin: 1M rows, 527 s row-by-row, 63 s with 100 rows per statement and 100 statements per transaction; FromDual: 881 s to 4.5 s on 100k rows). PyMySQL's `executemany` only rewrites `INSERT ... VALUES` into a multi-row statement; `UPDATE` stays one round trip per row, so closes need a `CASE`/`VALUES ROW` form.
2. **Group the batch by key in memory** so each `(subject, predicate)` is read once per batch and later events in the batch resolve against memory. Batched reads use row constructors (`WHERE (tenant, subject, predicate) IN (...)`), which MySQL turns into range access on the composite index as long as the list stays in the hundreds to low thousands (above `range_optimizer_max_mem_size` it silently falls back to a scan, warning 3170).
3. **One transaction per batch of 500 to 5000** and inserts in primary-key order (done); each extra secondary index multiplies the cost (DoltHub: 4 indexes, 4.4 times slower).
4. **`LOAD DATA LOCAL INFILE`** for replays and imports: a further 1.5 to 3 times over multi-row inserts (100k+ rows/s), at the cost of enabling `local_infile` on both sides and temp files.
5. **Parallel workers by tenant**, 4 to 8, each with its own connection, `READ COMMITTED`: 2 to 6 times, flattening past four. Partition by tenant, never by subject within a tenant, so no two writers share a unique key (concurrent `INSERT IGNORE` on the same keys deadlocks or stalls, Bug #106610).
6. Server knobs (`innodb_flush_log_at_trx_commit`, `unique_checks`), the C driver, multi-statement packets: 5 to 30 percent each once batching exists, some with correctness or ops risk. Not the library's business.

Event-sourcing projections do the same: Marten fetches 500 events per batch and, for rebuilds, aggregates a stream in memory and writes once; Kafka Connect's JDBC sink batches 3000 rows; EventStoreDB stores the checkpoint in the same transaction as the rows.

## Recommended design

**A. Buffered flush per batch, both SQL stores.** `Ingestor.ingest(..., batch=N)` already groups events; the store gains a bulk path the Ingestor uses when the batch is larger than one:

1. Collect the batch's keys: subjects, `(subject, predicate)` pairs, relation triples, event ids.
2. Three batched reads (row-constructor `IN`, chunks of 1000): open facts per pair, entities, open relations; plus one `SELECT` of already-seen event ids.
3. Run the handlers against an in-memory overlay seeded with that state, in event order. Placement and closing (`place_fact`) happen in memory, exactly as today, so arrival order and provenance keep the 0.4.1 guarantees.
4. Write back: multi-row `INSERT ... ON DUPLICATE KEY UPDATE` for entities, multi-row `INSERT` for new relations and new facts, one `UPDATE ... CASE seq` (or `VALUES ROW` join on MySQL 8.0.19+) for closed facts and relations, multi-row `INSERT` into `ingested`. Commit.

About eight statements per batch of 1000 instead of 4000. Estimated for one million events: handlers and parsing about 12 s, four million rows at 60 000 rows/s about 70 s, round trips negligible; **two minutes on MySQL** against 45 today, and SQLite under 20 s with `executemany`. A failed batch rolls back whole, as now. `batch=1` keeps the current per-event path for streams.

**B. Replay fast path (later, MySQL).** A replay starts from an empty subgraph, so no reads at all: build the tenant in memory, spill to TSV, `LOAD DATA LOCAL INFILE`. About one minute per million events; needs `local_infile` on.

**C. Parallel driver (later).** `keel ingest --workers N` sharding tenants across processes. Only when backfills span many tenants.

## Caveats to carry into the implementation

- Keep `IN` lists at or under 1000 tuples; check `SHOW WARNINGS` in tests on MySQL.
- `executemany` is per-row for `UPDATE` on PyMySQL: closes must be one statement with `CASE` or a `VALUES ROW` join.
- Memory per batch is bounded by the batch, not the tenant: the overlay holds only the touched keys.
- The audit `seq` order of facts is insertion order within a batch; `dump` and `export` are unaffected.
- `innodb_flush_log_at_trx_commit` is global: document it for operators, never set it from the library.

## Research sources

- MySQL insert cost model and bulk loading: https://dev.mysql.com/doc/refman/8.4/en/insert-optimization.html, https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-bulk-data-loading.html
- Row constructor range optimization and its memory limit: https://dev.mysql.com/doc/refman/8.4/en/range-optimization.html, https://www.percona.com/blog/why-are-queries-with-many-in-values-more-expensive-after-upgrading-to-mysql-8-x/
- Table value constructors for set-based updates: https://dev.mysql.com/doc/refman/8.4/en/values.html
- Measured bulk-load numbers: https://www.slideshare.net/billkarwin/load-data-fast, https://fromdual.com/node/106, https://www.dolthub.com/docs/sql-reference/benchmarks/import, https://planet.mysql.com/entry/?id=2007881
- PyMySQL `executemany` rewrite: https://github.com/PyMySQL/PyMySQL/blob/main/pymysql/cursors.py
- InnoDB locking of duplicate inserts and auto-increment modes: https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html, https://dev.mysql.com/doc/refman/8.4/en/innodb-auto-increment-handling.html, https://bugs.mysql.com/bug.php?id=106610
- SQLite: https://www.sqlite.org/faq.html#q19, https://avi.im/blag/2021/fast-sqlite-inserts/, https://www.sqlite.org/pragma.html#pragma_synchronous
- Projection practice: https://martendb.io/events/projections/rebuilding, https://martendb.io/events/optimizing, https://docs.confluent.io/kafka-connectors/jdbc/current/sink-connector/sink_config_options.html, https://developers.eventstore.com/clients/dotnet/5.0/subscriptions/

## After implementation (0.8.0)

`SqlStore.bulk(events)` is the overlay described in design A; `Ingestor.ingest(batch=N)` uses it for every chunk larger than one event, so the CLI (500), the HTTP batch and `replay` get it without changes; streams (`batch=1`) keep the per-event path. Prefetch reads the batch's event ids first, so a redelivered batch costs one lookup per thousand events and loads no state.

| Measure (same machine, same streams) | 0.7.1 | 0.8.0 |
| --- | --- | --- |
| 3030 events on MySQL | 8.9 s | 0.5 s |
| 101 000 events on MySQL | about 300 s (extrapolated) | 8.4 s |
| 101 000 events on SQLite | 4.0 s | 2.9 s |
| 101 000 events re-sent to MySQL (all duplicates) | | 2.1 s |

About 12 000 events per second on MySQL through Docker: a million events in under a minute and a half, write-bound (four million rows at 60 000 rows per second), with round trips no longer measurable. Designs B (replay through `LOAD DATA`) and C (workers per tenant) stay on the shelf until someone needs more.
