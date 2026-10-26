# Reproducing the Jira benchmark

Everything the paper measures on the Public Jira Dataset, in the order it was run. Every result row in
`results/` records the commit, whether `keel/` or `evals/` had uncommitted changes, and the package versions it
was measured with (`versions`), so each number can be traced to the code that produced it.

## Requirements

- Python 3.10+ and `uv` (`uv sync --extra dev` at the repo root).
- API keys: `OFOX_API_KEY` for the OfoxAI gateway (GPT-6 Luna, embeddings, the memory systems' extractor), and an
  Anthropic key for Claude, in `ANTHROPIC_API_KEY` or `~/.anthropic_key`. Claude runs go straight to Anthropic at its
  default temperature, with thinking off and prompt caching; everything else goes through the gateway at temperature 0.
- Docker, for MongoDB (to restore the dataset) and Neo4j (Graphiti).

## 1. The data

The Public Jira Dataset (Montgomery et al., MSR 2022; Zenodo, CC BY 4.0) ships as a `mongodump`. One whole project per
Jira instance is one tenant:

| Tenant | Instance | Project |
| --- | --- | --- |
| hyperledger | Hyperledger | INDY |
| mariadb | MariaDB | MXS |
| spring | Spring | SEC |

```bash
D=~/datasets/public-jira; E=$D/export
docker run -d --rm --name jira-mongo -v "$D/ThePublicJiraDataset/3. DataDump":/dump:ro mongo:7
docker exec jira-mongo mongorestore --gzip --archive=/dump/mongodump-JiraReposAnon.archive --nsInclude 'JiraReposAnon.Hyperledger'
docker exec jira-mongo mongoexport --db JiraReposAnon --collection Hyperledger --jsonArray \
    --query '{"fields.project.key":"INDY"}' > $E/Hyperledger-INDY.json
# the same for MariaDB/MXS and Spring/SEC
```

Then, per tenant (shown for mariadb), deterministic and seeded:

```bash
uv run python -m evals.jira.adapt $E/MariaDB-MXS.json mariadb $E/mariadb.jsonl --projects MXS   # Jira -> events
uv run python -m evals.jira.check $E/MariaDB-MXS.json $E/mariadb.jsonl --projects MXS            # replay == Jira today
uv run python -m evals.jira.inject $E/mariadb.jsonl $E/mariadb.bitemporal.jsonl $E/mariadb.injections.json \
    --seed 7 --late 0.05 --corrections 0.02 --check                                             # late arrivals, corrections
uv run python -m evals.jira.questions $E/mariadb.bitemporal.jsonl $E/mariadb.injections.json \
    $E/mariadb.questions.json --seed 7 --check                                                  # 129 questions, gold by replay
```

`--check` on `questions` answers every question with Keel's primitives directly, with no agent, against an oracle
that imports nothing from Keel. Every system then reads the same `<tenant>.bitemporal.jsonl`.

## 2. Memory systems' ingest (Graphiti, Mem0)

Both ingest the part of the log the questions touch (4,381 events over the three tenants), with GPT-6 Luna as their
extractor. The ingest can be resumed, and every LLM call's tokens are written to `<tenant>.<system>.<model>.ingest.jsonl`.

```bash
docker run -d --name bench-neo4j -p 7699:7687 -v $D/neo4j:/data -e NEO4J_AUTH=neo4j/benchpass neo4j:5.26
for t in hyperledger mariadb spring; do for s in mem0 graphiti; do
  uv run --extra mcp --with tiktoken --with graphiti-core==0.30.2 --with "mem0ai[extras]==2.2.1" \
      python -m evals.jira.memories ingest --system $s --tenant $t --data $E --model openai/gpt-6-luna
done; done
```

## 3. The full run

`bench` skips runs already in `--out`, so an interrupted command is resumed by running it again. `--max-usd` stops
starting new runs once the invocation has spent that much.

```bash
B="uv run --extra mcp --with tiktoken --with anthropic --with graphiti-core==0.30.2 --with mem0ai==2.2.1 python -m evals.jira.bench --data $E"
ONE=current,as_of,known_at,links,rule,abstain
R=evals/jira/results

# GPT-6 Luna
$B --models openai/gpt-6-luna --systems keel,api --reps 3 --out $R/full-luna.jsonl
$B --models openai/gpt-6-luna --systems rag --types $ONE --reps 3 --out $R/full-luna.jsonl
$B --models openai/gpt-6-luna --systems rag --types tenant --reps 1 --out $R/full-luna.jsonl
$B --models openai/gpt-6-luna --systems closed --reps 1 --out $R/full-luna.jsonl
# Claude Haiku 5.5
$B --models anthropic/claude-haiku-5.5 --systems keel,api,rag,closed --reps 3 --out $R/full-haiku.jsonl
# Graphiti and Mem0, one-issue questions, one repetition
$B --systems graphiti,mem0 --types $ONE --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --reps 1 --out $R/full-memories.jsonl
# Claude Sonnet 5.5, one repetition (its RAG on tenant questions stopped at its 8 USD brake: 34 of 72)
$B --models anthropic/claude-sonnet-5.5 --systems keel,api --types tenant --reps 1 --out $R/full-sonnet.jsonl
$B --models anthropic/claude-sonnet-5.5 --systems keel,api,rag,closed --types $ONE --reps 1 --out $R/full-sonnet.jsonl
$B --models anthropic/claude-sonnet-5.5 --systems rag,closed --types tenant --reps 1 --max-usd 8 --out $R/full-sonnet.jsonl
$B --models anthropic/claude-sonnet-5.5 --systems closed --types tenant --reps 1 --out $R/full-sonnet.jsonl   # the brake had stopped it too

# summary with bootstrap intervals (B=10,000, seeded; a question's repetitions averaged first) and paired differences
uv run python -m evals.jira.bench summarize $R/full-luna.jsonl $R/full-haiku.jsonl $R/full-sonnet.jsonl $R/full-memories.jsonl
```

Keel runs with the listing ceiling of 200 (`--keel-max-list 200`, the default), as an operator serving agents would
set it.

**Rows left out.** A run that failed because the gateway or the network failed, not the system, is not an answer.
Those rows were moved out of the results, named by cause, and the runs asked again:

| File | Rows | Cause | Fixed in |
| --- | --- | --- | --- |
| `*.discarded-429.jsonl` | 160 | a rate limit under a search call reached the agent as a tool error | b950062 |
| `*.discarded-network.jsonl` | 1,543 | a DNS outage recorded as wrong answers | 72314eb |
| `full-luna.discarded-405.jsonl` | 9 | the gateway's CDN served an HTML 405 page | 6eecdaf |
| `full-memories.discarded-mem0-lock.jsonl` | 105 | Mem0's telemetry locked every tenant after the first in a process | 9451668 |

## 4. Ablations

```bash
$B --models openai/gpt-6-luna --systems keel --types tenant --keel-max-list 0 --reps 1 --out $R/full-luna-keel-uncapped.jsonl
IDS=$(python3 -c "import json; print(','.join(q['id'] for t in ['hyperledger','mariadb','spring'] \
    for q in json.load(open('$E/'+t+'.questions.json')) if q['type']=='rule' or (q['type']=='tenant' and q['params'].get('rule'))))")
$B --systems keel --keel-no-rules --ids "$IDS" --reps 3 --out $R/ablation-no-rules.jsonl       # the agent applies the definition
$B --systems keel --keel-detail full --reps 1 --out $R/ablation-full-detail.jsonl               # every read in its full form
```

## 5. Governed memory (C4)

```bash
uv run pytest tests/test_memory_properties.py -s                                            # E5a, CI size
KEEL_PROPS_WRITES=2000 KEEL_PROPS_READS=20000 uv run pytest tests/test_memory_properties.py -s   # E5a, the paper's size
uv run --extra mcp --with tiktoken --with anthropic python -m evals.jira.c4_adversary --data $E --out $R/c4-adversary.jsonl --reps 3   # E5b
uv run --extra mcp --with tiktoken --with anthropic --with mem0ai==2.2.1 python -m evals.jira.c4 --data $E --out $R/c4.jsonl --reps 3  # E5c
uv run --extra mcp --with tiktoken --with anthropic python -m evals.jira.c4 --data $E --out $R/c4-subject.jsonl --conditions cited-subject --reps 3
uv run python -m evals.jira.c4 report $R/c4.jsonl
```

`c4.jsonl` was measured at 242d8c2 and its rows are marked dirty, because `c4.py` was not yet committed when the run
started. It is the code of 8bd6a6e without the `cited-subject` condition, which was measured after ea9b30a and is in
`c4-subject.jsonl`. E5c's Mem0 conditions write to a copy of each tenant's store under `results/c4-mem0/`, which is not
committed.

## 6. TGMS

TGMS 0.8.0, loaded by `evals/jira/tgms_system.py`, two ways on the 72 tenant-wide questions, one repetition:

- `tgms`: the agent calls TGMS's fifteen tools in a tool loop (GPT-6 Luna, `full-tgms.jsonl`; the MariaDB pilot that
  preceded it, stopped by its 1.5 USD brake after 18 of 24, is `pilot-mariadb-tenant-tgms.jsonl`).
