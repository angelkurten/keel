"""The optional sixth tool, `query`: read-only SQL over the schema generated from the cartridge (keel/sqlgen.py),
bound to one tenant by the store, never by the agent's SQL.

SqliteStore, DuckdbStore and PostgresStore (KEEL_POSTGRES_URL) serve it; MemoryStore does not. Rows come back as
objects keyed by column, JSON-safe (instants as ISO text in UTC), at most a page of them with a leading
{"more": ...} marker when cut. What it reads: the tenant's accepted tenant-scope facts and the global memory its
tools read; never agent or task memory, never proposals. The attacks live in test_query_security.py.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter
from keel.store.base import ApiKey, Scope

ROOT = Path(__file__).resolve().parent.parent
POSTGRES = os.environ.get("KEEL_POSTGRES_URL")
TENANT = "acme-logistics"
RIVAL = "rival"
T = "2030-01-01T00:00:00+00:00"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SECRETS = ("RIVAL-SECRET", "AGENT-SECRET", "TASK-SECRET", "PROPOSED-SECRET", "KEY-SECRET", "OTHER-GLOBAL-SECRET")
STORES = ["sqlite", "sqlite-memory", "duckdb"] + (["postgres"] if POSTGRES else [])


def cartridge(agents: dict[str, Any] | None = None) -> Any:
    """The fleet cartridge with facts declared on tires and tickets, so per-kind views exist."""
    c = copy.copy(load_cartridge(ROOT / "examples" / "fleet"))
    c.entities = {k: dict(v) if isinstance(v, dict) else {} for k, v in c.entities.items()}
    c.entities["tire"].update(facts=["depth_mm"], attrs=["position"])
    c.entities["ticket"].update(facts=["status", "urgency"])
    if agents is not None:
        from keel.agents import parse_agents

        c.agents = parse_agents(agents)
    return c


def make_store(kind: str, tmp_path: Path) -> Any:
    if kind == "sqlite":
        return SqliteStore(tmp_path / "keel.db")
    if kind == "sqlite-memory":
        return SqliteStore(":memory:")
    if kind == "duckdb":
        pytest.importorskip("duckdb")
        from keel.store.duckdb import DuckdbStore

        return DuckdbStore(tmp_path / "keel.duckdb")
    from conftest import postgres_store

    return postgres_store()


def populate(store: Any, c: Any) -> Any:
    """The fleet log for TENANT, a rival tenant whose every value is a secret, and in TENANT memory no query may
    show: another agent's, a task's, a proposal; global memory it may; and the server's own tables."""
    Ingestor(c, store).ingest(JsonlAdapter(ROOT / "examples" / "fleet" / "events.jsonl"))
    at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    store.upsert_entity(RIVAL, "tire", "RIVAL-SECRET", ref="RIVAL-SECRET", position="RIVAL-SECRET")
    store.upsert_entity(RIVAL, "tire", "T-9001", ref="RIVAL-SECRET", position="RIVAL-SECRET")
    store.open_fact(RIVAL, "tire:T-9001", "depth_mm", 1.0, valid_from=at, source="RIVAL-SECRET", evidence="RIVAL-SECRET")
    store.open_fact(RIVAL, "tire:RIVAL-SECRET", "depth_mm", "RIVAL-SECRET", valid_from=at, source="rival", evidence="e")
    store.open_fact(RIVAL, "ticket:TK-1", "status", "RIVAL-SECRET", valid_from=at, source="rival", evidence="e")
    store.link(RIVAL, "tire:T-9001", "mounted_on", "unit:RIVAL-SECRET", at, source="RIVAL-SECRET")
    store.open_fact("acc2/__global__", "tire:T-9001", "note", "OTHER-GLOBAL-SECRET", valid_from=at, source="x", scope=Scope.GLOBAL, evidence="e")
    tools = Tools(c, store, TENANT, "spy", clock=lambda: NOW)
    tools.annotate("tire:T-9001", "note", "AGENT-SECRET", scope="agent")
    tools.annotate("tire:T-9001", "note", "TASK-SECRET", scope="task", task="t1")
    tools.annotate("tire:T-9001", "note", "PROPOSED-SECRET", scope="tenant", evidence="e", status="proposed")
    tools.annotate("tire:T-9001", "global_note", "GLOBAL-OK", scope="global", evidence="e")
    tools.annotate("tire:T-9001", "tenant_note", "TENANT-OK", scope="tenant", evidence="e")
    store.put_key(ApiKey(id="kk_x", hash="KEY-SECRET", account="KEY-SECRET", created_at=at, tenant=RIVAL))
    store.meter([(RIVAL, "2026-09-02", "KEY-SECRET", 1)])
    return store


