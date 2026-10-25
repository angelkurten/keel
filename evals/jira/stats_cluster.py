"""Re-analysis of the Jira benchmark's result rows, for the statistics a reviewer asked for. No LLM call, 0 USD.

Questions are nested in tenants and question types (templates), so resampling them as independent draws can
understate the uncertainty. Beside the paper's bootstrap over questions (`iid`, as `bench.bootstrap`), this computes:

- `stratified`: questions resampled within each tenant x type cell (the design's composition kept fixed);
- `cluster`: tenant x type cells resampled whole, as clusters (one-issue: 18 cells, tenant-wide: 15, by sub-type);
- `cluster_fine`: tenant x template cells (type plus predicate/relation/rule/field) resampled whole.

A question's repetitions are averaged before resampling, as in the paper. B = 10,000, seed 0. Paired differences
are Keel minus each system on the questions both answered; Holm-Bonferroni adjusts the paper's Tables 6-7 (and
those plus the two ablation tables). It also splits the held-out tenants (Hyperledger, Spring) from the pilot tenant
(MariaDB), breaks tenant-wide accuracy down by sub-type, and checks the CDN-405 rerun.

    uv run --with numpy python -m evals.jira.stats_cluster [--data ~/datasets/public-jira/export]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

B = 10_000
SEED = 0
HERE = Path(__file__).parent
RESULTS = HERE / "results"
MAIN = ["full-luna", "full-haiku", "full-sonnet", "full-memories", "full-tgms", "full-tgms-pev"]
AGENTS = {
    "openai/gpt-6-luna": "Luna",
    "anthropic/claude-haiku-5.5": "Haiku",
    "anthropic/claude-sonnet-5.5": "Sonnet",
    "anthropic/claude-opus-5.5": "Opus",
}
AGENT_ORDER = ["Luna", "Haiku", "Sonnet", "Opus"]
TENANTS = ["hyperledger", "mariadb", "spring"]
HELD_OUT = {"hyperledger", "spring"}
METHODS = ["iid", "stratified", "cluster", "cluster_fine"]
SUBTYPES = ["status_at", "count_at", "changed", "known_at", "rule_at"]
API_EXPRESSIBLE = {
    "status_at",
    "count_at",
    "changed",
}  # JQL's WAS / history operators; not as-known-at, not rules
# the paired tests the paper reports: Table 6 (one issue), Table 7 (tenant-wide)
TABLE6 = {
    "Luna": ["api", "rag", "mem0", "graphiti"],
    "Haiku": ["api", "rag", "mem0", "graphiti"],
    "Sonnet": ["api", "rag"],
}
TABLE7 = {
    "Luna": ["api", "rag", "closed", "tgms"],
    "Haiku": ["api", "rag", "closed"],
    "Sonnet": ["api", "rag", "closed"],
}


# ---------------------------------------------------------------- resampling


def _summary(v: np.ndarray, means: np.ndarray) -> tuple[float, float, float, float]:
    """Point estimate, 95% percentile interval and two-sided p of the mean being 0 (as `bench.bootstrap`)."""
    p = min(1.0, 2 * min(float((means <= 0).mean()), float((means >= 0).mean())))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(v.mean()), float(lo), float(hi), p


def iid_dist(values: Sequence[float], b: int = B, seed: int = SEED) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    return v[np.random.default_rng(seed).integers(0, len(v), (b, len(v)))].mean(axis=1)


def stratified_dist(
    values: Sequence[float], strata: Sequence[str], b: int = B, seed: int = SEED
) -> np.ndarray:
    """Resample questions with replacement inside each stratum; every stratum keeps its size."""
    v, s = np.asarray(values, dtype=float), np.asarray(strata)
    rng, total = np.random.default_rng(seed), np.zeros(b)
    for key in sorted(set(strata)):
        cell = v[s == key]
        total += cell[rng.integers(0, len(cell), (b, len(cell)))].sum(axis=1)
    return total / len(v)


def cluster_dist(
    values: Sequence[float], clusters: Sequence[str], b: int = B, seed: int = SEED
) -> np.ndarray:
    """Resample whole clusters with replacement; the mean is over every question of the drawn clusters."""
    v, c = np.asarray(values, dtype=float), np.asarray(clusters)
    keys = sorted(set(clusters))
    sums = np.array([v[c == k].sum() for k in keys])
    sizes = np.array([(c == k).sum() for k in keys], dtype=float)
    idx = np.random.default_rng(seed).integers(0, len(keys), (b, len(keys)))
    return sums[idx].sum(axis=1) / sizes[idx].sum(axis=1)


def bootstrap(
    values: Sequence[float], b: int = B, seed: int = SEED
) -> tuple[float, float, float, float]:
    return _summary(np.asarray(values, dtype=float), iid_dist(values, b, seed))


def stratified_bootstrap(
    values: Sequence[float], strata: Sequence[str], b: int = B, seed: int = SEED
) -> tuple[float, float, float, float]:
    return _summary(np.asarray(values, dtype=float), stratified_dist(values, strata, b, seed))


def cluster_bootstrap(
    values: Sequence[float], clusters: Sequence[str], b: int = B, seed: int = SEED
) -> tuple[float, float, float, float]:
    return _summary(np.asarray(values, dtype=float), cluster_dist(values, clusters, b, seed))


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return adj


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval: the honest interval when a bootstrap collapses (k = 0 or k = n)."""

    def solve(
        f: Callable[[float], float],
    ) -> float:  # f increasing in p, find f(p) = 0 by bisection
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) < 0 else (lo, mid)
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: (1 - _binom_cdf(k - 1, n, p)) - alpha / 2)
    upper = 1.0 if k == n else solve(lambda p: alpha / 2 - _binom_cdf(k, n, p))
    return lower, upper


