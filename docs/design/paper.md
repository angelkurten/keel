# Design note: the paper

Status: week 1 of 16 (plan at https://claude.ai/artifact/L1ZYTBKwJqx16z8uSKfyyZ). Decided 2026-10-20: a benchmark of our own over the Public Jira Dataset (MSR 2022, Zenodo, CC BY 4.0); five systems (Keel, RAG, the product's API as tools, Graphiti, Mem0); agents `openai/gpt-6-luna` and `anthropic/claude-haiku-5.5`; judge `deepseek/deepseek-v4-pro-0813` (to confirm); a preprint on arXiv in early February 2027; about 110–140 USD of API.

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
