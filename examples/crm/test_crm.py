"""The crm example: the second domain, which proves the core knows none. Runs from anywhere, so a copy of this folder keeps a working test."""

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


def test_crm_runs_on_the_same_core():
    c, s, report = _run()
    assert report.processed == 4
    t = Tools(c, s, "salon-luna")
    assert any(f["predicate"] == "paid" for f in t.context("appointment:A-900")["facts"])
    assert t.related("customer:C-41", "customer_of")[0]["subject"] == "business:salon-luna"