@pytest.fixture(params=STORES)
def kind(request: Any) -> str:
    return request.param


@pytest.fixture
def setup(kind: str, tmp_path: Path) -> tuple[Any, Any]:
    c = cartridge()
    return c, populate(make_store(kind, tmp_path), c)


def tools_of(setup: tuple[Any, Any], **kw: Any) -> Tools:
    c, store = setup
    return Tools(c, store, TENANT, clock=lambda: NOW, **kw)


def rows(result: list[Any]) -> list[dict[str, Any]]:
    return [r for r in result if not (len(r) == 1 and "more" in r)]


# ---- what it answers ---------------------------------------------------------------------------------------


def test_query_reads_a_per_kind_view_as_rows_keyed_by_column(setup):
    out = tools_of(setup).query(f"SELECT id, depth_mm, position FROM tire_state_at('{TENANT}', '{T}') ORDER BY id")
    assert out and all(set(r) == {"id", "depth_mm", "position"} for r in out)
    by_id = {r["id"]: r for r in out}
    assert by_id["T-9001"]["depth_mm"] == pytest.approx(2.6)
    assert json.dumps(out)  # JSON-safe


def test_instants_come_back_as_iso_text_in_utc(setup):
    out = tools_of(setup).query(f"SELECT depth_mm_since FROM tire_state_at('{TENANT}', '{T}') WHERE id = 'T-9001'")
    since = out[0]["depth_mm_since"]
    assert isinstance(since, str)
    parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0 and since.startswith("2026-")


def test_rules_relations_and_the_lower_level_are_queryable(setup):
    t = tools_of(setup)
    crit = t.query(f"SELECT subject FROM tire_critical_at('{TENANT}', '{T}')")
    assert {r["subject"] for r in crit} == set(t.search(rule="tire_critical", detail="key", as_of=datetime.fromisoformat(T)))
    rels = t.query(f"SELECT src, rel, dst FROM relations_at('{TENANT}', '{T}') WHERE rel = 'mounted_on'")
    assert rels and all(r["rel"] == "mounted_on" for r in rels)
    assert t.query(f"SELECT count(*) AS n FROM keel_facts('{TENANT}')")[0]["n"] > 0
    assert t.query(f"SELECT count(*) AS n FROM state_at('{TENANT}', '{T}')")[0]["n"] > 0


def test_query_sees_tenant_and_global_memory_but_no_agent_or_task_memory_or_proposals(setup):
    t = tools_of(setup)
    values = json.dumps(t.query(f"SELECT predicate, value FROM keel_facts('{TENANT}')", limit=0))
    assert "TENANT-OK" in values and "GLOBAL-OK" in values
    for secret in SECRETS:
        assert secret not in values
    raw = json.dumps(t.query("SELECT * FROM facts", limit=0))
    assert "TENANT-OK" in raw and not any(s in raw for s in SECRETS)


def test_a_cut_page_leads_with_a_more_marker(setup):
    t = tools_of(setup)
    out = t.query(f"SELECT subject FROM state_at('{TENANT}', '{T}') ORDER BY subject", limit=2)
    assert len(out) == 3 and set(out[0]) == {"more"} and out[0]["more"]["shown"] == 2
    assert "next" in out[0]["more"]
    whole = t.query(f"SELECT subject FROM state_at('{TENANT}', '{T}') ORDER BY subject", limit=0)
    assert "more" not in whole[0] and len(whole) > 2


