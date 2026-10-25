"""Tests for the scalability harness: the synthetic generator, the harness's own logic, and the DuckDB baseline.

    uv run --with duckdb --with psutil pytest evals/scale -q
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from evals.scale import gen, harness
from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.envelope import Event

JIRA = Path(__file__).parents[1] / "jira"


def _ingest(events, store=None):
    store = store or MemoryStore()
    report = Ingestor(load_cartridge(JIRA), store).ingest([Event(**e) for e in events], strict=True, batch=500)
    return store, report


# ---- generator ------------------------------------------------------------------------------------


def test_generator_is_seeded_and_exact():
    a = list(gen.generate(2000, seed=3))
    assert a == list(gen.generate(2000, seed=3))
    assert a != list(gen.generate(2000, seed=4))
    assert len(a) == 2000
    assert len({e["id"] for e in a}) == 2000


def test_generator_writes_the_log_in_recorded_order():
    evs = list(gen.generate(5000, seed=1))
    rec = [datetime.fromisoformat(e["recorded_at"]) for e in evs]
    assert rec == sorted(rec)
    assert all(datetime.fromisoformat(e["recorded_at"]) >= datetime.fromisoformat(e["occurred_at"]) for e in evs)


def test_generated_events_ingest_cleanly_with_the_jira_cartridge():
    evs = list(gen.generate(4000, seed=2))
    store, report = _ingest(evs)
    assert report.processed == 4000 and not report.errors and not report.unhandled
    assert store.entities("syn0", "issue")


def test_generator_mimics_the_jira_mix_and_injection_rates():
    evs = list(gen.generate(40000, seed=5))
    kinds = Counter(e["type"] for e in evs)
    share = {k: v / len(evs) for k, v in kinds.items()}
    assert 0.33 < share["issue.status_changed"] < 0.47
    assert 0.17 < share["issue.assigned"] < 0.27
    assert 0.10 < share["issue.created"] < 0.18
    assert 0.04 < share["issue.linked"] < 0.10
    assert kinds["issue.unlinked"] > 0
    changes = [e for e in evs if e["type"] in gen.CHANGES and not e["id"].endswith(":wrong")]
    wrong = [e for e in evs if e["id"].endswith(":wrong")]
    late = [e for e in changes if e["recorded_at"] != e["occurred_at"]]
    assert 0.015 < len(wrong) / len(changes) < 0.025  # corrections: 2% of changes
    assert 0.06 < len(late) / len(changes) < 0.08  # late (5%) plus the corrected values that arrive later (2%)


def test_corrections_are_wrong_first_and_right_later():
    evs = list(gen.generate(20000, seed=6))
    by_id = {e["id"]: e for e in evs}
    wrong = [e for e in evs if e["id"].endswith(":wrong")]
    assert wrong
    store, _ = _ingest(evs)
    for w in wrong[:20]:
        right = by_id[w["id"].removesuffix(":wrong")]
        assert w["occurred_at"] == right["occurred_at"] and w["recorded_at"] == w["occurred_at"] < right["recorded_at"]
        predicate, key = gen.CHANGES[right["type"]]
        assert w["payload"][key] != right["payload"][key]
        at = datetime.fromisoformat(right["occurred_at"])
        known = datetime.fromisoformat(right["recorded_at"])

        def value(k, w=w, at=at, predicate=predicate):
            return [f.value for f in store.facts(w["tenant"], w["subject"], at=at, known_at=k) if f.predicate == predicate]

        assert value(known - timedelta(microseconds=1)) == [w["payload"][key]]
        assert value(known) == [right["payload"][key]]


def test_unlinks_only_close_links_that_are_open():
    evs = sorted(gen.generate(20000, seed=8), key=lambda e: (e["occurred_at"], e["id"]))
    open_links: set[tuple[str, str, str]] = set()
    for e in evs:
        if e["type"] in ("issue.linked", "issue.unlinked"):
            k = (e["subject"], e["payload"]["relation"], e["payload"]["other"])
            if e["type"] == "issue.linked":
                assert k not in open_links
                open_links.add(k)
            else:
                assert k in open_links
                open_links.remove(k)


def test_tenants_partition_issues():
    evs = list(gen.generate(10000, seed=9, tenants=10))
    tenants = defaultdict(set)
    for e in evs:
        tenants[e["subject"]].add(e["tenant"])
    assert all(len(t) == 1 for t in tenants.values())
    per = Counter(e["tenant"] for e in evs)
    assert len(per) == 10 and min(per.values()) > 500


@pytest.mark.parametrize("m", [8, 64, 512])
def test_events_per_subject_is_controlled(m):
    evs = list(gen.generate(20000, seed=10, per_subject=m))
    per = Counter(e["subject"] for e in evs)
    mean = len(evs) / len(per)
    assert 0.6 * m < mean < 1.4 * m


# ---- harness ----------------------------------------------------------------------------------------


def test_percentiles():
    p = harness.percentiles([i / 1000 for i in range(1, 101)])  # 1..100 ms
    assert p["n"] == 100
    assert p["p50_ms"] == pytest.approx(50.5, abs=0.6)
    assert p["p95_ms"] == pytest.approx(95.05, abs=0.6)
    assert p["p99_ms"] == pytest.approx(99.01, abs=0.6)
    assert harness.percentiles([])["n"] == 0


def test_graph_hash_ignores_arrival_order_and_store():
    evs = list(gen.generate(6000, seed=11))
    shuffled = evs[:]
    random.Random(1).shuffle(shuffled)
    a, _ = _ingest(evs)
    b, _ = _ingest(shuffled)
    c, _ = _ingest(evs, SqliteStore(":memory:"))
    ha, hb, hc = (harness.graph_hash(s, "syn0") for s in (a, b, c))
    assert ha["facts"] == hb["facts"] == hc["facts"]
    assert ha["entities"] == hb["entities"] == hc["entities"]
    assert ha["relations"] == hc["relations"]  # the same order on two stores builds the same links
    assert ha["n_facts"] > 6000


def test_graph_hash_sees_a_changed_value():
    evs = list(gen.generate(3000, seed=12))
    a, _ = _ingest(evs)
    i = next(i for i, e in enumerate(evs) if e["type"] == "issue.assigned")
    evs[i] = {**evs[i], "payload": {**evs[i]["payload"], "assignee": "someone-else"}}
    b, _ = _ingest(evs)
    assert harness.graph_hash(a, "syn0")["facts"] != harness.graph_hash(b, "syn0")["facts"]


def test_query_mix_is_seeded_covers_every_op_and_runs(tmp_path):
    evs = list(gen.generate(5000, seed=13))
    path = tmp_path / "e.jsonl"
    gen.write(path, evs)
    sample = harness.Sample.of(harness.read_events(path), seed=1)
    ops = harness.ops(sample, random.Random(2), per_op=4)
    assert ops == harness.ops(sample, random.Random(2), per_op=4)
    assert {o.name for o in ops} == set(harness.OPS)
    store, _ = _ingest(evs)
    tools = Tools(load_cartridge(JIRA), store, "syn0")
    res = harness.run_ops(tools, store, ops, budget_s=30)
    assert set(res) == set(harness.OPS)
    assert all(r["n"] == 4 and r["errors"] == 0 for r in res.values())


def test_read_events_streams_envelopes(tmp_path):
    evs = list(gen.generate(100, seed=14))
    path = tmp_path / "e.jsonl"
    gen.write(path, evs)
    got = list(harness.read_events(path))
    assert [e.id for e in got] == [e["id"] for e in evs]
    assert all("recorded_at" in e.model_fields_set for e in got)


def test_machine_specs():
    m = harness.machine()
    for k in ("cpu", "cores", "ram_gb", "os", "python", "keel_commit", "keel_dirty", "sqlite"):
        assert k in m


# ---- DuckDB baseline: answers what Keel answers ------------------------------------------------------


def test_duckdb_baseline_agrees_with_keel(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from evals.scale import duck

    evs = list(gen.generate(6000, seed=15))
    path = tmp_path / "e.jsonl"
    gen.write(path, evs)
    con = duckdb.connect()
    stats = duck.load(con, path)
    assert stats["rows"] > 6000
    store, _ = _ingest(evs)
    tools = Tools(load_cartridge(JIRA), store, "syn0")
    rng = random.Random(3)
    sample = harness.Sample.of(harness.read_events(path), seed=2)
    for op in harness.ops(sample, rng, per_op=15):
        if op.name not in ("context_now", "context_as_of", "context_known_at"):
            continue
        subject, at, known = op.kw["subject"], op.kw.get("as_of"), op.kw.get("as_known_at")
        at = at or known
        for field in ("status", "assignee", "priority"):
            fs = [f.value for f in store.facts("syn0", subject, at=at, known_at=known) if f.predicate == field and (at or f.valid_to is None)]
            assert duck.value(con, "syn0", subject, field, at, known) == fs, (op, field)
    t0, t1 = sample.lo + (sample.hi - sample.lo) / 3, sample.lo + (sample.hi - sample.lo) / 2
    status = "In Progress"
    keel_keys = sorted(tools.search(kind="issue", where={"status": status}, as_of=t1, detail="key", limit=0))
    assert duck.status_at(con, "syn0", status, t1) == keel_keys
    known_keys = sorted(tools.search(kind="issue", where={"status": status}, as_of=t0, as_known_at=t1, detail="key", limit=0))
    assert duck.status_at(con, "syn0", status, t0, known_at=t1) == known_keys
    hist = tools.history(since=t0, until=t1, predicate="assignee", limit=0)
    assert duck.changed(con, "syn0", "assignee", t0, t1) == len(hist)


# ---- summary ------------------------------------------------------------------------------------------


def test_summary_renders_every_section(tmp_path):
    from evals.scale import summary

    lat = {op: {**harness.percentiles([0.001, 0.002]), "errors": 0, "skipped": 0} for op in harness.OPS}
    m = harness.machine()
    rows = [
        {"kind": "one", "label": "syn:10", "store": "memory", "phase": "ingest", "events": 10, "ingest_s": 0.1, "events_per_s": 100.0,
         "bytes_per_event_rss": 5000.0, "peak_rss_bytes": 2**30, "machine": m},
        {"kind": "one", "label": "syn:10", "store": "memory", "phase": "queries", "latency": lat, "mix_mean_ms": 1.5},
        {"kind": "one", "label": "syn:10", "store": "memory", "phase": "replay", "replay_s": 0.1, "replay_events_per_s": 100.0},
        {"kind": "one", "label": "syn:100", "store": "sqlite", "phase": "timeout", "wall_s": 1800.0},
        {"kind": "duck", "label": "syn:10", "events": 10, "load_s": 0.01, "rows": 20, "db_bytes": 1000, "latency": lat},
        {"kind": "determinism", "label": "syn:10", "same": {"facts": True}, "replay_same_facts": True,
         "hashes": {"memory/written": {"n_facts": 1, "n_relations": 1, "facts": "a" * 64, "relations": "b" * 64, "ingest_s": 0.1}}},
        {"kind": "http", "label": "syn:10", "in_process": {"context_now": lat["context_now"]}, "http": {"context_now": lat["context_now"]}},
    ]
    res = tmp_path / "r.jsonl"
    res.write_text("".join(json.dumps(r) + "\n" for r in rows))
    text = summary.write(res, tmp_path / "s.md")
    for heading in ("Ingest throughput", "Per-tool latency", "DuckDB baseline", "Determinism", "Transport", "Where it stopped"):
        assert heading in text
    assert "syn:100" in text and "timeout" in text


# ---- push-down stores: the SQLite build's rows copied into DuckDB / Postgres ------------------------------


@pytest.mark.parametrize("target", ["duckdb"] + (["postgres"] if __import__("os").environ.get("KEEL_POSTGRES_URL") else []))
def test_rows_copied_from_a_sqlite_build_answer_the_tenant_ops_alike(target, tmp_path):
    """`one --load-from`: the rows Keel's SqliteStore ingest wrote, copied as stored, give the same graph and the
    same tenant-wide answers (push-down on the target, Python path on SQLite with its capabilities hidden)."""
    pytest.importorskip("duckdb")
    log = tmp_path / "log.jsonl"
    gen.write(log, gen.generate(6000, seed=11))
    src = SqliteStore(tmp_path / "src.db")
    report = Ingestor(load_cartridge(JIRA), src).ingest(harness.read_events(log), batch=1000)
    assert not report.errors
    dst = harness._open(target, tmp_path / "dst.duckdb")
    rows = harness.copy_rows(tmp_path / "src.db", dst)
    assert rows["facts"] == src._one("SELECT COUNT(*) AS n FROM facts")["n"] > 0
    assert harness.graph_hash(dst, "syn0") == harness.graph_hash(src, "syn0")
    sample = harness.Sample.of(harness.read_events(log))
    c = load_cartridge(JIRA)
    from tests.test_pushdown import Plain

    a, b = Tools(c, dst, sample.tenant), Tools(c, Plain(src), sample.tenant)
    for op in harness.ops(sample, random.Random(1), 0, 4):
        assert harness.call(a, dst, op) == harness.call(b, src, op), op



@pytest.mark.skipif(not __import__("os").environ.get("KEEL_POSTGRES_URL"), reason="KEEL_POSTGRES_URL not set")
def test_rule_push_down_on_postgres_is_not_quadratic(tmp_path):
    """Postgres runs a correlated lookup of a CTE as a scan of it: a rule over a few thousand issues must still
    answer in a moment (and as SQLite's Python path does)."""
    import time

    log = tmp_path / "log.jsonl"
    gen.write(log, gen.generate(40000, seed=12))
    src = SqliteStore(tmp_path / "src.db")
    assert not Ingestor(load_cartridge(JIRA), src).ingest(harness.read_events(log), batch=5000).errors
    dst = harness._open("postgres", None)
    harness.copy_rows(tmp_path / "src.db", dst)
    c = load_cartridge(JIRA)
    from tests.test_pushdown import Plain

    sample = harness.Sample.of(harness.read_events(log))
    at = sample.lo + (sample.hi - sample.lo) * 0.6
    a, b = Tools(c, dst, sample.tenant), Tools(c, Plain(src), sample.tenant)
    for rule in harness.RULES:
        a.search(rule=rule, as_of=at, detail="count")  # the functions installed, the plan cached
        t0 = time.perf_counter()
        got = a.search(rule=rule, as_of=at, detail="count")
        took = time.perf_counter() - t0
        assert got == b.search(rule=rule, as_of=at, detail="count"), rule
        assert took < 3.0, (rule, took)


def test_summary_puts_the_pushed_down_reads_beside_the_python_path(tmp_path):
    from evals.scale import summary

    lat = lambda ms: {op: {**harness.percentiles([ms / 1000] * 3), "errors": 0, "skipped": 0} for op in harness.TENANT_OPS}
    rows = [
        {"kind": "one", "label": "syn:1000", "store": "sqlite", "phase": "queries", "latency": lat(9000.0), "machine": harness.machine()},
        {"kind": "one", "label": "pushdown:syn:1000", "store": "sqlite", "phase": "queries", "latency": lat(12.0)},
        {"kind": "one", "label": "pushdown:syn:1000", "store": "postgres", "phase": "load", "load_s": 3.0, "rows": {"facts": 5}},
        {"kind": "one", "label": "pushdown:syn:1000", "store": "postgres", "phase": "queries", "latency": lat(40.0)},
    ]
    res = tmp_path / "r.jsonl"
    res.write_text("".join(json.dumps(r) + "\n" for r in rows))
    text = summary.write(res, tmp_path / "s.md")
    assert "Tenant-wide reads pushed down" in text
    section = text.split("Tenant-wide reads pushed down", 1)[1]
    assert "syn:1000" in section and "9,000" in section and "12.0" in section and "40.0" in section and "PostgresStore" in section
