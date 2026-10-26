# Why tenant-wide questions take Keel's agents five requests (Jira benchmark)

On the tenant-wide questions ("on date T, which issues of the INDY project had status S?", "how many had a status
among ...", "which were idle / blocked / urgent and unassigned", "whose priority changed between A and B", "as recorded
by K, which had status S at T") Keel's agents sent 5-10x SQL's input tokens in the compact arm of the A/B
(`results/ab-compact-compact.jsonl`, 72 questions x GPT-6 Luna and Claude Haiku 5.5): 5.0 requests per run on Luna and
3.4 on Haiku against SQL's 2.2, each request re-sending everything before it (context_tokens.md). Those 144 runs are
59% of Luna's input over the whole benchmark and 46% of Haiku's. This note says what the extra calls were for, what
this code changes so one call answers, and what that could save. No model is called. Reproduce (needs the questions
and logs in the data directory):

```bash
uv run --extra mcp --with tiktoken --with numpy python -m evals.jira.requests --data ~/datasets/public-jira/export
```

Everything below the summary is that script's output, unedited.

## Summary

**What the calls were for** (877 calls in 144 runs; section 1). Only 201 calls answered or led to the answer. The rest:

| class | calls (Luna / Haiku) | result tokens (Luna / Haiku) | what the agent did | what this code changes |
| --- | ---: | ---: | --- | --- |
| scope lookup | 115 / 58 | 79,769 / 11,621 | looked the project up (`search kind=project text=INDY`), tried `where={"project": "INDY"}` (empty), listed the project's 1,000+ issues (`related project:INDY in_project`) or checked each hit's project | the bound server's instructions name the subjects of a kind with at most three: `Kinds: issue, project (project:INDY).`; an empty `where` on a kind or relation name answers with a note naming `related_to='project:INDY', via='in_project'` |
| whole-kind listing | 29 / 28 | 243,337 / 168,590 | read every issue (`search kind=issue detail=brief limit=0`, 6-9k tokens) to filter by eye | descriptions: `where`, `count` and `key` up front; brief/concise "facts included" |
| per-subject check | 137 / 88 | 72,657 / 22,009 | after a listing that already answered: `context` per hit to confirm the status at T, or `history` per hit to measure "30 days in a status" (idle) | `where` hits carry `since` (when each filtered fact took its value): idle is one search; descriptions say brief/concise results need no `context` call; the instructions say `search(rule=)` lists where a rule holds |
| fan-out over values | 75 / 58 | 9,681 / 6,906 | one search per status ("a status among A, B, C, D, E": 5 counts) or per priority | `where={"status": [A, B, C]}`: any of them |
| repeat | 26 / 33 | 10,513 / 13,382 | the same search again with `related_to/via in_project` added (or the same history with `detail=full`) | the project named (the filter is visibly redundant with one project) |
| tenant history dump | 4 / 17 | 14,848 / 129,458 | the tenant's whole status history (~11k tokens) to check a set at T | the one-call paths below; nothing specific |
| error | 0 / 8 | 0 / 187 | `rule="in_project"`, `history(predicate="blocks")` | the note above; errors already name the valid rules |

Luna wastes more by looking up scope and fanning out (59 of its 72 runs touch the project; Haiku's 42); Haiku by
reading wholes (the whole kind, the whole history). On idle, only 2 of 14 runs used `rule="stalled"` (the question
says "idle"; the rule is named stalled): the rest measured 30 days by hand. The gap is not the size of one answer: the answering calls cost 87 o200k tokens on
average, the wasted calls 430k (Luna) and 352k (Haiku) tokens of results over 72 runs, each then re-sent with every
later request.

**What one call can do now** (section 3). Every tenant-wide question has a one-call answer through the five tools, and
this code's server reaches the gold on **72 / 72** with it (and idle also without the rule, from `since`: 7 / 7). Two of
the five paths needed this branch: "a status among" (a list in `where`; before, 4-5 parallel calls) and idle without
trusting the rule (`since` on hits; before, a history per hit). Two requests at this code's fixed part would cost ~2,300
Luna and ~4,600 Haiku input tokens per tenant-wide run, against 26,451 and 29,064 logged and SQL's 2,494 and 5,504.

**What to expect** (section 4). Replaying the logged runs without the targeted calls (requests cut in proportion) gives
4,032 Luna / 7,367 Haiku tokens per tenant-wide run (-85% / -75%), and -50% / -34% of all input per run over the
benchmark. That assumes every targeted call goes; the paid A/B says how many the agents actually drop.

**Changes in this branch for B** (each TDD'd; none breaks the contract, tests/contract.json unchanged):

1. `search(where={name: [v1, v2]})`: a list against a single value is any of them (before, never a match); list-valued
   facts match as before.
2. Brief and concise `where` hits carry `since: {predicate: valid_from}` for the facts the filter matched.
3. An empty `where` result whose name is a relation or a kind returns `[{"note": "... related_to='project:P', via='in_project'"}]`
   (with `detail="count"`, the note leads `{"count": 0}`).
4. A bound server's instructions name the subjects of a kind with at most three (as the server starts), and the
   rules line says `search(rule=)` lists where a rule holds.
5. With part A: shorter descriptions that put `where` / `[any of]` / `since` / `count` / `key` and "facts included: no
   context call needed" up front; `--read-only` drops annotate.

Not changed, and why: `count` returning the first keys too (count questions were answered by the count; the follow-up
listings were checks of fanned-out counts, which the list `where` removes); `more` hints (3 of 144 runs paged; the rest asked
`limit=0` or a large limit); a rule's own wording. **Open:** the eval cartridge's `urgent_unassigned`
definition says "priority Blocker or Critical" while its `when` (and the question) include Highest, so agents distrust the
rule and rebuild it by hand (11 of 14 runs searched priorities themselves, though 9 also listed the rule); fixing the wording is a
one-line change to `evals/jira/cartridge.yaml`, left out so the A/B measures the code alone.

**The paid A/B** (the main session's): the baseline is the compact arm already logged; the new arm is this branch with
`--keel-compact --keel-read-only` (bench builds its prompt from the server: current descriptions, the project named):

```bash
OFOX_API_KEY=$(cat ~/.ofox_key) uv run --extra mcp --with tiktoken python -m evals.jira.bench \
    --data ~/datasets/public-jira/export --systems keel --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 \
    --keel-compact --keel-read-only --out evals/jira/results/ab-requests.jsonl
# a pilot first: --types tenant (144 runs) shows the request count on the questions it targets
```

## 1. What the calls were for

Every tool call of the 144 tenant-wide runs of the compact arm, by what it did (tested in this order):

| class | what it is |
| --- | --- |
| error | a call that failed (a wrong argument, a rule name that is a relation) |
| scope lookup | the project looked up or each issue's project checked (search kind=project, where={project: ...}, related project:X/in_project) |
| repeat | a search or history call made again with only the project filter, the detail or the limit changed |
| fan-out over values | the same search once per value of one field (a status, a priority) |
| whole-kind listing | every issue of the tenant read (search kind=issue without a filter, brief, >1,500 tokens) |
| per-subject check | context or history of one hit after a listing that already answered (verification, or 30 days in a status) |
| tenant history dump | the tenant's whole history of a field (history without a subject) on a question that is not about changes |
| answer | the call that answered, or a step towards it |

| class | Luna calls | Haiku calls | Luna tokens of results | Haiku tokens of results | runs with one (both) |
| --- | ---: | ---: | ---: | ---: | ---: |
| error | 0 | 8 | 0 | 187 | 8 / 144 |
| scope lookup | 115 | 58 | 79,769 | 11,621 | 80 / 144 |
| repeat | 26 | 33 | 10,513 | 13,382 | 42 / 144 |
| fan-out over values | 75 | 58 | 9,681 | 6,906 | 45 / 144 |
| whole-kind listing | 29 | 28 | 243,337 | 168,590 | 55 / 144 |
| per-subject check | 137 | 88 | 72,657 | 22,009 | 53 / 144 |
| tenant history dump | 4 | 17 | 14,848 | 129,458 | 18 / 144 |
| answer | 102 | 99 | 50,784 | 48,578 | 142 / 144 |
| **all** | 488 | 389 | 481,589 | 400,731 |  |

By question type (both models), calls per class:

| type | runs | error | scope lookup | repeat | fan-out over values | whole-kind listing | per-subject check | tenant history dump | answer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| status_at | 36 | 0 | 53 | 9 | 0 | 10 | 28 | 3 | 45 |
| count_at | 24 | 2 | 33 | 22 | 76 | 16 | 2 | 4 | 37 |
| rule_at | 36 | 2 | 49 | 10 | 57 | 23 | 181 | 7 | 63 |
| changed | 24 | 1 | 6 | 15 | 0 | 0 | 1 | 0 | 26 |
| known_at | 24 | 3 | 32 | 3 | 0 | 8 | 13 | 7 | 30 |

## 2. Requests per run

Mean requests (model turns) per tenant-wide run, logged; SQL from results/full-sql.jsonl (3 repetitions). A run needs at least two: the call and the answer.

| type | Luna Keel / SQL | Haiku Keel / SQL |
| --- | ---: | ---: |
| status_at | 4.7 / 2.0 | 2.9 / 2.2 |
| count_at | 5.8 / 2.1 | 4.2 / 2.2 |
| rule_at | 6.6 / 2.1 | 4.0 / 2.4 |
| changed | 2.9 / 2.9 | 2.9 / 2.3 |
| known_at | 4.3 / 2.0 | 3.2 / 2.0 |
| **all** | 5.0 / 2.2 | 3.4 / 2.2 |

Requests beyond two: Luna 215 over 72 runs, Haiku 104 over 72 runs.

## 3. One call per question, with this code

Each tenant-wide question asked once through a compact, read-only MCP server bound to its tenant (this code), with the call below; the answer read off the result by a fixed reader and compared with the gold.

| type | the call |
| --- | --- |
| status_at, known_at | `search(kind="issue", where={"status": S}, as_of=T[, as_known_at=K], detail="key", limit=0)` |
| count_at | `search(kind="issue", where={"status": [the statuses the question lists]}, as_of=T, detail="count")` (new: a list is any of them) |
| rule_at | `search(rule=R, as_of=T, detail="key", limit=0)`; idle also without the rule: `search(kind="issue", where={"status": [...]}, as_of=T, detail="brief", limit=0)`, kept where `since.status` is 30 days or more before T (new: since on where hits) |
| changed | `history(since=A, until=B, predicate=F, limit=0)`, the subjects of entries with `was` |

Gold reached: **72 / 72** with one call each; idle without the rule, from `since`: 7 / 7.

Cost of that path against the logged runs, per run: two requests, each carrying the fixed part this code sends (bench --keel-read-only: current descriptions, four tools, the server's instructions), the second also the call and its result. o200k; provider tokens weighed by each model's compact-arm calibration (rf for the fixed part, rc for the conversation). Logged: the compact arm's provider input, and SQL's.

| type | Luna logged | Luna one call | Luna SQL logged | Haiku logged | Haiku one call | Haiku SQL logged |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| status_at | 16,990 | 2,233 (-87%) | 2,013 | 15,263 | 4,513 (-70%) | 4,402 |
| count_at | 33,168 | 2,194 (-93%) | 2,144 | 50,806 | 4,446 (-91%) | 4,309 |
| rule_at | 49,749 | 2,292 (-95%) | 2,253 | 41,529 | 4,636 (-89%) | 7,111 |
| changed | 5,840 | 2,568 (-56%) | 4,280 | 11,530 | 5,024 (-56%) | 7,439 |
| known_at | 19,592 | 2,315 (-88%) | 2,140 | 26,863 | 4,653 (-83%) | 4,005 |
| **all** | 26,451 | 2,311 (-91%) | 2,494 | 29,064 | 4,641 (-84%) | 5,504 |

The one-call path's result tokens (o200k, mean): 87; its fixed part 1,285 per request.

## 4. The logged runs without the calls these changes target

The one-call path is what the tools now allow, not what an agent will do. Closer to the logs: each logged run replayed without its calls of the classes this code targets (scope lookup, repeat, fan-out, whole-kind listing, per-subject check, tenant history dump), its other calls and answer kept, its requests cut in proportion to the calls removed (at least two), with this code's fixed part. Provider tokens by the same calibration.

| type | Luna logged | Luna without the targeted calls | Haiku logged | Haiku without the targeted calls |
| --- | ---: | ---: | ---: | ---: |
| status_at | 16,990 | 3,899 (-77%) | 15,263 | 6,012 (-61%) |
| count_at | 33,168 | 4,013 (-88%) | 50,806 | 6,511 (-87%) |
| rule_at | 49,749 | 5,310 (-89%) | 41,529 | 10,086 (-76%) |
| changed | 5,840 | 3,058 (-48%) | 11,530 | 5,985 (-48%) |
| known_at | 19,592 | 3,307 (-83%) | 26,863 | 7,561 (-72%) |
| **all** | 26,451 | 4,032 (-85%) | 29,064 | 7,367 (-75%) |

What that is over the whole benchmark (387 questions; tenant-wide are 72 of them), mean logged input per run:

| model | all questions, logged | tenant-wide share of it | with tenant-wide runs without the targeted calls |
| --- | ---: | ---: | ---: |
| Luna | 8,279 | 59% | 4,108 (-50%) |
| Haiku | 11,740 | 46% | 7,704 (-34%) |

It still assumes every targeted call goes away: an upper bound on what these changes do through the agent, which the paid A/B measures. The tenant-wide rows include this code's fixed part; the other questions gain it too (context_tokens.md section 5), not counted here.

