"""The SPEC questions the battery left open, decided on 2026-10-22, one test each, on every store.

1. Naive datetimes are UTC everywhere: the envelope, the store primitives and the tools.
2. An event whose subject is not the kind its catalog entry declares is refused at ingest.
3. close_task expires task-scope memory only; `task=` on another scope is provenance.
4. unlink has a transaction time: the relation's `closed_at`, read by as_known_at.
5. An event is atomic: a handler that fails leaves nothing behind and the event is not remembered.
6. An annotate key reused with another scope raises.
7. A text search reads facts at `as_of`.
"""

from datetime import datetime, timezone

import pytest

from keel import Event, Ingestor, Tools, load_cartridge

UTC = timezone.utc
FLEET, T, TIRE = "examples/fleet", "acme", "tire:T-1"




def d(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


def measure(eid: str, occurred: datetime, depth: float = 5.0, **kw) -> Event:
    return Event(id=eid, tenant=T, type="tire.depth_measured", subject=TIRE, occurred_at=occurred, source="b",
                 payload={"unit_id": "U1", "position": "P1", "depth_mm": depth}, **kw)


def fleet(store):
    c = load_cartridge(FLEET)
    assert not Ingestor(c, store).ingest([measure("a", d(10, 1))], strict=True).errors
    return c, Tools(c, store, T)


# 1 ----------------------------------------------------------------------------------------------


def test_naive_datetimes_are_utc_in_the_tools(store):
    _, t = fleet(store)
    assert t.history(TIRE, since=datetime(2026, 9, 1)) == t.history(TIRE, since=d(9, 1))
    assert t.context(TIRE, as_of=datetime(2026, 10, 2), detail="full")["facts"] == t.context(TIRE, as_of=d(10, 2), detail="full")["facts"]
    assert t.related(TIRE, as_of=datetime(2026, 10, 2)) == t.related(TIRE, as_of=d(10, 2))


def test_naive_datetimes_are_utc_in_the_store_primitives(store):
    store.open_fact(T, "tire:N", "depth_mm", 4.0, datetime(2026, 9, 1))
    store.close_fact(T, "tire:N", "depth_mm", valid_to=datetime(2026, 10, 1))
    store.link(T, "tire:N", "mounted_on", "unit:U", valid_from=datetime(2026, 9, 1))
    store.unlink(T, "tire:N", "mounted_on", "unit:U", valid_to=datetime(2026, 10, 1))
    (f,) = [f for f in store.facts(T, "tire:N") if f.predicate == "depth_mm"]
    (r,) = store.relations(T, "tire:N")
    assert (f.valid_from, f.valid_to, r.valid_from, r.valid_to) == (d(9, 1), d(10, 1), d(9, 1), d(10, 1))
    assert [f.value for f in store.facts(T, "tire:N", at=d(9, 15))] == [4.0]


# 2 ----------------------------------------------------------------------------------------------


def test_an_event_about_another_kind_than_its_catalog_entry_is_refused(store):
    c = load_cartridge(FLEET)
    wrong = measure("w", d(10, 1)).model_copy(update={"subject": "unit:U1"})  # tire.depth_measured is about a tire
    report = Ingestor(c, store).ingest([wrong, measure("ok", d(10, 2))])
    assert [e for e, _ in report.errors] == ["w"] and report.processed == 1
    assert "is not a 'tire'" in report.not_applied[0]
    assert store.facts(T, "unit:U1") == [] and not store.seen(T, "w")  # refused whole, so a fixed event can come again
    with pytest.raises(ValueError, match="is not a 'tire'"):
        Ingestor(c, store).ingest([wrong], strict=True)


# 3 ----------------------------------------------------------------------------------------------


def test_close_task_expires_task_memory_only(store):
    _, t = fleet(store)
    t.annotate(TIRE, "note", "shared finding", scope="tenant", task="job-1", evidence="a")
    t.annotate(TIRE, "scratch", "working note", scope="task", task="job-1")
    assert store.close_task(T, "job-1") == 1
    assert [f["value"] for f in t.history(TIRE, predicate="note")] == ["shared finding"]
    assert t.history(TIRE, predicate="scratch", task="job-1") == []


# 4 ----------------------------------------------------------------------------------------------


def test_an_unlink_counts_only_once_it_was_learned(store):
    c = load_cartridge(FLEET)
    mounted = measure("m", d(9, 1), recorded_at=d(9, 1))  # a measurement links the tire to its unit
    c.registry.register("tire.removed", lambda e, s: s.unlink(e.tenant, e.subject, "mounted_on", "unit:U1", valid_to=e.occurred_at))
    removed = Event(id="r", tenant=T, type="tire.removed", subject=TIRE, occurred_at=d(10, 1), recorded_at=d(10, 22), source="b", payload={})
    assert not Ingestor(c, store).ingest([mounted, removed]).errors
    (r,) = [r for r in store.relations(T, TIRE) if r.rel == "mounted_on"]
    assert (r.valid_to, r.closed_at) == (d(10, 1), d(10, 22))
    t = Tools(c, store, T)
    assert [x["key"] for x in t.related(TIRE, as_of=d(10, 5))] == []  # what we know now about Oct 5
    seen_then = t.context(TIRE, as_known_at=d(10, 5), detail="full")["relations"]  # what an agent saw on Oct 5
    assert [x["dst"] for x in seen_then if x["rel"] == "mounted_on"] == ["unit:U1"]
    assert [x for x in t.context(TIRE, as_known_at=d(10, 23), detail="full")["relations"] if x["rel"] == "mounted_on"] == []


# 5 ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("batch", [1, 10])
def test_a_failing_handler_leaves_nothing_behind_and_its_event_comes_again(store, batch):
    c = load_cartridge(FLEET)

    def half(e, s):
        s.open_fact(e.tenant, e.subject, "half", 1, valid_from=e.occurred_at)
        raise RuntimeError("boom")

    c.registry.register("tire.half", half)
    bad = Event(id="h", tenant=T, type="tire.half", subject=TIRE, occurred_at=d(10, 1), source="b", payload={})
    report = Ingestor(c, store).ingest([measure("a", d(9, 1)), bad, measure("b", d(10, 2), 4.0)], batch=batch)
    assert [e for e, _ in report.errors] == ["h"] and report.processed == 2
    assert sorted(f.value for f in store.facts(T, TIRE) if f.predicate == "depth_mm") == [4.0, 5.0]  # the others landed
    assert [f for f in store.facts(T, TIRE) if f.predicate == "half"] == [] and not store.seen(T, "h")
    fixed = load_cartridge(FLEET)
    fixed.registry.register("tire.half", lambda e, s: s.open_fact(e.tenant, e.subject, "half", 1, valid_from=e.occurred_at))
    assert Ingestor(fixed, store).ingest([bad]).processed == 1  # fixed, the same event applies
    assert [f.value for f in store.facts(T, TIRE) if f.predicate == "half"] == [1]


# 6 ----------------------------------------------------------------------------------------------


def test_an_annotate_key_reused_with_another_scope_raises(store):
    _, t = fleet(store)
    t.annotate(TIRE, "obs", "v", scope="agent", key="k")
    with pytest.raises(ValueError, match="idempotency key 'k'"):
        t.annotate(TIRE, "obs", "v", scope="tenant", key="k", evidence="a")
    assert t.annotate(TIRE, "obs", "v", scope="agent", key="k", detail="full")["scope"] == "agent"  # the same call still replays


# 7 ----------------------------------------------------------------------------------------------


def test_a_text_search_reads_facts_at_as_of(store):
    _, t = fleet(store)
    hits = lambda **kw: [h for h in t.search("depth", detail="full", **kw) if h.get("predicate") == "depth_mm"]
    assert hits(as_of=d(9, 15)) == []  # measured on Oct 1
    assert [h["value"] for h in hits(as_of=d(10, 15))] == [5.0]
    assert [h["value"] for h in hits()] == [5.0]
