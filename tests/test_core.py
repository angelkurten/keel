from datetime import datetime, timezone

import pytest

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter

FLEET = "cartridges/fleet"
CRM = "cartridges/crm"


def _run(cart, events):
    c = load_cartridge(cart)
    s = MemoryStore()
    ing = Ingestor(c, s)
    report = ing.ingest(JsonlAdapter(events), strict=True)
    return c, s, ing, report


def test_both_cartridges_validate():
    for cart in (FLEET, CRM):
        assert load_cartridge(cart).validate() == []


def test_fleet_ingest_and_context():
    c, s, ing, report = _run(FLEET, "examples/events/fleet.jsonl")
    assert report.processed == 12 and not report.errors
    t = Tools(c, s, "acme-logistics")
    ctx = t.context("unit:MXK-4821")
    rels = {r["rel"] for r in ctx["relations"]}
    assert {"assigned_to", "mounted_on", "has_inspection", "has_ticket", "in_workshop", "covers_unit"} <= rels
    assert "tire_critical" not in ctx["rules"]  # rule applies to tires, not units
    assert "fuel_anomaly" in ctx["rules"]


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
