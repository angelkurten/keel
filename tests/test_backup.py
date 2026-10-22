"""Backup and restore (ROADMAP, exit to beta: data that survives upgrades and moves).

`store.backup(tenant)` holds everything needed to rebuild a tenant byte for byte: entities, relations,
facts with their transaction times (close marks included, so a late fact still meets a close after a
move), the applied event ids (so a redelivered event stays a duplicate) and the retention cutoff.
`store.restore(tenant, backup)` writes it as it was into an empty tenant of any store. `keel export`
writes it as JSONL and `keel import` reads it back.
"""

from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from keel import Event, Ingestor, SqliteStore, Tools, load_cartridge
from keel.cli import app
from keel.ingest import JsonlAdapter

UTC = timezone.utc
FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"


def d(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


def cartridge():
    c = load_cartridge(FLEET)
    c.registry.register("tire.retired", lambda e, s: s.close_fact(e.tenant, e.subject, "depth_mm", valid_to=e.occurred_at))
    return c


def measure(eid: str, occurred: datetime, recorded: datetime, depth: float) -> Event:
    return Event(id=eid, tenant=TENANT, type="tire.depth_measured", subject="tire:T-9", occurred_at=occurred, recorded_at=recorded,
                 source="b", payload={"unit_id": "MXK-4821", "position": "P2", "depth_mm": depth})


def live(store):
    """A tenant with history: the fleet log, an explicit close, annotations in three scopes and a retention cutoff."""
    c = cartridge()
    assert not Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True, batch=5).errors
    retire = Event(id="x", tenant=TENANT, type="tire.retired", subject="tire:T-9", occurred_at=d(10, 1), recorded_at=d(10, 2), source="b", payload={})
    assert not Ingestor(c, store).ingest([measure("m1", d(9, 1), d(9, 1), 6.0), retire], strict=True).errors
    t = Tools(c, store, TENANT, agent=None)
    t.annotate("tire:T-9001", "assessment", "critical", scope="tenant", evidence="evt-0006")
    t.annotate("tire:T-9001", "scratch", "check again", scope="task", task="job-1")
    Tools(c, store, TENANT).annotate("unit:MXK-4821", "note", "driver called", scope="agent")
    store.forget_before(TENANT, datetime(2020, 1, 1, tzinfo=UTC))
    return c


def answers(c, store):
    t = Tools(c, store, TENANT)
    subjects = sorted({e.key for e in store.entities(TENANT)} | {"tire:T-9"})
    return {s: (t.context(s, detail="full"), t.history(s, detail="full", limit=0), t.context(s, as_known_at=d(10, 1), detail="full")) for s in subjects}


def strip_watermark(a):
    return {s: tuple({k: v for k, v in x.items() if k != "watermark"} if isinstance(x, dict) else x for x in v) for s, v in a.items()}


def test_a_restored_tenant_is_the_same_tenant(store, tmp_path):
    source = SqliteStore(tmp_path / "source.db")
    c = live(source)
    store.restore(TENANT, source.backup(TENANT))
    assert store.backup(TENANT) == source.backup(TENANT)  # every row, every transaction time, the marks, the ids, the cutoff
    assert answers(c, store) == answers(c, source)
    assert store.watermark(TENANT) == source.watermark(TENANT) and store.cutoff(TENANT) == source.cutoff(TENANT)


def test_after_a_restore_dedupe_and_close_marks_still_hold(store, tmp_path):
    source = SqliteStore(tmp_path / "source.db")
    c = live(source)
    store.restore(TENANT, source.backup(TENANT))
    assert Ingestor(c, store).ingest(JsonlAdapter(EVENTS)).duplicates == 12  # redelivered: nothing applied twice
    late = measure("m2", d(9, 15), d(10, 10), 4.0)  # before the close of Oct 1: it must end there, as in the source
    for s in (store, source):
        assert not Ingestor(c, s).ingest([late], strict=True).errors
    view = lambda s: [(f.value, f.valid_from, f.valid_to) for f in s.facts(TENANT, "tire:T-9") if f.predicate == "depth_mm"]
    assert view(store) == view(source) == [(6.0, d(9, 1), d(9, 15)), (4.0, d(9, 15), d(10, 1))]


def test_restore_refuses_a_tenant_that_has_data(store, tmp_path):
    source = SqliteStore(tmp_path / "source.db")
    live(source)
    store.upsert_entity(TENANT, "unit", "X")
    with pytest.raises(ValueError, match="not empty"):
        store.restore(TENANT, source.backup(TENANT))


def test_keel_export_then_import_moves_a_tenant(tmp_path):
    src, dst, out = str(tmp_path / "a.db"), str(tmp_path / "b.db"), tmp_path / "acme.jsonl"
    c = live(SqliteStore(src))
    res = CliRunner().invoke(app, ["export", TENANT, "--db", src, "--out", str(out)])
    assert res.exit_code == 0, res.output
    records = {line.split('"record": "', 1)[1].split('"', 1)[0] for line in out.read_text().splitlines()}
    assert {"entity", "relation", "fact", "close", "ingested", "cutoff"} <= records
    res = CliRunner().invoke(app, ["import", str(out), TENANT, "--db", dst])
    assert res.exit_code == 0 and "imported" in res.output, res.output
    assert SqliteStore(dst).backup(TENANT) == SqliteStore(src).backup(TENANT)
    assert answers(c, SqliteStore(dst)) == answers(c, SqliteStore(src))
    res = CliRunner().invoke(app, ["import", str(out), TENANT, "--db", dst])
    assert res.exit_code != 0 and "not empty" in res.output  # never merged into a tenant that has data


def test_keel_replay_check_compares_a_replay_with_the_db_and_changes_nothing(tmp_path):
    db = str(tmp_path / "keel.db")
    assert CliRunner().invoke(app, ["ingest", FLEET, EVENTS, "--db", db]).exit_code == 0
    Tools(load_cartridge(FLEET), SqliteStore(db), TENANT).annotate("tire:T-9001", "assessment", "critical", scope="tenant", evidence="evt-0006")
    res = CliRunner().invoke(app, ["replay", FLEET, EVENTS, TENANT, "--db", db, "--check"])
    assert res.exit_code == 0 and "identical" in res.output, res.output
    before = SqliteStore(db).backup(TENANT)
    SqliteStore(db).open_fact(TENANT, "tire:T-9001", "depth_mm", 1.0, valid_from=d(10, 20), source="hand-edit")  # state no event explains
    drifted = SqliteStore(db).backup(TENANT)
    res = CliRunner().invoke(app, ["replay", FLEET, EVENTS, TENANT, "--db", db, "--check"])
    assert res.exit_code == 1 and "only in the db" in res.output and "hand-edit" in res.output, res.output
    assert SqliteStore(db).backup(TENANT) == drifted != before  # a check never writes
