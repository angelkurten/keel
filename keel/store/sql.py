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
import math
import re
import threading
import warnings
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from keel.envelope import Event, aware, utcnow
from keel.scopes import Scope, global_of, is_global
from keel.store.base import Account, ApiKey, DuplicateKey, Entity, Fact, Relation
from keel.store.memory import (
    ANY,
    CLOSE,
    SlotIndex,
    changed_after,
    close_mark,
    filter_facts,
    filter_relations,
    in_scope,
    is_close,
    is_relation_mark,
    new_fact,
    place_close,
    place_fact,
    rel_start,
    relation_history,
    relation_tail,
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


def _covering(rows: list[Relation], new: Relation) -> Relation:
    """What link returns: the period holding at its instant, else the row its own event became."""
    t = new.valid_from
    held = [r for r in rows if not is_relation_mark(r) and r.valid_from <= t and (r.valid_to is None or r.valid_to > t)]
    return held[0] if held else next((r for r in rows if rel_start(r) == rel_start(new)), new)


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
        changed_at=_dt(row["changed_at"]),
        closed_at=_dt(row["closed_at"]),
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
        cartridge=row["cartridge"],
        changed_at=_dt(row["changed_at"]),
    )


FACT_COLUMNS = (
    "tenant, subject, predicate, value, valid_from, valid_to, recorded_at, source, scope, "
    "corrected_by, evidence, expires_at, task, idempotency_key, closed_at, status, trace, cartridge, changed_at"
)
# Two indexed branches, never `a = ? OR b = ?`: without table statistics SQLite plans that OR on the
# tenant prefix alone and scans the whole tenant, so each read (and ingest) grew with the tenant.
REL_MARK = "(valid_to IS NOT NULL AND valid_to = valid_from)"  # is_relation_mark in SQL
RELATIONS_OF = "SELECT * FROM relations WHERE tenant = ? AND src = ? UNION ALL SELECT * FROM relations WHERE tenant = ? AND dst = ? ORDER BY seq"
CHANGES = "SELECT * FROM facts WHERE tenant = ? AND changed_at > ? ORDER BY seq"
NOT_CLOSE = "(source IS NULL OR source <> ?)"  # not a close mark (is_close in SQL), with CLOSE as its parameter


def _once(rows: list[Any]) -> list[Any]:
    """Rows of a UNION ALL, each seq once (a self-link)."""
    seen: set[int] = set()
    return [r for r in rows if not (r["seq"] in seen or seen.add(r["seq"]))]


INSERT_FACT = f"INSERT INTO facts ({FACT_COLUMNS}) VALUES ({', '.join('?' for _ in range(19))})"
INSERT_RELATION = "INSERT INTO relations (tenant, src, rel, dst, valid_from, valid_to, recorded_at, source, changed_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"


def _fact_row(f: Fact) -> tuple[Any, ...]:
    return (f.tenant, f.subject, f.predicate, json.dumps(f.value), _ts(f.valid_from), _ts(f.valid_to), _ts(f.recorded_at), f.source,
            f.scope.value, f.corrected_by, f.evidence, _ts(f.expires_at), f.task, f.idempotency_key, _ts(f.closed_at), f.status, f.trace, f.cartridge,
            _ts(f.changed_at))


def _relation_row(r: Relation) -> tuple[Any, ...]:
    return (r.tenant, r.src, r.rel, r.dst, _ts(r.valid_from), _ts(r.valid_to), _ts(r.recorded_at), r.source, _ts(r.changed_at), _ts(r.closed_at))


