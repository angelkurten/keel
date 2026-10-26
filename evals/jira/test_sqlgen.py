"""The generated SQL baseline (evals/jira/sqlgen_system.py) against every gold answer of the benchmark.

    uv run pytest evals/jira/test_sqlgen.py

Skipped when the Public Jira Dataset export is not on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from evals.jira import sqlgen_system

DATA = Path("~/datasets/public-jira/export").expanduser()


@pytest.mark.skipif(not (DATA / "mariadb.questions.json").exists(), reason="the Public Jira Dataset export is not on disk")
@pytest.mark.parametrize("tenant", ["hyperledger", "mariadb", "spring"])
def test_generated_sql_reaches_every_gold_answer(tenant):
    events = [json.loads(line) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]
    con = sqlgen_system.build(events, tenant)
    questions = json.loads((DATA / f"{tenant}.questions.json").read_text())
    bad = [(q["id"], got, q["answer"]) for q in questions if (got := sqlgen_system.oracle_answer(con, tenant, q)) != q["answer"]]
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
