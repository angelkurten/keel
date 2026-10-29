"""`query` on Postgres, against what the security review reproduced (scripts p1-p4), on two deployments: the store's
user a superuser (KEEL_POSTGRES_URL), and an ordinary user with only LOGIN CREATEROLE and its own schema (made here
from KEEL_POSTGRES_URL, as CI would: `app_store`).

- a CREATEROLE-only user sets query up (role attributes it may not set are never asked for); a user without
  CREATEROLE gets a clear error and leaves no role behind;
- each tenant queries as a role of its own: another tenant can neither read its running statement in
  pg_stat_activity nor cancel or terminate it, and nothing an agent sets (set_config, SET ROLE) changes whose rows
  it reads;
- the role's password comes from a random secret only the store's user reads, never from the DSN;
- a wide result is measured on the server and refused there, a long one stops at the byte budget.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

import pytest
from conftest import as_user
from conftest import postgres_app_store as app_store
from test_query import RIVAL, SECRETS, TENANT, T, cartridge, populate

from keel import Tools

POSTGRES = os.environ.get("KEEL_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES, reason="set KEEL_POSTGRES_URL")


def super_store() -> Any:
    from conftest import postgres_store

    return postgres_store()


@pytest.fixture(params=["superuser", "createrole"])
def store(request: Any) -> Any:
    return super_store() if request.param == "superuser" else app_store()


def test_a_createrole_user_sets_query_up(store):
    c = cartridge()
    populate(store, c)
    t = Tools(c, store, TENANT, query=True)
    assert t.query(f"SELECT id FROM tire_state_at('{TENANT}', '{T}') WHERE id = 'T-9001'") == [{"id": "T-9001"}]
    assert "RIVAL-SECRET" not in json.dumps(t.query("SELECT * FROM facts", limit=0))


def roles() -> set[str]:
    import psycopg  # roles are the cluster's: any database shows them

    with psycopg.connect(POSTGRES, autocommit=True) as conn:
        return {r[0] for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'keel_q_%'").fetchall()}


def test_a_user_without_createrole_gets_a_clear_error_and_leaves_no_role():
    """Review p1: setup asked for attributes only a superuser sets, failed half way (a password-less LOGIN role left
    behind) and surfaced as a 500."""
    store = app_store("keel_weak", createrole=False)
    c = cartridge()
    before = roles()
    with pytest.raises(ValueError, match="CREATEROLE"):
        Tools(c, store, TENANT, query=True).query("SELECT 1 AS one")
    assert roles() == before


def test_tenants_cannot_see_or_signal_each_others_queries(store):
    """Review p2: every tenant shared one role, so tenant B read tenant A's running SQL and could cancel it."""
    c = cartridge()
    populate(store, c)
    victim, spy = Tools(c, store, TENANT, query=True, query_timeout=6), Tools(c, store, RIVAL, query=True)
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["victim"] = victim.query("SELECT (SELECT 1 FROM pg_sleep(2)) AS slept, 'PATIENT-JANE-DOE' AS who")
        except Exception as exc:  # noqa: BLE001
            out["victim"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.8)
    seen = json.dumps(spy.query("SELECT query FROM pg_stat_activity WHERE pid <> pg_backend_pid()", limit=0))
    assert "PATIENT-JANE-DOE" not in seen
    try:
        spy.query("SELECT pg_cancel_backend(pid) AS c, pg_terminate_backend(pid) AS t FROM pg_stat_activity "
                  "WHERE usename LIKE 'keel_q_%' AND pid <> pg_backend_pid()", limit=0)
    except ValueError as exc:
        assert "permission" in str(exc).lower() or "must be" in str(exc).lower(), exc
    th.join()
    assert out["victim"] == [{"slept": 1, "who": "PATIENT-JANE-DOE"}]


