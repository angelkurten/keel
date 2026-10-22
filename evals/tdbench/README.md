# TDBench through Keel

An external check of valid-time reasoning on someone else's data: TDBench (Kim, Wang, Xie and Whang, ICLR 2026,
[arXiv 2508.02045](https://arxiv.org/abs/2508.02045)) builds time-sensitive questions by SQL over temporal tables.
Its leaders and organizations tables (569 terms: who held an office or a post from Start to End) become one Keel
tenant through `cartridge.yaml` (two kinds, `office` and `post`) and `handlers.py` (a term opens `holder` at Start and
closes it at End), with no change under `keel/`. The tables have no transaction time, so only valid time is exercised.

The paper is CC BY 4.0; the repository that holds the data ([github.com/ssoy0701/tdbench](https://github.com/ssoy0701/tdbench))
ships no license file, so this directory holds no copy of it: clone it and pass the clone as `--src`.

```bash
git clone --depth 1 https://github.com/ssoy0701/tdbench.git ~/datasets/tdbench-src/repo
B="uv run --extra mcp --with tiktoken --with anthropic python -m evals.tdbench.run --src ~/datasets/tdbench-src/repo"
$B --out evals/tdbench/results.jsonl --systems keel,open,closed --reps 3                                   # Luna and Haiku
$B --out evals/tdbench/results-table.jsonl --systems table --reps 3
$B --out evals/tdbench/results-sonnet.jsonl --systems keel,open,table,closed --models anthropic/claude-sonnet-5.5 --reps 1
uv run python -m evals.tdbench.run report evals/tdbench/results.jsonl evals/tdbench/results-table.jsonl evals/tdbench/results-sonnet.jsonl
```

`questions()` draws the same 200 questions every time (seed 7, stratified by file and question type, offices whose terms
overlap in the tables left out). Conditions: `keel` (the five tools), `open` (the asked office's rows pasted into the
prompt, TDBench's open-book setting), `table` (both whole tables pasted), `closed` (no tools). Grading is exact set match
of names against TDBench's `correct_rows` (its answer accuracy A; its time accuracy T needs an LLM judge and is not
measured). Rows carry the same fields as the Jira benchmark's, `versions` included. 84 of Sonnet's `table` rows were
measured before commit f364733 (a cache breakpoint for the system prompt) and overstate that condition's cost.
`results.jsonl` rows are stamped `dirty` because `pilot.jsonl` was still untracked when that run started; the code is 6a3faea.
