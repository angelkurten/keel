"""The finance example: a household's debts and dues, in Spanish. Runs from anywhere, so a copy of this folder keeps a working test."""

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


def test_debts_and_dues_keep_their_history():
    c, s, report = _run()
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


def test_finance_rules_evaluate():
    c, s, _ = _run()
    t = Tools(c, s, "hogar-demo")
    assert t.context("debt:visa")["evaluation"] == {"debt_expensive": True}
    due = "due:visa-2026-11"  # pending, due 2026-11-05
    assert t.context(due, as_of=datetime(2026, 10, 14, tzinfo=UTC))["evaluation"] == {"due_critical": False}
    assert t.context(due, as_of=datetime(2026, 11, 2, tzinfo=UTC))["evaluation"] == {"due_critical": True}
    paid = t.context("due:visa-2026-10", as_of=datetime(2026, 11, 2, tzinfo=UTC))
    assert paid["evaluation"] == {"due_critical": False}
