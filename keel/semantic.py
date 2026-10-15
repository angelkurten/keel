"""Semantic search: two adapter contracts, an indexer, and the adapters Keel ships.

    VectorIndex  where vectors live, namespaced by tenant (MemoryIndex here; ChromaIndex in
                 keel.semantic_chroma; Qdrant, pgvector, Pinecone are other implementations)
    Embedder     text -> vectors (OpenAIEmbedder for any /v1/embeddings API: OpenAI, OfoxAI, Azure,
                 Ollama, vLLM; HashEmbedder, deterministic, for tests)

Any project writes its own adapter by implementing the protocol and points Keel at it with a spec,
the same way stores are pluggable:

    keel index cartridges/fleet --tenant acme --db keel.db --index my_pkg.vectors:make?url=... --embedder my_pkg.emb:make
    KEEL_VECTOR_INDEX=my_pkg.vectors:make KEEL_EMBEDDER=my_pkg.emb:make uv run pytest tests/test_semantic.py   # conformance

Indexing runs outside ingest (ingest stays deterministic, no network: design rule 10). One document
per subject: its key, attributes and open accepted facts of tenant and global scope, rendered as
short text; task and agent memory is never indexed. `index_tenant` is incremental from a watermark.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse

from keel.scopes import Scope
from keel.store.base import Store


class VectorIndex(Protocol):
    """Where vectors live. Every call takes the tenant first; an index never returns another tenant's keys."""

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None: ...  # (subject key, vector, document)

    def delete(self, tenant: str, keys: list[str]) -> None: ...

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[str, float]]: ...  # (key, score in [-1, 1]), best first

    def clear(self, tenant: str) -> None: ...


class Embedder(Protocol):
    """Text to vectors, one per input, in order, all of the same dimension."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 0.0 if not na or not nb else sum(x * y for x, y in zip(a, b)) / (na * nb)


class MemoryIndex:
    """Reference index: exact cosine over a dict. Tests, demos, small tenants."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, tuple[list[float], str]]] = {}

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        bucket = self._data.setdefault(tenant, {})
        for key, vector, doc in items:
            bucket[key] = (list(vector), doc)

    def delete(self, tenant: str, keys: list[str]) -> None:
        for key in keys:
            self._data.get(tenant, {}).pop(key, None)

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[str, float]]:
        scored = [(key, cosine(vector, v)) for key, (v, _) in self._data.get(tenant, {}).items()]
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:k]

    def clear(self, tenant: str) -> None:
        self._data.pop(tenant, None)


