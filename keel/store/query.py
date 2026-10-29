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
import threading
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
    """What changes when anything a sandbox copied of `tenant` does (a write, a delete, an expiry, an entity's attrs).
    Its first element is how many rows a copy would hold."""
    facts = store._one("SELECT count(*) AS n, sum(seq) AS t, max(seq) AS s, max(changed_at) AS c, max(closed_at) AS d, "
                       "sum(CASE WHEN expires_at IS NOT NULL AND expires_at <= ? THEN 1 ELSE 0 END) AS x "
                       "FROM facts WHERE tenant IN (?, ?)", (now, tenant, global_of(tenant)))
    rels = store._one("SELECT count(*) AS n, sum(seq) AS t, max(seq) AS s, max(changed_at) AS c FROM relations WHERE tenant = ?", (tenant,))
    ents = store._rows(f"SELECT {', '.join(ENTITY_COLUMNS)} FROM entities WHERE tenant = ? ORDER BY kind, id", (tenant,))
    size = int(facts["n"] or 0) + int(rels["n"] or 0) + len(ents)
    return (size, tuple(facts[k] for k in ("n", "t", "s", "c", "d", "x")), tuple(rels[k] for k in ("n", "t", "s", "c")),
            hash(tuple(tuple(r[c] for c in ENTITY_COLUMNS) for r in ents)))


COPY_MAX = 250_000  # rows (facts, relations, entities) a sandbox copies of one tenant; a larger tenant is refused


class Sandboxes:
    """The per-tenant sandboxes of a store (SqliteStore, DuckdbStore), at most `keep`, the least recently queried
    dropped past it. `acquire` hands a box out with its lock held, never one that is closed: a box is closed (evicted,
    dropped on forget) only under its own lock, so never under a query another thread is running."""

    def __init__(self, make: Any, keep: int = 8) -> None:
        self.make, self.keep = make, keep
        self.boxes: dict[str, Any] = {}
        self.lock = threading.Lock()

    def acquire(self, tenant: str) -> Any:
        while True:
            with self.lock:
                box = self.boxes.pop(tenant, None) or self.make()
                self.boxes[tenant] = box  # most recent last
                evicted = []
                while len(self.boxes) > self.keep:
                    evicted.append(self.boxes.pop(next(iter(self.boxes))))
            for old in evicted:
                self._close(old)
            box.lock.acquire()
            if not box.closed:
                return box
            box.lock.release()  # closed between the lookup and the lock: take a fresh one

    def drop(self, tenant: str | None = None) -> None:
        """Close the tenant's sandbox (None: every one), as forget does to the data it copied."""
        with self.lock:
            if tenant is None:
                gone, self.boxes = list(self.boxes.values()), {}
            else:
                gone = [b for b in [self.boxes.pop(tenant, None)] if b is not None]
        for box in gone:
            self._close(box)

    @staticmethod
    def _close(box: Any) -> None:
        with box.lock:
            box.close()
            box.closed, box.mark, box.gate = True, None, None


def refresh(store: Any, box: Any, tenant: str, cartridge: Any, gate: Any, now: str) -> None:
    """Bring a sandbox (its lock held) up to date with the store: nothing to do while nothing was written anywhere
    (`gate`, the store's change counter) and no copied fact expired; else a fingerprint of the tenant, and a copy only
    when it changed. A tenant larger than COPY_MAX rows is refused. Never under the store's main lock."""
    if box.mark is not None and box.gate == gate and (box.expiry is None or now < box.expiry):
        return
    mark = (fingerprint(store, tenant, now), getattr(cartridge, "ident", None), repr(getattr(cartridge, "rules", None)),
            repr(getattr(cartridge, "entities", None)), repr(getattr(cartridge, "relations", None)))
    if mark != box.mark:
        if mark[0][0] > COPY_MAX:
            raise ValueError(f"this tenant is too large for query on this store ({mark[0][0]} rows; this store copies at most "
                             f"{COPY_MAX}): serve query from PostgresStore")
        tables = visible(store, tenant, now)
        box.load(tables, cartridge)
        box.mark = mark
        expiries = [r[FACT_COLUMNS.index("expires_at")] for r in tables["facts"][1]]
        box.expiry = min((e for e in expiries if e), default=None)
    box.gate = gate


class Box:
    """What every sandbox carries for `Sandboxes` and `refresh`."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mark: Any = None
        self.gate: Any = None
        self.expiry: str | None = None
        self.closed = False


def clean_error(exc: BaseException) -> str:
    """A database error as one line for the agent (the first line: no context dump)."""
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__
