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

from keel import Ingestor, MemoryStore, Scope, SqliteStore, Tools, load_cartridge
from keel.cli import app
from keel.ingest import JsonlAdapter

FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"
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
        for table in ("entities", "relations", "facts", "ingested", "forgotten"):
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
    assert all(r["key"] == f"{r['kind']}:{r['id']}" for r in records if r["record"] == "entity")
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
    c = load_cartridge("examples/finance")
    events = list(JsonlAdapter("examples/finance/events.jsonl"))
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
    assert again == first and len(t.history("tire:T-9001", predicate="assessment", task="run-A")) == 1
    with pytest.raises(ValueError):
        t.annotate("tire:T-9001", "assessment", "keep", task="run-A", key="step-7")


def test_a_store_refuses_a_key_twice_on_one_subject(store):
    from keel.store.base import DuplicateKey

    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.open_fact(TENANT, "tire:T-1", "plan", "swap", now, scope=Scope.TASK, idempotency_key="k")
    with pytest.raises(DuplicateKey) as dup:
        store.open_fact(TENANT, "tire:T-1", "plan", "swap", now, scope=Scope.TASK, idempotency_key="k")
    assert dup.value.fact.value == first.value == "swap"
    store.open_fact(TENANT, "tire:T-2", "plan", "swap", now, scope=Scope.TASK, idempotency_key="k")  # per subject
    store.open_fact(TENANT, "tire:T-1", "plan", "keep", now, scope=Scope.TASK)  # no key, no limit
    assert [f.value for f in store.facts(TENANT, "tire:T-1") if f.predicate == "plan"] == ["swap", "keep"]


def test_annotate_with_a_key_is_one_write_across_processes(tmp_path, monkeypatch):
    """Two servers on one database: B writes between A's check and A's write. A gets B's fact back."""
    c = load_cartridge(FLEET)
    a, b = SqliteStore(tmp_path / "keel.db"), SqliteStore(tmp_path / "keel.db")
    Ingestor(c, a).ingest(JsonlAdapter(EVENTS), strict=True)
    won = Tools(c, b, TENANT).annotate("tire:T-9001", "assessment", "replace", task="run-A", key="step-7")
    monkeypatch.setattr(a, "facts", lambda *args, **kw: [])  # A's check ran before B's write landed
    ta = Tools(c, a, TENANT)
    assert ta.annotate("tire:T-9001", "assessment", "replace", task="run-A", key="step-7") == won
    with pytest.raises(ValueError):
        ta.annotate("tire:T-9001", "assessment", "keep", task="run-A", key="step-7")
    monkeypatch.undo()
    assert len(Tools(c, a, TENANT).history("tire:T-9001", predicate="assessment", task="run-A")) == 1


def test_a_database_with_a_key_already_written_twice_still_opens(tmp_path):
    path = tmp_path / "keel.db"
    s = SqliteStore(path)
    s._exec("DROP INDEX facts_key")  # as before 0.16.2, when the race could write a key twice
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(2):
        s.open_fact(TENANT, "tire:T-1", "plan", "swap", now, scope=Scope.TASK, idempotency_key="k")
    with pytest.warns(UserWarning, match="facts_key"):
        again = SqliteStore(path)
    assert len(again.facts(TENANT, "tire:T-1")) == 2


@pytest.mark.parametrize("kind", ["sqlite"] + (["mysql"] if MYSQL else []))
def test_processes_opening_one_database_at_once_do_not_fail(kind, tmp_path, monkeypatch):
    """Two processes open a database without the index: both see it missing, the second CREATE fails
    with 'already exists' (MySQL 1061). The loser must carry on, not crash."""
    if kind == "sqlite":
        open_store = lambda: SqliteStore(tmp_path / "keel.db")
    else:
        from keel.store.mysql import MysqlStore

        open_store = lambda: MysqlStore.from_url(MYSQL)
    first = open_store()  # the winner created the index
    real, lied = type(first)._has_index, []

    def has_index(self, table, name):  # what the loser saw just before the winner's CREATE
        if name == "facts_key" and not lied:
            lied.append(name)
            return False
        return real(self, table, name)

    monkeypatch.setattr(type(first), "_has_index", has_index)
    second = open_store()
    assert lied and real(second, "facts", "facts_key")