class HashEmbedder:
    """Deterministic bag of words hashed into a fixed dimension: shared words mean similar vectors.
    For tests and offline demos; it knows no synonyms."""

    def __init__(self, dim: int | str = 256) -> None:
        self.dim = int(dim)  # spec params arrive as text

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for token in re.findall(r"[a-z0-9áéíóúñü]+", text.lower()):
                v[int(hashlib.sha1(token.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(v)
        return out


class OpenAIEmbedder:
    """Any OpenAI-compatible /v1/embeddings endpoint, stdlib only.

    From the environment: KEEL_EMBEDDINGS_BASE_URL (default https://api.openai.com/v1),
    KEEL_EMBEDDINGS_MODEL (default text-embedding-3-small), KEEL_EMBEDDINGS_API_KEY."""

    def __init__(self, base_url: str = "https://api.openai.com/v1", model: str = "text-embedding-3-small", api_key: str | None = None, batch: int | str = 128, timeout: float | str = 60.0) -> None:
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.batch, self.timeout = int(batch), float(timeout)  # spec params arrive as text

    @classmethod
    def from_env(cls, **overrides: Any) -> OpenAIEmbedder:
        kw: dict[str, Any] = {
            "base_url": os.environ.get("KEEL_EMBEDDINGS_BASE_URL", "https://api.openai.com/v1"),
            "model": os.environ.get("KEEL_EMBEDDINGS_MODEL", "text-embedding-3-small"),
            "api_key": os.environ.get("KEEL_EMBEDDINGS_API_KEY"),
        }
        kw.update(overrides)
        return cls(**kw)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            part = texts[i : i + self.batch]
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(f"{self.base_url}/embeddings", data=json.dumps({"model": self.model, "input": part}).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    data = json.loads(res.read())["data"]
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"embeddings {exc.code}: {exc.read()[:300].decode(errors='replace')}") from exc
            out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        return out


# ---- what gets indexed --------------------------------------------------------------------

INDEXED_SCOPES = {Scope.GLOBAL, Scope.TENANT}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_VALUE_CHARS = 300


def render(store: Store, tenant: str, subject: str, policy: dict[str, Any] | None = None) -> str | None:
    """One subject as a short document: key, attributes, open accepted facts of tenant/global scope,
    and the open relations the cartridge names. `policy` is the cartridge's `semantic:` section:

        semantic:
          kinds: [card, cliente]          # absent: every kind
          predicates: [estado, resumen]   # what to include; or exclude: [comentario]
          attrs: true                     # entity attributes (default true); or a list of names
          relations: [de_cliente]         # open relations, with the other side's attributes

    None when there is nothing to index (not an indexed kind, or nothing left): remove it."""
    policy = policy or {}
    kind = subject.split(":", 1)[0]
    if policy.get("kinds") and kind not in policy["kinds"]:
        return None
    now = datetime.now(timezone.utc)
    ent = store.entity(tenant, subject)
    facts = [f for f in store.facts(tenant, subject, at=now) if f.status is None and f.scope in INDEXED_SCOPES]
    if "predicates" in policy:
        facts = [f for f in facts if f.predicate in policy["predicates"]]
    facts = [f for f in facts if f.predicate not in (policy.get("exclude") or [])]
    lines = [subject]
    attrs = policy.get("attrs", True)
    if ent is not None and attrs:
        lines += [f"{k}: {v}" for k, v in sorted(ent.attrs.items()) if v is not None and (attrs is True or k in attrs)]
    for f in facts:
        value = f.value if isinstance(f.value, str) else json.dumps(f.value, ensure_ascii=False)
        lines.append(f"{f.predicate}: {value[:MAX_VALUE_CHARS]}")
    for rel in policy.get("relations") or []:
        for r in store.relations(tenant, subject, rel=rel, at=now):
            other = r.dst if r.src == subject else r.src
            label = store.entity(tenant, other)
            extra = ", ".join(f"{k}: {v}" for k, v in sorted(label.attrs.items()) if v is not None) if label else ""
            lines.append(f"{rel}: {other}" + (f" ({extra})" if extra else ""))
    if ent is None and len(lines) == 1:
        return None
    return "\n".join(lines)


@dataclass
class IndexReport:
    indexed: int
    deleted: int
    cursor: datetime | None  # pass it back as `since` next time


def index_tenant(
    store: Store, tenant: str, embedder: Embedder, index: VectorIndex, since: datetime | None = None, batch: int = 64,
    policy: dict[str, Any] | None = None,
) -> IndexReport:
    """Bring the index up to date for one tenant. `since=None` rebuilds it; a watermark updates the
    subjects whose facts changed after it, and both ends of every link recorded after it (an indexed
    relation names the other side). `policy` is the cartridge's `semantic:` section. Entity attributes
    and unlinks carry no transaction time, so changes to them alone are picked up by `--full`."""
    cursor = store.watermark(tenant)
    if since is None:
        index.clear(tenant)
        subjects = sorted({e.key for e in store.entities(tenant)} | {f.subject for f in store.changes(tenant, EPOCH)})
    else:
        changed = {f.subject for f in store.changes(tenant, since)}
        if (policy or {}).get("relations"):
            for r in store.relation_changes(tenant, since):
                changed |= {r.src, r.dst}
        subjects = sorted(changed)
    docs, gone = [], []
    for s in subjects:
        text = render(store, tenant, s, policy)
        (docs.append((s, text)) if text is not None else gone.append(s))
    for i in range(0, len(docs), batch):
        part = docs[i : i + batch]
        vectors = embedder.embed([t for _, t in part])
        index.upsert(tenant, [(s, v, t) for (s, t), v in zip(part, vectors)])
    if gone:
        index.delete(tenant, gone)
    return IndexReport(indexed=len(docs), deleted=len(gone), cursor=cursor)


# ---- specs: how a deployment names its adapters --------------------------------------------


def _factory(spec: str) -> Any:
    target, _, query = spec.partition("?")
    module, _, name = target.partition(":")
    params = dict(parse_qsl(query))
    return getattr(importlib.import_module(module), name)(**params)


def load_index(spec: str) -> VectorIndex:
    """`memory`, `chroma:///abs/path` (persistent), `chroma://host:port` (server), `chroma://` (ephemeral),
    or `pkg.module:factory?param=value` for your own adapter."""
    if spec == "memory":
        return MemoryIndex()
    if spec.startswith("chroma:"):
        from keel.semantic_chroma import ChromaIndex

        u = urlparse(spec)
        if u.path and u.path != "/":
            return ChromaIndex(path=u.path)
        if u.hostname:
            return ChromaIndex(host=u.hostname, port=u.port or 8000)
        return ChromaIndex()
    return _factory(spec)


def load_embedder(spec: str) -> Embedder:
    """`openai` (any /v1/embeddings API, configured by KEEL_EMBEDDINGS_* env vars), `hash` (tests),
    or `pkg.module:factory?param=value` for your own adapter."""
    if spec == "openai":
        return OpenAIEmbedder.from_env()
    if spec.startswith("hash"):
        return HashEmbedder()
    return _factory(spec)
