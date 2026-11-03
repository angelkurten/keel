# Reproducing the paper

The single entry point for reproducing *Do LLM Agents Need a Compiled Domain?* (`main.tex`, `supplement.tex`).
It says where every table and figure of `main.tex` comes from, how to recompute each number from the committed
result rows (free, no model call), how to rerun each paid stage, and which commit registered each stage.

Tables and figures are named by their `\label` and the start of their caption, not by number.

## 1. Setup

**Requirements.**

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/); `uv sync --extra dev` at the repository root.
- DuckDB 2.0.0 for every `sql*` condition and the gold checks: pass `--with duckdb==2.0.0` to `uv run`, as the
  commands below do.
- [`tectonic`](https://tectonic-typesetting.github.io/) for the paper, the supplement and the two TikZ diagrams.
  On macOS, `qlmanage` (built in) makes the diagrams' PNG twins; it is optional.
- The Public Jira Dataset (Montgomery, Lüders and Maalej, MSR 2022, Zenodo, CC BY 4.0), unzipped to
  `~/datasets/public-jira/ThePublicJiraDataset/`, and Docker for the MongoDB restore. The event logs, injections
  and questions the pipeline writes go to:
  - `~/datasets/public-jira/export/`: the development set (hyperledger, mariadb, spring), seed 7. Its question files
    are not committed; `evals/jira/README.md` §1 generates them.
  - `~/datasets/public-jira/heldout/`: the held-out set (ecosystem-ac, sakai-grbk, redhat-projquay), seed 31, and
    every project export screened for the confirmation set.
  - `~/datasets/public-jira/confirm/`: the confirmation set (apache-tajo, apache-rave), seed 47.

  The held-out, confirmation and hard-slice questions are committed (`evals/jira/{heldout,confirm,hard}/`), each
  directory with a `manifest.json` of SHA-256 sums, so the analyses in §2 need no dataset except for the
  development set's question files.

**Keys** (only for the paid runs of §3; nothing in §2 calls a model). Never commit or paste a key.

- Anthropic: `ANTHROPIC_API_KEY`, or the file `~/.anthropic_key` (`evals/efficiency/run.py`, `anthropic_key()`).
  With either set, Claude runs go straight to Anthropic; `EVAL_ANTHROPIC_DIRECT=0` sends them through the gateway.
- `OFOX_API_KEY` (OfoxAI gateway) is required even when every call goes straight to Anthropic: the harness's
  `Gateway()` constructor (`evals/efficiency/run.py`) reads `os.environ["OFOX_API_KEY"]` and fetches the gateway's
  model list before any question, so a missing or invalid key stops the run.

**Shell.** Every command here is bash. The stage READMEs store the bench command in `$B` and run `$B --systems ...`;
zsh does not split an unquoted `$B` into words, so under zsh it fails with `command not found`. Run them with
`bash`, from the repository root. A fresh worktree's environment also lacked `httpx` during the hard slice's
pilot; add `--with httpx` to the bench command if it is missing.

## 2. Tables and figures of `main.tex`

`R=evals/jira/results`. Every analysis below is free (no model call). Each `--out` writes a file that is committed;
point it at a scratch path to compare with `diff`. All of these were rerun for this file and give the committed
outputs byte for byte, or the paper's numbers where there is no committed output.

| Label | Caption starts | Result file(s) | How to regenerate |
| --- | --- | --- | --- |
| `fig:1` | Keel (top): product events through cartridge handlers ... | none (diagram) | TikZ: `figures/fig1-architecture.tex`, built by `figures/build.sh` (`tectonic fig1-architecture.tex`) |
| `fig:2` | One field's versions in valid time (across) and recorded time (up) ... | none (diagram) | TikZ: `figures/fig2-bitemporal.tex`, built by `figures/build.sh` |
| `tab:questions` | Question types, with the number per tenant ... | `evals/jira/{heldout,confirm}/*.questions.json`, the development set's in `~/datasets/public-jira/export/` | Templates and counts in `evals/jira/questions.py`; per-type counts in each `manifest.json` (`counts`, `tenants.*.by_type`) |
| `tab:sets` | The three question sets. Tenants are whole projects ... | `evals/jira/{heldout,confirm}/manifest.json` | Issue counts and seeds from the manifests; development issues are the `issue.created` events of `~/datasets/public-jira/export/<tenant>.jsonl` (2,323, 3,333, 3,150) |
| `tab:conditions` | Conditions. The `sql*` conditions run DuckDB 2.0.0 ... | none (design) | `evals/jira/bench.py` (module docstring and `SQL_SYSTEMS`), `evals/jira/sql_system.py`, `evals/jira/sqlgen_system.py` |
| `tab:deviations` | Deviations from the registrations, in order ... | none (record) | The amendment, correction and result sections of `evals/jira/{heldout,confirm,hard}/README.md`; commits in §4 |
| `tab:heldout` | Held-out set, Haiku, 387 questions, three repetitions ... | `heldout-haiku.jsonl` (registered), `heldout-haiku-sqlgen-tzfix.jsonl`, `heldout-haiku-sqlgen2.jsonl`, `heldout-haiku-keel-query.jsonl` | Snippet A: registered rows (`heldout-stats.md`), zone-fixed `sqlgen` (`heldout-stats-tzfix.md`), `sqlgen2` (`heldout-sqlgen2-stats.md`), `keel+query` (printed) |
| `tab:planned` | Held-out set, the five registered comparisons ... | as `tab:heldout` | Snippet A: the registered rows from `heldout-stats.md`, the zone-fixed rows from `heldout-stats-tzfix.md` |
| `fig:hyp` | The three hypotheses on each set: paired difference ... | `figures/hypotheses.json`, from the rows of `tab:sets-acc` | Snippet B prints every value of `hypotheses.json`; then `cd docs/paper-pvldb/figures && uv run --with matplotlib python fig7-hypotheses.py` |
| `tab:hyp` | Confirmation set (258 questions, Haiku, three repetitions) ... | `confirm-haiku.jsonl` (the first run, not the clean rerun) | `uv run --with numpy python -m evals.jira.confirm_analysis $R/confirm-haiku.jsonl --out $R/confirm-stats.md`. Clean rerun (text of `sec:confirm`): the same on `confirm-haiku-clean.jsonl`, out `confirm-clean-stats.md`. Sensitivity paragraph: snippet C |
| `tab:sets-acc` | Accuracy (%) by condition and set, Haiku, three repetitions ... | dev: `full-haiku.jsonl` (`keel`), `full-sql.jsonl` (Haiku `sql`), `dev-haiku-query-sqlgen2.jsonl`; held-out: `heldout-haiku.jsonl`, `heldout-haiku-sqlgen2.jsonl`, `heldout-haiku-keel-query.jsonl`; `confirm-haiku.jsonl`; `hard-haiku.jsonl` | Snippet B |
| `tab:hard` | Hard slice (320 questions, 64 per type, Haiku, three repetitions) ... | `hard-haiku.jsonl`, `hard-ablation-haiku.jsonl`; "Remedy" rows: `hard-days-haiku.jsonl`; "Sonnet" rows: `hard-sonnet.jsonl` | Overall accuracy, USD per correct and tokens: `uv run --with numpy python -m evals.jira.confirm_analysis $R/hard-haiku.jsonl $R/hard-ablation-haiku.jsonl --questions evals/jira/hard --pairs sql-tables:sql,sql-log:sql,sqlgen2:sql-log,keel+query:sql-log,keel:sql-log --out $R/hard-ablation-stats.md` (the registered part alone: `confirm_analysis $R/hard-haiku.jsonl --questions evals/jira/hard --out $R/hard-stats.md`). Per-type rows and the cost rows as printed: snippet D. "Remedy" rows (`time_in` and all): the third addendum's command, first in snippet J, which writes `hard-days-stats.md` (its `time_in` and `all` tables, rows `<system>+days`). "Sonnet" rows: `uv run --with numpy python -m evals.jira.confirm_analysis $R/hard-sonnet.jsonl --questions evals/jira/hard --agent Sonnet --pairs sqlgen2:sql-log,keel+query:sql-log,sql-log:sql --out $R/hard-sonnet-stats.md`; as printed (USD per correct ×10⁻⁴, tokens in k): snippet I |
| `tab:gov` | Governed memory, development set ... | `c4.jsonl`, `c4-subject.jsonl` (cited, after ea9b30a), `c4-same-task.jsonl` | `for f in c4 c4-subject c4-same-task; do uv run --extra mcp --with numpy --with tiktoken python -m evals.jira.c4 report $R/$f.jsonl; done` (`evals/jira/README.md` writes `uv run python -m evals.jira.c4 report`, which fails without numpy) |

Numbers in the text that no table carries:

| Where | Number | How |
| --- | --- | --- |
| `sec:hard` | by tenant group, `sqlgen2 − sql` −0.8 / −1.7 / −0.8, `keel+query − sql` −1.1 / −1.9 / −1.7 | Snippet D (last three lines) |
| `sec:hard` | a date function in 131 / 25 / 8 of 192 `time_in` runs (`sql`, `sqlgen2`, `keel+query`), misses 4 / 21 / 26, one miss among the runs that use one | Snippet E |
| `sec:hard` | of the compiled interfaces' 47 `time_in` misses, only 2 match flooring each spell | Snippet F |
| `sec:hard`, "Testing the explanation"; abstract | `time_in` 89.1 → 98.4 (`sqlgen2`) and 86.5 → 99.0 (`keel+query`), paired +9.4 [+5.2, +13.5] and +12.5 [+8.3, +16.7]; overall 96.2 → 98.2 and 95.8 → 98.6, +2.0 [+0.5, +3.5] and +2.8 [+1.1, +4.8]; 6 to 7% more input tokens; against hand-written SQL +0.8 [−0.2, +2.0] and +1.2 [+0.2, +2.5] | Snippet J: the first command writes `hard-days-stats.md` (the paired differences against the same condition); the second prints the comparison with `sql`, the token increase and the cost per correct (0.00095 and 0.00067) |
| `sec:hard`, "A stronger agent"; `sec:lessons`; abstract | Sonnet 100 / 99.4 / 99.1 / 99.1 (`sql`, `sql-log`, `keel+query`, `sqlgen2`), 8 misses; every paired difference under 1 point, decided by at most 3 questions; raw log 7.5k input tokens a run against Haiku's 20.1k; `sql-log` 1.2 to 1.4 times the other three per correct answer (Haiku 1.7 to 2.5); Sonnet 8 to 15 times Haiku per correct on the same interface; Haiku `sql` against Sonnet `sql-log`, 2 points for 21 times the price | Paired differences and discordant counts: `hard-sonnet-stats.md` (command in the `tab:hard` row). The rest: snippet I |
| `sec:hard`, `sec:lessons` | `sql-log` 2.3 and 1.7 times the cost per correct answer of `keel+query` and `sqlgen2` | 15.4 / 6.7 and 15.4 / 9.3 from snippet D |
| `sec:confirm` | clean rerun H3 lower bound 4 × 10⁻¹⁹; Holm / Bonferroni bounds −0.4 and 0.0; sign test p = 0.039; 10 questions every condition misses, all `links` | Snippet C |
| `sec:heldout`, `sec:res-cost` | API 3.8 calls a run, Keel 1.6; 2.2 to 2.6 turns a run | Snippet G |
| `sec:posthoc` | development set: 1,534 `query` calls, 281 failed | Snippet G. The split into 250 timeouts (185 during the sandbox copy) and 31 SQL errors (17 schema prefixes) is not recomputable: rows record only `error: true` per call, not the error text |
| `sec:repro` | costs per stage; the hard slice 21.0 USD (3.3 + 2.3 + 13.9 + 1.5); 32.2 USD in all | Snippet H |
| `sec:determinism` | real-log and synthetic determinism, ingest rates, read latencies | `evals/scale/README.md` ("Running it"), results in `evals/scale/results/scale.jsonl` and `summary.md` (`uv run python -m evals.scale.harness summary`) |
| `sec:firstrun` | the development set's first run (accuracies, CIs, Holm) | `uv run --with numpy python -m evals.jira.stats_cluster` (writes `results/stats-cluster.{md,json}`; needs the development questions in `~/datasets/public-jira/export`); supplement §A to §E |

The supplement's figures (`fig3-tenant-accuracy` to `fig6-poisoned`) are built by the same `figures/build.sh`;
fig3 and fig5 read `figures/tenant-ci.json`, fig4 and fig6 carry their numbers in the script. Their docstrings cite
`full-run-results.md` and `c4-results.md`, which are not in this repository; the numbers are those of
`stats-cluster.md` and of the `c4 report` above.

### Snippets

Run each from the repository root under bash. Each was run for this file and prints the paper's numbers.

**A. Held-out table variants** (`tab:heldout`, `tab:planned`). The zone-fixed and `sqlgen2` outputs put the fixed
`sqlgen` arm in place of the registered one:

```bash
R=evals/jira/results; T=$(mktemp -d)
uv run --with numpy python -m evals.jira.heldout_analysis $R/heldout-haiku.jsonl --out $T/heldout-stats.md
diff $T/heldout-stats.md $R/heldout-stats.md && echo registered: same
python3 -I -c "import json,sys
for l in open('$R/heldout-haiku.jsonl'):
    if json.loads(l)['system'] != 'sqlgen': sys.stdout.write(l)" > $T/no-sqlgen.jsonl
uv run --with numpy python -m evals.jira.heldout_analysis $T/no-sqlgen.jsonl $R/heldout-haiku-sqlgen-tzfix.jsonl --out $T/tz.md
diff $T/tz.md $R/heldout-stats-tzfix.md && echo zone-fixed: same
uv run --with numpy python -m evals.jira.heldout_analysis $T/no-sqlgen.jsonl $R/heldout-haiku-sqlgen-tzfix.jsonl \
    $R/heldout-haiku-sqlgen2.jsonl --out $T/g2.md
diff $T/g2.md $R/heldout-sqlgen2-stats.md && echo sqlgen2: same
uv run --with numpy python -m evals.jira.heldout_analysis $R/heldout-haiku.jsonl $R/heldout-haiku-keel-query.jsonl \
    --out $T/kq.md | grep 'keel+query'
```

**B. Accuracy by set and the three hypotheses on each** (`tab:sets-acc`, `figures/hypotheses.json`):

```bash
uv run -q --with numpy python - <<'PY'
import json
from pathlib import Path
from evals.jira.confirm_analysis import analyze
R, J = Path("evals/jira/results"), Path("evals/jira")
def rows(*fs, keep=lambda r: True):
    return [r for f in fs for r in map(json.loads, open(R / f)) if keep(r)]
def qs(d):
    return {q["id"]: q for p in sorted(Path(d).expanduser().glob("*.questions.json")) for q in json.loads(p.read_text())}
haiku = lambda r: "haiku" in r["model"]
sets = {
    "dev": (qs("~/datasets/public-jira/export"),
            rows("full-haiku.jsonl", keep=lambda r: r["system"] == "keel") + rows("full-sql.jsonl", keep=haiku)
            + rows("dev-haiku-query-sqlgen2.jsonl")),
    "heldout": (qs(J / "heldout"), rows("heldout-haiku.jsonl", "heldout-haiku-sqlgen2.jsonl", "heldout-haiku-keel-query.jsonl")),
    "confirm": (qs(J / "confirm"), rows("confirm-haiku.jsonl")),
    "hard": (qs(J / "hard"), rows("hard-haiku.jsonl")),
}
for name, (q, rs) in sets.items():
    res = analyze(q, rs)
    acc = {s: round(100 * v["accuracy"], 1) for (_, s), v in res["systems"].items() if s in ("keel", "keel+query", "sql", "sqlgen2")}
    print(name, len(q), acc)
    for h in res["hypotheses"]:
        print("  ", h["name"], f"{100*h['diff']:+.1f} [{100*h['lo']:+.1f}, {100*h['hi']:+.1f}]", f"{h['pos']} / {h['neg']}")
PY
```

**C. Confirmation set: sensitivity, the clean rerun's H3 bound, the links questions** (`sec:confirm`):

```bash
uv run -q --with numpy python - <<'PY'
import collections, json
from math import comb
from pathlib import Path
import numpy as np
from evals.jira.confirm_analysis import analyze
from evals.jira.stats_cluster import cell, cluster_dist, per_question
qs = {q["id"]: q for p in Path("evals/jira/confirm").glob("*.questions.json") for q in json.loads(p.read_text())}
rows = [json.loads(l) for l in open("evals/jira/results/confirm-haiku.jsonl")]
acc = {s: per_question([r for r in rows if r["system"] == s]) for s in ("keel", "keel+query", "sql", "sqlgen2")}
for h, a, b in (("H1", "sqlgen2", "sql"), ("H2", "keel+query", "sql"), ("H3", "keel+query", "keel")):
    ids = sorted(acc[a].keys() & acc[b].keys())
    dist = cluster_dist([acc[a][q] - acc[b][q] for q in ids], [cell(qs[q]) for q in ids])
    print(h, "lower bound at 95%%: %+.1f, 97.5%%: %+.1f, 98.3%% (Bonferroni over 3): %+.1f"
          % tuple(100 * np.percentile(dist, 100 * (1 - lvl) / 2) for lvl in (0.95, 1 - 0.05 / 2, 1 - 0.05 / 3)))
print("H3 exact sign test, 10 / 2: p = %.3f" % (2 * sum(comb(12, i) for i in range(3)) / 2**12))
clean = analyze(qs, [json.loads(l) for l in open("evals/jira/results/confirm-haiku-clean.jsonl")])
print("clean rerun, H3 lower bound:", [h["lo"] for h in clean["hypotheses"] if h["name"] == "H3"][0])
wrong, kind = collections.defaultdict(collections.Counter), {}
for r in rows:
    wrong[r["id"]][r["system"]] += not r["correct"]; kind[r["id"]] = r["type"]
every = [q for q, c in wrong.items() if all(c[s] >= 2 for s in acc)]
print("missed by all four in 2 of 3 reps:", len(every), dict(collections.Counter(kind[q] for q in every)))
PY
```

**D. Hard slice by type, cost, tokens and tenant group** (`tab:hard`, `sec:hard`):

```bash
python3 -I - <<'PY'
import collections, json
rows = [json.loads(l) for f in ("hard-haiku", "hard-ablation-haiku") for l in open(f"evals/jira/results/{f}.jsonl")]
S = ["keel", "keel+query", "sql", "sqlgen2", "sql-tables", "sql-log"]
def acc(rs):  # a question's repetitions averaged first
    q = collections.defaultdict(list)
    for r in rs: q[r["id"]].append(bool(r["correct"]))
    return 100 * sum(sum(v) / len(v) for v in q.values()) / len(q)
by = {s: [r for r in rows if r["system"] == s] for s in S}
print(f"{'':13}", *(f"{s:>10}" for s in S))
for ty in ("two_hop", "known_count", "entries", "time_in", "both"):
    print(f"{ty:13}", *(f"{acc([r for r in by[s] if r['type'] == ty]):10.1f}" for s in S))
print(f"{'All':13}", *(f"{acc(by[s]):10.1f}" for s in S))
print(f"{'USD/corr e-4':13}", *(f"{1e4 * sum(r['cost_usd'] for r in by[s]) / sum(r['correct'] for r in by[s]):10.1f}" for s in S))
print(f"{'tokens (k)':13}", *(f"{sum(r['tokens']['input'] for r in by[s]) / len(by[s]) / 1e3:10.1f}" for s in S))
grp = {"hyperledger": "dev", "mariadb": "dev", "spring": "dev", "ecosystem-ac": "heldout", "sakai-grbk": "heldout",
       "redhat-projquay": "heldout", "apache-tajo": "confirm", "apache-rave": "confirm"}
for g in ("dev", "heldout", "confirm"):
    a = {s: acc([r for r in by[s] if grp[r["tenant"]] == g]) for s in S}
    print(g, "sqlgen2 - sql %+.1f" % (a["sqlgen2"] - a["sql"]), " keel+query - sql %+.1f" % (a["keel+query"] - a["sql"]))
PY
```

**E. Date functions in the agents' `time_in` SQL** (`sec:hard`):

```bash
python3 -I - <<'PY'
import json, re
rows = [r for r in map(json.loads, open("evals/jira/results/hard-haiku.jsonl")) if r["type"] == "time_in"]
date_fn = re.compile(r"date_diff|datediff|julianday|epoch", re.I)  # DuckDB and SQLite date arithmetic
for s in ("sql", "sqlgen2", "keel+query"):
    rs = [r for r in rows if r["system"] == s]
    use = [r for r in rs if any(date_fn.search(json.dumps(c.get("args", {}))) for c in r["calls"])]
    print(f"{s:11} runs {len(rs)}  with a date function {len(use):3}  missed {sum(not r['correct'] for r in rs):2}"
          f"  missed among those {sum(not r['correct'] for r in use)}")
PY
```

**F. The compiled interfaces' `time_in` misses that match flooring each spell** (`sec:hard`; reads the bitemporal logs
under `~/datasets/public-jira` through the oracle):

