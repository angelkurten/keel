# Hard slice: pre-registration

The three question sets so far sit near the ceiling: on the confirmation set every SQL-backed condition answered 95.7
to 96.0%, and its non-inferiority hypotheses were decided on 4 and 12 questions where conditions differed. A reviewer
asked whether the ties hold when the questions are harder. This directory fixes, before any model has seen one of its
questions, a slice of 320 harder questions over the eight tenants of the three sets, the conditions, the commands, the
budget and the analysis. Nothing here changes once the first paid run starts; a fix the pilot forces is a new commit,
named in the results.

Written 2026-11-02, by the author with Claude Code. What the author knew when writing it: every result of the
development, held-out and confirmation sets. What the author had seen of these questions: their text, their gold and
the distribution of answers (below); no model has answered any.

## Questions

`hard.py` (with `test_hard.py`), seed 53, 8 questions of each of five types per tenant, 40 per tenant, 320 in all. Each
type combines two steps that the main set's questions take one at a time:

- `two_hop`: issues two relations away from one, both relations as they stood at an instant ("take the sub-tasks of
  X; what were the issues that block any of them?"). Relations named explicitly (the links lesson of the confirmation
  set): sub-task, parent, blocks, blocked by, and 'relates to', 'depends on', 'duplicates' in either direction.
- `known_count`: how many issues of the project had a status at an instant, as recorded by a later instant. Only
  where a late or corrected record makes that count differ from today's knowledge of the same instant.
- `entries`: how many times one issue was moved into a status within a window (never its creation); mostly 2 or more.
- `time_in`: whole days one issue spent in a status up to an instant, every spell added, rounded down; the total is
  never within an hour of a whole day.
