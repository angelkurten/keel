"""Payload notation: short hints to JSON Schema, and the checker behind `keel validate --events`."""

import json

import pytest
from typer.testing import CliRunner

from keel.cli import app
from keel.schema import check, parse, payload_schema

SPEC = {
    "id": "string",
    "amount": "number | null",
    "status": ["pending", "paid"],
    "tags": "string[]",
    "lines": [{"id": "string", "qty": "int"}],
    "meta": {"source": "string", "score": "number | null"},
    "when": "datetime",
    "extra": "any",
}


def test_short_hints_become_json_schema():
    s = payload_schema(SPEC)
    assert s["type"] == "object" and s["required"] == ["id", "status", "tags", "lines", "meta", "when", "extra"]
    p = s["properties"]
    assert p["id"] == {"type": "string"}
    assert p["amount"] == {"type": ["number", "null"]}
    assert p["status"] == {"enum": ["pending", "paid"]}
    assert p["tags"] == {"type": "array", "items": {"type": "string"}}
    assert p["lines"] == {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["id", "qty"]}}
    assert p["meta"]["required"] == ["source"] and p["meta"]["properties"]["score"] == {"type": ["number", "null"]}
    assert p["when"] == {"type": "string", "format": "date-time"} and p["extra"] == {}


def test_checker_reports_every_mismatch_with_its_path():
    good = {"id": "A", "amount": None, "status": "paid", "tags": ["x"], "lines": [{"id": "l1", "qty": 2}], "meta": {"source": "s"}, "when": "2026-10-14T10:00:00Z", "extra": {"anything": 1}}
    assert check(good, parse(SPEC), "") == []
    bad = {"id": 1, "status": "lost", "tags": "x", "lines": [{"id": "l1", "qty": "2"}], "meta": {"score": "high"}, "when": "yesterday", "extra": None}
    assert check(bad, parse(SPEC), "") == [
        ("id", "should be string, got int"),
        ("status", "should be one of pending, paid, got 'lost'"),
        ("tags", "should be string[], got str"),
        ("lines[0].qty", "should be int, got str"),
        ("meta.source", "is missing (string)"),
        ("meta.score", "should be number | null, got str"),
        ("when", "should be datetime, got str"),
    ]


def test_unknown_hints_are_rejected_and_validate_reports_them(tmp_path):
    with pytest.raises(ValueError):
        parse({"id": "strng"})
    with pytest.raises(ValueError):
        parse({"x": ["string"]})  # ambiguous: an enum with the literal 'string'? use string[]
    cart = tmp_path / "odd"
    cart.mkdir()
    (cart / "cartridge.yaml").write_text("name: odd\nentities:\n  thing: {description: x}\nevents:\n  thing.made:\n    subject: thing\n    payload: { id: strng }\n", encoding="utf-8")
    (cart / "handlers.py").write_text("from keel.handlers import HandlerRegistry\nregistry = HandlerRegistry()\n\n@registry.on('thing.made')\ndef made(event, store):\n    pass\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["validate", str(cart)])
    assert res.exit_code == 1 and "event 'thing.made': unknown payload hint 'strng'" in res.output


def test_keel_schema_prints_the_generator_input():
    res = CliRunner().invoke(app, ["schema", "examples/fleet"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["cartridge"] == "fleet" and doc["envelope"]["properties"]["tenant"]
    ev = doc["events"]["tire.depth_measured"]
    assert ev["subject"] == "tire" and ev["payload"]["properties"]["depth_mm"] == {"type": "number"}
    assert ev["payload"]["required"] == ["unit_id", "position", "depth_mm"]
    opened = doc["events"]["ticket.opened"]["payload"]["properties"]["inspection_id"]
    assert opened == {"type": ["string", "null"]}
    assert doc["rules"]["tire_critical"]["when"] == {"predicate": "depth_mm", "lt": 3}
    assert doc["memory"]["ttl"]["task"] == 14 * 86400


def test_null_in_an_enum_list_is_an_optional_enum():
    s = payload_schema({"stage": ["new", "done", None], "kind": ["a", "b"]})
    assert s["properties"]["stage"] == {"enum": ["new", "done", None]} and s["required"] == ["kind"]
    h = parse({"stage": ["new", "done", None]})
    assert check({}, h, "") == [] and check({"stage": None}, h, "") == [] and check({"stage": "done"}, h, "") == []
    assert check({"stage": "x"}, h, "") == [("stage", "should be one of new, done, or null, got 'x'")]
