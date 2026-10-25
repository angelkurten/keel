"""The bitemporal SQL baseline (evals/jira/sql_system.py): the log in DuckDB tables, the rules as table macros.

    uv run --extra mcp --with duckdb==2.0.0 --with numpy pytest evals/jira/test_sql.py

The tables are checked on a small log with a late arrival, a correction and a link that ends, and, when the
dataset is on disk, against every gold answer of the benchmark (the ceiling an agent writing SQL can reach).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")  # an eval-side dependency (`--with duckdb`), never the core's

from evals.jira import sql_system

DATA = Path("~/datasets/public-jira/export").expanduser()


def ev(id: str, type: str, key: str, occurred: str, payload: dict[str, Any], recorded: str | None = None) -> dict[str, Any]:
    return {"id": id, "type": type, "subject": f"issue:{key}", "tenant": "t", "source": "jira", "payload": payload,
            "occurred_at": f"{occurred}+00:00", "recorded_at": f"{recorded or occurred}+00:00"}


def created(key: str, at: str, status: str = "Open", cat: str = "new", **kw: Any) -> dict[str, Any]:
    p = {"project": "P", "summary": f"{key} summary", "issue_type": "Bug", "status": status, "status_category": cat,
         "priority": "Major", "assignee": "user-a", "reporter": "user-r", "resolution": None, "parent": None} | kw
    return ev(f"jira:t:{key}:created", "issue.created", key, at, p)


LOG = [
    created("P-1", "2020-01-01T00:00:00"),
    created("P-2", "2020-01-01T00:00:00", priority="Critical", assignee=None),
    created("P-3", "2020-01-01T00:00:00", parent="P-1"),
    # P-1 goes in progress on Jan 10, but the change is recorded only on Jan 20 (a late arrival)
    ev("jira:t:P-1:h1", "issue.status_changed", "P-1", "2020-01-10T00:00:00",
       {"status": "In Progress", "status_category": "indeterminate", "by": None}, recorded="2020-01-20T00:00:00"),
    # P-2 is resolved on Feb 1; first recorded wrong (Closed... as "Won't Fix" then corrected to "Fixed" on Feb 5)
    ev("jira:t:P-2:h2:wrong", "issue.resolution_changed", "P-2", "2020-02-01T00:00:00", {"resolution": "Won't Fix", "by": None}),
    ev("jira:t:P-2:h2", "issue.resolution_changed", "P-2", "2020-02-01T00:00:00", {"resolution": "Fixed", "by": None},
       recorded="2020-02-05T00:00:00"),
    # P-2 blocks P-1 from Jan 5 to Mar 1
    ev("jira:t:P-2:l1", "issue.linked", "P-2", "2020-01-05T00:00:00", {"relation": "blocks", "other": "P-1"}),
    ev("jira:t:P-2:l2", "issue.unlinked", "P-2", "2020-03-01T00:00:00", {"relation": "blocks", "other": "P-1"}),
    ev("jira:t:P-2:h3", "issue.status_changed", "P-2", "2020-02-01T00:00:00", {"status": "Closed", "status_category": "done", "by": None}),
]
LOG.sort(key=lambda e: (e["recorded_at"], e["id"]))


@pytest.fixture(scope="module")
def con() -> Any:
    return sql_system.connect(sql_system.build(LOG))


def one(con: Any, sql: str) -> Any:
    rows = con.execute(sql).fetchall()
    assert len(rows) == 1, rows
    return rows[0][0] if len(rows[0]) == 1 else rows[0]


def test_tables_hold_issues_versions_and_links(con):
    assert one(con, "SELECT count(*) FROM issues") == 3
    assert one(con, "SELECT parent FROM issues WHERE key = 'P-3'") == "P-1"
    fields = {r[0] for r in con.execute("SELECT DISTINCT field FROM field_versions").fetchall()}
    assert fields == {"status", "status_category", "assignee", "priority", "resolution", "issue_type"}
    assert one(con, "SELECT count(*) FROM events") == len(LOG)
    assert one(con, "SELECT category FROM statuses WHERE status = 'In Progress'") == "indeterminate"
    # the parent of a sub-task is a link too, from its creation
    assert one(con, "SELECT relation FROM links WHERE src = 'P-3' AND dst = 'P-1'") == "subtask_of"


def value(con: Any, key: str, field: str, at: str, known: str | None = None) -> Any:
    tx = "superseded_at IS NULL" if known is None else (
        f"recorded_at <= TIMESTAMP '{known}' AND (superseded_at IS NULL OR superseded_at > TIMESTAMP '{known}')")
    return one(con, f"SELECT value FROM field_versions WHERE issue_key = '{key}' AND field = '{field}' AND {tx} "
                    f"AND valid_from <= TIMESTAMP '{at}' AND (valid_to IS NULL OR valid_to > TIMESTAMP '{at}')")


def test_as_of_and_known_at_handle_a_late_arrival(con):
    assert value(con, "P-1", "status", "2020-01-15") == "In Progress"  # as known now
    assert value(con, "P-1", "status", "2020-01-15", known="2020-01-16") == "Open"  # not yet recorded then
    assert value(con, "P-1", "status", "2020-01-15", known="2020-01-20") == "In Progress"
    assert value(con, "P-1", "status", "2020-01-05", known="2020-01-16") == "Open"


def test_known_at_handles_a_correction(con):
    assert value(con, "P-2", "resolution", "2020-02-02") == "Fixed"
    assert value(con, "P-2", "resolution", "2020-02-02", known="2020-02-03") == "Won't Fix"
    assert value(con, "P-2", "resolution", "2020-02-02", known="2020-02-06") == "Fixed"
    assert value(con, "P-2", "resolution", "2020-01-15", known="2020-02-03") is None  # unresolved before
    # the wrong version is closed in transaction time, never deleted
    assert one(con, "SELECT superseded_at FROM field_versions WHERE event_id = 'jira:t:P-2:h2:wrong'").isoformat() == "2020-02-05T00:00:00"


def test_links_have_valid_time(con):
    live = "SELECT src FROM links WHERE dst = 'P-1' AND relation = 'blocks' AND superseded_at IS NULL AND valid_from <= TIMESTAMP '{0}' AND (valid_to IS NULL OR valid_to > TIMESTAMP '{0}')"
    assert con.execute(live.format("2020-02-15")).fetchall() == [("P-2",)]
    assert con.execute(live.format("2020-03-15")).fetchall() == []


def keys(con: Any, sql: str) -> list[str]:
    return sorted(r[0] for r in con.execute(sql).fetchall())


def test_state_macros(con):
    rows = con.execute("SELECT issue_key, status, status_category, assignee FROM issue_state_at(TIMESTAMP '2020-01-15') ORDER BY 1").fetchall()
    assert rows == [("P-1", "In Progress", "indeterminate", "user-a"), ("P-2", "Open", "new", None), ("P-3", "Open", "new", "user-a")]
    assert one(con, "SELECT status FROM issue_state_known_at(TIMESTAMP '2020-01-15', TIMESTAMP '2020-01-16') WHERE issue_key = 'P-1'") == "Open"
    assert keys(con, "SELECT issue_key FROM issue_state_at(TIMESTAMP '2019-12-01')") == []  # nothing existed yet


def test_rule_macros(con):
    # blocked: P-1 not done and P-2 (its blocker) not done, until P-2 closes on Feb 1
    assert keys(con, "SELECT issue_key FROM blocked_at(TIMESTAMP '2020-01-15')") == ["P-1"]
    assert keys(con, "SELECT issue_key FROM blocked_at(TIMESTAMP '2020-02-15')") == []
    # stalled: in progress since Jan 10, 30 days later
    assert keys(con, "SELECT issue_key FROM stalled_at(TIMESTAMP '2020-02-08')") == []
    assert keys(con, "SELECT issue_key FROM stalled_at(TIMESTAMP '2020-02-10')") == ["P-1"]
    # urgent_unassigned: P-2 is Critical, nobody assigned, until it is done
    assert keys(con, "SELECT issue_key FROM urgent_unassigned_at(TIMESTAMP '2020-01-15')") == ["P-2"]
    assert keys(con, "SELECT issue_key FROM urgent_unassigned_at(TIMESTAMP '2020-02-15')") == []


def test_query_tool_caps_rows_and_says_so(con):
    out = json.loads(sql_system.query(con, "SELECT * FROM range(500) t(n)", max_rows=200))
    assert len(out["rows"]) == 200 and out["truncated"] is True and out["total_rows"] == 500
    assert "200" in out["note"]
    out = json.loads(sql_system.query(con, "SELECT key, created_at FROM issues ORDER BY key"))
    assert out["columns"] == ["key", "created_at"] and out["rows"][0] == ["P-1", "2020-01-01 00:00:00"] and out["truncated"] is False


def test_query_tool_reports_errors_and_refuses_writes(con):
    assert "error" in json.loads(sql_system.query(con, "SELECT nope FROM issues"))
    assert "error" in json.loads(sql_system.query(con, "DELETE FROM issues"))
    assert one(con, "SELECT count(*) FROM issues") == 3


def test_schema_text_documents_every_table_and_macro():
    for name in ("issues", "field_versions", "links", "statuses", "events", "issue_state_at", "issue_state_known_at",
                 "links_at", "blocked_at", "stalled_at", "urgent_unassigned_at", "superseded_at", "valid_to"):
        assert name in sql_system.TOOLS_TEXT, name


def test_bench_runs_the_sql_condition_with_a_scripted_model(monkeypatch, tmp_path):
    pytest.importorskip("numpy")  # the harness's own dependencies, not the core's
    pytest.importorskip("mcp")
    from evals.jira import bench

    (tmp_path / "t.bitemporal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    monkeypatch.setitem(bench.INSTANCES, "t", ("Test", "P"))
    seen: list[list[dict[str, Any]]] = []

    class Gw:
        def route(self, model: str) -> str:
            return "fake"

        def chat(self, model: str, messages: list[dict[str, Any]], tools: Any) -> tuple[dict[str, Any], dict[str, int], float]:
            seen.append([dict(m) for m in messages])
            assert [t["function"]["name"] for t in tools] == ["sql_query"]
            if len(messages) == 2:
                call = {"id": "c1", "function": {"name": "sql_query", "arguments": json.dumps(
                    {"sql": "SELECT status FROM issue_state_at(TIMESTAMP '2020-01-15') WHERE issue_key = 'P-1'"})}}
                return {"content": "", "tool_calls": [call]}, {"input": 10, "output": 5}, 0.001
            return {"content": 'ANSWER: "In Progress"'}, {"input": 20, "output": 5}, 0.001

    q = {"id": "t-as_of-001", "tenant": "t", "type": "as_of", "question": "What was the status of P-1 on 2020-01-15 00:00 UTC?",
         "answer": "In Progress", "answer_type": "value", "params": {}}
    res = bench.ask(Gw(), tmp_path, "sql", "m", q, {}, {})
    assert res["correct"] and res["system"] == "sql" and res["calls"][0]["tool"] == "sql_query" and not res["calls"][0]["error"]
    assert sql_system.TOOLS_TEXT in seen[0][0]["content"]  # the tool paragraph is the only part of the prompt that differs
    assert "In Progress" in seen[1][-1]["content"]


@pytest.mark.skipif(not (DATA / "mariadb.questions.json").exists(), reason="the Public Jira Dataset export is not on disk")
@pytest.mark.parametrize("tenant", ["hyperledger", "mariadb", "spring"])
def test_sql_reaches_every_gold_answer(tenant):
    """Every question answered by one SQL query over the tables and macros, no agent: the ceiling, and a check
    of the tables against the oracle's gold (late arrivals and corrections included)."""
    events = [json.loads(line) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]
    con = sql_system.connect(sql_system.build(events))
    questions = json.loads((DATA / f"{tenant}.questions.json").read_text())
    bad = [(q["id"], got, q["answer"]) for q in questions if (got := sql_system.oracle_answer(con, q)) != q["answer"]]
    assert not bad, bad[:5]


