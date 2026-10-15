"""The semantic-search eval harness keeps working (offline: hash embedder, no network)."""

from evals.semantic.run import BM25, convex, key_first, rrf
from evals.semantic.sandboxes import SANDBOXES


def test_sandboxes_build_and_have_labeled_queries():
    for name, build in SANDBOXES.items():
        box = build()
        keys = {e.key for e in box.store.entities(box.tenant)}
        assert box.queries, name
        for q in box.queries:
            assert q.relevant <= keys, (name, q.text)  # every label points at a subject that exists
        assert any(not q.relevant for q in box.queries), name  # each sandbox has negatives


def test_fusion_helpers():
    assert rrf(["a", "b"], ["b", "c"])[0] == "b"
    assert convex([("a", 0.9), ("b", 0.8), ("c", 0.1)], [("b", 3.0), ("c", 1.0)], w=0.5)[0] == "b"  # no ties
    assert key_first("status of CUS-1990?", ["card:CUS-1974", "card:CUS-1990"], ["card:CUS-1974"])[0] == "card:CUS-1990"
    assert BM25({"x:1": "tread worn", "x:2": "slow leak"}).rank("leak")[0][0] == "x:2"


def test_one_sandbox_end_to_end_offline():
    from evals.semantic.run import evaluate_sandbox
    from keel.semantic import HashEmbedder

    rows = evaluate_sandbox(SANDBOXES["finance"](), HashEmbedder(), "hash")
    configs = {r["config"] for r in rows}
    assert {"dense", "bm25", "convex+key"} <= configs
    ids = [r for r in rows if r["config"] == "convex+key" and r["family"] == "exact"]
    assert ids and all(r["mrr@10"] == 1.0 for r in ids)  # "Bancolombia" is a shared word: lexical leg finds it