def test_search_enumerates_by_kind_and_rule_with_pages(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    for tire in ("T-9002", "T-9003"):
        store.upsert_entity(TENANT, "tire", tire, position="P1")
    t = Tools(c, store, TENANT)
    assert [e["id"] for e in t.search(kind="tire")] == ["T-9001", "T-9002", "T-9003"]
    page_keys = [e["key"] for e in t.search(kind="tire", limit=2)]
    assert page_keys == ["tire:T-9001", "tire:T-9002"]  # the key every other call takes, and what `after` expects
    assert [e["key"] for e in t.search(kind="tire", after=page_keys[-1])] == ["tire:T-9003"]
    assert all(e["key"] == f"{e['kind']}:{e['id']}" for e in t.search("T-900"))  # text search too
    assert t.context("unit:MXK-4821")["entity"]["key"] == "unit:MXK-4821"
    assert {n["entity"]["key"] for n in t.context("unit:MXK-4821", depth=1)["neighbors"] if n["entity"]} >= {"tire:T-9001"}
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
    assert {"task", "idempotency_key", "closed_at", "status", "trace"} <= store._columns("facts")
    assert [f.value for f in store.facts("t", "tire:T-1")] == [4.0]


def test_time_arguments_accept_what_the_tools_return(store):
    """Regression (0.9.0): the watermark is ISO text and the spec says to pass it back as is."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    events = list(JsonlAdapter(EVENTS))
    ing.ingest(events[:6], strict=True)
    t = Tools(c, store, TENANT)
    watermark = t.context("unit:MXK-4821")["watermark"]
    assert isinstance(watermark, str)
    _tick()
    ing.ingest(events[6:], strict=True)
    changed = t.history(changed_since=watermark)  # straight from context, no parsing
    assert {"inspection:INS-4821-10", "ticket:TK-3608"} <= {f["subject"] for f in changed}
    assert t.history("tire:T-9001", changed_since=watermark)
    assert t.context("tire:T-9001", as_known_at=watermark)["evaluation"] == {"tire_critical": False}
    assert [f["value"] for f in t.history("tire:T-9001", predicate="depth_mm", as_known_at=watermark)] == [5.2]
    assert t.context("tire:T-9001", as_of="2026-09-20")["evaluation"] == {"tire_critical": False}
    assert t.history("tire:T-9001", since="2026-10-01", until="2026-10-31Z")
    assert t.related("unit:MXK-4821", "mounted_on", as_of="2026-09-05") == []


# ---- 0.10: detail, conflicts, proposals, traces -------------------------------------------


def test_multi_valued_predicates_keep_every_author_and_show_the_conflict(store):
    c = load_cartridge(FLEET)
    c.predicates = {"recommendation": {"multi": True}}
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    tires_bot = Tools(c, store, TENANT, agent="tires-bot")
    compliance_bot = Tools(c, store, TENANT, agent="compliance-bot")
    tires_bot.annotate("unit:MXK-4821", "recommendation", "replace tire", task="r1")
    compliance_bot.annotate("unit:MXK-4821", "recommendation", "renew policy", task="r1")
    tires_bot.annotate("unit:MXK-4821", "recommendation", "replace both tires", task="r1")  # its own second word closes its first
    ctx = Tools(c, store, TENANT).context("unit:MXK-4821", task="r1")  # task memory is read by that task
    open_recs = sorted((f["source"], f["value"]) for f in ctx["facts"] if f["predicate"] == "recommendation")
    assert open_recs == [("annotate:compliance-bot", "renew policy"), ("annotate:tires-bot", "replace both tires")]
    assert sorted(x["value"] for x in ctx["conflicts"]["recommendation"]) == ["renew policy", "replace both tires"]
    plain = Tools(c, store, TENANT)  # `assessment` is not multi: last writer wins, no conflict
    plain.annotate("tire:T-9001", "assessment", "a", by="x")
    plain.annotate("tire:T-9001", "assessment", "b", by="y")
    assert "conflicts" not in plain.context("tire:T-9001")


def test_proposals_wait_for_a_decision(store):
    from keel.agents import parse_agents

    c = load_cartridge(FLEET)
    c.agents = parse_agents({"tires-bot": {"writes": ["task"]}, "dispatcher": {"kind": "human", "writes": ["task", "tenant"]}})
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    bot, person = Tools(c, store, TENANT, agent="tires-bot"), Tools(c, store, TENANT, agent="dispatcher")
    with pytest.raises(PermissionError):
        bot.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence="ev-1")  # it may not decide
    p = bot.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence="ev-1", status="proposed")  # but it may propose
    assert p["status"] == "proposed" and p["source"] == "annotate:tires-bot"
    ctx = person.context("tire:T-9001")
    assert [x["value"] for x in ctx["proposals"]] == ["replace"] and all(f["predicate"] != "condition" for f in ctx["facts"])
    accepted = person.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence=f"proposal by {p['source']}; photo:9")
    after = person.context("tire:T-9001")
    assert "proposals" not in after and [f["value"] for f in after["facts"] if f["predicate"] == "condition"] == ["replace"]
    assert accepted["corrected_by"] == "human:dispatcher"
    bot.annotate("tire:T-9001", "note", "rotate", scope="tenant", evidence="ev-2", status="proposed")
    person.annotate("tire:T-9001", "note", "no", scope="tenant", evidence="not needed", status="rejected")
    final = person.context("tire:T-9001")
    assert "proposals" not in final and all(f["predicate"] != "note" for f in final["facts"])  # rejected: history only
    statuses = [(f["value"], f["status"], f["valid_to"] is not None) for f in person.history("tire:T-9001", predicate="note")]
    assert statuses == [("rotate", "proposed", True), ("no", "rejected", True)]


def test_concise_detail_and_history_limit(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT)
    full = t.context("unit:MXK-4821", depth=1)
    short = t.context("unit:MXK-4821", depth=1, detail="concise")
    assert short["evaluation"] == full["evaluation"] and "rules" not in short
    assert {"relation": "mounted_on", "direction": "in", "subject": "tire:T-9001"} in short["relations"]
    tire = next(n for n in short["neighbors"] if n["subject"] == "tire:T-9001")
    assert tire["facts"] == {"depth_mm": 2.6} and tire["evaluation"] == {"tire_critical": True}
    assert len(json.dumps(short)) * 2.5 < len(json.dumps(full))  # well under half the size
    assert [f["value"] for f in t.history("tire:T-9001", predicate="depth_mm", limit=1)] == [2.6]  # the most recent
    with pytest.raises(ValueError):
        t.context("unit:MXK-4821", detail="verbose")


def test_writes_carry_the_callers_trace(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    Tools(c, store, TENANT, trace=f"{tp}; tool_call_id=call_7").annotate("tire:T-9001", "assessment", "replace")
    exported = [o for o in store.dump(TENANT) if getattr(o, "predicate", None) == "assessment"]
    assert exported[0].trace == f"{tp}; tool_call_id=call_7"


def test_multi_valued_authors_are_the_by_when_given(store):
    """Feedback: two people writing through one agent were one author (same source) and overwrote each other."""
    c = load_cartridge(FLEET)
    c.predicates = {"diagnosis": {"multi": True}}
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    console = Tools(c, store, TENANT, agent="dev-console")
    console.annotate("unit:MXK-4821", "diagnosis", "alternator", by="human:ana")
    console.annotate("unit:MXK-4821", "diagnosis", "battery", by="human:luis")
    console.annotate("unit:MXK-4821", "diagnosis", "alternator belt", by="human:ana")  # ana revises her own
    ctx = console.context("unit:MXK-4821")
    open_ = sorted((f["corrected_by"], f["value"]) for f in ctx["facts"] if f["predicate"] == "diagnosis")
    assert open_ == [("human:ana", "alternator belt"), ("human:luis", "battery")]
    assert sorted((x["by"], x["value"]) for x in ctx["conflicts"]["diagnosis"]) == open_
    short = console.context("unit:MXK-4821", detail="concise")["facts"]["diagnosis"]
    assert sorted((x["by"], x["value"]) for x in short) == open_


def test_search_by_neighbour_and_one_format_for_after(store):
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    now = datetime.now(UTC)
    for tid, depth in (("T-9002", 2.1), ("T-9003", 6.0)):
        store.upsert_entity(TENANT, "tire", tid, position="P2")
        store.link(TENANT, f"tire:{tid}", "mounted_on", "unit:MXK-4821", now)
        store.open_fact(TENANT, f"tire:{tid}", "depth_mm", depth, valid_from=now)
    store.upsert_entity(TENANT, "unit", "OTHER-1")
    store.upsert_entity(TENANT, "tire", "T-7777", position="P1")
    store.link(TENANT, "tire:T-7777", "mounted_on", "unit:OTHER-1", now)
    store.open_fact(TENANT, "tire:T-7777", "depth_mm", 1.0, valid_from=now)  # critical, but on another unit
    t = Tools(c, store, TENANT)
    critical_here = t.search(rule="tire_critical", related_to="unit:MXK-4821", via="mounted_on")
    assert [e["key"] for e in critical_here] == ["tire:T-9001", "tire:T-9002"]  # one call; T-7777 is not this unit's
    assert [e["key"] for e in t.search(kind="tire", related_to="unit:MXK-4821")] == ["tire:T-9001", "tire:T-9002", "tire:T-9003"]
    assert {h["subject"] if "subject" in h else h["key"] for h in t.search("2.1", related_to="unit:MXK-4821")} == {"tire:T-9002"}
    with pytest.raises(ValueError):
        t.search(kind="tire", related_to="unit:MXK-4821", via="flies")
    with pytest.raises(ValueError):
        t.search(kind="tire", via="mounted_on")  # via without related_to
    # `after`: the key, or an id when the kind is known; never silently wrong
    assert [e["key"] for e in t.search(kind="tire")] == ["tire:T-7777", "tire:T-9001", "tire:T-9002", "tire:T-9003"]
    by_key = [e["key"] for e in t.search(kind="tire", after="tire:T-9001")]
    assert by_key == [e["key"] for e in t.search(kind="tire", after="T-9001")] == ["tire:T-9002", "tire:T-9003"]
    with pytest.raises(ValueError):
        t.search(kind="tire", after="unit:MXK-4821")  # a key of another kind
    with pytest.raises(ValueError):
        t.search("x", after="tire:T-9001")  # text search does not page by key


# ---- 0.14: consolidation ----------------------------------------------------------------


def _with_notes(store, c, notes, subject="tire:T-9001", predicate="assessment", scope="task"):
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT)
    for value, by in notes:
        t.annotate(subject, predicate, value, scope=scope, by=by, evidence=f"note-by-{by}")
    return t


def test_consolidation_summarizes_episodic_memory_into_a_durable_fact(store):
    from keel.consolidate import consolidate_tenant

    c = load_cartridge(FLEET)
    c.consolidate = {"recurring": {"from": {"predicate": "assessment"}, "applies_to": "tire", "min_count": 3, "reduce": "mode", "into": {"predicate": "recurring_issue"}}}
    assert c.validate() == []
    t = _with_notes(store, c, [("slow leak", "a"), ("ok", "b"), ("slow leak", "c"), ("slow leak", "a")])
    report = consolidate_tenant(c, store, TENANT)
    assert report.written == {"recurring": 1}
    fact = next(f for f in t.context("tire:T-9001")["facts"] if f["predicate"] == "recurring_issue")
    assert fact["value"] == "slow leak" and fact["scope"] == "tenant" and fact["source"] == "consolidate:recurring"
    assert fact["evidence"].startswith("4 assessment facts") and "note-by-a" in fact["evidence"]
    assert len(t.history("tire:T-9001", predicate="assessment")) == 4  # every original stays (consolidation deletes nothing)
    again = consolidate_tenant(c, store, TENANT)
    assert again.written == {"recurring": 0} and again.unchanged == {"recurring": 1}  # idempotent
    t.annotate("tire:T-9001", "assessment", "sidewall bulge", by="d")
    t.annotate("tire:T-9001", "assessment", "sidewall bulge", by="e")
    t.annotate("tire:T-9001", "assessment", "sidewall bulge", by="f")
    assert consolidate_tenant(c, store, TENANT).written == {"recurring": 1}  # new inputs: a new value closes the old
    values = [f["value"] for f in t.history("tire:T-9001", predicate="recurring_issue")]
    assert values[-1] == "sidewall bulge" and values[0] == "slow leak"


def test_consolidation_reducers_window_and_thresholds(store):
    from datetime import timedelta

    from keel.consolidate import JoinSummarizer, consolidate_tenant

    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    now = datetime.now(UTC)
    for i, v in enumerate([3.1, 2.9, 2.4]):
        store.open_fact(TENANT, "tire:T-9001", "reading_mm", v, valid_from=now - timedelta(hours=3 - i), scope=Scope.TASK, recorded_at=now - timedelta(hours=3 - i), source=f"annotate:probe{i}")
    store.open_fact(TENANT, "tire:T-9001", "reading_mm", 9.9, valid_from=now - timedelta(days=60), scope=Scope.TASK, recorded_at=now - timedelta(days=60), source="annotate:old")
    base = {"from": {"predicate": "reading_mm"}, "within": "7d", "min_count": 2}
    c.consolidate = {r: {**base, "reduce": r, "into": {"predicate": f"reading_{r}"}} for r in ("latest", "count", "min", "max", "avg", "distinct", "summary")}
    consolidate_tenant(c, store, TENANT, summarizer=JoinSummarizer())
    got = {f.predicate: f.value for f in store.facts(TENANT, "tire:T-9001") if f.predicate.startswith("reading_") and f.predicate != "reading_mm"}
    assert got == {"reading_latest": 2.4, "reading_count": 3, "reading_min": 2.4, "reading_max": 3.1, "reading_avg": 2.8,
                   "reading_distinct": [3.1, 2.9, 2.4], "reading_summary": "3.1; 2.9; 2.4"}  # the 60-day-old 9.9 is outside the window
    c.consolidate = {"strict": {**base, "min_count": 5, "reduce": "count", "into": {"predicate": "reading_strict"}}}
    assert consolidate_tenant(c, store, TENANT).written == {"strict": 0}
    c.consolidate = {"needs": {**base, "reduce": "summary", "into": {"predicate": "reading_x"}}}
    with pytest.raises(ValueError, match="summarizer"):
        consolidate_tenant(c, store, TENANT)


def test_consolidation_validation(store):
    c = load_cartridge(FLEET)
    c.consolidate = {"bad": {"from": {"predicate": "a", "scopes": ["galaxy"]}, "reduce": "median", "into": {"predicate": "a", "scope": "x"}, "applies_to": "spaceship"}}
    msgs = c.validate()
    for expected in ("reduce is one of", "unknown scope 'galaxy'", "unknown scope 'x'", "applies_to 'spaceship'", "must differ"):
        assert any(expected in m for m in msgs), msgs


def test_expire_consolidates_first_so_nothing_is_dropped_unsummarized(tmp_path):
    import shutil

    cart = tmp_path / "fleet"
    shutil.copytree(FLEET, cart)
    with (cart / "cartridge.yaml").open("a", encoding="utf-8") as fh:
        fh.write("\nconsolidate:\n  recurring:\n    from: { predicate: assessment, scopes: [agent] }\n    min_count: 2\n    reduce: mode\n    into: { predicate: recurring_issue }\n")
    db = str(tmp_path / "keel.db")
    CliRunner().invoke(app, ["ingest", str(cart), EVENTS, "--db", db])
    store = SqliteStore(db)
    past = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(2):  # agent memory that has already expired, not yet dropped
        store.open_fact(TENANT, "tire:T-9001", "assessment", "slow leak", valid_from=past, scope=Scope.AGENT, recorded_at=past, expires_at=past, source=f"annotate:a{i}", corrected_by=f"a{i}")
    res = CliRunner().invoke(app, ["expire", TENANT, "--db", db, "--consolidate", str(cart)])
    assert res.exit_code == 0 and "consolidated recurring: written=1" in res.output and "dropped=2" in res.output, res.output
    assert [f.value for f in SqliteStore(db).facts(TENANT, "tire:T-9001") if f.predicate == "recurring_issue"] == ["slow leak"]
    res = CliRunner().invoke(app, ["consolidate", str(cart), "--tenant", TENANT, "--db", db])
    assert res.exit_code == 0 and "written=0" in res.output, res.output


@pytest.mark.skipif(not os.environ.get("KEEL_SUMMARIZER_API_KEY"), reason="set KEEL_SUMMARIZER_API_KEY/_BASE_URL/_MODEL for a live summarizer")
def test_live_llm_summarizer(store):
    from keel.consolidate import consolidate_tenant, load_summarizer

    c = load_cartridge(FLEET)
    c.consolidate = {"story": {"from": {"predicate": "assessment"}, "min_count": 3, "reduce": "summary", "into": {"predicate": "assessment_summary"}}}
    _with_notes(store, c, [("pressure dropped 0.4 bar since Monday", "a"), ("pressure low again, refilled", "b"), ("slow leak suspected near the valve", "c")])
    consolidate_tenant(c, store, TENANT, summarizer=load_summarizer("llm"))
    text = next(f.value for f in store.facts(TENANT, "tire:T-9001") if f.predicate == "assessment_summary")
    assert isinstance(text, str) and 3 < len(text.split()) <= 40 and any(w in text.lower() for w in ("leak", "pressure"))


def test_memory_scopes_are_enforced_on_read(store):
    """SPEC §6: agent memory is its author's, task memory its task's, global memory every tenant's."""
    from pydantic import ValidationError

    from keel import Event
    from keel.scopes import GLOBAL_TENANT

    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    a, b, nobody = Tools(c, store, TENANT, agent="bot-a"), Tools(c, store, TENANT, agent="bot-b"), Tools(c, store, TENANT)
    preds = lambda t, **kw: {f["predicate"] for f in t.context("tire:T-9001", **kw)["facts"]}

    a.annotate("tire:T-9001", "scratch", "a thinks aloud", scope="agent")
    assert "scratch" in preds(a) and "scratch" not in preds(b) and "scratch" not in preds(nobody)
    assert all(f["predicate"] != "scratch" for f in b.history("tire:T-9001"))
    assert all(f["predicate"] != "scratch" for f in b.history(changed_since="2000-01-01T00:00:00Z"))
    assert a.search("a thinks aloud") and not b.search("a thinks aloud")

    a.annotate("tire:T-9001", "note", "for run 1", task="run-1")
    a.annotate("tire:T-9001", "loose", "tied to no task")  # task scope without a task id: the tenant's
    assert "note" in preds(b, task="run-1") and "note" not in preds(b) and "note" not in preds(b, task="run-2")
    assert "loose" in preds(b) and "loose" in preds(b, task="run-2")

    nobody.annotate("tire:T-9001", "recall", "model recalled", scope="global", evidence="maker-bulletin-7")
    other = Tools(c, store, "another-tenant")
    assert all(f.predicate != "recall" for f in store.facts(TENANT, "tire:T-9001"))  # not in any tenant's partition
    assert "recall" in preds(a) and "recall" in {f["predicate"] for f in other.context("tire:T-9001")["facts"]}
    assert "recall" in [f["predicate"] for f in other.history(changed_since="2000-01-01T00:00:00Z")]
    assert other.context("tire:T-9001")["watermark"] is not None
    with pytest.raises(ValueError):
        Tools(c, store, GLOBAL_TENANT)
    with pytest.raises(ValidationError):
        Event(tenant=GLOBAL_TENANT, type="x.y", subject="tire:1", occurred_at=datetime.now(timezone.utc), source="s")


def test_scopes_never_close_each_other(store):
    """kagent (5): an evaluator's private note closed its shared task diagnosis on the same multi predicate."""
    c = load_cartridge(FLEET)
    c.predicates = {"diagnosis": {"multi": True}}
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    ev, alexa = Tools(c, store, TENANT, agent="evaluator"), Tools(c, store, TENANT, agent="alexa")
    ev.annotate("unit:MXK-4821", "diagnosis", "alternator", task="CUS-1998")
    alexa.annotate("unit:MXK-4821", "diagnosis", "battery", task="CUS-1998")
    ev.annotate("unit:MXK-4821", "diagnosis", "I doubt my own call", scope="agent")  # private
    dev = Tools(c, store, TENANT, agent="dev")
    ctx = dev.context("unit:MXK-4821", task="CUS-1998")
    assert sorted(f["value"] for f in ctx["facts"] if f["predicate"] == "diagnosis") == ["alternator", "battery"]
    assert "diagnosis" in ctx["conflicts"]
    ev.annotate("unit:MXK-4821", "diagnosis", "alternator, again", task="CUS-2001")  # another task keeps its own slot
    assert sorted(f["value"] for f in dev.context("unit:MXK-4821", task="CUS-1998")["facts"] if f["predicate"] == "diagnosis") == ["alternator", "battery"]
    # not multi: a private note no longer ends the tenant's truth, nor one agent's note another's
    ev.annotate("tire:T-9001", "depth_mm", 1.0, scope="agent")
    alexa.annotate("tire:T-9001", "depth_mm", 9.0, scope="agent")
    assert [f["value"] for f in dev.context("tire:T-9001")["facts"] if f["predicate"] == "depth_mm"] == [2.6]
    assert [f["value"] for f in ev.context("tire:T-9001")["facts"] if f["predicate"] == "depth_mm" and f["scope"] == "agent"] == [1.0]
    # a promotion leaves the narrower fact to its own lifetime
    ev.annotate("tire:T-9001", "assessment", "replace", task="t1")
    Tools(c, store, TENANT).annotate("tire:T-9001", "assessment", "replace", scope="tenant", evidence="photo:9")
    assert len([f for f in ev.history("tire:T-9001", predicate="assessment", task="t1") if f["valid_to"] is None]) == 1


def test_consolidation_keeps_agent_memory_private_and_uses_world_time(store):
    """kagent (5): private notes were promoted by default, and within/evidence used the ingest time."""
    from datetime import timedelta

    from keel.consolidate import consolidate_tenant

    c = load_cartridge(FLEET)
    c.consolidate = {"recurring": {"from": {"predicate": "assessment"}, "within": "30d", "min_count": 2, "reduce": "count", "into": {"predicate": "times"}}}
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    now = datetime(2026, 10, 15, tzinfo=UTC)
    for i, days in enumerate((60, 10, 5)):  # one outside the 30-day window by world time, all recorded today
        store.open_fact(TENANT, "tire:T-9001", "assessment", "leak", valid_from=now - timedelta(days=days), scope=Scope.TASK, task="r", source=f"annotate:a{i}", corrected_by=f"a{i}", recorded_at=now)
    for i in range(3):
        store.open_fact(TENANT, "tire:T-9001", "assessment", "private", valid_from=now - timedelta(days=1), scope=Scope.AGENT, source=f"annotate:p{i}", recorded_at=now)
    consolidate_tenant(c, store, TENANT, now=now)
    (fact,) = [f for f in store.facts(TENANT, "tire:T-9001") if f.predicate == "times"]
    assert fact.value == 2  # the two task notes in the window; agent memory not read unless listed
    assert fact.evidence.startswith(f"2 assessment facts, {(now - timedelta(days=10)).isoformat()} to {(now - timedelta(days=5)).isoformat()}")
    c.consolidate["recurring"]["from"]["scopes"] = ["task", "agent"]  # asked for explicitly
    consolidate_tenant(c, store, TENANT, now=now)
    assert [f.value for f in store.facts(TENANT, "tire:T-9001", at=now) if f.predicate == "times"] == [5]


def test_close_fact_closes_tenant_facts_only_unless_asked(store):
    """A handler's close_fact ends the tenant's truth, never an agent's or a task's memory."""
    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT, agent="bot")
    t.annotate("tire:T-9001", "depth_mm", 1.0, scope="agent")
    t.annotate("tire:T-9001", "depth_mm", 1.5, task="r")
    now = datetime.now(UTC)
    open_ = lambda: sorted((Scope(f.scope).value, f.value) for f in store.facts(TENANT, "tire:T-9001", at=now) if f.predicate == "depth_mm")
    assert open_() == [("agent", 1.0), ("task", 1.5), ("tenant", 2.6)]
    store.close_fact(TENANT, "tire:T-9001", "depth_mm", valid_to=now)  # what a handler does
    assert open_() == [("agent", 1.0), ("task", 1.5)]
    store.close_fact(TENANT, "tire:T-9001", "depth_mm", valid_to=now, scope=None)  # every scope, when asked
    assert open_() == []


