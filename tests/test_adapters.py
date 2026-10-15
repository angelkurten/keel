import json
import os
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
    assert r.json() == {"processed": 3, "duplicates": 0, "unhandled": [], "errors": []}
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
    ing = Ingestor(c, store)
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