```bash
uv run -q python - <<'PY'
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from evals.jira.hard import timeline
from evals.jira.questions import Oracle
D = Path.home() / "datasets/public-jira"
home = {"hyperledger": "export", "mariadb": "export", "spring": "export", "ecosystem-ac": "heldout",
        "sakai-grbk": "heldout", "redhat-projquay": "heldout", "apache-tajo": "confirm", "apache-rave": "confirm"}
qs = {q["id"]: q for p in Path("evals/jira/hard").glob("*.questions.json") for q in json.loads(p.read_text())}
miss = [r for r in map(json.loads, open("evals/jira/results/hard-haiku.jsonl"))
        if r["type"] == "time_in" and r["system"] in ("sqlgen2", "keel+query") and not r["correct"]]
oracles, floor = {}, 0
for r in miss:
    p = qs[r["id"]]["params"]
    if r["tenant"] not in oracles:
        oracles[r["tenant"]] = Oracle([json.loads(l) for l in open(D / home[r["tenant"]] / f"{r['tenant']}.bitemporal.jsonl")])
    line = timeline(oracles[r["tenant"]], p["subject"])
    at = datetime.strptime(p["as_of"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    ends = [s for s, _ in line[1:]] + [at]
    floored = sum((min(e, at) - s) // timedelta(days=1) for (s, v), e in zip(line, ends) if v == p["status"] and s < at)
    floor += int(r["answer"]) == floored
print("misses", len(miss), " matching the floor of each spell, summed", floor)
PY
```

