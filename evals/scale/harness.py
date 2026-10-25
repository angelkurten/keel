"""Scalability harness: ingest throughput, replay time, store size and per-tool latency of Keel's stores.

No LLM, no network. Every measurement runs in its own process (clean memory, a hard timeout), appends
one JSON line to `--out`, and records the machine and the commit it ran on.

    uv run --with duckdb --with psutil python -m evals.scale.harness one --store sqlite --events log.jsonl --out r.jsonl
    uv run --with duckdb --with psutil python -m evals.scale.harness all          # the whole plan of README.md

Subcommands: `one` (ingest, size, queries, replay of one log into one store), `determinism` (the log shuffled
builds the same graph), `duck` (the DuckDB baseline, evals/scale/duck.py), `http` (the same tools over
`keel serve`), `all` (the plan), `summary` (results -> markdown).
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import resource
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.envelope import Event
from keel.ingest import BATCH, JsonlAdapter

ROOT = Path(__file__).resolve().parents[2]
JIRA = ROOT / "evals" / "jira"
RESULTS = Path(__file__).resolve().parent / "results"
DATA = Path(os.environ.get("KEEL_SCALE_DATA", "/tmp/keel-scale"))
REAL = Path.home() / "datasets" / "public-jira" / "export"
EPS = timedelta(microseconds=1)

SUBJECT_OPS = ("context_now", "context_as_of", "context_known_at", "context_missing", "history_subject", "related", "facts_as_of", "facts_known_at")
TENANT_OPS = ("history_tenant_window", "search_rule_count", "search_status_at", "search_known_at_count")
OPS = SUBJECT_OPS + TENANT_OPS
# How often each op is drawn in the weighted mix: the benchmark's question types (129 per tenant: current 20, as_of 25,
# known_at 25, links 15, rule 15, abstain 5, tenant 24; a rule question is a context as_of, a tenant one a listing).
MIX = {"context_now": 20, "context_as_of": 40, "context_known_at": 25, "related": 15, "context_missing": 5,
       "search_status_at": 6, "search_known_at_count": 6, "search_rule_count": 6, "history_tenant_window": 6}
RULES = ("blocked", "stalled", "urgent_unassigned")


# ---- statistics ------------------------------------------------------------------------------------


def percentiles(xs: list[float]) -> dict[str, Any]:
    """Latencies in seconds -> n, mean and p50/p95/p99 in milliseconds (linear interpolation)."""
    if not xs:
        return {"n": 0}
    s = sorted(xs)

    def q(p: float) -> float:
        k = (len(s) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return (s[lo] + (s[hi] - s[lo]) * (k - lo)) * 1000

    return {"n": len(s), "mean_ms": sum(s) / len(s) * 1000, "p50_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99), "max_ms": s[-1] * 1000}


# ---- the graph, canonically ------------------------------------------------------------------------


def _t(d: datetime | None) -> str | None:
    return None if d is None else d.astimezone(timezone.utc).isoformat()


def graph_hash(store: Any, tenant: str, rows: bool = False) -> dict[str, Any]:
    """SHA-256 of the tenant's entities, relations and facts, each sorted, without the ingest clock
    (changed_at): what replay must rebuild identically from the same log in any order."""
    ents, rels, facts = [], [], []
    for x in store.dump(tenant):
        if hasattr(x, "predicate"):
            facts.append(json.dumps([x.subject, x.predicate, x.value, _t(x.valid_from), _t(x.valid_to), _t(x.recorded_at), _t(x.closed_at),
                                     x.source, x.evidence, str(getattr(x.scope, "value", x.scope)), x.status], sort_keys=True, default=str))
        elif hasattr(x, "rel"):
            rels.append(json.dumps([x.src, x.rel, x.dst, _t(x.valid_from), _t(x.valid_to), _t(x.recorded_at), _t(x.closed_at), x.source]))
        else:
            ents.append(json.dumps([x.kind, x.id, x.ref, x.attrs], sort_keys=True, default=str))
    h = lambda rows: hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
    out = {"entities": h(ents), "relations": h(rels), "facts": h(facts), "n_entities": len(ents), "n_relations": len(rels), "n_facts": len(facts)}
    if rows:  # the relation rows themselves, to count how many differ between two builds
        out["_relation_rows"] = rels
    return out


# ---- the query mix --------------------------------------------------------------------------------


def read_events(path: Path) -> Iterator[Event]:
    return iter(JsonlAdapter(path))


@dataclass
class Sample:
    """What the query mix is drawn from: a seeded reservoir of one tenant's events, its late ones, its links,
    and its world-time range."""

    tenant: str
    lo: datetime
    hi: datetime
    events: list[tuple[str, datetime, datetime]]  # (subject, occurred_at, recorded_at)
    late: list[tuple[str, datetime, datetime]]
    links: list[tuple[str, str, datetime]]  # (subject, relation, occurred_at)
    statuses: list[str]
    count: int = 0

    @classmethod
    def of(cls, events: Iterable[Event], seed: int = 1, tenant: str | None = None) -> Sample:
        b = SampleBuilder(seed, tenant)
        for e in events:
            b.add(e)
        return b.sample()


@dataclass
class SampleBuilder:
    seed: int = 1
    tenant: str | None = None
    k: int = 4000
    n: int = 0
    seen: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    res: dict[str, list[Any]] = field(default_factory=lambda: {"events": [], "late": [], "links": []})
    statuses: set[str] = field(default_factory=set)
    lo: datetime | None = None
    hi: datetime | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def _keep(self, name: str, item: Any) -> None:
        self.seen[name] += 1
        pool = self.res[name]
        if len(pool) < self.k:
            pool.append(item)
        else:
            j = self.rng.randrange(self.seen[name])
            if j < self.k:
                pool[j] = item

    def add(self, e: Event) -> None:
        self.tenant = self.tenant or e.tenant
        if e.tenant != self.tenant:
            return
        self.n += 1
        at, rec = e.occurred_at, e.recorded_at
        self.lo = at if self.lo is None or at < self.lo else self.lo
        self.hi = at if self.hi is None or at > self.hi else self.hi
        self._keep("events", (e.subject, at, rec))
        if rec > at:
            self._keep("late", (e.subject, at, rec))
        if e.type == "issue.linked":
            self._keep("links", (e.subject, e.payload["relation"], at))
        if e.type in ("issue.created", "issue.status_changed") and len(self.statuses) < 100:
            self.statuses.add(e.payload["status"])

    def tap(self, events: Iterable[Event]) -> Iterator[Event]:
        """Pass events through while sampling them: the ingest's own read of the log feeds the query mix."""
        for e in events:
            self.add(e)
            yield e

    def sample(self) -> Sample:
        return Sample(self.tenant or "", self.lo, self.hi, self.res["events"], self.res["late"], self.res["links"], sorted(self.statuses), self.n)