def test_close_fact_scope_in_a_bulk_batch(store):
    if not hasattr(store, "bulk"):
        pytest.skip("store has no bulk path")
    from keel import Event

    c = load_cartridge(FLEET)
    Ingestor(c, store).ingest(JsonlAdapter(EVENTS), strict=True)
    Tools(c, store, TENANT, agent="bot").annotate("tire:T-9001", "depth_mm", 1.0, scope="agent")
    now = datetime.now(UTC)
    ev = [Event(tenant=TENANT, type="tire.depth_measured", subject="tire:T-9001", occurred_at=now, source="t", payload={"unit_id": "MXK-4821", "position": "P3", "depth_mm": 2.0})] * 2
    with store.bulk(ev):
        store.close_fact(TENANT, "tire:T-9001", "depth_mm", valid_to=now)
    vals = sorted((Scope(f.scope).value, f.value) for f in store.facts(TENANT, "tire:T-9001", at=now) if f.predicate == "depth_mm")
    assert vals == [("agent", 1.0)]


def test_replay_keeps_what_agents_and_people_wrote(store):
    """Annotations are earned memory, not derived from events: replay rebuilds around them."""
    c = load_cartridge(FLEET)
    ing = Ingestor(c, store)
    ing.ingest(JsonlAdapter(EVENTS), strict=True)
    t = Tools(c, store, TENANT, agent="bot")
    t.annotate("tire:T-9001", "assessment", "replace", task="run-1")
    t.annotate("tire:T-9001", "scratch", "thinking", scope="agent")
    t.annotate("tire:T-9001", "approved", "replace", scope="tenant", evidence="human ok")
    t.annotate("tire:T-9001", "depth_mm", 2.4, scope="tenant", evidence="re-measured")  # a correction of an event fact
    view = lambda: (_strip(t.context("tire:T-9001", task="run-1")), sorted((f["predicate"], str(f["value"]), f["valid_to"] is None) for f in t.history("tire:T-9001")))
    before = view()
    report = ing.replay(TENANT, events=JsonlAdapter(EVENTS))
    assert report.processed > 0 and view() == before
    assert [f["value"] for f in t.context("tire:T-9001")["facts"] if f["predicate"] == "depth_mm"] == [2.4]  # the correction still wins