def sign_test(pos: int, neg: int) -> float:
    """Exact two-sided sign test on the discordant questions (ties dropped)."""
    n = pos + neg
    if n == 0:
        return 1.0
    return min(1.0, 2 * _binom_cdf(min(pos, neg), n, 0.5))


# ---------------------------------------------------------------- data


def load(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (RESULTS / f"{name}.jsonl").open()]


def questions(data: Path) -> dict[str, dict[str, Any]]:
    return {q["id"]: q for t in TENANTS for q in json.load((data / f"{t}.questions.json").open())}


def per_question(rows: list[dict[str, Any]]) -> dict[str, float]:
    by: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by[r["id"]].append(bool(r["correct"]))
    return {q: sum(v) / len(v) for q, v in by.items()}


def cell(q: dict[str, Any]) -> str:
    """Tenant x question type; tenant-wide questions by their sub-type."""
    kind = f"tenant:{q['params']['subtype']}" if q["type"] == "tenant" else q["type"]
    return f"{q['tenant']}/{kind}"


def template(q: dict[str, Any]) -> str:
    """Tenant x template: the type plus the parameter that picks the template's wording and semantics."""
    p = q["params"]
    extra = p.get("rule") or p.get("field") or p.get("relation") or p.get("predicate") or ""
    return f"{cell(q)}:{extra}"