**G. Calls and turns per run (held-out), `query` calls on the development set:**

```bash
python3 -I - <<'PY'
import json
R = "evals/jira/results/"
rs = [json.loads(l) for f in ("heldout-haiku", "heldout-haiku-sqlgen2", "heldout-haiku-keel-query") for l in open(R + f + ".jsonl")]
for s in ("api", "keel", "sql", "sql-tables", "sqlgen", "sqlgen2", "keel+query"):
    x = [r for r in rs if r["system"] == s]
    print(f"{s:11} calls/run {sum(len(r['calls']) for r in x) / len(x):.1f}  turns/run {sum(r['turns'] for r in x) / len(x):.1f}")
q = [c for l in open(R + "dev-haiku-query-sqlgen2.jsonl") for r in [json.loads(l)] if r["system"] == "keel+query"
     for c in r["calls"] if c["tool"] == "query"]
print("development set: query calls", len(q), "failed", sum(bool(c.get("error")) for c in q))
PY
```

**H. Spend per stage, and the commit every row carries** (`sec:repro`, §4):

```bash
python3 -I - <<'PY'
import json
R = "evals/jira/results/"
stages = {"held-out (registered, zone fix, sqlgen2, keel+query)": ["heldout-pilot", "heldout-haiku", "heldout-haiku-sqlgen-tzfix",
          "heldout-haiku-sqlgen2", "heldout-haiku-keel-query"],
          "development keel+query, sqlgen2 (gateway)": ["dev-haiku-query-sqlgen2"],
          "confirmation (pilot, 82 rows set aside, run)": ["confirm-pilot", "confirm-haiku.discarded-gateway", "confirm-haiku"],
          "confirmation, clean rerun": ["confirm-haiku-clean"],
          "hard slice, registered": ["hard-pilot", "hard-haiku"], "hard slice, addendum": ["hard-ablation-pilot", "hard-ablation-haiku"],
          "hard slice, second addendum (Sonnet)": ["hard-sonnet-pilot", "hard-sonnet"],
          "hard slice, third addendum (remedy)": ["hard-days-haiku"]}
total = 0
for stage, files in stages.items():
    usd = 0
    for f in files:
        rs = [json.loads(l) for l in open(R + f + ".jsonl")]
        usd += sum(r["cost_usd"] for r in rs)
        print(f"  {f:34} {len(rs):5} runs  {sum(r['cost_usd'] for r in rs):.3f} USD  commit/dirty",
              sorted({(r["versions"]["commit"][:7], r["versions"]["dirty"]) for r in rs}))
    total += usd
    print(f"{stage}: {usd:.2f} USD")
print(f"all: {total:.2f} USD")
PY
```

