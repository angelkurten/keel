"""A bitemporal SQL baseline: the tenant's log in DuckDB tables, the way a database engineer would model it, and an
agent that writes SQL against them (bench system `sql`).

    pinned: duckdb==2.0.0; run the bench with `--with duckdb==2.0.0` (an eval-side dependency, never the core's).

The same log every system reads (<tenant>.bitemporal.jsonl, late arrivals and corrections included), loaded
deterministically into:

- issues:          one row per issue (its creation).
- field_versions:  each field's history as a bitemporal table (SQL:2011 style): a row is a value over valid time
                   [valid_from, valid_to) as the tracker believed it over transaction time [recorded_at,
                   superseded_at). A later record (a late change, a correction) never updates a row in place: it
                   closes the rows it changes (superseded_at) and adds the new ones, so "as recorded by K" is one
                   WHERE clause.
- links:           the issue links (and sub-task parents), bitemporal the same way.
- statuses:        each status and its category (new, indeterminate, done).
- events:          the raw log as written.

And, so rule questions do not disadvantage it, the cartridge's rules and the usual point-in-time views as table
macros parameterised by instant: issue_state_at(t), issue_state_known_at(t, k), links_at(t), blocked_at(t),
stalled_at(t), urgent_unassigned_at(t). The agent's tool is sql_query (read-only, no file or network access, rows
capped at 200 like Keel's listing ceiling, the cut said explicitly); the schema is in the tool paragraph.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

FIELDS = ("status", "status_category", "assignee", "priority", "resolution", "issue_type")
CHANGES = {"issue.status_changed": ("status", "status_category"), "issue.assigned": ("assignee",), "issue.priority_changed": ("priority",),
           "issue.resolution_changed": ("resolution",), "issue.type_changed": ("issue_type",)}
MAX_ROWS = 200
TIMEOUT_S = 30.0
SCHEMA_VERSION = "1"  # a changed schema is built again (part of the cache file's name)

DDL = """
CREATE TABLE issues (key VARCHAR PRIMARY KEY, project VARCHAR, summary VARCHAR, reporter VARCHAR, parent VARCHAR,
                     created_at TIMESTAMP, recorded_at TIMESTAMP);
CREATE TABLE field_versions (issue_key VARCHAR, field VARCHAR, value VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP,
                             recorded_at TIMESTAMP, superseded_at TIMESTAMP, event_id VARCHAR, event_type VARCHAR);
CREATE TABLE links (src VARCHAR, relation VARCHAR, dst VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP,
                    recorded_at TIMESTAMP, superseded_at TIMESTAMP, event_id VARCHAR);
CREATE TABLE statuses (status VARCHAR, category VARCHAR);
CREATE TABLE events (id VARCHAR, type VARCHAR, issue_key VARCHAR, occurred_at TIMESTAMP, recorded_at TIMESTAMP, payload JSON);
"""

MACROS_DDL = """
CREATE MACRO issue_state_known_at(t, k) AS TABLE
    SELECT f.issue_key,
           max(CASE WHEN f.field = 'status' THEN f.value END) AS status,
           max(CASE WHEN f.field = 'status_category' THEN f.value END) AS status_category,
           max(CASE WHEN f.field = 'assignee' THEN f.value END) AS assignee,
           max(CASE WHEN f.field = 'priority' THEN f.value END) AS priority,
           max(CASE WHEN f.field = 'resolution' THEN f.value END) AS resolution,
           max(CASE WHEN f.field = 'issue_type' THEN f.value END) AS issue_type,
           max(CASE WHEN f.field = 'status_category' THEN f.valid_from END) AS status_since
    FROM field_versions f
    WHERE f.valid_from <= t AND (f.valid_to IS NULL OR f.valid_to > t)
      AND f.recorded_at <= k AND (f.superseded_at IS NULL OR f.superseded_at > k)
    GROUP BY f.issue_key;

CREATE MACRO issue_state_at(t) AS TABLE
    SELECT * FROM issue_state_known_at(t, TIMESTAMP '9999-12-31');

CREATE MACRO links_at(t) AS TABLE
    SELECT src, relation, dst, valid_from, valid_to FROM links
    WHERE superseded_at IS NULL AND valid_from <= t AND (valid_to IS NULL OR valid_to > t);