class Analysis:
    def __init__(self, qs: dict[str, dict[str, Any]], b: int = B, seed: int = SEED) -> None:
        self.qs, self.b, self.seed = qs, b, seed

    def stats(self, acc: dict[str, float], paired: bool = False) -> dict[str, Any]:
        ids = sorted(acc)
        v = [acc[i] for i in ids]
        cells = [cell(self.qs[i]) for i in ids]
        fine = [template(self.qs[i]) for i in ids]
        out: dict[str, Any] = {
            "n": len(ids),
            "mean": float(np.mean(v)),
            "clusters": len(set(cells)),
            "clusters_fine": len(set(fine)),
        }
        for name, (_, lo, hi, p) in {
            "iid": bootstrap(v, self.b, self.seed),
            "stratified": stratified_bootstrap(v, cells, self.b, self.seed),
            "cluster": cluster_bootstrap(v, cells, self.b, self.seed),
            "cluster_fine": cluster_bootstrap(v, fine, self.b, self.seed),
        }.items():
            out[name] = {"lo": lo, "hi": hi, **({"p": p} if paired else {})}
        if paired:
            pos, neg = sum(x > 0 for x in v), sum(x < 0 for x in v)
            out.update(pos=pos, neg=neg, sign_p=sign_test(pos, neg))
        else:
            if all(x in (0.0, 1.0) for x in v) and v and (sum(v) in (0, len(v))):
                out["exact"] = clopper_pearson(int(sum(v)), len(v))
        return out

    def paired(self, keel: dict[str, float], other: dict[str, float]) -> dict[str, Any] | None:
        both = sorted(keel.keys() & other.keys())
        if not both:
            return None
        res = self.stats({q: keel[q] - other[q] for q in both}, paired=True)
        res["keel"], res["other"] = (
            float(np.mean([keel[q] for q in both])),
            float(np.mean([other[q] for q in both])),
        )
        return res

    def did(
        self, k1: dict[str, float], o1: dict[str, float], k2: dict[str, float], o2: dict[str, float]
    ) -> dict[str, Any]:
        """(Keel - X) on subset 1 minus (Keel - X) on subset 2, each resampled stratified and independently."""

        def dist(k: dict[str, float], o: dict[str, float], seed: int) -> tuple[float, np.ndarray]:
            both = sorted(k.keys() & o.keys())
            d = [k[q] - o[q] for q in both]
            return float(np.mean(d)), stratified_dist(
                d, [cell(self.qs[q]) for q in both], self.b, seed
            )

        m1, d1 = dist(k1, o1, self.seed)
        m2, d2 = dist(k2, o2, self.seed + 1)
        _, lo, hi, p = _summary(np.array([m1 - m2]), d1 - d2)
        return {"mean": m1 - m2, "lo": lo, "hi": hi, "p": p}


# ---------------------------------------------------------------- the analyses


