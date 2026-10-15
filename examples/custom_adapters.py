"""Bring your own vector store and embedding model: two small adapters, then semantic search with them.

    uv run python examples/custom_adapters.py

Keel's semantic search talks to two protocols in keel.semantic. Any class with these methods works;
nothing to inherit, nothing to register:

    VectorIndex   upsert(tenant, items) · delete(tenant, keys) · query(tenant, vector, k) · clear(tenant)
    Embedder      embed(texts)  (optionally role="query" | "document")

Point the CLI at them with a spec, `module:factory?param=value`:

    keel index cartridges/fleet --tenant acme-logistics --db keel.db \
      --index examples.custom_adapters:make_index?path=vectors.db \
      --embedder examples.custom_adapters:make_embedder

and prove the index honours the contract with the conformance suite:

    KEEL_VECTOR_INDEX="examples.custom_adapters:make_index?path=/tmp/v.db" uv run pytest tests/test_semantic.py
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from typing import Any

from keel import Ingestor, SqliteStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter
from keel.semantic import HashEmbedder, index_tenant


class SqliteVectorIndex:
    """Vectors in one SQLite table, exact cosine in Python. Replace the SQL with your engine's calls
    (pgvector, Qdrant, Pinecone, OpenSearch...); the four methods are the whole contract."""

    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (tenant TEXT, key TEXT, vector TEXT, doc TEXT, PRIMARY KEY (tenant, key))")

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)", [(tenant, k, json.dumps(v), d) for k, v, d in items])
        self.db.commit()

    def delete(self, tenant: str, keys: list[str]) -> None:
        self.db.executemany("DELETE FROM vectors WHERE tenant = ? AND key = ?", [(tenant, k) for k in keys])
        self.db.commit()

    def query(self, tenant: str, vector: list[float], k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        """(key, cosine), best first. Every query is filtered by tenant: an index never returns another
        tenant's keys. `where={"kind": [...]}` is optional; taking it lets Keel filter inside the index."""
        kinds = set(where["kind"]) if where and where.get("kind") else None
        rows = self.db.execute("SELECT key, vector FROM vectors WHERE tenant = ?", (tenant,)).fetchall()
        scored = [(key, _cosine(vector, json.loads(v))) for key, v in rows if kinds is None or key.split(":", 1)[0] in kinds]
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:k]  # ponytail: full scan, an ANN index past ~100k vectors

    def clear(self, tenant: str) -> None:
        self.db.execute("DELETE FROM vectors WHERE tenant = ?", (tenant,))
        self.db.commit()


class ModelEmbedder:
    """Wraps your own model: any function from a list of texts to a list of vectors. `role` lets an
    asymmetric model (e5, Qwen3, Voyage) embed questions and documents differently."""

    def __init__(self, encode: Callable[[list[str]], list[list[float]]], query_prefix: str = "", document_prefix: str = "") -> None:
        self.encode, self.prefix = encode, {"query": query_prefix, "document": document_prefix}

    def embed(self, texts: list[str], role: str = "document") -> list[list[float]]:
        return self.encode([self.prefix[role] + t for t in texts])


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


# factories for the CLI spec; parameters arrive as text from the query string
def make_index(path: str = ":memory:") -> SqliteVectorIndex:
    return SqliteVectorIndex(path)


def make_embedder() -> ModelEmbedder:
    return ModelEmbedder(HashEmbedder().embed)  # the demo's "model"; put sentence-transformers, an HTTP call, etc. here


if __name__ == "__main__":
    c, store = load_cartridge("cartridges/fleet"), SqliteStore()
    Ingestor(c, store).ingest(JsonlAdapter("examples/events/fleet.jsonl"), strict=True)
    index, embedder = make_index(), make_embedder()
    report = index_tenant(store, "acme-logistics", embedder, index, policy=c.semantic)
    print("indexed:", report.indexed)
    tools = Tools(c, store, "acme-logistics", embedder=embedder, index=index)
    hits = tools.search("tread depth", mode="semantic", limit=3)
    assert hits and hits[0]["key"] == "tire:T-9001", hits
    for h in hits:
        print(f"{h['key']:<24} score={h['score']:.2f} match={h.get('match', 'semantic')}")