def test_sqlite_reads_by_key_never_scan_the_tenant(tmp_path):
    """No statistics (a fresh database never ran ANALYZE): every branch must still seek its own index."""
    from keel.store.sql import CHANGES, RELATIONS_OF

    store = SqliteStore(tmp_path / "plan.db")
    for sql in (RELATIONS_OF, CHANGES):
        plan = " | ".join(r["detail"] for r in store._rows(f"EXPLAIN QUERY PLAN {sql}", ("t", "x", "t", "x")))
        assert plan.count("SEARCH") == 2 and "(tenant=?)" not in plan, plan
    # the watermark: one MAX per index, answered from the index alone
    for sql in ("SELECT (SELECT MAX(recorded_at) FROM facts WHERE tenant = ?) AS r, (SELECT MAX(closed_at) FROM facts WHERE tenant = ?) AS c",
                "SELECT MAX(recorded_at) AS r FROM relations WHERE tenant = ?"):
        details = [r["detail"] for r in store._rows(f"EXPLAIN QUERY PLAN {sql}", ("t",) * sql.count("?"))]
        assert all("COVERING INDEX" in d for d in details if d.startswith("SEARCH")) and any(d.startswith("SEARCH") for d in details), details


def _bulk_history(store):
    """Three depth readings, the last one open; then a batch that starts after all of them."""
    from datetime import timedelta

    from keel import Event

    base = datetime(2026, 1, 1, tzinfo=UTC)
    for k in range(3):
        store.open_fact(TENANT, "tire:T-1", "depth_mm", 8 - k, valid_from=base + timedelta(days=k), source="b")
    later = Event(tenant=TENANT, type="x.y", subject="tire:T-1", occurred_at=base + timedelta(days=10), source="b")
    return base, later


