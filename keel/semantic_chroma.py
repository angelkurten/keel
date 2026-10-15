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
            self._col(tenant).upsert(ids=[k for k, _, _ in items], embeddings=[list(v) for _, v, _ in items], documents=[d for _, _, d in items])

    def delete(self, tenant: str, keys: list[str]) -> None:
        if keys:
            self._col(tenant).delete(ids=keys)

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[str, float]]:
        col = self._col(tenant)
        n = min(k, col.count())
        if n == 0:
            return []
        res = col.query(query_embeddings=[list(vector)], n_results=n)
        return [(key, 1.0 - dist) for key, dist in zip(res["ids"][0], res["distances"][0])]

    def clear(self, tenant: str) -> None:
        name = collection_name(tenant, self.prefix)
        if name in [getattr(c, "name", c) for c in self.client.list_collections()]:
            self.client.delete_collection(name)
