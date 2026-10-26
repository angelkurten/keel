"""The held-out question set (evals/jira/heldout/): new tenants, disjoint from development, gold reproduced three ways.

    uv run --extra mcp --with tiktoken --with numpy --with duckdb==2.0.0 pytest evals/jira/test_heldout.py -q

The committed question files must match the manifest written before any run. When the held-out data is on disk
(~/datasets/public-jira/heldout, or KEEL_JIRA_HELDOUT), the questions are generated again from it and must come out
byte for byte the same, and every gold answer must be reached by Keel's primitives, by hand-written SQL over
sql_system's tables and by SQL over the macros generated from the cartridge, with no model anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from evals.jira import questions as qgen

HERE = Path(__file__).parent / "heldout"
DEV = Path(os.environ.get("KEEL_JIRA_DATA", Path.home() / "datasets" / "public-jira" / "export"))
DATA = Path(os.environ.get("KEEL_JIRA_HELDOUT", Path.home() / "datasets" / "public-jira" / "heldout"))
MANIFEST = json.loads((HERE / "manifest.json").read_text())
TENANTS = sorted(MANIFEST["tenants"])
DEV_TENANTS = ("hyperledger", "mariadb", "spring")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def committed(tenant: str) -> list[dict]:
    return json.loads((HERE / f"{tenant}.questions.json").read_text())


def test_heldout_tenants_come_from_jira_instances_development_never_used():
    from evals.jira.bench import HELDOUT, INSTANCES, instance

    assert set(HELDOUT) == set(TENANTS) and set(INSTANCES) == set(DEV_TENANTS)  # the development default is unchanged
    assert not {HELDOUT[t][0] for t in HELDOUT} & {INSTANCES[t][0] for t in DEV_TENANTS}
    assert all(instance(t) == HELDOUT[t] for t in HELDOUT)  # the bench can ask about them


def test_committed_questions_match_the_manifest():
    for t in TENANTS:
        info = MANIFEST["tenants"][t]
        assert sha256(HERE / f"{t}.questions.json") == info["questions_sha256"], t
        qs = committed(t)
        assert len(qs) == info["questions"] and all(q["tenant"] == t for q in qs)
        assert {k: sum(q["type"] == k for q in qs) for k in info["by_type"]} == info["by_type"]


@pytest.mark.skipif(not (DEV / "mariadb.questions.json").exists(), reason="the development questions are not on disk")
def test_heldout_questions_share_nothing_with_development():
    dev = [q for t in DEV_TENANTS for q in json.loads((DEV / f"{t}.questions.json").read_text())]
    held = [q for t in TENANTS for q in committed(t)]
    assert not {q["id"] for q in dev} & {q["id"] for q in held}
    assert not {q["question"] for q in dev} & {q["question"] for q in held}
    assert not ({q["params"].get("subject") for q in dev} - {None}) & {q["params"].get("subject") for q in held}


needs_data = pytest.mark.skipif(not all((DATA / f"{t}.bitemporal.jsonl").exists() for t in TENANTS),
                                reason="the held-out data is not on disk")


def events_of(tenant: str) -> list[dict]:
    return [json.loads(line) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]


@needs_data
@pytest.mark.parametrize("tenant", TENANTS)
def test_heldout_data_and_questions_regenerate_identically(tenant):
    info = MANIFEST["tenants"][tenant]
    assert sha256(DATA / f"{tenant}.bitemporal.jsonl") == info["bitemporal_sha256"]
    assert sha256(DATA / f"{tenant}.injections.json") == info["injections_sha256"]
    injections = json.loads((DATA / f"{tenant}.injections.json").read_text())
    again = qgen.generate(events_of(tenant), injections, MANIFEST["seed"], MANIFEST["counts"])
    assert again == committed(tenant)


@needs_data
@pytest.mark.parametrize("tenant", TENANTS)
def test_heldout_gold_is_reached_by_keel_by_hand_sql_and_by_generated_sql(tenant):
    pytest.importorskip("duckdb")
    from evals.jira import sql_system, sqlgen_system

    events, qs = events_of(tenant), committed(tenant)
    assert qgen.check(events, qs) == 0  # Keel's primitives
    hand = sql_system.connect(sql_system.build(events))
    assert [q["id"] for q in qs if sql_system.oracle_answer(hand, q) != q["answer"]] == []
    gen = sqlgen_system.build(events, tenant)
    assert [q["id"] for q in qs if sqlgen_system.oracle_answer(gen, tenant, q) != q["answer"]] == []


SMOKE = {  # one representative tool call per condition; {t} is the tenant, {k} an issue key of the tenant
    "keel": ("search", {"kind": "issue", "where": {"status": "Open"}, "as_of": "2020-01-01T00:00:00Z", "detail": "key"}),
    "sql": ("sql_query", {"sql": "SELECT count(*) FROM issue_state_at(TIMESTAMP '2020-01-01')"}),
    "sql-tables": ("sql_query", {"sql": "SELECT count(*) FROM field_versions"}),
    "sqlgen": ("sql_query", {"sql": "SELECT count(*) FROM state_at('{t}', '2020-01-01T00:00:00+00:00')"}),
    "api": ("get_issue", {"key": "{k}"}),
}


@needs_data
@pytest.mark.parametrize("system", sorted(SMOKE))
def test_every_condition_runs_on_the_heldout_tenants_without_a_model(monkeypatch, system):
    """Each planned condition, through bench.ask with a scripted model: the prompt names the tenant's instance and
    project, and the tool answers without an error. Nothing is sent to a model."""
    pytest.importorskip("numpy")
    pytest.importorskip("mcp")
    pytest.importorskip("duckdb")
    from evals.jira import bench

    monkeypatch.setattr(bench, "KEEL_COMPACT", True)
    monkeypatch.setattr(bench, "KEEL_READ_ONLY", True)
    bench.keel_tools.cache_clear()
    for t in TENANTS:
        q = committed(t)[0]
        name, args = SMOKE[system]
        args = json.loads(json.dumps(args).replace("{t}", t).replace("{k}", q["params"]["subject"].split(":", 1)[1]))
        seen: dict = {}

        class Gw:
            def route(self, model):
                return "fake"

            def chat(self, model, messages, tools, name=name, args=args, seen=seen):
                seen.setdefault("system", messages[0]["content"])
                if len(messages) == 2:
                    call = {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}
                    return {"content": "", "tool_calls": [call]}, {"input": 1, "output": 1}, 0.0
                seen["result"] = messages[-1]["content"]
                return {"content": "ANSWER: null"}, {"input": 1, "output": 1}, 0.0

        apis = {t: bench.Api(DATA, t)} if system == "api" else {}
        res = bench.ask(Gw(), DATA, system, "m", q, {}, apis)
        instance, project = bench.HELDOUT[t]
        assert f"project {project} of the {instance} tracker" in seen["system"], (t, system)
        assert not res["calls"][0]["error"] and '{"error"' not in seen["result"][:20], (t, system, seen["result"][:300])
    bench.keel_tools.cache_clear()