# ---- the ablation: the same tables, no macros and no query recipes (bench system `sql-tables`) ----------------

MACROS = ("issue_state_at", "issue_state_known_at", "links_at", "blocked_at", "stalled_at", "urgent_unassigned_at")


def test_tables_only_database_has_the_tables_and_no_macros():
    con = sql_system.connect(sql_system.build(LOG, macros=False))
    assert one(con, "SELECT count(*) FROM field_versions") > 0 and one(con, "SELECT count(*) FROM links") > 0
    for m in MACROS:
        assert "error" in json.loads(sql_system.query(con, f"SELECT * FROM {m}(TIMESTAMP '2020-01-15')")), m
    # the bitemporal predicates still answer, written by hand
    assert value(con, "P-1", "status", "2020-01-15", known="2020-01-16") == "Open"


def test_tables_only_text_documents_columns_but_no_macros_or_recipes():
    text = sql_system.TOOLS_TEXT_TABLES
    for name in ("issues", "field_versions", "links", "statuses", "events", "valid_from", "valid_to", "recorded_at", "superseded_at"):
        assert name in text, name
    for name in MACROS:
        assert name not in text, name
    assert "As recorded by an instant K" not in text and "A field's value at T" not in text
    assert "recorded_at <=" not in text and "valid_to > " not in text  # no ready-made predicates
    for rule in ("blocked", "stalled", "urgent_unassigned"):  # the cartridge's definitions, in words
        assert rule in text


