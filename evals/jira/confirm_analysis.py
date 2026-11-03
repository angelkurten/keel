"""The confirmation set's analysis, fixed before any run (evals/jira/confirm/README.md). No LLM call, 0 USD.

    uv run --with numpy python -m evals.jira.confirm_analysis evals/jira/results/confirm-haiku.jsonl [--out ...]

Three hypotheses, each a paired difference over the questions both conditions answered with a 95% interval from the
held-out set's cluster bootstrap (tenant x type cells resampled whole), B = 10,000, seed 0: H1 sqlgen2 - sql and
H2 keel+query - sql are confirmed when the interval's lower bound is above -2 points (not worse than hand-written
SQL), H3 keel+query - keel when it is above 0. No multiplicity adjustment. Secondary, descriptive: cost per correct
answer and input tokens per run, overall and by scope, as the held-out analysis reports them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.jira import heldout_analysis as ha
from evals.jira.stats_cluster import AGENTS, SEED, Analysis, B, per_question

HERE = Path(__file__).parent / "confirm"
AGENT = "Haiku"
HYPOTHESES = [
    {"name": "H1", "a": "sqlgen2", "b": "sql", "margin": -0.02, "claim": "generated SQL is not worse than hand-written SQL"},
    {"name": "H2", "a": "keel+query", "b": "sql", "margin": -0.02, "claim": "Keel with query is not worse than hand-written SQL"},
    {"name": "H3", "a": "keel+query", "b": "keel", "margin": 0.0, "claim": "query adds to Keel's five tools"},
]


def analyze(qs: dict[str, dict[str, Any]], rows: list[dict[str, Any]], b: int = B, seed: int = SEED,
            pairs: list[tuple[str, str]] = (), agent: str = AGENT) -> dict[str, Any]:
    res = ha.analyze(qs, rows, b, seed)  # per-condition accuracy, cost and tokens, as for the held-out set
    an = Analysis(qs, b, seed)
    acc = {s: per_question([r for r in rows if r["id"] in qs and r["system"] == s and AGENTS.get(r["model"], r["model"]) == agent])
           for s in {r["system"] for r in rows}}
    hyps = []
    for h in HYPOTHESES:
        if not acc.get(h["a"]) or not acc.get(h["b"]):
            continue
        p = an.paired(acc[h["a"]], acc[h["b"]])
        if p is None:
            continue
        cl = p["cluster"]
        hyps.append(h | {"n": p["n"], "diff": p["mean"], "lo": cl["lo"], "hi": cl["hi"], "pos": p["pos"], "neg": p["neg"],
                         "confirmed": cl["lo"] > h["margin"]})
    extra = []  # exploratory differences (--pairs): the same statistics, no decision rule
    for a, b_ in pairs:
        p = an.paired(acc[a], acc[b_]) if acc.get(a) and acc.get(b_) else None
        if p is not None:
            extra.append({"a": a, "b": b_, "n": p["n"], "diff": p["mean"], "lo": p["cluster"]["lo"], "hi": p["cluster"]["hi"],
                          "pos": p["pos"], "neg": p["neg"]})
    return res | {"hypotheses": hyps, "pairs": extra}


def markdown(res: dict[str, Any]) -> str:
    text = ha.markdown(res | {"comparisons": []}).replace("# Held-out set: results", "# Confirmation set: results")
    text = text.split("\n## Planned comparisons")[0].rstrip() + "\n"
    out = ["", "## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)", "",
           "| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |",
           "| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |"]
    for h in res["hypotheses"]:
        out.append(f"| {h['name']} | {h['claim']} | {h['a']} - {h['b']} | {h['n']} | {ha._pt(h['diff'])} | "
                   f"[{ha._pt(h['lo'])}, {ha._pt(h['hi'])}] | > {ha._pt(h['margin'])} | {h['pos']} / {h['neg']} | "
                   f"{'confirmed' if h['confirmed'] else 'not confirmed'} |")
    if res.get("pairs"):
        out += ["", "## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)", "",
                "| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |", "| --- | ---: | ---: | --- | --- |"]
        out += [f"| {x['a']} - {x['b']} | {x['n']} | {ha._pt(x['diff'])} | [{ha._pt(x['lo'])}, {ha._pt(x['hi'])}] | {x['pos']} / {x['neg']} |"
                for x in res["pairs"]]
    return text + "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("--questions", type=Path, default=HERE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--agent", default=AGENT, help="whose rows the hypotheses and pairs read (default Haiku, as registered)")
    ap.add_argument("--pairs", default="", help="exploratory differences to add, as a:b,a:b (e.g. sql-log:sql)")
    a = ap.parse_args()
    manifest = json.loads((a.questions / "manifest.json").read_text())
    qs = {q["id"]: q for t in manifest["tenants"] for q in json.loads((a.questions / f"{t}.questions.json").read_text())}
    rows = [json.loads(line) for p in a.results for line in p.open()]
    pairs = [tuple(x.split(":")) for x in a.pairs.split(",") if x]
    text = markdown(analyze(qs, rows, pairs=pairs, agent=a.agent))
    print(text)
    if a.out:
        a.out.write_text(text)


if __name__ == "__main__":
    main()
