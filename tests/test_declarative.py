"""Declarative handlers: an event that only copies payload fields to facts and relations needs no handlers.py."""

from datetime import datetime, timezone

import yaml

from keel import Event, Ingestor, MemoryStore, load_cartridge

UTC = timezone.utc

YAML = {
    "name": "shop",
    "entities": {"order": {"description": "An order."}, "customer": {"description": "A customer."}},
    "relations": {"placed_by": {"from": "order", "to": "customer", "one": True}},
    "events": {
        "order.observed": {
            "subject": "order",
            "owner": "source:shop",
            "payload": {"total": "number", "status": "string", "customer_id": "string | null"},
            "facts": ["total", "status"],
            "relate": {"placed_by": "customer:{customer_id}"},
        }
    },
}


def _cart(tmp_path, raw=YAML, handlers=None):
    (tmp_path / "cartridge.yaml").write_text(yaml.safe_dump(raw))
    if handlers:
        (tmp_path / "handlers.py").write_text(handlers)
    return load_cartridge(tmp_path)


def _ev(i, day, **payload):
    return Event(id=f"e{i}", tenant="t", type="order.observed", subject="order:o1", occurred_at=datetime(2026, 10, day, tzinfo=UTC),
                 source="shop", payload=payload)


def test_declarative_event_writes_facts_and_relations(tmp_path):
    c = _cart(tmp_path)
    assert c.validate() == []
    s = MemoryStore()
    r = Ingestor(c, s).ingest([_ev(1, 1, total=10, status="open", customer_id="c1"), _ev(2, 2, total=12, status="paid", customer_id="c2")], strict=True)
    assert r.processed == 2
    facts = {f.predicate: f for f in s.facts("t", "order:o1")}
    assert facts["total"].value == 12 and facts["status"].value == "paid" and facts["status"].evidence == "e2"
    assert s.entity("t", "order:o1") and s.entity("t", "customer:c2")
    live = [x.dst for x in s.relations("t", "order:o1", "placed_by", at=datetime(2026, 10, 3, tzinfo=UTC))]
    assert live == ["customer:c2"]  # `one`: the earlier customer was closed


def test_declarative_skips_absent_and_null_fields(tmp_path):
    c = _cart(tmp_path)
    s = MemoryStore()
    Ingestor(c, s).ingest([_ev(1, 1, total=10, customer_id=None)], strict=True)
    assert {f.predicate for f in s.facts("t", "order:o1")} == {"total"}
    assert s.relations("t", "order:o1") == []


def test_declarative_validation(tmp_path):
    bad = yaml.safe_load(yaml.safe_dump(YAML))
    spec = bad["events"]["order.observed"]
    spec["facts"] = ["total", "nope"]
    spec["relate"] = {"placed_by": "order:{customer_id}", "missing": "customer:{customer_id}"}
    problems = _cart(tmp_path, bad).validate()
    assert any("'nope' is not a payload field" in p for p in problems)
    assert any("'missing' is not a declared relation" in p for p in problems)
    assert any("'placed_by' goes order -> customer, not 'order' -> 'order'" in p for p in problems)


def test_declarative_and_handler_both_is_refused(tmp_path):
    h = "from keel.handlers import HandlerRegistry\nregistry = HandlerRegistry()\n\n@registry.on('order.observed')\ndef f(e, s):\n    pass\n"
    assert any("both" in p for p in _cart(tmp_path, handlers=h).validate())
