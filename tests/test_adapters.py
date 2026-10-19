import json
import os
import sqlite3
import sys

import pytest

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.adapters.postgres import PostgresTableAdapter
from keel.ingest import JsonlAdapter

FLEET = "examples/fleet"
FLEET_EVENTS = "examples/fleet/events.jsonl"
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
    ing = Ingestor(c, s, keep_log=True)
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

    c, s = load_cartridge("examples/crm"), MemoryStore()
    client = TestClient(build_app(Ingestor(c, s), token="s3cret"))
    ok = {"Authorization": "Bearer s3cret"}
    with open("examples/crm/events.jsonl", encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip() and not line.startswith("#")]

    assert client.post("/events", json=events).status_code == 401
    assert client.post("/events", json=events, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/events", json={"tenant": "x", "type": "a.b"}, headers=ok).status_code == 422
    assert client.post("/events", json={**events[0], "subject": "no-colon"}, headers=ok).status_code == 422

    r = client.post("/events", json=events[:1], headers=ok)
    assert r.status_code == 200 and r.json()["processed"] == 1
    r = client.post("/events", json=events[1:], headers=ok)
    assert r.json() == {"processed": 3, "duplicates": 0, "unhandled": [], "errors": [], "forgotten": [], "skipped": 0, "retained": []}
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
            tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
            res = await session.call_tool("annotate", {"subject": "tire:T-9001", "predicate": "assessment", "value": "replace"}, meta={"traceparent": tp})
            assert json.loads(res.content[0].text)["trace"] == tp  # MCP carries W3C trace context in _meta
            value = {"priority": "high", "reasons": ["tire_critical"], "depth_mm": 2.6}
            res = await session.call_tool("annotate", {"subject": "tire:T-9001", "predicate": "triage", "value": value})
            assert json.loads(res.content[0].text)["value"] == value  # any JSON, as over HTTP and in Python
            res = await session.call_tool("annotate", {"subject": "tire:T-9001", "predicate": "depth_seen", "value": 2.6})
            assert json.loads(res.content[0].text)["value"] == 2.6

    anyio.run(run)


# ---- bus adapters on fakes: the loop, the ack order, the poison pill ----------------------


class _KafkaMessage:
    def __init__(self, value, offset):
        self._value, self.offset = value, offset

    def value(self):
        return self._value

    def error(self):
        return None


class _KafkaConsumer:
    def __init__(self, bodies):
        self.queue = [_KafkaMessage(b, i) for i, b in enumerate(bodies)]
        self.committed = []

    def poll(self, timeout):
        return self.queue.pop(0) if self.queue else None

    def commit(self, message, asynchronous=False):
        self.committed.append(message.offset)


class _Sqs:
    def __init__(self, bodies):
        self.pending = {f"rh-{i}": b for i, b in enumerate(bodies)}  # stays until deleted
        self.deleted, self.in_flight = [], set()  # received, hidden until the visibility timeout

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds):
        items = [(h, b) for h, b in self.pending.items() if h not in self.in_flight][:MaxNumberOfMessages]
        self.in_flight |= {h for h, _ in items}
        return {"Messages": [{"MessageId": h, "ReceiptHandle": h, "Body": b} for h, b in items]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.pending.pop(ReceiptHandle)
        self.deleted.append(ReceiptHandle)

    def delete_message_batch(self, QueueUrl, Entries):
        assert len(Entries) <= 10
        for e in Entries:
            self.delete_message(QueueUrl, e["ReceiptHandle"])


def _lines():
    from pathlib import Path

    lines = Path(FLEET_EVENTS).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.startswith("#")]  # what JsonlAdapter yields


def _bus(kind, bodies, **kw):
    from keel.adapters.bus import KafkaAdapter, SqsAdapter

    if kind == "kafka":
        consumer = _KafkaConsumer([b.encode() for b in bodies])
        return KafkaAdapter(consumer, **kw), consumer
    client = _Sqs(bodies)
    return SqsAdapter(client, "https://sqs.example/keel", **kw), client