def test_bulk_writes_before_the_batch_still_land_in_their_slot(store):
    """Prefetch skips history the batch cannot change; a write before the batch's earliest event, or
    a read of the facts, must still see all of it, and give what the reference store gives."""
    from datetime import timedelta

    if not hasattr(store, "bulk"):
        pytest.skip("no bulk path")
    reference = MemoryStore()
    base, later = _bulk_history(store)
    _bulk_history(reference)
    with store.bulk([later]):
        assert len(store.facts(TENANT, "tire:T-1")) == 3  # a read inside the batch sees the whole history
        store.open_fact(TENANT, "tire:T-1", "depth_mm", 7.5, valid_from=base + timedelta(hours=12), source="b")
    reference.open_fact(TENANT, "tire:T-1", "depth_mm", 7.5, valid_from=base + timedelta(hours=12), source="b")
    with store.bulk([later]):  # no read first: the early write alone must load what prefetch skipped
        store.open_fact(TENANT, "tire:T-1", "depth_mm", 7.8, valid_from=base + timedelta(hours=6), source="b")
    reference.open_fact(TENANT, "tire:T-1", "depth_mm", 7.8, valid_from=base + timedelta(hours=6), source="b")
    slots = lambda s: sorted((f.value, f.valid_from, f.valid_to) for f in s.facts(TENANT, "tire:T-1"))
    assert slots(store) == slots(reference)


