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
    ts = generate("typescript", load_cartridge("cartridges/fleet"))
    assert 'import { Client, envelope, type ClientOptions, type Envelope } from "@keel-context/sdk";' in ts
    assert "export type TireDepthMeasuredPayload = { unit_id: string; position: string; depth_mm: number };" in ts
    assert "export type TicketOpenedPayload = { id: string; kind: string; urgency: string; inspection_id?: string | null };" in ts
    assert '  tireDepthMeasured: (a: EmitArgs<"tire.depth_measured", "tire">): Envelope => envelope({ type: "tire.depth_measured", ...a }),' in ts
    assert '  budget_line' not in ts and "  tire: (id: string): Subject<\"tire\">" in ts
    assert '"tire_critical":' in ts and '"when": {"predicate": "depth_mm", "lt": 3}' in ts


def test_sdk_command_writes_and_checks(tmp_path):
    out = tmp_path / "keel-fleet.ts"
    res = CliRunner().invoke(app, ["sdk", "typescript", "cartridges/fleet", "-o", str(out)])
    assert res.exit_code == 0 and out.exists(), res.output
    assert CliRunner().invoke(app, ["sdk", "typescript", "cartridges/fleet", "-o", str(out), "--check"]).exit_code == 0
    out.write_text("stale", encoding="utf-8")
    res = CliRunner().invoke(app, ["sdk", "typescript", "cartridges/fleet", "-o", str(out), "--check"])
    assert res.exit_code == 1 and "stale" in res.output
    assert CliRunner().invoke(app, ["sdk", "rust", "cartridges/fleet"]).exit_code != 0