It prints 3.27, 2.30, 13.92 and 1.53 USD for the four hard-slice stages (21.0 in the paper) and 32.20 in all.

**I. Sonnet against Haiku on the hard slice** (`tab:hard` Sonnet rows, `sec:hard` "A stronger agent", `sec:lessons`):

```bash
python3 -I - <<'PY'
import collections, json
R = "evals/jira/results/"
def load(*fs):
    return [json.loads(l) for f in fs for l in open(R + f + ".jsonl")]
def acc(rs):  # a question's repetitions averaged first
    q = collections.defaultdict(list)
    for r in rs: q[r["id"]].append(bool(r["correct"]))
    return 100 * sum(sum(v) / len(v) for v in q.values()) / len(q)
S = ["sql", "sqlgen2", "keel+query", "sql-log"]
agents = {"Haiku": load("hard-haiku", "hard-ablation-haiku"), "Sonnet": load("hard-sonnet")}
st = {}
for a, rows in agents.items():
    for s in S:
        rs = [r for r in rows if r["system"] == s]
        st[a, s] = (acc(rs), sum(r["cost_usd"] for r in rs) / sum(r["correct"] for r in rs),
                    sum(r["tokens"]["input"] for r in rs) / len(rs) / 1e3)
        print(f"{a:6} {s:10} acc {st[a, s][0]:5.1f}  USD/correct {st[a, s][1]:.5f}  input tokens {st[a, s][2]:.1f}k")
print("Sonnet misses:", sum(not r["correct"] for r in agents["Sonnet"]),
      dict(collections.Counter(r["type"] for r in agents["Sonnet"] if not r["correct"])))
for a in agents:
    x = [st[a, "sql-log"][1] / st[a, s][1] for s in S[:3]]
    print(f"{a}: sql-log per correct / the other three: {min(x):.2f} to {max(x):.2f}")
x = [st["Sonnet", s][1] / st["Haiku", s][1] for s in S]
print(f"Sonnet / Haiku per correct, same interface: {min(x):.1f} to {max(x):.1f}", [round(v, 1) for v in x])
print("Haiku sql vs Sonnet sql-log: %+.1f points for %.1f times the price"
      % (st["Sonnet", "sql-log"][0] - st["Haiku", "sql"][0], st["Sonnet", "sql-log"][1] / st["Haiku", "sql"][1]))
PY
```

