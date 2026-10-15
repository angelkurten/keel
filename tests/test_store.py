"""Store conformance: every store answers like the reference one; the SQLite one survives a restart.

Run it against your own store with KEEL_STORE="your_pkg.module:factory", a zero-argument callable
(or class) that returns a fresh, empty store:  KEEL_STORE=keel.store.sqlite:SqliteStore uv run pytest tests/test_store.py
"""

import importlib
import json
import os
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.cli import app
from keel.ingest import JsonlAdapter

FLEET, EVENTS, TENANT = "cartridges/fleet", "examples/events/fleet.jsonl", "acme-logistics"
UTC = timezone.utc


def _strip(o):  # recorded_at and as_of are transaction time: they differ between runs by design
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in ("recorded_at", "as_of", "closed_at", "watermark")}
    if isinstance(o, list):
        return sorted((_strip(v) for v in o), key=lambda v: json.dumps(v, sort_keys=True))
    return o


EXTERNAL = os.environ.get("KEEL_STORE")  # "pkg.module:factory"
MYSQL = os.environ.get("KEEL_MYSQL_URL")  # mysql://root:keel@127.0.0.1:3306/keel, see CONTRIBUTING


@pytest.fixture(params=["memory", "sqlite"] + (["mysql"] if MYSQL else []) + (["external"] if EXTERNAL else []))
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "keel.db")
    if request.param == "mysql":
        from keel.store.mysql import MysqlStore

        s = MysqlStore.from_url(MYSQL)
        for table in ("entities", "relations", "facts", "ingested"):
            s._exec(f"DELETE FROM {table}")  # a clean database per test
        return s
    module, _, factory = EXTERNAL.partition(":")
    return getattr(importlib.import_module(module), factory)()


def test_stores_answer_identically(store):
    c = load_cartridge(FLEET)
    reference = MemoryStore()
    for s in (reference, store):
        assert not Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True).errors
    a, b = Tools(c, reference, TENANT), Tools(c, store, TENANT)
    for subject in ("unit:MXK-4821", "tire:T-9001", "ticket:TK-3608", "document:POL-7731", "inspection:INS-4821-10"):
        assert _strip(a.context(subject)) == _strip(b.context(subject))
        assert _strip(a.history(subject)) == _strip(b.history(subject))
        assert _strip(a.related(subject)) == _strip(b.related(subject))
    assert _strip(a.search("T-9001")) == _strip(b.search("T-9001")) != []
    from dataclasses import asdict

    from keel.tools import _ser

    dumps = [_strip([_ser(asdict(o)) for o in s.dump(TENANT)]) for s in (reference, store)]
    assert dumps[0] == dumps[1] and len(dumps[0]) > 20


def test_invariants_hold_in_every_store(store):
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    assert not ing.ingest(JsonlAdapter(EVENTS), strict=True).errors
    t = Tools(c, store, TENANT)
    depths = t.history("tire:T-9001", predicate="depth_mm")
    assert [d["value"] for d in depths] == [5.2, 2.6] and depths[0]["valid_to"] == depths[1]["valid_from"]
    at_sep = store.facts(TENANT, "tire:T-9001", at=datetime(2026, 9, 20, tzinfo=UTC))
    assert [f.value for f in at_sep if f.predicate == "depth_mm"] == [5.2]
    assert store.facts("someone-else", "tire:T-9001") == [] and store.entity("someone-else", "tire:T-9001") is None
    with pytest.raises(PermissionError):
        t.annotate("tire:T-9001", "assessment", "critical", scope="tenant")
    note = t.annotate("tire:T-9001", "assessment", "critical", scope="agent", by="agent:x")
    assert note["expires_at"] is not None
    assert store.expire(TENANT, now=datetime.fromisoformat(note["expires_at"])) == 1
    assert all(f["predicate"] != "assessment" for f in t.history("tire:T-9001"))
    before = _strip(t.context("unit:MXK-4821"))
    ing.replay(TENANT, events=JsonlAdapter(EVENTS))  # the external log rebuilds the subgraph
    assert _strip(t.context("unit:MXK-4821")) == before


