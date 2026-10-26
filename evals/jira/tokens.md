# Where Keel's tool-result tokens go (Jira benchmark)

Measured offline, no model called: every Keel tool call the agents made in the full run (`full-luna.jsonl` and
`full-haiku.jsonl`, system `keel`, 2,322 runs = 387 questions x 2 models x 3 repetitions, 5,456 calls) replayed through
real MCP sessions on the same graph (MemoryStore, `--max-list 200`, a fixed clock), at c72965c plus the compact form.
Tokens are o200k_base (tiktoken), as the benchmark counts them. The replay reproduces 3,745 of 5,456 calls to the
token and the total within 2% (5.03M replayed against 5.14M logged; the difference is code that changed since the
run, e.g. the `initial` marker of a tenant window). SQL is `full-sql.jsonl` as logged (same questions, models and
repetitions). Reproduce:

```bash
uv run --extra mcp --with tiktoken python -m evals.jira.tokens replay --data ~/datasets/public-jira/export --out /tmp/replay.jsonl
uv run --extra mcp --with tiktoken python -m evals.jira.tokens report /tmp/replay.jsonl
```

## Totals

| | tokens of tool results | per run | vs SQL |
| --- | ---: | ---: | ---: |
| Keel, JSON (today's default) | 5,033,758 | 2,168 | 8.0x |
| Keel, compact (`keel serve --compact`) | 3,216,175 | 1,385 | 5.1x |
| SQL baseline | 628,182 | 271 | 1x |

Per model: GPT-6 Luna 2,622 -> 1,605 per run (SQL 93); Claude Haiku 5.5 1,714 -> 1,165 (SQL 448).

## By tool and detail

| tool | calls | JSON | share | compact | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| search | 1557 | 2,220,684 | 44.1% | 1,359,776 | -39% |
| context | 2329 | 1,661,954 | 33.0% | 1,104,857 | -34% |
| history | 1116 | 683,009 | 13.6% | 458,755 | -33% |
| related | 454 | 468,111 | 9.3% | 292,787 | -37% |

| tool, detail | JSON | share | compact | change |
| --- | ---: | ---: | ---: | ---: |
| search brief | 1,776,116 | 35.3% | 1,123,393 | -37% |
| context full | 1,075,883 | 21.4% | 658,439 | -39% |
| related | 468,111 | 9.3% | 292,787 | -37% |
| context brief | 393,696 | 7.8% | 293,983 | -25% |
| search full | 355,484 | 7.1% | 166,360 | -53% |
| history full | 349,399 | 6.9% | 203,149 | -42% |
| history concise | 333,610 | 6.6% | 255,606 | -23% |
| context concise | 192,375 | 3.8% | 152,435 | -21% |
| search entity | 37,678 | 0.7% | 27,618 | -27% |
| search key | 26,339 | 0.5% | 26,339 | 0% |
| search concise | 22,455 | 0.4% | 13,454 | -40% |
| search count | 2,612 | 0.1% | 2,612 | 0% |

Two choices of the agents dominate: listings in `brief` (200 issues with attributes and current facts, mostly for
tenant-wide questions a `key` listing or a `count` answers), and `detail="full"` contexts (33% of context calls, 65% of
context tokens), which the agents asked for themselves.

## By part of the JSON

Each piece tokenized alone, as a share of the pieces' sum (in context, punctuation merges with keys and values):

| part | share |
| --- | ---: |
| values (not times) | 42.2% |
| key names | 37.8% |
| times (ISO, `+00:00`) | 19.9% |

Largest single paths: `search[].attrs.summary` 5.6%, `related[].since` 3.8%, `search[].key` 3.7%,
`search[].attrs.reporter` 3.6%, `search[].facts.assignee` 2.6%, `history[].valid_from` 2.1%, `history[].evidence` 1.9%;
the key names of every search row (`attrs`, `summary`, `reporter`, six predicates) another ~14%; `context.facts[]`
of `full` repeats `tenant`, `subject`, `source`, `scope` and eight nulls per fact; `context.rules` (full) restates the
rule trees the session instructions already define (2.9%).

Already lean before this step: compact separators (no whitespace), no `tenant`/transaction-time fields in
`concise`/`brief`, relations grouped in `brief`, no nulls in lean facts, one text block per result.

## What compact changes

Lossless: `unpack` gives back the JSON (times written short, the same instants). Checked on all 5,456 replayed calls
and, with the gold answer derived from both forms, on all 387 questions (`evals/jira/test_compact.py`).

- Lists of records as one table: `{"all": {shared fields}, "cols": [...], "rows": [[...]]}`; a record's own fields in
  a trailing object; shared maps (`attrs`, `facts`) as `map.key` columns. This is most of the saving: key names and
  per-row constants (`relation`, `direction`, `predicate`, `source`, `tenant`, nulls) said once.
- UTC times: `2018-08-22T15:17:06+00:00` (18 tokens) -> `2018-08-22T15:17:06Z` (15), `...T08:00Z` (13) on the minute.

```
history(subject="issue:INDY-1638", predicate="status", until=..., detail="concise")   284 -> 234 tokens
[{"predicate":"status","value":"New","valid_from":"2018-08-22T15:17:06+00:00","valid_to":"2018-08-23T05:51:51+00:00","source":"jira","evidence":"jira:hyperledger:INDY-1638:created"},{"predicate":"status","value":"In Progress",...
{"all":{"predicate":"status","source":"jira"},"cols":["value","valid_from","evidence"],"rows":[["New","2018-08-22T15:17:06Z","jira:hyperledger:INDY-1638:created",{"valid_to":"2018-08-23T05:51:51Z"}],["In Progress","2018-08-23T05:51:51Z","jira:hyperledger:INDY-1638:h000000164443:0",{"valid_to":"2018-08-24T08:13:50Z","was":"New"}],...
```

## What is left, and would lose information

Measured on top of compact (share of the compact total); none is done, each removes a documented field:

| option | saves |
| --- | ---: |
| search `brief`/`concise`/`entity` rows without `attrs` | 15.9% |
| related without `since` | 5.3% |
| context `brief`/`concise` without `since` | 4.9% |
| context `full` without `rules` (in the instructions already) | 4.5% |
| history without `source`/`evidence` | 3.4% |
| context `brief`/`concise` without `watermark`/`as_of` | 1.6% |

The rest of the gap to SQL is which calls agents make: SQL agents select the columns a question needs.
