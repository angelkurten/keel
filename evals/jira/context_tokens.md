# Where Keel's input tokens go (Jira benchmark)

The compact MCP text cut Keel's tool-result tokens ~36% but its input tokens only ~10% (A/B:
`results/ab-compact-{plain,compact}.jsonl`, 387 questions x GPT-6 Luna and Claude Haiku 5.5, one repetition, at
1c58601). Keel still sends 3.7x (Luna) and 2.3x (Haiku) the input tokens of the hand-written SQL baseline
(`results/full-sql.jsonl`) and costs 3.2x and 1.6x per correct answer, at the same accuracy (97-99%). This note
measures the rest of the input: the fixed part every request carries, and the conversation each request re-sends.
No model is called. Reproduce (needs the questions in the data directory):

```bash
uv run --extra mcp --with tiktoken --with numpy python -m evals.jira.context_tokens --data ~/datasets/public-jira/export
```

Everything below the summary is that script's output, unedited.

## Method, in short

- **Fixed part**: built as `bench.ask` builds it: bench's system prompt (frame, answer format, the system's tool
  paragraph; for Keel that is bench's Keel line plus `mcp_server.instructions()`), the tool list exactly as bench
  sends it (name, description, `inputSchema`; MCP annotations are dropped), and the question. Tokenized with o200k
  as bench and `tokens.py` do.
- **Per run**: request k = fixed part + every earlier assistant turn (its calls' names and arguments, plus whatever
  else the model wrote, from the logged output) + every earlier tool result (logged per call). The log keeps the
  calls' order but not which turn made each parallel call; the rebuild takes the two bounds (extras on the first or
  last tool turn) and blends them by the weight that matches the logged totals. Luna's parallel calls come out on
  its last tool turn; Haiku's in between.
- **Calibration**: the providers count their own tokens and render tool definitions their own way. On the runs whose
  shape is known (one call, then the answer), logged = 2 x rf x fixed + rc x conversation gives each provider's
  weight per o200k token: rf for the fixed part, rc for the conversation. With it, the rebuild matches the logged
  input per run to ~2% MAPE (raw o200k: 14-16% on Luna, ~40% on Haiku) and in total to within 1.2%.
- **Cost**: Luna at the gateway's prices, which the logged costs fit exactly (0.10 / 0.50 $ per M; every prompt
  token billed fresh). Haiku went straight to Anthropic with caching on (bench's `to_anthropic(direct=True)`: a
  breakpoint after the system prompt and one at the end); its logged effective input price is 0.036-0.040 $/M
  against 0.10 list, so most of its prompt is already cache reads. A cache model (tools+system prefix read from the
  cache, each request reading the previous prompt; reads 0.1x, writes 1.25x) reproduces Haiku's logged input cost
  within 3-6%, and prices the options for Haiku.

## Summary

**Fixed part per request** (o200k; provider tokens in brackets, Luna / Haiku):

| | Keel compact | SQL |
| --- | ---: | ---: |
| bench frame + answer format + question | 169 | 169 |
| system's own text (Keel line + MCP instructions / SQL schema and macros) | 246 | 695 |
| tool definitions | 1,257 (5 tools: descriptions 553, schemas 624) | 83 (1 tool) |
| **total** | **1,672 (1,419 / 2,894)** | **946 (962 / 1,683)** |

Keel's prose is shorter than SQL's (246 against 695): the difference is entirely the five tool definitions, 75% of
Keel's fixed part. Search alone is 391 tokens, annotate (never called in this eval) 195. Keel's parameters carry
no descriptions; the schemas are mostly structure (`anyOf` for str | list, enums, defaults). Luna renders JSON tool
definitions tighter than o200k counts them (~0.78x); Claude counts everything heavier (rf ~1.75, rc ~1.45).

**Per run, Keel compact against SQL** (logged input tokens):

| | Luna | Haiku |
| --- | ---: | ---: |
| Keel compact / SQL | 8,279 / 2,235 | 11,740 / 5,026 |
| gap | 6,044 | 6,714 |
| of which: fixed part x requests | 32% | 50% |
| of which: tool results (first sent and re-sent) | 62% | 45% |
| of which: assistant turns | 6% | 5% |
| requests per run, Keel / SQL | 2.82 / 2.11 | 2.46 / 2.24 |
| share of Keel's input that is re-sent (fixed part of requests 2..n, earlier results and turns) | 60% | 52% |