def test_bulk_prefetch_reads_only_what_the_batch_can_change(tmp_path):
    """A subject's closed history is not read per batch: only its open fact is."""
    store = SqliteStore(tmp_path / "prefetch.db")
    _, later = _bulk_history(store)
    with store.bulk([later]):
        assert [f.value for f, _ in store._bulk.facts_by[(TENANT, "tire:T-1")]] == [6]


def test_sqlite_caches_pages_in_process(tmp_path):
    store = SqliteStore(tmp_path / "cache.db")
    assert store._one("PRAGMA cache_size")["cache_size"] == -262144


def _unit_with_tires(store, tires):
    from datetime import timedelta

    from keel import Event

    c = load_cartridge(FLEET)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    evs = [Event(tenant=TENANT, type="unit.registered", subject="unit:U1", occurred_at=base, source="b", payload={"id": "U1", "plate": "P", "model": "X"})]
    for n in range(tires):
        evs.append(Event(tenant=TENANT, type="tire.depth_measured", subject=f"tire:T{n}", occurred_at=base + timedelta(hours=n), source="b", payload={"unit_id": "U1", "position": f"P{n}", "depth_mm": 2.0 + n % 5}))
    Ingestor(c, store).ingest(evs, strict=True)
    return Tools(c, store, TENANT)


def test_neighbourhoods_and_rule_sweeps_read_in_batches_with_the_same_answer(store):
    """context(depth=1) and search(rule=) read many subjects at once on a store with facts_of; the
    answer is the reference store's, which reads one subject at a time."""
    a, b = _unit_with_tires(MemoryStore(), 12), _unit_with_tires(store, 12)
    assert _strip(a.context("unit:U1", depth=1)) == _strip(b.context("unit:U1", depth=1))
    for rule in ("tire_critical", "unit_grounded"):
        assert _strip(a.search(rule=rule)) == _strip(b.search(rule=rule)) != []
    assert _strip(a.search(rule="tire_critical", limit=2, after="tire:T0")) == _strip(b.search(rule="tire_critical", limit=2, after="tire:T0"))


def test_reads_do_not_grow_with_the_neighbourhood(tmp_path):
    """No N+1: on a networked database every statement is a round trip."""

    def statements(tires, call):
        store = SqliteStore(tmp_path / f"n{tires}.db")
        tools, n, orig = _unit_with_tires(store, tires), [0], store._rows
        store._rows = lambda sql, params=(): (n.__setitem__(0, n[0] + 1), orig(sql, params))[1]
        call(tools)
        return n[0]

    for call in (lambda t: t.context("unit:U1", depth=1), lambda t: t.search(rule="tire_critical"), lambda t: t.search(rule="unit_grounded")):
        assert statements(20, call) == statements(40, call)