@dataclass(frozen=True)
class Op:
    name: str
    kw: dict[str, Any]

    def __hash__(self) -> int:
        return hash((self.name, json.dumps(self.kw, sort_keys=True, default=str)))


def ops(sample: Sample, rng: random.Random, per_op: int, tenant_per_op: int | None = None) -> list[Op]:
    """`per_op` calls of every subject op and `tenant_per_op` (default the same) of every tenant-wide op,
    with parameters drawn from the sample: an instant inside the log, a late arrival's window."""
    tenant_per_op = per_op if tenant_per_op is None else tenant_per_op
    span = (sample.hi - sample.lo).total_seconds()
    minute = lambda d: d.replace(second=0, microsecond=0)
    instant = lambda: minute(sample.lo + timedelta(seconds=rng.uniform(0.05, 1.0) * span))
    late = sample.late or [(s, a, a + timedelta(hours=1)) for s, a, _ in sample.events]
    out: list[Op] = []
    for name in OPS:
        for _ in range(per_op if name in SUBJECT_OPS else tenant_per_op):
            subject, at, _ = sample.events[rng.randrange(len(sample.events))]
            later = minute(at + timedelta(seconds=rng.uniform(0, 30 * 86400)))
            if name == "context_now":
                kw: dict[str, Any] = {"subject": subject}
            elif name == "context_as_of":
                kw = {"subject": subject, "as_of": later}
            elif name in ("context_known_at", "facts_known_at"):
                s, a, r = late[rng.randrange(len(late))]
                known = a + (r - a) * rng.random()  # inside the window: the late value is not known yet
                kw = {"subject": s, "as_of": a, "as_known_at": known}
            elif name == "facts_as_of":
                kw = {"subject": subject, "as_of": later}
            elif name == "context_missing":
                kw = {"subject": f"issue:MISSING-{rng.randrange(10**6)}"}
            elif name == "history_subject":
                kw = {"subject": subject}
            elif name == "related":
                if sample.links:
                    s, rel, a = sample.links[rng.randrange(len(sample.links))]
                    kw = {"subject": s, "relation": rel, "as_of": minute(a + timedelta(days=rng.uniform(0, 60)))}
                else:
                    kw = {"subject": subject}
            elif name == "history_tenant_window":
                t = instant()
                kw = {"since": t, "until": t + timedelta(days=7), "predicate": rng.choice(["assignee", "status"])}
            elif name == "search_rule_count":
                kw = {"rule": rng.choice(RULES), "as_of": instant(), "detail": "count"}
            elif name == "search_status_at":
                kw = {"kind": "issue", "where": {"status": rng.choice(sample.statuses)}, "as_of": instant(), "detail": "key", "limit": 0}
            else:  # search_known_at_count
                t = instant()
                kw = {"kind": "issue", "where": {"status": rng.choice(sample.statuses)}, "as_of": t,
                      "as_known_at": t + timedelta(days=rng.uniform(0, 14)), "detail": "count"}
            out.append(Op(name, kw))
    return out


