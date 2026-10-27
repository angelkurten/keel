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

## Tenant-wide reads pushed down to the store (label `pushdown:syn:*`)

Stores may answer the tenant-wide reads themselves through optional capabilities Tools feature-detects:
`facts_between` (history's window by valid_from), `subjects_where` (a kind's `where` listing at an instant, as known
then) and `rule_subjects` (a rule's compiled macro). Without them Tools sweeps in Python; with them the outputs are
the same bytes (`tests/test_pushdown.py` on every example cartridge, `evals/jira/test_pushdown.py` on the tool calls
behind all 387 benchmark questions, on SQLite, DuckDB and Postgres). All three stores answer all three: SqliteStore
in plain SQL, its rules compiled by sqlgen's SQLite dialect (one statement per rule, macros inlined, helpers as Python
functions on the connection); DuckdbStore and PostgresStore by their generated macros/functions. The DuckDB and
Postgres columns below were measured on rows copied from the SQLite build (`one --load-from <sqlite file>`), from
before DuckdbStore had its bulk path. Postgres 16 ran in Docker (6 vCPU, 12 GB, default settings, JIT off in Keel's
sessions).

p50 in ms, one 10^7-event tenant (before: the Python path on the same ops):

| op | MemoryStore, Python | SqliteStore, Python | SqliteStore | DuckdbStore | PostgresStore |
| --- | --- | --- | --- | --- | --- |
| history window | 15,019 | 232,525 | 115 | 138 | 114 |
| search status listing at an instant | 15,054 | 129,582 | 16,695 | 162 | 2,719 |
| search count as known at | 37,103 | 141,230 | 22,829 | 438 | 13,133 |
| rule count | 18,481 | 101,007 | 47,703 (pushed down) | 426 | 34,013 |

The (tenant, valid_from) index costs SqliteStore ingest ~20% (10^6: 18.1k -> 14.5k events/s; 10^7: 16.0k -> 13.6k)
and ~20% on disk. See `results/summary.md` for 10^6 and p95.

**SQLite rule push-down** (`kind: rule_ab` in `scale.jsonl`: one process, `search(rule=, detail="count")` pushed
down, then with the capabilities hidden, then pushed down again, same counts every time). In seconds:

| rule | 10^6 pushed down | 10^6 Python | 10^7 pushed down | 10^7 Python |
| --- | --- | --- | --- | --- |
| blocked | 3.9 / 1.3 | 8.5 | 80.7 / 64.0 | 202.1 |
| stalled | 1.2 / 1.4 | 6.7 | 59.4 / 60.8 | 133.1 |
| urgent_unassigned | 2.2 / 2.3 | 6.6 | 67.2 / 72.2 | 124.1 |

3-6x at 10^6, ~2-3x at 10^7. At 10^7 the 15 GB SQLite file did not fit the page cache beside the other work on
the machine (a few hundred MB free), so these runs are I/O-bound and noisy: the harness's latest 10^7 run (5 calls
per op, `results/summary.md`) gave 68 s for the rule count and also slower SQL-only reads than the table above
(status listing 56 s, history window 368 ms) on unchanged code paths. DuckDB answers the same rules in ~0.4 s.

**Typed `where` wants** (`kind: where_ab` in `scale.jsonl`, `python -m evals.scale.where_ab`). `subjects_where` used to
compare only ASCII text; a null, a number, a boolean, non-ASCII text or a list of wants made the store decline and Tools
swept the kind in Python. It now compares them in SQL on the json.dumps text of each value (every JSON text Python finds
equal to a number, text by casefold when that is ASCII, a list as any of), leaving only list values, escaped text and
numeric attributes for Tools to check. One process per store: pushed down, capabilities hidden (the old path), pushed
down again; same counts. In seconds (second pushed-down run):

| op | 10^6 SQLite: before / after | 10^6 DuckDB | 10^7 SQLite | 10^7 DuckDB |
| --- | --- | --- | --- | --- |
| `{"assignee": null}` at an instant | 13.0 / 0.44 | 36.2 / 0.04 | 218.8 / 56.3 | 1,400 / 0.42 |
| `{"status": ["New", "In Progress"]}` at an instant | 12.1 / 0.53 | 37.4 / 0.11 | 227.9 / 46.3 | 1,291 / 0.66 |
| `{"resolution": null}` as known at | 22.8 / 1.19 | 37.7 / 0.18 | 280.5 / 68.2 | 852 / 1.11 |

The SQLite 10^7 file (15 GB) is I/O-bound as above. The last two 10^7 DuckDB rows ran beside the test suite.

The per-kind views sqlgen generates (`<kind>_state_at`, `<kind>_state_known_at`, `<kind>_changes`) are not what
Tools reads: they keep one value per fact (Tools' `where` matches any current value: a tenant fact beside untasked
task memory or a global annotation), they have no author or task (agent and task memory), they list only entities
with a fact, and `<kind>_changes` leaves out first values and corrected ones that `history` returns (marked `initial`,
or with no `was`). On the same 10^6 DuckDB graph, `issue_state_at(...) WHERE status = ...` took 109 ms against
`subjects_where`'s 67 ms (assignee null: 103 vs 50 ms). They stay what agents query in SQL.

**DuckdbStore ingests through Keel** (label `syn:*`, store `duckdb`). A bulk batch's flush binds one list per column
(`INSERT ... SELECT UNNEST(?), ...`) and closes rows with one `UPDATE ... FROM`, instead of DuckDB's row-by-row
executemany: 14.2k events/s at 10^5 (was ~635), 11.1k at 10^6, 7.7k at 10^7 (21.7 min, with the test suite running
beside it); 0.9 KB/event on disk. The 10^6 and 10^7 graphs are row-for-row identical to SqliteStore's builds of the same log
(every table, every column but seq and the ingest clock, close marks and dedupe ids included).