def test_sqlite_store_survives_a_restart(tmp_path):
    path = tmp_path / "keel.db"
    c = load_cartridge(FLEET)
    Ingestor(c, SqliteStore(path)).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, SqliteStore(path), TENANT)  # a new process: no ingest, no in-memory log
    ctx = t.context("tire:T-9001")
    assert ctx["evaluation"] == {"tire_critical": True} and ctx["entity"]["attrs"] == {"position": "P3"}
    assert len(t.history("tire:T-9001", predicate="depth_mm")) == 2


def test_cli_ingests_and_replays_into_a_db(tmp_path):
    db = str(tmp_path / "keel.db")
    res = CliRunner().invoke(app, ["ingest", FLEET, EVENTS, "--db", db])
    assert res.exit_code == 0 and "processed=12" in res.output, res.output
    res = CliRunner().invoke(app, ["replay", FLEET, EVENTS, TENANT, "--db", db])
    assert res.exit_code == 0 and "processed=12" in res.output, res.output
    assert len(SqliteStore(db).facts(TENANT, "tire:T-9001")) == 2  # replayed, not duplicated


def test_cli_expire_drops_expired_memory(tmp_path):
    db = str(tmp_path / "keel.db")
    CliRunner().invoke(app, ["ingest", FLEET, EVENTS, "--db", db])
    store = SqliteStore(db)
    store.open_fact(TENANT, "tire:T-9001", "note", "old", valid_from=datetime(2026, 9, 1, tzinfo=UTC), scope="agent", expires_at=datetime(2026, 9, 2, tzinfo=UTC))
    assert all(f.predicate != "note" for f in store.facts(TENANT, "tire:T-9001"))  # reads already hide it
    res = CliRunner().invoke(app, ["expire", TENANT, "--db", db])
    assert res.exit_code == 0 and "dropped=1" in res.output, res.output


def test_cli_export_is_the_audit_trail(tmp_path):
    db = str(tmp_path / "keel.db")
    CliRunner().invoke(app, ["ingest", FLEET, EVENTS, "--db", db])
    c = load_cartridge(FLEET)
    Tools(c, SqliteStore(db), TENANT).annotate("tire:T-9001", "assessment", "replace", scope="tenant", evidence="photo:123", by="human:angel")
    out = tmp_path / "audit.jsonl"
    res = CliRunner().invoke(app, ["export", TENANT, "--db", db, "--out", str(out)])
    assert res.exit_code == 0 and "records=" in res.output, res.output
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    kinds = {r["record"] for r in records}
    assert kinds == {"entity", "relation", "fact"}
    facts = [r for r in records if r["record"] == "fact"]
    assert all(r["tenant"] == TENANT and r["evidence"] for r in facts)  # provenance on every fact
    closed = [r for r in facts if r["predicate"] == "depth_mm" and r["valid_to"]]
    assert len(closed) == 1 and closed[0]["value"] == 5.2  # history is exported, not just the present
    human = next(r for r in facts if r["predicate"] == "assessment")
    assert human["corrected_by"] == "human:angel" and human["scope"] == "tenant" and human["source"] == "annotate:human:angel"
    empty = CliRunner().invoke(app, ["export", "someone-else", "--db", db])
    assert empty.exit_code == 0 and empty.output.strip() == ""  # no cross-tenant leak


def test_mcp_serve_reads_and_writes_a_db_file(tmp_path):
    """Regression: MCP servers run tools in worker threads; the SQLite connection must survive that."""
    pytest.importorskip("mcp")
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db = str(tmp_path / "keel.db")
    assert CliRunner().invoke(app, ["ingest", FLEET, EVENTS, "--db", db]).exit_code == 0
    params = StdioServerParameters(command=sys.executable, args=["-m", "keel.cli", "serve", FLEET, "--tenant", TENANT, "--db", db])

    def failed(res):
        return getattr(res, "is_error", None) or getattr(res, "isError", None)

    async def run():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("context", {"subject": "tire:T-9001"})
            assert not failed(res) and json.loads(res.content[0].text)["evaluation"] == {"tire_critical": True}
            res = await s.call_tool("annotate", {"subject": "tire:T-9001", "predicate": "assessment", "value": "critical", "scope": "task", "by": "agent:mcp"})
            assert not failed(res), res.content[0].text

    anyio.run(run)
    assert any(f.predicate == "assessment" for f in SqliteStore(db).facts(TENANT, "tire:T-9001"))  # written through MCP, durable


