"""The DuckDB baseline: the same Jira log as one bitemporal table, and the reads Keel's tools answer.

`field_versions(tenant, subject, field, value, valid_from, valid_to, recorded_at, closed_at, id)` holds one row
per field value an event set (what the Jira handlers write as facts). valid_to is the next version's start in
Keel's succession order (valid_from, recorded_at, event id), closed_at the later of the two recorded_at: the
same derivation as keel.store.memory.place_fact, done once at load with a window function. A read as known
at K cannot use the stored valid_to (a successor may not be known yet), so it re-derives the successor over
the rows recorded by K, at query time. Only the facts: links and rules are not modelled here.

    uv run --with duckdb python -m evals.scale.harness duck --events log.jsonl --label syn:100000
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

FIELDS = [("issue.created", f) for f in ("status", "status_category", "assignee", "priority", "resolution", "issue_type")] + [
    ("issue.status_changed", "status"), ("issue.status_changed", "status_category"), ("issue.assigned", "assignee"),
    ("issue.priority_changed", "priority"), ("issue.resolution_changed", "resolution"), ("issue.type_changed", "issue_type")]


def load(con: Any, path: Path) -> dict[str, Any]:
    """Load a .jsonl log into field_versions. Returns rows and seconds (read, derive, index)."""
    t0 = time.perf_counter()
    con.execute("DROP TABLE IF EXISTS field_versions")
    con.execute("CREATE OR REPLACE TEMP TABLE fields (type VARCHAR, field VARCHAR)")
    con.executemany("INSERT INTO fields VALUES (?, ?)", FIELDS)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ev AS SELECT * FROM read_json('{path}', format = 'newline_delimited',
            columns = {{id: 'VARCHAR', tenant: 'VARCHAR', type: 'VARCHAR', subject: 'VARCHAR',
                       occurred_at: 'TIMESTAMPTZ', recorded_at: 'TIMESTAMPTZ', payload: 'JSON'}})""")
    t_read = time.perf_counter() - t0
    con.execute("""
        CREATE TABLE field_versions AS
        SELECT tenant, subject, field, value, valid_from, recorded_at, id,
               lead(valid_from) OVER w AS valid_to,
               CASE WHEN lead(valid_from) OVER w IS NULL THEN NULL ELSE greatest(recorded_at, lead(recorded_at) OVER w) END AS closed_at
        FROM (SELECT e.tenant, e.subject, f.field, json_extract_string(e.payload, '$.' || f.field) AS value,
                     e.occurred_at AS valid_from, e.recorded_at, e.id
              FROM ev e JOIN fields f USING (type))
        WINDOW w AS (PARTITION BY tenant, subject, field ORDER BY valid_from, recorded_at, id)
        ORDER BY tenant, subject, field, valid_from""")
    t_derive = time.perf_counter() - t0 - t_read
    con.execute("CREATE INDEX fv_subject ON field_versions (tenant, subject)")
    con.execute("DROP TABLE ev")
    con.execute("CHECKPOINT")
    rows = con.execute("SELECT count(*) FROM field_versions").fetchone()[0]
    return {"rows": rows, "load_s": time.perf_counter() - t0, "read_s": t_read, "derive_s": t_derive}


def facts(con: Any, tenant: str, subject: str, at: datetime | None = None, known_at: datetime | None = None) -> list[tuple[str, Any]]:
    """(field, value) of a subject: current, at an instant, or at an instant as known at another."""
    if known_at is None and at is None:
        sql, args = "SELECT field, value FROM field_versions WHERE tenant = ? AND subject = ? AND valid_to IS NULL", [tenant, subject]
    elif known_at is None:
        sql = ("SELECT field, value FROM field_versions WHERE tenant = ? AND subject = ? AND valid_from <= ? "
               "AND (valid_to IS NULL OR valid_to > ?)")
        args = [tenant, subject, at, at]
    else:
        at = at or known_at
        sql = ("SELECT field, value FROM (SELECT field, value, valid_from, lead(valid_from) OVER "
               "(PARTITION BY field ORDER BY valid_from, recorded_at, id) AS nxt FROM field_versions "
               "WHERE tenant = ? AND subject = ? AND recorded_at <= ?) WHERE valid_from <= ? AND (nxt IS NULL OR nxt > ?)")
        args = [tenant, subject, known_at, at, at]
    return con.execute(sql, args).fetchall()


