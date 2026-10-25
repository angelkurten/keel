"""A long history must not make each new fact cost a scan of it. A subject with years of changes (thousands of
facts in one predicate) arriving out of order used to take quadratic time in MemoryStore: every insert compared
itself with every fact of its subject. The budget below is loose (a few times the indexed time on a slow CI box)
and still far under what a scan costs at this size."""

import itertools
import random
import time
from datetime import datetime, timedelta, timezone

from keel import MemoryStore

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
N = 4000


def ingest(order: list[int]) -> MemoryStore:
    s = MemoryStore()
    for i in order:
        s.open_fact("t", "issue:1", "status", f"s{i % 5}", T0 + timedelta(hours=i),
                    recorded_at=T0 + timedelta(hours=i, minutes=5), evidence=f"e{i}")
    return s


def test_a_long_history_arriving_out_of_order_ingests_in_near_linear_time():
    order = list(range(N))
    random.Random(7).shuffle(order)
    start = time.perf_counter()
    s = ingest(order)
    assert time.perf_counter() - start < 2.0  # a scan per insert takes ~15 s here
    facts = sorted(s.facts("t", "issue:1"), key=lambda f: f.valid_from)
    assert len(facts) == N
    for f, g in itertools.pairwise(facts):  # each fact ends where the next starts, learned once both were known
        assert f.valid_to == g.valid_from and f.closed_at == max(f.recorded_at, g.recorded_at)
    assert facts[-1].valid_to is None


def test_the_indexed_history_answers_like_the_in_order_one_at_every_instant():
    shuffled = list(range(300))
    random.Random(3).shuffle(shuffled)
    a, b = ingest(list(range(300))), ingest(shuffled)
    for h in range(0, 300, 7):
        at, known = T0 + timedelta(hours=h, minutes=30), T0 + timedelta(hours=h + 40)
        va = [f.value for f in a.facts("t", "issue:1", at=at, known_at=known)]
        vb = [f.value for f in b.facts("t", "issue:1", at=at, known_at=known)]
        assert va == vb and len(va) == 1