def test_ingest_is_idempotent_and_order_independent(store):
    """At-least-once delivery: the same batch twice changes nothing; any arrival order gives the same history."""
    c = load_cartridge("cartridges/finance")
    events = list(JsonlAdapter("examples/events/finance.jsonl"))
    ing = Ingestor(c, store)
    assert ing.ingest(events, strict=True).processed == 13
    t = Tools(c, store, "hogar-demo")
    subjects = ("debt:visa", "due:visa-2026-09", "due:visa-2026-10", "due:visa-2026-11", "budget_line:tarjeta")
    once = _strip({s: t.history(s) for s in subjects})
    again = ing.ingest(events, strict=True)
    assert (again.processed, again.duplicates) == (0, 13)
    assert _strip({s: t.history(s) for s in subjects}) == once
    ing.replay("hogar-demo", events=reversed(events))  # oldest last: every fact must still land in its slot
    assert _strip({s: t.history(s) for s in subjects}) == once
    balances = t.history("debt:visa", predicate="balance_cop")
    assert [(f["value"], f["valid_to"] is None) for f in balances] == [(4200000, False), (3870000, True)]
    assert balances[0]["valid_to"] == balances[1]["valid_from"]
    status = t.history("due:visa-2026-10", predicate="status")
    assert [f["value"] for f in status] == ["pending", "paid"] and status[0]["valid_to"] == status[1]["valid_from"]


