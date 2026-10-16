import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge
from keel.cli import app
from keel.ingest import JsonlAdapter

FLEET = "examples/fleet"
CRM = "examples/crm"


def _run(cart, events):
    c = load_cartridge(cart)
    s = MemoryStore()
    ing = Ingestor(c, s, keep_log=True)
    report = ing.ingest(JsonlAdapter(events), strict=True)
    return c, s, ing, report


def test_facts_are_bitemporal_not_overwritten():
    c, s, _, _ = _run(FLEET, "examples/fleet/events.jsonl")
    t = Tools(c, s, "acme-logistics")
    hist = t.history("tire:T-9001")
    depths = [f for f in hist if f["predicate"] == "depth_mm"]
    assert [d["value"] for d in depths] == [5.2, 2.6]
    assert depths[0]["valid_to"] is not None and depths[1]["valid_to"] is None
    at_sep = s.facts("acme-logistics", "tire:T-9001", at=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert [f.value for f in at_sep if f.predicate == "depth_mm"] == [5.2]


def test_annotate_scope_policy():
    c, s, _, _ = _run(FLEET, "examples/fleet/events.jsonl")
    t = Tools(c, s, "acme-logistics")
    with pytest.raises(PermissionError):
        t.annotate("tire:T-9001", "assessment", "critical", scope="tenant")  # no evidence
    f = t.annotate("tire:T-9001", "assessment", "critical", scope="task", by="agent:x")
    assert f["expires_at"] is not None  # task memory expires
    g = t.annotate("tire:T-9001", "assessment", "critical", scope="tenant", evidence="ev-1", by="human:angel")
    assert g["expires_at"] is None and g["corrected_by"] == "human:angel"


def test_replay_rebuilds_tenant():
    c, s, ing, _ = _run(FLEET, "examples/fleet/events.jsonl")
    before = len(Tools(c, s, "acme-logistics").context("unit:MXK-4821")["relations"])
    ing.replay("acme-logistics")
    after = len(Tools(c, s, "acme-logistics").context("unit:MXK-4821")["relations"])
    assert before == after


UTC = timezone.utc


def test_history_filters_by_predicate_and_scope():
    c, s, _, _ = _run(FLEET, "examples/fleet/events.jsonl")
    t = Tools(c, s, "acme-logistics")
    t.annotate("tire:T-9001", "assessment", "critical", scope="task", by="agent:x")
    assert len(t.history("tire:T-9001")) == 3
    assert [f["value"] for f in t.history("tire:T-9001", predicate="depth_mm")] == [5.2, 2.6]
    assert [f["predicate"] for f in t.history("tire:T-9001", scope="task")] == ["assessment"]
    assert {f["scope"] for f in t.history("tire:T-9001", scope="tenant")} == {"tenant"}


def test_validate_rejects_unknown_when_operators():
    c = load_cartridge(FLEET)
    c.rules["odd"] = {"when": {"predicate": "x", "between": [1, 2]}}
    assert any("unknown operator 'between'" in p for p in c.validate())


def test_validate_events_reports_unknown_types_and_bad_payloads(tmp_path):
    good = {
        "tenant": "t", "type": "unit.registered", "subject": "unit:U1", "occurred_at": "2026-09-01T00:00:00Z",
        "source": "s", "payload": {"id": "U1", "plate": "U1", "model": "X"},
    }
    lines = [
        good,
        {**good, "type": "unit.painted"},
        {**good, "type": "tire.depth_measured", "payload": {"unit_id": "U1", "position": "P1"}},
        {**good, "type": "inspection.created", "payload": {"id": "I1", "checklist": "c", "damages": "two", "photos": 0}},
        {**good, "subject": "no-colon"},
    ]
    stream = tmp_path / "bad.jsonl"
    stream.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    res = CliRunner().invoke(app, ["validate", FLEET, "--events", str(stream)])
    assert res.exit_code == 1
    for expected in (
        "line 2: unknown event type 'unit.painted'",
        "line 3: 'tire.depth_measured' subject 'unit:U1' is not a 'tire'",
        "line 3: 'tire.depth_measured' payload is missing 'depth_mm' (number)",
        "line 4: 'inspection.created' payload 'damages' should be int, got str",
        "line 5: bad envelope: subject",
    ):
        assert expected in res.output, res.output
    ok = CliRunner().invoke(app, ["validate", FLEET, "--events", "examples/fleet/events.jsonl"])
    assert ok.exit_code == 0 and "12 events match the catalog" in ok.output


def test_event_ids_are_content_hashes_when_the_source_has_none():
    a = [e.id for e in JsonlAdapter("examples/fleet/events.jsonl")]
    b = [e.id for e in JsonlAdapter("examples/fleet/events.jsonl")]
    assert a == b and len(set(a)) == len(a)  # stable across reads, distinct across events
    given = Event(tenant="t", type="x.y", subject="k:1", occurred_at=datetime(2026, 1, 1, tzinfo=UTC), source="s", id="given")
    assert given.id == "given"


def test_iso_parsing_accepts_z_and_bare_dates():
    from keel.envelope import parse_iso

    assert parse_iso("2026-09-05T10:00:00Z") == datetime(2026, 9, 5, 10, tzinfo=UTC)
    assert parse_iso("2026-09-05") == datetime(2026, 9, 5, tzinfo=UTC)
    c = load_cartridge("examples/clinics")
    ev = Event(tenant="t", type="appointment.booked", subject="patient:P-9", occurred_at=datetime(2026, 1, 1, tzinfo=UTC), source="s", payload={"id": "A-9", "starts_at": "not a date", "treatment": "x"})
    assert c.check_event(ev) == ["'appointment.booked' payload 'starts_at' should be datetime, got str"]


def test_ingestor_keeps_no_log_unless_asked():
    c, s = load_cartridge(FLEET), MemoryStore()
    ing = Ingestor(c, s)  # a server or a bulk load: memory must not grow with every event
    assert ing.ingest(JsonlAdapter("examples/fleet/events.jsonl"), strict=True).processed
    assert ing.log == []
    with pytest.raises(ValueError, match="replay needs events"):
        ing.replay("acme-logistics")
    assert ing.replay("acme-logistics", events=JsonlAdapter("examples/fleet/events.jsonl")).processed
