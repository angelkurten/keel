# Semantic search: research and evals

Status: 2026-10-15. Research on vector stores, RAG and hybrid retrieval, plus a labeled eval bench (`evals/semantic/`) run with real embeddings. The findings rank the next improvements; none is implemented yet.

## The bench

Three sandboxes built through the real store primitives, each with labeled queries in seven families chosen to probe known weak spots of dense retrieval (`evals/semantic/sandboxes.py`):

| Sandbox | Shape | Queries |
| --- | --- | --- |
| fleet | 40 units, 160 tires, noisy inspection comments with URLs, one policy | 14 |
| cards | 125 Jira-like cards in Spanish, 4 clients, comments with URLs and commit hashes, a pause rule | 12 |
| finance | a household's debts and dues, Spanish, creditors and amounts | 8 |

Families: paraphrase (no shared word), exact, id ("CUS-1990"), crosslang (Spanish query, English document or the reverse), relation (answerable only through a related subject), negative (nothing relevant exists), rule (semantic query plus a rule filter).

```bash
KEEL_EMBEDDINGS_API_KEY=... KEEL_EMBEDDINGS_BASE_URL=https://api.ofox.ai/v1 uv run python -m evals.semantic.run --models small,large
```

Embeddings are cached by model and text, so reruns are free. A full run embedded about 25 000 tokens per model (fractions of a cent with `text-embedding-3-small`). `tests/test_evals.py` keeps the harness working offline.

## Results (2026-10-15, OpenAI text-embedding-3-small and -large through OfoxAI)

MRR@10 per family, all sandboxes:

| Configuration | paraphrase | exact | id | crosslang | relation | rule | all (small) | all (large) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| substring (today's text mode) | 0.00 | 0.80 | 1.00 | 0.00 | 0.00 | 0.00 | 0.28 | 0.28 |
| dense (today's semantic mode, cartridge policy) | 0.78 | 1.00 | 0.45 | 1.00 | 0.50 | 1.00 | 0.82 | 0.85 |
| bm25 over the same documents | 0.45 | 1.00 | 0.80 | 0.00 | 1.00 | 0.67 | 0.58 | 0.58 |
| hybrid RRF(k=60) | 0.59 | 1.00 | 0.88 | 0.80 | 0.67 | 1.00 | 0.79 | 0.81 |
| dense + exact-key shortcut | 0.78 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 0.89 | 0.95 |
| convex (0.7 dense + 0.3 bm25, min-max normalized) | 0.85 | 1.00 | 0.78 | 1.00 | 0.67 | 1.00 | 0.89 | 0.92 |
| **convex + exact-key shortcut** | 0.85 | 1.00 | 1.00 | 1.00 | 0.67 | 1.00 | **0.93** | **0.98** |

Family columns are the small model. Hit@1 over all positive queries: dense 0.72 (small) and 0.83 (large); convex + key 0.86 and 0.97. Precision@10 on relation queries (many relevant subjects): dense 0.50, convex + key 0.88 (small); 0.75 and 0.88 (large). The offline test caught one impossible label in the first run (a tire that did not exist); these numbers are after the fix.

## What the numbers say

1. **Dense alone loses on identifiers** (MRR 0.45 small, 0.28 large): a pooled vector blurs "CUS-1990" into "some card". Today's substring mode gets every id right. An exact-key shortcut fixes ids with no cost elsewhere.
2. **RRF hurts paraphrases** (0.78 to 0.59): with equal weight, a weak lexical leg drags good dense results down. A convex combination weighted toward dense keeps paraphrases and still gains on ids and relations, in line with Bruch et al. (ACM TOIS), who found convex fusion beats RRF and is easy to tune with a few labels.
3. **Lexical carries relation queries** (BM25 precision@10 1.00): the client's name in the rendered relation line is an exact word match. Dense spreads over similar-sounding cards.
4. **The cartridge policy is not free.** Excluding noisy comments did not measurably help these embedders (noise-only equals no policy). Relation lines help relation queries (precision@10 0.50 vs 0.30) but, combined with the exclusion, cost two paraphrase queries: every tire's document now repeats its unit's model and plate, which makes tire documents look alike. Hypothesis to test next: render relations as key plus one label attribute, not every attribute.
5. **A fixed cosine threshold cannot say "nothing here".** Positives' top score starts at 0.36 to 0.39 while negatives reach 0.44 to 0.45; at 0.45 the large model drops 21% of positives to silence all negatives. The literature agrees: absolute cosine is model-specific (text-embedding-3 averages about 0.43 where ada-002 averaged 0.85), so any threshold must be calibrated per model, and better signals are a reranker score or agreement between the lexical and dense legs.
6. **The large model buys little once fusion is in** (0.93 to 0.98 MRR at 6.5 times the price). Small with fusion beats large without it.

Caveats: 34 positive queries and 9 negatives, written by us, so results are directional; differences of one or two queries per family are noise. Synthetic queries are known to be easier than real ones (higher lexical overlap); real agent queries from kagent and Toche should join the bench.

## What the research adds (sources in the research summary)

- Hybrid search is standard in production stores: Weaviate relativeScoreFusion, Elasticsearch RRF retriever, Qdrant sparse vectors, turbopuffer BM25. Anthropic's Contextual Retrieval: adding BM25 to contextual embeddings cut top-20 failures from 3.7% to 2.9%, a reranker on top to 1.9%.
- Filtering belongs in the store when a kind is a small slice of a tenant: pgvector 0.8 iterative scans, Qdrant filterable HNSW (99.8% recall at 1% selectivity) and `is_tenant` payload indexes, sqlite-vec metadata columns. Keel's widening post-filter loop is correct but costs round trips at scale.
- Asymmetric embedders (multilingual-e5 "query:" / "passage:", Qwen3 instructions) lose 1 to 5% recall without their prefixes; Keel's `Embedder.embed(texts)` cannot express the role yet.
- MySQL 9 Community has the VECTOR type but no distance function or index (HeatWave only): not a backend.
- GraphRAG-style LLM summaries at index time conflict with deterministic ingest; what transfers is query-time fusion and one-hop expansion from top hits.

## Ranked improvements

| # | Change | Evidence here | Size |
| --- | --- | --- | --- |
| 1 | Exact-key shortcut in `search(mode="semantic")` | id MRR 0.45 to 1.00, no regression elsewhere | XS |
| 2 | Hybrid with convex fusion (dense 0.7, lexical 0.3) and a lexical leg over the same documents, deterministic tokenizer | overall MRR 0.82 to 0.93 (small), 0.85 to 0.98 (large); relation p@10 0.50 to 0.88 | M |
| 3 | `min_score` option, documented as model-specific, off by default; the eval's threshold sweep as the way to choose it | negatives cannot be separated by a fixed cut | S |
| 4 | Compact relation lines (key plus one label attribute) | hypothesis from finding 4 | S |
| 5 | `Embedder.embed(texts, role="query"|"document")` for asymmetric models | research: 1 to 5% recall | S |
| 6 | Optional `where={"kind": ...}` in `VectorIndex.query`, mapped by adapters that filter natively | research: recall and round trips at scale | M |
| 7 | Reranker adapter over the top 30 to 50 fused candidates | research: +5 to 15 nDCG points; not measured here | M |
| 8 | Grow the bench with real agent queries from kagent and Toche | synthetic-query bias | ongoing |
