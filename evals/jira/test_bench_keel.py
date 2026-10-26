"""bench's Keel condition with a scripted model (no gateway): what the agent is handed, and `--keel-read-only`.

    uv run --extra mcp --with numpy pytest evals/jira/test_bench_keel.py -q
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from evals.jira.test_sql import LOG

pytest.importorskip("numpy")  # the harness's own dependencies, not the core's
pytest.importorskip("mcp")


def run(monkeypatch: Any, tmp_path: Any, read_only: bool) -> tuple[dict[str, Any], list[Any], str]:
    from evals.jira import bench

    (tmp_path / "t.bitemporal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    monkeypatch.setitem(bench.INSTANCES, "t", ("Test", "P"))
    monkeypatch.setattr(bench, "KEEL_COMPACT", True)
    monkeypatch.setattr(bench, "KEEL_READ_ONLY", read_only)
    bench.keel_tools.cache_clear()
    seen: dict[str, Any] = {}

    class Gw:
        def route(self, model: str) -> str:
            return "fake"

        def chat(self, model: str, messages: list[dict[str, Any]], tools: Any) -> tuple[dict[str, Any], dict[str, int], float]:
            seen.setdefault("system", messages[0]["content"])
            seen.setdefault("tools", tools)
            if len(messages) == 2:
                call = {"id": "c1", "function": {"name": "search", "arguments": json.dumps(
                    {"kind": "issue", "where": {"status": ["In Progress", "Done"]}, "as_of": "2020-01-15T00:00:00Z", "detail": "key"})}}
                return {"content": "", "tool_calls": [call]}, {"input": 10, "output": 5}, 0.001
            return {"content": 'ANSWER: ["P-1"]'}, {"input": 20, "output": 5}, 0.001

    q = {"id": "t-tenant-001", "tenant": "t", "type": "tenant", "question": "On 2020-01-15 00:00 UTC, which issues of the P project had status In Progress?",
         "answer": ["P-1"], "answer_type": "set", "params": {}}
    res = bench.ask(Gw(), tmp_path, "keel", "m", q, {}, {})
    bench.keel_tools.cache_clear()
    return res, seen["tools"], seen["system"]


def test_bench_hands_the_agent_what_the_server_says(monkeypatch, tmp_path):
    """The system prompt carries the bound server's own instructions (the project named), the tools its listing (all five
    by default, with the current descriptions), and a where over a list of statuses answers in one call."""
    from keel.tools import DESCRIPTIONS

    res, tools, system = run(monkeypatch, tmp_path, read_only=False)
    assert [t["function"]["name"] for t in tools] == ["context", "history", "search", "annotate", "related"]
    assert all(t["function"]["description"] == DESCRIPTIONS[t["function"]["name"]] for t in tools)
    assert "project (project:P)" in system and "search(rule=)" in system
    assert res["correct"] and len(res["calls"]) == 1 and not res["calls"][0]["error"]


def test_bench_keel_read_only_drops_annotate(monkeypatch, tmp_path):
    """`--keel-read-only`: the Jira agents never annotate, so they are served the four read tools (keel serve --read-only)."""
    res, tools, _ = run(monkeypatch, tmp_path, read_only=True)
    assert [t["function"]["name"] for t in tools] == ["context", "history", "search", "related"]
    assert res["correct"] and res.get("keel_read_only") is True