def test_a_write_from_another_thread_never_joins_a_batch_in_flight(store):
    """The HTTP server serves requests on a thread pool: an annotate that arrives while another thread
    ingests a batch must neither land in that batch (and be lost when it rolls back) nor read it."""
    import threading
    from datetime import timedelta

    from keel import Event

    if not hasattr(store, "bulk"):
        pytest.skip("no bulk path")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    store.upsert_entity(TENANT, "unit", "U1")
    ev = Event(tenant=TENANT, type="x.y", subject="unit:U1", occurred_at=base, source="b")
    inside, seen = threading.Event(), {}

    def other():
        inside.wait()
        seen["batch_entity"] = store.entity(TENANT, "unit:U2")  # written by the batch, not committed
        store.open_fact(TENANT, "unit:U1", "note", "from another thread", valid_from=base + timedelta(days=1), source="b")

    t = threading.Thread(target=other)
    t.start()
    with pytest.raises(RuntimeError), store.bulk([ev]):
        store.upsert_entity(TENANT, "unit", "U2")
        inside.set()
        t.join(0.5)  # the other thread waits for the batch, or (the bug) writes into it
        raise RuntimeError("batch fails: rolled back")
    t.join()
    assert seen["batch_entity"] is None
    assert [f.value for f in store.facts(TENANT, "unit:U1") if f.predicate == "note"] == ["from another thread"]


def test_reads_on_a_networked_store_never_queue_behind_a_transaction(store):
    """PER_THREAD_READS: a server thread reads on its own connection while another thread holds a transaction."""
    import threading

    if type(store).__name__ != "MysqlStore":
        pytest.skip("a networked store")
    store.upsert_entity(TENANT, "unit", "U1")
    got = {}
    reader = threading.Thread(target=lambda: got.setdefault("entity", store.entity(TENANT, "unit:U1")))
    with store.transaction():
        store.upsert_entity(TENANT, "unit", "U2")
        reader.start()
        reader.join(5)
        assert not reader.is_alive(), "the read waited for the transaction"
    assert got["entity"] is not None and got["entity"].id == "U1"


def test_a_rule_sweep_that_follows_relations_does_not_grow_with_the_kind(tmp_path):
    from datetime import timedelta

    from keel import Event

    def statements(units):
        store, c = SqliteStore(tmp_path / f"u{units}.db"), load_cartridge(FLEET)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        evs = []
        for u in range(units):
            evs.append(Event(tenant=TENANT, type="unit.registered", subject=f"unit:U{u}", occurred_at=base, source="b", payload={"id": f"U{u}", "plate": "P", "model": "X"}))
            evs.append(Event(tenant=TENANT, type="tire.depth_measured", subject=f"tire:T{u}", occurred_at=base + timedelta(hours=1), source="b", payload={"unit_id": f"U{u}", "position": "P1", "depth_mm": 2.0 + u % 3}))
        Ingestor(c, store).ingest(evs, strict=True)
        n, orig = [0], store._rows
        store._rows = lambda sql, params=(): (n.__setitem__(0, n[0] + 1), orig(sql, params))[1]
        found = Tools(c, store, TENANT).search(rule="unit_grounded")
        assert len(found) == len([u for u in range(units) if u % 3 == 0])
        return n[0]

    assert statements(10) == statements(30)


def test_the_state_now_reads_only_open_facts(tmp_path):
    """A subject's history keeps growing; what holds now is a few rows, read through facts_open."""
    store, base = SqliteStore(tmp_path / "open.db"), datetime(2026, 1, 1, tzinfo=UTC)
    from datetime import timedelta

    for k in range(30):
        store.open_fact(TENANT, "tire:T-1", "depth_mm", 10 - k * 0.2, valid_from=base + timedelta(days=k), source="b")
    fetched, orig = [], store._rows

    def rows(sql, params=()):
        r = orig(sql, params)
        fetched.append(len(r))
        return r

    store._rows = rows
    now = base + timedelta(days=100)
    assert [f.value for f in store.facts(TENANT, "tire:T-1", at=now)] == [pytest.approx(4.2)]
    assert fetched == [1]
    later = datetime(2100, 1, 1, tzinfo=UTC)
    assert len(store.facts(TENANT, "tire:T-1", at=now, known_at=later)) == 1  # known_at reads the history, same answer
    assert len(store.facts_of(TENANT, ["tire:T-1"], at=base + timedelta(days=5, hours=1))["tire:T-1"]) == 1


def _forgetting_fleet():
    c = load_cartridge(FLEET)
    c.events["fleet.closed"] = {"subject": "unit", "forgets": "tenant"}  # the customer deleted their account
    return c


def test_forget_erases_one_tenant_for_good(store):
    from keel.envelope import Event

    c = _forgetting_fleet()
    log = list(JsonlAdapter(EVENTS))
    other = [e.model_copy(update={"tenant": "beta"}) for e in log]
    assert not Ingestor(c, store).ingest(log + other, strict=True).errors
    Tools(c, store, TENANT).annotate("unit:MXK-4821", "note", "keep the spare", scope="tenant", evidence="call:1")
    Tools(c, store, TENANT).annotate("unit:MXK-4821", "fleet_rule", "x", scope="global", evidence="doc:1")
    beta = len(list(store.dump("beta")))
    closed = Event(tenant=TENANT, type="fleet.closed", subject="unit:MXK-4821", occurred_at=datetime(2026, 10, 1, tzinfo=UTC), source="t")
    # one call: the erasure in the middle of a batch, then an old redelivery that must not revive it
    report = Ingestor(c, store).ingest([closed] + log, strict=True, batch=5000)
    (tenant, counts), = report.forgotten
    assert tenant == TENANT and counts["annotations"] == 1 and counts["ingested"] == len(log)
    assert all(counts[k] > 0 for k in ("entities", "relations", "facts"))
    assert report.skipped == len(log) and report.processed == 1
    assert list(store.dump(TENANT)) == [] and store.forgotten(TENANT) is not None
    assert len(list(store.dump("beta"))) == beta > 0  # the other tenant is untouched
    assert any(f.predicate == "fleet_rule" for f in store.facts("__global__", "unit:MXK-4821"))  # every tenant's, kept
    at = store.forgotten(TENANT)
    assert store.forget(TENANT) == dict.fromkeys(("entities", "relations", "facts", "annotations", "ingested"), 0)  # idempotent
    assert store.forgotten(TENANT) == at and TENANT in store.forgotten_tenants()
    with pytest.raises(PermissionError):
        Tools(c, store, TENANT).annotate("unit:MXK-4821", "note", "late", scope="tenant", evidence="call:2")
    assert Ingestor(c, store).replay(TENANT, events=log).skipped == len(log) and list(store.dump(TENANT)) == []
    with pytest.raises(ValueError):
        store.forget("__global__")


