"""Relay: the support showcase's SaaS (docs/design/showcase.md). The planted stories must hold, as of TODAY."""

from datetime import datetime, timezone
from pathlib import Path

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter

HERE = Path(__file__).parent
TODAY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _store():
    c, s = load_cartridge(HERE), MemoryStore()
    report = Ingestor(c, s).ingest(JsonlAdapter(HERE / "events.jsonl"), strict=True)
    assert not report.errors and not report.unhandled
    return c, s


C, S = _store()


def tools(tenant: str) -> Tools:
    return Tools(C, S, tenant)


def facts(ctx: dict) -> dict:
    return {f["predicate"]: f["value"] for f in ctx["facts"]}


def test_the_cartridge_and_its_events_validate():
    assert C.validate() == []
    assert [p for e in JsonlAdapter(HERE / "events.jsonl") for p in C.check_event(e)] == []


def test_acme_september_is_a_prorated_upgrade_not_a_double_charge():
    t = tools("acme")
    assert facts(t.context("account:acme", as_of=TODAY))["plan"] == "enterprise"
    upgrade = t.context("invoice:INV-acme-2026-09-U", as_of=TODAY)
    assert upgrade["entity"]["attrs"]["prorated"] is True and facts(upgrade)["amount"] == 506.67
    assert facts(t.context("ticket:T-2001", as_of=TODAY))["status"] == "pending"


def test_globex_was_on_pro_when_it_reported_the_outage():
    t = tools("globex")
    assert facts(t.context("account:globex", as_of=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)))["plan"] == "pro"
    assert facts(t.context("account:globex", as_of=TODAY))["plan"] == "enterprise"


def test_initech_sync_ticket_was_reopened_and_resolved_again():
    t = tools("initech")
    assert [f["value"] for f in t.history("ticket:T-2201", predicate="status")] == ["open", "resolved", "open", "resolved"]
    assert facts(t.context("ticket:T-2201", as_of=TODAY))["status"] == "resolved"


def test_umbrella_cannot_be_refunded_and_a_fresh_payment_could():
    assert tools("umbrella").context("invoice:INV-umbrella-2026-A", as_of=TODAY)["evaluation"] == {"refund_eligible": False}
    assert tools("hooli").context("invoice:INV-hooli-2026-10", as_of=TODAY)["evaluation"] == {"refund_eligible": True}


def test_stark_has_one_ticket_past_its_sla():
    t = tools("stark")
    assert t.context("ticket:T-3112", as_of=TODAY)["evaluation"] == {"sla_breached": True}
    assert t.context("ticket:T-3105", as_of=TODAY)["evaluation"] == {"sla_breached": False}


def test_hooli_and_hooli_labs_never_mix():
    hooli = {r["subject"] for r in tools("hooli").related("account:hooli", relation="has_invoice")}
    assert hooli and all(s.startswith("invoice:INV-hooli-2026") for s in hooli)
    assert tools("hooli").search("INV-hooli-labs") == []


def test_wayne_502s_fall_inside_the_eu_west_incident():
    t = tools("wayne")
    assert "incident:INC-7" in {r["subject"] for r in t.related("account:wayne", relation="affected_by")}
    assert facts(t.context("incident:INC-7", as_of=TODAY))["status"] == "resolved"


def test_context_lists_the_most_recent_relations_and_counts_the_rest():
    """Stark has dozens of tickets: context shows the 10 newest per relation and says how many there are."""
    import json

    ctx = tools("stark").context("account:stark", as_of=TODAY)
    tickets = [r for r in ctx["relations"] if r["rel"] == "has_ticket"]
    more = {m["relation"]: m for m in ctx["relations_more"]}
    assert len(tickets) == 10 and more["has_ticket"]["total"] > 10 and more["has_ticket"]["direction"] == "out"
    assert "ticket:T-3112" in {r["dst"] for r in tickets}  # the newest is kept
    assert len(json.dumps(tools("stark").context("account:stark", as_of=TODAY, detail="concise"), default=str)) < 6000