- `both`: the issues in progress (the tenant's indeterminate statuses, listed) at two instants 7 to 90 days apart;
  only where neither instant alone gives the answer.

Instants keep an hour from every change the question reads, as in the main set. Gold comes from `questions.Oracle`
over the log alone; `hard.check` answers all 320 with Keel's primitives, written apart from the oracle, and agrees on
all 320 (`test_hard.py` regenerates every file from the data byte for byte and runs that check). Gold was not checked
through SQL as the main sets' was. `manifest.json` holds the SHA-256 of every question file and of the log and
injections each was generated from.

Answers seen while writing this: `two_hop` sets of 1 to 15 issues (35 of 64 have one), most of them through two
'relates to' hops (the relation the data has most of); `known_count` from 1 to 1,652; `entries` 1 to 6 (34 of 64 are
1); `time_in` 1 to 2,677 days; `both` sets of 1 to 15.

## Conditions, agent, commands

As registered for the confirmation set: `keel`, `keel+query`, `sql`, `sqlgen2`; Claude Haiku 5.5 straight from
Anthropic; 3 repetitions; Keel conditions on the compact read-only form; Keel's code `keel/` at **d620fcb**, unchanged
since. The runs start from a clean `git worktree` of the commit that adds this README, so every row records
`dirty: false`.

```bash
git worktree add ../keel-hard <this commit> && cd ../keel-hard
D=~/datasets/public-jira; R=evals/jira/results
B="uv run --extra mcp --with tiktoken --with anthropic --with numpy --with duckdb==2.0.0 python -m evals.jira.bench \
   --questions evals/jira/hard --keel-compact --keel-read-only --models anthropic/claude-haiku-5.5 --systems keel,keel+query,sql,sqlgen2"
# pilot, read for harness errors only, its rows not analysed (one question of each type per tenant)
$B --data $D/export  --tenants hyperledger,mariadb,spring --per-type 1 --reps 1 --max-usd 0.2 --out $R/hard-pilot.jsonl
$B --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --per-type 1 --reps 1 --max-usd 0.2 --out $R/hard-pilot.jsonl
$B --data $D/confirm --tenants apache-tajo,apache-rave --per-type 1 --reps 1 --max-usd 0.2 --out $R/hard-pilot.jsonl
# the run: 3,840 runs; one brake per command (4 USD in all); a rerun resumes
$B --data $D/export  --tenants hyperledger,mariadb,spring --reps 3 --max-usd 1.5 --out $R/hard-haiku.jsonl
$B --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 3 --max-usd 1.5 --out $R/hard-haiku.jsonl
$B --data $D/confirm --tenants apache-tajo,apache-rave --reps 3 --max-usd 1.0 --out $R/hard-haiku.jsonl
uv run --with numpy python -m evals.jira.confirm_analysis $R/hard-haiku.jsonl --questions evals/jira/hard --out $R/hard-stats.md
```

If a brake stops a command, nothing more is spent until the author decides; the rows so far are reported as partial.

## Analysis

`confirm_analysis.py` unchanged (its output's title says "Confirmation set"; the numbers are this slice's), over the
320 questions pooled: accuracy per condition, a question's repetitions averaged first, and the same three hypotheses
with the same bounds, each a paired difference with a 95% interval from the cluster bootstrap over tenant x type cells
(40 cells), B = 10,000, seed 0, no adjustment:

- **H1** `sqlgen2 − sql`, confirmed if the lower bound is above −2.0 points.
- **H2** `keel+query − sql`, the same.
- **H3** `keel+query − keel`, confirmed if the lower bound is above 0.

Descriptive, from the same rows: accuracy per type and per set for each condition, and cost and input tokens per run.
The slice is reported as off the ceiling if the best condition scores below 95%; if every condition scores 95% or
more, the slice did not do what it was made for, and the paper says so. Reported whatever it shows, beside the three
sets, never in place of them.

## Pilot, 2026-11-02 (`results/hard-pilot.jsonl`, not analysed)

160 runs, 0.15 USD, no harness error. Two changes to the commands, neither to the code: the worktree's fresh
environment lacks `httpx` (`--with httpx` added), and the commands run under bash (zsh does not split `$B`). The
bench prints an accuracy table after each command, so the author saw the pilot's accuracy by type; nothing was
changed because of it. The pilot projected 3.6 USD for the run, above the 2 USD first estimated; the author approved it.

## Result, 2026-11-02 (`results/hard-haiku.jsonl`, `results/hard-stats.md`)

3,840 runs, all on 552e729 with `dirty: false`, Claude straight from Anthropic, no harness error, 3.12 USD; no brake
reached. Accuracy: keel 92.6% [90.1, 94.9], keel+query 95.8 [93.6, 97.8], sql 97.4 [95.8, 98.8], sqlgen2 96.2
[94.4, 97.9]. The best condition scores above 95%, so by the rule above the slice is **not** off the ceiling, though
below it keel's five tools fall to 92.6 and the hypotheses are decided on 26 to 53 discordant questions, against 4 to
12 on the confirmation set.

- **H1** sqlgen2 − sql −1.1 points [−3.1, +0.8] (10 better, 16 worse): **not confirmed**.
- **H2** keel+query − sql −1.6 [−3.8, +0.5] (12 / 23): **not confirmed**.
- **H3** keel+query − keel +3.2 [+0.9, +5.7] (36 / 17): **confirmed**.

Descriptive, accuracy by type (keel, keel+query, sql, sqlgen2): two_hop 95.3, 93.8, 95.3, 94.3; known_count 100 for
all four (the type turned out easy); entries 94.3, 99.0, 93.7, 99.0; time_in 86.5, 86.5, 97.9, 89.1; both 87.0, 100,
100, 99.0. By set: development 95.0, 96.4, 97.5, 96.7; held-out 89.2, 95.3, 97.2, 95.6; confirmation 94.2, 95.8,
97.5, 96.7. Input tokens per run 16,287, 13,015, 6,908 and 12,183; USD per correct answer 0.00120, 0.00067, 0.00062
and 0.00093.

Read after the result: keel+query and sqlgen2 miss `time_in` mostly by one day (e.g. 1,529 for 1,528), the agent
doing the date arithmetic in its reply instead of in SQL. The agent's queries compute the dates in SQL in 131 of 192
`time_in` runs with `sql` (4 missed), 25 with `sqlgen2` (21 missed) and 8 with `keel+query` (26 missed). Neither
paragraph documents date functions; why the hand-written schema leads the agent to compute in SQL is not measured.
This is where most of hand-written SQL's lead over the generated layer comes from.

## Addendum, 2026-11-02, before any of its runs: the agent writing SQL from less (exploratory)

The author asked whether the agent could be measured writing the SQL itself, knowing neither Keel's generated layer
nor ours. Two conditions run on the same 320 questions, same agent, repetitions and route, both **exploratory** (no
decision rule; the result above is unchanged by them):

- `sql-tables`: our bitemporal tables (`field_versions`, `links` with valid and transaction intervals, `issues`,
  `statuses`, `events`) without macros or ready-made predicates; the agent writes every condition. Run before on the
  held-out set only.
- `sql-log`: new (`sql_system.TOOLS_TEXT_LOG`, with tests in `test_sql.py`): one table, `events`, the raw log as written,
  with each event type's payload and how late records and corrections show in it; the agent derives valid time,
  transaction time and corrections itself. Written by the author after this slice's result was known; like the other
  paragraphs it documents no date function. Event ids are as written, so a corrected record's id ends in `:wrong`, as
  in `sql-tables`' `events` table. No SQL over the raw log was checked to reach the gold.

What is reported: accuracy per condition and per type, and these paired differences with the same cluster interval,
no adjustment and no decision rule: `sql-tables − sql`, `sql-log − sql`, `sqlgen2 − sql-log`, `keel+query − sql-log`,
`keel − sql-log`. `confirm_analysis.py` gained `--pairs` for that (its registered output is unchanged, checked
byte for byte on `results/hard-stats.md`). Code: the commit that adds this section; `keel/` still d620fcb.

```bash
B="... the same command as above ... --systems sql-tables,sql-log"
# pilot, harness errors only, not analysed
$B --data $D/export --tenants hyperledger,mariadb,spring --per-type 1 --reps 1 --max-usd 0.1 --out $R/hard-ablation-pilot.jsonl
# (and the held-out and confirmation tenants, as above)
# the run: 1,920 runs, about 1.6 USD; brakes 0.9, 0.9 and 0.6 USD
$B --data $D/export  --tenants hyperledger,mariadb,spring --reps 3 --max-usd 0.9 --out $R/hard-ablation-haiku.jsonl
$B --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 3 --max-usd 0.9 --out $R/hard-ablation-haiku.jsonl
$B --data $D/confirm --tenants apache-tajo,apache-rave --reps 3 --max-usd 0.6 --out $R/hard-ablation-haiku.jsonl
uv run --with numpy python -m evals.jira.confirm_analysis $R/hard-haiku.jsonl $R/hard-ablation-haiku.jsonl --questions evals/jira/hard \
    --pairs sql-tables:sql,sql-log:sql,sqlgen2:sql-log,keel+query:sql-log,keel:sql-log --out $R/hard-ablation-stats.md
```

Pilot of the addendum, 2026-11-02 (`results/hard-ablation-pilot.jsonl`, not analysed): 80 runs, 0.11 USD, no harness
error; the bench printed its accuracy table. It projects 2.7 USD for the run, so before the run the brakes are raised to
1.2, 1.2 and 0.8 USD (3.2 in all), approved by the author; nothing else changes.

Result of the addendum, 2026-11-02 (`results/hard-ablation-haiku.jsonl`, `results/hard-ablation-stats.md`): 1,920 runs,
all on d5e7201 with `dirty: false`, no harness error, 2.18 USD; no brake reached. During the run the author asked for,
and saw, partial accuracy twice (at 782 and 1,527 runs); nothing was changed, and no decision rule depends on it.

Accuracy: sql-tables 95.9% [94.0, 97.7], sql-log 94.5 [92.5, 96.2] (beside sql 97.4, sqlgen2 96.2, keel+query 95.8,
keel 92.6). Paired differences, exploratory: sql-tables − sql −1.5 [−2.8, −0.2] (7 / 22); sql-log − sql −2.9
[−5.0, −1.0] (11 / 29); sqlgen2 − sql-log +1.8 [−0.3, +4.0] (24 / 14); keel+query − sql-log +1.4 [−0.8, +3.5]
(24 / 18); keel − sql-log −1.9 [−4.5, +0.7] (22 / 36). By type (sql-tables, sql-log): two_hop 95.3, 93.8;
known_count 100, 98.4; entries 92.7, 96.4; time_in 95.3, 92.7; both 96.4, 91.1. Input tokens per run 8,601 and
20,122; USD per correct answer 0.00085 and 0.00154, against 0.00062 for sql, 0.00067 for keel+query and 0.00093 for
sqlgen2.

## Second addendum, 2026-11-02, before any of its runs: a stronger agent (exploratory)

A reviewer asked whether the results hold for another agent. The author chose Claude Sonnet 5.5
(`anthropic/claude-sonnet-5.5`, straight from Anthropic), one repetition, on four conditions: `sql`, `sqlgen2`,
`keel+query` and `sql-log`. **Exploratory**: one repetition, no decision rule; Haiku's registered result above is
unchanged by it. Estimated 25 to 45 USD (Haiku's tokens at Sonnet's list prices, 45 USD, less prompt caching); brakes
17, 17 and 11 USD; if one is reached nothing more is spent until the author decides.

Reported: accuracy per condition, per type and per tenant group; the same paired differences as for Haiku, read on
Sonnet's rows with the same cluster interval and no decision rule: `sqlgen2 − sql` and `keel+query − sql` (H1, H2 as
descriptions, not tests), `sqlgen2 − sql-log` and `keel+query − sql-log`, `sql-log − sql`; cost per correct answer and
input tokens. `confirm_analysis.py` gained `--agent` (default Haiku; its registered output checked byte for byte).
Code: the commit that adds this section; `keel/` still d620fcb.

