"""Chroma adapter for keel.semantic.VectorIndex. Optional extra: `uv sync --extra chroma`.

One collection per tenant, cosine space; Chroma's collection names are restricted, so the tenant
is encoded as `keel-<safe part>-<hash>`. Scores are 1 - cosine distance.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def collection_name(tenant: str, prefix: str = "keel") -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", tenant).strip("-._")[:40] or "t"
    return f"{prefix}-{safe}-{hashlib.sha1(tenant.encode()).hexdigest()[:8]}"


class ChromaIndex:
    def __init__(self, path: str | None = None, host: str | None = None, port: int = 8000, client: Any = None, prefix: str = "keel") -> None:
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise SystemExit("ChromaIndex needs the optional dependency: uv sync --extra chroma") from exc
            client = chromadb.PersistentClient(path=path) if path else (chromadb.HttpClient(host=host, port=port) if host else chromadb.EphemeralClient())
        self.client, self.prefix = client, prefix

    def _col(self, tenant: str) -> Any:
        return self.client.get_or_create_collection(collection_name(tenant, self.prefix), metadata={"hnsw:space": "cosine"})

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        if items:
            self._col(tenant).upsert(
                ids=[k for k, _, _ in items], embeddings=[list(v) for _, v, _ in items], documents=[d for _, _, d in items],
                metadatas=[{"kind": k.split(":", 1)[0]} for k, _, _ in items],  # filterable inside Chroma
            )

    def delete(self, tenant: str, keys: list[str]) -> None:
        if keys:
            self._col(tenant).delete(ids=keys)

    def query(self, tenant: str, vector: list[float], k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        col = self._col(tenant)
        n = min(k, col.count())
        if n == 0:
            return []
        kinds = (where or {}).get("kind")
        res = col.query(query_embeddings=[list(vector)], n_results=n, **({"where": {"kind": {"$in": list(kinds)}}} if kinds else {}))
        return [(key, 1.0 - dist) for key, dist in zip(res["ids"][0], res["distances"][0])]

    def text_query(self, tenant: str, text: str, k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        # ponytail: local Chroma has no BM25; documents are pulled and ranked here. Fine for thousands of
        # subjects per tenant; a server with native lexical search (Chroma Cloud, Qdrant sparse) scales further.
        from keel.semantic import BM25

        kinds = (where or {}).get("kind")
        got = self._col(tenant).get(include=["documents"], **({"where": {"kind": {"$in": list(kinds)}}} if kinds else {}))
        return BM25(dict(zip(got["ids"], got["documents"]))).rank(text)[:k]

    def clear(self, tenant: str) -> None:
        name = collection_name(tenant, self.prefix)
        if name in [getattr(c, "name", c) for c in self.client.list_collections()]:
            self.client.delete_collection(name)