def call(tools: Tools, store: Any, op: Op) -> Any:
    kw = op.kw
    if op.name.startswith("context"):
        return tools.context(**kw)
    if op.name.startswith("history"):
        return tools.history(**kw)
    if op.name == "related":
        return tools.related(**kw)
    if op.name.startswith("search"):
        return tools.search(**kw)
    if op.name == "facts_as_of":
        return store.facts(tools.tenant, kw["subject"], at=kw["as_of"])
    if op.name == "facts_known_at":
        return store.facts(tools.tenant, kw["subject"], at=kw["as_of"], known_at=kw["as_known_at"])
    raise ValueError(op.name)


def run_ops(tools: Tools, store: Any, todo: list[Op], budget_s: float = 60.0, call_fn: Any = None) -> dict[str, dict[str, Any]]:
    """Time every op; an op name stops once its calls have used `budget_s` (the rest is reported as skipped)."""
    call_fn = call_fn or (lambda op: call(tools, store, op))
    times: dict[str, list[float]] = defaultdict(list)
    spent: dict[str, float] = defaultdict(float)
    errors: dict[str, int] = defaultdict(int)
    first_error: dict[str, str] = {}
    skipped: dict[str, int] = defaultdict(int)
    for op in todo:
        if spent[op.name] >= budget_s:
            skipped[op.name] += 1
            continue
        t0 = time.perf_counter()
        try:
            call_fn(op)
        except Exception as exc:  # noqa: BLE001  counted, never fatal: one bad call must not lose the run
            errors[op.name] += 1
            first_error.setdefault(op.name, f"{type(exc).__name__}: {exc}"[:300])
        dt = time.perf_counter() - t0
        spent[op.name] += dt
        times[op.name].append(dt)
    names = dict.fromkeys(op.name for op in todo)
    return {n: {**percentiles(times[n]), "errors": errors[n], "skipped": skipped[n], **({"error": first_error[n]} if n in first_error else {})}
            for n in names}


def mix_latency(per_op: dict[str, dict[str, Any]]) -> float | None:
    """Mean latency (ms) of the weighted benchmark mix, from each op's mean."""
    have = {k: w for k, w in MIX.items() if per_op.get(k, {}).get("n")}
    if not have:
        return None
    return sum(per_op[k]["mean_ms"] * w for k, w in have.items()) / sum(have.values())


# ---- machine ---------------------------------------------------------------------------------------


