"""The one-call path for the tenant-wide questions (evals/jira/requests.py) reaches every gold answer, and the call
classifier labels the patterns requests.md counts. No model is called.

    uv run --extra mcp --with tiktoken --with numpy pytest evals/jira/test_requests.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("tiktoken")

DATA = Path(os.environ.get("KEEL_JIRA_DATA", Path.home() / "datasets" / "public-jira" / "export"))


def test_the_classifier_names_the_wasted_calls():
    from evals.jira.requests import classify

    run = {"calls": [
        {"tool": "search", "args": {"kind": "project", "text": "INDY", "detail": "key"}, "tokens": 6, "error": False},
        {"tool": "search", "args": {"kind": "issue", "where": {"status": "A"}, "as_of": "t", "detail": "key"}, "tokens": 20, "error": False},
        {"tool": "search", "args": {"kind": "issue", "where": {"status": "A"}, "as_of": "t", "related_to": "project:INDY", "via": "in_project", "detail": "brief"},
         "tokens": 90, "error": False},
        {"tool": "search", "args": {"kind": "issue", "where": {"status": "B"}, "as_of": "t", "detail": "key"}, "tokens": 20, "error": False},
        {"tool": "context", "args": {"subject": "issue:INDY-1", "as_of": "t"}, "tokens": 300, "error": False},
        {"tool": "search", "args": {"kind": "issue", "as_of": "t", "detail": "brief", "limit": 0}, "tokens": 9000, "error": False},
    ]}
    assert classify(run, "count_at") == ["scope lookup", "answer", "repeat", "fan-out over values", "per-subject check", "whole-kind listing"]


@pytest.mark.skipif(not (DATA / "spring.questions.json").exists(), reason="Public Jira Dataset export not found")
def test_one_call_answers_every_tenant_wide_question():
    from evals.jira.requests import run_ideal

    rows = run_ideal(DATA)
    assert len(rows) == 72
    missed = [(r["q"]["id"], r["answer"], r["q"]["answer"]) for r in rows if r["answer"] != r["q"]["answer"]]
    assert not missed, missed
    idle = [r for r in rows if "since_answer" in r]
    assert idle and all(r["since_answer"] == r["q"]["answer"] for r in idle)  # idle without the rule, from `since`