@pytest.mark.parametrize("kind", ["kafka", "sqs"])
def test_bus_adapters_ingest_like_the_file_and_ack_after_the_handler(kind):
    c = load_cartridge(FLEET)
    adapter, client = _bus(kind, _lines())
    store = MemoryStore()
    seen_at_first_ack = []
    original = client.commit if kind == "kafka" else client.delete_message

    def ack_spy(*a, **k):  # the ack must run after the handler: the entity is already there
        if not seen_at_first_ack:
            seen_at_first_ack.append(store.entity(TENANT, "unit:MXK-4821") is not None)
        return original(*a, **k)

    if kind == "kafka":
        client.commit = ack_spy
    else:
        client.delete_message = ack_spy
    report = Ingestor(c, store).ingest(adapter, strict=True)
    assert report.processed == 12 and not report.errors
    assert seen_at_first_ack == [True]
    assert len(client.committed if kind == "kafka" else client.deleted) == 12
    reference = MemoryStore()
    Ingestor(c, reference).ingest(JsonlAdapter(FLEET_EVENTS))
    for subject in ("unit:MXK-4821", "tire:T-9001"):
        assert _shape(Tools(c, store, TENANT), subject) == _shape(Tools(c, reference, TENANT), subject)


@pytest.mark.parametrize("kind", ["kafka", "sqs"])
def test_bus_adapters_poison_message_raises_unless_dead_lettered(kind):
    c = load_cartridge(FLEET)
    good = _lines()[0]
    adapter, client = _bus(kind, ["not json", good])
    with pytest.raises(ValueError):
        Ingestor(c, MemoryStore()).ingest(adapter)
    assert not (client.committed if kind == "kafka" else client.deleted)  # nothing acknowledged
    dead = []
    adapter, client = _bus(kind, ["not json", good], dead_letter=lambda body, exc: dead.append(type(exc).__name__))
    report = Ingestor(c, MemoryStore()).ingest(adapter)
    assert report.processed == 1 and dead == ["JSONDecodeError"]
    assert len(client.committed if kind == "kafka" else client.deleted) == 2  # the poison pill was acked and skipped


def test_bus_adapter_leaves_a_failed_event_unacked_in_strict_mode():
    c = load_cartridge(FLEET)
    lines = _lines()
    broken = json.loads(lines[4])  # tire.depth_measured
    del broken["payload"]["depth_mm"]  # the handler will raise KeyError
    adapter, consumer = _bus("kafka", lines[:4] + [json.dumps(broken)])
    with pytest.raises(KeyError):
        Ingestor(c, MemoryStore()).ingest(adapter, strict=True)
    assert consumer.committed == [0, 1, 2, 3]  # the four before it were acked, the failed one is redelivered



# ---- the real thing, when a Postgres is at hand ----------------------------------------------


@pytest.mark.skipif(not os.environ.get("KEEL_PG_DSN"), reason="set KEEL_PG_DSN to run against a real Postgres")
def test_table_adapter_on_real_postgres(tmp_path):
    """docker run -d --rm --name keel-pg -e POSTGRES_PASSWORD=keel -p 127.0.0.1:5435:5432 postgres:16-alpine
    KEEL_PG_DSN=postgresql://postgres:keel@127.0.0.1:5435/postgres uv run --extra postgres pytest -k real_postgres"""
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["KEEL_PG_DSN"]
    ddl = (
        "DROP TABLE IF EXISTS keel_events; CREATE TABLE keel_events (seq bigserial PRIMARY KEY, id text NOT NULL, "
        "tenant text NOT NULL, type text NOT NULL, subject text NOT NULL, occurred_at timestamptz NOT NULL, "
        "recorded_at timestamptz NOT NULL DEFAULT now(), source text NOT NULL, payload jsonb NOT NULL DEFAULT '{}')"
    )
    ins = "INSERT INTO keel_events (id, tenant, type, subject, occurred_at, source, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    events = list(JsonlAdapter(FLEET_EVENTS))

    def insert(batch):
        with psycopg.connect(dsn, autocommit=True) as conn:
            for e in batch:
                conn.execute(ins, (e.id, e.tenant, e.type, e.subject, e.occurred_at, e.source, json.dumps(e.payload)))

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(ddl)
    insert(events[:7])
    c = load_cartridge(FLEET)
    store, ing = MemoryStore(), None
    ing = Ingestor(c, store, keep_log=True)
    adapter = PostgresTableAdapter(lambda: psycopg.connect(dsn), cursor_path=tmp_path / "cursor")
    assert ing.ingest(adapter, strict=True).processed == 7 and adapter.position == 7
    insert(events[7:])
    assert ing.ingest(adapter, strict=True).processed == 5 and adapter.position == 12
    assert ing.ingest(adapter, strict=True).processed == 0
    assert [e.id for e in ing.log] == [e.id for e in events]
    reference = MemoryStore()
    Ingestor(c, reference).ingest(JsonlAdapter(FLEET_EVENTS))
    for subject in ("unit:MXK-4821", "tire:T-9001", "ticket:TK-3608"):
        assert _shape(Tools(c, store, TENANT), subject) == _shape(Tools(c, reference, TENANT), subject)


