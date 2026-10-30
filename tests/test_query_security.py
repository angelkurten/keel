"""Attacks on `query`: whatever SQL an agent writes, it reads one tenant's visible rows and nothing else.

Isolation is the store's, never a check of the agent's text: SqliteStore and DuckdbStore answer from a sandbox that
holds only the tenant's rows (SQLite: an in-memory copy behind an authorizer that allows reads only; DuckDB: a
read-only copy with external access, extensions and configuration locked), PostgresStore from a login role that
may read only views bound to the session's backend, behind security barriers. Each attack either fails with a
clear error or answers without any other tenant's, agent's, task's or proposal's value and without the server's
own tables (keys, metering). Writes leave the store and the next answer unchanged; a runaway query times out.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from test_query import RIVAL, SECRETS, STORES, TENANT, T, cartridge, make_store, populate, tools_of

R = RIVAL
ATTACKS = {
    # another tenant named in the macros' tenant argument
    "rival in a per-kind view": f"SELECT * FROM tire_state_at('{R}', '{T}')",
    "rival in state_at": f"SELECT * FROM state_at('{R}', '{T}')",
    "rival in keel_facts": f"SELECT * FROM keel_facts('{R}')",
    "rival in facts_known_at": f"SELECT * FROM facts_known_at('{R}', '{T}')",
    "rival in relations_at": f"SELECT * FROM relations_at('{R}', '{T}')",
    "rival in a rule": f"SELECT * FROM tire_critical_at('{R}', '{T}')",
    "rival in verdicts": f"SELECT * FROM tire_critical_verdicts('{R}', '{T}')",
    "rival in changes": f"SELECT * FROM ticket_changes('{R}', NULL, NULL)",
    "rival in an internal macro": f"SELECT * FROM keel_facts_now('{R}')",
    "tenant argument as an expression": f"SELECT * FROM state_at('{TENANT}' OR 1 = 1, '{T}')",
    "tenant argument quote injection": f"SELECT * FROM state_at('{TENANT}'' OR tenant <> ''x', '{T}')",
    "the global partition as tenant": "SELECT * FROM keel_facts('__global__')",
    "another account's global partition": "SELECT * FROM keel_facts('acc2/x')",
    "as another author": f"SELECT * FROM state_known_at('{TENANT}', '{T}', as_author := 'spy')",
    "as a task": f"SELECT * FROM state_known_at('{TENANT}', '{T}', as_task := 't1')",
    "expired memory via alive": f"SELECT * FROM facts_known_at('{TENANT}', '{T}', alive := '1970-01-01T00:00:00+00:00')",
    # the base tables, directly or qualified
    "facts": "SELECT * FROM facts",
    "facts of rival": f"SELECT * FROM facts WHERE tenant = '{R}'",
    "entities": "SELECT * FROM entities",
    "relations": "SELECT * FROM relations",
    "main.facts": "SELECT * FROM main.facts",
    "public.facts": "SELECT * FROM public.facts",
    "temp.facts": "SELECT * FROM temp.facts",
    "api_keys": "SELECT * FROM api_keys",
    "metering": "SELECT * FROM metering",
    "accounts": "SELECT * FROM accounts",
    "ingested": "SELECT * FROM ingested",
    "forgotten": "SELECT * FROM forgotten",
    "cutoffs": "SELECT * FROM cutoffs",
    "the query binding": "SELECT * FROM keel_query_bound",
    "union of base tables": f"SELECT value FROM state_at('{TENANT}', '{T}') UNION ALL SELECT value FROM facts WHERE tenant = '{R}'",
    "union with api_keys": "SELECT 'x' AS a, 'y' AS b UNION ALL SELECT account, hash FROM api_keys",
    "scalar subquery": "SELECT (SELECT string_agg(value, ',') FROM facts) AS v",
    "proposals and memory in facts": "SELECT value FROM facts WHERE status IS NOT NULL OR scope IN ('agent', 'task')",
    # catalogs and settings
    "information_schema": "SELECT * FROM information_schema.tables",
    "information_schema columns": "SELECT * FROM information_schema.columns",
    "duckdb_tables": "SELECT * FROM duckdb_tables()",
    "duckdb_databases": "SELECT * FROM duckdb_databases()",
    "duckdb_settings": "SELECT * FROM duckdb_settings()",
    "sqlite_master": "SELECT * FROM sqlite_master",
    "pg_tables": "SELECT * FROM pg_catalog.pg_tables",
    "pg_stats": "SELECT * FROM pg_stats",
    "pg_stat_activity": "SELECT query FROM pg_stat_activity",
    "pg_proc": "SELECT prosrc FROM pg_proc WHERE proname = 'keel_global'",
    "pg_roles": "SELECT rolname, rolpassword FROM pg_authid",
    "current_setting data_directory": "SELECT current_setting('data_directory')",
    "query_to_xml": "SELECT query_to_xml('select * from public.facts', true, false, '')",
    "set_config role then read": "SELECT set_config('role', 'none', true), query_to_xml('select * from public.facts', true, false, '')",
    "set_config search_path": f"SELECT set_config('search_path', 'public', true), (SELECT count(*) FROM tire_critical_verdicts('{R}', '{T}'))",
    "duckdb query()": "SELECT * FROM query('SELECT * FROM api_keys')",
    "duckdb query_table": "SELECT * FROM query_table('api_keys')",
    "getenv": "SELECT getenv('HOME')",
    "python replacement scan": "SELECT * FROM store",
    # files, network, extensions
    "read_csv": "SELECT * FROM read_csv('/etc/passwd')",
    "read_text": "SELECT * FROM read_text('/etc/hosts')",
    "read_parquet over http": "SELECT * FROM read_parquet('https://example.com/x.parquet')",
    "glob": "SELECT * FROM glob('/etc/*')",
    "pg_read_file": "SELECT pg_read_file('/etc/passwd')",
    "pg_ls_dir": "SELECT pg_ls_dir('.')",
    "lo_import": "SELECT lo_import('/etc/passwd')",
    "sqlite load_extension": "SELECT load_extension('/tmp/x')",
    "sqlite readfile": "SELECT readfile('/etc/passwd')",
    "ATTACH": "ATTACH '/tmp/keel-attack.db' AS x",
    "ATTACH memory": "ATTACH ':memory:' AS x",
    "COPY out": "COPY facts TO '/tmp/keel-attack.csv'",
    "COPY select": "COPY (SELECT * FROM facts) TO STDOUT",
    "EXPORT": "EXPORT DATABASE '/tmp/keel-attack'",
    "INSTALL": "INSTALL httpfs",
    "LOAD": "LOAD httpfs",
    # statements that are not one SELECT
    "PRAGMA": "PRAGMA database_list",
    "PRAGMA table_info": "PRAGMA table_info(facts)",
    "SET": "SET enable_external_access = true",
    "SET search_path": "SET search_path = public",
    "SET ROLE": "SET ROLE NONE",
    "RESET ROLE": "RESET ROLE",
    "two statements": f"SELECT 1; SELECT * FROM facts WHERE tenant = '{R}'",
    "two statements, a write": "SELECT 1; DELETE FROM facts",
    "comment hiding a semicolon": "SELECT 1 /* ; */ ; SELECT * FROM api_keys",
    "line comment first": f"-- read\nSELECT * FROM facts WHERE tenant = '{R}'",
    "block comments as spaces": "SELECT/**/value/**/FROM/**/facts",
    "DO block": "DO $$ BEGIN PERFORM 1; END $$",
    "CREATE FUNCTION": "CREATE FUNCTION pg_temp.f() RETURNS int AS 'SELECT 1' LANGUAGE sql",
    "EXPLAIN": "EXPLAIN SELECT * FROM facts",
    "VACUUM": "VACUUM",
    # writes
    "DELETE": "DELETE FROM facts",
    "UPDATE": "UPDATE facts SET value = '\"x\"'",
    "INSERT": "INSERT INTO facts (tenant, subject, predicate, value, valid_from, scope) VALUES ('x', 'tire:x', 'p', '1', '2020', 'tenant')",
    "DROP": "DROP TABLE facts",
    "CREATE TABLE AS": "CREATE TABLE stolen AS SELECT * FROM facts",
    "CREATE TEMP": "CREATE TEMP TABLE t AS SELECT 1",
    "writing CTE delete": "WITH x AS (DELETE FROM facts RETURNING *) SELECT count(*) FROM x",
    "writing CTE update": "WITH x AS (UPDATE facts SET value = '\"x\"' RETURNING *) SELECT count(*) FROM x",
    "writing CTE insert": "WITH x AS (INSERT INTO entities (tenant, kind, id, attrs) VALUES ('acme-logistics', 'tire', 'EVIL', '{}') RETURNING *) SELECT * FROM x",
    "nextval": "SELECT nextval('facts_seq')",
    "pg_notify": "SELECT pg_notify('keel', 'x')",
    "advisory lock": "SELECT pg_advisory_lock(7413)",
}


@pytest.fixture(scope="module", params=STORES)
def attacked(request: Any, tmp_path_factory: Any) -> Any:
    c = cartridge()
    store = populate(make_store(request.param, tmp_path_factory.mktemp(request.param)), c)
    return c, store


def _answer(tools: Any, sql: str) -> str:
    try:
        return json.dumps(tools.query(sql, limit=0), default=str)
    except (ValueError, PermissionError) as exc:
        return f"error: {exc}"


@pytest.mark.parametrize("name", list(ATTACKS))
def test_an_attack_reads_nothing_it_may_not(attacked, name):
    tools = tools_of(attacked)
    baseline = _answer(tools, f"SELECT subject, predicate, value FROM keel_facts('{TENANT}') ORDER BY subject, predicate, value")
    answer = _answer(tools, ATTACKS[name])
    for secret in SECRETS:
        assert secret not in answer, f"{name}: leaked {secret}: {answer[:300]}"
    assert "/etc/passwd" not in answer or answer.startswith("error"), answer[:200]
    assert "root:" not in answer
    # whatever it tried, the tenant's data and the store are as they were
    assert _answer(tools, f"SELECT subject, predicate, value FROM keel_facts('{TENANT}') ORDER BY subject, predicate, value") == baseline
    _, store = attacked
    assert store.entity(TENANT, "tire:EVIL") is None
    assert {f.value for f in store.facts(RIVAL, "tire:T-9001")} == {1.0}


@pytest.mark.parametrize("name", list(ATTACKS))
def test_the_store_alone_holds_the_line(attacked, name):
    """The same attacks straight to the store, past Tools' first-word check: the isolation is the store's."""
    c, store = attacked
    try:
        cols, got, _ = store.query_rows(TENANT, c, ATTACKS[name], 10_000, 5.0)
        answer = json.dumps([cols, got], default=str)
    except ValueError as exc:
        answer = f"error: {exc}"
    for secret in SECRETS:
        assert secret not in answer, f"{name}: leaked {secret}: {answer[:300]}"
    assert "root:" not in answer
    assert store.entity(TENANT, "tire:EVIL") is None
    assert {f.value for f in store.facts(RIVAL, "tire:T-9001")} == {1.0}
    assert "TENANT-OK" in json.dumps(store.query_rows(TENANT, c, "SELECT value FROM facts", 10_000, 5.0)[1])


def test_rival_is_never_answered_even_where_the_macro_takes_its_name(attacked):
    """Named in a macro, the rival tenant has no rows here: only the global memory every tenant reads answers."""
    tools = tools_of(attacked)
    assert tools.query(f"SELECT * FROM relations_at('{R}', '{T}')", limit=0) == []
    assert tools.query(f"SELECT * FROM tire_state_at('{R}', '{T}') WHERE depth_mm IS NOT NULL", limit=0) == []
    out = tools.query(f"SELECT tenant, value FROM state_at('{R}', '{T}')", limit=0)
    assert all(r["tenant"] == "__global__" for r in out), out


def test_a_runaway_query_is_stopped(attacked):
    c, store = attacked
    from keel import Tools

    tools = Tools(c, store, TENANT, query=True, query_timeout=0.5)
    for sql in ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) AS n FROM r",
                f"SELECT count(*) AS n FROM keel_facts('{TENANT}') a, keel_facts('{TENANT}') b, keel_facts('{TENANT}') c, keel_facts('{TENANT}') d, keel_facts('{TENANT}') e, keel_facts('{TENANT}') f, keel_facts('{TENANT}') g, keel_facts('{TENANT}') h"):
        start = time.monotonic()
        with pytest.raises(ValueError):
            tools.query(sql)
        assert time.monotonic() - start < 5, sql


def test_no_session_state_outlives_a_query(attacked):
    """Postgres: a session-level advisory lock (not undone by a rollback) would block the store's own installs."""
    _, store = attacked
    if type(store).__name__ != "PostgresStore":
        pytest.skip("Postgres sessions only")
    tools = tools_of(attacked)
    tools.query("SELECT pg_advisory_lock(7413) AS l, pg_advisory_lock(7414) AS m")
    assert store._one("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND database = "
                      "(SELECT oid FROM pg_database WHERE datname = current_database())")["n"] == 0  # other xdist workers' are theirs


