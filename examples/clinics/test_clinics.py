"""The clinics example: the one docs/first-cartridge.md builds. Runs from anywhere, so a copy of this folder keeps a working test."""

from datetime import timezone
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


def test_the_tutorial_cartridge_answers():
    c, s, report = _run()
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