def test_forget_events_need_no_handler_and_validate():
    c = _forgetting_fleet()
    assert c.validate() == []
    c.events["fleet.closed"]["forgets"] = "subject"
    assert any("forgets is `tenant`" in p for p in c.validate())
    c.events["unit.registered"]["forgets"] = "tenant"
    assert any("would never run" in p for p in c.validate())


def test_forget_cli_and_the_index(tmp_path):
    from keel.semantic import HashEmbedder, MemoryIndex, index_tenant

    db = str(tmp_path / "keel.db")
    run = CliRunner().invoke
    assert run(app, ["ingest", FLEET, EVENTS, "--db", db]).exit_code == 0
    assert run(app, ["forget", TENANT, "--db", db]).exit_code != 0  # --yes is required
    out = run(app, ["forget", TENANT, "--db", db, "--yes", "--index", "memory"])
    assert out.exit_code == 0, out.output
    got = json.loads(out.output)
    assert got["tenant"] == TENANT and got["entities"] > 0 and got["index"] is True
    index = MemoryIndex()
    index.upsert(TENANT, [("unit:MXK-4821", [1.0, 0.0], "a unit")])
    index_tenant(SqliteStore(db), TENANT, HashEmbedder(), index)  # a forgotten tenant's documents go
    assert index.query(TENANT, [1.0, 0.0], 5) == []


def test_jobs_sweep_every_tenant(tmp_path):
    db, cursors = str(tmp_path / "keel.db"), tmp_path / "cursors"
    store = SqliteStore(db)
    log = list(JsonlAdapter(EVENTS))
    Ingestor(load_cartridge(FLEET), store).ingest(log + [e.model_copy(update={"tenant": "beta"}) for e in log], strict=True)
    assert store.tenants() == [TENANT, "beta"]
    run = lambda *a: CliRunner().invoke(app, list(a))
    out = run("expire", "--all-tenants", "--db", db).output
    assert f"tenant={TENANT}" in out and "tenant=beta" in out
    index = ["index", FLEET, "--all-tenants", "--db", db, "--index", "memory", "--embedder", "hash", "--cursor-dir", str(cursors)]
    out = run(*index).output
    assert out.count("indexed tenant=") == 2 and (cursors / "index-beta.cursor").exists(), out
    store.forget("beta")
    out = run(*index).output
    assert "cleared tenant=beta: forgotten" in out and not (cursors / "index-beta.cursor").exists()
    assert "beta" not in run(*index).output  # swept once
    assert run("consolidate", FLEET, "--all-tenants", "--db", db).exit_code == 0


@pytest.mark.skipif(not MYSQL, reason="KEEL_MYSQL_URL not set")
def test_mysql_reconnects_when_the_server_drops_the_connection():
    from keel.store.mysql import MysqlStore

    s, admin = MysqlStore.from_url(MYSQL), MysqlStore.from_url(MYSQL)
    s.upsert_entity(TENANT, "unit", "A")
    admin._exec(f"KILL {s.db.thread_id()}")  # what wait_timeout does to an idle consumer
    s._pings[id(s.db)] = 0  # idle long enough to be checked
    s.upsert_entity(TENANT, "unit", "B")
    s.db.close()  # what PyMySQL leaves after a statement lost the server
    with s.transaction():
        s.upsert_entity(TENANT, "unit", "C")
    assert all(s.entity(TENANT, f"unit:{x}") for x in "ABC")
    with pytest.raises(ConnectionError), s.transaction():  # never half a transaction on a new session
        s.upsert_entity(TENANT, "unit", "D")
        s.db.close()
        s.upsert_entity(TENANT, "unit", "E")
    assert s.entity(TENANT, "unit:D") is None and s.entity(TENANT, "unit:E") is None


def test_keys_that_differ_only_in_case_or_accents_are_different_subjects(store):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    keys = ["page:RENDIMIENTO", "page:Rendimiento", "bookmark:Numero", "bookmark:Número"]
    for key in keys:
        kind, id = key.split(":")
        store.upsert_entity(TENANT, kind, id, title=id)
        store.open_fact(TENANT, key, "title", id, valid_from=now)
    assert [store.entity(TENANT, k).attrs["title"] for k in keys] == ["RENDIMIENTO", "Rendimiento", "Numero", "Número"]
    got = store.facts_of(TENANT, keys) if hasattr(store, "facts_of") else {k: store.facts(TENANT, k) for k in keys}
    assert {k: [f.value for f in fs] for k, fs in got.items()} == {k: [k.split(":")[1]] for k in keys}


@pytest.mark.skipif(not MYSQL, reason="KEEL_MYSQL_URL not set")
def test_mysql_tables_from_before_0_24_2_get_exact_keys_and_name_what_collided():
    from keel.store.mysql import MysqlStore

    s = MysqlStore.from_url(MYSQL, prefix="ci_")
    for t in s.TABLES:
        s._exec(f"DELETE FROM {t}")
        s._exec(f"ALTER TABLE {t} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")  # what 0.24.1 created
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for id in ("RENDIMIENTO", "Rendimiento"):
        s.upsert_entity(TENANT, "page", id)
        s.open_fact(TENANT, f"page:{id}", "title", id, valid_from=now)
    s.open_fact(TENANT, "note:alone", "text", "no entity, nothing alike", valid_from=now)
    with pytest.warns(UserWarning, match="1 subjects .* acme-logistics/page:Rendimiento"):
        s = MysqlStore.from_url(MYSQL, prefix="ci_")
    assert all(s._collation(t) == "utf8mb4_bin" for t in s.TABLES)
    assert s.collided() == [(TENANT, "page:Rendimiento")]
    s.upsert_entity(TENANT, "page", "Rendimiento")  # the source publishes it again
    assert s.collided() == [] and [f.value for f in s.facts(TENANT, "page:Rendimiento")] == ["Rendimiento"]
