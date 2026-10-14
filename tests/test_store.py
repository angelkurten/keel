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
        return {k: _strip(v) for k, v in o.items() if k not in ("recorded_at", "as_of")}
    if isinstance(o, list):
        return sorted((_strip(v) for v in o), key=lambda v: json.dumps(v, sort_keys=True))
    return o


EXTERNAL = os.environ.get("KEEL_STORE")  # "pkg.module:factory"


@pytest.fixture(params=["memory", "sqlite"] + (["external"] if EXTERNAL else []))
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "keel.db")
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
