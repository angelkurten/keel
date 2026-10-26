"""The generated SQL baseline (evals/jira/sqlgen_system.py) against every gold answer of the benchmark.

    uv run pytest evals/jira/test_sqlgen.py

Skipped when the Public Jira Dataset export is not on disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from evals.jira import sqlgen_system

DATA = Path("~/datasets/public-jira/export").expanduser()


@pytest.mark.skipif(not (DATA / "mariadb.questions.json").exists(), reason="the Public Jira Dataset export is not on disk")
@pytest.mark.parametrize("tenant", ["hyperledger", "mariadb", "spring"])
@pytest.mark.parametrize("v2", [False, True])
def test_generated_sql_reaches_every_gold_answer(tenant, v2):
    events = [json.loads(line) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]
    con = sqlgen_system.build(events, tenant, v2=v2)
    questions = json.loads((DATA / f"{tenant}.questions.json").read_text())
    answer = sqlgen_system.oracle_answer_v2 if v2 else sqlgen_system.oracle_answer
    bad = [(q["id"], got, q["answer"]) for q in questions if (got := answer(con, tenant, q)) != q["answer"]]
    assert not bad, (len(bad), bad[:5])


# ---- the agent condition: an agent writing SQL against the generated schema and macros (bench system `sqlgen`) ----

GENERATED = ("state_at", "state_known_at", "relations_at", "relations_known_at", "keel_facts", "blocked_at", "stalled_at",
             "urgent_unassigned_at")


def test_generated_tool_text_names_the_tenant_and_every_macro():
    text = sqlgen_system.tools_text("t")
    assert "'t'" in text
    for name in GENERATED + ("entities", "facts", "relations", "evidence", "json_extract_string"):
        assert name in text, name


def test_generated_database_is_read_only_and_answers(tmp_path):
    from evals.jira import sql_system
    from evals.jira.test_sql import LOG

    con = sqlgen_system.tenant_db(tmp_path, "t", LOG)
    out = json.loads(sql_system.query(con, "SELECT subject FROM blocked_at('t', '2020-01-15T00:00:00+00:00')"))
    assert out["rows"] == [["issue:P-1"]]
    assert "error" in json.loads(sql_system.query(con, "DELETE FROM facts"))
    assert sqlgen_system.tenant_db(tmp_path, "t", LOG) is con  # built once per log, cached next to the data


def test_bench_runs_the_generated_sql_condition_with_a_scripted_model(monkeypatch, tmp_path):
    pytest.importorskip("numpy")  # the harness's own dependencies, not the core's
    pytest.importorskip("mcp")
    from evals.jira import bench
    from evals.jira.test_sql import LOG

    (tmp_path / "t.bitemporal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    monkeypatch.setitem(bench.INSTANCES, "t", ("Test", "P"))
    seen: list[list[dict]] = []

    class Gw:
        def route(self, model):
            return "fake"

        def chat(self, model, messages, tools):
            seen.append([dict(m) for m in messages])
            assert [t["function"]["name"] for t in tools] == ["sql_query"]
            if len(messages) == 2:
                call = {"id": "c1", "function": {"name": "sql_query", "arguments": json.dumps({"sql": (
                    "SELECT json_extract_string(value, '$') FROM state_at('t', '2020-01-15T00:00:00+00:00') "
                    "WHERE subject = 'issue:P-1' AND predicate = 'status'")})}}
                return {"content": "", "tool_calls": [call]}, {"input": 10, "output": 5}, 0.001
            return {"content": 'ANSWER: "In Progress"'}, {"input": 20, "output": 5}, 0.001

    q = {"id": "t-as_of-001", "tenant": "t", "type": "as_of", "question": "What was the status of P-1 on 2020-01-15 00:00 UTC?",
         "answer": "In Progress", "answer_type": "value", "params": {}}
    res = bench.ask(Gw(), tmp_path, "sqlgen", "m", q, {}, {})
    assert res["correct"] and res["system"] == "sqlgen" and not res["calls"][0]["error"]
    assert sqlgen_system.tools_text("t") in seen[0][0]["content"]
    assert "In Progress" in seen[1][-1]["content"]


# ---- sqlgen v2 (post hoc): per-kind views, changes and the generated paragraph (bench system `sqlgen2`) ----


def test_the_sqlgen_condition_keeps_its_schema_and_paragraph():
    """`sqlgen` stays what the held-out run measured: no per-kind view in its database, its hand-written paragraph."""
    from keel import sqlgen

    script = sqlgen.generate(sqlgen_system.cartridge(v2=False))
    assert "issue_state_at" not in script and "issue_changes" not in script
    # byte for byte the script at 89e8510 (the held-out sqlgen cache files are named by it)
    assert hashlib.sha256(script.encode()).hexdigest() == "23da4006d14f0c8b3042f0cbc4dc630a3a69876d839cbb11d13c78c617c57f09"
    assert "issue_state_at" in sqlgen.generate(sqlgen_system.cartridge(v2=True))
    assert sqlgen_system.tools_text("t") == sqlgen_system.TOOLS_TEXT.format(tenant="t")


def test_the_v2_paragraph_is_generated_from_the_cartridge():
    from keel import sqlgen

    text = sqlgen_system.tools_text_v2("t")
    assert sqlgen.describe(sqlgen_system.cartridge(v2=True), "t") in text and "sql_query" in text


def test_v2_database_answers_and_is_cached_apart(tmp_path):
    from evals.jira import sql_system
    from evals.jira.test_sql import LOG

    con = sqlgen_system.tenant_db(tmp_path, "t", LOG, v2=True)
    out = json.loads(sql_system.query(con, "SELECT id, status, in_project FROM issue_state_at('t', TIMESTAMP '2020-01-15') ORDER BY id"))
    assert out["rows"][0] == ["P-1", "In Progress", "P"]
    assert sqlgen_system.tenant_db(tmp_path, "t", LOG, v2=True) is con
    assert sqlgen_system.tenant_db(tmp_path, "t", LOG) is not con


def test_bench_runs_the_sqlgen2_condition_with_a_scripted_model(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("mcp")
    from evals.jira import bench
    from evals.jira.test_sql import LOG

    (tmp_path / "t.bitemporal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    monkeypatch.setitem(bench.INSTANCES, "t", ("Test", "P"))
    seen: list[list[dict]] = []

    class Gw:
        def route(self, model):
            return "fake"

        def chat(self, model, messages, tools):
            seen.append([dict(m) for m in messages])
            assert [t["function"]["name"] for t in tools] == ["sql_query"]
            if len(messages) == 2:
                call = {"id": "c1", "function": {"name": "sql_query", "arguments": json.dumps(
                    {"sql": "SELECT status FROM issue_state_at('t', TIMESTAMP '2020-01-15') WHERE id = 'P-1'"})}}
                return {"content": "", "tool_calls": [call]}, {"input": 10, "output": 5}, 0.001
            return {"content": 'ANSWER: "In Progress"'}, {"input": 20, "output": 5}, 0.001

    q = {"id": "t-as_of-001", "tenant": "t", "type": "as_of", "question": "What was the status of P-1 on 2020-01-15 00:00 UTC?",
         "answer": "In Progress", "answer_type": "value", "params": {}}
    res = bench.ask(Gw(), tmp_path, "sqlgen2", "m", q, {}, {})
    assert res["correct"] and res["system"] == "sqlgen2" and not res["calls"][0]["error"]
    assert sqlgen_system.tools_text_v2("t") in seen[0][0]["content"]
    assert "In Progress" in seen[1][-1]["content"]
