"""SPEC §2 (envelope), §4 (ingestion) and §5.3/§5.5 (catalog checks): ingest, dedupe, replay, provenance, bulk.

Every test that touches a store runs on each store of the conformance fixture (memory, sqlite, and
mysql or an external one when configured), like tests/test_store.py.
"""

import json
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from keel import Event, Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.cli import app
from keel.handlers import UnhandledEvent
from keel.ingest import JsonlAdapter
from keel.tools import _ser

UTC = timezone.utc
FLEET, FLEET_EVENTS, FLEET_TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"
EXAMPLES = ("fleet", "crm", "finance", "clinics", "relay")
TRANSACTION_TIME = ("recorded_at", "closed_at", "changed_at")  # differ between runs unless the source declares them





def _graph(store, tenant, drop=TRANSACTION_TIME):
    """Everything stored for a tenant, order-free, without the fields named in `drop`."""
    rows = []
    for o in store.dump(tenant):
        d = {k: v for k, v in _ser(asdict(o)).items() if k not in drop}
        rows.append(json.dumps({"type": type(o).__name__, **d}, sort_keys=True, default=str))
    return sorted(rows)


def _facts(store, tenant):
    return [o for o in store.dump(tenant) if type(o).__name__ == "Fact"]


def _ev(**kw):
    base = {"tenant": "t1", "type": "tire.depth_measured", "subject": "tire:T-1", "occurred_at": "2026-09-10T08:00:00Z",
            "source": "test", "payload": {"unit_id": "U-1", "position": "FL", "depth_mm": 5.0}}
    return Event.model_validate({**base, **kw})


def _log(path):
    return list(JsonlAdapter(path))


# ---- §2 envelope -------------------------------------------------------------------------


def test_naive_datetimes_never_reach_a_handler():
    """§2: a naive datetime is rejected or coerced to UTC; whatever passes the envelope is tz-aware."""
    for field in ("occurred_at", "recorded_at"):
        try:
            ev = _ev(**{field: "2026-09-10T08:00:00"})
        except ValidationError:
            continue
        assert getattr(ev, field).utcoffset() == timedelta(0)
    try:
        ev = _ev(occurred_at=datetime(2026, 9, 10, 8))
    except ValidationError:
        return
    assert ev.occurred_at.tzinfo is not None