def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=ROOT, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def machine() -> dict[str, Any]:
    cpu = _sh("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor()
    mem = _sh("sysctl", "-n", "hw.memsize")
    try:
        import duckdb

        duck = duckdb.__version__
    except ImportError:
        duck = None
    return {"cpu": cpu, "cores": os.cpu_count(), "ram_gb": round(int(mem) / 2**30, 1) if mem.isdigit() else None,
            "os": f"{platform.system()} {platform.release()} ({_sh('sw_vers', '-productVersion') or platform.version()})",
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "duckdb": duck,
            "keel_commit": _sh("git", "rev-parse", "HEAD"), "keel_dirty": bool(_sh("git", "status", "--porcelain", "keel")),
            "keel_version": _keel_version()}


def _keel_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("keel-context")
    except ImportError:
        return None


def rss() -> int:
    try:
        import psutil

        return psutil.Process().memory_info().rss
    except ImportError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def peak_rss() -> int:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


def emit(out: Path, row: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(json.dumps({**row, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, default=str) + "\n")
    print(json.dumps({k: v for k, v in row.items() if k not in ("latency", "machine")}, default=str)[:400], flush=True)


# ---- one log into one store ------------------------------------------------------------------------


STORES = ("memory", "sqlite", "duckdb", "postgres")
PG_URL = os.environ.get("KEEL_POSTGRES_URL")  # postgresql://postgres:keel@127.0.0.1:5499/keel for --store postgres
PG_TABLES = ("entities", "relations", "facts", "ingested", "forgotten", "api_keys", "metering", "cutoffs", "accounts")


def _open(kind: str, db: Path | None, fresh: bool = True) -> Any:
    """A store to measure. `fresh`: empty (files deleted, Postgres tables truncated); else reopen what is there."""
    if kind == "memory":
        return MemoryStore()
    if kind == "postgres":
        from keel.store.postgres import PostgresStore

        assert PG_URL, "--store postgres needs KEEL_POSTGRES_URL"
        store = PostgresStore.from_url(PG_URL)
        if fresh:
            store._exec("TRUNCATE " + ", ".join(PG_TABLES))
        return store
    assert db is not None
    if fresh:
        for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}.wal")):
            p.unlink(missing_ok=True)
    if kind == "duckdb":
        from keel.store.duckdb import DuckdbStore

        return DuckdbStore(db)
    return SqliteStore(db)


# ---- loading a push-down store from a SqliteStore build -------------------------------------------------
# DuckdbStore and PostgresStore ingest through the generic SqlStore path (DuckDB: ~600 events/s), too slow for
# 10^6-10^7 events. The rows Keel's own SqliteStore ingest wrote are the graph, stored identically in every SQL
# store (ISO text instants, json.dumps values), so they are copied as stored: the same graph, loaded fast.

COPIED = {
    "entities": ("tenant", "kind", "id", "ref", "attrs"),
    "relations": ("seq", "tenant", "src", "rel", "dst", "valid_from", "valid_to", "recorded_at", "source", "closed_at", "changed_at"),
    "facts": ("seq", "tenant", "subject", "predicate", "value", "valid_from", "valid_to", "recorded_at", "source", "scope", "corrected_by",
              "evidence", "expires_at", "task", "idempotency_key", "closed_at", "status", "trace", "cartridge", "changed_at"),
    "ingested": ("tenant", "id"),
}
NULL = "\\N"


def _source_rows(src: Path, table: str, cols: tuple[str, ...]) -> Iterator[tuple[Any, ...]]:
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    order = "seq" if "seq" in cols else "rowid"
    cur = con.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")
    while batch := cur.fetchmany(50_000):
        yield from batch
    con.close()


def copy_rows(src: Path, dst: Any) -> dict[str, int]:
    """Copy entities, relations, facts and applied event ids, as stored, from a SqliteStore file into an empty
    DuckdbStore (through CSV and read_csv) or PostgresStore (COPY). Returns rows per table."""
    import csv
    import tempfile

    counts = {}
    if type(dst).__name__ == "PostgresStore":
        for table, cols in COPIED.items():
            n = 0
            with dst._lock:
                cur = dst.db.cursor()
                with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
                    for row in _source_rows(src, table, cols):
                        cp.write_row(row)
                        n += 1
                if "seq" in cols or table == "entities":
                    cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'seq'), (SELECT max(seq) FROM {table}))")
            counts[table] = n
        dst._exec("ANALYZE")
        return counts
    tmp = Path(tempfile.mkdtemp(prefix="keel-copy-", dir=DATA if DATA.exists() else None))
    for table, cols in COPIED.items():
        path, n = tmp / f"{table}.csv", 0
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            for row in _source_rows(src, table, cols):
                w.writerow([NULL if v is None else v for v in row])
                n += 1
        types = ", ".join(f"'{c}': '{'BIGINT' if c == 'seq' else 'VARCHAR'}'" for c in cols)
        with dst._lock:  # the raw connection: the store's cursor would ask the sequence for the id it did not hand out
            dst.db.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM read_csv('{path}', header = false, auto_detect = false, "
                           f"columns = {{{types}}}, nullstr = '{NULL}', quote = '\"', escape = '\"')")
        path.unlink()
        counts[table] = n
    dst._exec("CHECKPOINT")  # ponytail: DuckDB sequences are not moved past the copied seq; the copy is measured, never written to
    tmp.rmdir()
    return counts


