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