def test_table_prefix_keeps_a_shared_database_tidy(tmp_path):
    store = SqliteStore(tmp_path / "shared.db", prefix="keel_")
    c = load_cartridge(FLEET)
    assert Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True).processed == 12
    names = {r["name"] for r in store._rows("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    assert {"keel_entities", "keel_relations", "keel_facts", "keel_ingested", "keel_facts_subject"} <= names
    assert not {"entities", "facts"} & names
    assert Tools(c, store, TENANT).context("tire:T-9001")["evaluation"] == {"tire_critical": True}
    assert len(list(store.dump(TENANT))) > 20 and store.expire(TENANT) == 0


def test_a_failed_chunk_is_rolled_back_whole_and_redelivered(store):
    """In strict mode a chunk that raises leaves nothing behind: no facts, no remembered ids."""
    c = load_cartridge(FLEET)
    events = list(JsonlAdapter(EVENTS))
    broken = events[4].model_copy(update={"payload": {k: v for k, v in events[4].payload.items() if k != "depth_mm"}})
    ing = Ingestor(c, store)
    with pytest.raises(KeyError):
        ing.ingest([events[0], events[1], broken], strict=True, batch=10)
    assert not store.seen(TENANT, events[0].id)  # every store forgets the chunk's ids, so the redelivery is not a duplicate
    if not isinstance(store, MemoryStore):  # the SQL stores roll the writes back too
        assert store.entity(TENANT, "unit:MXK-4821") is None
    again = ing.ingest(events, strict=True, batch=10)  # the redelivery
    assert (again.processed, again.duplicates) == (12, 0) and store.entity(TENANT, "unit:MXK-4821") is not None
    assert Tools(c, store, TENANT).context("tire:T-9001")["evaluation"] == {"tire_critical": True}


def test_streams_are_not_buffered_and_bulk_is_batched(tmp_path):
    store = SqliteStore(tmp_path / "keel.db")
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    seen_during = []

    def trickle():
        for ev in JsonlAdapter(EVENTS):
            yield ev
            seen_during.append(store.seen(TENANT, ev.id))  # visible before the next event arrives

    assert ing.ingest(trickle(), strict=True).processed == 12 and all(seen_during)
    assert Ingestor(c, SqliteStore(tmp_path / "bulk.db")).ingest(JsonlAdapter(EVENTS), strict=True, batch=500).processed == 12


def test_batched_ingest_with_repeated_subjects_matches_the_reference(store):
    """A bulk batch repeats subjects, which is what the per-transaction read cache serves; the result
    must equal the reference store's, forwards and shuffled."""
    import random
    from datetime import timedelta

    from keel import Event

    c = load_cartridge(FLEET)
    base = datetime(2026, 1, 1, 8, tzinfo=UTC)
    events = []
    for unit in range(3):
        events.append(Event(tenant=TENANT, type="unit.registered", subject=f"unit:U{unit}", occurred_at=base, source="b", payload={"id": f"U{unit}", "plate": f"P{unit}", "model": "X"}))
        for pos in range(4):
            for k in range(6):
                events.append(Event(tenant=TENANT, type="tire.depth_measured", subject=f"tire:T{unit}-{pos}", occurred_at=base + timedelta(days=7 * k + unit), source="b", payload={"unit_id": f"U{unit}", "position": f"P{pos}", "depth_mm": round(6 - 0.7 * k, 1)}))
    reference = MemoryStore()
    Ingestor(c, reference).ingest(events, strict=True, batch=500)
    assert Ingestor(c, store).ingest(events, strict=True, batch=500).processed == len(events)
    a, b = Tools(c, reference, TENANT), Tools(c, store, TENANT)
    for unit in range(3):
        assert _strip(a.context(f"unit:U{unit}", depth=1)) == _strip(b.context(f"unit:U{unit}", depth=1))
        for pos in range(4):
            assert _strip(a.history(f"tire:T{unit}-{pos}")) == _strip(b.history(f"tire:T{unit}-{pos}"))
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)
    Ingestor(c, store).replay(TENANT, events=shuffled)
    for pos in range(4):
        assert _strip(a.history(f"tire:T1-{pos}")) == _strip(b.history(f"tire:T1-{pos}"))


# ---- 0.9: what agentic workflows demand ---------------------------------------------------


def _tick():
    import time

    time.sleep(0.005)  # distinct transaction times on fast machines
    t = datetime.now(UTC)
    time.sleep(0.005)
    return t


def test_as_known_at_answers_what_the_agent_saw(store):
    c = load_cartridge(FLEET)
    events = list(JsonlAdapter(EVENTS))
    ing = Ingestor(c, store)
    ing.ingest(events[:5], strict=True)  # the tire at 5.2 mm, not yet measured again
    seen_then = _tick()
    ing.ingest(events[5:], strict=True)  # the 2.6 mm measurement arrives later
    t = Tools(c, store, TENANT)
    now = t.context("tire:T-9001")
    assert now["evaluation"] == {"tire_critical": True} and now["watermark"] > seen_then.isoformat()
    then = t.context("tire:T-9001", as_known_at=seen_then)
    assert then["evaluation"] == {"tire_critical": False} and [f["value"] for f in then["facts"]] == [5.2]
    assert then["facts"][0]["valid_to"] is None  # the close was learned later, so it is not applied yet
    assert [f["value"] for f in t.history("tire:T-9001", predicate="depth_mm", as_known_at=seen_then)] == [5.2]
    later = t.context("tire:T-9001", as_of=datetime(2026, 9, 20, tzinfo=UTC), as_known_at=seen_then)
    assert later["as_of"].startswith("2026-09-20") and later["as_known_at"] == seen_then.isoformat()


def test_changes_since_a_watermark_span_the_tenant(store):
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    events = list(JsonlAdapter(EVENTS))
    ing.ingest(events[:6], strict=True)
    t = Tools(c, store, TENANT)
    cursor = t.context("unit:MXK-4821")["watermark"]
    _tick()
    ing.ingest(events[6:], strict=True)
    note = t.annotate("tire:T-9001", "assessment", "replace", scope="task", by="agent:x")
    changed = t.history(changed_since=datetime.fromisoformat(cursor))
    subjects = {f["subject"] for f in changed}
    assert {"tire:T-9001", "inspection:INS-4821-10", "ticket:TK-3608"} <= subjects and "document:POL-7731" not in subjects
    assert changed[-1]["predicate"] == "assessment" and changed[-1]["recorded_at"] == note["recorded_at"]
    closed = [f for f in changed if f["predicate"] == "depth_mm" and f["valid_to"]]
    assert [f["value"] for f in closed] == [5.2]  # a close is a change too
    assert t.history(changed_since=datetime.fromisoformat(t.context("unit:MXK-4821")["watermark"])) == []
    with pytest.raises(ValueError):
        t.history()


def test_task_ids_scope_memory_and_close_with_the_task(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT)
    t.annotate("tire:T-9001", "assessment", "run A says replace", task="run-A")
    t.annotate("tire:T-9002", "assessment", "run A, other tire", task="run-A")
    t.annotate("tire:T-9001", "note", "run B is looking", task="run-B")
    assert [f["value"] for f in t.history("tire:T-9001", task="run-A")] == ["run A says replace"]
    preds = {f["predicate"] for f in t.context("tire:T-9001", task="run-B")["facts"]}
    assert preds == {"depth_mm", "note"}  # tenant facts plus run B's memory, not run A's
    assert store.close_task(TENANT, "run-A") == 2
    assert t.history("tire:T-9001", task="run-A") == [] and t.history("tire:T-9001", task="run-B")


def test_annotate_with_a_key_is_idempotent(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT)
    first = t.annotate("tire:T-9001", "assessment", "replace", task="run-A", key="step-7")
    again = t.annotate("tire:T-9001", "assessment", "replace", task="run-A", key="step-7")  # a durable-execution replay
    assert again == first and len(t.history("tire:T-9001", predicate="assessment")) == 1
    with pytest.raises(ValueError):
        t.annotate("tire:T-9001", "assessment", "keep", task="run-A", key="step-7")


def test_search_enumerates_by_kind_and_rule_with_pages(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    for tire in ("T-9002", "T-9003"):
        store.upsert_entity(TENANT, "tire", tire, position="P1")
    t = Tools(c, store, TENANT)
    assert [e["id"] for e in t.search(kind="tire")] == ["T-9001", "T-9002", "T-9003"]
    page = t.search(kind="tire", limit=2)
    assert [e["id"] for e in page] == ["T-9001", "T-9002"] and [e["id"] for e in t.search(kind="tire", after="tire:T-9002")] == ["T-9003"]
    assert [e["id"] for e in t.search(rule="tire_critical")] == ["T-9001"]  # kind from the rule's applies_to
    assert [e["id"] for e in t.search(rule="unit_grounded")] == ["MXK-4821"]
    assert [e["id"] for e in t.search("9003", kind="tire")] == ["T-9003"]
    with pytest.raises(ValueError):
        t.search(rule="fuel_anomaly")  # no `when`


def test_people_are_principals(store):
    from keel.agents import parse_agents

    c = load_cartridge(FLEET)
    c.agents = parse_agents({"planner-bot": {"writes": ["task"]}, "dispatcher": {"kind": "human", "writes": ["task", "tenant"]}})
    assert c.validate() == []
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    with pytest.raises(PermissionError):
        Tools(c, store, TENANT, agent="planner-bot").annotate("unit:MXK-4821", "approved_plan", "x", scope="tenant", evidence="e")
    f = Tools(c, store, TENANT, agent="dispatcher").annotate("unit:MXK-4821", "approved_plan", "x", scope="tenant", evidence="e")
    assert f["source"] == "annotate:human:dispatcher" and f["corrected_by"] == "human:dispatcher"
    c.agents = parse_agents({"x": {"kind": "robot"}})
    assert any("kind 'robot'" in p for p in c.validate())


def test_old_databases_gain_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE facts (seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,"
        " value TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT, source TEXT,"
        " scope TEXT NOT NULL, corrected_by TEXT, evidence TEXT, expires_at TEXT);"
        "INSERT INTO facts (tenant, subject, predicate, value, valid_from, recorded_at, scope) VALUES"
        " ('t', 'tire:T-1', 'depth_mm', '4.0', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'tenant');"
    )
    conn.commit()
    conn.close()
    store = SqliteStore(path)  # a 0.8 database: the columns are added on open, rows kept
    assert {"task", "idempotency_key", "closed_at"} <= store._columns("facts")
    assert [f.value for f in store.facts("t", "tire:T-1")] == [4.0]
