"""Weaviate adapter for keel.semantic.VectorIndex. Optional extra: `uv sync --extra weaviate`.

One multi-tenant collection (`Keel` by default); each Keel tenant is a Weaviate tenant, so tenants are
isolated by the server, not by a filter. Tenant names are restricted, so a tenant is encoded as
`<safe part>-<hash>`. Vectors come from Keel's embedder (no Weaviate vectorizer), cosine distance;
scores are 1 - distance. `kind` and `subject` are filterable properties, and the lexical leg is
Weaviate's own BM25 over the document, so hybrid search needs no documents pulled into Keel.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
from typing import Any

from keel.semantic import kind_of, subject_of, tokens


def tenant_name(tenant: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", tenant).strip("-_")[:40] or "t"
    return f"{safe}-{hashlib.sha1(tenant.encode()).hexdigest()[:8]}"


class WeaviateIndex:
    def __init__(
        self, host: str = "localhost", port: int | str = 8080, grpc_host: str | None = None, grpc_port: int | str = 50051,
        secure: bool | str = False, api_key: str | None = None, collection: str = "Keel", client: Any = None,
    ) -> None:
        if client is None:
            try:
                import weaviate
                from weaviate.classes.init import Auth
            except ImportError as exc:
                raise SystemExit("WeaviateIndex needs the optional dependency: uv sync --extra weaviate") from exc
            tls = secure in (True, "true", "1")
            key = api_key or os.environ.get("KEEL_WEAVIATE_API_KEY")
            client = weaviate.connect_to_custom(
                http_host=host, http_port=int(port), http_secure=tls, grpc_host=grpc_host or host, grpc_port=int(grpc_port),
                grpc_secure=tls, auth_credentials=Auth.api_key(key) if key else None,
            )
            atexit.register(client.close)
        self.client, self.name = client, collection[:1].upper() + collection[1:]  # Weaviate class names start uppercase
        self._base: Any = None
        self._tenants: dict[str, Any] = {}

    def _collection(self) -> Any:
        if self._base is None:
            from weaviate.classes.config import (
                Configure,
                DataType,
                Property,
                Tokenization,
                VectorDistances,
            )

            if not self.client.collections.exists(self.name):
                field = {"data_type": DataType.TEXT, "tokenization": Tokenization.FIELD, "index_searchable": False}
                try:
                    self.client.collections.create(
                        self.name,
                        multi_tenancy_config=Configure.multi_tenancy(enabled=True),
                        vector_config=Configure.Vectors.self_provided(
                            vector_index_config=Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE)),
                        properties=[Property(name="key", **field), Property(name="kind", **field), Property(name="subject", **field),
                                    Property(name="doc", data_type=DataType.TEXT, tokenization=Tokenization.WORD)],
                    )
                except Exception:  # another process created it first
                    if not self.client.collections.exists(self.name):
                        raise
            self._base = self.client.collections.use(self.name)
        return self._base

    def _col(self, tenant: str) -> Any:
        if tenant not in self._tenants:  # one existence check per tenant, not one per call
            base, name = self._collection(), tenant_name(tenant)
            if not base.tenants.exists(name):
                try:
                    base.tenants.create(name)
                except Exception:  # another process created it first
                    if not base.tenants.exists(name):
                        raise
            self._tenants[tenant] = base.with_tenant(name)
        return self._tenants[tenant]

    def _with(self, tenant: str, fn: Any) -> Any:
        """Run `fn(collection)`; if a cached tenant is gone (another process cleared it), set it up again once."""
        cached = tenant in self._tenants
        try:
            return fn(self._col(tenant))
        except Exception:
            if not cached:
                raise
            self._tenants.pop(tenant, None)
            return fn(self._col(tenant))

    @staticmethod
    def _filter(where: dict[str, Any] | None) -> Any:
        from weaviate.classes.query import Filter

        conds = []
        if kinds := (where or {}).get("kind"):
            conds.append(Filter.by_property("kind").contains_any(list(kinds)))
        if (where or {}).get("subject") is not None:
            conds.append(Filter.by_property("subject").contains_any(sorted(where["subject"])))
        return Filter.all_of(conds) if len(conds) > 1 else (conds[0] if conds else None)

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        if not items:
            return
        from weaviate.classes.data import DataObject
        from weaviate.util import generate_uuid5

        objs = [DataObject(uuid=generate_uuid5(key), vector=list(vec),
                           properties={"key": key, "kind": kind_of(key), "subject": subject_of(key)[0], "doc": doc})
                for key, vec, doc in items]
        res = self._with(tenant, lambda col: col.data.insert_many(objs))  # a batch replaces objects with the same uuid
        if res.errors:
            raise RuntimeError(f"Weaviate rejected {len(res.errors)} of {len(objs)} documents: {next(iter(res.errors.values())).message}")

    def delete(self, tenant: str, keys: list[str]) -> None:
        if keys:
            from weaviate.classes.query import Filter
            from weaviate.util import generate_uuid5

            self._with(tenant, lambda col: col.data.delete_many(where=Filter.by_id().contains_any([generate_uuid5(k) for k in keys])))

    def query(self, tenant: str, vector: list[float], k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        if k <= 0 or (where or {}).get("subject") in (set(), []):
            return []
        from weaviate.classes.query import MetadataQuery

        res = self._with(tenant, lambda col: col.query.near_vector(
            near_vector=list(vector), limit=k, filters=self._filter(where), return_properties=["key"],
            return_metadata=MetadataQuery(distance=True)))
        return [(o.properties["key"], 1.0 - o.metadata.distance) for o in res.objects]

    def text_query(self, tenant: str, text: str, k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        # ponytail: Weaviate's word tokenizer splits cus-1974 into cus and 1974 where Keel's keeps one token;
        # both still match, ranks may differ slightly from MemoryIndex/Chroma.
        if k <= 0 or not tokens(text) or (where or {}).get("subject") in (set(), []):
            return []
        from weaviate.classes.query import MetadataQuery

        res = self._with(tenant, lambda col: col.query.bm25(
            query=text, query_properties=["doc"], limit=k, filters=self._filter(where), return_properties=["key"],
            return_metadata=MetadataQuery(score=True)))
        return [(o.properties["key"], o.metadata.score) for o in res.objects if o.metadata.score > 0]

    def clear(self, tenant: str) -> None:
        self._tenants.pop(tenant, None)
        base, name = self._collection(), tenant_name(tenant)
        if base.tenants.exists(name):
            base.tenants.remove([name])