It prints Sonnet 100.0, 99.1, 99.1 and 99.4 (`sql`, `sqlgen2`, `keel+query`, `sql-log`); 8 misses (entries 5,
time_in 2, two_hop 1); `sql-log` 7.5k input tokens a run against Haiku's 20.1k; `sql-log` per correct over the other
three 1.20 to 1.42 (Sonnet) and 1.65 to 2.47 (Haiku); Sonnet 8.4 to 14.6 times Haiku per correct on the same
interface; and +2.0 points for 20.8 times the price.

**J. The duration remedy** (`tab:hard` Remedy rows, `sec:hard` "Testing the explanation"). The first command is the
third addendum's (`evals/jira/hard/README.md`), checked against the committed output; the second is post hoc: the
remedied rows, relabelled `<system>+days`, against hand-written SQL's registered rows, then the input-token increase
and the cost per correct answer after the remedy:

```bash
R=evals/jira/results; T=$(mktemp -d)
uv run -q --with numpy python -c "
import json; from pathlib import Path; from evals.jira import confirm_analysis as ca
H = Path('evals/jira/hard'); m = json.loads((H / 'manifest.json').read_text())
qs = {q['id']: q for t in m['tenants'] for q in json.loads((H / f'{t}.questions.json').read_text())}
old = [json.loads(l) for l in open('$R/hard-haiku.jsonl')]
new = [json.loads(l) | {'system': json.loads(l)['system'] + '+days'} for l in open('$R/hard-days-haiku.jsonl')]
for typ in (None, 'time_in'):
    q = {k: v for k, v in qs.items() if typ is None or v['type'] == typ}
    print(typ or 'all', ca.markdown(ca.analyze(q, old + new, pairs=[('sqlgen2+days', 'sqlgen2'), ('keel+query+days', 'keel+query')])))
" > $T/days.md
diff $T/days.md $R/hard-days-stats.md && echo remedy: same
uv run -q --with numpy python - <<'PY'
import json
from pathlib import Path
from evals.jira.confirm_analysis import analyze
R = Path("evals/jira/results")
qs = {q["id"]: q for p in Path("evals/jira/hard").glob("*.questions.json") for q in json.loads(p.read_text())}
old = [json.loads(l) for l in open(R / "hard-haiku.jsonl")]
new = [r | {"system": r["system"] + "+days"} for r in map(json.loads, open(R / "hard-days-haiku.jsonl"))]
res = analyze(qs, old + new, pairs=[("sqlgen2+days", "sql"), ("keel+query+days", "sql")])
for x in res["pairs"]:
    print(f"{x['a']} - {x['b']}", f"{100*x['diff']:+.1f} [{100*x['lo']:+.1f}, {100*x['hi']:+.1f}]", f"{x['pos']} / {x['neg']}")
for s in ("sqlgen2", "keel+query"):
    o, n = [r for r in old if r["system"] == s], [r for r in new if r["system"] == s + "+days"]
    tok = lambda rs: sum(r["tokens"]["input"] for r in rs) / len(rs)
    print(f"{s}: input tokens {tok(n) / tok(o) - 1:+.1%}, USD per correct after the remedy "
          f"{sum(r['cost_usd'] for r in n) / sum(r['correct'] for r in n):.5f}")
PY
```

