"""The bitemporal SQL baseline, generated: the Jira cartridge compiled by keel/sqlgen.py instead of hand-written.

sql_system.py (the `sql` bench system) models the log in DuckDB by hand: field_versions, links and six macros,
about 330 lines written for Jira. Here the same questions are answered over what the generator emits from
cartridge.yaml alone: Keel's own facts/relations tables (filled by ingesting the log through Keel), state_at,
state_known_at, relations_at and one `<rule>_at` per rule. Each benchmark question is one SQL query (the
ceiling an agent writing SQL against the generated schema can reach), checked against the oracle's gold.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from keel import Event, Ingestor, MemoryStore, load_cartridge, sqlgen
from keel.store.sql import INSERT_FACT, INSERT_RELATION, _fact_row, _relation_row

HERE = Path(__file__).parent


def build(events: list[dict[str, Any]], tenant: str) -> Any:
    """Keel ingests the log; its rows are copied into an in-memory DuckDB with the generated schema and macros."""
    import duckdb

    c = load_cartridge(HERE)
    store = MemoryStore()
    Ingestor(c, store).ingest([Event(**e) for e in events], strict=True)
    con = duckdb.connect()
    con.execute(sqlgen.generate(c))
    copy(con, store.backup(tenant))
    return con


def copy(con: Any, backup: dict[str, Any]) -> None:
    con.executemany("INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?)",
                    [(e.tenant, e.kind, e.id, e.ref, json.dumps(e.attrs)) for e in backup["entities"]])
    con.executemany(INSERT_RELATION, [_relation_row(r) for r in backup["relations"]])
    con.executemany(INSERT_FACT, [_fact_row(f) for f in backup["facts"]])


def oracle_answer(con: Any, tenant: str, q: dict[str, Any]) -> Any:
    """A question answered by one SQL query over the generated macros."""
    p = q["params"]

    def at(k: str) -> str | None:
        return datetime.strptime(p[k], "%Y-%m-%d %H:%M UTC").strftime("%Y-%m-%dT%H:%M:%S+00:00") if k in p else None

    def rows(sql: str, *args: Any) -> list[tuple[Any, ...]]:
        return con.execute(sql, [tenant, *args]).fetchall()

    def key(s: str) -> str:
        return s.split(":", 1)[1]

    value = lambda r: json.loads(r[0][0]) if r else None
    if q["type"] in ("current", "as_of", "known_at", "abstain"):
        t, k = at("as_of"), at("known_at")
        if t is None:
            return value(rows("SELECT value FROM state_at(?, keel_now()) WHERE subject = ? AND predicate = ? AND valid_to IS NULL",
                              p["subject"], p["predicate"]))
        return value(rows("SELECT value FROM state_known_at(?, ?, k := ?) WHERE subject = ? AND predicate = ?", t, k, p["subject"], p["predicate"]))
    if q["type"] == "links":
        direction = {"blocks": "in", "depends_on": "out"}.get(p["relation"], "any")
        ends = {"in": ["SELECT src AS o FROM relations_at($1, $2) WHERE rel = $3 AND dst = $4"],
                "out": ["SELECT dst AS o FROM relations_at($1, $2) WHERE rel = $3 AND src = $4"]}
        others = " UNION ".join(ends.get(direction) or ends["in"] + ends["out"])
        r = con.execute(f"SELECT n.o, json_extract_string(s.value, '$') FROM ({others}) n JOIN state_at($1, $2) s "
                        "ON s.subject = n.o AND s.predicate = 'status_category'", [tenant, at("as_of"), p["relation"], p["subject"]]).fetchall()
        return sorted(key(o) for o, cat in r if (cat == "done") == p["done"])
    if q["type"] == "rule":
        return bool(rows(f"SELECT 1 FROM {sqlgen.rule_macro(p['rule'])}_at(?, ?) WHERE subject = ?", at("as_of"), p["subject"]))
    sub = p["subtype"]
    if sub in ("status_at", "known_at"):
        return sorted(key(r[0]) for r in rows("SELECT subject FROM state_known_at(?, ?, k := ?) WHERE predicate = 'status' AND json_extract_string(value, '$') = ?",
                                              at("as_of"), at("known_at"), p["status"]))
    if sub == "count_at":
        return rows("SELECT count(*) FROM state_at(?, ?) WHERE predicate = 'status_category' AND json_extract_string(value, '$') = ?",
                    at("as_of"), p["category"])[0][0]
    if sub == "rule_at":
        return sorted(key(r[0]) for r in rows(f"SELECT subject FROM {sqlgen.rule_macro(p['rule'])}_at(?, ?)", at("as_of")))
    # changed: a value that replaced another (it has a predecessor in its history) within [since, until), as now known
    return sorted(key(r[0]) for r in rows(
        "SELECT DISTINCT f.subject FROM keel_facts($1) f WHERE f.tenant = $1 AND f.predicate = $2 AND f.valid_from >= $3 AND f.valid_from < $4 "
        "AND (f.valid_to IS NULL OR f.valid_to > f.valid_from) AND EXISTS (SELECT 1 FROM keel_facts($1) g WHERE g.tenant = $1 "
        "AND g.subject = f.subject AND g.predicate = f.predicate AND g.valid_to = f.valid_from AND g.valid_from < g.valid_to)",
        p["field"], at("since"), at("until")))