CREATE MACRO blocked_at(t) AS TABLE
    SELECT s.issue_key FROM issue_state_at(t) s
    WHERE s.status_category IS DISTINCT FROM 'done'
      AND EXISTS (SELECT 1 FROM links_at(t) l JOIN issue_state_at(t) b ON b.issue_key = l.src
                  WHERE l.relation = 'blocks' AND l.dst = s.issue_key AND b.status_category IS DISTINCT FROM 'done');

CREATE MACRO stalled_at(t) AS TABLE
    SELECT issue_key, status, status_since FROM issue_state_at(t)
    WHERE status_category = 'indeterminate' AND status_since <= t - INTERVAL 30 DAY;

CREATE MACRO urgent_unassigned_at(t) AS TABLE
    SELECT issue_key, priority, status FROM issue_state_at(t)
    WHERE priority IN ('Blocker', 'Critical', 'Highest') AND status_category IS DISTINCT FROM 'done' AND assignee IS NULL;
"""

TOOLS_TEXT = """The project's history is in a DuckDB database, queried with SQL through sql_query (read-only; a result shows at most \
200 rows and says when it cut, so filter or aggregate in SQL). Times are TIMESTAMP in UTC: write TIMESTAMP '2018-06-12 14:00'. \
Valid time is when a change happened in Jira; transaction time is when the tracker recorded it (some changes were recorded \
late, and some were first recorded with a wrong value and corrected later).
Tables:
- issues(key, project, summary, reporter, parent, created_at, recorded_at): one row per issue; key like 'MXS-12'.
- field_versions(issue_key, field, value, valid_from, valid_to, recorded_at, superseded_at, event_id, event_type): each \
field's history, bitemporal. field is one of status, status_category ('new', 'indeterminate' = in progress, 'done'), assignee, \
priority, resolution, issue_type; value NULL means empty (unassigned, unresolved). A row says the field had `value` over valid \
time [valid_from, valid_to) (valid_to NULL: still holds), as the tracker believed from recorded_at until superseded_at \
(superseded_at NULL: still believed). Current belief: superseded_at IS NULL. As recorded by an instant K: recorded_at <= K AND \
(superseded_at IS NULL OR superseded_at > K). A field's value at T: valid_from <= T AND (valid_to IS NULL OR valid_to > T). \
event_type is the log event that set the value ('issue.created' for the initial values; a change is any other). Rows are \
never deleted: a later record closes the rows it changes and adds new ones.
- links(src, relation, dst, valid_from, valid_to, recorded_at, superseded_at, event_id): src <relation> dst, bitemporal the \
same way. relation: blocks (src blocks dst), depends_on (src depends on dst), subtask_of (src is a sub-task of dst), \
duplicates, clones, relates_to (these three read in either direction).
- statuses(status, category): each status and its status_category.
- events(id, type, issue_key, occurred_at, recorded_at, payload JSON): the raw log as written (occurred_at = valid time, \
recorded_at = transaction time).
Table macros, used as FROM name(...):
- issue_state_at(t): issue_key, status, status_category, assignee, priority, resolution, issue_type, status_since (when its \
status last changed), for every issue that existed at t, as currently believed.
- issue_state_known_at(t, k): the same, as recorded by instant k.
- links_at(t): src, relation, dst, valid_from, valid_to of the links that held at t.
Rules, as table macros returning issue_key for each issue the rule holds for at instant t (trust them):
- blocked_at(t): it is not done (status_category <> 'done') and an issue that blocks it is not done.
- stalled_at(t): its status_category is 'indeterminate' (in progress) and its status has not changed for 30 days or more.
- urgent_unassigned_at(t): its priority is Blocker, Critical or Highest, it is not done, and nobody is assigned to it."""

# The ablation (bench system `sql-tables`): the same tables and the same plain-language meaning of every column, but no
# macros and no ready-made predicates, so the agent writes the bitemporal conditions itself; the rules are the
# cartridge's definitions in words, as Keel's tool paragraph gives them.
TOOLS_TEXT_TABLES = """The project's history is in a DuckDB database, queried with SQL through sql_query (read-only; a result shows at most \
200 rows and says when it cut, so filter or aggregate in SQL). Times are TIMESTAMP in UTC: write TIMESTAMP '2018-06-12 14:00'. \
Valid time is when a change happened in Jira; transaction time is when the tracker recorded it (some changes were recorded \
late, and some were first recorded with a wrong value and corrected later).
Tables:
- issues(key, project, summary, reporter, parent, created_at, recorded_at): one row per issue; key like 'MXS-12'.
- field_versions(issue_key, field, value, valid_from, valid_to, recorded_at, superseded_at, event_id, event_type): each \
field's history, bitemporal. field is one of status, status_category ('new', 'indeterminate' = in progress, 'done'), assignee, \
priority, resolution, issue_type; value NULL means empty (unassigned, unresolved). A row says the field had `value` over valid \
time from valid_from (inclusive) to valid_to (exclusive; NULL: still holds), as the tracker believed from recorded_at \
(inclusive) until superseded_at (exclusive; NULL: still believed). event_type is the log event that set the value \
('issue.created' for the initial values; a change is any other). Rows are never deleted: a later record closes the rows it \
changes and adds new ones.
- links(src, relation, dst, valid_from, valid_to, recorded_at, superseded_at, event_id): src <relation> dst, bitemporal the \
same way. relation: blocks (src blocks dst), depends_on (src depends on dst), subtask_of (src is a sub-task of dst), \
duplicates, clones, relates_to (these three read in either direction).
- statuses(status, category): each status and its status_category.
- events(id, type, issue_key, occurred_at, recorded_at, payload JSON): the raw log as written (occurred_at = valid time, \
recorded_at = transaction time).
Rules (definitions):
- blocked (issue): An issue is blocked while an issue that blocks it is not done.
- stalled (issue): An issue has stalled when it is in progress and its status has not changed for 30 days.
- urgent_unassigned (issue): An urgent issue (priority Blocker, Critical or Highest) that is not done and has nobody assigned."""

TOOLS = [{"type": "function", "function": {
    "name": "sql_query", "description": "Run one read-only SQL query (DuckDB dialect) over the project's tables and macros; returns columns and rows (at most 200, with the total when cut).",
    "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "one SELECT (or WITH ... SELECT) statement"}}, "required": ["sql"]}}}]


def ts(s: str) -> datetime:
    """A log instant as a naive UTC timestamp (the tables' TIMESTAMP)."""
    return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)