```bash
S="... the same command as above, with --models anthropic/claude-sonnet-5.5 --systems sql,sqlgen2,keel+query,sql-log"
# pilot on the confirmation tenants, harness errors only, not analysed (40 runs)
$S --data $D/confirm --tenants apache-tajo,apache-rave --per-type 1 --reps 1 --max-usd 3 --out $R/hard-sonnet-pilot.jsonl
# the run: 1,280 runs
$S --data $D/export  --tenants hyperledger,mariadb,spring --reps 1 --max-usd 17 --out $R/hard-sonnet.jsonl
$S --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 1 --max-usd 17 --out $R/hard-sonnet.jsonl
$S --data $D/confirm --tenants apache-tajo,apache-rave --reps 1 --max-usd 11 --out $R/hard-sonnet.jsonl
uv run --with numpy python -m evals.jira.confirm_analysis $R/hard-sonnet.jsonl --questions evals/jira/hard --agent Sonnet \
    --pairs sqlgen2:sql-log,keel+query:sql-log,sql-log:sql --out $R/hard-sonnet-stats.md
```

Pilot of the second addendum, 2026-11-02 (`results/hard-sonnet-pilot.jsonl`, not analysed): 40 runs, 0.49 USD, no
harness error, all straight to Anthropic; it projects 16 USD for the run. Nothing changes.

