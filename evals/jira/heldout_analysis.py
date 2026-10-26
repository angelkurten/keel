"""The held-out set's analysis, fixed before any run (evals/jira/heldout/README.md). No LLM call, 0 USD.

    uv run --with numpy python -m evals.jira.heldout_analysis evals/jira/results/heldout-*.jsonl [--out evals/jira/results/heldout-stats.md]

Primary metric: accuracy per system and agent, a question's repetitions averaged first. Planned comparisons
(PLANNED, ten): per agent, Keel minus each SQL condition and the API, and hand-written minus generated SQL; each
a paired difference over the questions both answered, its 95% interval and two-sided p from a cluster bootstrap
(tenant x type cells resampled whole, tenant-wide questions by sub-type: stats_cluster's `cluster`), Holm-adjusted
over the ten. The bootstrap over questions as independent draws (`iid`) is reported beside it as a sensitivity
check. Secondary: cost per correct answer (USD spent over correct runs) and mean input tokens per run, overall and
by scope (one-issue, tenant-wide). B = 10,000, seed 0.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.jira.stats_cluster import AGENTS, SEED, Analysis, B, holm, per_question

HERE = Path(__file__).parent / "heldout"
SYSTEMS = ["keel", "sql", "sqlgen", "sql-tables", "api"]
PLANNED = [(agent, a, b) for agent in ("Luna", "Haiku")
           for a, b in (("keel", "sql"), ("keel", "sqlgen"), ("keel", "sql-tables"), ("keel", "api"), ("sql", "sqlgen"))]


def scope(r: dict[str, Any]) -> str:
    return "tenant" if r["type"] == "tenant" else "one"


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    acc = per_question(rows)
    right = sum(bool(r["correct"]) for r in rows)
    cost = sum(r["cost_usd"] for r in rows)
    return {"questions": len(acc), "runs": len(rows), "reps": max(sum(1 for r in rows if r["id"] == q) for q in acc),
            "accuracy": sum(acc.values()) / len(acc), "cost_usd": cost, "cost_per_correct": cost / right if right else None,
            "input_tokens": sum(r["tokens"]["input"] for r in rows) / len(rows)}


def analyze(qs: dict[str, dict[str, Any]], rows: list[dict[str, Any]], b: int = B, seed: int = SEED) -> dict[str, Any]:
    an = Analysis(qs, b, seed)
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["id"] in qs:
            by[(AGENTS.get(r["model"], r["model"]), r["system"])].append(r)
    systems = {}
    for key, rs in sorted(by.items()):
        s = summary(rs) | {"by_scope": {sc: summary([r for r in rs if scope(r) == sc]) for sc in ("one", "tenant") if any(scope(r) == sc for r in rs)}}
        s["stats"] = an.stats(per_question(rs))
        systems[key] = s
    comps = []
    for agent, a, b_ in PLANNED:
        if (agent, a) not in by or (agent, b_) not in by:
            continue
        x, y = per_question(by[(agent, a)]), per_question(by[(agent, b_)])
        res = an.paired(x, y)
        if res is None:
            continue
        comps.append({"agent": agent, "a": a, "b": b_, "n": res["n"], "diff": res["mean"], "acc_a": res["keel"], "acc_b": res["other"],
                      "cluster": res["cluster"], "iid": res["iid"], "pos": res["pos"], "neg": res["neg"]})
    for c, p in zip(comps, holm([c["cluster"]["p"] for c in comps]), strict=True):
        c["p_holm"] = p
    return {"systems": systems, "comparisons": comps, "b": b, "seed": seed}


def _pt(x: float) -> str:
    return f"{100 * x:+.1f}"


def markdown(res: dict[str, Any]) -> str:
    out = ["# Held-out set: results", "", f"Bootstrap B = {res['b']}, seed {res['seed']}; a question's repetitions averaged first.", "",
           "## Accuracy, cost per correct answer and input tokens", "",
           "| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |",
           "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for (agent, system), s in res["systems"].items():
        sc = s["by_scope"]
        cl = s["stats"]["cluster"]
        cpc = f"{s['cost_per_correct']:.5f}" if s["cost_per_correct"] is not None else "-"
        out.append(f"| {agent} | {system} | {s['questions']} | {s['reps']} | {100 * s['accuracy']:.1f} | [{100 * cl['lo']:.1f}, {100 * cl['hi']:.1f}] | "
                   + " | ".join(f"{100 * sc[k]['accuracy']:.1f}" if k in sc else "-" for k in ("one", "tenant"))
                   + f" | {s['cost_usd']:.3f} | {cpc} | {s['input_tokens']:.0f} |")
    out += ["", "## Planned comparisons (paired, questions both answered; Holm over the ten)", "",
            "| Agent | A | B | n | A - B (points) | 95% CI (cluster) | p (cluster) | p (Holm) | 95% CI (iid) | A better / B better |",
            "| --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- | --- |"]
    for c in res["comparisons"]:
        cl, iid = c["cluster"], c["iid"]
        out.append(f"| {c['agent']} | {c['a']} | {c['b']} | {c['n']} | {_pt(c['diff'])} | [{_pt(cl['lo'])}, {_pt(cl['hi'])}] | {cl['p']:.4f} | "
                   f"{c['p_holm']:.4f} | [{_pt(iid['lo'])}, {_pt(iid['hi'])}] | {c['pos']} / {c['neg']} |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("--questions", type=Path, default=HERE)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    manifest = json.loads((a.questions / "manifest.json").read_text())
    qs = {q["id"]: q for t in manifest["tenants"] for q in json.loads((a.questions / f"{t}.questions.json").read_text())}
    rows = [json.loads(line) for p in a.results for line in p.open()]
    text = markdown(analyze(qs, rows))
    print(text)
    if a.out:
        a.out.write_text(text)


if __name__ == "__main__":
    main()