def versions(facts: list[tuple[datetime, datetime, str, Any, str]]) -> list[dict[str, Any]]:
    """Bitemporal rows of one field's history from its facts (valid_from, recorded_at, event id, value, event type).

    At each instant something was recorded, the timeline as then known: at each valid_from the latest record wins
    (a correction, recorded later for the same instant), and each value holds until the next valid_from. Rows that
    stop being part of the timeline are superseded at that instant; new ones are recorded at it."""
    open_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for tx in sorted({f[1] for f in facts}):
        best: dict[datetime, tuple[Any, ...]] = {}
        for f in facts:
            if f[1] <= tx and (f[0] not in best or (f[1], f[2]) > (best[f[0]][1], best[f[0]][2])):
                best[f[0]] = f
        starts = sorted(best)
        segments = {(s, starts[i + 1] if i + 1 < len(starts) else None, best[s][2]): best[s] for i, s in enumerate(starts)}
        for seg in [s for s in open_rows if s not in segments]:
            open_rows.pop(seg)["superseded_at"] = tx
        for seg, f in segments.items():
            if seg not in open_rows:
                open_rows[seg] = {"valid_from": seg[0], "valid_to": seg[1], "value": f[3], "event_id": f[2], "event_type": f[4],
                                  "recorded_at": tx, "superseded_at": None}
                rows.append(open_rows[seg])
    return rows


def link_versions(facts: list[tuple[datetime, datetime, str, bool]]) -> list[dict[str, Any]]:
    """Bitemporal rows of one (src, relation, dst) link from its link/unlink events (occurred, recorded, id, linked):
    as then known, replayed in valid-time order, an open link is linked once and an unlink ends it."""
    open_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for tx in sorted({f[1] for f in facts}):
        intervals, start = [], None
        for occurred, _, eid, linked in sorted((f for f in facts if f[1] <= tx), key=lambda f: (f[0], f[2])):
            if linked and start is None:
                start = (occurred, eid)
            elif not linked and start is not None:
                intervals.append((start[0], occurred, start[1]))
                start = None
        if start is not None:
            intervals.append((start[0], None, start[1]))
        for seg in [s for s in open_rows if s not in intervals]:
            open_rows.pop(seg)["superseded_at"] = tx
        for seg in intervals:
            if seg not in open_rows:
                open_rows[seg] = {"valid_from": seg[0], "valid_to": seg[1], "event_id": seg[2], "recorded_at": tx, "superseded_at": None}
                rows.append(open_rows[seg])
    return rows


