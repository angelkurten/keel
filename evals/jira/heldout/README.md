# Held-out set: pre-registration

The Jira benchmark's 387 development questions (`<tenant>.questions.json` for hyperledger, mariadb and spring, seed 7)
were read while Keel was tuned: tool descriptions, the compact text, `where` lists, the request trimming. Numbers on
them are not a test of what development never saw. This directory fixes, before any run, a second set of 387
questions on three projects of three Jira instances development never read, the conditions that will be run on it,
their exact commands and budget, and the analysis. Nothing here is changed once the first paid run starts; a fix the
pilot forces is a new commit, named in the results.

## Frozen code

- Keel (`keel/`): **d7b3576cb270a4b406ac4de8167b4996b930776a** (the `paper` tip this set was built from). No change
  under `keel/` before the runs; every result row records the repo commit and whether `keel/` or `evals/` were dirty.
- Harness: the commit that adds this README. The pipeline that made the questions (`adapt.py`, `check.py`,
  `inject.py`, `questions.py`) is unchanged from d7b3576.

## How the tenants were chosen

The Public Jira Dataset has 16 Jira instances; development used Hyperledger, MariaDB and Spring. The rule, applied
to the projects of every other instance restored here (all but Apache and Mojang, which were not restored for size):

1. 900 to 4,000 issues (development: 2,323 to 3,333);
2. `check.py` (replay equals Jira today) mismatches on at most 1% of issues;
3. a priority scheme with Blocker, Critical or Highest (so `urgent_unassigned` can hold);
4. uncategorised statuses (no new / indeterminate / done) in at most 1% of status values (screened first at 0.6% of
   all events);
5. `inject.py` and `questions.py` run unmodified at seed 31 and fill every per-type count of the development set.

97 projects in 11 instances were screened; 28 passed 1 to 3, 9 passed 4, and 3 passed 5, one per instance. Those
three are the held-out set; no other choice was made.

| Tenant | Instance | Project | Issues | Events (bitemporal) | `check.py` mismatches | Late / corrections injected |
| --- | --- | --- | ---: | ---: | --- | --- |
| ecosystem-ac | JiraEcosystem | AC | 1,544 | 11,142 | none | 424 / 170 |
| sakai-grbk | Sakai | GRBK | 1,433 | 8,952 | 11 issues (resolution 4, assignee 5, priority 2) | 342 / 137 |
| redhat-projquay | RedHat | PROJQUAY | 2,455 | 15,906 | 20 issues (links 16, assignee 4) | 609 / 244 |

## Why it is disjoint from development

- Different instances, so different projects, issues, people, workflows, status names and priority schemes; no
  issue key, question text or subject is shared (`test_heldout.py` checks it).
- A different seed (31, development 7) for the injected late arrivals and corrections and for the question
  templates' instants and subjects. The generator seeds with `"<seed>:<tenant>"`, so even the same seed could not
  reproduce a development question.
- Same question types, templates, per-type counts and gold machinery (`questions.py`'s oracle, which imports nothing
  from Keel), so the two sets measure the same thing.

## Generation

```bash
D=~/datasets/public-jira; H=$D/heldout
docker run -d --rm --name jira-mongo -v "$D/ThePublicJiraDataset/3. DataDump":/dump:ro mongo:7
docker exec jira-mongo mongorestore --gzip --archive=/dump/mongodump-JiraReposAnon.archive \
    --nsInclude JiraReposAnon.JiraEcosystem --nsInclude JiraReposAnon.Sakai --nsInclude JiraReposAnon.RedHat
docker exec jira-mongo mongoexport --quiet --db JiraReposAnon --collection JiraEcosystem --jsonArray \
    --query '{"fields.project.key":"AC"}' > $H/JiraEcosystem-AC.json
# the same for Sakai/GRBK and RedHat/PROJQUAY
for x in JiraEcosystem:AC:ecosystem-ac Sakai:GRBK:sakai-grbk RedHat:PROJQUAY:redhat-projquay; do
  IFS=: read inst key t <<< "$x"
  uv run python -m evals.jira.adapt $H/$inst-$key.json $t $H/$t.jsonl --projects $key
  uv run python -m evals.jira.check $H/$inst-$key.json $H/$t.jsonl
  uv run python -m evals.jira.inject $H/$t.jsonl $H/$t.bitemporal.jsonl $H/$t.injections.json --seed 31 --late 0.05 --corrections 0.02 --check
  uv run python -m evals.jira.questions $H/$t.bitemporal.jsonl $H/$t.injections.json $H/$t.questions.json --seed 31 --check
done
```

`manifest.json` holds the SHA-256 of every file this writes (export, events, bitemporal log, injections, questions);
the questions themselves are committed here (`<tenant>.questions.json`, 129 each).

## Questions

| Type | per tenant | total | |
| --- | ---: | ---: | --- |
| current | 20 | 60 | |
| as_of | 25 | 75 | |
| known_at | 25 | 75 | |
| links | 15 | 45 | relates_to 35, duplicates 6, depends_on 3, clones 1 |
| rule | 15 | 45 | blocked 10 (GRBK has no blocks links), stalled 18, urgent_unassigned 17 |
| abstain | 5 | 15 | |
| tenant | 24 | 72 | status_at 18, rule_at 18, count_at 12, changed 12, known_at 12 |
| **all** | **129** | **387** | one-issue 315, tenant-wide 72 |