def test_a_very_long_query_is_refused_before_it_runs(attacked):
    tools = tools_of(attacked)
    with pytest.raises(ValueError, match="(?i)long"):
        tools.query("SELECT " + " + ".join(["1"] * 20_000))


def test_one_tenant_never_reads_another_through_the_same_store(tmp_path, kind):
    """Two tenants queried alternately from one store: each sees only its own rows, whatever the cache holds."""
    from keel import Tools

    c = cartridge()
    store = populate(make_store(kind, tmp_path), c)
    mine, theirs = Tools(c, store, TENANT, query=True), Tools(c, store, RIVAL, query=True)
    for _ in range(2):
        assert "RIVAL-SECRET" not in json.dumps(mine.query("SELECT * FROM facts", limit=0))
        assert "RIVAL-SECRET" in json.dumps(theirs.query("SELECT * FROM facts", limit=0))
        assert "TENANT-OK" not in json.dumps(theirs.query("SELECT * FROM facts", limit=0))


@pytest.fixture(params=STORES)
def kind(request: Any) -> str:
    return request.param


def test_nested_macros_cannot_blow_up_the_sqlite_expansion(tmp_path):
    """Review a7/a8: macro calls nested in macro arguments expanded exponentially (depth 15, under 1,000 characters: 7 s
    and 2 GB before the timeout could start). The expansion now has a budget and stops at once with a clear error."""
    import resource

    from keel import SqliteStore, Tools

    c = cartridge()
    tools = Tools(c, populate(SqliteStore(tmp_path / "k.db"), c), TENANT, query=True, query_timeout=5)
    inner = f"'{T}'"
    for _ in range(15):
        inner = f"(SELECT max(valid_from) FROM state_at('{TENANT}', {inner}))"
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    start = time.monotonic()
    with pytest.raises(ValueError, match="(?i)too large|nest"):
        tools.query(f"SELECT count(*) AS n FROM state_at('{TENANT}', {inner})")
    assert time.monotonic() - start < 2
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 300 * 2**20  # bytes on macOS, KB on Linux: loose either way
    with pytest.raises(ValueError):
        tools.query(f"SELECT * FROM state_at('{TENANT}', '{T}', 'x', 'y', 'z', 'w', 'v')")  # more arguments than the macro takes


