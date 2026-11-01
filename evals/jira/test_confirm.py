"""The confirmation set's selection rules (evals/jira/screen.py) and pre-registered analysis
(evals/jira/confirm_analysis.py), on synthetic numbers. No model is called.

    uv run --with numpy pytest evals/jira/test_confirm.py -q
"""

from __future__ import annotations

import pytest

from evals.jira import screen

HAIKU = "anthropic/claude-haiku-5.5"


def test_rules_one_to_four_are_the_held_out_sets():
    ok = {"issues": 1500, "mismatched": 5, "urgent_priority": True, "uncategorised": 0.004}
    assert screen.failed_rules(ok) == []
    assert screen.failed_rules(ok | {"issues": 899}) == [1]
    assert screen.failed_rules(ok | {"issues": 4001}) == [1]
    assert screen.failed_rules(ok | {"mismatched": 16}) == [2]  # over 1% of 1,500 issues
    assert screen.failed_rules(ok | {"urgent_priority": False}) == [3]
    assert screen.failed_rules(ok | {"uncategorised": 0.011}) == [4]


def test_rule_five_needs_both_checks_100_questions_and_half_of_every_type():
    full = dict(screen.COUNTS)
    assert screen.rule_five(full, checks_passed=True)
    assert not screen.rule_five(full, checks_passed=False)
    short = full | {"links": 5, "rule": 12, "tenant": 20}  # IntelDAOS CART at seed 31: 112 questions, links under half
    assert not screen.rule_five(short, checks_passed=True)
    assert screen.rule_five(full | {"links": 8, "rule": 8, "tenant": 12}, checks_passed=True)  # 103, every type at half
    assert not screen.rule_five(full | {"links": 8, "rule": 8, "tenant": 12, "as_of": 20}, checks_passed=True)  # 98
    assert not screen.rule_five(full | {"tenant": 11}, checks_passed=True)  # 116, but under half of 24


def test_select_takes_the_most_questions_one_per_instance_with_the_tie_breaks():
    c = [
        {"instance": "MongoDB", "key": "A", "questions": 129, "mismatched": 3, "issues": 1000},
        {"instance": "MongoDB", "key": "B", "questions": 129, "mismatched": 1, "issues": 900},  # fewer mismatches wins
        {"instance": "Qt", "key": "C", "questions": 120, "mismatched": 0, "issues": 1000},
        {"instance": "Qt", "key": "D", "questions": 120, "mismatched": 0, "issues": 2000},  # more issues wins
        {"instance": "Jira", "key": "E", "questions": 110, "mismatched": 0, "issues": 3000},
        {"instance": "JFrog", "key": "F", "questions": 101, "mismatched": 0, "issues": 3000},
    ]
    assert [(x["instance"], x["key"]) for x in screen.select(c)] == [("MongoDB", "B"), ("Qt", "D"), ("Jira", "E")]
    assert [x["key"] for x in screen.select(c[:2])] == ["B"]  # fewer than three instances: what passes
    assert screen.select([]) == []


def test_tenant_ids_are_instance_dash_key_in_lower_case():
    assert screen.tenant_id("IntelDAOS", "CART") == "inteldaos-cart"


def questions() -> dict[str, dict]:
    qs = {}
    for t in ("a", "b"):
        for kind in ("current", "rule"):
            for i in range(10):
                qs[f"{t}-{kind}-{i:03d}"] = {"id": f"{t}-{kind}-{i:03d}", "tenant": t, "type": kind, "params": {}}
    return qs


def rows(qs: dict[str, dict], system: str, wrong: set[int], reps: int = 3) -> list[dict]:
    return [{"id": q, "system": system, "model": HAIKU, "rep": r, "type": qs[q]["type"], "tenant": qs[q]["tenant"],
             "correct": n not in wrong, "cost_usd": 0.001, "tokens": {"input": 1000, "output": 10}}
            for n, q in enumerate(sorted(qs)) for r in range(reps)]


def test_the_three_hypotheses_and_their_decision_rules():
    pytest.importorskip("numpy")
    from evals.jira import confirm_analysis as ca

    assert [(h["a"], h["b"], h["margin"]) for h in ca.HYPOTHESES] == [
        ("sqlgen2", "sql", -0.02), ("keel+query", "sql", -0.02), ("keel+query", "keel", 0.0)]
    qs = questions()
    data = (rows(qs, "sql", set()) + rows(qs, "sqlgen2", set()) + rows(qs, "keel+query", set())
            + rows(qs, "keel", set(range(0, 40, 4))))  # keel misses 10 of 40
    res = ca.analyze(qs, data, b=500)
    h = {(x["a"], x["b"]): x for x in res["hypotheses"]}
    assert h[("sqlgen2", "sql")]["confirmed"] and h[("sqlgen2", "sql")]["diff"] == 0
    assert h[("keel+query", "sql")]["confirmed"]
    assert h[("keel+query", "keel")]["confirmed"] and h[("keel+query", "keel")]["diff"] == pytest.approx(0.25)
    worse = rows(qs, "sqlgen2", set(range(0, 40, 5)))  # 8 of 40 wrong: -20 points, far past the margin
    res = ca.analyze(qs, rows(qs, "sql", set()) + worse, b=500)
    only = res["hypotheses"]
    assert [(x["a"], x["b"]) for x in only] == [("sqlgen2", "sql")]  # a hypothesis without its conditions is left out
    assert not only[0]["confirmed"]
    assert "H1" in ca.markdown(res) and "not confirmed" in ca.markdown(res)
