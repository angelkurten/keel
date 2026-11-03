"""The confirmation set's tenant selection (evals/jira/confirm/README.md): rules 1 to 5 over every candidate project.

    uv run python -m evals.jira.screen ~/datasets/public-jira/heldout ~/datasets/public-jira/confirm \
        --out evals/jira/confirm/screen.json

Each candidate export (<Instance>-<KEY>.json of an instance neither earlier set used) is adapted, checked, injected
and turned into questions by the unmodified pipeline (adapt, check, inject, questions as subprocesses), into the
output directory; the numbers of every candidate go to --out, and the selected three are printed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

INSTANCES = ("IntelDAOS", "JFrog", "Jira", "Mindville", "MongoDB", "Qt", "SecondLife", "Sonatype")  # as registered; none passed
USED = {"JiraEcosystem-AC", "Sakai-GRBK", "RedHat-PROJQUAY"}  # the held-out set's (development's instances were never exported here)
COUNTS = {"current": 20, "as_of": 25, "known_at": 25, "links": 15, "rule": 15, "abstain": 5, "tenant": 24}
URGENT = {"Blocker", "Critical", "Highest"}
SEED = 47


def tenant_id(instance: str, key: str) -> str:
    return f"{instance}-{key}".lower()


def failed_rules(m: dict[str, Any]) -> list[int]:
    """The held-out set's rules 1 to 4, unchanged: which ones a candidate fails."""
    out = []
    if not 900 <= m["issues"] <= 4000:
        out.append(1)
    if m["mismatched"] > 0.01 * m["issues"]:
        out.append(2)
    if not m["urgent_priority"]:
        out.append(3)
    if m["uncategorised"] > 0.01:
        out.append(4)
    return out


def rule_five(by_type: dict[str, int], checks_passed: bool) -> bool:
    """Relaxed rule 5: both checks pass, at least 100 questions, and at least half of each type's count."""
    return checks_passed and sum(by_type.values()) >= 100 and all(2 * by_type.get(k, 0) >= n for k, n in COUNTS.items())


def select(passing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three with the most questions, at most one per instance; ties: fewer mismatches, then more issues. With
    fewer than three instances, filled to three by the same ranking, at most two per instance (second amendment)."""
    ranked = sorted(passing, key=lambda c: (-c["questions"], c["mismatched"], -c["issues"]))
    out: list[dict[str, Any]] = []
    for c in ranked:
        if c["instance"] not in {x["instance"] for x in out}:
            out.append(c)
    for c in ranked:
        if len(out) < 3 and c not in out and sum(x["instance"] == c["instance"] for x in out) < 2:
            out.append(c)
    return out[:3]


def candidates(exports: Path) -> list[Path]:
    """Amendment 2026-11-01: every exported project (<Instance>-<KEY>.json) but the held-out set's three."""
    return sorted(p for p in exports.glob("*-*.json") if re.fullmatch(r"[A-Za-z]+-[A-Z0-9]+", p.stem) and p.stem not in USED)


def run(*args: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", *map(str, args)], check=False, capture_output=True, text=True)


def measure(export: Path, out: Path) -> dict[str, Any]:
    instance, key = export.stem.split("-", 1)
    t = tenant_id(instance, key)
    m: dict[str, Any] = {"instance": instance, "key": key, "tenant": t, "issues": len(json.loads(export.read_text()))}
    log = out / f"{t}.jsonl"
    a = run("evals.jira.adapt", export, t, log, "--projects", key)
    if a.returncode:
        return m | {"error": "adapt: " + a.stderr[-300:]}
    c = run("evals.jira.check", export, log)
    found = re.search(r"mismatches: (\{.*\}|none)", c.stdout)
    m["mismatched"] = sum(json.loads(found.group(1).replace("'", '"')).values()) if found and found.group(1) != "none" else 0 if found else m["issues"]
    events = [json.loads(line) for line in log.open()]
    priorities = {e["payload"].get("priority") for e in events if "priority" in e["payload"]}
    statuses = [e["payload"] for e in events if "status" in e["payload"]]
    m["urgent_priority"] = bool(priorities & URGENT)
    m["uncategorised"] = sum(p.get("status_category") is None for p in statuses) / max(1, len(statuses))
    m["failed"] = failed_rules(m)
    if m["failed"]:
        return m
    bi, inj, qs = out / f"{t}.bitemporal.jsonl", out / f"{t}.injections.json", out / f"{t}.questions.json"
    i = run("evals.jira.inject", log, bi, inj, "--seed", SEED, "--late", 0.05, "--corrections", 0.02, "--check")
    q = run("evals.jira.questions", bi, inj, qs, "--seed", SEED, "--check") if not i.returncode else None
    by = dict(Counter(x["type"] for x in json.loads(qs.read_text()))) if q is not None and qs.exists() else {}
    m |= {"by_type": by, "questions": sum(by.values()), "checks_passed": i.returncode == 0 and q is not None and q.returncode == 0}
    if not rule_five(by, m["checks_passed"]):
        m["failed"] = [5]
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exports", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--out", dest="report", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cands = candidates(a.exports)
    report = []
    for p in cands:
        m = measure(p, a.out)
        report.append(m)
        print(f"{m['tenant']:24} issues {m['issues']:5} failed {m.get('failed', m.get('error'))} questions {m.get('questions', '-')}", flush=True)
    chosen = select([m for m in report if m.get("failed") == []])
    a.report.write_text(json.dumps({"seed": SEED, "candidates": report, "selected": [m["tenant"] for m in chosen]}, indent=1) + "\n")
    print("selected:", [m["tenant"] for m in chosen])


if __name__ == "__main__":
    main()