The second prints `sqlgen2+days - sql +0.8 [-0.2, +2.0] 14 / 7` and `keel+query+days - sql +1.2 [+0.2, +2.5] 12 / 3`,
then +6.3% and +7.4% input tokens and 0.00095 and 0.00067 USD per correct answer.

## 3. Rerunning the paid stages

Every paid command is in a stage README, beside its registration; this file does not copy them. Each `bench`
command resumes when rerun with the same `--out`, and stops at its `--max-usd` brake. Costs are measured from the
rows (snippet H), pilots and rows set aside included; Claude Haiku 5.5 and, for the second addendum, Claude Sonnet 5.5,
at Anthropic's list prices of 2026-10-06 (`evals/efficiency/run.py`, `DIRECT_PRICES`).

| Stage | Commands | Out file(s) | Measured cost |
| --- | --- | --- | ---: |
| Held-out, registered | `evals/jira/heldout/README.md`, "Budget and commands" (pilot and run), with "Amendment, 2026-10-26": `--models anthropic/claude-haiku-5.5` only | `heldout-pilot.jsonl`, `heldout-haiku.jsonl` | 0.07 + 3.75 |
| Held-out, zone-fixed `sqlgen` | No block of its own ("Correction after the run"): the run command above with `--systems sqlgen` on 89e8510 or later | `heldout-haiku-sqlgen-tzfix.jsonl` | 0.85 |
| Held-out, `sqlgen2` | same README, "Post hoc: `sqlgen2`" | `heldout-haiku-sqlgen2.jsonl` | 0.54 |
| Held-out, `keel+query` | same README, "Post hoc: `keel+query`" | `heldout-haiku-keel-query.jsonl` | 0.50 |
| Development, `keel+query` and `sqlgen2` | same README, end of "Post hoc: `keel+query`" (`EVAL_ANTHROPIC_DIRECT=0`, gateway prices) | `dev-haiku-query-sqlgen2.jsonl` | 2.52 |
| Confirmation | `evals/jira/confirm/README.md`, "Conditions and agent", with `--tenants apache-tajo,apache-rave` ("Selected") and without `EVAL_ANTHROPIC_DIRECT=0` ("Third amendment") | `confirm-pilot.jsonl`, `confirm-haiku.discarded-gateway.jsonl`, `confirm-haiku.jsonl` | 0.05 + 0.07 + 1.44 |
| Confirmation, clean rerun | same README, "Clean rerun, registered 2026-11-02": the run command from a clean `git worktree` of 98a9621, `--max-usd 2.5`, out `confirm-haiku-clean.jsonl` | `confirm-haiku-clean.jsonl` | 1.40 |
| Hard slice | `evals/jira/hard/README.md`, "Conditions, agent, commands" (pilot and run, from a clean worktree of 552e729) | `hard-pilot.jsonl`, `hard-haiku.jsonl` | 0.15 + 3.12 |
| Hard slice, addendum | same README, "Addendum", with the raised brakes 1.2, 1.2 and 0.8 USD of its pilot paragraph (clean worktree of d5e7201) | `hard-ablation-pilot.jsonl`, `hard-ablation-haiku.jsonl` | 0.11 + 2.18 |
| Hard slice, second addendum (Sonnet) | same README, "Second addendum" (`--models anthropic/claude-sonnet-5.5`; pilot brake 3 USD, then 17, 17 and 11) | `hard-sonnet-pilot.jsonl`, `hard-sonnet.jsonl` | 0.49 + 13.43 |
| Hard slice, third addendum (remedy) | same README, "Third addendum" (`--systems sqlgen2,keel+query`, brakes 1.0, 1.0 and 0.7 USD, clean worktree of a75faee) | `hard-days-haiku.jsonl` | 1.53 |
| **All of the above** | | | **32.20** (paper: 32.2; the hard slice 21.0 = 3.3 + 2.3 + 13.9 + 1.5) |