def run(data: Path, b: int = B, seed: int = SEED) -> dict[str, Any]:
    qs = questions(data)
    an = Analysis(qs, b, seed)
    rows = [r for name in MAIN for r in load(name)]
    by: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
        list
    )  # (agent, system, scope)
    for r in rows:
        by[(AGENTS[r["model"]], r["system"], "tenant" if r["type"] == "tenant" else "one")].append(
            r
        )
    acc = {k: per_question(v) for k, v in by.items()}
    out: dict[str, Any] = {
        "b": b,
        "seed": seed,
        "files": MAIN,
        "accuracy": [],
        "paired": [],
        "holm": {},
        "held_out": [],
        "subtypes": [],
        "api_groups": [],
        "ablations": [],
        "rerun_405": {},
    }

    # 1. accuracy and paired differences, every bootstrap
    for (agent, system, scope), a in sorted(acc.items()):
        out["accuracy"].append(
            {
                "agent": agent,
                "system": system,
                "scope": scope,
                "reps": _reps(by[(agent, system, scope)]),
                **an.stats(a),
            }
        )
        if system != "keel" and (agent, "keel", scope) in acc:
            res = an.paired(acc[(agent, "keel", scope)], a)
            if res:
                table = TABLE6 if scope == "one" else TABLE7
                out["paired"].append(
                    {
                        "agent": agent,
                        "system": system,
                        "scope": scope,
                        "in_table": system in table.get(agent, []),
                        **res,
                    }
                )

    # ablations: rules (with - none) and brief (main - full), same agents
    for kind, path in [("rules", "ablation-no-rules"), ("brief", "ablation-full-detail")]:
        ab: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in load(path):
            ab[(AGENTS[r["model"]], "tenant" if r["type"] == "tenant" else "one")].append(r)
        for (agent, scope), rs in sorted(ab.items()):
            other = per_question(rs)
            main = {q: v for q, v in acc[(agent, "keel", scope)].items() if q in other}
            res = an.paired(main, other)
            out["ablations"].append({"ablation": kind, "agent": agent, "scope": scope, **res})

    # 2. Holm across the paper's families, for each resampling scheme
    tables = [p for p in out["paired"] if p["in_table"]]
    for fam, tests in {
        "tables_6_7": tables,
        "tables_6_7_and_ablations": tables + out["ablations"],
    }.items():
        out["holm"][fam] = {"m": len(tests)}
        for method in METHODS:
            raw = [max(t[method]["p"], 1 / b) for t in tests]  # a bootstrap p of 0 means p < 1/B
            for t, a in zip(tests, holm(raw)):
                t[method].setdefault("holm", {})[fam] = a

    # 3. held-out tenants against the pilot tenant
    for agent in ["Luna", "Haiku", "Sonnet"]:
        for scope in ["one", "tenant"]:
            parts: dict[str, dict[str, dict[str, float]]] = {}
            for split, keep in [("held_out", HELD_OUT), ("mariadb", {"mariadb"})]:
                parts[split] = {}
                for system in ["keel", "api", "rag"]:
                    a = acc.get((agent, system, scope), {})
                    parts[split][system] = {q: v for q, v in a.items() if qs[q]["tenant"] in keep}
                entry: dict[str, Any] = {"agent": agent, "scope": scope, "split": split}
                for system, a in parts[split].items():
                    entry[system] = an.stats(a) if a else None
                entry["keel_minus_api"] = an.paired(parts[split]["keel"], parts[split]["api"])
                out["held_out"].append(entry)
            out["held_out"].append(
                {
                    "agent": agent,
                    "scope": scope,
                    "split": "held_out_minus_mariadb",
                    "did_keel_minus_api": an.did(
                        parts["held_out"]["keel"],
                        parts["held_out"]["api"],
                        parts["mariadb"]["keel"],
                        parts["mariadb"]["api"],
                    ),
                }
            )

    # 4. tenant-wide by sub-type, and the sub-types the API can express against those it cannot
    for (agent, system, scope), a in sorted(acc.items()):
        if scope != "tenant":
            continue
        row: dict[str, Any] = {"agent": agent, "system": system}
        for st in SUBTYPES:
            vals = [v for q, v in a.items() if qs[q]["params"]["subtype"] == st]
            row[st] = {"mean": float(np.mean(vals)), "n": len(vals)} if vals else None
        for group, keep in [
            ("api_expressible", API_EXPRESSIBLE),
            ("not_api_expressible", set(SUBTYPES) - API_EXPRESSIBLE),
        ]:
            vals = {q: v for q, v in a.items() if qs[q]["params"]["subtype"] in keep}
            row[group] = an.stats(vals) if vals else None
        out["subtypes"].append(row)
    for agent in ["Luna", "Haiku", "Sonnet"]:
        k, o = acc[(agent, "keel", "tenant")], acc[(agent, "api", "tenant")]
        for group, keep in [
            ("api_expressible", API_EXPRESSIBLE),
            ("not_api_expressible", set(SUBTYPES) - API_EXPRESSIBLE),
        ]:
            sel = {q for q in k if qs[q]["params"]["subtype"] in keep}
            out["api_groups"].append(
                {
                    "agent": agent,
                    "group": group,
                    **an.paired({q: k[q] for q in sel}, {q: o[q] for q in sel if q in o}),
                }
            )

    # 6. Luna's Keel tenant-wide before and after the CDN-405 rows were asked again
    out["rerun_405"] = rerun_405()
    return out


def _reps(rows: list[dict[str, Any]]) -> int:
    return len({r["rep"] for r in rows})


