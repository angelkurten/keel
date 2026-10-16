"""SDK generation: the TypeScript template and the keel sdk command."""

from typer.testing import CliRunner

from keel import load_cartridge
from keel.cli import app
from keel.schema import payload_schema
from keel.sdk import generate
from keel.sdk.typescript import ts_type


def test_json_schema_subset_maps_to_typescript_types():
    schema = payload_schema({
        "id": "string", "amount": "number | null", "status": ["pending", "paid"], "tags": "string[]",
        "lines": [{"id": "string", "qty": "int"}], "meta": {"source": "string", "score": "number | null"},
        "when": "datetime", "extra": "any", "odd-name": "bool",
    })
    assert ts_type(schema) == (
        "{ id: string; amount?: number | null; status: \"pending\" | \"paid\"; tags: Array<string>; "
        "lines: Array<{ id: string; qty: number }>; meta: { source: string; score?: number | null }; "
        "when: string; extra: unknown; \"odd-name\": boolean }"
    )


def test_typescript_file_for_fleet():
    ts = generate("typescript", load_cartridge("examples/fleet"))
    assert 'import { Client, envelope, type ClientOptions, type Envelope } from "@keel-context/sdk";' in ts
    assert "export type TireDepthMeasuredPayload = { unit_id: string; position: string; depth_mm: number };" in ts
    assert "export type TicketOpenedPayload = { id: string; kind: string; urgency: string; inspection_id?: string | null };" in ts
    assert '  tireDepthMeasured: (a: EmitArgs<"tire.depth_measured", "tire">): Envelope => envelope({ type: "tire.depth_measured", ...a }),' in ts
    assert '  budget_line' not in ts and "  tire: (id: string): Subject<\"tire\">" in ts
    assert '"tire_critical":' in ts and '"when": {"predicate": "depth_mm", "lt": 3}' in ts


def test_sdk_command_writes_and_checks(tmp_path):
    out = tmp_path / "keel-fleet.ts"
    res = CliRunner().invoke(app, ["sdk", "typescript", "examples/fleet", "-o", str(out)])
    assert res.exit_code == 0 and out.exists(), res.output
    assert CliRunner().invoke(app, ["sdk", "typescript", "examples/fleet", "-o", str(out), "--check"]).exit_code == 0
    out.write_text("stale", encoding="utf-8")
    res = CliRunner().invoke(app, ["sdk", "typescript", "examples/fleet", "-o", str(out), "--check"])
    assert res.exit_code == 1 and "stale" in res.output
    assert CliRunner().invoke(app, ["sdk", "rust", "examples/fleet"]).exit_code != 0


def test_optional_enum_in_every_template(tmp_path):
    cart = tmp_path / "jira"
    cart.mkdir()
    (cart / "cartridge.yaml").write_text(
        "name: jira\nentities:\n  card: {description: x}\nevents:\n  card.moved:\n    subject: card\n    payload: { status: string, category: [new, indeterminate, done, null] }\n",
        encoding="utf-8",
    )
    (cart / "handlers.py").write_text("from keel.handlers import HandlerRegistry\nregistry = HandlerRegistry()\n\n@registry.on('card.moved')\ndef moved(event, store):\n    pass\n", encoding="utf-8")
    c = load_cartridge(cart)
    assert c.validate() == []
    ts = generate("typescript", c)
    assert 'export type CardMovedPayload = { status: string; category?: "new" | "indeterminate" | "done" | null };' in ts
    py = generate("python", c)
    assert 'class CardMovedPayload(_CardMovedPayloadRequired, total=False):\n    category: Literal["new", "indeterminate", "done"] | None' in py
    go = generate("go", c, package="jira")
    assert "type CardMovedPayloadCategory string" in go and '\tCategory *CardMovedPayloadCategory `json:"category,omitempty"`' in go and "CardMovedPayloadCategoryNull" not in go
    php = generate("php", c)
    assert "enum CardMovedPayloadCategory: string" in php and "case Null" not in php
    assert "public readonly ?CardMovedPayloadCategory $category = null," in php and "'category' => ($this->category === null ? null : $this->category->value)," in php