def _size(kind: str, store: Any, db: Path | None) -> dict[str, Any]:
    if kind != "sqlite":
        return {}
    store._exec("PRAGMA wal_checkpoint(TRUNCATE)")
    files = {p.name: p.stat().st_size for p in (db, Path(f"{db}-wal")) if p.exists()}
    counts = {t: store._one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("entities", "relations", "facts", "ingested")}
    return {"db_bytes": sum(files.values()), "rows": counts}


def _counts(store: Any, tenants: set[str]) -> dict[str, int]:
    if isinstance(store, MemoryStore):
        return {"entities": sum(len(store._entities[t]) for t in tenants), "relations": sum(len(store._all_rels[t]) for t in tenants),
                "facts": sum(len(store._all_facts[t]) for t in tenants), "ingested": sum(len(store._seen[t]) for t in tenants)}
    return {}


def prepared(store_kind: str, events: Path, out: Path, base: dict[str, Any], db: Path | None, load_from: Path | None, reuse: bool,
             seed: int) -> tuple[Any, Sample, int]:
    """A store already holding the log's graph, without ingesting it here: rows copied from a SqliteStore build
    (`load_from`), or an existing SqliteStore file reopened (`reuse`). The query sample is read from the log."""
    t0 = time.perf_counter()
    store = _open(store_kind, db, fresh=load_from is not None)
    opened = time.perf_counter() - t0  # reopening runs the store's migrations (a new index on an old file)
    sampler = SampleBuilder(seed)
    n = sum(1 for _ in sampler.tap(read_events(events)))
    row = {**base, "phase": "load", "events": n, "open_s": opened, "machine": machine()}
    if load_from is not None:
        t0 = time.perf_counter()
        row.update(rows=copy_rows(load_from, store), load_s=time.perf_counter() - t0, loaded_from=str(load_from))
    else:
        row["reused"] = str(db)
    emit(out, row)
    return store, sampler.sample(), n


