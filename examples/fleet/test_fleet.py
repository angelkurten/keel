"""The fleet example: the reference: a fleet's units, tires, drivers, inspections and documents. Runs from anywhere, so a copy of this folder keeps a working test."""

from datetime import datetime, timezone
from pathlib import Path

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter

HERE = Path(__file__).parent
UTC = timezone.utc


def _run():
    c, s = load_cartridge(HERE), MemoryStore()
    report = Ingestor(c, s).ingest(JsonlAdapter(HERE / "events.jsonl"), strict=True)
    return c, s, report


def test_the_cartridge_and_its_events_validate():
    c = load_cartridge(HERE)
    assert c.validate() == []
    assert [p for e in JsonlAdapter(HERE / "events.jsonl") for p in c.check_event(e)] == []


def test_fleet_ingest_and_context():
    c, s, report = _run()
    assert report.processed == 12 and not report.errors
    t = Tools(c, s, "acme-logistics")
    ctx = t.context("unit:MXK-4821")
    rels = {r["rel"] for r in ctx["relations"]}
    assert {"assigned_to", "mounted_on", "has_inspection", "has_ticket", "in_workshop", "covers_unit"} <= rels
    assert "tire_critical" not in ctx["rules"]  # rule applies to tires, not units
    assert "fuel_anomaly" in ctx["rules"]
    assert ctx["evaluation"] == {"unit_grounded": True}  # fuel_anomaly has no `when`: the agent applies it


def test_context_evaluates_rules_that_carry_when():
    c, s, _ = _run()
    t = Tools(c, s, "acme-logistics")
    assert t.context("tire:T-9001")["evaluation"] == {"tire_critical": True}  # 2.6 mm < 3 mm
    assert t.context("tire:T-9001", as_of=datetime(2026, 9, 20, tzinfo=UTC))["evaluation"] == {"tire_critical": False}
    assert t.context("tire:T-9001", as_of=datetime(2026, 9, 5, tzinfo=UTC))["evaluation"] == {"tire_critical": None}
    doc = "document:POL-7731"  # expires 2026-10-20; critical within 15 days
    assert t.context(doc, as_of=datetime(2026, 10, 10, tzinfo=UTC))["evaluation"] == {"document_critical": True}
    assert t.context(doc, as_of=datetime(2026, 9, 15, tzinfo=UTC))["evaluation"] == {"document_critical": False}