class SqlStore:
    PLACEHOLDER = "?"
    SCHEMA = ""  # statements separated by ';', run at start, all IF NOT EXISTS
    UPSERT_ENTITY = ""  # (tenant, kind, id, ref, attrs); on conflict update ref and attrs
    REMEMBER = ""  # (tenant, id); insert or ignore
    TOMBSTONE = ""  # (tenant, at) into forgotten; insert or ignore, so the first erasure's time stays
    METER = ""  # (tenant, day, item, n) into metering; on conflict add n
    ENTITY_ORDER = "rowid"
    _NAMES = re.compile(r"\b(entities_key|facts_key|relations_src|relations_dst|relations_recorded|relations_changed|facts_open|facts_from|facts_slot|facts_subject|facts_predicate|facts_recorded|facts_closed|facts_changed|facts_task|api_keys|accounts|metering|cutoffs|entities|relations|facts|ingested|forgotten)\b")
    # columns added after 0.8: (name, type) added to existing tables on start, with their index
    ADDED_FACT_COLUMNS: tuple[tuple[str, str, str | None], ...] = ()
    TIME = "TEXT"  # the dialect's type for a timestamp column
    INDEX_IF_NOT_EXISTS = False  # the dialect can rerun its index statements safely
    PER_THREAD_READS = False  # a networked database: each thread reads on its own connection, never queued behind the lock
    JSON_TEXT = True  # values are stored as json.dumps text (ASCII): subjects_where may compare that text; MySQL's JSON column is not
    # attribute access for subjects_where (SQLite's JSON1): the type of attrs at a path, its text, whether a text is ASCII
    JSON_TYPE, JSON_STR, JT_TEXT, JT_ARRAY = "json_type({0}, {1})", "json_extract({0}, {1})", "'text'", "'array'"
    JT_NULL, JT_NUMBER, JT_BOOL = "'null'", "'integer', 'real'", "'true', 'false'"
    ASCII = "(length({0}) = length(CAST({0} AS BLOB)))"
    JSON_PATH = "$.\"{}\""  # an attribute as the JSON functions name it
    INSTR = "instr({0}, {1})"

    def __init__(self, connect: Callable[[], Any], prefix: str = "") -> None:
        """`prefix` names the tables `<prefix>entities`, `<prefix>facts`... for a database shared with an application."""
        self.prefix = prefix
        self._lock = threading.RLock()  # re-entrant: a transaction holds it across its statements
        # A transaction, its read cache and a batch overlay belong to the thread that opened them: a server
        # thread that reads or writes meanwhile waits for the lock or reads committed state, never the batch.
        # ponytail: reads cached only inside a transaction (we hold the lock, nobody else writes):
        # bulk loads repeat subjects, and every read skipped is a network round trip skipped.
        self._mine = threading.local()
        self._connect = connect
        self.db = connect()
        self._prepare(self.db)
        for stmt in self.SCHEMA.split(";"):
            if stmt.strip():
                self._exec(stmt)
        have = self._columns("facts")
        for name, sqltype, index in self.ADDED_FACT_COLUMNS:  # additive migration for databases from <= 0.8
            if name not in have:
                self._ddl(f"ALTER TABLE facts ADD COLUMN {name} {sqltype}", lambda n=name: n in self._columns("facts"))
            if index and (name not in have or self.INDEX_IF_NOT_EXISTS):
                self._ddl(index, lambda i=index: self._has_index("facts", re.search(r"INDEX (\w+)", i).group(1)))
        self._add_changed_at()
        if "closed_at" not in self._columns("relations"):  # since 0.33: the transaction time of an unlink
            self._ddl(f"ALTER TABLE relations ADD COLUMN closed_at {self.TIME}", lambda: "closed_at" in self._columns("relations"))
            self._mark_old_closes()  # a database from <= 0.32 (no relations.closed_at) never wrote a close mark
        if not self._has_index("relations", "relations_recorded"):  # transaction-time reads of links
            self._ddl("CREATE INDEX relations_recorded ON relations (tenant, recorded_at)", lambda: self._has_index("relations", "relations_recorded"))
        if not self._has_index("facts", "facts_slot"):  # a late fact reads its neighbours, not its history
            self._ddl("CREATE INDEX facts_slot ON facts (tenant, subject, predicate, valid_from)", lambda: self._has_index("facts", "facts_slot"))
        if not self._has_index("facts", "facts_from"):  # a tenant's history window (facts_between) reads its window, not the tenant
            self._ddl("CREATE INDEX facts_from ON facts (tenant, valid_from)", lambda: self._has_index("facts", "facts_from"))
        if not self._has_index("facts", "facts_open"):  # a bulk batch prefetches only what it can still change
            self._ddl("CREATE INDEX facts_open ON facts (tenant, subject, valid_to)", lambda: self._has_index("facts", "facts_open"))
        if not self._has_index("facts", "facts_key"):  # annotate(key=) stays one write even across processes
            try:
                self._ddl("CREATE UNIQUE INDEX facts_key ON facts (tenant, subject, idempotency_key)", lambda: self._has_index("facts", "facts_key"))
            except Exception as exc:  # an older database may already hold a key written twice
                if type(exc).__name__ != "IntegrityError":  # every driver's name for it
                    raise
                warnings.warn(f"facts_key not created, so annotate(key=) is not race-safe here: {exc}", stacklevel=2)

    # per-thread state: see __init__
    @property
    def _in_tx(self) -> bool:
        return getattr(self._mine, "in_tx", False)

    @_in_tx.setter
    def _in_tx(self, v: bool) -> None:
        self._mine.in_tx = v

    @property
    def _cache(self) -> dict[tuple[Any, ...], Any] | None:
        return getattr(self._mine, "cache", None)

    @_cache.setter
    def _cache(self, v: dict[tuple[Any, ...], Any] | None) -> None:
        self._mine.cache = v

    @property
    def _bulk(self) -> _Bulk | None:
        return getattr(self._mine, "bulk", None)

    @_bulk.setter
    def _bulk(self, v: _Bulk | None) -> None:
        self._mine.bulk = v

    def _reader(self) -> Any:
        """This thread's read connection (PER_THREAD_READS): autocommit, so each read sees what is committed."""
        conn = getattr(self._mine, "conn", None)
        if conn is None:
            conn = self._mine.conn = self._connect()
            self._prepare(conn)
        return conn

    def _mark_old_closes(self) -> None:
        """Releases up to 0.32 kept an explicit close_fact only as the fact's valid_to. A tenant fact whose
        valid_to no later fact of its predicate starts at was closed that way: write the close mark it would
        have today, so a late fact still meets the close. Learned when the close was; no change to the cursor."""
        # ponytail: one pass over the accepted tenant facts, once; two processes migrating at once write a mark twice, which ends nothing more
        rows = self._rows("SELECT * FROM facts WHERE status IS NULL AND scope = ? ORDER BY seq", (Scope.TENANT.value,))
        starts = {(r["tenant"], r["subject"], r["predicate"], _dt(r["valid_from"])) for r in rows}  # parsed: old releases wrote other formats
        marks = []
        for r in rows:
            if r["valid_to"] is not None and r["source"] != CLOSE and (r["tenant"], r["subject"], r["predicate"], _dt(r["valid_to"])) not in starts:
                c = close_mark(r["tenant"], r["subject"], r["predicate"], _dt(r["valid_to"]), None, Scope.TENANT, _dt(r["closed_at"]) or _dt(r["recorded_at"]))
                c.changed_at = _dt(r["changed_at"])  # _add_changed_at ran first
                marks.append(_fact_row(c))
        if marks:
            self._execmany(INSERT_FACT, marks)

    def _add_changed_at(self) -> None:
        """Since 0.32: `changed_at`, the ingest clock of a row's last write, is the cursor of `changes` and
        the watermark (transaction time may come from the event). Rows from before it take their latest
        transaction time, which is what the watermark was then."""
        for table, latest in (("facts", "CASE WHEN closed_at IS NOT NULL AND closed_at > recorded_at THEN closed_at ELSE recorded_at END"),
                              ("relations", "recorded_at")):
            if "changed_at" not in self._columns(table):
                self._ddl(f"ALTER TABLE {table} ADD COLUMN changed_at {self.TIME}", lambda t=table: "changed_at" in self._columns(t))
                # ponytail: one UPDATE over the table, once; a process that dies before it leaves old rows out of changes()
                self._exec(f"UPDATE {table} SET changed_at = {latest} WHERE changed_at IS NULL")
            index = f"{table}_changed"
            if not self._has_index(table, index):
                self._ddl(f"CREATE INDEX {index} ON {table} (tenant, changed_at)", lambda t=table, i=index: self._has_index(t, i))

    def _ddl(self, stmt: str, done: Callable[[], bool]) -> None:
        """A migration step several processes may race to run on one database: when it fails because
        another process already did it (MySQL 1060/1061, SQLite "already exists"), carry on."""
        try:
            self._exec(stmt)
        except Exception:
            if not done():
                raise

    def _columns(self, table: str) -> set[str]:
        """Column names of a table (dialect hook)."""
        raise NotImplementedError

    def _has_index(self, table: str, name: str) -> bool:
        """Whether the (prefixed) index exists (dialect hook)."""
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
            self._writes = getattr(self, "_writes", 0) + 1  # a change counter for query's sandboxes (keel/store/query.py)
            cur = self._cursor(self.db)
            cur.execute(self._sql(sql), params)
            return cur.rowcount

    def _insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Rows inserted; `self._last_id` holds the new autoincrement id."""
        with self._lock:
            self._writes = getattr(self, "_writes", 0) + 1
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
            self._writes = getattr(self, "_writes", 0) + 1
            for i in range(0, len(rows), size):
                cur = self._cursor(self.db)
                cur.executemany(self._sql(sql), rows[i : i + size])

    def _close_rows(self, table: str, closes: dict[int, tuple[datetime | None, datetime | None, datetime | None]], size: int = 500) -> None:
        """One UPDATE ... CASE per chunk: PyMySQL's executemany is a round trip per row for UPDATE.
        `closes` maps seq -> (valid_to, closed_at, changed_at)."""
        items = list(closes.items())
        columns = ("valid_to", "closed_at", "changed_at")
        for i in range(0, len(items), size):
            part = items[i : i + size]
            case = " ".join("WHEN ? THEN ?" for _ in part)
            marks = ", ".join("?" for _ in part)
            params: list[Any] = []
            assigns = []
            for n, column in enumerate(columns):
                if column:
                    assigns.append(f"{column} = CASE seq {case} END")
                    for seq, values in part:
                        params += [seq, _ts(values[n])]
            sets = ", ".join(assigns)
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
        if self.PER_THREAD_READS and not self._in_tx:  # inside its own transaction a thread reads what it wrote
            cur = self._cursor(self._reader())
            cur.execute(self._sql(sql), params)
            return list(cur.fetchall())
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

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None = None, recorded_at: datetime | None = None) -> Relation:
        valid_from, recorded_at = aware(valid_from), aware(recorded_at)
        if self._bulk is not None:
            return self._bulk.link(tenant, src, rel, dst, valid_from, source, recorded_at)
        new = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=recorded_at or utcnow(), source=source)
        return self._relate(new, unlink=False)

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime, recorded_at: datetime | None = None) -> None:
        valid_to, recorded_at = aware(valid_to), aware(recorded_at)
        if self._bulk is not None:
            self._bulk.unlink(tenant, src, rel, dst, valid_to, recorded_at)
            return
        self._relate(Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_to, recorded_at=recorded_at or utcnow()), unlink=True)

    def _relate(self, new: Relation, unlink: bool) -> Relation:
        """MemoryStore._relate over the relation's rows: insert the new ones, move the ends that changed, delete
        the marks whose event now opens or ends a period."""
        triple = (new.tenant, new.src, new.rel, new.dst)
        last = self._one("SELECT MAX(valid_from) AS f, MAX(valid_to) AS t FROM relations WHERE tenant = ? AND src = ? AND rel = ? AND dst = ?", triple)
        late = last is not None and any(x is not None and x >= _ts(new.valid_from) for x in (last["f"], last["t"]))
        # in order (the usual case) only the open period matters; a late event reads the relation's whole history
        which = "" if late else " AND valid_to IS NULL"
        rows = [(_relation(r), r["seq"]) for r in self._rows(f"SELECT * FROM relations WHERE tenant = ? AND src = ? AND rel = ? AND dst = ?{which}", triple)]
        tail = None if late else relation_tail([r for r, _ in rows], new, unlink)
        held: dict[tuple[Any, ...], list[tuple[Relation, int]]] = {}
        for r, seq in rows:
            held.setdefault(rel_start(r), []).append((r, seq))
        now, final = utcnow(), [r for r, _ in rows] if tail is not None else []
        for spec in tail if tail is not None else relation_history([r for r, _ in rows], new, unlink):
            bucket = held.get(rel_start(spec))
            if not bucket:
                spec.changed_at = now
                self._exec(INSERT_RELATION, _relation_row(spec))
                final.append(spec)
                continue
            r, seq = bucket.pop()
            if (r.valid_to, r.closed_at) != (spec.valid_to, spec.closed_at):
                r.valid_to, r.closed_at, r.changed_at = spec.valid_to, spec.closed_at, now
                self._exec("UPDATE relations SET valid_to = ?, closed_at = ?, changed_at = ? WHERE seq = ?", (_ts(r.valid_to), _ts(r.closed_at), _ts(now), seq))
            if not any(x is r for x in final):
                final.append(r)
        for _, seq in (x for bucket in held.values() for x in bucket) if tail is None else ():
            self._exec("DELETE FROM relations WHERE seq = ?", (seq,))
        return _covering(final, new)

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, **kw: Any) -> Fact:
        if self._bulk is not None:
            return self._bulk.open_fact(tenant, subject, predicate, value, valid_from, kw)
        # never overwrite; the new fact takes its place by valid_from even when it arrives late (place_fact)
        key = ("f", tenant, subject, predicate)

        def load() -> list[tuple[Fact, int]]:
            rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? AND predicate = ?", (tenant, subject, predicate))
            return [(_fact(r), r["seq"]) for r in rows]

        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        same = self._cache.get(key) if self._cache is not None else None
        if same is not None:  # this transaction already read the history: place against it
            closed = place_fact(f, same_slot((fact for fact, _ in same), f, kw.get("per_source", False)), (fact for fact, _ in same))
            near = same
        else:  # only the two neighbours and the close marks, never the whole history (it keeps growing)
            near = self._neighbours(f, kw.get("per_source", False))
            marks = [_fact(r) for r in self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? AND predicate = ? AND source = ?",
                                                  (tenant, subject, predicate, CLOSE))]
            closed = place_fact(f, [fact for fact, _ in near], marks)
        try:  # insert before closing the previous fact: a refused insert leaves nothing half done
            self._insert(INSERT_FACT, _fact_row(f))
        except Exception as exc:  # each driver has its own IntegrityError; the key decides if it is ours
            row = f.idempotency_key and self._one(
                "SELECT * FROM facts WHERE tenant = ? AND subject = ? AND idempotency_key = ?", (tenant, subject, f.idempotency_key)
            )
            if not row:
                raise
            raise DuplicateKey(_fact(row)) from exc
        if closed is not None:
            seq = next(s for fact, s in near if fact is closed)
            self._exec("UPDATE facts SET valid_to = ?, closed_at = ?, changed_at = ? WHERE seq = ?", (_ts(closed.valid_to), _ts(closed.closed_at), _ts(closed.changed_at), seq))
        if same is not None:
            same.append((f, self._last_id))
        return f

    def _neighbours(self, f: Fact, per_source: bool) -> list[tuple[Fact, int]]:
        """The rows of `f`'s slot (same_slot) at the valid_from just before it, at its own and just after it:
        the facts that succession places right before and after `f` are among them, whatever the ties."""
        scope = Scope(f.scope)
        where, args = ["tenant = ?", "subject = ?", "predicate = ?", "(source IS NULL OR source <> ?)", "scope = ?"], [f.tenant, f.subject, f.predicate, CLOSE, scope.value]
        pairs = [("status", f.status)]
        if scope == Scope.TASK:
            pairs.append(("task", f.task))
        if scope == Scope.AGENT or per_source:
            pairs.append(("source", f.source))
        if per_source:
            pairs.append(("corrected_by", f.corrected_by))
        for col, v in pairs:
            where.append(f"{col} IS NULL" if v is None else f"{col} = ?")
            args += [] if v is None else [v]
        w, at = " AND ".join(where), _ts(f.valid_from)
        rows = self._rows(f"SELECT * FROM facts WHERE {w} AND valid_from IN (?, (SELECT MAX(valid_from) FROM facts WHERE {w} AND valid_from < ?), "
                          f"(SELECT MIN(valid_from) FROM facts WHERE {w} AND valid_from > ?))", (*args, at, *args, at, *args, at))
        return [(_fact(r), r["seq"]) for r in rows]

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT,
                   recorded_at: datetime | None = None, value: Any = ANY) -> None:
        valid_to, recorded_at = aware(valid_to), aware(recorded_at)
        if self._bulk is not None:
            self._bulk.close_fact(tenant, subject, predicate, valid_to, status, scope, recorded_at, value)
            return
        # a close that predates a fact's start is about an earlier period and leaves this fact alone;
        # `status` picks accepted facts (None) or proposals
        now = utcnow()
        if scope is not None and status is None:  # an accepted fact's close is a mark in its history, so later arrivals meet it (place_close)
            key = ("f", tenant, subject, predicate)

            def load() -> list[tuple[Fact, int]]:
                rows = self._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? AND predicate = ?", (tenant, subject, predicate))
                return [(_fact(r), r["seq"]) for r in rows]

            same = self._cached(key, load)
            c = close_mark(tenant, subject, predicate, valid_to, status, scope, recorded_at, value)
            closed = place_close(c, (fact for fact, _ in same))
            self._insert(INSERT_FACT, _fact_row(c))
            seqs = {id(fact): seq for fact, seq in same}
            for f in closed:
                self._exec("UPDATE facts SET valid_to = ?, closed_at = ?, changed_at = ? WHERE seq = ?", (_ts(f.valid_to), _ts(f.closed_at), _ts(f.changed_at), seqs[id(f)]))
            same.append((c, self._last_id))
            return
        # a decision (proposals, rejections) or every scope: what is open now (MemoryStore.close_fact)
        if value is not ANY:
            raise ValueError("close_fact takes a value only to close an accepted fact of one scope")
        which = "status IS NULL" if status is None else "status = ?"
        if scope is not None:
            which += " AND scope = ?"
        self._exec(
            f"UPDATE facts SET valid_to = ?, closed_at = ?, changed_at = ? WHERE tenant = ? AND subject = ? AND predicate = ? AND {which} AND valid_to IS NULL AND valid_from <= ?",
            (_ts(valid_to), _ts(recorded_at or now), _ts(now), tenant, subject, predicate, *(() if status is None else (status,)),
             *(() if scope is None else (Scope(scope).value,)), _ts(valid_to)),
        )
        if self._cache is not None:
            for fact, _ in self._cache.get(("f", tenant, subject, predicate), []):
                if fact.status == status and fact.valid_to is None and fact.valid_from <= valid_to and in_scope(fact, scope):
                    fact.valid_to, fact.closed_at, fact.changed_at = valid_to, recorded_at or now, now

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
        multi: Iterable[str] = (),
    ) -> list[Fact]:
        if self._bulk is not None:
            return filter_facts(self._bulk.facts(tenant, subject), at, since, until, known_at, multi)
        rows = self._fact_rows(tenant, [subject], at if known_at is None else None, marks=known_at is not None)
        return filter_facts((_fact(r) for r in rows), at, since, until, known_at, multi)

    def _fact_rows(self, tenant: str, subjects: list[str], open_at: datetime | None, marks: bool = True) -> list[Any]:
        """Facts of some subjects; with `open_at`, only those still open then (valid_to after it, or none):
        the state now reads the few current facts through facts_open, not a history that keeps growing.
        Not with `known_at`: a close learned later than it still counts as open there. Close marks only
        with `marks` (as_known_at rebuilds closes from them; stored facts already carry them)."""
        ins = _in(len(subjects))
        skip, args = ("", ()) if marks else (" AND (source IS NULL OR source <> ?)", (CLOSE,))
        if open_at is None:
            return self._rows(f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({ins}){skip} ORDER BY seq", (tenant, *subjects, *args))
        return self._rows(  # a mark is closed at birth: never in the first branch
            f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({ins}) AND valid_to IS NULL UNION ALL "
            f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({ins}) AND valid_to > ?{skip} ORDER BY seq",
            (tenant, *subjects, tenant, *subjects, _ts(open_at), *args),
        )

    def facts_of(self, tenant: str, subjects: list[str], **kw: Any) -> dict[str, list[Fact]]:
        """`facts` for many subjects in a few statements (optional on a store: Tools falls back to one
        `facts` call per subject). A neighbourhood or a rule sweep is one round trip per chunk."""
        out: dict[str, list[Fact]] = {s: [] for s in subjects}
        if self._bulk is not None:
            return {s: self.facts(tenant, s, **kw) for s in subjects}
        subs = sorted(out)
        open_at = kw.get("at") if kw.get("known_at") is None else None
        for i in range(0, len(subs), _Bulk.CHUNK):
            for r in self._fact_rows(tenant, subs[i : i + _Bulk.CHUNK], open_at, marks=kw.get("known_at") is not None):
                if r["subject"] in out:  # exactly: a case-insensitive collation also matches its look-alikes
                    out[r["subject"]].append(_fact(r))
        return {s: filter_facts(fs, kw.get("at"), kw.get("since"), kw.get("until"), kw.get("known_at"), kw.get("multi", ())) for s, fs in out.items()}

    def relations_of(self, tenant: str, keys: list[str], at: datetime | None = None) -> dict[str, list[Relation]]:
        """`relations` for many keys in a few statements (optional, like `facts_of`)."""
        if self._bulk is not None:
            return {k: self.relations(tenant, k, at=at) for k in keys}
        out: dict[str, list[Relation]] = {k: [] for k in keys}
        ks = sorted(out)
        for i in range(0, len(ks), _Bulk.CHUNK):
            part = ks[i : i + _Bulk.CHUNK]
            marks = _in(len(part))
            rows = self._rows(
                f"SELECT * FROM relations WHERE tenant = ? AND src IN ({marks}) UNION ALL "
                f"SELECT * FROM relations WHERE tenant = ? AND dst IN ({marks}) ORDER BY seq",
                (tenant, *part, tenant, *part),
            )
            for r in _once(rows):
                rel = _relation(r)
                for k in {rel.src, rel.dst}:
                    if k in out:
                        out[k].append(rel)
        return {k: filter_relations(rs, None, at) for k, rs in out.items()}

    def entities_of(self, tenant: str, keys: list[str]) -> dict[str, Entity | None]:
        """`entity` for many keys in a few statements (optional, like `facts_of`)."""
        out: dict[str, Entity | None] = {k: None for k in keys}
        if self._bulk is not None:
            return {k: self.entity(tenant, k) for k in keys}
        ks = sorted(out)
        for i in range(0, len(ks), _Bulk.CHUNK):
            pairs = [tuple(k.partition(":")[::2]) for k in ks[i : i + _Bulk.CHUNK]]
            flat = [x for kind, id in pairs for x in (kind, id)]
            for r in self._rows(f"SELECT * FROM entities WHERE tenant = ? AND (kind, id) IN ({', '.join('(?, ?)' for _ in pairs)})", (tenant, *flat)):
                out[f"{r['kind']}:{r['id']}"] = _entity(r)
        return out

    def entities(self, tenant: str, kind: str | None = None) -> list[Entity]:
        """Every entity of a kind (or all), ordered by key: the work list of a sweep."""
        if kind is None:
            rows = self._rows("SELECT * FROM entities WHERE tenant = ? ORDER BY kind, id", (tenant,))
        else:
            rows = self._rows("SELECT * FROM entities WHERE tenant = ? AND kind = ? ORDER BY id", (tenant, kind))
        return [_entity(r) for r in rows]

    def changes(self, tenant: str, after: datetime, alive_at: datetime | None = None) -> list[Fact]:
        """Facts written (opened or closed) after a watermark, across subjects; memory expired by `alive_at` (now by default) is left out."""
        rows = self._rows(CHANGES, (tenant, _ts(after)))
        now = alive_at or utcnow()
        return [f for f in changed_after((_fact(r) for r in rows), after) if f.expires_at is None or f.expires_at > now]

    def facts_between(self, tenant: str, since: datetime, until: datetime, predicate: str | None = None, recorded_by: datetime | None = None,
                      alive_at: datetime | None = None) -> list[Fact]:
        """Optional capability (Tools feature-detects it): the tenant's facts whose valid_from falls in [since, until],
        as stored, in write order (seq), close marks left out: one predicate when given, only those recorded by
        `recorded_by` when given, memory expired by `alive_at` (now by default) left out. `history`'s tenant window
        reads this instead of every fact of the tenant."""
        sql = f"SELECT * FROM facts WHERE tenant = ? AND valid_from >= ? AND valid_from <= ? AND {NOT_CLOSE}"
        params: list[Any] = [tenant, _ts(since), _ts(until), CLOSE]
        if predicate is not None:
            sql += " AND predicate = ?"
            params.append(predicate)
        if recorded_by is not None:
            sql += " AND (recorded_at IS NULL OR recorded_at <= ?)"
            params.append(_ts(recorded_by))
        sql += " AND (expires_at IS NULL OR expires_at > ?) ORDER BY seq"
        params.append(_ts(alive_at or utcnow()))
        return [_fact(r) for r in self._rows(sql, tuple(params))]

    def subjects_where(self, tenant: str, cartridge: Any, kinds: list[str], where: dict[str, Any], as_of: datetime, known_at: datetime | None = None,
                       author: str | None = None, task: str | None = None) -> dict[str, bool] | None:
        """Optional capability (Tools feature-detects it): the entities of `kinds` that `where` may hold for at `as_of`
        (as known at `known_at`), each entry an attribute or a current fact (accepted, visible to `author` and `task`
        as Tools reads them, the global partition's global facts included) that is the wanted value, or for a list
        of wants any of them, as Tools' `where` compares (text without case, 1 == 1.0 == True). Key -> True when
        certain, False when the store cannot compare exactly (a list value, non-ASCII text, a numeric attribute) and
        Tools must check it. None when the filter is not one the store compares (see `where_plan`): Tools sweeps."""
        plan = where_plan(where)
        if plan is None or not self.JSON_TEXT:
            return None
        cur, params = self._where_state(tenant, cartridge, [n for n, _ in plan], as_of, known_at, author, task)
        cols, fparams = [], []
        for i, (name, want) in enumerate(plan):
            sure, maybe, p = _value_match(want, self.INSTR)
            cols.append(f"max(CASE WHEN predicate = ? THEN CASE WHEN {sure} THEN 2 WHEN {maybe} THEN 1 ELSE 0 END ELSE 0 END) AS f{i}")
            fparams += [name, *p]
        attrs, aparams, sel = [], [], []
        for i, (name, want) in enumerate(plan):
            sure, maybe, p = self._attr_match(want, self.JSON_PATH.format(name))
            attrs.append(f"CASE WHEN {sure} THEN 2 WHEN {maybe} THEN 1 ELSE 0 END AS a{i}")
            aparams += p
            sel.append(f"(a{i} > 0 OR f{i} > 0)")
        mention = " OR ".join(f"{self.INSTR.format('e.attrs', '?')} > 0" for _ in plan)
        sql = (f"WITH cur AS ({cur}), g AS (SELECT subject, {', '.join(cols)} FROM cur GROUP BY subject) "
               f"SELECT * FROM (SELECT e.kind AS kind, e.id AS id, {', '.join(attrs)}, "
               f"{', '.join(f'coalesce(g.f{i}, 0) AS f{i}' for i in range(len(plan)))} "
               f"FROM entities e LEFT JOIN g ON g.subject = e.kind || ':' || e.id "
               f"WHERE e.tenant = ? AND e.kind IN ({_in(len(kinds))}) AND (g.subject IS NOT NULL OR {mention})) x WHERE {' AND '.join(sel)}")
        rows = self._rows(sql, (*params, *fparams, *aparams, tenant, *kinds, *(json.dumps(n) for n, _ in plan)))
        return {f"{r['kind']}:{r['id']}": all(max(r[f"a{i}"], r[f"f{i}"]) == 2 for i in range(len(plan))) for r in rows}

    def _attr_match(self, want: Any, path: str) -> tuple[str, str, list[Any]]:
        """SQL saying an entity's attribute at `path` is certainly `want` (or one of a list of wants), and SQL saying
        it may be (Tools checks): a text compared like Tools (an ASCII one by its lower case, any other exactly; other
        non-ASCII text may be equal without case), null by its type; a number, boolean or list attribute may be."""
        jt, js = self.JSON_TYPE.format("e.attrs", "?"), self.JSON_STR.format("e.attrs", "?")
        text = f"{jt} = {self.JT_TEXT}"
        sure, maybe, ps, pm = [], [f"{jt} = {self.JT_ARRAY}"], [], [path]
        for w in want if isinstance(want, list) else [want]:
            if w is None:
                sure.append(f"{jt} = {self.JT_NULL}")
                ps.append(path)
            elif isinstance(w, str):
                sure.append(f"({text} AND {js} = ?)")
                ps += [path, path, w]
                if (c := w.casefold()).isascii():
                    sure.append(f"({text} AND {self.ASCII.format(js)} AND lower({js}) = ?)")
                    ps += [path, path, path, path, c]
                maybe.append(f"({text} AND NOT {self.ASCII.format(js)})")
                pm += [path, path, path]
            else:  # a number or a boolean: an attribute of either type may equal it (1 == 1.0 == True), Tools says
                maybe.append(f"{jt} IN ({self.JT_NUMBER}, {self.JT_BOOL})")
                pm.append(path)
        return " OR ".join(sure) or "1 = 0", " OR ".join(maybe), ps + pm

    def _where_state(self, tenant: str, cartridge: Any, predicates: list[str], as_of: datetime, known_at: datetime | None,
                     author: str | None, task: str | None) -> tuple[str, tuple[Any, ...]]:
        """SQL for the current facts of some predicates (subject, predicate, value) as Tools reads them for one
        caller (keel/tools.py _fetch): accepted, the tenant's and the global partition's global facts, agent memory
        only its author's, task memory only untasked or `task`'s, alive at known_at (else now). As stored; with
        `known_at`, closes rebuilt as Keel knew them (keel/store/memory.py as_known_at): per slot, the last fact
        recorded by then starting by `as_of`, unless a close it knew by then (its own, or a close mark) ended it."""
        t, alive = _ts(as_of), _ts(known_at or utcnow())
        seen = (f"tenant IN (?, ?) AND predicate IN ({_in(len(predicates))}) AND valid_from <= ? AND status IS NULL AND {NOT_CLOSE} "
                f"AND (tenant = ? OR scope = '{Scope.GLOBAL.value}') AND (scope <> '{Scope.AGENT.value}' OR source = ?) "
                f"AND (scope <> '{Scope.TASK.value}' OR task IS NULL OR task = ?)")
        params: tuple[Any, ...] = (tenant, global_of(tenant), *predicates, t, CLOSE, tenant, f"annotate:{author}" if author else None, task)
        if known_at is None:
            return (f"SELECT subject, predicate, value FROM facts WHERE {seen} AND (valid_to IS NULL OR valid_to > ?) "
                    f"AND (expires_at IS NULL OR expires_at > ?)"), (*params, t, alive)
        k = _ts(known_at)
        multi = sorted(p for p, spec in (getattr(cartridge, "predicates", None) or {}).items() if isinstance(spec, dict) and spec.get("multi"))
        per = f"predicate IN ({_in(len(multi))})" if multi else "1 = 0"
        slot = (f"tenant, subject, predicate, scope, CASE WHEN scope = '{Scope.TASK.value}' THEN task END, "
                f"CASE WHEN scope = '{Scope.AGENT.value}' THEN source END, CASE WHEN {per} THEN source END, CASE WHEN {per} THEN corrected_by END")
        last = (f"SELECT *, row_number() OVER (PARTITION BY {slot} ORDER BY valid_from DESC, coalesce(recorded_at, '') DESC, "
                f"coalesce(evidence, '') DESC, value DESC, seq DESC) AS rn FROM facts WHERE {seen} AND (recorded_at IS NULL OR recorded_at <= ?)")
        mark = ("SELECT 1 FROM facts c WHERE c.tenant = x.tenant AND c.subject = x.subject AND c.predicate = x.predicate AND c.source = ? "
                "AND c.status IS NULL AND c.scope = x.scope AND (c.recorded_at IS NULL OR c.recorded_at <= ?) AND c.valid_from > x.valid_from "
                "AND c.valid_from <= ? AND (c.value = 'null' OR c.value = '[' || x.value || ']')")
        sql = (f"SELECT subject, predicate, value FROM ({last}) x WHERE rn = 1 "
               f"AND NOT (valid_to IS NOT NULL AND valid_to <= ? AND (closed_at IS NULL OR closed_at <= ?)) "
               f"AND (expires_at IS NULL OR expires_at > ?) AND NOT EXISTS ({mark})")
        return sql, (*multi, *multi, *params, k, t, k, alive, CLOSE, k, t)

    def relation_changes(self, tenant: str, after: datetime) -> list[Relation]:
        """Relations linked or unlinked after a watermark (by the ingest clock, like changes)."""
        return [_relation(r) for r in self._rows(f"SELECT * FROM relations WHERE tenant = ? AND changed_at > ? AND NOT {REL_MARK} ORDER BY seq", (tenant, _ts(after)))]

    def watermark(self, tenant: str) -> datetime | None:
        """The latest write in the tenant (facts and links, ingest clock): a cursor for `changes`."""
        # one MAX per statement: each is then a seek on its index
        times = [self._one(f"SELECT MAX(changed_at) AS m FROM {table} WHERE tenant = ?", (tenant,)) for table in ("facts", "relations")]
        times = [_dt(row["m"]) for row in times if row and row["m"]]
        return max(times) if times else None

    def close_task(self, tenant: str, task: str, now: datetime | None = None) -> int:
        """End a task: its memory expires now (reads stop seeing it; `expire` removes it later)."""
        n = _ts(now or utcnow())
        return self._exec("UPDATE facts SET expires_at = ? WHERE tenant = ? AND task = ? AND scope = ? AND (expires_at IS NULL OR expires_at > ?)", (n, tenant, task, Scope.TASK.value, n))

    def relations(self, tenant: str, key: str, rel: str | None = None, at: datetime | None = None) -> list[Relation]:
        if self._bulk is not None:
            return filter_relations(self._bulk.relations(tenant, key), rel, at)
        rows = _once(self._rows(RELATIONS_OF, (tenant, key, tenant, key)))
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
            if not is_close(f := _fact(row)):
                yield f

    def forget(self, tenant: str) -> dict[str, int]:
        """Erase the tenant and leave a tombstone, in one transaction (Store protocol). Idempotent."""
        if is_global(tenant):
            raise ValueError("the global partition is every tenant's; it cannot be forgotten")
        if self._bulk is not None:
            raise RuntimeError("forget inside a bulk batch would be undone by its flush")
        with self.transaction():
            counts = {
                "entities": self._exec("DELETE FROM entities WHERE tenant = ?", (tenant,)),
                "relations": self._exec(f"DELETE FROM relations WHERE tenant = ? AND NOT {REL_MARK}", (tenant,)),
                "relation marks": self._exec("DELETE FROM relations WHERE tenant = ?", (tenant,)),  # what is left: marks, uncounted
                "annotations": self._exec("DELETE FROM facts WHERE tenant = ? AND (source LIKE ? OR source LIKE ?)", (tenant, "annotate:%", "consolidate:%")),
                "facts": self._exec("DELETE FROM facts WHERE tenant = ?", (tenant,)),
                "ingested": self._exec("DELETE FROM ingested WHERE tenant = ?", (tenant,)),
            }
            self._exec(self.TOMBSTONE, (tenant, _ts(utcnow())))
            if self._cache is not None:
                self._cache.clear()
        return {k: counts[k] for k in ("entities", "relations", "facts", "annotations", "ingested")}

    def tenants(self) -> list[str]:
        """Every tenant with something stored (not the global partition), for jobs that sweep them all."""
        # ponytail: DISTINCT over the (tenant, ...) indexes; a tenants table if it ever shows in a profile
        rows = self._rows("SELECT DISTINCT tenant FROM entities UNION SELECT DISTINCT tenant FROM facts")
        return sorted(t for t in {r["tenant"] for r in rows} if not is_global(t))

    def forgotten(self, tenant: str) -> datetime | None:
        row = self._one("SELECT at FROM forgotten WHERE tenant = ?", (tenant,))
        return _dt(row["at"]) if row else None

    def forget_before(self, tenant: str, before: datetime) -> dict[str, int]:
        """Delete what closed at or before `before` and keep the cutoff, in one transaction (Store protocol). Idempotent."""
        if is_global(tenant):
            raise ValueError("the global partition is every tenant's; retention runs per tenant")
        if self._bulk is not None:
            raise RuntimeError("forget_before inside a bulk batch would be undone by its flush")
        cut = _ts(before)
        closed = "tenant = ? AND valid_to IS NOT NULL AND valid_to <= ?"
        with self.transaction():
            counts = {
                "annotations": self._exec(f"DELETE FROM facts WHERE {closed} AND (source LIKE ? OR source LIKE ?)", (tenant, cut, "annotate:%", "consolidate:%")),
                "facts": self._exec(f"DELETE FROM facts WHERE {closed}", (tenant, cut)),
                "relations": self._exec(f"DELETE FROM relations WHERE {closed} AND NOT {REL_MARK}", (tenant, cut)),
                "relation marks": self._exec(f"DELETE FROM relations WHERE {closed}", (tenant, cut)),  # what is left: marks, uncounted
            }
            row = self._one("SELECT cut FROM cutoffs WHERE tenant = ?", (tenant,))
            if row is None:
                self._exec("INSERT INTO cutoffs (tenant, cut, at) VALUES (?, ?, ?)", (tenant, cut, _ts(utcnow())))
            elif _dt(row["cut"]) < before:
                self._exec("UPDATE cutoffs SET cut = ?, at = ? WHERE tenant = ?", (cut, _ts(utcnow()), tenant))
            if self._cache is not None:
                self._cache.clear()
        return {k: counts[k] for k in ("facts", "annotations", "relations")}

    def cutoff(self, tenant: str) -> datetime | None:
        row = self._one("SELECT cut FROM cutoffs WHERE tenant = ?", (tenant,))
        return _dt(row["cut"]) if row else None

    def backup(self, tenant: str) -> dict[str, Any]:
        """Everything a restore needs (Store protocol, optional), as MemoryStore.backup."""
        # ponytail: the whole tenant in memory; stream it if a tenant outgrows a process
        return {
            "entities": [_entity(r) for r in self._rows(f"SELECT * FROM entities WHERE tenant = ? ORDER BY {self.ENTITY_ORDER}", (tenant,))],
            "relations": [_relation(r) for r in self._rows("SELECT * FROM relations WHERE tenant = ? ORDER BY seq", (tenant,))],
            "facts": [_fact(r) for r in self._rows("SELECT * FROM facts WHERE tenant = ? ORDER BY seq", (tenant,))],
            "ingested": sorted(r["id"] for r in self._rows("SELECT id FROM ingested WHERE tenant = ?", (tenant,))),
            "cutoff": self.cutoff(tenant),
        }

    def restore(self, tenant: str, backup: dict[str, Any]) -> None:
        """Write a backup as it was into an empty tenant, in one transaction (Store protocol, optional)."""
        with self.transaction():
            if any(self._one(f"SELECT 1 AS one FROM {t} WHERE tenant = ? LIMIT 1", (tenant,)) for t in ("entities", "relations", "facts", "ingested")):
                raise ValueError(f"tenant '{tenant}' is not empty: restore writes into an empty tenant only")
            for sql, rows in ((self.UPSERT_ENTITY, [(tenant, e.kind, e.id, e.ref, json.dumps(e.attrs)) for e in backup["entities"]]),
                              (INSERT_RELATION, [_relation_row(replace(r, tenant=tenant)) for r in backup["relations"]]),
                              (INSERT_FACT, [_fact_row(replace(f, tenant=tenant)) for f in backup["facts"]]),
                              (self.REMEMBER, [(tenant, i) for i in backup["ingested"]])):
                if rows:
                    self._execmany(sql, rows)
            if backup.get("cutoff") is not None:
                self._exec("INSERT INTO cutoffs (tenant, cut, at) VALUES (?, ?, ?)", (tenant, _ts(backup["cutoff"]), _ts(utcnow())))
            if self._cache is not None:
                self._cache.clear()

    def forgotten_tenants(self) -> dict[str, datetime]:
        return {r["tenant"]: _dt(r["at"]) for r in self._rows("SELECT tenant, at FROM forgotten")}

    # ---- metering (keel/usage.py): counters, not tenant data, so forget and reset leave them ----

    def meter(self, rows: list[tuple[str, str, str, int]]) -> None:
        """Add (tenant, day, item, n) to the counters: item is `events` or a tool's name, day `YYYY-MM-DD`."""
        if rows:
            with self.transaction():
                for row in rows:
                    self._exec(self.METER, row)

    def usage(self, since: str | None = None, until: str | None = None) -> list[tuple[str, str, str, int]]:
        """(tenant, day, item, n) with `since <= day < until` (days or months as ISO prefixes)."""
        rows = self._rows("SELECT tenant, day, item, n FROM metering WHERE day >= ? AND day < ? ORDER BY tenant, day, item", (since or "", until or "9999"))
        return [(r["tenant"], r["day"], r["item"], int(r["n"])) for r in rows]

    # ---- API keys (keel/keys.py): not tenant data, so forget and reset leave them ----

    def put_account(self, account: Account) -> None:
        """Insert or replace an account's limits (keel/accounts.py)."""
        with self.transaction():
            self._exec("DELETE FROM accounts WHERE id = ?", (account.id,))
            self._exec("INSERT INTO accounts (id, rps, events_month, store, retention) VALUES (?, ?, ?, ?, ?)",
                       (account.id, account.rps, account.events_month, account.store, account.retention))

    def account(self, account_id: str) -> Account | None:
        row = self._one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return _account(row) if row else None

    def accounts(self) -> list[Account]:
        return [_account(r) for r in self._rows("SELECT * FROM accounts ORDER BY id")]

    def put_key(self, key: ApiKey) -> None:
        self._exec(
            "INSERT INTO api_keys (id, hash, account, tenant, agent, glob, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key.id, key.hash, key.account, key.tenant, key.agent, int(key.glob), _ts(key.created_at), _ts(key.revoked_at)),
        )

    def api_key(self, digest: str) -> ApiKey | None:
        row = self._one("SELECT * FROM api_keys WHERE hash = ?", (digest,))
        return _key(row) if row else None

    def api_keys(self, account: str | None = None) -> list[ApiKey]:
        where, params = (" WHERE account = ?", (account,)) if account is not None else ("", ())
        return [_key(r) for r in self._rows(f"SELECT * FROM api_keys{where} ORDER BY account, created_at", params)]

    def revoke_key(self, key_id: str, at: datetime) -> bool:
        return self._exec("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_ts(at), key_id)) > 0

    def reset(self, tenant: str, keep_annotations: bool = False) -> None:
        """Wipe a tenant's subgraph. Used by replay: the event log rebuilds it. With
        `keep_annotations`, what agents and people wrote (and consolidated) stays, closed history included."""
        for table in ("entities", "relations", "ingested"):
            self._exec(f"DELETE FROM {table} WHERE tenant = ?", (tenant,))
        keep = " AND (source IS NULL OR (source NOT LIKE ? AND source NOT LIKE ?))" if keep_annotations else ""
        self._exec(f"DELETE FROM facts WHERE tenant = ?{keep}", (tenant, "annotate:%", "consolidate:%") if keep_annotations else (tenant,))
        if self._cache is not None:
            self._cache.clear()


def _account(row: Any) -> Account:
    num = lambda v: None if v is None else int(v)
    return Account(id=row["id"], rps=num(row["rps"]), events_month=num(row["events_month"]), store=row["store"], retention=row["retention"])


def _key(row: Any) -> ApiKey:
    return ApiKey(id=row["id"], hash=row["hash"], account=row["account"], created_at=_dt(row["created_at"]), tenant=row["tenant"],
                  agent=row["agent"], glob=bool(row["glob"]), revoked_at=_dt(row["revoked_at"]))


def where_plan(where: dict[str, Any]) -> list[tuple[str, Any]] | None:
    """`search(where=)` entries a store compares in SQL: under a plain ASCII name (a JSON path needs no quoting), a
    text, a finite number, a boolean, null, or a list of those (any of them). Anything else (NaN or infinity, an
    object, a list in a list) is compared in Python."""
    plan = []
    for name, want in where.items():
        if not (isinstance(name, str) and name.isascii() and name.isprintable() and '"' not in name and "\\" not in name):
            return None
        if not all(_comparable(w) for w in (want if isinstance(want, list) else [want])):
            return None
        plan.append((name, want))
    return plan or None


def _comparable(w: Any) -> bool:
    return w is None or isinstance(w, (str, bool)) or (isinstance(w, int) or (isinstance(w, float) and math.isfinite(w)))


def _numbers(w: float) -> set[str]:
    """Every JSON text (as json.dumps writes it) whose value Python finds equal to the number `w`: the int, the float
    (and -0.0 for zero), and true or false for 1 and 0. A value with another text is not equal."""
    out = {json.dumps(w)}
    try:
        f = float(w)
    except OverflowError:  # an int no float reaches: only ints equal it
        f = None
    if f is not None and f == w:
        out.add(json.dumps(f))
        if f.is_integer():
            out.add(json.dumps(int(f)))
        if f == 0:
            out |= {"0.0", "-0.0", "false"}
        if f == 1:
            out.add("true")
    return out


def _value_match(want: Any, instr: str) -> tuple[str, str, list[Any]]:
    """SQL over a fact's `value` (JSON text as json.dumps writes it: ASCII, \\uXXXX escapes) saying it certainly is
    `want` (or one of a list of wants) as Tools' `where` compares, and SQL saying it may be (Tools checks): a list
    value, or for a wanted text a value with escapes (non-ASCII text equal without case: ß and SS)."""
    texts: set[str] = set()
    lowered: set[str] = set()
    escaped = False
    for w in want if isinstance(want, list) else [want]:
        if w is None:
            texts.add("null")
        elif isinstance(w, str):
            texts.add(json.dumps(w))
            if (c := w.casefold()).isascii():  # an ASCII value is equal without case when its lower case is `c`
                lowered.add(json.dumps(c))
            escaped = True
        else:
            texts |= _numbers(w)
    sure = [f"value IN ({_in(len(texts))})"] if texts else []
    sure += [f"lower(value) IN ({_in(len(lowered))})"] if lowered else []
    maybe = "value LIKE ?" + (f" OR {instr.format('value', '?')} > 0" if escaped else "")
    return " OR ".join(sure) or "1 = 0", maybe, [*sorted(texts), *sorted(lowered), "[%", *(["\\u"] if escaped else [])]


def _in(n: int) -> str:
    return ", ".join("?" for _ in range(n))


class _Bulk:
    """The overlay behind `SqlStore.bulk`: everything a batch touches, in memory, with what changed.

    Keys are prefetched from the batch's envelope subjects; anything a handler reaches beyond them
    (a payload id, a neighbour) is loaded lazily, one key at a time, merged with the batch's own
    unsaved writes so reads never miss them. A subject's facts are prefetched only where the batch
    can still change them: open, or closing after the batch's earliest event. A history that keeps
    growing is never read whole per batch; a handler that writes before that instant, or reads the
    facts, loads the rest of that one subject first. `flush` writes entities, relations, facts, closes and
    the dedupe marks in a handful of multi-row statements.
    """

    CHUNK = 1000

    def __init__(self, store: SqlStore) -> None:
        self.s = store
        self.entities: dict[tuple[str, str], Entity | None] = {}
        self.dirty_entities: set[tuple[str, str]] = set()
        self.facts_by: dict[tuple[str, str], list[list[Any]]] = {}  # (tenant, subject) -> [[Fact, seq|None], ...]
        self.slots: dict[tuple[str, str], tuple[list[list[Any]], int, SlotIndex]] = {}  # facts_by's list, its length, its index
        self.new_facts: list[Fact] = []
        self.closed_facts: dict[int, Fact] = {}
        self.rels_by: dict[tuple[str, str], list[list[Any]]] = {}  # (tenant, key) -> [[Relation, seq|None], ...]
        self.new_rels: list[Relation] = []
        self.closed_rels: dict[int, Relation] = {}
        self.gone_rels: set[int] = set()  # saved relation marks whose event now opens or ends a period
        self.seen: dict[str, set[str]] = {}
        self.known_ids: dict[str, set[str]] = {}  # ids whose presence we checked
        self.new_ids: list[tuple[str, str]] = []
        self.partial: dict[tuple[str, str], datetime] = {}  # subjects whose facts closed before this instant are not loaded

    # ---- prefetch: the batch's subjects and ids in a few statements ----------------------------

    def prefetch(self, events: list[Event]) -> None:
        by_tenant: dict[str, tuple[set[str], set[str]]] = {}
        since: dict[str, datetime] = {}  # per tenant, the batch's earliest event
        for ev in events:
            subjects, ids = by_tenant.setdefault(ev.tenant, (set(), set()))
            ids.add(ev.id)
            if ev.tenant not in since or ev.occurred_at < since[ev.tenant]:
                since[ev.tenant] = ev.occurred_at
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
                    self.partial[(tenant, sub)] = since[tenant]
                marks = _in(len(part))
                for row in self.s._rows(
                    f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({marks}) AND valid_to IS NULL UNION ALL "
                    f"SELECT * FROM facts WHERE tenant = ? AND subject IN ({marks}) AND valid_to > ? ORDER BY seq",
                    (tenant, *part, tenant, *part, _ts(since[tenant])),
                ):
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

    def _whole(self, tenant: str, subject: str) -> list[list[Any]]:
        """The subject's full history: what prefetch skipped, around what the batch already holds
        (loaded facts keep their identity, since a close may already point at them)."""
        k = (tenant, subject)
        if self.partial.pop(k, None) is not None:
            held = {seq: e for e in self.facts_by[k] if (seq := e[1]) is not None}
            rows = self.s._rows("SELECT * FROM facts WHERE tenant = ? AND subject = ? ORDER BY seq", (tenant, subject))
            self.facts_by[k] = [held.get(r["seq"]) or [_fact(r), r["seq"]] for r in rows] + [e for e in self.facts_by[k] if e[1] is None]
        return self._facts(tenant, subject)

    def facts(self, tenant: str, subject: str) -> list[Fact]:
        return [f for f, _ in self._whole(tenant, subject)]

    def _slots(self, tenant: str, subject: str, same: list[list[Any]]) -> SlotIndex:
        """`same`'s SlotIndex, rebuilt when the list was replaced (_whole) or grew behind it."""
        held = self.slots.get((tenant, subject))
        if held is None or held[0] is not same or held[1] != len(same):
            held = self.slots[(tenant, subject)] = (same, len(same), SlotIndex(same, fact=lambda e: e[0]))
        return held[2]

    def _append(self, tenant: str, subject: str, same: list[list[Any]], entry: list[Any]) -> None:
        idx = self._slots(tenant, subject, same)
        same.append(entry)
        idx.add(entry)
        self.slots[(tenant, subject)] = (same, len(same), idx)

    def _rels(self, tenant: str, key: str) -> list[list[Any]]:
        k = (tenant, key)
        if k not in self.rels_by:
            rows = _once(self.s._rows(RELATIONS_OF, (tenant, key, tenant, key)))
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

    def link(self, tenant: str, src: str, rel: str, dst: str, valid_from: datetime, source: str | None, recorded_at: datetime | None = None) -> Relation:
        new = Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_from, recorded_at=recorded_at or utcnow(), source=source)
        return self._relate(new, unlink=False)

    def unlink(self, tenant: str, src: str, rel: str, dst: str, valid_to: datetime, recorded_at: datetime | None = None) -> None:
        self._relate(Relation(tenant=tenant, src=src, rel=rel, dst=dst, valid_from=valid_to, recorded_at=recorded_at or utcnow()), unlink=True)

    def _relate(self, new: Relation, unlink: bool) -> Relation:
        """SqlStore._relate in the batch overlay: new rows join new_rels, moved ends of saved rows go to
        closed_rels, saved marks that now open or end a period to gone_rels."""
        tenant, src, dst = new.tenant, new.src, new.dst
        entries = [e for e in self._rels(tenant, src) if e[0].src == src and e[0].rel == new.rel and e[0].dst == dst]
        tail = relation_tail([e[0] for e in entries], new, unlink)  # in order: only the open period or a new row changes
        specs = tail if tail is not None else relation_history([e[0] for e in entries], new, unlink)
        wanted = {rel_start(spec) for spec in specs}
        held: dict[tuple[Any, ...], list[list[Any]]] = {}
        for e in entries:
            if tail is None or rel_start(e[0]) in wanted:
                held.setdefault(rel_start(e[0]), []).append(e)
        now, final = utcnow(), [e[0] for e in entries] if tail is not None else []
        for spec in specs:
            bucket = held.get(rel_start(spec))
            if not bucket:
                spec.changed_at = now
                self.new_rels.append(spec)
                self._rels(tenant, src).append([spec, None])
                if dst != src and (tenant, dst) in self.rels_by:
                    self.rels_by[(tenant, dst)].append([spec, None])
                final.append(spec)
                continue
            r, seq = bucket.pop()
            if (r.valid_to, r.closed_at) != (spec.valid_to, spec.closed_at):
                r.valid_to, r.closed_at, r.changed_at = spec.valid_to, spec.closed_at, now
                if seq is not None:
                    self.closed_rels[seq] = r  # an unsaved one carries its new end into its own INSERT
            if not any(x is r for x in final):
                final.append(r)
        for r, seq in (x for bucket in held.values() for x in bucket) if tail is None else ():
            for k in ((tenant, src), (tenant, dst)):
                if k in self.rels_by:
                    self.rels_by[k] = [x for x in self.rels_by[k] if x[0] is not r]
            if seq is None:
                self.new_rels = [x for x in self.new_rels if x is not r]
            else:
                self.closed_rels.pop(seq, None)
                self.gone_rels.add(seq)
        return _covering(final, new)

    def open_fact(self, tenant: str, subject: str, predicate: str, value: Any, valid_from: datetime, kw: dict[str, Any]) -> Fact:
        since = self.partial.get((tenant, subject))
        # a fact that closed before `since` can neither hold nor follow a later one: only an earlier write needs it
        same = self._whole(tenant, subject) if since is not None and valid_from < since else self._facts(tenant, subject)
        f = new_fact(tenant, subject, predicate, value, valid_from, **kw)
        idx = self._slots(tenant, subject, same)
        near = idx.neighbours(f, kw.get("per_source", False))
        closed = place_fact(f, [x for x, _ in near], idx.marks)
        if closed is not None:
            seq = next(s for x, s in near if x is closed)
            if seq is not None:
                self.closed_facts[seq] = closed  # an unsaved one carries its valid_to into its own INSERT
        self._append(tenant, subject, same, [f, None])
        self.new_facts.append(f)
        return f

    def close_fact(self, tenant: str, subject: str, predicate: str, valid_to: datetime, status: str | None = None, scope: Scope | str | None = Scope.TENANT,
                   recorded_at: datetime | None = None, value: Any = ANY) -> None:
        now = utcnow()
        if scope is not None and status is None:  # a mark in the history, as SqlStore.close_fact
            since = self.partial.get((tenant, subject))
            same = self._whole(tenant, subject) if since is not None and valid_to < since else self._facts(tenant, subject)
            c = close_mark(tenant, subject, predicate, valid_to, status, scope, recorded_at, value)
            seqs = {id(x): seq for x, seq in same}
            for f in place_close(c, (x for x, _ in same)):
                if seqs[id(f)] is not None:
                    self.closed_facts[seqs[id(f)]] = f
            self._append(tenant, subject, same, [c, None])
            self.new_facts.append(c)
            return
        if value is not ANY:
            raise ValueError("close_fact takes a value only to close an accepted fact of one scope")
        for f, seq in self._facts(tenant, subject):
            if f.predicate == predicate and f.status == status and f.valid_to is None and f.valid_from <= valid_to and in_scope(f, scope):
                f.valid_to, f.closed_at, f.changed_at = valid_to, recorded_at or now, now
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
            s._execmany(INSERT_RELATION, [_relation_row(r) for r in self.new_rels])
        if self.closed_rels:
            s._close_rows("relations", {seq: (r.valid_to, r.closed_at, r.changed_at) for seq, r in self.closed_rels.items()})
        if self.gone_rels:
            gone = sorted(self.gone_rels)
            for i in range(0, len(gone), 500):
                part = gone[i : i + 500]
                s._exec(f"DELETE FROM relations WHERE seq IN ({_in(len(part))})", tuple(part))
        if self.new_facts:
            s._execmany(INSERT_FACT, [_fact_row(f) for f in self.new_facts])
        if self.closed_facts:
            s._close_rows("facts", {seq: (f.valid_to, f.closed_at, f.changed_at) for seq, f in self.closed_facts.items()})
        if self.new_ids:
            s._execmany(s.REMEMBER, self.new_ids)