def one(store_kind: str, events: Path, out: Path, label: str, db: Path | None = None, per_op: int = 200, tenant_per_op: int = 10,
        budget_s: float = 60.0, replay: bool = True, queries: bool = True, seed: int = 1, load_from: Path | None = None,
        reuse: bool = False) -> None:
    cartridge = load_cartridge(JIRA)
    base = {"kind": "one", "label": label, "store": store_kind, "events_file": str(events)}
    if load_from is not None or reuse:  # the graph is there already: measure the queries only
        store, sample, n = prepared(store_kind, events, out, base, db, load_from, reuse, seed)
        tools = Tools(cartridge, store, sample.tenant)
        todo = ops(sample, random.Random(seed), per_op, tenant_per_op)
        random.Random(seed).shuffle(todo)
        t0 = time.perf_counter()
        lat = run_ops(tools, store, todo, budget_s)
        emit(out, {**base, "phase": "queries", "events": n, "tenant": sample.tenant, "tenant_events": sample.count, "latency": lat,
                   "mix_mean_ms": mix_latency(lat), "wall_s": time.perf_counter() - t0})
        return
    gc.collect()
    rss0 = rss()
    store = _open(store_kind, db)
    sampler = SampleBuilder(seed)
    tenants: set[str] = set()

    def tapped() -> Iterator[Event]:
        for e in sampler.tap(read_events(events)):
            tenants.add(e.tenant)
            yield e

    t0 = time.perf_counter()
    report = Ingestor(cartridge, store).ingest(tapped(), batch=BATCH)
    ingest_s = time.perf_counter() - t0
    gc.collect()
    rss1 = rss()
    n = report.processed + report.duplicates + len(report.errors) + len(report.unhandled)
    row = {**base, "phase": "ingest", "events": n, "processed": report.processed, "errors": len(report.errors), "tenants": len(tenants),
           "ingest_s": ingest_s, "events_per_s": n / ingest_s, "rss_delta_bytes": rss1 - rss0, "peak_rss_bytes": peak_rss(),
           "bytes_per_event_rss": (rss1 - rss0) / max(n, 1), **_size(store_kind, store, db)}
    if store_kind == "memory":
        row["rows"] = _counts(store, tenants)
    if store_kind == "sqlite":
        row["bytes_per_event_disk"] = row["db_bytes"] / max(n, 1)
    if report.errors:
        row["first_error"] = report.errors[0]
    row["machine"] = machine()
    emit(out, row)
    sample = sampler.sample()
    if queries:
        tools = Tools(cartridge, store, sample.tenant)
        todo = ops(sample, random.Random(seed), per_op, tenant_per_op)
        random.Random(seed).shuffle(todo)  # interleaved, so no op runs only on a warm or a cold cache
        t0 = time.perf_counter()
        lat = run_ops(tools, store, todo, budget_s)
        emit(out, {**base, "phase": "queries", "events": n, "tenant": sample.tenant, "tenant_events": sample.count, "latency": lat,
                   "mix_mean_ms": mix_latency(lat), "wall_s": time.perf_counter() - t0})
    if replay:
        t0 = time.perf_counter()
        rep = Ingestor(cartridge, store).replay(sample.tenant, events=read_events(events))
        emit(out, {**base, "phase": "replay", "events": n, "tenant": sample.tenant, "tenant_events": sample.count, "replayed": rep.processed,
                   "replay_s": time.perf_counter() - t0, "replay_events_per_s": rep.processed / (time.perf_counter() - t0)})


# ---- determinism: the log shuffled builds the same graph ------------------------------------------


def determinism(events: Path, out: Path, label: str, stores: list[str], db_dir: Path, seed: int = 1) -> None:
    cartridge = load_cartridge(JIRA)
    log = list(read_events(events))
    shuffled = log[:]
    random.Random(seed).shuffle(shuffled)
    tenant = log[0].tenant
    hashes: dict[str, dict[str, Any]] = {}
    for kind in stores:
        for order, evs in (("written", log), ("shuffled", shuffled)):
            store = _open(kind, db_dir / f"det-{label}-{kind}-{order}.db")
            t0 = time.perf_counter()
            Ingestor(cartridge, store).ingest(evs, batch=BATCH)
            h = graph_hash(store, tenant, rows=True)
            h["ingest_s"] = time.perf_counter() - t0
            if order == "written":  # replay of the written log over the built graph
                Ingestor(cartridge, store).replay(tenant, events=log)
                h["replay_facts"] = graph_hash(store, tenant)["facts"]
            hashes[f"{kind}/{order}"] = h
            del store
            gc.collect()
    ref = hashes[f"{stores[0]}/written"]
    same = {part: all(h[part] == ref[part] for h in hashes.values()) for part in ("entities", "relations", "facts")}
    from collections import Counter

    base = Counter(ref["_relation_rows"])
    for h in hashes.values():  # relation rows present in one build and not in the reference (multiset difference, both ways)
        mine = Counter(h.pop("_relation_rows"))
        h["relation_rows_differing"] = sum(((mine - base) + (base - mine)).values())
    ref.pop("_relation_rows", None)
    emit(out, {"kind": "determinism", "label": label, "events": len(log), "events_file": str(events), "hashes": hashes, "same": same,
               "replay_same_facts": all(h.get("replay_facts", h["facts"]) == h["facts"] for h in hashes.values()), "machine": machine()})