def test_the_default_page_and_the_deployment_ceiling(setup):
    from keel.tools import QUERY_ROWS

    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 600) SELECT n FROM r"
    out = tools_of(setup).query(sql)
    assert len(rows(out)) == QUERY_ROWS and out[0]["more"]["shown"] == QUERY_ROWS
    capped = tools_of(setup, max_list=10).query(sql, limit=0)
    assert len(rows(capped)) == 10 and "more" in capped[0]
    assert len(rows(tools_of(setup, max_list=10).query(sql, limit=500))) == 10
    assert len(rows(tools_of(setup).query(sql, limit=5))) == 5


def test_a_bad_query_is_a_clear_error(setup):
    t = tools_of(setup)
    with pytest.raises(ValueError, match="(?i)syntax|parser|near|error"):
        t.query("SELECT * FROM WHERE")
    with pytest.raises(ValueError, match="SELECT"):
        t.query("SELEC nothing")
    with pytest.raises(ValueError):
        t.query("SELECT * FROM no_such_view")
    with pytest.raises(ValueError, match="SELECT"):
        t.query("")


def test_a_slow_query_times_out(setup):
    t = tools_of(setup, query_timeout=0.5)
    start = time.monotonic()
    with pytest.raises(ValueError, match="(?i)timed out|timeout|cancel|interrupt"):
        t.query("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) AS n FROM r")
    assert time.monotonic() - start < 4
    assert t.query("SELECT 1 AS one") == [{"one": 1}]  # the next query runs


def test_the_store_answers_what_was_written_since_the_last_query(setup):
    _, store = setup
    t = tools_of(setup)
    sql = f"SELECT depth_mm FROM tire_state_at('{TENANT}', '{T}') WHERE id = 'T-NEW'"
    assert t.query(sql) == []
    store.open_fact(TENANT, "tire:T-NEW", "depth_mm", 7.5, valid_from=datetime(2026, 9, 30, tzinfo=timezone.utc), source="s", evidence="e")
    assert t.query(sql) == [{"depth_mm": 7.5}]


# ---- who gets it -------------------------------------------------------------------------------------------


def test_a_store_without_sql_refuses_clearly():
    c = cartridge()
    with pytest.raises(ValueError, match="(?i)not supported"):
        Tools(c, MemoryStore(), TENANT).query("SELECT 1")


def test_spec_lists_query_only_when_enabled_and_supported(tmp_path):
    c = cartridge()
    store = SqliteStore(tmp_path / "k.db")
    names = lambda t: [s["name"] for s in t.spec()]
    assert names(Tools(c, store, TENANT)) == ["context", "history", "search", "annotate", "related"]
    assert names(Tools(c, MemoryStore(), TENANT, query=True)) == ["context", "history", "search", "annotate", "related"]
    spec = Tools(c, store, TENANT, query=True).spec()
    assert [s["name"] for s in spec][-1] == "query"
    q = spec[-1]
    assert q["args"] == ["sql", "limit?"]
    assert "tire_state_at" in q["doc"] and TENANT in q["doc"]


def test_registry_bound_agents_need_query_true(tmp_path):
    c = cartridge(agents={"bot": {"writes": ["task"]}, "analyst": {"writes": ["task"], "query": True}})
    store = SqliteStore(tmp_path / "k.db")
    with pytest.raises(PermissionError, match="query"):
        Tools(c, store, TENANT, "bot").query("SELECT 1 AS one")
    assert Tools(c, store, TENANT, "analyst").query("SELECT 1 AS one") == [{"one": 1}]
    assert "query" not in [s["name"] for s in Tools(c, store, TENANT, "bot", query=True).spec()]
    assert "query" in [s["name"] for s in Tools(c, store, TENANT, "analyst", query=True).spec()]


def test_the_registry_flag_is_validated():
    from keel.agents import parse_agents

    assert parse_agents({"a": {"query": True}})["a"].query is True
    assert parse_agents({"a": {}})["a"].query is False


def test_the_sqlite_schema_says_how_sqlite_reads_instants():
    from keel import sqlgen

    text = sqlgen.describe(cartridge(), TENANT, "sqlite")
    assert "SQLite" in text and "TIMESTAMP '" not in text and "keel_now()" in text
    assert sqlgen.describe(cartridge(), TENANT) == sqlgen.describe(cartridge(), TENANT, "duckdb")  # as before