- The **re-sending** is most of it: more than half of Keel's input is content the model already saw in an earlier
  request. Every extra request re-sends the fixed part (1,419 Luna tokens) and every result so far.
- The **long tail** carries the gap. On Luna, the 17% of runs with 4 or more requests take 61% of the input; on
  Haiku the 33% with 3 or more take about 60%. By type, Keel is 1.5-2.6x SQL on single-issue questions and **10.6x
  (Luna) / 5.3x (Haiku) on the tenant-wide ones** (5.0 requests per run against 2.2), where SQL answers in one
  aggregate query and Keel's agent pages, fans out and re-reads.
- **No rule text is repeated**: context's `evaluation` carries only names and booleans; the definitions are said once,
  in the instructions (SQL's prompt carries them too). The only repetition found is small: `'<kind>:<id>'` is said
  by bench's Keel line, the instructions' first line and context's description (~10 tokens).

**Reduction options, ranked** (compact arm, per run; tokens are provider tokens; $ in micro-dollars, run cost is
961 Luna / 691 Haiku):

| # | option | Luna tokens | Luna $ | Haiku tokens | Haiku $ | where / contract | accuracy risk |
| ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | Prompt caching on Luna's route (cached input at 0.1x; 0.25x: 481; 0.5x: 320) | 0 | **577** | (already cached) | 0 | deployment/gateway; no code | none |
| 2 | Fewer requests: every run in at most 2 (upper bound) | **3,600 (43%)** | 360 | **3,261 (28%)** | 33 | agent behaviour, steered by descriptions / richer one-call answers for tenant-wide questions (e.g. `search(detail="count")`, `where` at `as_of`) | high if forced; the realistic target is the tenant-type tail |
| 3 | Compact text as default (measured; in progress in parallel) | 990 (12%) | 85 | 1,153 (10%) | 95 | core default flip; no schema change | measured on par |
| 4 | All four tool-list trims together (5, 6, 7 and 10: 542 o200k per request) | 1,298 (16%) | 130 | 2,310 (20%) | 23 | core; breaking parts need a deprecation | low-medium |
| 5 | Shorter tool descriptions (wordings in the script: 553 -> 293 o200k) | 632 (8%) | 63 | 1,125 (10%) | 11 | core `DESCRIPTIONS`; **no contract change** (prose is not in contract.json) | low-medium |
| 6 | Hide annotate from a read-only agent's tool list (registry: no write scope) | 465 (6%) | 47 | 827 (7%) | 8 | core `mcp_server`; **additive** (refresh only if the snapshot sees it; default lists all five) | none here (never called) |
| 7 | Drop never-used parameters (context.task/exclude, history.task/changed_since, search.mode/min_score/depth/per_kind/exclude) | 227 (3%) | 23 | 405 (3%) | 4 | core signatures; **breaking: deprecation first** (SPEC §11) | none here; other agents lose features |
| 8 | Drop the rules from the instructions | 199 (2%) | 20 | 354 (3%) | 4 | core instructions; prose | high (rule questions) |
| 9 | Drop the TABLES note (compact) | 165 (2%) | 17 | 294 (3%) | 3 | core instructions; prose | medium-high (how tables are read) |
| 10 | Drop schema defaults | 115 (1%) | 12 | 205 (2%) | 2 | core `_compact_schema`; **breaking** in the inputSchema snapshot | low |
| 11 | Drop the relations line | 117 (1%) | 12 | 209 (2%) | 2 | core instructions; prose | high (links questions) |
| 12 | Trim bench's frame (40%) | 100 (1%) | 10 | 177 (2%) | 2 | harness only | low; SQL gains the same, ratio unchanged |

Reading it:

- **Cost** on Luna is dominated by billing, not by Keel: if the route honoured OpenAI-style cached input, Keel's
  input cost per run would drop from ~838 to ~261 micro$ (SQL's from 224 to 42), more than every token trim together.
  On Haiku caching is already on, so trimming the fixed part saves tokens but little money (a cached token costs
  0.1x).
- **Tokens**: the non-breaking pair (5 + 6) takes 415 o200k off every request, 12% of Luna's and 15% of Haiku's input
  per run, with no contract change; with the breaking parts (7, 10, after a deprecation) 16% / 20%. Even then Keel's
  fixed part (1,130 o200k) stays above SQL's (946): the remaining gap is results and requests.
- **The largest lever is the number of requests on the tenant-wide questions**, which is about what the tools can
  answer in one call, not about wording. It is a core-semantics question (richer `search`/`history` answers within
  the five tools, rule 7) and needs its own A/B before anyone claims it.

## 1. The fixed part of every request

Sent with every request of a run, o200k tokens, mean over the runs (the question and answer format vary).

| component | keel-plain | keel-compact | sql |
| --- | ---: | ---: | ---: |
| bench frame | 103 | 103 | 103 |
| answer format | 31 | 31 | 31 |
| Keel line (bench) | 31 | 31 | 0 |
| MCP instructions | 147 | 215 | 0 |
| SQL schema+macros text | 0 | 0 | 695 |
| tool definitions | 1,257 | 1,257 | 83 |
| question | 35 | 35 | 35 |
| **total (whole, tokenized together)** | 1,604 | 1,672 | 946 |

Keel's MCP instructions, line by line (compact adds the TABLES note):

| line | tokens |
| --- | ---: |
| Subjects are '<kind>:<id>'. Kinds: issue, project. | 15 |
| Relations: blocks (issue->issue), clones (issue->issue), depends_on (issue->issue), duplic... | 49 |
| Rules, evaluated in context's `evaluation` (trust it): | 13 |
| - blocked (issue): An issue is blocked while an issue that blocks it is not done. | 19 |
| - stalled (issue): An issue has stalled when it is in progress and its status has not chan... | 25 |
| - urgent_unassigned (issue): An urgent issue (priority Blocker or Critical) that is not do... | 26 |
| Lists of records come as tables: {"all": {fields every record shares}, "cols": [...], "row... | 68 |

Keel's five tool definitions (as bench sends them: name, description, JSON input schema; no MCP annotations):

| tool | description | input schema | whole definition | parameters |
| --- | ---: | ---: | ---: | ---: |
| context | 116 | 121 | 253 | 8 |
| history | 149 | 110 | 276 | 10 |
| search | 172 | 202 | 391 | 16 |
| annotate | 59 | 119 | 195 | 10 |
| related | 57 | 72 | 145 | 5 |
| **all five** | 553 | 624 | 1,257 | 49 |

SQL's one tool: description 34, schema 31, whole 83. Keel's tool parameters carry no descriptions (FastMCP schemas, compacted by `_compact_schema`): every word an agent reads about a parameter is in the tool description.

Each parameter's schema tokens and how often the agents passed it (both A/B arms, both models: 3,722 calls):

| parameter | schema tokens | calls passing it | share of the tool's calls |
| --- | ---: | ---: | ---: |
| context.subject | 22 | 1,417 | 98% |
| context.as_of | 6 | 1,147 | 80% |
| context.depth | 9 | 113 | 8% |
| context.as_known_at | 7 | 273 | 19% |
| context.task | 5 | 0 | 0% |
| context.detail | 21 | 1,235 | 86% |
| context.exclude | 12 | 0 | 0% |
| context.only | 12 | 299 | 21% |
| history.subject | 5 | 797 | 85% |
| history.since | 5 | 125 | 13% |
| history.until | 5 | 481 | 51% |
| history.predicate | 5 | 835 | 89% |
| history.scope | 16 | 55 | 6% |
| history.as_known_at | 7 | 291 | 31% |
| history.task | 5 | 0 | 0% |
| history.changed_since | 6 | 0 | 0% |
| history.limit | 9 | 307 | 33% |
| history.detail | 20 | 599 | 64% |
| search.text | 25 | 201 | 19% |
| search.kind | 22 | 940 | 88% |
| search.rule | 5 | 70 | 7% |
| search.limit | 5 | 430 | 40% |
| search.after | 5 | 7 | 1% |
| search.mode | 16 | 0 | 0% |
| search.min_score | 6 | 0 | 0% |
| search.related_to | 6 | 390 | 36% |
| search.via | 5 | 387 | 36% |
| search.detail | 25 | 1,066 | 100% |
| search.depth | 9 | 3 | 0% |
| search.per_kind | 6 | 0 | 0% |
| search.exclude | 12 | 0 | 0% |
| search.as_of | 6 | 852 | 80% |
| search.where | 5 | 641 | 60% |
| search.as_known_at | 7 | 95 | 9% |
| annotate.subject | 5 | 0 | - |
| annotate.predicate | 5 | 0 | - |
| annotate.value | 3 | 0 | - |
| annotate.scope | 20 | 0 | - |
| annotate.evidence | 6 | 0 | - |
| annotate.by | 5 | 0 | - |
| annotate.task | 5 | 0 | - |
| annotate.key | 5 | 0 | - |
| annotate.status | 14 | 0 | - |
| annotate.detail | 16 | 0 | - |
| related.subject | 5 | 277 | 100% |
| related.relation | 22 | 213 | 77% |
| related.as_of | 6 | 233 | 84% |
| related.depth | 9 | 185 | 67% |
| related.limit | 9 | 119 | 43% |

Never or almost never passed (5 calls or fewer), besides annotate (never called: 0 calls): context.task, context.exclude, history.task, history.changed_since, search.mode, search.min_score, search.depth, search.per_kind, search.exclude (77 schema tokens).

## 2. Per run: fixed part, re-sent conversation, and the rebuild against the logged tokens

A run's input is the sum of its requests; request k carries the fixed part plus every earlier assistant turn and tool result. Rebuilt from the log (o200k), mean per run. The log does not say which turn made each parallel call; the two bounds put the extras on the last tool turn or on the first, and the rebuild blends them by the weight w that makes the calibrated runs with parallel calls sum to their logged input (Luna: the extras sit on the last tool turn; Haiku: in between). Assistant turns: each call's name and arguments, plus the output the model wrote besides its calls and its final reply (logged output minus those), spread over its tool turns.

| per run | keel-plain Luna | keel-plain Haiku | keel-compact Luna | keel-compact Haiku | sql Luna | sql Haiku |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| requests (turns) | 2.84 | 2.42 | 2.82 | 2.46 | 2.11 | 2.24 |
| tool calls | 2.36 | 2.40 | 2.48 | 2.37 | 1.11 | 1.75 |
| fixed, first request | 1,604 | 1,604 | 1,672 | 1,672 | 946 | 946 |
| fixed, re-sent (requests 2..T) | 2,959 | 2,272 | 3,049 | 2,446 | 1,049 | 1,174 |
| tool results, first send | 2,574 | 1,785 | 1,741 | 1,458 | 93 | 448 |
| tool results, re-sent | 2,507 | 1,846 | 2,193 | 1,106 | 18 | 174 |
| assistant, first send | 207 | 411 | 232 | 407 | 134 | 384 |
| assistant, re-sent | 200 | 213 | 280 | 205 | 23 | 85 |
| **rebuilt total (o200k)** | 10,052 | 8,131 | 9,167 | 7,294 | 2,263 | 3,211 |
| bounds (parallel calls last / first) | 10,002 / 12,208 | 7,488 / 8,302 | 8,939 / 11,250 | 7,103 / 7,866 | 2,263 / 2,263 | 3,079 / 3,211 |
| w (fitted, 0 = last) | 0.02 | 0.79 | 0.10 | 0.25 | 0.50 | 1.00 |
| **logged input (provider)** | 9,269 | 12,893 | 8,279 | 11,740 | 2,235 | 5,026 |
| logged / rebuilt | 0.92 | 1.59 | 0.90 | 1.61 | 0.99 | 1.57 |
| fixed share of rebuilt | 45% | 48% | 52% | 56% | 88% | 66% |
| re-sent share of rebuilt | 56% | 53% | 60% | 52% | 48% | 45% |

### How close the rebuild gets

The rebuild counts o200k; each provider counts its own tokens, renders the tool definitions its own way and adds framing. Calibration per condition and model, on the runs whose shape is known (one call, then the answer): logged = 2 x rf x fixed + rc x conversation. rf: provider tokens per o200k token of the fixed part; rc: per token of conversation. MAPE: mean absolute error per run, raw (o200k as is) and calibrated, over all runs and over the runs with at most one call per turn (whose every call's turn is known).

| condition | runs | fit on | rf | fixed per request (provider) | rc | MAPE raw | MAPE calibrated | exact runs | exact MAPE raw | exact MAPE calibrated | calibrated total vs logged |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| keel-plain Luna | 387 | 222 | 0.84 | 1,352 | 1.00 | 14.1% | 2.3% | 309 | 14.4% | 1.3% | +0.6% |
| keel-plain Haiku | 387 | 148 | 1.75 | 2,807 | 1.44 | 39.3% | 2.2% | 181 | 40.8% | 0.8% | +0.2% |
| keel-compact Luna | 387 | 228 | 0.85 | 1,419 | 0.98 | 15.3% | 2.2% | 318 | 15.6% | 1.6% | +1.2% |
| keel-compact Haiku | 387 | 164 | 1.73 | 2,894 | 1.47 | 39.5% | 2.0% | 206 | 40.5% | 1.1% | +0.4% |
| sql Luna | 1,160 | 1,053 | 1.02 | 962 | 0.78 | 1.5% | 1.1% | 1,160 | 1.5% | 1.1% | +0.2% |
| sql Haiku | 1,161 | 578 | 1.78 | 1,683 | 1.15 | 37.9% | 1.0% | 664 | 39.6% | 0.7% | +0.0% |

On Luna, SQL's fixed part (mostly prose) weighs what o200k says (rf 1.02) and tool results weigh rc ~1.0; Keel's fixed part weighs less (rf 0.85) because the provider renders JSON tool definitions more tightly than their JSON: taking SQL's rf for the prose, Keel's 1,257 o200k tokens of tool definitions weigh about 0.78x, ~977 Luna tokens per request. On Haiku everything weighs more (Claude's tokenizer on JSON and text, plus Anthropic's tool-use preamble): rf ~1.8, rc ~1.4-1.5.


### Where the gap to SQL comes from (per run, provider tokens: the rebuild weighed by the calibration)

| Keel vs SQL | logged gap | modelled gap | from the fixed part x requests | from tool results (sent + re-sent) | from assistant turns |
| --- | ---: | ---: | ---: | ---: | ---: |
| keel-plain Luna | 7,034 | 7,089 | 1,817 (26%) | 4,988 (70%) | 284 (4%) |
| keel-compact Luna | 6,044 | 6,139 | 1,978 (32%) | 3,780 (62%) | 381 (6%) |
| keel-plain Haiku | 7,867 | 7,886 | 3,010 (38%) | 4,517 (57%) | 359 (5%) |
| keel-compact Haiku | 6,714 | 6,753 | 3,357 (50%) | 3,039 (45%) | 357 (5%) |

The fixed part's share splits further into its size per request and the number of requests:

| Keel vs SQL | fixed per request (provider tokens) | requests per run | bigger fixed part x SQL's requests | Keel's extra requests x its fixed part |
| --- | ---: | ---: | ---: | ---: |
| keel-plain Luna | 1,352 vs 962 | 2.84 vs 2.11 | 822 | 992 |
| keel-compact Luna | 1,419 vs 962 | 2.82 vs 2.11 | 963 | 1,012 |
| keel-plain Haiku | 2,807 vs 1,683 | 2.42 vs 2.24 | 2,517 | 493 |
| keel-compact Haiku | 2,894 vs 1,683 | 2.46 vs 2.24 | 2,713 | 643 |

By number of requests (compact arm): the share of runs and of logged input. A run's k-th request re-sends everything before it, so the long runs weigh more than their count.

| model | requests | runs | share of runs | mean logged input | share of input | accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Luna | 2 | 228 | 59% | 3,412 | 24% | 100% |
| Luna | 3 | 89 | 23% | 5,593 | 16% | 99% |
| Luna | 4 | 29 | 7% | 11,161 | 10% | 93% |
| Luna | 5 | 16 | 4% | 21,061 | 11% | 100% |
| Luna | 6 | 14 | 4% | 33,432 | 15% | 93% |
| Luna | 7 | 5 | 1% | 63,891 | 10% | 60% |
| Luna | 8 | 3 | 1% | 64,482 | 6% | 67% |
| Luna | 9 | 1 | 0% | 77,786 | 2% | 100% |
| Luna | 10 | 1 | 0% | 144,787 | 5% | 0% |
| Luna | 11 | 1 | 0% | 63,908 | 2% | 100% |
| Haiku | 2 | 261 | 67% | 6,807 | 39% | 98% |
| Haiku | 3 | 83 | 21% | 14,044 | 26% | 98% |
| Haiku | 4 | 36 | 9% | 33,516 | 27% | 92% |
| Haiku | 5 | 4 | 1% | 57,795 | 5% | 100% |
| Haiku | 6 | 3 | 1% | 54,483 | 4% | 100% |

By question type: mean logged input per run (compact arm against SQL) and mean requests.

| type | Luna input Keel / SQL | Luna requests | Haiku input Keel / SQL | Haiku requests |
| --- | ---: | ---: | ---: | ---: |
| abstain | 4,889 / 3,255 (1.5x) | 3.1 / 3.1 | 9,434 / 7,589 (1.2x) | 2.9 / 3.5 |
| as_of | 3,351 / 2,054 (1.6x) | 2.0 / 2.0 | 6,707 / 4,443 (1.5x) | 2.0 / 2.1 |
| current | 3,142 / 2,054 (1.5x) | 2.0 / 2.1 | 6,264 / 3,742 (1.7x) | 2.0 / 2.0 |
| known_at | 4,644 / 2,164 (2.1x) | 2.4 / 2.0 | 7,043 / 4,051 (1.7x) | 2.0 / 2.0 |
| links | 5,871 / 2,258 (2.6x) | 3.2 / 2.1 | 12,665 / 7,391 (1.7x) | 3.3 / 2.7 |
| rule | 3,861 / 2,118 (1.8x) | 2.0 / 2.1 | 7,384 / 5,354 (1.4x) | 2.0 / 2.2 |
| tenant | 26,451 / 2,494 (10.6x) | 5.0 / 2.2 | 29,064 / 5,504 (5.3x) | 3.4 / 2.2 |

## 3. Cost per run and what caching does

Input cost per run in micro-dollars. Logged: bench's own cost (Luna: every prompt token at the gateway's input price, no cache discount; Haiku: straight to Anthropic with prompt caching, cache reads at 0.1x and writes at 1.25x). Modelled: the rebuilt prompts (scaled by the calibration), priced fresh, and priced with caching as bench's direct route sets it (the tools+system prefix read from the cache, each request reading the previous one).

| condition | logged input cost | modelled, fresh | modelled, Anthropic caching | modelled, OpenAI-style caching at 0.1x | logged total cost | accuracy | total cost per correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| keel-plain Luna | 927 | 933 | 416 | 346 | 1,046 | 98.7% | 1,060 |
| keel-plain Haiku | 516 | 1,291 | 500 | 419 | 786 | 96.6% | 813 |
| keel-compact Luna | 828 | 838 | 310 | 261 | 961 | 97.7% | 984 |
| keel-compact Haiku | 426 | 1,178 | 439 | 369 | 691 | 97.7% | 708 |
| sql Luna | 223 | 224 | 47 | 42 | 306 | 99.1% | 309 |
| sql Haiku | 179 | 503 | 168 | 142 | 433 | 99.3% | 436 |

Haiku's logged input cost already sits near the cached model: its prompts are mostly cache reads. Luna's are billed as fresh; if the gateway passed on an OpenAI-style cached-input discount, its Keel input cost would fall to the last modelled column. How far Keel's fixed part is a cost depends on that: on Haiku, a token removed from the fixed part saves about 0.1x of its price per request; on Luna as billed, the full price.

## 4. Reduction options, priced on this eval

Per run, on the compact arm (the one becoming the default). Tokens: o200k saved per request x mean requests, then weighed by each model's calibration (rf for the fixed part, rc for the conversation). Cost: Luna at the full input price; Haiku through the cached model (the saving recomputed with the smaller prompts). Contract: descriptions and the instructions text are prose, outside tests/contract.json (its MCP part freezes inputSchema and annotations only), so rewording needs no refresh; removing a parameter or an enum value is breaking (SPEC §11: deprecate first); adding an opt-in is a refresh.

| option | o200k saved per request | Luna input tokens saved per run | Luna $ saved per run (micro$) | Haiku input tokens saved per run | Haiku $ saved per run (micro$) | where | contract | accuracy risk |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| Fixed: shorter tool descriptions (the SHORT wordings in this script) | 264 | 632 (7.6%) | 63.2 | 1,125 (9.6%) | 11.3 | core (keel/tools.py DESCRIPTIONS) | none: prose, outside contract.json | low-medium: the dropped clauses are the ones the agents rarely need, but each was added after a misuse |
| Fixed: drop never-used parameters from the schemas (context.task, context.exclude, history.task, history.changed_since, search.mode, search.min_score, search.depth, search.per_kind, search.exclude) | 95 | 227 (2.7%) | 22.7 | 405 (3.4%) | 4.0 | core (signatures) | breaking: deprecation first (SPEC §11) | none on this eval; other agents lose those features |
| Fixed: hide annotate from a read-only agent's tool list (registry: an agent with no write scope) | 194 | 465 (5.6%) | 46.5 | 827 (7.0%) | 8.3 | core (mcp_server lists tools per agent) | additive: refresh (default lists all five) | none on this eval (never called) |
| Fixed: drop schema defaults (they repeat the descriptions) | 48 | 115 (1.4%) | 11.5 | 205 (1.7%) | 2.0 | core (_compact_schema) | breaking for the MCP inputSchema snapshot (default removed) | low |
| Fixed: the four tool-list trims above together (overlaps counted once) | 542 | 1,298 (15.7%) | 129.8 | 2,310 (19.7%) | 23.1 | core | breaking (parameters, defaults): deprecation first | low-medium |
| Fixed: the two non-breaking ones together (short descriptions + no annotate for a read-only agent) | 415 | 994 (12.0%) | 99.4 | 1,769 (15.1%) | 17.7 | core | none / additive refresh | low-medium |
| Fixed: drop the TABLES note (compact only) | 69 | 165 (2.0%) | 16.5 | 294 (2.5%) | 2.9 | core (instructions) | none: prose | medium-high: it is how the agent reads tables; moving it into a tool description saves nothing |
| Fixed: drop the rules from the instructions (context's evaluation names them) | 83 | 199 (2.4%) | 19.9 | 354 (3.0%) | 3.5 | core (instructions) | none: prose | high: rule questions need the definitions to pick the rule; the SQL prompt carries them too |
| Fixed: drop the relations line | 49 | 117 (1.4%) | 11.7 | 209 (1.8%) | 2.1 | core (instructions) | none: prose | high on links questions |
| Fixed: trim bench's frame (shared by every system) | 42 | 100 (1.2%) | 10.0 | 177 (1.5%) | 1.8 | harness only | n/a | low; helps SQL equally, so it does not move the ratio |
| Results: compact text (already measured in the A/B; becoming the default) | - | 990 (12.0%) | 85.4 | 1,153 (9.8%) | 94.8 | core (default flip, in parallel) | opt-in today: default flip is behaviour, not schema | measured: accuracy on par |
| Turns: every run in at most 2 requests (upper bound; parallel calls) | - | 3,600 (43.5%) | 360.0 | 3,261 (27.8%) | 32.6 | agent behaviour / descriptions | n/a | high if forced: later calls depend on earlier results |
| Cost: prompt caching on Luna's route, cached input at 0.1x (if the gateway honours it) | - | 0 (tokens unchanged) | 576.8 | already cached | 0 | deployment / gateway | n/a | none |
| Cost: prompt caching on Luna's route, cached input at 0.25x (if the gateway honours it) | - | 0 (tokens unchanged) | 480.6 | already cached | 0 | deployment / gateway | n/a | none |
| Cost: prompt caching on Luna's route, cached input at 0.5x (if the gateway honours it) | - | 0 (tokens unchanged) | 320.4 | already cached | 0 | deployment / gateway | n/a | none |

For scale, on the compact arm: Luna 8,279 input tokens and 961 micro$ per run; Haiku 11,740 and 691. SQL: Luna 2,235 and 306; Haiku 5,026 and 433.

Short wordings measured (o200k): context 116 -> 68, history 149 -> 85, search 172 -> 94, annotate 59 -> 17, related 57 -> 29.