def test_bus_connect_passes_every_option(monkeypatch):
    import types

    from keel.adapters.bus import KafkaAdapter, SqsAdapter

    class Consumer:
        def __init__(self, conf):
            self.conf = conf

        def subscribe(self, topics):
            self.topics = topics

    monkeypatch.setitem(sys.modules, "confluent_kafka", types.SimpleNamespace(Consumer=Consumer))
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service, **kw: (service, kw)))
    dl = lambda raw, exc: None
    k = KafkaAdapter.connect("b:9092", "events", poll_timeout=0.2, dead_letter=dl, **{"security.protocol": "SASL_SSL"})
    assert k.poll_timeout == 0.2 and k.dead_letter is dl and k.consumer.topics == ["events"]
    assert k.consumer.conf["security.protocol"] == "SASL_SSL" and k.consumer.conf["enable.auto.commit"] is False
    s = SqsAdapter.connect("https://q", batch_size=5, wait_seconds=1, dead_letter=dl, region_name="us-east-1")
    assert (s.batch_size, s.wait_seconds, s.dead_letter, s.client) == (5, 1, dl, ("sqs", {"region_name": "us-east-1"}))


class _Crash(Exception):
    pass


def _crash_at(cartridge, event_id):
    """The process dies while storing `event_id`, once: the batch it is in rolls back."""
    dispatch, fired = cartridge.registry.dispatch, []

    def crashing(ev, store):
        if ev.id == event_id and not fired:
            fired.append(ev.id)
            raise _Crash(ev.id)
        return dispatch(ev, store)

    cartridge.registry.dispatch = crashing


def test_table_adapter_cursor_never_runs_ahead_of_a_batch_commit(tmp_path):
    from keel.store.sqlite import SqliteStore

    db, cursor = tmp_path / "outbox.db", tmp_path / "cursor"
    events = list(JsonlAdapter(FLEET_EVENTS))
    conn = sqlite3.connect(db)
    conn.execute(DDL)
    conn.executemany(
        "INSERT INTO keel_events (id, tenant, type, subject, occurred_at, recorded_at, source, payload) VALUES (?,?,?,?,?,?,?,?)",
        [(e.id, e.tenant, e.type, e.subject, e.occurred_at.isoformat(), e.recorded_at.isoformat(), e.source, json.dumps(e.payload)) for e in events],
    )
    conn.commit()

    def adapter():
        return PostgresTableAdapter(lambda: sqlite3.connect(db), cursor_path=cursor, placeholder="?", batch_size=4)

    c, store = load_cartridge(FLEET), SqliteStore(tmp_path / "keel.db")
    _crash_at(c, events[7].id)  # in the third batch of three (6, 7, 8)
    with pytest.raises(_Crash):
        Ingestor(c, store).ingest(adapter(), strict=True, batch=3)
    assert adapter().position == 6  # the two committed batches, not the events taken after them
    Ingestor(c, store).ingest(adapter(), strict=True, batch=3)
    assert adapter().position == 12
    ref = MemoryStore()
    Ingestor(load_cartridge(FLEET), ref).ingest(JsonlAdapter(FLEET_EVENTS), strict=True)
    assert _shape(Tools(c, store, TENANT), "unit:MXK-4821") == _shape(Tools(c, ref, TENANT), "unit:MXK-4821")


@pytest.mark.parametrize("kind", ["kafka", "sqs"])
def test_bus_acks_only_what_a_batch_committed(kind, tmp_path):
    from keel.store.sqlite import SqliteStore

    lines = _lines()
    adapter, client = _bus(kind, lines)
    c = load_cartridge(FLEET)
    _crash_at(c, list(JsonlAdapter(FLEET_EVENTS))[7].id)
    with pytest.raises(_Crash):
        Ingestor(c, SqliteStore(tmp_path / "keel.db")).ingest(adapter, strict=True, batch=3)
    acked = client.committed if kind == "kafka" else client.deleted
    assert len(acked) == 6  # messages 6..8 stay unacknowledged and come back