The development set's first run (GPT-6 Luna, Claude Haiku 5.5, Claude Sonnet 5.5; Graphiti, Mem0, retrieval, the API,
TGMS, governed memory) is in `evals/jira/README.md`, in the order it was run; its spend is in supplement
`app:dev-systems`. Graphiti's ingest alone takes hours.

Before any paid run, the offline checks need no key: `uv run --extra mcp --with tiktoken --with numpy --with
duckdb==2.0.0 pytest evals/jira/test_heldout.py -q` (checksums, byte-for-byte regeneration of the questions, the
gold reached three ways, every condition run with a scripted model), and `evals/jira/test_hard.py` and
`evals/jira/test_confirm.py` the same way for the other two.

## 4. Registration commits

A stage is pre-registered if its registration commit is older than the commit its rows carry (`versions.commit` in
every row; `versions.dirty` says whether `keel/` or `evals/` had uncommitted changes). Check with
`git log --format='%h %ad %s' --date=iso -- evals/jira/<set>/README.md` and snippet H. Keel's own code, `keel/`, is
identical at d7b3576 and bef9b07 (held-out), and at d620fcb (release 0.36.0), 78d3f0d, 98a9621, 552e729, d5e7201,
eaf3951 and fd6353c (`git diff --stat d620fcb <commit> -- keel/` is empty). The one exception is the third addendum's
a75faee: `git diff d620fcb a75faee -- keel/` is one sentence added to `describe()` in `keel/sqlgen.py`, naming
`keel_days` (the remedy that addendum tests); nothing else under `keel/` differs.