def test_nothing_an_agent_sets_changes_whose_rows_it_reads(store):
    """The role is the binding: set_config, SET ROLE or a new search_path inside the query read no other tenant."""
    c = cartridge()
    populate(store, c)
    t = Tools(c, store, TENANT, query=True)
    for sql in ("SELECT set_config('role', 'none', false) AS r, (SELECT count(*) FROM facts) AS n",
                "SELECT set_config('search_path', 'pg_catalog', false) AS r, (SELECT string_agg(value, ',') FROM facts) AS v",
                f"SELECT set_config('keel.tenant', '{RIVAL}', false) AS r, (SELECT string_agg(value, ',') FROM facts) AS v"):
        try:
            answer = json.dumps(t.query(sql, limit=0))
        except ValueError as exc:
            answer = str(exc)
        assert not any(s in answer for s in SECRETS), (sql, answer)
    assert "TENANT-OK" in json.dumps(t.query("SELECT value FROM facts", limit=0))
    for sql in ("SELECT * FROM keel_query_roles", "SELECT * FROM keel_query_secret", "SELECT * FROM keel_q_facts WHERE tenant = 'rival'"):
        try:
            answer = json.dumps(t.query(sql, limit=0))
        except ValueError as exc:
            answer = str(exc)
        assert not any(s in answer for s in SECRETS) and "secret" not in answer.split(":")[0], (sql, answer)


def test_the_role_password_is_not_derived_from_the_dsn(store):
    """Review p3: with no password in the DSN the role's password was HMAC(DSN, role): anyone knowing the DSN logged in."""
    import psycopg

    c = cartridge()
    Tools(c, store, TENANT, query=True).query("SELECT 1 AS one")
    role = store._query_role(TENANT)[0]
    for key in (store._dsn, str(urlparse(store._dsn).password or "")):
        guess = hmac.new(key.encode(), role.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(as_user(role, guess)).close()


def test_a_wide_row_is_refused_on_the_server(store):
    """Review p4: a client-side cursor pulled 1,500 rows of 1 MB before the cap; a 500 MB value came whole."""
    import resource

    c = cartridge()
    t = Tools(c, store, TENANT, query=True)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out = t.query("SELECT i, repeat('x', 100000) AS blob FROM generate_series(1, 1500) i", limit=1000)
    assert "more" in out[0] and len(out) < 200
    with pytest.raises(ValueError, match="too large"):
        t.query("SELECT repeat('x', 50000000) AS blob", limit=1)
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 200 * 2**20


def test_an_agent_holding_keels_advisory_locks_stalls_it_no_longer_than_its_timeout(store):
    """An agent may take the advisory locks Keel's install and setup wait on; it holds them at most its query's
    timeout, and they are released after it whatever it did."""
    import copy as _copy

    c = cartridge()
    t = Tools(c, store, TENANT, query=True, query_timeout=1.5)
    th = threading.Thread(target=lambda: _quiet(t, "SELECT pg_advisory_lock(7413) AS a, pg_advisory_lock(7414) AS b, (SELECT 1 FROM pg_sleep(10)) AS s"))
    th.start()
    time.sleep(0.5)
    start = time.monotonic()
    other = _copy.copy(c)
    other.relations = {**c.relations, "extra_rel": {"from": "unit", "to": "unit"}}  # another revision: install runs again
    store.install(other)
    assert time.monotonic() - start < 4
    th.join()
    assert store._one("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'")["n"] == 0


def _quiet(t: Any, sql: str) -> None:
    try:
        t.query(sql)
    except ValueError:
        pass


def test_the_row_check_wrapper_cannot_be_escaped(store):
    """Second review q1/q2: the agent's text was pasted into the row-measuring wrapper with a first-word check only, so
    `... ) AS keel_q UNION ALL SELECT * FROM (...` closed it early and a 300 MB value reached Keel unmeasured."""
    import resource

    c = cartridge()
    t = Tools(c, store, TENANT, query=True)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for sql in ("SELECT repeat('x', 300000000) AS a\n) AS keel_q UNION ALL SELECT * FROM (SELECT 'y' AS a",
                "SELECT 1 AS a) AS keel_q; SELECT 2 AS b --",
                "SELECT 1 AS a /*"):
        with pytest.raises(ValueError):
            t.query(sql, limit=1)
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 100 * 2**20
    assert t.query("SELECT 1 AS a -- a trailing comment") == [{"a": 1}]
    assert t.query("SELECT 1 AS a;") == [{"a": 1}]


def test_the_timeout_bounds_the_whole_query_not_each_fetch(store):
    """Second review q2: statement_timeout restarted on every FETCH of the cursor, so a query whose rows each took a
    little ran 61.8 s under a 5 s timeout, holding its locks. One deadline now covers the whole query."""
    c = cartridge()
    t = Tools(c, store, TENANT, query=True, query_timeout=2)
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        t.query("SELECT i, CAST(pg_sleep(0.02) AS text) AS s FROM generate_series(1, 1000) i", limit=0)  # each FETCH under 2 s
    assert time.monotonic() - start < 4
    assert t.query("SELECT 1 AS one") == [{"one": 1}]