def test_consume_acks_what_it_stored_and_leaves_the_rest_to_the_dead_letter_queue(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import keel.cli
    from keel import SqliteStore
    from keel.adapters.bus import SqsAdapter
    from keel.cli import app

    bad_type = json.dumps({"tenant": TENANT, "type": "nobody.handles", "subject": "unit:X", "occurred_at": "2026-09-01T00:00:00Z", "source": "t"})
    client = _Sqs(_lines() + [bad_type, "not json"])
    monkeypatch.setattr(keel.cli, "_queue", lambda url: SqsAdapter(client, url))
    db = str(tmp_path / "keel.db")
    res = CliRunner().invoke(app, ["consume", FLEET, "--queue", "http://elasticmq:9324/000000000000/keel", "--db", db, "--once"])
    assert res.exit_code == 1 and "consumed=12 failed=2" in res.output, res.output
    assert len(client.deleted) == 12 and len(client.pending) == 2  # the two failures wait for redelivery, then the DLQ
    assert SqliteStore(db).entity(TENANT, "unit:MXK-4821") is not None


def test_consume_stores_a_batch_per_transaction_and_stops_when_the_store_is_gone(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import keel.cli
    from keel.adapters.bus import SqsAdapter
    from keel.cli import app

    calls = []
    real = keel.cli.Ingestor.ingest
    monkeypatch.setattr(keel.cli.Ingestor, "ingest", lambda self, evs, **kw: calls.append(len(evs)) or real(self, evs, **kw))
    client = _Sqs(_lines())
    monkeypatch.setattr(keel.cli, "_queue", lambda url: SqsAdapter(client, url))
    args = ["consume", FLEET, "--queue", "http://elasticmq:9324/000000000000/keel", "--db", str(tmp_path / "keel.db"), "--once"]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0 and "consumed=12 failed=0" in res.output, res.output
    assert calls == [12] and len(client.deleted) == 12  # SQS hands 10 per receive; the batch is topped up to all 12

    def down(*a, **kw):
        raise ConnectionError("Already closed")

    client = _Sqs(_lines())
    monkeypatch.setattr(keel.cli.Ingestor, "ingest", down)
    monkeypatch.setattr(keel.store.sqlite.SqliteStore, "entity", down)
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 2 and "store unavailable" in res.output, res.output
    assert client.deleted == [] and len(client.pending) == 12  # nothing acknowledged, nothing dead-lettered by us


def test_pgoutput_decode_without_a_server():
    import struct

    from keel.adapters.pgcdc import decode

    s = lambda x: x.encode() + b"\0"
    col = lambda name, oid: struct.pack("!b", 1) + s(name) + struct.pack("!Ii", oid, -1)
    rel = b"R" + struct.pack("!I", 7) + s("public") + s("tires") + b"d" + struct.pack("!h", 3) + col("serial", 25) + col("depth_mm", 1700) + col("ok", 16)
    tup = lambda *vals: struct.pack("!h", len(vals)) + b"".join(b"n" if v is None else b"t" + struct.pack("!i", len(v)) + v.encode() for v in vals)
    begin = b"B" + struct.pack("!Qqi", (1 << 32) + 0x2A, 1_000_000, 9)  # 1 second after 2000-01-01
    ins = b"I" + struct.pack("!I", 7) + b"N" + tup("T-1", "5.2", "t")
    upd = b"U" + struct.pack("!I", 7) + b"O" + tup("T-1", "5.2", "t") + b"N" + tup("T-1", "3", None)
    dele = b"D" + struct.pack("!I", 7) + b"K" + tup("T-1", None, None)
    commit = b"C" + struct.pack("!bQQq", 0, (1 << 32) + 0x2A, (1 << 32) + 0x40, 1_000_000)
    out = list(decode([begin, rel, ins, upd, dele, commit]))
    assert [(o[0], o[2], o[4]) for o in out] == [
        ("1/2A", "created", {"serial": "T-1", "depth_mm": 5.2, "ok": True}),
        ("1/2A", "updated", {"serial": "T-1", "depth_mm": 3, "ok": None}),  # the new row, after the old one
        ("1/2A", "deleted", {"serial": "T-1", "depth_mm": None, "ok": None}),
        ("1/40", "commit", {}),  # the end lsn to confirm
    ]
    assert out[0][1].isoformat() == "2000-01-01T00:00:01+00:00" and out[0][3].name == "tires"


PG = os.environ.get("KEEL_PG_URL")  # postgresql://postgres:keel@127.0.0.1:5432/postgres, with wal_level=logical


@pytest.mark.skipif(not PG, reason="set KEEL_PG_URL to a Postgres with wal_level=logical")
def test_cdc_follows_a_table_and_moves_the_slot_only_after_storing(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    from typer.testing import CliRunner

    from keel.cli import app

    cart = tmp_path / "tires"
    cart.mkdir()
    (cart / "cartridge.yaml").write_text("""name: tires
entities:
  tire: {description: A tire.}
relations: {}
events:
  tire.created: {subject: tire, owner: core, payload: {serial: string, depth_mm: number}}
  tire.updated: {subject: tire, owner: core, payload: {serial: string, depth_mm: number}}
  tire.deleted: {subject: tire, owner: core, payload: {serial: string}}
rules: {}
memory: {ttl: {task: 14d, agent: 1d}, requires_evidence: [global, tenant]}
""", encoding="utf-8")
    (cart / "handlers.py").write_text('''from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("tire.created")
@registry.on("tire.updated")
def depth(event, store):
    store.upsert_entity(event.tenant, "tire", event.payload["serial"], ref=event.source)
    store.open_fact(event.tenant, event.subject, "depth_mm", event.payload["depth_mm"], event.occurred_at, source=event.source, evidence=event.id)


@registry.on("tire.deleted")
def gone(event, store):
    store.close_fact(event.tenant, event.subject, "depth_mm", event.occurred_at)
''', encoding="utf-8")
    (tmp_path / "cdc.yaml").write_text("tires: {kind: tire, id: serial, tenant: fleet_id, columns: [depth_mm]}\n", encoding="utf-8")
    slot = f"keel_test_{os.getpid()}"
    c = psycopg.connect(PG, autocommit=True)
    c.execute("DROP TABLE IF EXISTS tires; CREATE TABLE tires (serial text PRIMARY KEY, fleet_id text, depth_mm numeric, secret text)")
    c.execute(f"DROP PUBLICATION IF EXISTS {slot}")
    db = str(tmp_path / "g.db")
    args = ["cdc", str(cart), "--dsn", PG, "--map", str(tmp_path / "cdc.yaml"), "--db", db, "--slot", slot, "--publication", slot, "--once"]
    try:
        assert CliRunner().invoke(app, [*args, "--setup"]).exit_code == 0
        c.execute("INSERT INTO tires VALUES ('T-1', 'acme', 5.2, 'not for keel')")
        c.execute("UPDATE tires SET depth_mm = 2.6 WHERE serial = 'T-1'")
        c.execute("DELETE FROM tires WHERE serial = 'T-1'")
        out = CliRunner().invoke(app, args)
        assert out.exit_code == 0 and "processed=3" in out.output
        from keel.store.sqlite import SqliteStore

        facts = SqliteStore(db).facts("acme", "tire:T-1")
        assert [f.value for f in facts] == [5.2, 2.6] and all(f.valid_to for f in facts)  # history kept, closed by the delete
        assert "secret" not in SqliteStore(db).entity("acme", "tire:T-1").attrs
        assert "processed" not in CliRunner().invoke(app, args).output  # the slot moved past what was stored
        bad = tmp_path / "bad.yaml"
        bad.write_text("tires: {kind: wheel, id: serial, tenant: fleet_id}\n", encoding="utf-8")
        assert CliRunner().invoke(app, ["cdc", str(cart), "--dsn", PG, "--map", str(bad), "--db", db, "--once"]).exit_code != 0  # undeclared types
        c.execute("INSERT INTO tires VALUES ('T-2', 'acme', 4.0, '')")
        assert CliRunner().invoke(app, [*args, "--account", "hosted"]).exit_code == 0  # a hosted container puts tenants under its account
        assert SqliteStore(db).entity("hosted/acme", "tire:T-2") is not None and SqliteStore(db).entity("acme", "tire:T-2") is None
        # keel serve --cdc follows the same tables in a thread, under its account, and says how it goes in /health
        import time

        from keel.cli import _follow_in_background
        from keel.http import RecentEvents

        status: dict = {}
        recent = RecentEvents()
        c.execute("INSERT INTO tires VALUES ('T-3', 'acme', 3.0, '')")
        _follow_in_background(load_cartridge(cart), SqliteStore(db), PG, tmp_path / "cdc.yaml", "hosted", status, slot=slot, publication=slot, recent=recent)
        for _ in range(100):
            if status.get("events"):
                break
            time.sleep(0.1)
        assert status["ok"] and status["events"] == 1 and SqliteStore(db).entity("hosted/acme", "tire:T-3") is not None
        assert [(e["subject"], e["status"]) for e in recent.of("hosted/acme")] == [("tire:T-3", "applied")]  # the live log shows CDC too
    finally:
        c.execute(f"SELECT pg_drop_replication_slot('{slot}') WHERE EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '{slot}')")
        c.execute(f"DROP PUBLICATION IF EXISTS {slot}")