- `tgms-pev`: TGMS's own agent layer, as its harness runs it (`tgms.eval.harness.run_task_ours`): `Agent.ask` plans in
  TGMS's operator IR, checks the plan statically, executes it with up to three repairs, and is scored on the engine's
  answer (`trace.answer`, read with TGMS's `mechanical_answer` and `extract_pred`). The schema paragraph the tool-loop
  agent gets is passed as a memory note. One adaptation: TGMS's planner parses the reply as bare JSON, as its guided
  decoding guarantees on the models it was built with, so the plan object is cut out of an API model's reply. Each row
  keeps the plan and, per step, its status, rows returned, completeness (`paginated` when cut at TGMS's default
  limit) and error code. Haiku 5.5 on MariaDB only, Sonnet 5.5 and Opus 5.5 on all 72 (`full-tgms-pev.jsonl`; rows
  marked dirty, measured at 1522b13 with the `tgms-pev` code uncommitted, the code of the commit that adds this file).

```bash
$B --with tgms==0.8.0 --systems tgms --types tenant --models openai/gpt-6-luna --workers 1 --out $R/full-tgms.jsonl
$B --with tgms==0.8.0 --systems tgms-pev --types tenant --models anthropic/claude-sonnet-5.5 --max-usd 4 --out $R/full-tgms-pev.jsonl
$B --with tgms==0.8.0 --systems tgms-pev --types tenant --models anthropic/claude-opus-5.5 --max-usd 8 --out $R/full-tgms-pev.jsonl
$B --with tgms==0.8.0 --systems tgms-pev --tenants mariadb --types tenant --models anthropic/claude-haiku-5.5 --out $R/full-tgms-pev.jsonl
```

Two runs of the same command must not overlap: each builds TGMS's graph in `<data>/tgms/<tenant>`, and TGMS allows
one writer per directory.

## 7. Bitemporal SQL baseline

`sql` (`evals/jira/sql_system.py`): the same log loaded deterministically into DuckDB as a database engineer would model
it, and an agent with one tool, `sql_query` (read-only, no file or network access, 200 rows per result with the cut
said, 30 s per query). Tables: `issues`, `field_versions` and `links` (each row a value over valid time
`[valid_from, valid_to)` as believed over transaction time `[recorded_at, superseded_at)`; a later record closes the
rows it changes and adds new ones), `statuses` and the raw `events`. Table macros by instant: `issue_state_at(t)`,
`issue_state_known_at(t, k)`, `links_at(t)` and the cartridge's three rules, `blocked_at(t)`, `stalled_at(t)`,
`urgent_unassigned_at(t)`. The schema and macros are the tool paragraph of the system prompt. Each tenant's database
is built once and cached next to the data (`<tenant>.sql-<digest>.duckdb`). `evals/jira/test_sql.py` checks the
tables on a small log and answers all 387 questions with one SQL query each against the gold (the ceiling).

```bash
uv run --extra mcp --with duckdb==2.0.0 --with numpy pytest evals/jira/test_sql.py
$B --with duckdb==2.0.0 --with numpy --systems sql --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --reps 3 --max-usd 10 --out $R/full-sql.jsonl
```

`sql-tables`, an ablation: the same tables, row cap and prompt without the macros and without the ready-made
predicates (the column semantics stay), so the agent writes the bitemporal conditions itself; the rules are the
cartridge's definitions in words, as Keel's tool paragraph gives them. One repetition:

```bash
$B --with duckdb==2.0.0 --with numpy --systems sql-tables --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --reps 1 --max-usd 3 --out $R/full-sql-tables.jsonl
```

## 8. Tool-result tokens and the compact form

Offline, no model called: `tokens.py` replays every Keel call of the full run on the plain and the compact MCP server
(`keel serve --compact`) and breaks the result tokens down by tool, detail and field against the SQL baseline;
`test_compact.py` checks that the compact text unpacks to the JSON and gives the same gold answer on all 387 questions.
The breakdown is `tokens.md`. `bench --keel-compact` runs the agents on the compact form.

```bash
uv run --extra mcp --with tiktoken python -m evals.jira.tokens replay --data $E --out /tmp/replay.jsonl
uv run --extra mcp --with tiktoken python -m evals.jira.tokens report /tmp/replay.jsonl
uv run --extra mcp --with tiktoken pytest evals/jira/test_compact.py -q
```

## 7. Held-out set

The 387 questions above were read while Keel was tuned. A second set of 387, on three projects of Jira instances
development never read (JiraEcosystem/AC, Sakai/GRBK, RedHat/PROJQUAY), is pre-registered in
[`heldout/README.md`](heldout/README.md) with its generation, conditions, commands, budget and analysis
(`heldout_analysis.py`), all committed before its first run. Bench reads it with `--questions evals/jira/heldout`.

## Results files

| File | What |
| --- | --- |
| `full-{luna,haiku,sonnet,memories}.jsonl` | the full run |
| `full-luna-keel-uncapped.jsonl`, `ablation-*.jsonl` | the ablations |
| `c4*.jsonl` | governed memory: E5b (`c4-adversary`), E5c (`c4`, `c4-subject`), the E5c pilot (`c4-pilot`) |
| `full-tgms.jsonl`, `full-tgms-pev.jsonl` | TGMS through its tools, and through its own planner, verifier and executor |
| `full-sql.jsonl` | the bitemporal SQL baseline (DuckDB tables and rule macros, `sql_query`) |
| `full-sql-tables.jsonl` | the same tables without macros or ready-made predicates (ablation) |
| `pilot-*.jsonl` | the MariaDB pilot that shaped the full run, TGMS included |

Each row holds the question id, system, model, repetition, gold and parsed answer, whether it was correct, every
tool call (name, arguments, tokens, error), turns, input and output tokens, cost in USD, wall time and `versions`.