def tables(events: list[dict[str, Any]]) -> dict[str, list[tuple[Any, ...]]]:
    """The rows of every table, computed from the log alone (deterministic: the same log, the same rows)."""
    issues, statuses, raw = [], {}, []
    facts: dict[tuple[str, str], list[tuple[Any, ...]]] = defaultdict(list)
    link_facts: dict[tuple[str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for e in events:
        key, p, at, rec = e["subject"].split(":", 1)[1], e["payload"], ts(e["occurred_at"]), ts(e["recorded_at"])
        raw.append((e["id"], e["type"], key, at, rec, json.dumps(p, ensure_ascii=False, sort_keys=True)))
        if p.get("status") is not None and "status_category" in p:
            statuses.setdefault(p["status"], p["status_category"])
        if e["type"] == "issue.created":
            issues.append((key, p["project"], p["summary"], p["reporter"], p["parent"], at, rec))
            for f in FIELDS:
                facts[(key, f)].append((at, rec, e["id"], p[f], e["type"]))
            if p["parent"]:
                link_facts[(key, "subtask_of", p["parent"])].append((at, rec, e["id"], True))
        elif e["type"] in CHANGES:
            for f in CHANGES[e["type"]]:
                facts[(key, f)].append((at, rec, e["id"], p[f], e["type"]))
        elif e["type"] in ("issue.linked", "issue.unlinked"):
            link_facts[(key, p["relation"], p["other"])].append((at, rec, e["id"], e["type"] == "issue.linked"))
    field_rows = [(key, f, r["value"], r["valid_from"], r["valid_to"], r["recorded_at"], r["superseded_at"], r["event_id"], r["event_type"])
                  for (key, f), line in sorted(facts.items()) for r in versions(line)]
    link_rows = [(src, rel, dst, r["valid_from"], r["valid_to"], r["recorded_at"], r["superseded_at"], r["event_id"])
                 for (src, rel, dst), line in sorted(link_facts.items()) for r in link_versions(line)]
    return {"issues": sorted(issues), "field_versions": field_rows, "links": link_rows,
            "statuses": sorted(statuses.items()), "events": raw}


def build(events: list[dict[str, Any]], path: Path | None = None, macros: bool = True) -> Path:
    """Write the database for a log to `path` (a temporary file when None) and return its path; without `macros`,
    the tables alone (the `sql-tables` ablation)."""
    import duckdb

    if path is None:
        path = Path(tempfile.mkdtemp(prefix="keel-sql-")) / "jira.duckdb"
    tmp = path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(DDL + (MACROS_DDL if macros else ""))
    for name, rows in tables(events).items():
        if rows:
            con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    con.execute("CHECKPOINT")
    con.close()
    tmp.replace(path)
    return path


def connect(path: Path) -> Any:
    """A read-only connection with no file or network access: the agent can read the tables and nothing else."""
    import duckdb

    return duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})


_lock = threading.Lock()


@cache
def _tenant_db(path: Path) -> Any:
    return connect(path)


def tenant_db(data: Path, tenant: str, events: list[dict[str, Any]], macros: bool = True) -> Any:
    """The tenant's database, built once per log (cached next to the data, like the RAG embeddings)."""
    digest = hashlib.sha1((SCHEMA_VERSION + "\n" + "\n".join(json.dumps(e, sort_keys=True) for e in events)).encode()).hexdigest()[:12]
    path = data / f"{tenant}.sql{'' if macros else '-tables'}-{digest}.duckdb"
    with _lock:
        if not path.exists():
            build(events, path, macros=macros)
        return _tenant_db(path)


def cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return str(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def query(con: Any, sql: str, max_rows: int = MAX_ROWS) -> str:
    """One query's result as the agent reads it: columns and rows (cut at max_rows, the total and the cut said)."""
    cur = con.cursor()
    timer = threading.Timer(TIMEOUT_S, cur.interrupt)
    timer.start()
    try:
        cur.execute(sql)
        if cur.description is None:
            return json.dumps({"columns": [], "rows": [], "total_rows": 0, "truncated": False})
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - a bad query is the agent's to read, as a database would say it
        msg = str(exc)
        if "INTERRUPT" in msg.upper():
            msg = f"the query ran longer than {TIMEOUT_S:.0f} s and was stopped"
        return json.dumps({"error": f"{type(exc).__name__}: {msg[:1000]}"})
    finally:
        timer.cancel()
        cur.close()
    out: dict[str, Any] = {"columns": columns, "rows": [[cell(v) for v in r] for r in rows[:max_rows]], "total_rows": len(rows),
                           "truncated": len(rows) > max_rows}
    if out["truncated"]:
        out["note"] = f"only the first {max_rows} of {len(rows)} rows are shown; filter, aggregate or ORDER BY ... LIMIT in SQL to see the rest"
    return json.dumps(out, ensure_ascii=False)


# ---- the ceiling: every benchmark question as one SQL query, no agent --------------------------------------


def oracle_answer(con: Any, q: dict[str, Any]) -> Any:
    """A question answered by SQL over the tables and macros alone (for the tests: tables vs the oracle's gold)."""
    p = q["params"]

    def at(k: str) -> str | None:
        return f"TIMESTAMP '{datetime.strptime(p[k], '%Y-%m-%d %H:%M UTC').strftime('%Y-%m-%d %H:%M:%S')}'" if k in p else None

    def rows(sql: str, *args: Any) -> list[tuple[Any, ...]]:
        return con.execute(sql, list(args)).fetchall()

    def state(t: str, known: str | None = None) -> str:
        return f"issue_state_known_at({t}, {known})" if known else f"issue_state_at({t})"

    key = lambda s: s.split(":", 1)[1]
    if q["type"] in ("current", "as_of", "known_at", "abstain"):
        t = at("as_of")
        if t is None:
            r = rows("SELECT value FROM field_versions WHERE issue_key = ? AND field = ? AND superseded_at IS NULL AND valid_to IS NULL",
                     key(p["subject"]), p["predicate"])
        else:
            r = rows(f"SELECT {p['predicate']} FROM {state(t, at('known_at'))} WHERE issue_key = ?", key(p["subject"]))
        return r[0][0] if r else None
    if q["type"] == "links":
        t, k, rel = at("as_of"), key(p["subject"]), p["relation"]
        direction = {"blocks": "in", "depends_on": "out"}.get(rel, "any")
        ends = {"in": ["src FROM links_at({t}) WHERE dst = ?"], "out": ["dst FROM links_at({t}) WHERE src = ?"]}
        parts = ends.get(direction) or ends["in"] + ends["out"]
        others = " UNION ".join(f"SELECT {x.format(t=t)} AND relation = '{rel}'" for x in parts)
        r = rows(f"SELECT s.issue_key, s.status_category FROM ({others}) o(k) JOIN issue_state_at({t}) s ON s.issue_key = o.k",
                 *([k] * len(parts)))
        return sorted(i for i, c in r if (c == "done") == p["done"])
    if q["type"] == "rule":
        return bool(rows(f"SELECT 1 FROM {p['rule']}_at({at('as_of')}) WHERE issue_key = ?", key(p["subject"])))
    sub, t = p["subtype"], at("as_of")
    if sub in ("status_at", "known_at"):
        return sorted(r[0] for r in rows(f"SELECT issue_key FROM {state(t, at('known_at'))} WHERE status = ?", p["status"]))
    if sub == "count_at":
        return rows(f"SELECT count(*) FROM {state(t)} WHERE status_category = 'indeterminate'")[0][0]
    if sub == "rule_at":
        return sorted(r[0] for r in rows(f"SELECT issue_key FROM {p['rule']}_at({t})"))
    return sorted(r[0] for r in rows(
        f"SELECT DISTINCT issue_key FROM field_versions WHERE field = ? AND superseded_at IS NULL AND event_type <> 'issue.created' "
        f"AND valid_from >= {at('since')} AND valid_from < {at('until')}", p["field"]))