def test_offset_datetimes_keep_their_instant():
    """§2: an aware datetime with an offset is the same instant once in the envelope."""
    ev = _ev(occurred_at="2026-09-10T10:00:00+02:00")
    assert ev.occurred_at == datetime(2026, 9, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize("missing", ["tenant", "type", "subject", "occurred_at", "source"])
def test_required_fields(missing):
    """§2: tenant, type, subject, occurred_at and source are required."""
    raw = {"tenant": "t1", "type": "x.y", "subject": "a:1", "occurred_at": "2026-09-10T08:00:00Z", "source": "s"}
    del raw[missing]
    with pytest.raises(ValidationError):
        Event.model_validate(raw)


def test_optional_fields_default():
    """§2: id, recorded_at and payload are optional: a hash id, now (UTC), an empty object."""
    before = datetime.now(UTC)
    ev = Event.model_validate({"tenant": "t1", "type": "x.y", "subject": "a:1", "occurred_at": "2026-09-10T08:00:00Z", "source": "s"})
    assert ev.id and ev.payload == {}
    assert before <= ev.recorded_at <= datetime.now(UTC)
    assert "recorded_at" not in ev.model_fields_set


def test_subject_must_be_kind_colon_id():
    """§2: subject is `<kind>:<id>`; one without a kind separator is rejected."""
    with pytest.raises(ValidationError):
        _ev(subject="T-1")
    ev = _ev(subject="tire:T-1:left")
    assert (ev.subject_kind, ev.subject_id) == ("tire", "T-1:left")


def test_global_tenant_is_reserved():
    """§6/§2: events never write the global partition."""
    with pytest.raises(ValidationError):
        _ev(tenant="__global__")


def test_content_hash_id_is_stable_and_ignores_recorded_at():
    """§2: without an id, the id is a hash of tenant, type, subject, occurred_at, source and payload."""
    a = _ev()
    assert a.id == _ev().id == _ev(recorded_at="2026-10-01T00:00:00Z").id
    for change in ({"tenant": "t2"}, {"subject": "tire:T-2"}, {"occurred_at": "2026-09-11T08:00:00Z"}, {"source": "other"},
                   {"payload": {"unit_id": "U-1", "position": "FL", "depth_mm": 4.0}}):
        assert _ev(**change).id != a.id, change
    assert _ev(id="src-1").id == "src-1"


def test_jsonl_adapter_skips_blank_and_comment_lines(tmp_path):
    """§2: the .jsonl adapter reads one envelope per line, ignoring blank lines and lines starting with #."""
    p = tmp_path / "e.jsonl"
    p.write_text("# header\n\n" + _ev().model_dump_json() + "\n   \n# trailer\n", encoding="utf-8")
    assert [e.id for e in JsonlAdapter(p)] == [_ev().id]


def test_declared_recorded_at_is_the_transaction_time_of_what_the_handler_writes(store):
    """§2/§3.3: when the event declares recorded_at, its facts and relations carry it."""
    c = load_cartridge(FLEET)
    at = datetime(2026, 9, 12, tzinfo=UTC)
    Ingestor(c, store).ingest([_ev(recorded_at=at.isoformat())], strict=True)
    (f,) = store.facts("t1", "tire:T-1")
    assert f.recorded_at == at
    (r,) = store.relations("t1", "tire:T-1", rel="mounted_on")
    assert r.recorded_at == at


def test_undeclared_recorded_at_is_the_ingest_clock(store):
    """§2: without a declared recorded_at, facts carry the time they are ingested."""
    c = load_cartridge(FLEET)
    before = datetime.now(UTC)
    Ingestor(c, store).ingest([_ev()], strict=True)
    (f,) = store.facts("t1", "tire:T-1")
    assert before - timedelta(seconds=1) <= f.recorded_at <= datetime.now(UTC) + timedelta(seconds=1)


def test_old_declared_recorded_at_is_still_a_new_change(store):
    """§3.3: changed_at is the ingest clock; an event declaring an old recorded_at is still a new change."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    ing.ingest([_ev()], strict=True)
    mark = store.watermark("t1")
    ing.ingest([_ev(occurred_at="2026-09-20T08:00:00Z", recorded_at="2020-01-01T00:00:00Z",
                    payload={"unit_id": "U-1", "position": "FL", "depth_mm": 3.0})], strict=True)
    assert store.watermark("t1") > mark
    assert 3.0 in [f.value for f in store.changes("t1", mark)]


def test_handlers_use_occurred_at_as_valid_from(store):
    """§2: valid_from is occurred_at, never recorded_at."""
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest([_ev(recorded_at="2026-10-01T00:00:00Z")], strict=True)
    (f,) = store.facts("t1", "tire:T-1")
    assert f.valid_from == datetime(2026, 9, 10, 8, tzinfo=UTC)


# ---- §4 duplicates -----------------------------------------------------------------------


def test_same_event_twice_applies_once(store):
    """§4: an event id already applied for the tenant is skipped and counted as a duplicate."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    r1 = ing.ingest([_ev()])
    r2 = ing.ingest([_ev()])
    assert (r1.processed, r1.duplicates) == (1, 0)
    assert (r2.processed, r2.duplicates) == (0, 1) and r2.not_applied == {0: "duplicate"}
    assert len(store.facts("t1", "tire:T-1")) == 1


def test_duplicate_inside_one_call_applies_once(store):
    """§4: a retry delivered in the same call (at-least-once) is also a duplicate, in a batch too."""
    c = load_cartridge(FLEET)
    rep = Ingestor(c, store).ingest([_ev(), _ev(), _ev()], batch=10)
    assert (rep.processed, rep.duplicates) == (1, 2)
    assert len(store.facts("t1", "tire:T-1")) == 1


def test_reingesting_a_whole_log_changes_nothing(store):
    """§4: re-ingesting the whole log is all duplicates and leaves the graph and its watermark untouched."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    log = _log(FLEET_EVENTS)
    ing.ingest(log, strict=True)
    before, mark = _graph(store, FLEET_TENANT, drop=()), store.watermark(FLEET_TENANT)
    rep = Ingestor(c, store).ingest(_log(FLEET_EVENTS), strict=True, batch=5000)
    assert (rep.processed, rep.duplicates) == (0, len(log))
    assert _graph(store, FLEET_TENANT, drop=()) == before
    assert store.watermark(FLEET_TENANT) == mark
    assert store.changes(FLEET_TENANT, mark) == []


def test_dedupe_is_per_tenant(store):
    """§4: duplicates are by id *for that tenant*; the same source id in another tenant is applied."""
    c = load_cartridge(FLEET)
    rep = Ingestor(c, store).ingest([_ev(id="e-1", tenant="t1"), _ev(id="e-1", tenant="t2")], strict=True)
    assert (rep.processed, rep.duplicates) == (2, 0)
    assert len(store.facts("t1", "tire:T-1")) == len(store.facts("t2", "tire:T-1")) == 1


# ---- §4 determinism and replay -----------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
def test_same_log_same_graph_in_two_stores(store, name):
    """§4: dispatch is deterministic; the same log in the same order yields an identical graph."""
    c = load_cartridge(f"examples/{name}")
    log = _log(f"examples/{name}/events.jsonl")
    ref = MemoryStore()
    for s in (ref, store):
        rep = Ingestor(c, s).ingest(log, strict=True)
        assert not rep.errors and not rep.unhandled
    for tenant in sorted({e.tenant for e in log})[:5]:
        assert _graph(store, tenant) == _graph(ref, tenant)


@pytest.mark.parametrize("name", EXAMPLES)
def test_replay_rebuilds_an_identical_graph(store, name):
    """§4/rule 1: replay(tenant, events=log) wipes the event-derived subgraph and rebuilds the same one."""
    c = load_cartridge(f"examples/{name}")
    log = _log(f"examples/{name}/events.jsonl")
    ing = Ingestor(c, store)
    ing.ingest(log, strict=True)
    tenant = log[0].tenant
    before = _graph(store, tenant)
    rep = ing.replay(tenant, events=log)
    assert not rep.errors and rep.duplicates == 0
    assert rep.processed == sum(1 for e in log if e.tenant == tenant)
    assert _graph(store, tenant) == before


def test_replay_with_declared_recorded_at_rebuilds_transaction_time_too(store):
    """§2/rule 3: when the source declares recorded_at, replay rebuilds recorded_at and closed_at as well."""
    c = load_cartridge(FLEET)
    log = [e.model_copy(update={"recorded_at": e.occurred_at + timedelta(minutes=5)}) for e in _log(FLEET_EVENTS)]
    log = [Event.model_validate({**e.model_dump(mode="json")}) for e in log]  # recorded_at declared, as from the wire
    assert all("recorded_at" in e.model_fields_set for e in log)
    ing = Ingestor(c, store)
    ing.ingest(log, strict=True)
    keep = ("changed_at",)
    before = _graph(store, FLEET_TENANT, drop=keep)
    ing.replay(FLEET_TENANT, events=log)
    assert _graph(store, FLEET_TENANT, drop=keep) == before


def test_replay_from_the_kept_log_and_without_a_log(store):
    """§4: replay without events uses the in-memory log (keep_log=True); without either it is an error."""
    c = load_cartridge(FLEET)
    with pytest.raises(ValueError):
        Ingestor(c, store).replay(FLEET_TENANT)
    ing = Ingestor(c, store, keep_log=True)
    ing.ingest(_log(FLEET_EVENTS), strict=True)
    before = _graph(store, FLEET_TENANT)
    rep = ing.replay(FLEET_TENANT)
    assert rep.processed == len(_log(FLEET_EVENTS)) and _graph(store, FLEET_TENANT) == before


def test_replay_drops_state_not_in_the_log(store):
    """Rule 1: the graph is derived; a fact written outside the log is gone after replay."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    log = _log(FLEET_EVENTS)
    ing.ingest(log, strict=True)
    before = _graph(store, FLEET_TENANT)
    store.open_fact(FLEET_TENANT, "tire:T-9001", "stray", 1, valid_from=datetime(2026, 9, 1, tzinfo=UTC), source="oops")
    ing.replay(FLEET_TENANT, events=log)
    assert _graph(store, FLEET_TENANT) == before


def test_replay_touches_only_its_tenant(store):
    """Rule 8: replaying one tenant leaves every other tenant as it was."""
    c = load_cartridge("examples/relay")
    log = _log("examples/relay/events.jsonl")
    ing = Ingestor(c, store)
    ing.ingest(log, strict=True, batch=5000)
    other = "globex"
    before = _graph(store, other, drop=())
    ing.replay("acme", events=log)
    assert _graph(store, other, drop=()) == before


def test_annotations_survive_replay_and_the_graph_is_rebuilt_around_them(store):
    """Rule 1 exception: annotations are kept by replay (closed history included); events land around them."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    log = _log(FLEET_EVENTS)
    ing.ingest(log, strict=True)
    t = Tools(c, store, FLEET_TENANT)
    t.annotate("tire:T-9001", "note", "first", scope="tenant", evidence="photo://1", by="ops")
    t.annotate("tire:T-9001", "note", "second", scope="tenant", evidence="photo://2", by="ops")
    notes = sorted((f.value, f.valid_to is None) for f in _facts(store, FLEET_TENANT) if f.predicate == "note")
    assert notes == [("first", False), ("second", True)]
    events_before = [r for r in _graph(store, FLEET_TENANT) if '"annotate:' not in r]
    ing.replay(FLEET_TENANT, events=log)
    after = sorted((f.value, f.valid_to is None) for f in _facts(store, FLEET_TENANT) if f.predicate == "note")
    assert after == notes
    assert [r for r in _graph(store, FLEET_TENANT) if '"annotate:' not in r] == events_before
    assert [f.value for f in store.facts(FLEET_TENANT, "tire:T-9001") if f.predicate == "depth_mm" and f.valid_to is None] == [2.6]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fact_history_does_not_depend_on_arrival_order(store, seed):
    """§4 ordering: open_fact places by valid_from, so a shuffled log yields the same facts."""
    c = load_cartridge(FLEET)
    log = _log(FLEET_EVENTS)
    shuffled = log[:]
    random.Random(seed).shuffle(shuffled)
    ref = MemoryStore()
    Ingestor(c, ref).ingest(log, strict=True)
    Ingestor(c, store).ingest(shuffled, strict=True)

    def facts(s):
        return [r for r in _graph(s, FLEET_TENANT) if '"type": "Fact"' in r]

    assert facts(store) == facts(ref)


# ---- §4 unknown types, handler errors, strict mode ---------------------------------------


def test_unhandled_event_is_reported_and_raises_in_strict(store):
    """§4: an event with no handler is reported as unhandled and the stream goes on; strict raises."""
    c = load_cartridge(FLEET)
    stray = _ev(type="unit.scrapped", subject="unit:U-1", payload={})
    rep = Ingestor(c, store).ingest([stray, _ev()])
    assert rep.unhandled == ["unit.scrapped"] and rep.processed == 1
    assert rep.not_applied[0].startswith("unhandled")
    with pytest.raises(UnhandledEvent):
        Ingestor(c, store).ingest([_ev(id="u-2", type="unit.scrapped", subject="unit:U-1")], strict=True)


def test_handler_error_is_reported_with_the_event_id_and_does_not_stop_the_stream(store):
    """§4: a handler exception is reported with the event id and the stream continues."""
    c = load_cartridge(FLEET)
    bad = _ev(id="bad-1", payload={"position": "FL"})  # no unit_id: the handler raises KeyError
    rep = Ingestor(c, store).ingest([bad, _ev(subject="tire:T-2")])
    assert [eid for eid, _ in rep.errors] == ["bad-1"] and "KeyError" in rep.errors[0][1]
    assert rep.processed == 1 and rep.not_applied[0].startswith("error")
    assert len(store.facts("t1", "tire:T-2")) == 1


def test_strict_handler_error_raises(store):
    """§4: in strict mode a handler exception raises."""
    c = load_cartridge(FLEET)
    with pytest.raises(KeyError):
        Ingestor(c, store).ingest([_ev(payload={"position": "FL"})], strict=True)




@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_strict_failure_is_redelivered_whole(kind, tmp_path):
    """§4: a chunk that raises in strict mode is rolled back whole and redelivered whole: the redelivery
    ends in the same graph as a clean ingest, on every store that offers transaction()."""
    store = MemoryStore() if kind == "memory" else SqliteStore(tmp_path / "k.db")
    assert callable(getattr(store, "transaction", None))
    c = load_cartridge(FLEET)
    good = _log(FLEET_EVENTS)
    bad = _ev(id="bad-1", tenant=FLEET_TENANT, payload={"position": "FL"})
    ing = Ingestor(c, store)
    with pytest.raises(KeyError):
        ing.ingest(good[:4] + [bad], strict=True, batch=10)
    assert not store.seen(FLEET_TENANT, bad.id)
    rep = ing.ingest(good, strict=True, batch=10)
    assert rep.errors == []
    ref = MemoryStore()
    Ingestor(c, ref).ingest(good, strict=True)
    assert _graph(store, FLEET_TENANT) == _graph(ref, FLEET_TENANT)


def test_sql_strict_chunk_is_rolled_back_whole(tmp_path):
    """§4 transactions: on a store with transactions, a strict chunk that raises leaves nothing behind."""
    c = load_cartridge(FLEET)
    s = SqliteStore(tmp_path / "k.db")
    good = _log(FLEET_EVENTS)
    with pytest.raises(KeyError):
        Ingestor(c, s).ingest(good[:4] + [_ev(tenant=FLEET_TENANT, payload={})], strict=True, batch=10)
    assert list(s.dump(FLEET_TENANT)) == []
    assert not any(s.seen(FLEET_TENANT, e.id) for e in good[:4])


# ---- §5.3 / §5.5 catalog checks ---------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_streams_match_their_catalog(name):
    """§5.3: every example stream passes check_event (types, subject kinds, payload shape)."""
    c = load_cartridge(f"examples/{name}")
    problems = [(e.id, p) for e in _log(f"examples/{name}/events.jsonl") for p in c.check_event(e)]
    assert problems == []


def test_check_event_flags_drift():
    """§5.3/§5.5: unknown type, wrong subject kind, missing field and wrong shape are each reported."""
    c = load_cartridge(FLEET)
    assert c.check_event(_ev()) == []
    assert any("unknown event type" in p for p in c.check_event(_ev(type="tire.exploded")))
    assert any("subject" in p for p in c.check_event(_ev(subject="unit:T-1")))
    missing = c.check_event(_ev(payload={"position": "FL", "depth_mm": 5.0}))
    assert len(missing) == 1 and "unit_id" in missing[0]
    wrong = c.check_event(_ev(payload={"unit_id": "U-1", "position": "FL", "depth_mm": "deep"}))
    assert len(wrong) == 1 and "depth_mm" in wrong[0]


def test_check_event_allows_extra_fields_and_optional_ones():
    """§5.5: extra payload fields are allowed; `| null` makes a field optional and nullable."""
    c = load_cartridge(FLEET)
    assert c.check_event(_ev(payload={"unit_id": "U-1", "position": "FL", "depth_mm": 5, "extra": [1, 2]})) == []
    opened = {"type": "ticket.opened", "subject": "unit:U-1"}
    assert c.check_event(_ev(**opened, payload={"id": "TK-1", "kind": "tire", "urgency": "high"})) == []
    assert c.check_event(_ev(**opened, payload={"id": "TK-1", "kind": "tire", "urgency": "high", "inspection_id": None})) == []
    assert c.check_event(_ev(**opened, payload={"id": "TK-1", "kind": "tire", "urgency": "high", "inspection_id": 7})) != []


def test_check_event_is_structural_not_semantic():
    """§5.3: check_event looks at keys and types, never at values."""
    c = load_cartridge(FLEET)
    assert c.check_event(_ev(payload={"unit_id": "", "position": "nowhere", "depth_mm": -999.0})) == []


def test_cli_validate_events_passes_on_examples():
    """§5.3: `keel validate <cartridge> --events <stream>` exits 0 when the stream fits the catalog."""
    for name in ("fleet", "crm", "finance", "clinics"):
        res = CliRunner().invoke(app, ["validate", f"examples/{name}", "--events", f"examples/{name}/events.jsonl"])
        assert res.exit_code == 0, res.output
        assert "match the catalog" in res.output


def test_cli_validate_events_reports_drift_by_line(tmp_path):
    """§5.3: a drifted stream fails validation, each problem with its line number; a bad envelope is a problem too."""
    p = tmp_path / "drift.jsonl"
    lines = [
        "# comment",
        _ev().model_dump_json(),
        _ev(type="tire.exploded").model_dump_json(),
        json.dumps({"tenant": "t1", "type": "tire.depth_measured", "subject": "T-1", "occurred_at": "2026-09-10T08:00:00Z", "source": "s"}),
        _ev(payload={"position": "FL", "depth_mm": 1.0}).model_dump_json(),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["validate", FLEET, "--events", str(p)])
    assert res.exit_code == 1
    assert "line 3:" in res.output and "unknown event type" in res.output
    assert "line 4: bad envelope" in res.output
    assert "line 5:" in res.output and "unit_id" in res.output
    assert "line 2:" not in res.output


def test_cli_ingest_reports_counts_and_strict_fails(tmp_path):
    """§4: `keel ingest` reports what it applied; with --strict an unhandled event fails the command."""
    res = CliRunner().invoke(app, ["ingest", FLEET, FLEET_EVENTS])
    assert res.exit_code == 0, res.output
    assert f"processed={len(_log(FLEET_EVENTS))} duplicates=0 unhandled=0 errors=0" in res.output
    p = tmp_path / "stray.jsonl"
    p.write_text(_ev(type="unit.scrapped", subject="unit:U-1").model_dump_json() + "\n", encoding="utf-8")
    loose = CliRunner().invoke(app, ["ingest", FLEET, str(p)])
    assert loose.exit_code == 0 and "unhandled=1" in loose.output
    assert CliRunner().invoke(app, ["ingest", FLEET, str(p), "--strict"]).exit_code != 0


# ---- rule 4 provenance ------------------------------------------------------------------




@pytest.mark.parametrize("name", EXAMPLES)
def test_every_fact_from_ingest_carries_source_and_evidence(store, name):
    """Rule 4: after ingest, every fact has a source and an evidence pointer; event facts point at their event."""
    c = load_cartridge(f"examples/{name}")
    log = _log(f"examples/{name}/events.jsonl")
    Ingestor(c, store).ingest(log, strict=True, batch=5000)
    ids = {e.id for e in log}
    for tenant in sorted({e.tenant for e in log})[:5]:
        facts = _facts(store, tenant)
        assert facts
        for f in facts:
            assert f.source, (f.subject, f.predicate)
            assert f.evidence, (f.subject, f.predicate)
        assert any(f.evidence in ids for f in facts)


def test_evidence_is_the_event_id(store):
    """Rule 4 / §2: the event id is used as evidence on derived facts, the source id or the content hash."""
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest([_ev(id="src-77"), _ev(subject="tire:T-2")], strict=True)
    assert [f.evidence for f in store.facts("t1", "tire:T-1")] == ["src-77"]
    assert [f.evidence for f in store.facts("t1", "tire:T-2")] == [_ev(subject="tire:T-2").id]
    assert store.facts("t1", "tire:T-1")[0].source == "test"


# ---- §4 batches -------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
def test_bulk_ingest_matches_event_by_event(store, name):
    """§4 batches: a chunk through bulk() yields the same graph as event by event."""
    c = load_cartridge(f"examples/{name}")
    log = _log(f"examples/{name}/events.jsonl")
    ref = MemoryStore()
    Ingestor(c, ref).ingest(log, strict=True, batch=1)
    Ingestor(c, store).ingest(log, strict=True, batch=5000)
    for tenant in sorted({e.tenant for e in log})[:8]:
        assert _graph(store, tenant) == _graph(ref, tenant), tenant


def test_bulk_matches_event_by_event_on_the_same_store_kind(tmp_path):
    """§4 batches: on a SQL store, batch=5000 and batch=1 write the same rows."""
    c = load_cartridge("examples/relay")
    log = _log("examples/relay/events.jsonl")
    a, b = SqliteStore(tmp_path / "a.db"), SqliteStore(tmp_path / "b.db")
    Ingestor(c, a).ingest(log, strict=True, batch=1)
    Ingestor(c, b).ingest(log, strict=True, batch=5000)
    for tenant in sorted({e.tenant for e in log}):
        assert _graph(a, tenant) == _graph(b, tenant), tenant


def test_bulk_with_late_and_repeated_events(store):
    """§4: inside one bulk chunk, a late event lands in its slot and a repeated one is applied once."""
    c = load_cartridge(FLEET)
    d = lambda day, mm: _ev(occurred_at=f"2026-09-{day:02d}T08:00:00Z", payload={"unit_id": "U-1", "position": "FL", "depth_mm": mm})
    chunk = [d(10, 5.0), d(20, 3.0), d(15, 4.0), d(20, 3.0)]
    rep = Ingestor(c, store).ingest(chunk, strict=True, batch=10)
    assert (rep.processed, rep.duplicates) == (3, 1)
    hist = sorted(store.facts("t1", "tire:T-1"), key=lambda f: f.valid_from)
    assert [(f.value, f.valid_to) for f in hist] == [
        (5.0, datetime(2026, 9, 15, 8, tzinfo=UTC)),
        (4.0, datetime(2026, 9, 20, 8, tzinfo=UTC)),
        (3.0, None),
    ]
