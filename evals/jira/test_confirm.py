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
    assert [x["key"] for x in screen.select(c[:2])] == ["B", "A"]  # one instance: filled, two at most (second amendment)
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


def test_exploratory_pairs_are_reported_without_a_decision():
    pytest.importorskip("numpy")
    from evals.jira import confirm_analysis as ca

    qs = questions()
    data = rows(qs, "sql", set()) + rows(qs, "sql-log", set(range(0, 40, 4)))  # sql-log misses 10 of 40
    res = ca.analyze(qs, data, b=500, pairs=[("sql-log", "sql"), ("keel", "sql")])
    assert [(x["a"], x["b"]) for x in res["pairs"]] == [("sql-log", "sql")]  # a pair without its rows is left out
    assert res["pairs"][0]["diff"] == pytest.approx(-0.25) and "confirmed" not in res["pairs"][0]
    md = ca.markdown(res)
    assert "Exploratory" in md and "sql-log - sql" in md


def test_the_hypotheses_can_be_read_for_another_agent():
    pytest.importorskip("numpy")
    from evals.jira import confirm_analysis as ca

    qs = questions()
    sonnet = [r | {"model": "anthropic/claude-sonnet-5.5"} for r in rows(qs, "sqlgen2", set(range(0, 40, 4)))]
    data = rows(qs, "sql", set()) + rows(qs, "sqlgen2", set()) + [r | {"model": "anthropic/claude-sonnet-5.5"} for r in rows(qs, "sql", set())] + sonnet
    assert ca.analyze(qs, data, b=500)["hypotheses"][0]["diff"] == 0  # Haiku by default, as registered
    h = ca.analyze(qs, data, b=500, agent="Sonnet")["hypotheses"][0]
    assert h["diff"] == pytest.approx(-0.25)


def test_amended_candidates_are_every_project_neither_set_used(tmp_path):
    """Amendment 2026-11-01: no candidate of the eight unused instances passed; every project of the restored instances
    but the held-out set's three is a candidate."""
    names = ["RedHat-PROJQUAY", "RedHat-ARQ", "Sakai-GRBK", "Sakai-EVALSYS", "JiraEcosystem-AC", "JiraEcosystem-UPM", "Qt-QBS"]
    for n in names:
        (tmp_path / f"{n}.json").write_text("[]")
    (tmp_path / "sakai-grbk.questions.json").write_text("[]")  # generated files are not exports
    assert [p.stem for p in screen.candidates(tmp_path)] == ["JiraEcosystem-UPM", "Qt-QBS", "RedHat-ARQ", "Sakai-EVALSYS"]


def test_fewer_than_three_instances_fill_to_three_at_most_two_per_instance():
    """Second amendment, 2026-11-02, before Apache and Mojang were restored."""
    c = [{"instance": "Apache", "key": k, "questions": q, "mismatched": 0, "issues": 1000}
         for k, q in (("A", 129), ("B", 128), ("C", 127))] + [{"instance": "Mojang", "key": "M", "questions": 110, "mismatched": 0, "issues": 1000}]
    assert [x["key"] for x in screen.select(c)] == ["A", "M", "B"]
    assert [x["key"] for x in screen.select(c[:3])] == ["A", "B"]  # one instance: two at most


def test_the_bench_knows_the_selected_tenants():
    from evals.jira import bench

    manifest = __import__("json").loads((screen.Path(__file__).parent / "confirm" / "manifest.json").read_text())
    for t, m in manifest["tenants"].items():
        assert bench.instance(t) == (m["instance"], m["project"])
