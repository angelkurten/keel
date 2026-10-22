"""SPEC §3.3, §4, §7.6 and §9: the bitemporal store, probed on every store.

Facts are placed by valid_from whatever order events arrive in, and `as_known_at` rebuilds what Keel
knew at a transaction time from the facts recorded by then and the closes learned by then.
"""

import itertools
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from keel import Event, Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge

FLEET, SUBJECT = "examples/fleet", "tire:T-9"
UTC = timezone.utc


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return MemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "keel.db")


def d(month: int, day: int, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def cartridge():
    """Fleet, plus `tire.retired`: a handler that closes the tire's depth, as a status-ending event would."""
    c = load_cartridge(FLEET)
    c.registry.register("tire.retired", lambda e, s: s.close_fact(e.tenant, e.subject, "depth_mm", valid_to=e.occurred_at))
    return c


def measure(tenant: str, eid: str, occurred: datetime, recorded: datetime, depth: float) -> Event:
    return Event(id=eid, tenant=tenant, type="tire.depth_measured", subject=SUBJECT, occurred_at=occurred, recorded_at=recorded,
                 source="b", payload={"unit_id": "U1", "position": "P1", "depth_mm": depth})


def retire(tenant: str, eid: str, occurred: datetime, recorded: datetime) -> Event:
    return Event(id=eid, tenant=tenant, type="tire.retired", subject=SUBJECT, occurred_at=occurred, recorded_at=recorded, source="b", payload={})


def history(store, tenant: str, **kw) -> list[tuple]:
    """The depth history as stored (or as known at `known_at`): the four times of every fact."""
    facts = store.facts(tenant, SUBJECT, **kw)
    return sorted(((f.value, f.valid_from, f.valid_to, f.recorded_at, f.closed_at) for f in facts if f.predicate == "depth_mm"), key=repr)


def values_at(store, tenant: str, at: datetime, known_at: datetime | None = None) -> list[float]:
    return [f.value for f in store.facts(tenant, SUBJECT, at=at, known_at=known_at) if f.predicate == "depth_mm"]


def ingest(store, tenant: str, rows) -> None:
    """rows: (kind, eid, occurred, recorded[, depth]) in arrival order, each kind 'm' (measure) or 'x' (retire)."""
    c = cartridge()
    events = [measure(tenant, *r[1:]) if r[0] == "m" else retire(tenant, *r[1:]) for r in rows]
    assert not Ingestor(c, store).ingest(events, strict=True).errors


def oracle(rows, at: datetime, known_at: datetime) -> list[float]:
    """What a measurements-only history says at (as_of, as_known_at): of the facts recorded by then, the
    last to start by `at` in succession order (valid_from, recorded_at, event id)."""
    known = sorted((r for r in rows if r[3] <= known_at), key=lambda r: (r[2], r[3], r[1]))
    started = [r for r in known if r[2] <= at]
    return [started[-1][4]] if started else []


def random_history(rng: random.Random) -> list[tuple]:
    days = [d(9, 1), d(9, 10), d(9, 20), d(10, 1)]  # few instants: same-instant ties and corrections happen
    rows = []
    for n in range(rng.randint(3, 5)):
        valid = rng.choice(days)
        recorded = rng.choice([valid, valid + timedelta(days=rng.choice([1, 5, 15, 40]))])
        rows.append(("m", f"e{n}", valid, recorded, round(rng.uniform(1, 9), 1)))
    return rows


PROBES = [d(8, 31), d(9, 1), d(9, 5), d(9, 10), d(9, 20), d(10, 1), d(10, 15), d(12, 1)]


def test_random_histories_are_order_independent_and_match_the_oracle_at_every_probe(store):
    """§3.3/§9.9: any arrival order of a small history (ties, late corrections) builds the same stored
    history, and every (as_of, as_known_at) read answers the one value the oracle gives: never two."""
    rng = random.Random(20261022)
    for h in range(25):
        rows = random_history(rng)
        orders = [rows, rows[::-1]] + [rng.sample(rows, len(rows)) for _ in range(3)]
        stored = []
        for n, order in enumerate(orders):
            tenant = f"h{h}-o{n}"
            ingest(store, tenant, order)
            stored.append(history(store, tenant))
            for at, known in itertools.product(PROBES, PROBES):
                assert values_at(store, tenant, at, known) == oracle(rows, at, known), (order, at, known)
        assert all(s == stored[0] for s in stored), rows


def test_exhaustive_orders_of_a_tie_and_a_correction_build_one_history(store):
    """Two facts at the same instant recorded at the same time, plus a correction of the first recorded
    later: all 24 arrival orders store one history, with exactly one open fact."""
    rows = [("m", "a", d(9, 1), d(9, 1), 6.0), ("m", "b", d(9, 10), d(9, 10), 5.0), ("m", "c", d(9, 10), d(9, 10), 4.8),
            ("m", "b2", d(9, 10), d(9, 25), 5.1)]
    stored = []
    for n, order in enumerate(itertools.permutations(rows)):
        ingest(store, f"o{n}", order)
        stored.append(history(store, f"o{n}"))
        assert len(values_at(store, f"o{n}", d(9, 15))) == 1
    assert all(s == stored[0] for s in stored)
    assert [f[0] for f in stored[0] if f[2] is None] == [5.1]  # the correction, last in succession, is current


def test_as_known_at_never_shows_a_fact_before_it_was_recorded(store):
    """§7.6: a fact counts as known at t when recorded_at <= t (inclusive), and with it the close it
    caused on its predecessor, learned at the same time."""
    ingest(store, "t", [("m", "a", d(9, 1), d(9, 1), 6.0), ("m", "b", d(10, 1), d(10, 5), 4.0)])
    assert history(store, "t", known_at=d(10, 4)) == [(6.0, d(9, 1), None, d(9, 1), None)]
    assert history(store, "t", known_at=d(10, 5)) == [(4.0, d(10, 1), None, d(10, 5), None), (6.0, d(9, 1), d(10, 1), d(9, 1), d(10, 5))]
    assert history(store, "t", known_at=d(8, 1)) == []
    assert values_at(store, "t", d(10, 3), known_at=d(10, 4)) == [6.0]  # what an agent saw on Oct 4
    assert values_at(store, "t", d(10, 3)) == [4.0]  # what we know now about Oct 3


def test_an_explicit_close_counts_only_once_it_was_learned(store):
    """§7.6: a close counts when closed_at <= t; before that, the fact reads open."""
    ingest(store, "t", [("m", "a", d(9, 1), d(9, 1), 6.0), ("x", "x", d(10, 1), d(10, 2))])
    assert history(store, "t") == [(6.0, d(9, 1), d(10, 1), d(9, 1), d(10, 2))]
    assert values_at(store, "t", d(10, 3), known_at=d(10, 1, 12)) == [6.0]
    assert values_at(store, "t", d(10, 3), known_at=d(10, 2)) == []
    assert values_at(store, "t", d(9, 15), known_at=d(10, 2)) == [6.0]


def test_a_fact_after_an_explicit_close_starts_a_new_period_in_either_order(store):
    """A close then a later measurement: the closed fact keeps its close, the new one is open."""
    rows = [("m", "a", d(9, 1), d(9, 1), 6.0), ("x", "x", d(10, 1), d(10, 1)), ("m", "c", d(10, 15), d(10, 15), 3.0)]
    ingest(store, "fwd", rows)
    assert history(store, "fwd") == [(3.0, d(10, 15), None, d(10, 15), None), (6.0, d(9, 1), d(10, 1), d(9, 1), d(10, 1))]
    assert values_at(store, "fwd", d(10, 10)) == []


@pytest.mark.xfail(strict=True, reason="BUG: close_fact only touches facts open on arrival, so a late fact before the close stays open")
def test_a_close_and_a_late_fact_before_it_give_one_history_in_every_order(store):
    """§9.9: A from Sep 1, a close on Oct 1, and a late B from Sep 15. Arriving A, close, B leaves B
    open; arriving A, B, close closes B on Oct 1. Any order must store the same facts."""
    rows = [("m", "a", d(9, 1), d(9, 1), 6.0), ("x", "x", d(10, 1), d(10, 2)), ("m", "b", d(9, 15), d(10, 10), 4.0)]
    stored = []
    for n, order in enumerate(itertools.permutations(rows)):
        ingest(store, f"o{n}", order)
        stored.append(history(store, f"o{n}"))
    assert all(s == stored[0] for s in stored), stored


@pytest.mark.xfail(strict=True, reason="BUG: a close between two stored facts is dropped (no fact is open at its instant)")
def test_a_close_between_two_facts_gives_one_history_in_every_order(store):
    """§9.9: A from Sep 1, C from Oct 15, a close on Oct 1. Arriving A, close, C ends A on Oct 1;
    arriving A, C, close ends A on Oct 15 and drops the close. Any order must store the same facts."""
    rows = [("m", "a", d(9, 1), d(9, 1), 6.0), ("m", "c", d(10, 15), d(10, 15), 3.0), ("x", "x", d(10, 1), d(10, 1))]
    stored = []
    for n, order in enumerate(itertools.permutations(rows)):
        ingest(store, f"o{n}", order)
        stored.append(history(store, f"o{n}"))
    assert all(s == stored[0] for s in stored), stored


@pytest.mark.xfail(strict=True, reason="BUG: a close that arrives before the fact it closes is a no-op")
def test_a_close_arriving_before_its_fact_gives_one_history(store):
    """§9.9: a close on Oct 1 delivered before the Sep 1 measurement it ends (out-of-order adapter)."""
    rows = [("m", "a", d(9, 1), d(9, 1), 6.0), ("x", "x", d(10, 1), d(10, 1))]
    ingest(store, "fwd", rows)
    ingest(store, "rev", rows[::-1])
    assert history(store, "rev") == history(store, "fwd")


@pytest.mark.xfail(strict=True, reason="BUG: place_fact overwrites the closed_at of an explicit close, so as_known_at reopens the fact")
def test_a_late_fact_never_reopens_a_close_already_learned(store):
    """§7.6: "a late fact that later moved a close never reopens the fact before it". A is closed on
    Oct 1 (learned Oct 2); B, from Sep 15, is learned Oct 10. Asked as known on Oct 5, A had ended."""
    ingest(store, "t", [("m", "a", d(9, 1), d(9, 1), 6.0), ("x", "x", d(10, 1), d(10, 2)), ("m", "b", d(9, 15), d(10, 10), 4.0)])
    assert values_at(store, "t", d(10, 3), known_at=d(10, 5)) == []


def test_history_windows_read_the_view_known_then(store):
    """§7.2: history(since, until, as_known_at) windows the facts as known then, closes rebuilt first.
    A from Sep 1, B from Oct 15 (learned Oct 16), late C from Oct 1 (learned Oct 20)."""
    c = cartridge()
    ingest(store, "t", [("m", "a", d(9, 1), d(9, 1), 4.2), ("m", "b", d(10, 15), d(10, 16), 3.5), ("m", "c", d(10, 1), d(10, 20), 3.87)])
    t = Tools(c, store, "t")
    window = {"since": d(10, 5), "until": d(10, 12), "predicate": "depth_mm"}
    assert [f["value"] for f in t.history(SUBJECT, as_known_at=d(10, 17), **window)] == [4.2]  # A ran to Oct 15
    assert [f["value"] for f in t.history(SUBJECT, as_known_at=d(10, 21), **window)] == [3.87]  # A ended Oct 1
    assert [f["value"] for f in t.history(SUBJECT, as_known_at=d(10, 15), predicate="depth_mm")] == [4.2]
    assert [f["value"] for f in t.history(SUBJECT, predicate="depth_mm")] == [4.2, 3.87, 3.5]


def test_tenant_wide_history_known_then_ends_each_fact_at_its_successor_known_then(store):
    """§7.2/§7.6: history(since, until, as_known_at) without a subject must close a fact where its
    successor known then starts, even if that successor started outside the window. A (Oct 1), B (Oct
    15, learned Oct 16), late C (Oct 10, learned Oct 20); as known Oct 17, A ran to Oct 15."""
    c = cartridge()
    ingest(store, "t", [("m", "a", d(10, 1), d(10, 1), 4.2), ("m", "b", d(10, 15), d(10, 16), 3.5), ("m", "c", d(10, 10), d(10, 20), 3.87)])
    t = Tools(c, store, "t")
    by_subject = t.history(SUBJECT, as_known_at=d(10, 17), predicate="depth_mm")
    tenant_wide = t.history(since=d(10, 1), until=d(10, 3), as_known_at=d(10, 17), predicate="depth_mm")
    assert by_subject[0]["valid_to"] == d(10, 15).isoformat()
    assert [(f["value"], f["valid_to"]) for f in tenant_wide] == [(4.2, d(10, 15).isoformat())]


def test_relations_count_as_known_once_recorded(store):
    """§7.6: in context(as_known_at=t) a relation counts when its recorded_at <= t."""
    c = cartridge()
    ingest(store, "t", [("m", "a", d(9, 1), d(9, 5), 6.0)])  # the handler links the tire to its unit, learned Sep 5
    t = Tools(c, store, "t")
    rels = lambda known: [r["dst"] for r in t.context(SUBJECT, as_of=d(9, 3), as_known_at=known)["relations"]]
    assert rels(d(9, 4)) == []
    assert rels(d(9, 5)) == ["unit:U1"]
    assert t.context(SUBJECT, as_of=d(9, 3), as_known_at=d(9, 4))["facts"] == []


def test_replay_rebuilds_an_identical_graph_and_the_same_views(store):
    """§4/§9.4: replaying the log rebuilds the same facts, with the same four times, and every
    (as_of, as_known_at) read answers the same."""
    rng = random.Random(7)
    rows = random_history(rng) + [("x", "x", d(10, 20), d(10, 21))]
    ingest(store, "t", rows)
    c = cartridge()

    def graph():
        return sorted(repr({k: v for k, v in asdict(o).items() if k != "changed_at"}) for o in store.dump("t"))

    def views():
        return [values_at(store, "t", at, known) for at, known in itertools.product(PROBES, PROBES)]

    before, seen = graph(), views()
    events = [measure("t", *r[1:]) if r[0] == "m" else retire("t", *r[1:]) for r in rows]
    report = Ingestor(c, store).replay("t", events=events)
    assert not report.errors
    assert graph() == before and views() == seen