WIDE = {"sqlite": "printf('%500000000d', {i})", "duckdb": "repeat('x', 500000000)", "postgres": "repeat('x', 500000000)"}
TALL = {"sqlite": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 400) SELECT n, printf('%100000d', n) AS pad FROM r",
        "duckdb": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 400), p AS (SELECT string_agg('x', '') AS s FROM (SELECT 1 FROM r, r AS r2 LIMIT 100000)) SELECT n, s AS pad FROM r, p",
        "postgres": "SELECT i AS n, repeat('x', 100000) AS pad FROM generate_series(1, 400) i"}


def _kind(store: Any) -> str:
    return {"SqliteStore": "sqlite", "DuckdbStore": "duckdb", "PostgresStore": "postgres"}[type(store).__name__]  # either Postgres user


@pytest.mark.parametrize("n", [1, 3])
def test_a_huge_value_is_refused_not_fetched(attacked, n):
    """Review a3/p4: the row cap did not bound bytes: one row of 500 MB values went whole into the server's memory."""
    import resource

    _, store = attacked
    tools = tools_of(attacked)
    f = WIDE[_kind(store)]
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    start = time.monotonic()
    from keel.store.query import ROW_MAX

    try:  # refused (DuckDB, Postgres), or cut by the database's own length limit to NULL (SQLite)
        out = tools.query("SELECT " + ", ".join(f.format(i=i) + f" AS c{i}" for i in range(n)), limit=1)
        assert sum(len(str(v)) for v in out[-1].values()) <= ROW_MAX
    except ValueError as exc:
        assert any(w in str(exc).lower() for w in ("too large", "too big", "may not call", "memory", "limit", "timed out")), exc
    assert time.monotonic() - start < 8  # Postgres builds the value, then stops at the timeout: never sent
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert grown < 200 * 2**20, grown  # bytes on macOS (KB on Linux: looser still)