Result of the second addendum, 2026-11-03 (`results/hard-sonnet.jsonl`, `results/hard-sonnet-stats.md`): 1,280 runs,
all on fd6353c with `dirty: false`, no harness error, 13.43 USD. Sonnet saturates the slice: sql 100.0%, sql-log 99.4,
keel+query 99.1, sqlgen2 99.1; 8 misses in all (entries 5, time_in 2, two_hop 1). Paired, exploratory: sqlgen2 − sql
−0.9 [−2.2, 0.0] (0 / 3), keel+query − sql −0.9 [−2.2, 0.0] (0 / 3), sql-log − sql −0.6 [−1.6, 0.0] (0 / 2),
sqlgen2 − sql-log and keel+query − sql-log −0.3 (1 / 2). `confirm_analysis` prints "not confirmed" beside H1 and H2:
that label is the Haiku registration's rule, which does not apply here (no decision rule was registered for Sonnet).
USD per correct answer: sql 0.0091, keel+query 0.0094, sqlgen2 0.0108, sql-log 0.0129 (1.2 to 1.4 times the others);
input tokens per run 5,531, 10,170, 7,355 and 7,507 (Haiku read 20,122 over the raw log).

## Third addendum, 2026-11-03, before any of its runs: the duration remedy (exploratory, with a stated prediction)

Read after the slice's result: Haiku over the compiled interfaces missed `time_in` mostly by one day, counting days in
its reply, although the compiled SQL has had `keel_days(a, b)` (fractional days between two instants) in every
dialect; the generated paragraph never named it. The remedy, a change to `keel/` (`keel.sqlgen.describe`, the
first time this slice's runs leave d620fcb): one sentence in the paragraph, the same for every cartridge, "keel_days(a,
b) is the days from instant a to b as a fraction (2.5: two and a half days): compute durations in SQL with it, adding
periods and rounding there, rather than counting dates by hand." Nothing else under `keel/` changes; tests in
`tests/test_sqlgen_views.py`.

Run: Haiku, 3 repetitions, `sqlgen2` and `keel+query` (the two conditions whose paragraph is generated), all 320
questions so a side effect on the other types shows, from a clean worktree of the commit that adds this section;
1,920 runs, about 1.5 USD (their cost on the registered run), brakes 1.0, 1.0 and 0.7 USD.

