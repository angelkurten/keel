import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge
from keel.cli import app
from keel.ingest import JsonlAdapter

FLEET = "cartridges/fleet"
CRM = "cartridges/crm"


def _run(cart, events):
    c = load_cartridge(cart)
    s = MemoryStore()
    ing = Ingestor(c, s)
    report = ing.ingest(JsonlAdapter(events), strict=True)
    return c, s, ing, report


def test_every_cartridge_validates():
    for cart in (FLEET, CRM, "cartridges/finance", "cartridges/clinics"):
        assert load_cartridge(cart).validate() == []


def test_fleet_ingest_and_context():
    c, s, _, report = _run(FLEET, "examples/events/fleet.jsonl")
    assert report.processed == 12 and not report.errors
    t = Tools(c, s, "acme-logistics")
    ctx = t.context("unit:MXK-4821")
    rels = {r["rel"] for r in ctx["relations"]}
    assert {"assigned_to", "mounted_on", "has_inspection", "has_ticket", "in_workshop", "covers_unit"} <= rels
    assert "tire_critical" not in ctx["rules"]  # rule applies to tires, not units
    assert "fuel_anomaly" in ctx["rules"]
    assert ctx["evaluation"] == {"unit_grounded": True}  # fuel_anomaly has no `when`: the agent applies it


