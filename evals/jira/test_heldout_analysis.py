"""The held-out set's pre-registered analysis (evals/jira/heldout_analysis.py) on synthetic rows. No model is called.

    uv run --with numpy pytest evals/jira/test_heldout_analysis.py -q
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

from evals.jira import heldout_analysis as ha

LUNA, HAIKU = "openai/gpt-6-luna", "anthropic/claude-haiku-5.5"


def questions() -> dict[str, dict]:
    qs = {}
    for t in ("a", "b"):
        for kind in ("current", "rule"):
            for i in range(10):
                qs[f"{t}-{kind}-{i:03d}"] = {"id": f"{t}-{kind}-{i:03d}", "tenant": t, "type": kind, "params": {}}
        for i in range(10):
            qs[f"{t}-tenant-{i:03d}"] = {"id": f"{t}-tenant-{i:03d}", "tenant": t, "type": "tenant", "params": {"subtype": "status_at"}}
    return qs


def rows(qs: dict[str, dict], system: str, model: str, right: float, reps: int = 3, cost: float = 0.001) -> list[dict]:
    out = []
    for n, q in enumerate(sorted(qs)):
        for r in range(reps):
            out.append({"id": q, "system": system, "model": model, "rep": r, "type": qs[q]["type"], "tenant": qs[q]["tenant"],
                        "correct": (n % 10) < right * 10, "cost_usd": cost, "tokens": {"input": 1000, "output": 10}})
    return out


def test_the_planned_comparisons_are_ten_and_holm_adjusted():
    assert len(ha.PLANNED) == 10
    assert ("Luna", "keel", "sql") in ha.PLANNED and ("Haiku", "sql", "sqlgen") in ha.PLANNED
    qs = questions()
    data = []
    for m in (LUNA, HAIKU):
        data += rows(qs, "keel", m, 0.9) + rows(qs, "sql", m, 0.6) + rows(qs, "sqlgen", m, 0.6)
        data += rows(qs, "sql-tables", m, 0.5, reps=1) + rows(qs, "api", m, 0.5, reps=1, cost=0.004)
    res = ha.analyze(qs, data, b=500)
    comps = res["comparisons"]
    assert len(comps) == 10
    c = next(x for x in comps if (x["agent"], x["a"], x["b"]) == ("Luna", "keel", "sql"))
    assert c["n"] == len(qs) and c["diff"] == pytest.approx(0.3)
    assert c["p_holm"] >= c["cluster"]["p"]
    same = next(x for x in comps if (x["agent"], x["a"], x["b"]) == ("Luna", "sql", "sqlgen"))
    assert same["diff"] == 0 and same["p_holm"] == 1.0


def test_secondary_metrics_cost_per_correct_and_input_tokens():
    qs = questions()
    data = rows(qs, "keel", LUNA, 0.5, reps=2, cost=0.002)
    res = ha.analyze(qs, data, b=200)
    s = res["systems"][("Luna", "keel")]
    assert s["accuracy"] == pytest.approx(0.5)
    assert s["cost_per_correct"] == pytest.approx(0.004)  # 0.002 a run, one run in two correct
    assert s["input_tokens"] == 1000 and s["runs"] == 2 * len(qs) and s["reps"] == 2
    assert set(s["by_scope"]) == {"one", "tenant"}


def test_markdown_names_every_planned_comparison():
    qs = questions()
    data = [r for m in (LUNA, HAIKU) for s in ("keel", "sql", "sqlgen", "sql-tables", "api") for r in rows(qs, s, m, 0.7, reps=1)]
    text = ha.markdown(ha.analyze(qs, data, b=200))
    assert text.count("| Luna |") >= 5 and "Holm" in text and "cost per correct" in text
