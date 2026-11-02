# Confirmation set: pre-registration

Two sets came before this one. The development set (Hyperledger, MariaDB, Spring) was read while Keel was tuned.
The held-out set (`../heldout/`, JiraEcosystem AC, Sakai GRBK, RedHat PROJQUAY) was pre-registered, and on it Keel's
five tools scored below hand-written SQL (−2.8 points) and the first generated SQL (`sqlgen`) below it too (−9.1).
Two conditions built **after** reading those results, `sqlgen2` (the generator's per-kind views and its generated
schema paragraph) and `keel+query` (Keel's tools plus the optional `query`), tied hand-written SQL there and again on
the development set. Both are exploratory on both sets. This directory fixes, before any question of it is
generated, a third set on projects of Jira instances neither set used, the conditions, the commands, the budget and
the analysis that would confirm or refute those two ties. Nothing here changes once the first paid run starts; a
fix the pilot forces is a new commit, named in the results.

Written 2026-11-01, by the author with Claude Code. What the author knew when writing it: every result of the
development and held-out sets, and, from the held-out screening at seed 31, that two of this rule's candidates
(IntelDAOS CART, SecondLife STORM) generated 112 and 114 of 129 questions (short on links, rule and tenant-wide
questions). Nothing else about the candidates below.

## Frozen code

- Keel (`keel/`): **d620fcb** (release 0.36.0). No change under `keel/` before the runs; every result row records
  the repo commit and whether `keel/` or `evals/` were dirty.
- Harness: the commit that adds this README, `screen.py` and `confirm_analysis.py` (with `test_confirm.py`); one more
  commit after screening adds the three tenants to `bench.py`'s tenant table and writes `screen.json` and
  `manifest.json`, and nothing else. The question pipeline (`adapt.py`, `check.py`, `inject.py`,
  `questions.py`) is unchanged from the held-out set's.

## How the tenants are chosen

Candidates: every project of the Public Jira Dataset instances restored here that neither earlier set used. Those
instances are IntelDAOS, JFrog, Jira, Mindville, MongoDB, Qt, SecondLife and Sonatype (28 projects already exported
to `~/datasets/public-jira/heldout/<Instance>-<KEY>.json` by the held-out screening; Apache and Mojang were not
restored for size). `screen.py` applies, to each candidate, the held-out set's rules 1 to 4 unchanged:

1. 900 to 4,000 issues;
2. `check.py` (replay equals Jira today) mismatches on at most 1% of issues (mismatch counts summed over fields);
3. a priority scheme with Blocker, Critical or Highest;
4. uncategorised statuses (no new / indeterminate / done) in at most 1% of status values.

Rule 5 is relaxed, because in the held-out screening only 3 of 9 projects filled every per-type count:

5. `inject.py` (seed 47, `--late 0.05 --corrections 0.02 --check`) and `questions.py` (seed 47, `--check`) run
   unmodified and pass their checks, and the project gets at least 100 of the 129 questions, with at least half of
   each type's count.

Of those that pass, the three with the most questions, at most one per instance (ties: fewer `check.py`
mismatches, then more issues). If fewer than three instances pass, the set is what passes, and the analysis runs on
it unchanged; if none passes, no paid run happens and this README says so. Tenant ids are `<instance>-<key>` in
lower case. `screen.py` writes every candidate's numbers to `screen.json`; `manifest.json` holds the SHA-256 of
every file the generation writes, as for the held-out set.

## Conditions and agent

Four conditions, one agent, Claude Haiku 5.5 (`anthropic/claude-haiku-5.5`), 3 repetitions, Keel conditions on the
compact read-only form, as for the held-out set:

- `keel`: the five core tools.
- `keel+query`: the five plus the optional `query`.
- `sql`: hand-written SQL macros over the event log.
- `sqlgen2`: the views and paragraph generated from the cartridge.

Through the OfoxAI gateway (`EVAL_ANTHROPIC_DIRECT=0`): the direct Anthropic account has no credit, and the
development-set confirmation ran the same way. Costs are therefore not comparable with the held-out set's.

```bash
C=~/datasets/public-jira/confirm; R=evals/jira/results
B="EVAL_ANTHROPIC_DIRECT=0 uv run --extra mcp --with tiktoken --with anthropic --with numpy --with duckdb==2.0.0 python -m evals.jira.bench \
   --data $C --questions evals/jira/confirm --tenants <the three> --keel-compact --keel-read-only --models anthropic/claude-haiku-5.5"
# pilot, read for harness errors only, its rows not analysed (about 0.1 USD)
$B --systems keel,keel+query,sql,sqlgen2 --per-type 1 --reps 1 --max-usd 0.3 --out $R/confirm-pilot.jsonl
# the run (about 5 USD measured through the gateway on the development set; one brake; a rerun resumes)
$B --systems keel,keel+query,sql,sqlgen2 --reps 3 --max-usd 7.5 --out $R/confirm-haiku.jsonl
uv run --with numpy python -m evals.jira.confirm_analysis $R/confirm-haiku.jsonl --out $R/confirm-stats.md
```

## Analysis

`confirm_analysis.py`, committed with this README, reusing the held-out set's statistics:

- **Primary metric**: accuracy per condition over the set's questions, a question's repetitions averaged first.
- **Three confirmatory hypotheses**, each a paired difference over the questions both conditions answered, with a
  95% interval from the cluster bootstrap that resamples tenant x type cells whole (`stats_cluster.py`), B = 10,000,
  seed 0:
  - **H1, generated SQL is not worse than hand-written SQL**: `sqlgen2 − sql`, confirmed if the interval's lower
    bound is above −2.0 points.
  - **H2, Keel with `query` is not worse than hand-written SQL**: `keel+query − sql`, the same margin.
  - **H3, `query` adds to Keel's five tools**: `keel+query − keel`, confirmed if the interval's lower bound is
    above 0.
  The 2-point margin is fixed here, before any run: it is smaller than every gap the planned comparisons found on
  the held-out set (2.8 to 9.1 points). Each hypothesis is reported as confirmed or not, with its interval; no
  multiplicity adjustment (three separate claims, each stated in advance).
- **Secondary**: cost per correct answer and mean input tokens per run, overall and by scope (one-issue,
  tenant-wide); descriptive.
- Reported whatever they show, beside the development and held-out numbers, never in place of them.

## Amendment, 2026-11-01, after screening and before any question was generated

The registered screening (`screen-registered.json`, commit b51b725's `screen.py`) passed no candidate. Of the 28,
rule 3 failed 21: MongoDB, Qt, JFrog and IntelDAOS name their priorities `Blocker - P1` or `P1-Urgent`, and the
benchmark's urgent rule asks for Blocker, Critical or Highest by name. Rule 4 failed the rest but one (Jira's three
projects and Sonatype's: 8 to 16% uncategorised status values). Mindville ICS passed 1 to 4 and failed 5:
`inject.py` raised an IndexError at seed 47, so no question was generated. As registered, no paid run happened on
that list.

What the author saw before writing this amendment: those per-rule outcomes, and nothing else; no question of any
candidate, no model run. What changes: **the candidates are every exported project but the held-out set's three**
(`screen.candidates`), so projects of the held-out instances (JiraEcosystem, Sakai, RedHat) that the held-out set
did not use enter, alongside the eight instances above. Rules 1 to 5, the seed, the selection (most questions, at
most one per instance, the tie-breaks), the conditions, the commands, the budget and the analysis are unchanged.
What it costs: if a selected project shares an instance with the held-out set, it shares that instance's workflow,
status names and people, so this set is disjoint from the held-out set in projects, issues and questions, and from
the development set in instances too, but not from the held-out set in instances. The results say which.

## Outcome of the amended screening, 2026-11-01: no set

`screen-amended.json`: of 94 candidates, 6 passed rules 1 to 4 (as the held-out screening found 9, three of them now used)
and none passed rule 5. Short on links questions (at least 8 of 15 needed): JiraEcosystem CE 121 questions with 7
links, RedHat APIMAN 113 with 1, ERRAI 104 with none, JBJCA 103 with 1; ERRAI and JBJCA also under half of the
rule or tenant-wide counts; Mindville ICS and RedHat CS: `inject.py` raised, no questions. As registered, no paid
run happens. A second amendment was not made: these counts were seen, and any rule written now would be written
knowing which projects it lets in. `sqlgen2` and `keel+query` stay exploratory.

## Second amendment, 2026-11-02, before Apache or Mojang was restored

The held-out screening left out the dataset's two largest instances, Apache and Mojang, for size; nothing about
their projects has been seen by the author (no issue count, no rule outcome). They are restored now and their
projects join the candidates: `screen.candidates` already takes every export in the directory but the held-out
set's three, so the code is unchanged. Each project with 900 to 4,000 issues (rule 1, applied with a count in
Mongo before export, the rule unchanged) is exported as `<Instance>-<KEY>.json`, as the held-out screening did:

```bash
D=~/datasets/public-jira; H=$D/heldout
docker run -d --rm --name jira-mongo-confirm -v "$D/ThePublicJiraDataset/3. DataDump":/dump:ro mongo:7
docker exec jira-mongo-confirm mongorestore --gzip --archive=/dump/mongodump-JiraReposAnon.archive \
    --nsInclude JiraReposAnon.Apache --nsInclude JiraReposAnon.Mojang
# per instance: project keys with 900-4,000 issues, then for each
docker exec jira-mongo-confirm mongoexport --quiet --db JiraReposAnon --collection Apache --jsonArray \
    --query '{"fields.project.key":"<KEY>"}' > $H/Apache-<KEY>.json
uv run --with pymongo python -m evals.jira.screen $H ~/datasets/public-jira/confirm --out evals/jira/confirm/screen.json
```

One rule is added, written before any of their numbers exist, because two instances can give at most two
tenants under "at most one per instance": **if fewer than three instances pass, the set is filled to three with the
next projects by the same ranking, at most two per instance.** Rules 1 to 5, the seed, the ranking and tie-breaks,
the conditions, the commands, the budget and the analysis are unchanged. If no project passes, no paid run happens.