What is compared: each condition after the remedy against the same condition's registered rows
(`results/hard-haiku.jsonl`), paired per question, accuracy per type and overall, with the cluster interval and no
adjustment. Stated before the run: the remedy is called effective if, for **both** conditions, `time_in` accuracy
rises by at least 5 points and overall accuracy does not fall by more than 1 point; otherwise it is reported as not
effective. Either way the slice's registered result above is unchanged; the paper reports this as an exploratory test
of a post-hoc explanation.

```bash
B="... the same command as above, with --systems sqlgen2,keel+query"
$B --data $D/export  --tenants hyperledger,mariadb,spring --reps 3 --max-usd 1.0 --out $R/hard-days-haiku.jsonl
$B --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 3 --max-usd 1.0 --out $R/hard-days-haiku.jsonl
$B --data $D/confirm --tenants apache-tajo,apache-rave --reps 3 --max-usd 0.7 --out $R/hard-days-haiku.jsonl
# the new rows renamed `<system>+days`, paired against the registered rows of the same condition
uv run --with numpy python -c "
import json; from pathlib import Path; from evals.jira import confirm_analysis as ca
H = Path('evals/jira/hard'); m = json.loads((H / 'manifest.json').read_text())
qs = {q['id']: q for t in m['tenants'] for q in json.loads((H / f'{t}.questions.json').read_text())}
old = [json.loads(l) for l in open('$R/hard-haiku.jsonl')]
new = [json.loads(l) | {'system': json.loads(l)['system'] + '+days'} for l in open('$R/hard-days-haiku.jsonl')]
for typ in (None, 'time_in'):
    q = {k: v for k, v in qs.items() if typ is None or v['type'] == typ}
    print(typ or 'all', ca.markdown(ca.analyze(q, old + new, pairs=[('sqlgen2+days', 'sqlgen2'), ('keel+query+days', 'keel+query')])))
" > $R/hard-days-stats.md
```

Result of the third addendum, 2026-11-03 (`results/hard-days-haiku.jsonl`, `results/hard-days-stats.md`): 1,920 runs,
all on a75faee with `dirty: false`, no harness error, 1.53 USD. **The remedy is effective by the rule stated above**:
`time_in` rose from 89.1% to 98.4% for sqlgen2 (+9.4 points paired, [+5.2, +13.5], 15 / 3) and from 86.5% to 99.0%
for keel+query (+12.5, [+8.3, +16.7], 19 / 1); overall accuracy rose too, 96.2 → 98.2 (+2.0, [+0.5, +3.5], 20 / 7) and
95.8 → 98.6 (+2.8, [+1.1, +4.8], 24 / 3). By type after the remedy (sqlgen2, keel+query): two_hop 95.3, 95.8;
known_count 100, 100; entries 98.4, 99.0; time_in 98.4, 99.0; both 99.0, 99.5. Input tokens per run rose by about 6
to 7% (12,954 and 13,982); cost per correct answer 0.00095 and 0.00067.

Read after the result, post hoc and not registered: against hand-written SQL's registered rows (on d620fcb's
`keel/`), sqlgen2 with the remedy is +0.8 points [−0.2, +2.0] (14 / 7) and keel+query +1.2 [+0.2, +2.5] (12 / 3).
Different commits and a remedy chosen after reading the result: a direction, not a test.

## Fourth addendum, 2026-11-03, before any of its runs: the duration remedy for the baselines too

A reviewer noted the third addendum's remedy was asymmetric: only the generated paragraphs got the duration sentence,
though the hand-written ones never named a date function either. This run gives the three hand-written baselines the
same sentence, with DuckDB's own function in place of `keel_days` (their databases are unchanged):
`sql_system.DAYS_SENTENCE`, "date_diff('microsecond', a, b) / 86400000000.0 is the days from instant a to b as a
fraction (2.5: two and a half days): compute durations in SQL with it, adding periods and rounding there, rather than
counting dates by hand.", appended to `sql`'s, `sql-tables`' and `sql-log`'s paragraphs (bench systems `sql+days`,
`sql-tables+days`, `sql-log+days`; tests in `test_sql.py`).