def value(con: Any, tenant: str, subject: str, field: str, at: datetime | None = None, known_at: datetime | None = None) -> list[Any]:
    return [v for f, v in facts(con, tenant, subject, at, known_at) if f == field]


def history(con: Any, tenant: str, subject: str) -> list[tuple[Any, ...]]:
    return con.execute("SELECT field, value, valid_from, valid_to, recorded_at, closed_at FROM field_versions "
                       "WHERE tenant = ? AND subject = ? ORDER BY valid_from", [tenant, subject]).fetchall()


def status_at(con: Any, tenant: str, status: str, at: datetime, known_at: datetime | None = None) -> list[str]:
    """Subjects whose status was `status` at `at` (as known at `known_at`), sorted."""
    if known_at is None:
        rows = con.execute("SELECT subject FROM field_versions WHERE tenant = ? AND field = 'status' AND value = ? AND valid_from <= ? "
                           "AND (valid_to IS NULL OR valid_to > ?)", [tenant, status, at, at]).fetchall()
    else:
        rows = con.execute(
            "SELECT subject FROM (SELECT subject, value, valid_from, lead(valid_from) OVER (PARTITION BY subject ORDER BY valid_from, recorded_at, id) AS nxt "
            "FROM field_versions WHERE tenant = ? AND field = 'status' AND recorded_at <= ?) WHERE value = ? AND valid_from <= ? AND (nxt IS NULL OR nxt > ?)",
            [tenant, known_at, status, at, at]).fetchall()
    return sorted(r[0] for r in rows)


def changed(con: Any, tenant: str, field: str, since: datetime, until: datetime, rows: bool = False) -> Any:
    """Versions of `field` that started in [since, until]: their count, or the rows oldest first."""
    if rows:
        return con.execute("SELECT subject, value, valid_from FROM field_versions WHERE tenant = ? AND field = ? AND valid_from BETWEEN ? AND ? "
                           "ORDER BY valid_from", [tenant, field, since, until]).fetchall()
    return con.execute("SELECT count(*) FROM field_versions WHERE tenant = ? AND field = ? AND valid_from BETWEEN ? AND ?",
                       [tenant, field, since, until]).fetchone()[0]


def call(con: Any, tenant: str, op: Any) -> Any:
    """The DuckDB read equivalent to a harness op, or None when there is none (links, rules, an unknown subject)."""
    kw = op.kw
    if op.name == "context_now":
        return facts(con, tenant, kw["subject"])
    if op.name in ("context_as_of", "facts_as_of"):
        return facts(con, tenant, kw["subject"], kw["as_of"])
    if op.name in ("context_known_at", "facts_known_at"):
        return facts(con, tenant, kw["subject"], kw["as_of"], kw["as_known_at"])
    if op.name == "history_subject":
        return history(con, tenant, kw["subject"])
    if op.name == "search_status_at":
        return status_at(con, tenant, kw["where"]["status"], kw["as_of"])
    if op.name == "search_known_at_count":
        return len(status_at(con, tenant, kw["where"]["status"], kw["as_of"], kw["as_known_at"]))
    if op.name == "history_tenant_window":
        return changed(con, tenant, kw["predicate"], kw["since"], kw["until"], rows=True)
    return None


SUPPORTED = ("context_now", "context_as_of", "facts_as_of", "context_known_at", "facts_known_at", "history_subject", "search_status_at",
             "search_known_at_count", "history_tenant_window")


def bench(events: Path, out: Path, label: str, per_op: int, tenant_per_op: int, data: Path, seed: int = 1) -> None:
    import duckdb

    from evals.scale import harness

    data.mkdir(parents=True, exist_ok=True)
    db = data / f"duck-{label.replace(':', '-')}.duckdb"
    for p in (db, Path(f"{db}.wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    stats = load(con, events)
    size = db.stat().st_size
    sample = harness.Sample.of(harness.read_events(events), seed=seed)
    todo = [op for op in harness.ops(sample, random.Random(seed), per_op, tenant_per_op) if op.name in SUPPORTED]
    random.Random(seed).shuffle(todo)
    lat = harness.run_ops(None, None, todo, 60.0, call_fn=lambda op: call(con, sample.tenant, op))  # type: ignore[arg-type]
    harness.emit(out, {"kind": "duck", "label": label, "events": sample.count if sample.tenant else None, "events_file": str(events), **stats,
                       "db_bytes": size, "latency": lat, "threads": con.execute("SELECT current_setting('threads')").fetchone()[0],
                       "machine": harness.machine()})