def test_a_long_result_stops_at_the_byte_budget(attacked):
    """Many wide rows: the answer stops at the byte budget and says more exist, as a row cap does."""
    from keel.store.query import RESULT_MAX

    _, store = attacked
    out = tools_of(attacked).query(TALL[_kind(store)], limit=0)
    assert set(out[0]) == {"more"}
    assert 0 < len(out) - 1 < 400
    assert sum(len(r["pad"]) for r in out[1:]) <= RESULT_MAX


def test_duckdb_refuses_its_row_generators_too(tmp_path):
    """Second review d1-d3: repeat_row(num_rows := 10^9) generates rows from nothing as range does; it is refused with
    the other generators, however it is spelled."""
    pytest.importorskip("duckdb")
    from keel import Tools

    c = cartridge()
    tools = Tools(c, populate(make_store("duckdb", tmp_path), c), TENANT, query=True)
    for sql in ("SELECT count(*) AS n FROM repeat_row(1, num_rows := 1000000000)",
                "SELECT count(*) AS n FROM REPEAT_ROW(1, num_rows := 1000)",
                "SELECT md5(CAST(random() AS VARCHAR)) AS v FROM main.repeat_row(1, num_rows := 100000)"):
        with pytest.raises(ValueError, match="may not call"):
            tools.query(sql)