def test_bench_runs_the_tables_only_condition(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("mcp")
    from evals.jira import bench

    (tmp_path / "t.bitemporal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    monkeypatch.setitem(bench.INSTANCES, "t", ("Test", "P"))
    seen: list[list[dict[str, Any]]] = []

    class Gw:
        def route(self, model: str) -> str:
            return "fake"

        def chat(self, model: str, messages: list[dict[str, Any]], tools: Any) -> tuple[dict[str, Any], dict[str, int], float]:
            seen.append([dict(m) for m in messages])
            if len(messages) == 2:
                call = {"id": "c1", "function": {"name": "sql_query", "arguments": json.dumps(
                    {"sql": "SELECT * FROM issue_state_at(TIMESTAMP '2020-01-15')"})}}
                return {"content": "", "tool_calls": [call]}, {"input": 10, "output": 5}, 0.001
            return {"content": 'ANSWER: "unknown"'}, {"input": 20, "output": 5}, 0.001

    q = {"id": "t-as_of-001", "tenant": "t", "type": "as_of", "question": "What was the status of P-1 on 2020-01-15 00:00 UTC?",
         "answer": "In Progress", "answer_type": "value", "params": {}}
    res = bench.ask(Gw(), tmp_path, "sql-tables", "m", q, {}, {})
    assert res["system"] == "sql-tables" and res["calls"][0]["error"]  # the macro does not exist there
    assert sql_system.TOOLS_TEXT_TABLES in seen[0][0]["content"] and "issue_state_at" not in seen[0][0]["content"]
