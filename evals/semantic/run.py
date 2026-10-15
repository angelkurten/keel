"""Semantic-search evals over the sandboxes: which retrieval configuration finds what, per query family.

    KEEL_EMBEDDINGS_API_KEY=... KEEL_EMBEDDINGS_BASE_URL=... uv run python -m evals.semantic.run [--models small,large]

Configurations (rows): what Keel does today, and candidate improvements prototyped here.
    substring        search(text): today's text mode
    dense            search(mode="semantic") with the sandbox's cartridge policy (today's semantic mode)
    dense-nopolicy   the same, documents rendered without the policy (noise included)
    bm25             lexical BM25 over the same rendered documents (prototype)
    hybrid           RRF(k=60) of bm25 and dense (prototype)
    dense-noise      ablation: only the `exclude` part of the policy (no relations)
    dense-rel        ablation: only the relations part of the policy (noise kept)
    dense+key        exact subject-key match first, then dense (prototype)
    convex           0.7 * dense + 0.3 * bm25, both min-max normalized (prototype, Bruch et al.)
    convex+key       convex with the key shortcut
    keel             search(mode="semantic") as shipped in 0.12: keys, convex fusion, compact relations
    keel+rerank      the same with LLMReranker (KEEL_RERANK_* env vars), when configured
Metrics per family: hit@1, recall@5, MRR@10; negatives report the top score instead (lower is better).
Embeddings are cached on disk by (model, text) so reruns cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from evals.semantic.sandboxes import SANDBOXES, Query, Sandbox
from keel import Tools
from keel.semantic import MemoryIndex, OpenAIEmbedder, index_tenant, render

CACHE = Path(os.environ.get("KEEL_EVAL_CACHE", ".keel/eval-embeddings.json"))
RERANKER = None
if os.environ.get("KEEL_RERANK_MODEL"):
    from keel.semantic import LLMReranker

    RERANKER = LLMReranker.from_env()


class CachedEmbedder:
    def __init__(self, inner: OpenAIEmbedder) -> None:
        self.inner, self.calls, self.tokens_in = inner, 0, 0
        self.cache: dict[str, list[float]] = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    def _k(self, text: str) -> str:
        return hashlib.sha1(f"{self.inner.model}|{text}".encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if self._k(t) not in self.cache]
        if missing:
            self.calls += 1
            self.tokens_in += sum(len(t) // 4 for t in missing)
            for t, v in zip(missing, self.inner.embed(missing)):
                self.cache[self._k(t)] = v
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(self.cache))
        return [self.cache[self._k(t)] for t in texts]


TOKEN = re.compile(r"[a-z0-9áéíóúñü]+(?:-[a-z0-9]+)*")


class BM25:
    """Okapi BM25 over the rendered documents; identifiers like cus-1974 stay one token."""

    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs = {key: TOKEN.findall(text.lower()) for key, text in docs.items()}
        self.avg = sum(len(d) for d in self.docs.values()) / max(1, len(self.docs))
        self.df = Counter(tok for d in self.docs.values() for tok in set(d))
        self.n, self.k1, self.b = len(self.docs), k1, b

    def rank(self, query: str) -> list[tuple[str, float]]:
        q = TOKEN.findall(query.lower())
        scores = {}
        for key, d in self.docs.items():
            tf = Counter(d)
            s = 0.0
            for tok in q:
                if tok in tf:
                    idf = math.log(1 + (self.n - self.df[tok] + 0.5) / (self.df[tok] + 0.5))
                    s += idf * tf[tok] * (self.k1 + 1) / (tf[tok] + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            if s > 0:
                scores[key] = s
        return sorted(scores.items(), key=lambda x: -x[1])


def rrf(*rankings: list[str], k: int = 60) -> list[str]:
    score: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for i, key in enumerate(ranking):
            score[key] += 1 / (k + i + 1)
    return sorted(score, key=lambda x: -score[x])


def allowed(tools: Tools, key: str, q: Query) -> bool:
    """The filters Keel applies after retrieval: kind and rule."""
    if q.kind and not key.startswith(q.kind + ":"):
        return False
    if q.rule:
        from keel.envelope import utcnow
        from keel.rules import evaluate

        graph = tools._graph(utcnow())
        return evaluate(tools.cartridge.rules[q.rule], key, graph.as_of, graph) is True
    return True


def metrics(ranked: list[str], relevant: set[str]) -> dict[str, float]:
    hit1 = 1.0 if ranked[:1] and ranked[0] in relevant else 0.0
    recall5 = len(set(ranked[:5]) & relevant) / min(5, len(relevant))
    precision10 = len(set(ranked[:10]) & relevant) / min(10, len(relevant))
    mrr = next((1 / (i + 1) for i, key in enumerate(ranked[:10]) if key in relevant), 0.0)
    return {"hit@1": hit1, "recall@5": recall5, "p@10": precision10, "mrr@10": mrr}


KEY = re.compile(r"\b[A-Za-z]+-\d+\b")


def key_first(query: str, keys: list[str], ranked: list[str]) -> list[str]:
    """Subjects whose id appears verbatim in the query go first: ids are what dense vectors blur."""
    ids = {m.lower() for m in KEY.findall(query)}
    exact = [k for k in keys if k.split(":", 1)[1].lower() in ids]
    return exact + [k for k in ranked if k not in exact]


def convex(dense: list[tuple[str, float]], lex: list[tuple[str, float]], w: float = 0.7) -> list[str]:
    def norm(xs):
        if not xs:
            return {}
        hi, lo = max(v for _, v in xs), min(v for _, v in xs)
        return {k: (v - lo) / (hi - lo) if hi > lo else 1.0 for k, v in xs}

    d, l_ = norm(dense), norm(lex)
    return sorted(set(d) | set(l_), key=lambda k: -(w * d.get(k, 0.0) + (1 - w) * l_.get(k, 0.0)))


def evaluate_sandbox(box: Sandbox, embedder, label: str) -> list[dict]:
    rows = []
    policy_docs = {k: d for k in sorted({e.key for e in box.store.entities(box.tenant)}) if (d := render(box.store, box.tenant, k, box.policy))}
    indexes = {}
    variants = (("dense", box.policy), ("dense-nopolicy", {}), ("dense-noise", {k: v for k, v in box.policy.items() if k != "relations"}),
                ("dense-rel", {k: v for k, v in box.policy.items() if k == "relations"}))
    for name, policy in variants:
        idx = MemoryIndex()
        index_tenant(box.store, box.tenant, embedder, idx, policy=policy)
        indexes[name] = idx
    bm25 = BM25(policy_docs)
    for q in box.queries:
        tools = {n: Tools(box.cartridge, box.store, box.tenant, embedder=embedder, index=idx) for n, idx in indexes.items()}
        dense_policy = {**box.policy, "lexical_weight": 0}  # the dense rows stay dense-only
        box.cartridge.semantic = dense_policy
        dense_scored = indexes["dense"].query(box.tenant, embedder.embed([q.text])[0], 500)
        dense_ok = [(k, v) for k, v in dense_scored if allowed(tools["dense"], k, q)]
        dense_rank = [k for k, _ in dense_ok]
        lex_ok = [(k, v) for k, v in bm25.rank(q.text) if allowed(tools["dense"], k, q)]
        lex_rank = [k for k, _ in lex_ok]
        keys = [k for k in policy_docs if allowed(tools["dense"], k, q)]
        sub = tools["dense"].search(q.text, kind=q.kind, rule=q.rule) if (q.kind or q.rule) else tools["dense"].search(q.text)
        sub_rank = list(dict.fromkeys(h.get("key") or h.get("subject") for h in sub))
        ranks = {
            "substring": sub_rank,
            "dense": [h["key"] for h in tools["dense"].search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)],
            "dense-nopolicy": [h["key"] for h in tools["dense-nopolicy"].search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)],
            "bm25": lex_rank,
            "hybrid": rrf(lex_rank[:50], dense_rank[:50]),
            "dense-noise": [h["key"] for h in tools["dense-noise"].search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)],
            "dense-rel": [h["key"] for h in tools["dense-rel"].search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)],
            "dense+key": key_first(q.text, keys, dense_rank)[:10],
            "convex": convex(dense_ok[:50], lex_ok[:50])[:10],
            "convex+key": key_first(q.text, keys, convex(dense_ok[:50], lex_ok[:50]))[:10],
        }
        box.cartridge.semantic = box.policy  # shipped defaults: lexical_weight 0.3
        ranks["keel"] = [h["key"] for h in tools["dense"].search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)]
        if RERANKER is not None:
            rr = Tools(box.cartridge, box.store, box.tenant, embedder=embedder, index=indexes["dense"], reranker=RERANKER)
            ranks["keel+rerank"] = [h["key"] for h in rr.search(q.text, mode="semantic", kind=q.kind, rule=q.rule, limit=10)]
        box.cartridge.semantic = dense_policy
        top = dense_scored[0][1] if dense_scored else 0.0
        for config, ranked in ranks.items():
            row = {"sandbox": box.name, "model": label, "config": config, "family": q.family, "query": q.text}
            if q.relevant:
                row.update(metrics(ranked, q.relevant))
            else:
                row.update({"returned": len(ranked), "top_score": round(top, 3) if config.startswith("dense") or config == "hybrid" else None})
            rows.append(row)
        if q.relevant:
            rows.append({"sandbox": box.name, "model": label, "config": "_positive_top_score", "family": q.family, "query": q.text, "top_score": round(top, 3)})
    return rows


def report(rows: list[dict]) -> str:
    out = []
    configs = ["substring", "dense", "dense-nopolicy", "dense-noise", "dense-rel", "bm25", "hybrid", "dense+key", "convex", "convex+key", "keel", "keel+rerank"]
    configs = [c for c in configs if any(r["config"] == c for r in rows)]
    for model in sorted({r["model"] for r in rows}):
        out.append(f"\n## {model}\n\nMRR@10 by query family (all sandboxes), higher is better\n")
        families = [f for f in ("paraphrase", "exact", "id", "crosslang", "relation", "rule") if any(r["family"] == f for r in rows)]
        out.append("| config | " + " | ".join(families) + " | all |")
        out.append("|---" * (len(families) + 2) + "|")
        for c in configs:
            cells = []
            vals_all = []
            for f in families:
                vals = [r["mrr@10"] for r in rows if r["model"] == model and r["config"] == c and r["family"] == f and "mrr@10" in r]
                vals_all += vals
                cells.append(f"{sum(vals) / len(vals):.2f}" if vals else "-")
            out.append(f"| {c} | " + " | ".join(cells) + f" | {sum(vals_all) / len(vals_all):.2f} |")
        hits = {c: [r["hit@1"] for r in rows if r["model"] == model and r["config"] == c and "hit@1" in r] for c in configs}
        out.append("\nhit@1 (all positive queries): " + ", ".join(f"{c} {sum(v) / len(v):.2f}" for c, v in hits.items()))
        rel = {c: [r["p@10"] for r in rows if r["model"] == model and r["config"] == c and r["family"] == "relation"] for c in configs}
        out.append("relation queries, precision@10 (many relevant subjects): " + ", ".join(f"{c} {sum(v) / len(v):.2f}" for c, v in rel.items() if v))
        pos = sorted(r["top_score"] for r in rows if r["model"] == model and r["config"] == "_positive_top_score")
        neg = sorted(r["top_score"] for r in rows if r["model"] == model and r["config"] == "dense" and r["family"] == "negative")
        out.append(f"\ndense top score, positives: min {pos[0]:.3f} median {pos[len(pos) // 2]:.3f} | negatives: {', '.join(f'{x:.3f}' for x in neg)}")
        for th in (0.35, 0.40, 0.45, 0.50):
            fn = sum(1 for x in pos if x < th) / len(pos)
            fp = sum(1 for x in neg if x >= th) / len(neg)
            out.append(f"  min_score {th:.2f}: positives dropped {fn:.0%}, negatives answered {fp:.0%}")
        out.append("\nmisses of today's dense mode (hit@1 = 0):")
        for r in rows:
            if r["model"] == model and r["config"] == "dense" and r.get("hit@1") == 0.0:
                out.append(f"- [{r['sandbox']}/{r['family']}] {r['query']}  (mrr {r['mrr@10']:.2f})")
    return "\n".join(out)


def main() -> None:
    models = {"small": "openai/text-embedding-3-small", "large": "openai/text-embedding-3-large"}
    wanted = (sys.argv[sys.argv.index("--models") + 1] if "--models" in sys.argv else "small").split(",")
    rows = []
    for label in wanted:
        emb = CachedEmbedder(OpenAIEmbedder.from_env(model=os.environ.get("KEEL_EMBEDDINGS_MODEL") if label == "env" else models[label]))
        t0 = time.perf_counter()
        for build in SANDBOXES.values():
            rows += evaluate_sandbox(build(), emb, label)
        print(f"{label}: {time.perf_counter() - t0:.1f}s, {emb.calls} embedding calls, ~{emb.tokens_in} tokens embedded", file=sys.stderr)
    Path(".keel/eval-rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(report(rows))


if __name__ == "__main__":
    main()