# ---- the plan ---------------------------------------------------------------------------------------


def _child(args: list[str], timeout_s: float, out: Path, what: dict[str, Any]) -> bool:
    """Run one measurement in its own process; on a timeout, record where it stopped."""
    cmd = [sys.executable, "-m", "evals.scale.harness", *args, "--out", str(out)]
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, cwd=ROOT, timeout=timeout_s, check=False)
        if r.returncode != 0:
            emit(out, {**what, "phase": "failed", "returncode": r.returncode, "wall_s": time.perf_counter() - t0})
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        emit(out, {**what, "phase": "timeout", "timeout_s": timeout_s, "wall_s": time.perf_counter() - t0})
        return False


def synthetic(n: int, tenants: int = 1, per_subject: float = 7.0, seed: int = 7) -> Path:
    from evals.scale import gen

    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"syn-{n}-t{tenants}-m{int(per_subject)}-s{seed}.jsonl"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        gen.write(tmp, gen.generate(n, seed=seed, tenants=tenants, per_subject=per_subject))
        tmp.rename(path)
    return path


def _memory_need(out: Path, n: int) -> float | None:
    """RSS a MemoryStore would take for n events, from the largest synthetic size measured so far."""
    best = None
    if out.exists():
        for line in out.open():
            r = json.loads(line)
            if (r.get("store") == "memory" and r.get("phase") == "ingest" and str(r.get("label", "")).startswith("syn:")
                    and (best is None or r["events"] > best["events"])):
                best = r
    return None if best is None else best["bytes_per_event_rss"] * n


