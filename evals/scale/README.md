# Scalability evaluation

How Keel's two bundled stores, `MemoryStore` and `SqliteStore`, behave as the log grows: ingest
throughput, replay time, store size, per-tool query latency, a DuckDB bitemporal table as a baseline,
and a determinism check at scale. No LLM calls, no network, no MySQL. It costs nothing to run.

Results: `results/scale.jsonl` (one JSON line per measurement, each with the machine and the Keel
commit) and `results/summary.md` (the tables).

## Data

- **Real**: the three Public Jira Dataset tenants of `evals/jira` (`~/datasets/public-jira/export/<tenant>.bitemporal.jsonl`,
  ~16–18k events each, with inject.py's late arrivals and corrections already in).
- **Synthetic** (`gen.py`): a seeded, streaming generator that mimics the Jira cartridge's event mix as measured
  on those logs (status changes ~42%, assignments ~23%, resolutions ~11%, links ~8.6%, priority/type changes,
  unlinks ~0.6%; creations = 1 / events per issue), Hyperledger's status/priority/type vocabularies, ~3% of issues
  created as sub-tasks, and inject.py's late arrivals (5% of changes) and corrections (2%: a wrong value recorded at
  the change's instant, the right one recorded later) with inject.py's own delay distribution (log-uniform, 1 h to
  30 days). The log is written in recorded order. Knobs: size, tenants (issues split round-robin), mean events per
  issue. Every size spans about five years of world time; 500 issues are in flight at once.

## What is measured

| Measurement | How |
| --- | --- |
| Ingest throughput | `Ingestor.ingest(JsonlAdapter(log), batch=5000)`, i.e. `keel ingest`'s path, JSON parsing included, one fresh process per (log, store). |
| Replay time | `Ingestor.replay(tenant, events=log)` over the built graph: wipe the tenant, rebuild it. |
| Store size | SQLite: file bytes after `wal_checkpoint(TRUNCATE)`. Memory: process RSS after ingest minus before (`psutil`). |
| Query latency | p50/p95/p99 of in-process `Tools` calls, plus the `store.facts` primitive, on a seeded mix drawn like the benchmark's questions (`harness.OPS`). |
| Tenants | 10^6 events split into 1, 10, 100 tenants. |
| Events per subject | 10^5 events with 8, 64, 512, 4096 events per issue (late inserts scan a subject's facts). |
| DuckDB | The same log as `field_versions(tenant, subject, field, value, valid_from, valid_to, recorded_at, closed_at)`, derived with a window function in Keel's succession order; equivalent reads timed on the same parameters (`duck.py`). A test checks DuckDB and Keel give the same answers. |
| Determinism | The whole log, seeded-shuffled, ingested into both stores; the graph hashed without the ingest clock. |
| Transport | The same ops in-process and over `keel serve --transport http` (loopback, keep-alive). |

The ops (`harness.ops`): `context` now, `as_of`, `as_known_at` inside a late arrival's window, and on an unknown
subject; `history` of a subject and of the tenant over a week (`since`/`until`, one predicate); `related` by
relation at an instant; `search` listing the issues with a status at an instant (`where`, `detail="key"`), the
count as known at a later instant, and a rule's count at an instant (`blocked`, `stalled`, `urgent_unassigned`);
and `store.facts` at an instant / as known at one. The weighted mix follows the benchmark's question types.

## Running it

```bash
export KEEL_SCALE_DATA=/path/with/space    # generated logs and databases (10^7 events is ~3.6 GB of .jsonl)
uv run --with duckdb --with psutil pytest evals/scale -q                         # generator, harness, DuckDB parity
uv run --extra http --with duckdb --with psutil python -m evals.scale.harness all --timeout 2700
uv run python -m evals.scale.harness summary                                      # results/summary.md
```

`all` runs every measurement in its own process with a timeout, appends to `results/scale.jsonl`, and does not
try a larger size once a store has timed out. `--steps real,sizes,tenants,per_subject,determinism,duck,http` and
`--sizes` narrow it; `one`, `duck`, `determinism` and `http` run a single measurement (see `--help`).

## Results in brief (Apple M5, 10 cores, 32 GB, macOS 27, Python 3.13, commit e8ae81e)

See `results/summary.md` for every table. The machine was shared with other work: the first `tenants:10` run
was slowed ~2.8x by contention and was rerun (both runs are in `scale.jsonl`; the summary takes the latest).

- **Ingest** finished at every size up to 10^7, no timeouts. SqliteStore: 16–23k events/s at every size
  (16k at 10^7, 10.4 min; 1.2 KB/event on disk). MemoryStore: 30–46k events/s up to 10^5, falling to 15.6k at 10^6
  and 9.9k at 10^7 (11.3 GiB peak RSS). The fall is Python's cyclic GC walking a growing heap: the same 10^6 ingest
  with `gc.disable()` runs at 44k events/s (vs 13.9k).
- **Subject-level reads** (context, history of a subject, related, `store.facts`) stay sub-millisecond at p50 up to
  10^6 and ≤1 ms (memory) / ≤1 ms p50, ≤4 ms p99 (SQLite) at 10^7: they are indexed by subject.
- **Tenant-wide reads grow linearly with the tenant**: `history(since=, until=)` reads every fact of the tenant
  (`keel/tools.py`, `Tools.history`, `self.store.changes(self.tenant, EPOCH)`; the code's own ponytail note), and
  `search(kind/rule/where)` sweeps every entity of the kind (`Tools._search`). In one 10^7-event tenant one call takes
  15–45 s (memory) and 100–230 s (SQLite). Split into 100 tenants, the same 10^6 events answer in 10–100 ms.
- **Many events per subject** (late-insert cost): MemoryStore ingest falls from 27k (8/issue) to 161 events/s
  (4,096/issue) with 7.3 GiB peak RSS for 10^5 events; SqliteStore's bulk path falls to 2.9k events/s.
- **DuckDB** loads the same log 30–90x faster from 10^5 up (10^7 events in 7 s vs 625 s), stores it in 85 B/event
  (14x less than SQLite), and answers tenant-wide reads 2–4 orders of magnitude faster (10^6: status listing 3.5 ms vs
  0.9 s memory / 7.8 s SQLite). On one subject Keel is at par to 6x faster for a whole `context` (which also evaluates
  rules and reads links) and 10–170x faster for the raw `store.facts` read.
- **Determinism**: facts and entities hash identically in written and shuffled order, on both stores, at 10^5 and
  10^6, and replay rebuilds the same facts. Relations do not: 0.6% of relation rows differ under shuffle (link and
  unlink are applied in arrival order, not placed by valid time as facts are).
- **HTTP** (`keel serve`, loopback) adds ~0.6–0.7 ms per call.
