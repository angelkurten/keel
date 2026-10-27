"""The store side of the optional `query` tool: read-only SQL over the schema generated from the cartridge
(keel/sqlgen.py), bound to one tenant by the database, never by the agent's SQL.

A store that serves it has `query_rows(tenant, cartridge, sql, cap, timeout) -> (columns, rows, more)` and
`QUERY_DIALECT`; Tools.query calls it. SqliteStore and DuckdbStore answer from a per-tenant sandbox built here:
a copy of only what the tenant may read (its accepted tenant-scope facts, its global partition's global facts, its
entities and relations), refreshed when the tenant's rows change. PostgresStore binds a restricted login role to
views instead (keel/store/postgres.py). Agent and task memory and proposals are never in what a query reads: the
store cannot apply Tools' per-caller visibility to arbitrary SQL, so it leaves them out.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from keel.scopes import Scope, global_of
from keel.sqlgen import FACT_COLUMNS

QUERY_MAX_CHARS = 20_000  # an agent's query; anything longer is refused before it reaches the database
ENTITY_COLUMNS = ("tenant", "kind", "id", "ref", "attrs")
RELATION_COLUMNS = ("seq", "tenant", "src", "rel", "dst", "valid_from", "valid_to", "recorded_at", "source", "closed_at", "changed_at")
_LEAD = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
_FIRST = re.compile(r"[A-Za-z]+|\(")
READS = ("select", "with", "values", "table", "from", "(")


def checked(sql: Any) -> str:
    """The agent's text, if it can be one query: a friendly first answer, not the isolation (the store's)."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("query takes one SELECT (or WITH ... SELECT) statement over the generated views")
    if len(sql) > QUERY_MAX_CHARS:
        raise ValueError(f"query is too long ({len(sql)} characters; at most {QUERY_MAX_CHARS})")
    m = _FIRST.match(sql, _LEAD.match(sql).end())
    if m is None or m.group(0).lower() not in READS:
        raise ValueError("query reads only: one SELECT (or WITH ... SELECT) statement over the generated views")
    return sql


def json_safe(v: Any) -> Any:
    """A database value as JSON: instants as ISO 8601 in UTC (a naive one is UTC), decimals as numbers."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, datetime):
        return (v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)).isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return str(v)


def visible(store: Any, tenant: str, now: str) -> dict[str, tuple[tuple[str, ...], list[tuple[Any, ...]]]]:
    """What a query may read of `tenant` in a SQL store, table by table: accepted (no status) facts of scope tenant,
    the global partition's global facts, unexpired at `now`; its entities and relations."""
    t, g = Scope.TENANT.value, Scope.GLOBAL.value
    facts = store._rows(f"SELECT {', '.join(FACT_COLUMNS)} FROM facts WHERE status IS NULL AND "
                        f"((tenant = ? AND scope = '{t}') OR (tenant = ? AND scope = '{g}')) "
                        "AND (expires_at IS NULL OR expires_at > ?) ORDER BY seq", (tenant, global_of(tenant), now))
    ents = store._rows(f"SELECT {', '.join(ENTITY_COLUMNS)} FROM entities WHERE tenant = ?", (tenant,))
    rels = store._rows(f"SELECT {', '.join(RELATION_COLUMNS)} FROM relations WHERE tenant = ? ORDER BY seq", (tenant,))
    tup = lambda rows, cols: [tuple(r[c] for c in cols) for r in rows]
    return {"facts": (FACT_COLUMNS, tup(facts, FACT_COLUMNS)), "entities": (ENTITY_COLUMNS, tup(ents, ENTITY_COLUMNS)),
            "relations": (RELATION_COLUMNS, tup(rels, RELATION_COLUMNS))}


def fingerprint(store: Any, tenant: str, now: str) -> tuple[Any, ...]:
    """What changes when anything a sandbox copied of `tenant` does (a write, a delete, an expiry, an entity's attrs)."""
    facts = store._one("SELECT count(*) AS n, max(seq) AS s, max(changed_at) AS c, max(closed_at) AS d, "
                       "sum(CASE WHEN expires_at IS NOT NULL AND expires_at <= ? THEN 1 ELSE 0 END) AS x "
                       "FROM facts WHERE tenant IN (?, ?)", (now, tenant, global_of(tenant)))
    rels = store._one("SELECT count(*) AS n, max(seq) AS s, max(changed_at) AS c FROM relations WHERE tenant = ?", (tenant,))
    ents = store._rows(f"SELECT {', '.join(ENTITY_COLUMNS)} FROM entities WHERE tenant = ? ORDER BY kind, id", (tenant,))
    return (tuple(facts[k] for k in ("n", "s", "c", "d", "x")), tuple(rels[k] for k in ("n", "s", "c")),
            hash(tuple(tuple(r[c] for c in ENTITY_COLUMNS) for r in ents)))


def clean_error(exc: BaseException) -> str:
    """A database error as one line for the agent (the first line: no context dump)."""
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__