def plan(out: Path, sizes: list[int], timeout_s: float, steps: set[str]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    db = lambda name: DATA / f"{name}.db"
    stopped: dict[str, int] = {}  # store -> the size it timed out at: larger sizes are not tried
    if "real" in steps:
        for t in ("hyperledger", "mariadb", "spring"):
            f = REAL / f"{t}.bitemporal.jsonl"
            if f.exists():
                for s in ("memory", "sqlite"):
                    _child(["one", "--store", s, "--events", str(f), "--label", f"real:{t}", "--db", str(db(f"real-{t}"))], timeout_s, out,
                           {"kind": "one", "label": f"real:{t}", "store": s})
    if "sizes" in steps:
        for n in sizes:
            f = synthetic(n)
            for s in ("memory", "sqlite"):
                if s in stopped:
                    emit(out, {"kind": "one", "label": f"syn:{n}", "store": s, "phase": "not_run", "reason": f"timed out at {stopped[s]}"})
                    continue
                need = _memory_need(out, n) if s == "memory" else None
                if need is not None and need > 0.8 * (machine()["ram_gb"] or 0) * 2**30:
                    emit(out, {"kind": "one", "label": f"syn:{n}", "store": s, "phase": "not_run", "projected_bytes": need,
                               "reason": f"MemoryStore would need ~{need / 2**30:.0f} GiB (RSS per event at the previous size x {n}), over 80% of RAM"})
                    continue
                tpo = 10 if n <= 10**6 else 3
                ok = _child(["one", "--store", s, "--events", str(f), "--label", f"syn:{n}", "--db", str(db(f"syn-{n}")), "--tenant-per-op", str(tpo)],
                            timeout_s, out, {"kind": "one", "label": f"syn:{n}", "store": s, "events": n})
                if not ok:
                    stopped[s] = n
    if "tenants" in steps:
        for t in (1, 10, 100):
            f = synthetic(10**6, tenants=t)
            for s in ("memory", "sqlite"):
                _child(["one", "--store", s, "--events", str(f), "--label", f"tenants:{t}", "--db", str(db(f"ten-{t}"))], timeout_s, out,
                       {"kind": "one", "label": f"tenants:{t}", "store": s})
    if "per_subject" in steps:
        for m in (8, 64, 512, 4096):
            f = synthetic(10**5, per_subject=m)
            for s in ("memory", "sqlite"):
                _child(["one", "--store", s, "--events", str(f), "--label", f"per_subject:{m}", "--db", str(db(f"per-{m}"))], timeout_s, out,
                       {"kind": "one", "label": f"per_subject:{m}", "store": s})
    if "determinism" in steps:
        for n in (10**5, 10**6):
            _child(["determinism", "--events", str(synthetic(n)), "--label", f"syn:{n}"], timeout_s * 2, out, {"kind": "determinism", "label": f"syn:{n}"})
    if "duck" in steps:
        files = [(f"real:{t}", REAL / f"{t}.bitemporal.jsonl") for t in ("hyperledger",)] + [(f"syn:{n}", synthetic(n)) for n in sizes]
        for label, f in files:
            if f.exists():
                _child(["duck", "--events", str(f), "--label", label], timeout_s, out, {"kind": "duck", "label": label})
    if "http" in steps:
        _child(["http", "--events", str(synthetic(10**5)), "--label", "syn:100000", "--db", str(db("http"))], timeout_s, out, {"kind": "http"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("one")
    o.add_argument("--store", choices=list(STORES), required=True)
    o.add_argument("--load-from", type=Path, help="copy the rows of this SqliteStore file (built from --events) instead of ingesting")
    o.add_argument("--reuse", action="store_true", help="reopen the SqliteStore file --db (built from --events) instead of ingesting")
    o.add_argument("--events", type=Path, required=True)
    o.add_argument("--label", required=True)
    o.add_argument("--db", type=Path)
    o.add_argument("--per-op", type=int, default=200)
    o.add_argument("--tenant-per-op", type=int, default=10)
    o.add_argument("--budget", type=float, default=60.0)
    o.add_argument("--no-replay", action="store_true")
    o.add_argument("--no-queries", action="store_true")
    d = sub.add_parser("determinism")
    d.add_argument("--events", type=Path, required=True)
    d.add_argument("--label", required=True)
    d.add_argument("--stores", default="memory,sqlite")
    k = sub.add_parser("duck")
    k.add_argument("--events", type=Path, required=True)
    k.add_argument("--label", required=True)
    k.add_argument("--per-op", type=int, default=200)
    k.add_argument("--tenant-per-op", type=int, default=10)
    h = sub.add_parser("http")
    h.add_argument("--events", type=Path, required=True)
    h.add_argument("--label", required=True)
    h.add_argument("--db", type=Path, required=True)
    h.add_argument("--per-op", type=int, default=100)
    a = sub.add_parser("all")
    a.add_argument("--sizes", default="10000,100000,1000000,10000000")
    a.add_argument("--timeout", type=float, default=1800, help="seconds per measurement process (the ~30 min rule)")
    a.add_argument("--steps", default="real,sizes,tenants,per_subject,determinism,duck,http")
    s = sub.add_parser("summary")
    s.add_argument("results", type=Path, nargs="?", default=RESULTS / "scale.jsonl")
    s.add_argument("--md", type=Path, default=RESULTS / "summary.md")
    for p in (o, d, k, h, a):
        p.add_argument("--out", type=Path, default=RESULTS / "scale.jsonl")
    args = ap.parse_args()
    if args.cmd == "one":
        one(args.store, args.events, args.out, args.label, args.db, args.per_op, args.tenant_per_op, args.budget, not args.no_replay, not args.no_queries,
            load_from=args.load_from, reuse=args.reuse)
    elif args.cmd == "determinism":
        determinism(args.events, args.out, args.label, args.stores.split(","), DATA)
    elif args.cmd == "duck":
        from evals.scale import duck

        duck.bench(args.events, args.out, args.label, args.per_op, args.tenant_per_op, DATA)
    elif args.cmd == "http":
        from evals.scale import http_leg

        http_leg.bench(args.events, args.out, args.label, args.db, args.per_op)
    elif args.cmd == "all":
        plan(args.out, [int(x) for x in args.sizes.split(",")], args.timeout, set(args.steps.split(",")))
    else:
        from evals.scale import summary

        summary.write(args.results, args.md)


if __name__ == "__main__":
    main()
