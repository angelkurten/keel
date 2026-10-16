"""Chroma adapter for keel.semantic.VectorIndex. Optional extra: `uv sync --extra chroma`.

One collection per tenant, cosine space; Chroma's collection names are restricted, so the tenant
is encoded as `keel-<safe part>-<hash>`. Scores are 1 - cosine distance. Each document carries its
kind and subject as metadata, so `kind` and `related_to` filter inside Chroma; a collection built
before 0.21 has no subject metadata and filters by subject after retrieval until `keel index --full`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from keel.semantic import BM25Cache, kind_of, lexical, subject_of

SUBJECTS = "keel:subjects"  # collection metadata: its documents carry `subject`, so related_to filters inside Chroma


def collection_name(tenant: str, prefix: str = "keel") -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", tenant).strip("-._")[:40] or "t"
    return f"{prefix}-{safe}-{hashlib.sha1(tenant.encode()).hexdigest()[:8]}"


class ChromaIndex:
    def __init__(
        self, path: str | None = None, host: str | None = None, port: int = 8000, client: Any = None, prefix: str = "keel", bm25_ttl: float = 300.0,
    ) -> None:
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise SystemExit("ChromaIndex needs the optional dependency: uv sync --extra chroma") from exc
            client = chromadb.PersistentClient(path=path) if path else (chromadb.HttpClient(host=host, port=port) if host else chromadb.EphemeralClient())
        self.client, self.prefix = client, prefix
        self._cols: dict[str, Any] = {}
        self._bm25 = BM25Cache(ttl=bm25_ttl)

    def _col(self, tenant: str) -> Any:
        if tenant not in self._cols:  # one lookup per tenant, not one round trip per call
            name = collection_name(tenant, self.prefix)
            try:
                self._cols[tenant] = self.client.get_collection(name)
            except Exception:  # noqa: BLE001 (NotFoundError in chromadb 1.x, ValueError before): created now, with subject metadata
                self._cols[tenant] = self.client.get_or_create_collection(name, metadata={"hnsw:space": "cosine", SUBJECTS: True})
        return self._cols[tenant]

    def _with(self, tenant: str, fn: Any) -> Any:
        """Run `fn(collection)`; if a cached collection is gone (another process rebuilt it), look it up again once."""
        cached = tenant in self._cols
        try:
            return fn(self._col(tenant))
        except Exception:
            if not cached:
                raise
            self._cols.pop(tenant, None)
            self._bm25.drop(tenant)
            return fn(self._col(tenant))

    def _filter(self, tenant: str, where: dict[str, Any] | None) -> dict[str, Any]:
        conds = []
        if kinds := (where or {}).get("kind"):
            conds.append({"kind": {"$in": list(kinds)}})
        if (where or {}).get("subject") is not None and (self._col(tenant).metadata or {}).get(SUBJECTS):
            conds.append({"subject": {"$in": sorted(where["subject"]) or [""]}})
        return {"where": conds[0] if len(conds) == 1 else {"$and": conds}} if conds else {}

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        if items:
            self._with(tenant, lambda col: col.upsert(
                ids=[k for k, _, _ in items], embeddings=[list(v) for _, v, _ in items], documents=[d for _, _, d in items],
                metadatas=[{"kind": kind_of(k), "subject": subject_of(k)[0]} for k, _, _ in items],  # filterable inside Chroma
            ))
            self._bm25.drop(tenant)

    def delete(self, tenant: str, keys: list[str]) -> None:
        if keys:
            self._with(tenant, lambda col: col.delete(ids=keys))
            self._bm25.drop(tenant)

    def query(self, tenant: str, vector: list[float], k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        def run(col: Any) -> list[tuple[str, float]]:
            n = min(k, col.count())
            if n == 0:
                return []
            res = col.query(query_embeddings=[list(vector)], n_results=n, **self._filter(tenant, where))
            return [(key, 1.0 - dist) for key, dist in zip(res["ids"][0], res["distances"][0])]

        return self._with(tenant, run)

    def text_query(self, tenant: str, text: str, k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        # ponytail: local Chroma has no BM25; a kind's documents are pulled once and ranked here, cached until
        # the collection's count changes or bm25_ttl seconds pass (another process may have re-indexed).
        # A server with native lexical search (Chroma Cloud, Qdrant sparse) scales further.
        kinds = (where or {}).get("kind")

        def run(col: Any) -> list[tuple[str, float]]:
            def load() -> dict[str, str]:
                got = col.get(include=["documents"], **self._filter(tenant, {"kind": kinds} if kinds else None))
                return dict(zip(got["ids"], got["documents"]))

            return lexical(self._bm25.get(tenant, kinds, col.count(), load), text, k, where)

        return self._with(tenant, run)

    def clear(self, tenant: str) -> None:
        name = collection_name(tenant, self.prefix)
        self._cols.pop(tenant, None)
        self._bm25.drop(tenant)
        if name in [getattr(c, "name", c) for c in self.client.list_collections()]:
            self.client.delete_collection(name)