| Stage | Registered in | Amendments and later records | Rows carry (dirty) |
| --- | --- | --- | --- |
| Held-out | e7cfeba (2026-10-26 10:27) | bef9b07 Haiku only, before any run; 6b68924 the zone fix recorded as a correction after the run | pilot and `heldout-haiku`: bef9b07 (dirty); zone fix: 89e8510 (dirty) |
| Held-out, post hoc (not registered) | 1cca5aa `sqlgen2`; 1719e41 `keel+query` | 29da32b `keel+query` result; 59eb821 development-set confirmation | `sqlgen2`: 1cca5aa (clean); `keel+query`: 25979b2 (dirty); development: 74e7079 (dirty) |
| Confirmation | b51b725 (2026-11-01 15:40) | 1476765 first amendment; bb03436 its screening passes none; 7721f67 second amendment; 1456094; b538eff tenants selected; 78d3f0d third amendment (Anthropic direct; slipped a clause into rule 5); 3b62a01 result; b53e381 links note; 46eabbf rule 5 restored | pilot and the 82 rows set aside: b538eff (dirty); `confirm-haiku`: 78d3f0d (dirty) |
| Confirmation, clean rerun | 98a9621 (2026-11-02 22:18) | 53d0bc2 result | 98a9621 (clean) |
| Hard slice | 552e729 (2026-11-02 18:42) | 22e8716 result | pilot and run: 552e729 (clean) |
| Hard slice, addendum (exploratory) | 9df0aa3 (2026-11-02 21:35) | d5e7201 brakes raised after its pilot; 5bc7a20 result | pilot: 9df0aa3 (clean); run: d5e7201 (clean) |
| Hard slice, second addendum (Sonnet, exploratory) | eaf3951 (2026-11-02 23:36) | fd6353c pilot recorded; 15ed1f0 result | pilot: eaf3951 (clean); run: fd6353c (clean) |
| Hard slice, third addendum (duration remedy, exploratory, prediction stated) | a75faee (2026-11-03 09:09), with the `keel/sqlgen.py` sentence | 5f60d2b result | a75faee (clean) |

Development-set rows: `full-haiku.jsonl` `keel` on 784630f and 72314eb (clean), `full-sql.jsonl` on e8ae81e (dirty).

## 5. Building the paper

```bash
bash docs/paper-pvldb/figures/build.sh   # optional: the seven figures (matplotlib through uv, TikZ through tectonic)
bash docs/paper-pvldb/build.sh           # main.pdf and supplement.pdf, each compiled twice (xr cross-references)
```

`build.sh` ends by printing the page count and the page where the references start (`pages.py`). `main.pdf`,
`supplement.pdf` and the LaTeX by-products are not tracked (`docs/paper-pvldb/.gitignore`); the figure PDFs under
`figures/` are tracked, their PNG twins are not.
