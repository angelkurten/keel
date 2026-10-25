"""A long history must not make each new fact cost a read of it in the SQL stores either: one event at a time
the store asks the database for the new fact's two neighbours, and a bulk batch finds them by bisection in its
overlay. The budgets are loose and still far under what reading the whole history costs at this size."""

import itertools
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keel import Event, Ingestor, SqliteStore, load_cartridge

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
N = 2000
FLEET = Path(__file__).resolve().parent.parent / "examples" / "fleet"


def shuffled(n: int, seed: int = 7) -> list[int]:
    order = list(range(n))
    random.Random(seed).shuffle(order)
    return order


def check_history(facts: list, n: int) -> None:
    facts = sorted(facts, key=lambda f: f.valid_from)
    assert len(facts) == n
    for f, g in itertools.pairwise(facts):  # each fact ends where the next starts, learned once both were known
        assert f.valid_to == g.valid_from and f.closed_at == max(f.recorded_at, g.recorded_at)
    assert facts[-1].valid_to is None


def test_one_event_at_a_time_a_late_fact_reads_its_neighbours_not_the_history():
    s = SqliteStore()
    start = time.perf_counter()
    for i in shuffled(N):
        s.open_fact("t", "issue:1", "status", f"s{i % 5}", T0 + timedelta(hours=i),
                    recorded_at=T0 + timedelta(hours=i, minutes=5), evidence=f"e{i}")
    assert time.perf_counter() - start < 2.5  # reading the history per insert takes ~11 s here
    check_history(s.facts("t", "issue:1"), N)


def test_a_bulk_batch_places_a_long_history_by_bisection():
    s = SqliteStore()
    events = [Event(id=f"e{i}", tenant="t", type="x", subject="issue:1", occurred_at=T0, source="b", payload={}) for i in range(2)]
    start = time.perf_counter()
    with s.bulk(events):
        for i in shuffled(N):
            s.open_fact("t", "issue:1", "status", f"s{i % 5}", T0 + timedelta(hours=i),
                        recorded_at=T0 + timedelta(hours=i, minutes=5), evidence=f"e{i}")
    assert time.perf_counter() - start < 1.5  # a scan per insert takes ~5 s here
    check_history(s.facts("t", "issue:1"), N)


def test_a_relation_linked_again_by_every_event_stays_cheap_in_order():
    """A handler that links on every event (a tire's mounted_on, on each measurement) keeps each link as a mark
    of the relation's history; in order, each costs the open period, not a replay of the history."""
    events = [Event(id=f"e{i}", tenant="t", type="tire.depth_measured", subject="tire:T-9", occurred_at=T0 + timedelta(hours=i),
                    recorded_at=T0 + timedelta(hours=i, minutes=5), source="b",
                    payload={"unit_id": "U1", "position": "P1", "depth_mm": 3 + (i % 50) / 10}) for i in range(N)]
    s = SqliteStore()
    start = time.perf_counter()
    Ingestor(load_cartridge(FLEET), s).ingest(events, strict=True, batch=5000)
    assert time.perf_counter() - start < 3.0
    assert [(r.valid_from, r.valid_to) for r in s.relations("t", "tire:T-9", rel="mounted_on")] == [(T0, None)]
