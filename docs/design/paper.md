# Design note: the paper

Status: week 1 of 16 (plan at https://claude.ai/artifact/L1ZYTBKwJqx16z8uSKfyyZ). Decided 2026-10-20: a benchmark of our own over the Public Jira Dataset (MSR 2022, Zenodo, CC BY 4.0); five systems (Keel, RAG, the product's API as tools, Graphiti, Mem0); agents `openai/gpt-6-luna` and `anthropic/claude-haiku-5.5`; no judge (answers are structured and graded by exact match; decided in week 5); a preprint on arXiv in early February 2027; about 110–140 USD of API.

## What is not ours to claim

Related work already has these, so the paper cites them and builds on them:

- **A bitemporal store an agent can ask "what did we believe on date X"**, with retract kept apart from correct: TGMS (arXiv 2607.10265, Apache-2.0), Zep/Graphiti (arXiv 2501.13956), TOKI (arXiv 2606.06240).
- **An append-only log as the truth, replayed to an identical state across backends**: TGMS (Kùzu and DuckDB, byte-identical).
- **No LLM where facts become true; deterministic, bounded operators; MCP**: TGMS.
- **Fewer prompt tokens because structure stays out of the context**: TGMS (about 6.5k tokens per task against 32k for long-context RAG).

## The thesis

Agents inside a product should read the product's own events, compiled deterministically, instead of facts an LLM extracted from text. Every memory system we found builds memory from conversation; none builds it from a product's structured event log, and no study compares the two on cost, fidelity and reproducibility. The evidence so far is indirect: write-time extraction misses or garbles facts (HaluMem, arXiv 2511.03506: extraction recall under 60%; Ground Truth First, arXiv 2607.21962: questions backed by badly written facts fail 24.2% of the time against 1.6%), construction is slow (MemoryAgentBench: Mem0 up to 18,483 s to build memory, BM25 0.1 s), and LLM writers drift on replay (TOKI). Agents also find the new fact and still answer from the stale one (STALE, arXiv 2605.06527: Zep 6.0%, Mem0 8.3%).

## Claims and the experiment behind each

**C1. Deterministic ingest of product events is cheaper, more faithful and reproducible where LLM extraction is not.**
E1: ingest the same Jira events into Keel, Graphiti and Mem0 (extractor: GPT-6 Luna; one ablation with a stronger extractor). Per 1k events: LLM calls, tokens, dollars, wall time, failures and retries. Write fidelity: precision and recall of stored facts against the changelog, and the rate of facts no event supports. Determinism: ingest twice and once in shuffled arrival order, and diff the graphs. Keel's side needs no model; kagent's numbers (101k events in 8 s on MySQL) set the scale.

**C2. That fidelity carries into answers, most of all on time.**
E2: about 300 questions generated from the changelog, gold computed by replay, in five types: current state, as of a valid time, as known at a recorded time (with injected late and corrected events), multi-hop over issue links, and rule verdicts (blocked, waiting, inherited pause, the rules kagent uses). Plus abstention questions and a closed-book baseline, since public Jira may be in the models' training data. Structured answers (status, assignee, dates, sets of issue keys) are graded by exact or set match; the judge grades only free text. Systems without a recorded-time axis are reported as "not supported" on as-known-at, not scored as failures.

**C3. One domain-agnostic core serves different products, with the domain declared once.**
E3: the cartridges in `examples/` (fleet 193 lines, crm 85, finance 142, clinics 77, relay 219), the Jira cartridge and kagent in production, each with zero changes under `keel/`. E4: TGMS's correction-probe pairs on its own data (SNAP communication networks) through a cartridge, against TGMS's published numbers. Only the probes are comparable; Keel has no motif or reachability operators.

**C4. Agents can write memory into the graph under policy the server enforces.**
TGMS leaves agent write-back open. Keel ships scoped `annotate` (global, tenant, task, agent), TTL and evidence rules, a per-agent registry, isolation per tenant, and annotations that survive replay. E5: property tests and an adversarial agent that tries to read another tenant, another agent's memory, or a kind it is not registered for; leakage reported as a count (expected 0).

**C5 (secondary). How tool results are shaped is a cost and accuracy lever.**
Already measured (`evals/efficiency`, 0.31.0): tool-result tokens 45.2k to 12.3k on a fixed call set; Sonnet accuracy 0.90 to 0.98; DeepSeek Flash calls 10.4 to 5.7. In the paper: the brief/full and capped/uncapped ablations inside E2, framed as complementary to TGMS.

## Evaluation protocol

- **Gold from the log, never from a model.**
- **Judge**: a family neither agent belongs to. Two people label a stratified 20% per question type; report Cohen's kappa and judge-human agreement. Plant wrong answers, stale values on as-of questions among them, and report how many the judge accepts. LongMemEval's rule that a knowledge-update answer is correct while it still mentions the stale value does not apply.
- **Statistics**: 3 runs per agent, system and type; bootstrap 95% intervals; paired tests.
- **Fair baselines**: same extractor model for Graphiti and Mem0. Graphiti gets JSON episodes, the Jira ontology as Pydantic types, `reference_time` = `occurred_at`, one group per tenant, and we wait for its background processing before asking. Mem0 gets one `user_id` per tenant and timestamps as metadata. Versions pinned; configs and harness published; Zep and Mem0 get a review window before the preprint (the lesson of their LoCoMo dispute).
- **Read side**: accuracy per type, tokens per answer (each model's tokenizer), tool calls and turns, p50/p95 latency from the same host for every system.

## Open

- The judge (DeepSeek V4 Pro 0813) is not confirmed.
- E1's write-fidelity scoring for Graphiti and Mem0 means mapping their stored facts back to changelog fields; it may add a week to the harness (weeks 5–7).
- E4 depends on TGMS's repo running as published.
- kagent promised `keel usage` around 2026-11-08: real usage for C3.
- **Transaction time from the envelope (decided 2026-10-20, core change).** Until now stores stamped `recorded_at`, `closed_at` and link times with the ingest clock, so replay did not rebuild when Keel learned things and late arrivals could not be simulated. Now an event that declares `recorded_at` writes with it (the Ingestor wraps the store its handler gets); `changed_at`, a new ingest-clock column, carries the change feed and the watermark so a late event is still a new change. Determinism (E1) can now compare whole graphs, transaction time included; only `changed_at` and row ids differ between runs.

## Week 2: the Jira cartridge and adapter

`evals/jira/`: `cartridge.yaml` and `handlers.py` (issues, projects, six link relations, three rules: blocked, stalled, urgent_unassigned; nulls are facts, so a rule can test "nobody assigned"), `adapt.py` (issues with changelogs in Jira's REST shape, JSON or the dataset's BSON, to events; values named by id as they are today, links recorded from their outward end, moved issues under their final key) and `check.py` (replay must end where Jira is today). On kagent's 359 cards: 2,341 events, no mismatch with Jira's current fields, the same file byte for byte on a second run.

The tenants, one whole project per Jira instance (whole projects keep links inside the tenant), restored from the dataset's `mongodump` with `mongorestore --nsInclude` and exported with `mongoexport --jsonArray`:

| Tenant | Project | Span | Issues | Events | Links |
| --- | --- | --- | --- | --- | --- |
| hyperledger | INDY (Hyperledger Indy) | 2017-04 to 2020-11 | 2,323 | 18,036 | 1,590 (relates, blocks, clones, duplicates) |
| mariadb | MXS (MaxScale) | 2014-01 to 2021-01 | 3,333 | 17,934 | 805 (relates, blocks, duplicates) |
| spring | SEC (Spring Security) | 2005-06 to 2016-03 | 3,150 | 15,699 | 630 (relates, duplicates, depends on) |

All three: no mismatch with Jira's current fields, and the same graph, transaction time included, in log order, reversed, and three shuffles (memory and SQLite in batches). Ingest takes 0.1–0.2 s per tenant in memory.

Two core bugs surfaced on the way and are fixed, each with a conformance test: a close took the arriving fact's `recorded_at` instead of the later of the two (so a successor arriving first gave a different `closed_at`), and facts starting at the same instant (INDY-1393 moved to "To Test" twice in one second) were ordered by arrival, which left two open statuses. A predicate's history is now ordered by `valid_from`, then `recorded_at`, then evidence and value.

## Week 4: questions and gold answers

`evals/jira/questions.py` fills templates with a fixed seed (7) and computes every gold answer with its own oracle, a walk over the log that imports nothing from Keel. 105 questions per tenant, 315 in all:

| Type | Per tenant | What it asks | Gold |
| --- | --- | --- | --- |
| current | 20 | a field's value now | value |
| as_of | 25 | a field's value at an instant inside its history; in 80% the value is no longer current, so today's value does not pass for remembering | value |
| known_at | 25 | a field's value at an instant, as recorded by a later one, over the injected windows, before and after the record arrived | value |
| links | 15 | of the issues linked to one by a relation at an instant, which were done (or not) then | sorted list of keys |
| rule | 15 | blocked, stalled or urgent and unassigned at an instant, the definition in the question; yes and no balanced; Spring SEC has no blocks links, so its share goes to the other two | bool |
| abstain | 5 | an issue that does not exist, or a field the tracker does not keep | unknown |

Instants are whole minutes at least an hour from any change the question reads. `None` as a value means none (unassigned, unresolved). Statuses whose category no current issue shows (a migrated workflow: Spring's Open, Resolved, Reopened...) take it from a declared table of Jira's standard workflows (`FALLBACK_CATEGORIES` in adapt.py); without it, 61% of Spring's status events had none and its rules could not be evaluated over history.

`--check` answers every question with Keel's primitives directly, no agent: 315 of 315 agree. That is the ceiling an agent with Keel can reach, and the oracle and Keel checked against each other. Regenerating gives the same file.

### Human review (2026-10-20)

Angel reviewed the 50-question sheet (`review-50.md`, stratified by tenant and type, drawn from the final questions) against the issues' history: all 50 ok.

### Review by independent judges (2026-10-20)

Three agents, one per tenant, rebuilt the gold answer of 50 stratified questions (17, 17, 16) from the raw Jira export with code, never from the event log, so a bug shared by the adapter, the oracle and Keel could show. 46 ok, 3 ambiguous, 1 wrong. Fixed:

- **Wrong (an adapter bug):** Jira can hold the same link twice (INDY-2293 linked INDY-2292 twice and removed it once; 58 duplicate adds in Hyperledger alone). The adapter treated links as a set, so one removal dropped a link that still existed. It now counts adds and removes, as kagent's exporter did. `check.py` now also compares each issue's open links with its current `issuelinks` (relates_to without direction, since Jira shows it outward on one end): no mismatch in the three tenants, 4 with the old adapter.
- **Ambiguous (rule wording):** "stalled when it is in progress" tested the indeterminate category, which includes Confirmed (MariaDB) and Waiting for Feedback (Spring); MariaDB also has a status named Stalled. Rule and link questions now list the tenant's statuses that count as done or in progress, and the stalled rule is asked as "idle".
- **Abstentions on fields the raw tracker may hold** (sprint, due date: MariaDB has 2,129 sprint changes) were unfair to systems that read raw Jira. Abstentions are now issues that never existed and issues asked about before they were created.
- **Link questions** whose linked issues were all in one category made the done/not-done filter moot; now 80% have a mix.

To state in the paper: a correction window's gold answer before the correction arrives is the injected wrong value, a value that never existed in Jira, by design. Jira's "includes" / "is part of" links map to relates_to. An issue moved to another project keeps its old key in the changelog and its new one in `issuelinks`, so a link to it can split across two keys; links outside the tenant are not asked about.

Round two (24 new link, rule and abstain questions, same method): 24 ok; the three fixes hold. One more change from it: link questions ask only about the tenant's own issues (one asked about MDEV-20119, reached only through MXS issues' links).

## Week 5: the harness (Keel, RAG, the product's API, closed book)

`evals/jira/bench.py`. Every system reads the same source, the tenant's log as written (each event with when it happened and when it was recorded, corrections included), in its best ordinary form: Keel's five tools through a real MCP session; RAG with one text record per event, both times in it, hybrid search (dense `text-embedding-3-small` at 512 dimensions plus BM25, fused by RRF) and an optional issue-key filter; a Jira-like API (`get_issue`, `get_changelog` with both times, `search_issues`); and no tools at all, a contamination control. Same model, same prompt but for the paragraph about the tools, temperature 0. The agent ends with `ANSWER: <json>`, graded by exact match after normalising (case, none/unassigned, sets of keys, yes/no), so **no judge model is needed**: every answer in this benchmark is structured. That drops the judge's cost and its bias, and the judge-validation step shrinks to checking the normaliser.

Smoke run (Hyperledger, one question per type, 2026-10-20): Haiku 5.5 and DeepSeek V4.1 Flash 1017, 48 runs, 0.17 USD, every answer parseable. Keel, RAG and the API all answered the six correctly; closed book got only the abstention, so the models do not know these trackers from training. Six questions separate nothing; the differences will be in the full run and in cost (DeepSeek on RAG read 43k input tokens per question, on Keel 15k, on the API 3k).

`openai/gpt-6-luna` answers 401 through OfoxAI since 2026-10-20 (the gateway's OpenAI account is deactivated; embeddings still work). Agents: to be decided.

### Graphiti and Mem0 (2026-10-20)

`evals/jira/memories.py`, pinned to releases at least two weeks old: graphiti-core 0.30.2 (0.31.x came out 2026-10-15 and 10-20) and mem0ai 2.2.1 with its `extras` (BM25 on). Configured as their guides say for an OpenAI-compatible provider (checked against their current docs): Graphiti with `OpenAIGenericClient` on Neo4j 5.26 (its Kuzu backend is deprecated and fails in 0.30.2); `json_object` structured output made Haiku add text after the JSON (5 failed calls in 29), the default `json_schema` had none. Mem0 with its openai LLM and embedder pointed at the gateway and Qdrant embedded on disk; its OSS SDK refuses a `timestamp`, so times travel in the text and metadata, as its guide does for dates.

Ingest cost, measured per event with Haiku 5.5 as the extractor (12 events each):

| | LLM calls | Input tokens | Output tokens | Seconds |
| --- | --- | --- | --- | --- |
| Graphiti | 3.3 | 9,984 | 432 | 11.0 |
| Mem0 | 1.0 | 13,398 | 427 | 5.9 |
| Keel | 0 | 0 | 0 | ~0.00001 (51,669 events in ~0.5 s) |

The whole log through Graphiti would be ~134 hours and ~46 USD. Both ingest the subset the questions touch (asked issues and their linked ones: 2,321 + 1,122 + 938 = 4,381 events), about 12 USD and 7 hours with the three tenants in parallel. That subset is less noise than the other systems read, which favours Graphiti and Mem0; `bench.py --subset` gives every system the same subset for a like-for-like table. Both systems run through the harness end to end (`search_memory`).

**Agents confirmed (2026-10-20): `openai/gpt-6-luna` and `anthropic/claude-haiku-5.5`**, no judge (exact match). Luna answered again through OfoxAI the same evening; it is also the extractor for Graphiti and Mem0. Luna smoke run (Hyperledger, six questions): every answer parseable; on Keel 1.8 tool calls and 5.6k input tokens per question.

**Pilot finding (2026-10-20):** the blocked-rule question defined "blocked while an issue that blocks it is not done" and left out that the issue itself must not be done, which the rule and the gold require. Haiku on Keel answered MXS-1270 (closed at the time, its blocker open) by the definition as written, and said in its reply that Keel's rule evaluated false and why. The wording now states both conditions; the ten blocked questions keep their ids and gold, and their pilot runs are redone.

**Pilot findings to act on before the full run (2026-10-20):**
- The API baseline's changelog lacked each issue's initial values (Jira's history shows them), so the API agent answered unknown to as_of questions whose answer was the initial status. Fixed (`4d782cd`); the API runs were redone.
- Keel rejects times written as in the questions ("2016-09-14 07:38 UTC"): 3 of Haiku's 172 Keel calls failed on it, each costing a turn before the agent sent ISO 8601. Decided with Angel: accept that form in the core after the pilot and before the full run, rerun Keel there, and say so in the paper.

### A sixth type: questions over the whole tenant (2026-10-20)

The pilot with Haiku on MariaDB saturated: Keel, RAG and the API each answered 105 of 105 (closed book 0 of 90, all "unknown"), and the API was the cheapest (3.2k input tokens a question against Keel's 7.5k; Keel the fastest, 7 s against 17 s). Every question so far names one issue, and for that a per-issue API is enough. Agents inside a product also ask about the whole project at once ("what is blocked today?"), a category the benchmark lacked; it is added, and the one-issue questions stay and are reported as they are.

`tenant`, 24 per tenant, appended last so the 315 earlier questions keep their text and gold (checked): which issues had a status at an instant (6), how many were in progress (4, a count), where a rule held (6), which changed a field in a window (4), and which had a status as recorded by a later instant (4, over an injected window). Instants keep an hour from every change in the tenant; set answers hold 1 to 41 keys. Keel's primitives agree on all 72 (`search(rule=, as_of=)` for the rule listings).

Fairness: the API gets what Jira's JQL offers for history, `<field> WAS <value> ON <instant>` and `<field> CHANGED DURING (<from>, <until>)`, as filters on `search_issues`; with them it answers the 42 status, count and change questions exactly (checked without an agent). What neither the API nor Keel has a primitive for is reported as found: the API has no rule evaluation and no transaction time, and Keel's `search` has no `as_known_at` and no tenant-wide listing of changes in a world-time window.

### Core changes after the pilot (2026-10-20, decided with Angel)

From the pilot's Keel transcripts: agents copied times as the questions write them ("2016-09-14 07:38 UTC"), which Keel refused (3 of 172 calls); on tenant-wide questions they began by listing every issue with its facts (`search(kind="issue", detail="brief", limit=0)`, 80k to 250k tokens) to count or to orient, then made the right call (`search(where=..., as_of=...)`, 8 to 120 tokens); and `search` had no `as_known_at`, so a set "as recorded by K" was approximated. Changes, each with a conformance test, all reruns of Keel to come in the full run:

- Every time argument takes a trailing " UTC".
- `search(detail="count")` returns `[{"count": n}]`, no cap, nothing listed.
- `search(as_known_at=...)`, as `context` and `history` have it.
- A deployment's ceiling on one listing (`max_list`, `keel serve --max-list`), off by default: a report or an export may want everything, and the operator decides; the paper measures Keel with it off.