def rerun_405() -> dict[str, dict[str, Any]]:
    """Luna's tenant-wide rows before the CDN-405 rows were asked again (the 405 rows counted wrong) and after."""
    discarded, out = load("full-luna.discarded-405"), {}
    for system in sorted({r["system"] for r in discarded}):
        main = [r for r in load("full-luna") if r["system"] == system and r["type"] == "tenant"]
        bad = [r for r in discarded if r["system"] == system]
        gone = {(r["id"], r["rep"]) for r in bad}
        before = [r for r in main if (r["id"], r["rep"]) not in gone] + bad
        reruns = [r for r in main if (r["id"], r["rep"]) in gone]
        acc_after, acc_before = per_question(main), per_question(before)
        out[system] = {
            "discarded_rows": len(bad),
            "discarded_correct": sum(r["correct"] for r in bad),
            "rerun_rows": len(reruns),
            "rerun_correct": sum(r["correct"] for r in reruns),
            "rerun_ids": sorted(f"{i}#rep{rep}" for i, rep in gone),
            "accuracy_before": sum(acc_before.values()) / len(acc_before),
            "accuracy_after": sum(acc_after.values()) / len(acc_after),
        }
    return out


# ---------------------------------------------------------------- markdown


def _pt(x: float) -> str:
    return f"{100 * x:+.1f}"


def _p(p: float, b: int) -> str:
    return f"<{1 / b:.4f}" if p == 0 else f"{p:.3f}"


def _acc(s: dict[str, Any]) -> str:
    """Accuracy, with the exact binomial interval when every question is right (or every one wrong)."""
    if "exact" in s:
        return f"{s['mean']:.2f} (exact {s['exact'][0]:.2f}-{s['exact'][1]:.2f})"
    return f"{s['mean']:.2f}"


DEGENERATE = (
    "A bootstrap interval collapses to a point when every resample is the same: an accuracy of 1.00 (or 0.00) on "
    'every question, or two systems right and wrong on exactly the same questions. "0 [0, 0], p = 1.0" then '
    "says nothing about uncertainty and should not be printed as a test result. Report instead (a) the count: "
    '"identical on all 315 questions (0 discordant)"; (b) each accuracy with an exact (Clopper-Pearson) interval, '
    "e.g. 72/72 = 1.00 [0.95, 1.00], 0/72 = 0.00 [0.00, 0.05]; (c) if a bound on the difference is wanted, the "
    "exact upper bound on the discordance rate: with 0 discordant of n, at most 1 - 0.025^(1/n) (n = 315: 1.2 "
    "points; n = 72: 5.0 points), so the difference lies within +/- that; and (d) leave such cells out of the Holm "
    "family or keep them at p = 1, which changes no other adjusted p. Where a bootstrap p is 0 it means "
    "p < 1/B = 0.0001, not 0; it enters Holm as 1/B. Sonnet's one-issue Keel vs API and vs RAG are this case: "
    "the three systems are right on the same 313 and wrong on the same 2 questions."
)


def _ci(s: dict[str, Any], method: str, pts: bool = False) -> str:
    f = _pt if pts else (lambda x: f"{x:.2f}")
    return f"[{f(s[method]['lo'])}, {f(s[method]['hi'])}]"