def test_facts_are_bitemporal_not_overwritten():
    c, s, _, _ = _run(FLEET, "examples/events/fleet.jsonl")
    t = Tools(c, s, "acme-logistics")
    hist = t.history("tire:T-9001")
    depths = [f for f in hist if f["predicate"] == "depth_mm"]
    assert [d["value"] for d in depths] == [5.2, 2.6]
    assert depths[0]["valid_to"] is not None and depths[1]["valid_to"] is None
    at_sep = s.facts("acme-logistics", "tire:T-9001", at=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert [f.value for f in at_sep if f.predicate == "depth_mm"] == [5.2]


def test_annotate_scope_policy():
    c, s, _, _ = _run(FLEET, "examples/events/fleet.jsonl")
    t = Tools(c, s, "acme-logistics")
    with pytest.raises(PermissionError):
        t.annotate("tire:T-9001", "assessment", "critical", scope="tenant")  # no evidence
    f = t.annotate("tire:T-9001", "assessment", "critical", scope="task", by="agent:x")
    assert f["expires_at"] is not None  # task memory expires
    g = t.annotate("tire:T-9001", "assessment", "critical", scope="tenant", evidence="ev-1", by="human:angel")
    assert g["expires_at"] is None and g["corrected_by"] == "human:angel"


def test_replay_rebuilds_tenant():
    c, s, ing, _ = _run(FLEET, "examples/events/fleet.jsonl")
    before = len(Tools(c, s, "acme-logistics").context("unit:MXK-4821")["relations"])
    ing.replay("acme-logistics")
    after = len(Tools(c, s, "acme-logistics").context("unit:MXK-4821")["relations"])
    assert before == after


def test_second_cartridge_runs_on_same_core():
    c, s, _, report = _run(CRM, "examples/events/crm.jsonl")
    assert report.processed == 4
    t = Tools(c, s, "salon-luna")
    assert any(f["predicate"] == "paid" for f in t.context("appointment:A-900")["facts"])
    assert t.related("customer:C-41", "customer_of")[0]["subject"] == "business:salon-luna"


def test_third_cartridge_runs_on_same_core():
    c, s, _, report = _run("cartridges/finance", "examples/events/finance.jsonl")
    assert report.processed == 13 and not report.errors
    t = Tools(c, s, "hogar-demo")
    balances = [f for f in t.history("debt:visa") if f["predicate"] == "balance_cop"]
    assert [f["value"] for f in balances] == [4200000, 3870000]
    assert balances[0]["valid_to"] is not None and balances[1]["valid_to"] is None
    at_sep = s.facts("hogar-demo", "debt:visa", at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert [f.value for f in at_sep if f.predicate == "balance_cop"] == [4200000]
    dues = {d["subject"] for d in t.related("debt:visa", "for_debt")}
    assert dues == {"due:visa-2026-09", "due:visa-2026-10", "due:visa-2026-11"}
    status = {d: next(f["value"] for f in t.context(d)["facts"] if f["predicate"] == "status") for d in dues}
    assert status == {"due:visa-2026-09": "paid", "due:visa-2026-10": "paid", "due:visa-2026-11": "pending"}
    assert t.related("household:hogar-demo", "owed_by")[0]["subject"] == "debt:visa"


UTC = timezone.utc


def test_context_evaluates_rules_that_carry_when():
    c, s, _, _ = _run(FLEET, "examples/events/fleet.jsonl")
    t = Tools(c, s, "acme-logistics")
    assert t.context("tire:T-9001")["evaluation"] == {"tire_critical": True}  # 2.6 mm < 3 mm
    assert t.context("tire:T-9001", as_of=datetime(2026, 9, 20, tzinfo=UTC))["evaluation"] == {"tire_critical": False}
    assert t.context("tire:T-9001", as_of=datetime(2026, 9, 5, tzinfo=UTC))["evaluation"] == {"tire_critical": None}
    doc = "document:POL-7731"  # expires 2026-10-20; critical within 15 days
    assert t.context(doc, as_of=datetime(2026, 10, 10, tzinfo=UTC))["evaluation"] == {"document_critical": True}
    assert t.context(doc, as_of=datetime(2026, 9, 15, tzinfo=UTC))["evaluation"] == {"document_critical": False}
    c, s, _, _ = _run("cartridges/finance", "examples/events/finance.jsonl")
    t = Tools(c, s, "hogar-demo")
    assert t.context("debt:visa")["evaluation"] == {"debt_expensive": True}
    due = "due:visa-2026-11"  # pending, due 2026-11-05
    assert t.context(due, as_of=datetime(2026, 10, 14, tzinfo=UTC))["evaluation"] == {"due_critical": False}
    assert t.context(due, as_of=datetime(2026, 11, 2, tzinfo=UTC))["evaluation"] == {"due_critical": True}
    paid = t.context("due:visa-2026-10", as_of=datetime(2026, 11, 2, tzinfo=UTC))
    assert paid["evaluation"] == {"due_critical": False}


def test_history_filters_by_predicate_and_scope():
    c, s, _, _ = _run(FLEET, "examples/events/fleet.jsonl")
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
    ok = CliRunner().invoke(app, ["validate", FLEET, "--events", "examples/events/fleet.jsonl"])
    assert ok.exit_code == 0 and "12 events match the catalog" in ok.output


def test_event_ids_are_content_hashes_when_the_source_has_none():
    a = [e.id for e in JsonlAdapter("examples/events/fleet.jsonl")]
    b = [e.id for e in JsonlAdapter("examples/events/fleet.jsonl")]
    assert a == b and len(set(a)) == len(a)  # stable across reads, distinct across events
    given = Event(tenant="t", type="x.y", subject="k:1", occurred_at=datetime(2026, 1, 1, tzinfo=UTC), source="s", id="given")
    assert given.id == "given"


def test_fourth_cartridge_is_the_tutorial_one():
    c, s, _, report = _run("cartridges/clinics", "examples/events/clinics.jsonl")
    assert report.processed == 7 and not report.errors
    t = Tools(c, s, "clinic-centro")
    assert t.context("appointment:A-1")["evaluation"] == {"appointment_missed": True}
    assert t.context("appointment:A-3")["evaluation"] == {"appointment_missed": False}
    appts = [r["subject"] for r in t.related("patient:P-1", "booked_by")]
    assert appts == ["appointment:A-1", "appointment:A-2", "appointment:A-3"]
    missed = [a for a in appts if t.context(a)["evaluation"]["appointment_missed"]]
    assert len(missed) >= c.rules["at_risk_patient"]["no_shows"]  # the rule without `when`: the agent applies it
    assert t.related("appointment:A-4", "performed_in") == [
        {"relation": "performed_in", "direction": "in", "subject": "treatment:T-1", "since": "2026-10-01T11:40:00+00:00"}
    ]
    assert [f["value"] for f in t.history("appointment:A-4", predicate="status")] == ["booked", "done"]


def test_iso_parsing_accepts_z_and_bare_dates():
    from keel.envelope import parse_iso

    assert parse_iso("2026-09-05T10:00:00Z") == datetime(2026, 9, 5, 10, tzinfo=UTC)
    assert parse_iso("2026-09-05") == datetime(2026, 9, 5, tzinfo=UTC)
    c = load_cartridge("cartridges/clinics")
    ev = Event(tenant="t", type="appointment.booked", subject="patient:P-9", occurred_at=datetime(2026, 1, 1, tzinfo=UTC), source="s", payload={"id": "A-9", "starts_at": "not a date", "treatment": "x"})
    assert c.check_event(ev) == ["'appointment.booked' payload 'starts_at' should be datetime, got str"]
