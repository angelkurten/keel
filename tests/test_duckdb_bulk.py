"""DuckdbStore's bulk path: a batch of events is written in a few column-wise statements, not row by row, and
builds the same graph SqliteStore builds from the same log (and DuckdbStore itself, one event at a time):
entities, relations with their marks, facts with late arrivals, corrections and close marks."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from evals.scale import gen, harness
from keel import Ingestor, SqliteStore, load_cartridge

pytest.importorskip("duckdb")
from keel.store.duckdb import DuckdbStore

JIRA = Path(__file__).resolve().parent.parent / "evals" / "jira"


def _build(store, events, batch):
    report = Ingestor(load_cartridge(JIRA), store).ingest(iter(events), batch=batch)
    assert not report.errors
    return store


def test_bulk_builds_the_graph_sqlite_builds_and_its_own_event_by_event(tmp_path):
    events = list(harness.read_events(_log(tmp_path, 4000, seed=21)))
    ref = harness.graph_hash(_build(SqliteStore(tmp_path / "s.db"), events, 1000), "syn0", rows=True)
    bulk = harness.graph_hash(_build(DuckdbStore(tmp_path / "b.duckdb"), events, 1000), "syn0", rows=True)
    one = harness.graph_hash(_build(DuckdbStore(tmp_path / "o.duckdb"), events[:1500], 1), "syn0")
    assert ref["n_facts"] > 4000 and ref["n_relations"] > 0
    assert bulk == ref
    assert _order(DuckdbStore(tmp_path / "b.duckdb")) == _order(SqliteStore(tmp_path / "s.db"))  # seq follows the batch's order
    assert one == harness.graph_hash(_build(SqliteStore(tmp_path / "s1.db"), events[:1500], 1000), "syn0")


def test_a_second_batch_over_the_same_subjects_closes_and_marks_what_the_first_wrote(tmp_path):
    """Batches of 50: most events land on subjects an earlier batch wrote, so closes and relation marks
    update saved rows (the UPDATE path), and redelivered ids are skipped."""
    events = list(harness.read_events(_log(tmp_path, 1500, seed=5)))
    a = _build(DuckdbStore(tmp_path / "a.duckdb"), events + events[:300], 50)
    b = _build(SqliteStore(tmp_path / "b.db"), events, 50)
    assert harness.graph_hash(a, "syn0", rows=True) == harness.graph_hash(b, "syn0", rows=True)


def test_a_restore_writes_every_row_in_its_order(tmp_path):
    """restore goes through the same multi-row writes: entities keep their order too (DuckDB's ON CONFLICT does not)."""
    events = list(harness.read_events(_log(tmp_path, 1500, seed=8)))
    src = _build(SqliteStore(tmp_path / "src.db"), events, 500)
    dst = DuckdbStore(tmp_path / "dst.duckdb")
    dst.restore("syn0", src.backup("syn0"))
    assert dst.backup("syn0") == src.backup("syn0")


def test_bulk_ingest_is_within_a_small_factor_of_sqlite(tmp_path):
    """Row-by-row executemany made DuckDB ~12x slower than SQLite on the same batch (~900 vs ~11,000 events/s)."""
    import importlib.util

    import duckdb

    if int(duckdb.__version__.split(".")[0]) < 2 and importlib.util.find_spec("pandas") is None:
        pytest.skip("DuckDB 1.x imports pandas per value when it is missing (~40x slower): see DuckdbStore._execmany")
    events = list(harness.read_events(_log(tmp_path, 6000, seed=3)))
    times = {}
    for name, store in (("sqlite", SqliteStore(tmp_path / "t.db")), ("duckdb", DuckdbStore(tmp_path / "t.duckdb"))):
        t0 = time.perf_counter()
        _build(store, events, 5000)
        times[name] = time.perf_counter() - t0
    assert times["duckdb"] < 3 * times["sqlite"], times


def _order(store) -> list[tuple]:
    """Facts and relations in seq order (what every read sorted by seq sees), without the ingest clock."""
    return [(type(x).__name__, getattr(x, "subject", None) or getattr(x, "src", None), getattr(x, "predicate", None) or getattr(x, "rel", None),
             x.valid_from, x.valid_to) for x in store.dump("syn0") if not hasattr(x, "kind")]


def _log(tmp_path: Path, n: int, seed: int) -> Path:
    path = tmp_path / f"log-{n}-{seed}.jsonl"
    gen.write(path, gen.generate(n, seed=seed))
    return path