## Gold verified three ways, offline

```bash
uv run --extra mcp --with tiktoken --with numpy --with duckdb==2.0.0 pytest evals/jira/test_heldout.py -q
```

With the data on disk (`~/datasets/public-jira/heldout`, or `KEEL_JIRA_HELDOUT`), it checks the checksums,
regenerates the questions byte for byte, and reaches every one of the 387 gold answers, each computed by the
oracle walking the log, by (1) Keel's primitives (`questions.check`), (2) one hand-written SQL query over
`sql_system`'s bitemporal tables and macros, and (3) one SQL query over the macros `keel/sqlgen.py` generates from
the cartridge. All 387 agree on all three (at the commit that added the set). It also runs each condition below
through `bench.ask` with a scripted model on every tenant: prompt, tools and one tool call, no error.

## Conditions

Same system prompt but for one tool paragraph, same grading (exact match), the same log for every system.

| System | What the agent gets | Flags |
| --- | --- | --- |
| keel | Keel's tools over MCP, compact text, the four read tools, listing ceiling 200 | `--keel-compact --keel-read-only` |
| sql | `sql_query` over hand-modelled bitemporal tables with hand-written point-in-time and rule macros | |
| sqlgen | `sql_query` over Keel's own tables with the macros generated from the cartridge (`keel/sqlgen.py`) | |
| sql-tables | `sql_query` over the same tables as `sql`, no macros (the rules as definitions in words) | |
| api | a Jira-like API: get_issue, get_changelog, search_issues with JQL-style WAS / CHANGED | |

Agents: `openai/gpt-6-luna` (gateway, temperature 0) and `anthropic/claude-haiku-5.5` (Anthropic, default
temperature). Repetitions: 3 for every condition, as the budget allows (below).

## Budget and commands

Estimated from the per-run cost of the development results (one-issue / tenant-wide, mean USD per run):
keel from `ab-requests.jsonl` and `ab-tenant-new-3reps.jsonl` (compact, read-only), sql and sql-tables from
`full-sql*.jsonl`, api from `full-{luna,haiku}.jsonl`; sqlgen has no agent runs yet and is taken as 1.5 times sql.
The held-out tenants are smaller than development's (8,952 to 15,906 events against 15,927 to 18,267), so the
tenant-wide estimates should be high, not low.

| System | Luna, one rep | Haiku, one rep | 3 reps, both agents |
| --- | ---: | ---: | ---: |
| keel | 0.26 | 0.22 | 1.44 |
| sql | 0.12 | 0.17 | 0.86 |
| sqlgen | 0.18 | 0.25 | 1.30 |
| sql-tables | 0.14 | 0.24 | 1.14 |
| api | 0.40 | 0.34 | 2.22 |
| **all** | **1.10** | **1.22** | **6.96** |

The pilot first (one question of each type per tenant: 21 questions x 5 systems x 2 agents = 210 runs, about 0.2 USD), read
for harness errors only; its rows are not analysed:

```bash
H=~/datasets/public-jira/heldout; R=evals/jira/results
B="uv run --extra mcp --with tiktoken --with anthropic --with duckdb==2.0.0 python -m evals.jira.bench --data $H \
   --questions evals/jira/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --keel-compact --keel-read-only"
$B --systems keel,sql,sqlgen,sql-tables,api --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --per-type 1 --reps 1 \
   --max-usd 0.5 --out $R/heldout-pilot.jsonl
```

Then the run, one file per agent (each brake about 1.5 times its estimate; a rerun of the same command resumes):

```bash
$B --systems keel,sql,sqlgen,sql-tables,api --models openai/gpt-6-luna --reps 3 --max-usd 5 --out $R/heldout-luna.jsonl
$B --systems keel,sql,sqlgen,sql-tables,api --models anthropic/claude-haiku-5.5 --reps 3 --max-usd 5 --out $R/heldout-haiku.jsonl
```

`bench` orders its runs repetition first, so a brake that fires leaves whole repetitions of every system done. If
the spend after repetition 1 projects past 10 USD, the cuts, in order: api and sql-tables to one repetition, then
sqlgen to two. Rows lost to a gateway or network failure are moved out and asked again, as in the development run.

## Analysis plan

```bash
uv run --with numpy python -m evals.jira.heldout_analysis $R/heldout-luna.jsonl $R/heldout-haiku.jsonl --out $R/heldout-stats.md
```

`evals/jira/heldout_analysis.py`, committed with this README:

- **Primary metric**: accuracy per system and agent over the 387 questions, a question's repetitions averaged first.
- **Planned comparisons** (ten, per agent): keel - sql, keel - sqlgen, keel - sql-tables, keel - api, and
  sql - sqlgen (hand-written against generated macros). Each is a paired difference over the questions both
  answered, with a 95% interval and two-sided p from a cluster bootstrap that resamples tenant x type cells whole
  (tenant-wide questions by sub-type; `stats_cluster.py`'s `cluster`), B = 10,000, seed 0; p-values Holm-adjusted
  over the ten. The bootstrap over questions as independent draws is reported beside it as a sensitivity check.
- **Secondary**: cost per correct answer (USD spent over correct runs) and mean input tokens per run, per system and
  agent, overall and by scope (one-issue, tenant-wide). Descriptive, no test.
- Reported whatever they show, next to the development numbers, never in place of them.
