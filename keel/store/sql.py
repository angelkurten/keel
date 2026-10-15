"""One SQL store, several dialects: SQLite (stdlib) and MySQL (PyMySQL or mysql-connector).

Rows of one subject are filtered in Python with the same functions MemoryStore uses, so every
store answers identically; the SQL stays plain: inserts, updates by key, selects by (tenant,
subject). Timestamps are ISO 8601 strings in UTC, so string order is time order in any engine.
A dialect supplies the schema, the placeholder style, the two statements that differ (upsert,
insert-or-ignore) and how to open a connection.

Thread safety: MCP servers run tool calls in worker threads and DB-API connections are not
thread-safe, so every statement runs under one lock on one connection.
ponytail: one connection per store; a pool if a deployment ever shows contention.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from keel.envelope import Event, utcnow
from keel.scopes import Scope
from keel.store.base import Entity, Fact, Relation
from keel.store.memory import (
    changed_after,
    filter_facts,
    filter_relations,
    in_scope,
    new_fact,
    place_fact,
    same_slot,
    search_in,
)


def _ts(d: datetime | None) -> str | None:
    """ISO 8601 in UTC, so string order is time order."""
    return None if d is None else d.astimezone(timezone.utc).isoformat()


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _json(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    return json.loads(v)


def _entity(row: Any) -> Entity:
    return Entity(tenant=row["tenant"], kind=row["kind"], id=row["id"], ref=row["ref"], attrs=_json(row["attrs"]))


def _relation(row: Any) -> Relation:
    return Relation(
        tenant=row["tenant"],
        src=row["src"],
        rel=row["rel"],
        dst=row["dst"],
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]),
        recorded_at=_dt(row["recorded_at"]),
        source=row["source"],
    )


def _fact(row: Any) -> Fact:
    return Fact(
        tenant=row["tenant"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=_json(row["value"]),
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]),
        recorded_at=_dt(row["recorded_at"]),
        source=row["source"],
        scope=Scope(row["scope"]),
        corrected_by=row["corrected_by"],
        evidence=row["evidence"],
        expires_at=_dt(row["expires_at"]),
        task=row["task"],
        idempotency_key=row["idempotency_key"],
        closed_at=_dt(row["closed_at"]),
        status=row["status"],
        trace=row["trace"],
    )


FACT_COLUMNS = (
    "tenant, subject, predicate, value, valid_from, valid_to, recorded_at, source, scope, "
    "corrected_by, evidence, expires_at, task, idempotency_key, closed_at, status, trace"
)
INSERT_FACT = f"INSERT INTO facts ({FACT_COLUMNS}) VALUES ({', '.join('?' for _ in range(17))})"


def _fact_row(f: Fact) -> tuple[Any, ...]:
    return (f.tenant, f.subject, f.predicate, json.dumps(f.value), _ts(f.valid_from), _ts(f.valid_to), _ts(f.recorded_at), f.source,
            f.scope.value, f.corrected_by, f.evidence, _ts(f.expires_at), f.task, f.idempotency_key, _ts(f.closed_at), f.status, f.trace)


class SqlStore:
    PLACEHOLDER = "?"
    SCHEMA = ""  # statements separated by ';', run at start, all IF NOT EXISTS
    UPSERT_ENTITY = ""  # (tenant, kind, id, ref, attrs); on conflict update ref and attrs
    REMEMBER = ""  # (tenant, id); insert or ignore
    ENTITY_ORDER = "rowid"
    _NAMES = re.compile(r"\b(entities_key|relations_src|relations_dst|facts_subject|facts_predicate|facts_recorded|facts_closed|facts_task|entities|relations|facts|ingested)\b")
    # columns added after 0.8: (name, type) added to existing tables on start, with their index
    ADDED_FACT_COLUMNS: tuple[tuple[str, str, str | None], ...] = ()
    INDEX_IF_NOT_EXISTS = False  # the dialect can rerun its index statements safely

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        """`prefix` names the tables `<prefix>entities`, `<prefix>facts`... for a database shared with an application."""
        self.prefix = prefix
        self._lock = threading.RLock()  # re-entrant: a transaction holds it across its statements
        self._in_tx = False
        # ponytail: reads cached only inside a transaction (we hold the lock, nobody else writes):
        # bulk loads repeat subjects, and every read skipped is a network round trip skipped.
        self._cache: dict[tuple[Any, ...], Any] | None = None
        self._bulk: _Bulk | None = None  # the batch overlay, see bulk()
        self.db = connect()
        self._prepare(self.db)
        for stmt in self.SCHEMA.split(";"):
            if stmt.strip():
                self._exec(stmt)
        have = self._columns("facts")
        for name, sqltype, index in self.ADDED_FACT_COLUMNS:  # additive migration for databases from <= 0.8
            if name not in have:
                self._exec(f"ALTER TABLE facts ADD COLUMN {name} {sqltype}")
            if index and (name not in have or self.INDEX_IF_NOT_EXISTS):
                self._exec(index)

    def _columns(self, table: str) -> set[str]:
        """Column names of a table (dialect hook)."""
        raise NotImplementedError

    # ---- dialect hooks ---------------------------------------------------------------

    def _prepare(self, conn: Any) -> None:
        """Pragmas, autocommit: whatever the connection needs once."""

    def _cursor(self, conn: Any) -> Any:
        """A cursor whose rows support row["column"]."""
        return conn.cursor()

    # ---- plumbing --------------------------------------------------------------------

    def _sql(self, sql: str) -> str:
        if self.prefix:
            sql = self._NAMES.sub(self.prefix + r"\1", sql)
        return sql if self.PLACEHOLDER == "?" else sql.replace("?", self.PLACEHOLDER)

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            return cur.rowcount

    def _insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Rows inserted; `self._last_id` holds the new autoincrement id."""
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            self._last_id = cur.lastrowid
            return cur.rowcount

    def _cached(self, key: tuple[Any, ...], load: Callable[[], Any]) -> Any:
        if self._cache is None:
            return load()
        if key not in self._cache:
            self._cache[key] = load()
        return self._cache[key]

    def _execmany(self, sql: str, rows: list[tuple[Any, ...]], size: int = 1000) -> None:
        """Multi-row writes. PyMySQL folds an INSERT ... VALUES executemany into one statement per
        chunk; sqlite3 runs them in-process. Either way: one round trip per chunk, not per row."""
        with self._lock:
            for i in range(0, len(rows), size):
                cur = self._cursor(self.db)
                cur.executemany(self._sql(sql), rows[i : i + size])

    def _close_rows(self, table: str, closes: dict[int, tuple[datetime | None, datetime | None]], size: int = 500) -> None:
        """One UPDATE ... CASE per chunk: PyMySQL's executemany is a round trip per row for UPDATE.
        `closes` maps seq -> (valid_to, closed_at); relations have no closed_at column."""
        items = list(closes.items())
        for i in range(0, len(items), size):
            part = items[i : i + size]
            case = " ".join("WHEN ? THEN ?" for _ in part)
            marks = ", ".join("?" for _ in part)
            params: list[Any] = []
            for seq, (valid_to, _) in part:
                params += [seq, _ts(valid_to)]
            sets = f"valid_to = CASE seq {case} END"
            if table == "facts":
                sets += f", closed_at = CASE seq {case} END"
                for seq, (_, closed_at) in part:
                    params += [seq, _ts(closed_at)]
            params += [seq for seq, _ in part]
            self._exec(f"UPDATE {table} SET {sets} WHERE seq IN ({marks})", tuple(params))

    @contextmanager
    def bulk(self, events: list[Event]) -> Iterator[None]:
        """A batch of events: the state of their subjects is read in a few statements, the handlers
        read and write memory, and the result is written back in a few multi-row statements, all in
        one transaction. Thousands of events cost tens of round trips instead of thousands."""
        with self.transaction():
            self._bulk = _Bulk(self)
            try:
                self._bulk.prefetch(events)
                yield
                self._bulk.flush()
            finally:
                self._bulk = None

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            return list(cur.fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One commit for everything inside, rolled back whole on an exception. The Ingestor wraps
        each event (or each batch) in one: the handler's primitives and `remember` land together,
        and a database that syncs per statement does it once instead of once per primitive."""
        if self._in_tx:  # nested: the outer one owns it
            yield
            return
        with self._lock:
            self._exec("BEGIN")
            self._in_tx, self._cache = True, {}
            try:
                yield
            except BaseException:
                self._exec("ROLLBACK")
                raise
            else:
                self._exec("COMMIT")
            finally:
                self._in_tx, self._cache = False, None

    # ---- writes (the primitives) -------------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None = None, **attrs: Any) -> Entity:
        if self._bulk is not None:
            return self._bulk.upsert_entity(tenant, kind, id, ref, attrs)
        key = f"{kind}:{id}"
        ent = self.entity(tenant, key)
        if ent is not None and (not ref or ref == ent.ref) and all(ent.attrs.get(k) == v for k, v in attrs.items()):
            return ent  # nothing to write
        ent = ent or Entity(tenant=tenant, kind=kind, id=id)
        if ref:
            ent.ref = ref
        ent.attrs.update(attrs)
        self._exec(self.UPSERT_ENTITY, (tenant, kind, id, ent.ref, json.dumps(ent.attrs)))
        if self._cache is not None:
            self._cache[("e", tenant, key)] = ent
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None) -> Relation:
        if self._bulk is not None:
            return self._bulk.link(tenant, src, rel, dst, valid_from, source)
        key = ("r", tenant, src, rel, dst)

        def load() -> Relation | None:
            row = self._one("SELECT * FROM relations WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL", (tenant, src, rel, dst))
            return _relation(row) if row else None

        existing = self._cached(key, load)
        if existing is not None:
            return existing  # already open; idempotent
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self._exec(
            "INSERT INTO relations (tenant, src, rel, dst, valid_from, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant, src, rel, dst, _ts(r.valid_from), _ts(r.recorded_at), source),
        )
        if self._cache is not None:
            self._cache[key] = r
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        if self._bulk is not None:
            self._bulk.unlink(tenant, src, rel, dst, valid_to)
            return
        self._exec(
            "UPDATE relations SET valid_to = ? WHERE tenant = ? AND src = ? AND rel = ? AND dst = ? AND valid_to IS NULL",
            (_ts(valid_to), tenant, src, rel, dst),
        )
        if self._cache is not None:
            self._cache.pop(("r", tenant, src, rel, dst), None)

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        if self._bulk is not None:
            return self._bulk.open_fact(tenant, subject, predicate, value, valid_from, kw)
        # never overwrite; the new fact takes its place by valid_from even when it arrives late (place_fact)
        key = ("f", tenant, subject, predicate)

        def load() -> list[tuple[Fact, int]]:
            rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? AND predicate = ?", (tenant, subject, predicate))
            return [(_fact(r), r["seq"]) for r in rows]

        same = self._cached(key, load)
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        closed = place_fact(f, same_slot((fact for fact, _ in same), f, kw.get("per_source", False)))
        if closed is not None:
            seq = next(s for fact, s in same if fact is closed)
            self._exec("UPDATE facts SET valid_to = ?, closed_at = ? WHERE seq = ?", (_ts(closed.valid_to), _ts(closed.closed_at), seq))
        self._insert(INSERT_FACT, _fact_row(f))
        same.append((f, self._last_id))
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT) -> None:
        if self._bulk is not None:
            self._bulk.close_fact(tenant, subject, predicate, valid_to, status, scope)
            return
        # a close that predates a fact's start is about an earlier period and leaves this fact alone;
        # `status` picks accepted facts (None) or proposals
        now = utcnow()
        which = "status IS NULL" if status is None else "status = ?"
        if scope is not None:  # tenant by default: a handler never closes agent or task memory
            which += " AND scope = ?"
        self._exec(
            f"UPDATE facts SET valid_to = ?, closed_at = ? WHERE tenant = ? AND subject = ? AND predicate = ? AND {which} AND valid_to IS NULL AND valid_from <= ?",
            (_ts(valid_to), _ts(now), tenant, subject, predicate, *(() if status is None else (status,)), *(() if scope is None else (Scope(scope).value,)), _ts(valid_to)),
        )
        if self._cache is not None:
            for fact, _ in self._cache.get(("f", tenant, subject, predicate), []):
                if fact.status == status and fact.valid_to is None and fact.valid_from <= valid_to and in_scope(fact, scope):
                    fact.valid_to, fact.closed_at = valid_to, now

    # ---- reads -------------------------------------------------------------------

    def entity(self, tenant: str, key: str) -> Entity | None:
        if self._bulk is not None:
            return self._bulk.entity(tenant, key)

        def load() -> Entity | None:
            kind, _, id = key.partition(":")
            row = self._one("SELECT * FROM entities WHERE tenant = ? AND kind = ? AND id = ?", (tenant, kind, id))
            return None if row is None else _entity(row)

        return self._cached(("e", tenant, key), load)

    def facts(
        self,
        tenant: str,
        subject: str,
        at: datetime | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        known_at: datetime | None = None,
    ) -> list[Fact]:
        if self._bulk is not None:
            return filter_facts(self._bulk.facts(tenant, subject), at, since, until, known_at)
        rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? ORDER BY seq", (tenant, subject))
        return filter_facts((_fact(r) for r in rows), at, since, until, known_at)

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]:
        """Every entity of a kind (or all), ordered by key: the work list of a sweep."""
        if kind is None:
            rows = self._rows("SELECT * FROM entities WHERE tenant = ? ORDER BY kind, id", (tenant,))
        else:
            rows = self._rows("SELECT * FROM entities WHERE tenant = ? AND kind = ? ORDER BY id", (tenant, kind))
        return [_entity(r) for r in rows]

    def changes(self, tenant: str, after: datetime) -> list[Fact]:
        """Facts recorded or closed after a watermark, across subjects."""
        a = _ts(after)
        rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND (recorded_at > ? OR closed_at > ?) ORDER BY seq", (tenant, a, a))
        now = utcnow()
        return [f for f in changed_after((_fact(r) for r in rows), after) if f.expires_at is None or f.expires_at > now]

    def relation_changes(self, tenant: str, after: datetime) -> list[Relation]:
        """Relations recorded after a watermark (links; an unlink carries no transaction time yet)."""
        return [_relation(r) for r in self._rows("SELECT * FROM relations WHERE tenant = ? AND recorded_at > ? ORDER BY seq", (tenant, _ts(after)))]

    def watermark(self, tenant: str) -> datetime | None:
        """The latest transaction time in the tenant (facts and links): a cursor for `changes`."""
        row = self._one("SELECT MAX(recorded_at) AS r, MAX(closed_at) AS c FROM facts WHERE tenant = ?", (tenant,))
        rel = self._one("SELECT MAX(recorded_at) AS r FROM relations WHERE tenant = ?", (tenant,))
        times = [_dt(row[k]) for k in ("r", "c") if row and row[k]] + ([_dt(rel["r"])] if rel and rel["r"] else [])
        return max(times) if times else None

    def close_task(self, tenant: str, task: str, now: datetime | None = None) -> int:
        """End a task: its memory expires now (reads stop seeing it; `expire` removes it later)."""
        n = _ts(now or utcnow())
        return self._exec("UPDATE facts SET expires_at = ? WHERE tenant = ? AND task = ? AND (expires_at IS NULL OR expires_at > ?)", (n, tenant, task, n))

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        if self._bulk is not None:
            return filter_relations(self._bulk.relations(tenant, key), rel, at)
        rows = self._rows("SELECT * FROM relations WHERE tenant = ? AND (src = ? OR dst = ?) ORDER BY seq", (tenant, key, key))
        return filter_relations((_relation(r) for r in rows), rel, at)

    def search(self, tenant: str, text: str) -> list[Entity | Fact]:
        # ponytail: scans one tenant in Python; switch to LIKE or full-text when a tenant outgrows memory
        ents = [_entity(r) for r in self._rows(f"SELECT * FROM entities WHERE tenant = ? ORDER BY {self.ENTITY_ORDER}", (tenant,))]
        facts = [_fact(r) for r in self._rows("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,))]
        return search_in(ents, facts, text)

    # ---- ingest bookkeeping -----------------------------------------------------------

    def seen(self, tenant: str, event_id: str) -> bool:
        return self._one("SELECT 1 AS one FROM ingested WHERE tenant = ? AND id = ?", (tenant, event_id)) is not None

    def remember(self, tenant: str, event_id: str) -> bool:
        if self._bulk is not None:
            return self._bulk.remember(tenant, event_id)
        return self._exec(self.REMEMBER, (tenant, event_id)) == 1  # insert-or-ignore: 0 rows means already there

    # ---- maintenance --------------------------------------------------------------

    def expire(self, tenant: str, now: datetime | None = None) -> int:
        """Drop expired memory. Called by the consolidation job; never by handlers."""
        if self._cache is not None:
            self._cache.clear()
        return self._exec(
            "DELETE FROM facts WHERE tenant = ? AND expires_at IS NOT NULL AND expires_at <= ?", (tenant, _ts(now or utcnow()))
        )

    def dump(self, tenant: str) -> Iterator[Entity | Relation | Fact]:
        """Everything about one tenant, closed facts included: the audit trail."""
        for row in self._rows(f"SELECT * FROM entities WHERE tenant = ? ORDER BY {self.ENTITY_ORDER}", (tenant,)):
            yield _entity(row)
        for row in self._rows("SELECT * FROM relations WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _relation(row)
        for row in self._rows("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,)):
            yield _fact(row)

    def reset(self, tenant: str) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it."""
        for table in ("entities", "relations", "facts", "ingested"):
            self._exec(f"DELETE FROM {table} WHERE tenant = ?", (tenant,))
        if self._cache is not None:
            self._cache.clear()


def _in(n: int) -> str:
    return ", ".join("?" for _ in range(n))


class _Bulk:
    """The overlay behind `SqlStore.bulk`: everything a batch touches, in memory, with what changed.

    Keys are prefetched from the batch's envelope subjects; anything a handler reaches beyond them
    (a payload id, a neighbour) is loaded lazily, one key at a time, merged with the batch's own
    unsaved writes so reads never miss them. `flush` writes entities, relations, facts, closes and
    the dedupe marks in a handful of multi-row statements.
    """

    CHUNK = 1000

    def __init__(self, store: SqlStore) -> None:
        self.s = store
        self.entities: dict[tuple[str, str], Entity | None] = {}
        self.dirty_entities: set[tuple[str, str]] = set()
        self.facts_by: dict[tuple[str, str], list[list[Any]]] = {}  # (tenant, subject) -> [[Fact, seq|None], ...]
        self.new_facts: list[Fact] = []
        self.closed_facts: dict[int, Fact] = {}
        self.rels_by: dict[tuple[str, str], list[list[Any]]] = {}  # (tenant, key) -> [[Relation, seq|None], ...]
        self.new_rels: list[Relation] = []
        self.closed_rels: dict[int, Relation] = {}
        self.seen: dict[str, set[str]] = {}
        self.known_ids: dict[str, set[str]] = {}  # ids whose presence we checked
        self.new_ids: list[tuple[str, str]] = []

    # ---- prefetch: the batch's subjects and ids in a few statements ----------------------------

    def prefetch(self, events: list[Event]) -> None:
        by_tenant: dict[str, tuple[set[str], set[str]]] = {}
        for ev in events:
            subjects, ids = by_tenant.setdefault(ev.tenant, (set(), set()))
            ids.add(ev.id)
        for tenant, (_, ids) in by_tenant.items():  # ids first: a redelivered batch costs one lookup and no state
            known = self.known_ids.setdefault(tenant, set())
            present = self.seen.setdefault(tenant, set())
            id_list = sorted(ids)
            for i in range(0, len(id_list), self.CHUNK):
                part = id_list[i : i + self.CHUNK]
                known.update(part)
                for row in self.s._rows(f"SELECT id FROM ingested WHERE tenant = ? AND id IN ({_in(len(part))})", (tenant, *part)):
                    present.add(row["id"])
        for ev in events:
            if ev.id not in self.seen[ev.tenant]:
                by_tenant[ev.tenant][0].add(ev.subject)
        for tenant, (subjects, _) in by_tenant.items():
            subs = sorted(subjects)
            for i in range(0, len(subs), self.CHUNK):
                part = subs[i : i + self.CHUNK]
                for sub in part:
                    self.facts_by.setdefault((tenant, sub), [])
                    self.rels_by.setdefault((tenant, sub), [])
                for row in self.s._rows(f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({_in(len(part))}) ORDER BY seq", (tenant, *part)):
                    self.facts_by[(tenant, row["subject"])].append([_fact(row), row["seq"]])
                pairs = [tuple(sub.partition(":")[::2]) for sub in part]
                flat = [x for kind, id in pairs for x in (kind, id)]
                rows = self.s._rows(f"SELECT * FROM entities WHERE tenant = ? AND (kind, id) IN ({', '.join('(?, ?)' for _ in pairs)})", (tenant, *flat))
                found = {f"{r['kind']}:{r['id']}": _entity(r) for r in rows}
                for sub in part:
                    self.entities[(tenant, sub)] = found.get(sub)
                seen_seq: set[int] = set()
                for col in ("src", "dst"):
                    for row in self.s._rows(f"SELECT * FROM relations WHERE tenant = ? AND {col} IN ({_in(len(part))}) ORDER BY seq", (tenant, *part)):
                        if row["seq"] in seen_seq:
                            continue
                        seen_seq.add(row["seq"])
                        r = _relation(row)
                        for key in {r.src, r.dst}:
                            if (tenant, key) in self.rels_by:
                                self.rels_by[(tenant, key)].append([r, row["seq"]])

    # ---- lazy loads, merged with the batch's own writes --------------------------------------

    def entity(self, tenant: str, key: str) -> Entity | None:
        k = (tenant, key)
        if k not in self.entities:
            kind, _, id = key.partition(":")
            row = self.s._one("SELECT * FROM entities WHERE tenant = ? AND kind = ? AND id = ?", (tenant, kind, id))
            self.entities[k] = None if row is None else _entity(row)
        return self.entities[k]

    def _facts(self, tenant: str, subject: str) -> list[list[Any]]:
        k = (tenant, subject)
        if k not in self.facts_by:
            rows = self.s._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? ORDER BY seq", (tenant, subject))
            self.facts_by[k] = [[_fact(r), r["seq"]] for r in rows] + [[f, None] for f in self.new_facts if f.tenant == tenant and f.subject == subject]
        return self.facts_by[k]

    def facts(self, tenant: str, subject: str) -> list[Fact]:
        return [f for f, _ in self._facts(tenant, subject)]

    def _rels(self, tenant: str, key: str) -> list[list[Any]]:
        k = (tenant, key)
        if k not in self.rels_by:
            rows = self.s._rows("SELECT * FROM relations WHERE tenant = ? AND (src = ? OR dst = ?) ORDER BY seq", (tenant, key, key))
            self.rels_by[k] = [[_relation(r), r["seq"]] for r in rows] + [[r, None] for r in self.new_rels if r.tenant == tenant and key in (r.src, r.dst)]
        return self.rels_by[k]

    def relations(self, tenant: str, key: str) -> list[Relation]:
        return [r for r, _ in self._rels(tenant, key)]

    # ---- writes: memory now, statements at flush ---------------------------------------------

    def upsert_entity(self, tenant: str, kind: str, id: str, ref: str | None, attrs: dict[str, Any]) -> Entity:
        key = f"{kind}:{id}"
        ent = self.entity(tenant, key)
        if ent is not None and (not ref or ref == ent.ref) and all(ent.attrs.get(k) == v for k, v in attrs.items()):
            return ent
        ent = ent or Entity(tenant=tenant, kind=kind, id=id)
        if ref:
            ent.ref = ref
        ent.attrs.update(attrs)
        self.entities[(tenant, key)] = ent
        self.dirty_entities.add((tenant, key))
        return ent

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None) -> Relation:
        for r, _ in self._rels(tenant, src):
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                return r
        r = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=utcnow(), source=source)
        self.new_rels.append(r)
        self._rels(tenant, src).append([r, None])
        if dst != src and (tenant, dst) in self.rels_by:
            self.rels_by[(tenant, dst)].append([r, None])
        return r

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime) -> None:
        for r, seq in self._rels(tenant, src):
            if r.src == src and r.rel == rel and r.dst == dst and r.valid_to is None:
                r.valid_to = valid_to
                if seq is not None:
                    self.closed_rels[seq] = r

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, kw: dict[str, Any]) -> Fact:
        same = self._facts(tenant, subject)
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        closed = place_fact(f, same_slot((x for x, _ in same), f, kw.get("per_source", False)))
        if closed is not None:
            seq = next(s for x, s in same if x is closed)
            if seq is not None:
                self.closed_facts[seq] = closed  # an unsaved one carries its valid_to into its own INSERT
        same.append([f, None])
        self.new_facts.append(f)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT) -> None:
        now = utcnow()
        for f, seq in self._facts(tenant, subject):
            if f.predicate == predicate and f.status == status and f.valid_to is None and f.valid_from <= valid_to and in_scope(f, scope):
                f.valid_to, f.closed_at = valid_to, now
                if seq is not None:
                    self.closed_facts[seq] = f

    def remember(self, tenant: str, event_id: str) -> bool:
        known = self.known_ids.setdefault(tenant, set())
        present = self.seen.setdefault(tenant, set())
        if event_id not in known:
            known.add(event_id)
            if self.s._one("SELECT 1 AS one FROM ingested WHERE tenant = ? AND id = ?", (tenant, event_id)) is not None:
                present.add(event_id)
        if event_id in present:
            return False
        present.add(event_id)
        self.new_ids.append((tenant, event_id))
        return True

    # ---- flush ---------------------------------------------------------------------------------

    def flush(self) -> None:
        s = self.s
        if self.dirty_entities:
            rows = [(t, e.kind, e.id, e.ref, json.dumps(e.attrs)) for (t, _), e in ((k, self.entities[k]) for k in self.dirty_entities) if e is not None]
            s._execmany(s.UPSERT_ENTITY, rows)
        if self.new_rels:
            s._execmany(
                "INSERT INTO relations (tenant, src, rel, dst, valid_from, valid_to, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(r.tenant, r.src, r.rel, r.dst, _ts(r.valid_from), _ts(r.valid_to), _ts(r.recorded_at), r.source) for r in self.new_rels],
            )
        if self.closed_rels:
            s._close_rows("relations", {seq: (r.valid_to, None) for seq, r in self.closed_rels.items()})
        if self.new_facts:
            s._execmany(INSERT_FACT, [_fact_row(f) for f in self.new_facts])
        if self.closed_facts:
            s._close_rows("facts", {seq: (f.valid_to, f.closed_at) for seq, f in self.closed_facts.items()})
        if self.new_ids:
            s._execmany(s.REMEMBER, self.new_ids)
