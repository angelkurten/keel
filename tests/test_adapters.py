import json
import sqlite3
import sys

import pytest

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.adapters.postgres import PostgresTableAdapter
from keel.ingest import JsonlAdapter

FLEET = "cartridges/fleet"
FLEET_EVENTS = "examples/events/fleet.jsonl"
TENANT = "acme-logistics"
DDL = (
    "CREATE TABLE keel_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, tenant TEXT, "
    "type TEXT, subject TEXT, occurred_at TEXT, recorded_at TEXT, source TEXT, payload TEXT)"
)


def _shape(tools, subject):
    ctx = tools.context(subject)
    return (
        {(f["predicate"], str(f["value"]), f["valid_from"]) for f in ctx["facts"]},
        {(r["rel"], r["dst"]) for r in ctx["relations"]},
    )


def test_table_adapter_resumes_from_cursor(tmp_path):
    db, cursor = tmp_path / "outbox.db", tmp_path / "cursor"
    events = list(JsonlAdapter(FLEET_EVENTS))
    conn = sqlite3.connect(db)
    conn.execute(DDL)

    def insert(evs):
        conn.executemany(
            "INSERT INTO keel_events (id, tenant, type, subject, occurred_at, recorded_at, source, payload) VALUES (?,?,?,?,?,?,?,?)",
            [(e.id, e.tenant, e.type, e.subject, e.occurred_at.isoformat(), e.recorded_at.isoformat(), e.source, json.dumps(e.payload)) for e in evs],
        )
        conn.commit()

    def adapter(**kw):
        return PostgresTableAdapter(lambda: sqlite3.connect(db), cursor_path=cursor, placeholder="?", **kw)

    c, s = load_cartridge(FLEET), MemoryStore()
    ing = Ingestor(c, s)
    insert(events[:7])
    assert ing.ingest(adapter(batch_size=3), strict=True).processed == 7
    assert adapter().position == 7
    assert ing.ingest(adapter(), strict=True).processed == 0  # nothing new, nothing re-ingested

    insert(events[7:])  # "restart": a fresh adapter picks up only what arrived after the cursor
    assert ing.ingest(adapter(), strict=True).processed == 5
    assert [e.id for e in ing.log] == [e.id for e in events]

    c2, s2 = load_cartridge(FLEET), MemoryStore()
    Ingestor(c2, s2).ingest(JsonlAdapter(FLEET_EVENTS), strict=True)
    assert _shape(Tools(c, s, TENANT), "unit:MXK-4821") == _shape(Tools(c2, s2, TENANT), "unit:MXK-4821")


def test_webhook_validates_authenticates_and_ingests():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.adapters.webhook import build_app

    c, s = load_cartridge("cartridges/crm"), MemoryStore()
    client = TestClient(build_app(Ingestor(c, s), token="s3cret"))
    ok = {"Authorization": "Bearer s3cret"}
    with open("examples/events/crm.jsonl", encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip() and not line.startswith("#")]

    assert client.post("/events", json=events).status_code == 401
    assert client.post("/events", json=events, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/events", json={"tenant": "x", "type": "a.b"}, headers=ok).status_code == 422
    assert client.post("/events", json={**events[0], "subject": "no-colon"}, headers=ok).status_code == 422

    r = client.post("/events", json=events[:1], headers=ok)
    assert r.status_code == 200 and r.json()["processed"] == 1
    r = client.post("/events", json=events[1:], headers=ok)
    assert r.json() == {"processed": 3, "unhandled": [], "errors": []}
    assert any(f["predicate"] == "paid" for f in Tools(c, s, "salon-luna").context("appointment:A-900")["facts"])


def test_mcp_server_exposes_exactly_the_five_tools():
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "keel.cli", "serve", FLEET, "--tenant", TENANT, "--events", FLEET_EVENTS],
    )

    async def run():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert names == {"context", "history", "search", "annotate", "related"}
            read_only = {t.name for t in tools if (t.annotations or None) and t.annotations.model_dump(by_alias=True).get("readOnlyHint")}
            assert read_only == names - {"annotate"}  # clients that gate on annotations run reads unprompted
            res = await session.call_tool("context", {"subject": "unit:MXK-4821"})
            ctx = json.loads(res.content[0].text)
            assert ctx["subject"] == "unit:MXK-4821" and ctx["relations"] and "fuel_anomaly" in ctx["rules"]
            res = await session.call_tool("history", {"subject": "tire:T-9001", "since": "2026-10-01T00:00:00+00:00"})
            facts = [json.loads(c.text) for c in res.content]  # a list return is one content item per element
            assert facts and all(f["subject"] == "tire:T-9001" for f in facts)

    anyio.run(run)