Run: Haiku, 3 repetitions, the three on all 320 questions, from a clean worktree of the commit that adds this
section; 2,880 runs, about 2.8 USD (their cost on the registered run), brakes 1.2, 1.2 and 0.8 USD. `keel/` is a75faee's
(the remedied paragraph); the compiled conditions' remedied rows are the third addendum's (`hard-days-haiku.jsonl`,
same `keel/`; the harness differs only by the baselines' sentence).

Decided by rules fixed now (paired over all 320 questions, cluster interval, no adjustment):
- **R1** `sqlgen2+days − sql+days`: the compiled SQL is not worse than hand-written SQL when both are told the same,
  if the lower bound is above −2 points.
- **R2** `keel+query+days − sql+days`: the same.
- **R3**, whether "the views matter more than the language" survives for Haiku: it does if `sql+days − sql-log+days` has
  a lower bound above 0; otherwise the paper withdraws that lesson for Haiku.
Reported beside them: each baseline's change from its unremedied rows, by type, and cost per correct answer.

```bash
B="... the same command as above, with --systems sql+days,sql-tables+days,sql-log+days"
$B --data $D/export  --tenants hyperledger,mariadb,spring --reps 3 --max-usd 1.2 --out $R/hard-days-baselines-haiku.jsonl
$B --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 3 --max-usd 1.2 --out $R/hard-days-baselines-haiku.jsonl
$B --data $D/confirm --tenants apache-tajo,apache-rave --reps 3 --max-usd 0.8 --out $R/hard-days-baselines-haiku.jsonl
uv run --with numpy python -c "
import json; from pathlib import Path; from evals.jira import confirm_analysis as ca
H = Path('evals/jira/hard'); m = json.loads((H / 'manifest.json').read_text())
qs = {q['id']: q for t in m['tenants'] for q in json.loads((H / f'{t}.questions.json').read_text())}
old = [json.loads(l) for l in open('$R/hard-haiku.jsonl')] + [json.loads(l) for l in open('$R/hard-ablation-haiku.jsonl')]
comp = [json.loads(l) | {'system': json.loads(l)['system'] + '+days'} for l in open('$R/hard-days-haiku.jsonl')]
base = [json.loads(l) for l in open('$R/hard-days-baselines-haiku.jsonl')]
print(ca.markdown(ca.analyze(qs, old + comp + base, pairs=[('sqlgen2+days', 'sql+days'), ('keel+query+days', 'sql+days'),
      ('sql+days', 'sql-log+days'), ('sql+days', 'sql-tables+days'), ('sql+days', 'sql'), ('sql-tables+days', 'sql-tables'), ('sql-log+days', 'sql-log')])))
" > $R/hard-days-baselines-stats.md
```

Result of the fourth addendum, 2026-11-03 (`results/hard-days-baselines-haiku.jsonl`, `results/hard-days-baselines-stats.md`):
2,880 runs, all on 8a9b2e3 with `dirty: false`, no harness error, 2.45 USD. With the sentence: sql 97.8%, sql-tables
96.9, sql-log 96.6 (sqlgen2 98.2 and keel+query 98.6 from the third addendum).

- **R1** sqlgen2+days − sql+days +0.4 points [−0.6, +1.6] (10 / 7): **confirmed**.
- **R2** keel+query+days − sql+days +0.8 [−0.1, +2.2] (10 / 4): **confirmed**.
- **R3** sql+days − sql-log+days +1.2 [−0.2, +2.8] (14 / 8): the lower bound is not above 0, so **the lesson "the views
  matter more than the language" is withdrawn for Haiku**, as registered.

Beside them: sql+days − sql-tables+days +0.9 [−0.4, +2.3]; each baseline against its unremedied rows: sql +0.4
[−0.5, +1.3], sql-tables +0.9 [−0.6, +2.6], sql-log +2.1 [+0.5, +3.5]. time_in with the sentence: sql 99.5, sql-tables
99.0, sql-log 99.5. By type (sql, sql-tables, sql-log): two_hop 95.8, 95.8, 94.3; known_count 100, 99.5, 98.4; entries
94.8, 93.8, 96.9; both 99.0, 96.4, 93.8. USD per correct answer: sql 0.00059, sql-tables 0.00082, sql-log 0.00123
(keel+query 0.00067, sqlgen2 0.00095); input tokens per run 7,119, 8,601 and 15,084.

## Fifth addendum, 2026-11-03, before any of its runs: Sonnet with the duration sentence (exploratory)

The second addendum's Sonnet rows had no duration sentence (`keel/` d620fcb, baselines without `DAYS_SENTENCE`), so
Haiku with the sentence and Sonnet without it were compared across a difference in description. This run gives Sonnet
the same four interfaces with the sentence: `sql+days`, `sqlgen2` and `keel+query` (their paragraph is generated, and
`keel/` is a75faee's, which carries the sentence), and `sql-log+days`.

Run: Claude Sonnet 5.5, 1 repetition, all 320 questions, from a clean worktree of the commit that adds this section;
1,280 runs, about 14 USD (the second addendum's 13.4 plus the sentence's 6 to 7% more input), brakes 6, 6 and 4 USD.
No pilot: the harness and the conditions already ran (second, third and fourth addenda).

Exploratory, no decision rule. Reported: accuracy by type and overall; paired, with the cluster interval and no
adjustment, `sqlgen2 − sql+days`, `keel+query − sql+days`, `sql+days − sql-log+days`, `keel+query − sql-log+days`,
`sqlgen2 − sql-log+days`, and each condition against its second-addendum row without the sentence; cost per correct
answer and input tokens.

```bash
S="uv run --extra mcp --with tiktoken --with anthropic --with numpy --with duckdb==2.0.0 python -m evals.jira.bench \
   --questions evals/jira/hard --keel-compact --keel-read-only --models anthropic/claude-sonnet-5.5 \
   --systems sql+days,sqlgen2,keel+query,sql-log+days"
$S --data $D/export  --tenants hyperledger,mariadb,spring --reps 1 --max-usd 6 --out $R/hard-days-sonnet.jsonl
$S --data $D/heldout --tenants ecosystem-ac,sakai-grbk,redhat-projquay --reps 1 --max-usd 6 --out $R/hard-days-sonnet.jsonl
$S --data $D/confirm --tenants apache-tajo,apache-rave --reps 1 --max-usd 4 --out $R/hard-days-sonnet.jsonl
uv run --with numpy python -c "
import json; from pathlib import Path; from evals.jira import confirm_analysis as ca
H = Path('evals/jira/hard'); m = json.loads((H / 'manifest.json').read_text())
qs = {q['id']: q for t in m['tenants'] for q in json.loads((H / f'{t}.questions.json').read_text())}
old = [json.loads(l) for l in open('$R/hard-sonnet.jsonl')]
new = [json.loads(l) for l in open('$R/hard-days-sonnet.jsonl')]
new = [r | {'system': r['system'] + '+days'} if not r['system'].endswith('+days') else r for r in new]
print(ca.markdown(ca.analyze(qs, old + new, agent='Sonnet', pairs=[('sqlgen2+days', 'sql+days'),
      ('keel+query+days', 'sql+days'), ('sql+days', 'sql-log+days'), ('keel+query+days', 'sql-log+days'),
      ('sqlgen2+days', 'sql-log+days'), ('sql+days', 'sql'), ('sqlgen2+days', 'sqlgen2'),
      ('keel+query+days', 'keel+query'), ('sql-log+days', 'sql-log')])))
" > $R/hard-days-sonnet-stats.md
```

Result of the fifth addendum, 2026-11-03 (`results/hard-days-sonnet.jsonl`, `results/hard-days-sonnet-stats.md`):
1,280 runs, all on 3b99234 with `dirty: false`, no harness error, 13.84 USD. With the sentence Sonnet reads sql+days
99.7%, keel+query 99.4, sqlgen2 99.4, sql-log+days 99.1; 8 misses in all, 7 of them `entries`. The sentence moved no
condition by more than one question: sql −0.3 (0 / 1), sqlgen2 +0.3 (1 / 0), keel+query +0.3 (1 / 0), sql-log −0.3
(1 / 2). Paired, exploratory: sqlgen2 − sql and keel+query − sql −0.3 [−0.9, 0.0] (0 / 1); sql − sql-log +0.6
[0.0, +1.6] (2 / 0); keel+query − sql-log and sqlgen2 − sql-log +0.3 [0.0, +0.9] (1 / 0). `confirm_analysis` prints
H1 and H2 over the second addendum's rows with the Haiku rule's label, which does not apply. USD per correct answer:
sql 0.00905, keel+query 0.00955, sqlgen2 0.01123, sql-log 0.01370; input tokens per run 5,778, 10,712, 8,024 and 8,219.
Against Haiku with the sentence on the same interface, Sonnet is 0.8 to 2.5 points higher and costs 11 to 15 times as
much per correct answer (sql 15.3, keel+query 14.3, sqlgen2 11.8, sql-log 11.1).