def markdown(res: dict[str, Any]) -> str:
    b = res["b"]
    L: list[str] = [
        "# Jira benchmark: clustered statistics",
        "",
        (
            f"Re-analysis of the result rows (`{', '.join(res['files'])}`), no new LLM calls. B = {b:,}, seed {res['seed']}; "
            "a question's repetitions are averaged before resampling. `iid` = the paper's bootstrap over questions; "
            "`strat` = questions resampled within tenant x type cells; `cluster` = tenant x type cells resampled whole "
            "(one issue: 18 cells, tenant-wide: 15 by sub-type); `fine` = tenant x template cells resampled whole. "
            "Generated by `evals/jira/stats_cluster.py`."
        ),
        "",
        "## 1. Accuracy, 95% CI",
        "",
        "| Agent | System | Scope | reps | n | acc | iid | strat | cluster | fine |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in sorted(
        res["accuracy"], key=lambda a: (a["scope"], AGENT_ORDER.index(a["agent"]), a["system"])
    ):
        exact = f" (exact {a['exact'][0]:.2f}-{a['exact'][1]:.2f})" if "exact" in a else ""
        L.append(
            f"| {a['agent']} | {a['system']} | {a['scope']} | {a['reps']} | {a['n']} | {a['mean']:.2f}{exact} | "
            f"{_ci(a, 'iid')} | {_ci(a, 'stratified')} | {_ci(a, 'cluster')} | {_ci(a, 'cluster_fine')} |"
        )
    L += [
        "",
        "## 1-2. Paired Keel minus X, points, 95% CI, p (raw) and Holm-adjusted p",
        "",
        "Holm families: T = the paper's Tables 6-7 ("
        + str(res["holm"]["tables_6_7"]["m"])
        + " tests); "
        "T+A = those plus the rules and brief ablations ("
        + str(res["holm"]["tables_6_7_and_ablations"]["m"])
        + " tests). `+/-` = questions where Keel is ahead/behind (sign test p in `sign`). Tests outside the paper's "
        "tables (no Holm) are marked `-`.",
        "",
        "| Agent | vs | Scope | n | +/- | diff | iid CI, p | strat CI, p | cluster CI, p | fine CI, p | Holm T (iid/strat/cluster/fine) | Holm T+A (iid/strat/cluster/fine) | sign |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in sorted(
        res["paired"], key=lambda t: (t["scope"], AGENT_ORDER.index(t["agent"]), t["system"])
    ):
        L.append(_paired_row(t, f"{t['agent']} | {t['system']} | {t['scope']}", b))
    L += [
        "",
        "### Ablations (gap = Rules - None, Brief - Full)",
        "",
        "| Agent | Ablation | Scope | n | +/- | gap | iid CI, p | strat CI, p | cluster CI, p | fine CI, p | Holm T (iid/strat/cluster/fine) | Holm T+A (iid/strat/cluster/fine) | sign |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    notes: set[str] = set()
    for t in res["ablations"]:
        L.append(_paired_row(t, f"{t['agent']} | {t['ablation']} | {t['scope']}", b))
        if t["clusters"] < 8:
            notes.add(
                f"{t['ablation']}/{t['scope']}: {t['clusters']} type cells, {t['clusters_fine']} template cells"
            )
    if notes:
        L += [
            "",
            "Too few type cells for `cluster` to mean anything (read `fine` instead): "
            + "; ".join(sorted(notes))
            + ".",
        ]

    L += [
        "",
        "## 3. Held-out tenants (Hyperledger + Spring) against the pilot tenant (MariaDB)",
        "",
        (
            "Accuracy with stratified 95% CI; Keel - API in points with stratified and cluster CI. MariaDB alone has 5-6 "
            "type cells, too few for a cluster bootstrap to mean much; its cluster CI is shown only for completeness."
        ),
        "",
        "| Agent | Scope | Split | Keel | API | RAG | Keel-API (strat CI, p) | Keel-API cluster CI, p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in res["held_out"]:
        if h["split"] == "held_out_minus_mariadb":
            d = h["did_keel_minus_api"]
            L.append(
                f"| {h['agent']} | {h['scope']} | held-out minus MariaDB | | | | gap difference {_pt(d['mean'])} "
                f"[{_pt(d['lo'])}, {_pt(d['hi'])}], p={_p(d['p'], b)} | |"
            )
            continue
        cells = []
        for s in ["keel", "api", "rag"]:
            x = h[s]
            cells.append(f"{_acc(x)} {_ci(x, 'stratified')} (n={x['n']})" if x else "-")
        d = h["keel_minus_api"]
        L.append(
            f"| {h['agent']} | {h['scope']} | {h['split']} | "
            + " | ".join(cells)
            + f" | {_pt(d['mean'])} {_ci(d, 'stratified', True)}, p={_p(d['stratified']['p'], b)} | "
            f"{_ci(d, 'cluster', True)}, p={_p(d['cluster']['p'], b)} |"
        )

    L += [
        "",
        (
            "## 4. Tenant-wide accuracy by sub-type (questions per agent: status_at 18, count_at 12, changed 12, "
            "known_at 12, rule_at 18)"
        ),
        "",
        "API-expressible = status_at, count_at, changed (JQL `WAS`/`CHANGED`); not = known_at, rule_at.",
        "",
        "| Agent | System | status_at | count_at | changed | known_at | rule_at | API-expressible (cluster CI) | not (cluster CI) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(res["subtypes"], key=lambda r: (AGENT_ORDER.index(r["agent"]), r["system"])):
        cells = [
            f"{r[st]['mean']:.2f}" + ("" if r[st]["n"] in (18, 12) else f" (n={r[st]['n']})")
            if r[st]
            else "-"
            for st in SUBTYPES
        ]
        g = [
            f"{r[k]['mean']:.2f} {_ci(r[k], 'cluster')} (n={r[k]['n']})" if r[k] else "-"
            for k in ["api_expressible", "not_api_expressible"]
        ]
        L.append(
            f"| {r['agent']} | {r['system']} | " + " | ".join(cells) + " | " + " | ".join(g) + " |"
        )
    L += [
        "",
        "Keel - API by group, points:",
        "",
        "| Agent | Group | n | +/- | diff | strat CI, p | cluster CI, p | fine CI, p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for g in res["api_groups"]:
        L.append(
            f"| {g['agent']} | {g['group']} | {g['n']} | {g['pos']}/{g['neg']} | {_pt(g['mean'])} | "
            f"{_ci(g, 'stratified', True)}, {_p(g['stratified']['p'], b)} | {_ci(g, 'cluster', True)}, "
            f"{_p(g['cluster']['p'], b)} | {_ci(g, 'cluster_fine', True)}, {_p(g['cluster_fine']['p'], b)} |"
        )

    L += ["", "## 5. Degenerate cases", "", DEGENERATE, ""]
    L += ["", "## 6. Luna, tenant-wide, before and after the CDN-405 rerun", ""]
    for system, r6 in res["rerun_405"].items():
        L.append(
            f"- {system}: `full-luna.discarded-405.jsonl` holds {r6['discarded_rows']} rows "
            f"({', '.join(r6['rerun_ids'])}), {r6['discarded_correct']} correct (gateway HTML 405, no answer); "
            f"their reruns in `full-luna.jsonl`: {r6['rerun_rows']} rows, {r6['rerun_correct']} correct. "
            f"Accuracy, repetitions averaged: before {r6['accuracy_before']:.4f}, after {r6['accuracy_after']:.4f}."
        )
    return "\n".join(L) + "\n"


def _paired_row(t: dict[str, Any], head: str, b: int) -> str:
    def holm_cell(fam: str) -> str:
        if "holm" not in t["iid"] or fam not in t["iid"]["holm"]:
            return "-"
        return " / ".join(_p(t[m]["holm"][fam], b) for m in METHODS)

    cis = " | ".join(f"{_ci(t, m, True)}, {_p(t[m]['p'], b)}" for m in METHODS)
    return (
        f"| {head} | {t['n']} | {t['pos']}/{t['neg']} | {_pt(t['mean'])} | {cis} | {holm_cell('tables_6_7')} | "
        f"{holm_cell('tables_6_7_and_ablations')} | {t['sign_p']:.3g} |"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--data",
        type=Path,
        default=Path.home() / "datasets/public-jira/export",
        help="directory with <tenant>.questions.json",
    )
    ap.add_argument("--b", type=int, default=B)
    args = ap.parse_args()
    res = run(args.data, args.b)
    (RESULTS / "stats-cluster.json").write_text(json.dumps(res, indent=1) + "\n")
    (RESULTS / "stats-cluster.md").write_text(markdown(res))
    print(markdown(res))


if __name__ == "__main__":
    main()
