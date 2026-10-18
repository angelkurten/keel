"""Semantic search: two adapter contracts, an indexer, and the adapters Keel ships.

    VectorIndex  where vectors live, namespaced by tenant (MemoryIndex here; ChromaIndex in
                 keel.semantic_chroma; WeaviateIndex in keel.semantic_weaviate; Qdrant, pgvector,
                 Pinecone are other implementations)
    Embedder     text -> vectors (OpenAIEmbedder for any /v1/embeddings API: OpenAI, OfoxAI, Azure,
                 Ollama, vLLM; HashEmbedder, deterministic, for tests)

Any project writes its own adapter by implementing the protocol and points Keel at it with a spec,
the same way stores are pluggable:

    keel index examples/fleet --tenant acme --db keel.db --index my_pkg.vectors:make?url=... --embedder my_pkg.emb:make
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
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse

from keel.scopes import GLOBAL_TENANT, Scope
from keel.store.base import Store


class VectorIndex(Protocol):
    """Where vectors live. Every call takes the tenant first; an index never returns another tenant's keys.

    Optional, detected at runtime, so a minimal adapter keeps working:
      query(..., where={"kind": [...]})   filter inside the index (the kind is the key's prefix); without
                                          it Keel filters after retrieval, which an approximate index can miss
      where={"subject": [...]}            also offered, with `related_to`: only those subjects' documents
                                          (a split document belongs to its subject); an index may ignore it
      text_query(tenant, text, k, where)  a lexical leg (BM25 or the engine's own); enables hybrid search
    """

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None: ...  # (subject key, vector, document)

    def delete(self, tenant: str, keys: list[str]) -> None: ...

    def query(self, tenant: str, vector: list[float], k: int) -> list[tuple[str, float]]: ...  # (key, score in [-1, 1]), best first

    def clear(self, tenant: str) -> None: ...


class Embedder(Protocol):
    """Text to vectors, one per input, in order, all of the same dimension. May take `role`
    ("query" or "document") for asymmetric models (e5's "query: "/"passage: ", Qwen3 instructions);
    Keel passes it only when the signature accepts it."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """Reorders a short list of candidates by relevance to the query: (key, score), best first."""

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]: ...


def accepts(fn: Any, name: str) -> bool:
    """Does an adapter method take this keyword? Optional capabilities are detected, never required."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def embed_as(embedder: Embedder, texts: list[str], role: str) -> list[list[float]]:
    return embedder.embed(texts, role=role) if accepts(embedder.embed, "role") else embedder.embed(texts)


def kind_of(key: str) -> str:
    return key.split(":", 1)[0]


SPLIT = "\x1f"  # a split document's key: subject, predicate and a hash of the value, joined by it


def subject_of(key: str) -> tuple[str, str | None]:
    """The subject a document belongs to, and the predicate when it is a split document."""
    subject, _, rest = key.partition(SPLIT)
    return subject, (rest.split(SPLIT, 1)[0] if rest else None)


def split_hash(key: str) -> str | None:
    """A split document's value hash (`value_hash` of the fact's value, or of one element of a list)."""
    return key.rsplit(SPLIT, 1)[1] if key.count(SPLIT) == 2 else None


TOKEN = re.compile(r"[a-z0-9áéíóúñü]+(?:-[a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    """Deterministic lexical tokens; identifiers like cus-1974 stay one token."""
    return TOKEN.findall(text.lower())


class BM25:
    """Okapi BM25 over documents, the lexical leg of hybrid search."""

    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75) -> None:
        self.size: dict[str, int] = {}
        self.postings: dict[str, dict[str, int]] = {}  # token -> {key: term frequency}: a query reads only its tokens' documents
        for key, text in docs.items():
            toks = tokens(text)
            self.size[key] = len(toks)
            for tok in toks:
                post = self.postings.setdefault(tok, {})
                post[key] = post.get(key, 0) + 1
        self.avg = sum(self.size.values()) / max(1, len(self.size))
        self.n, self.k1, self.b = len(self.size), k1, b

    def rank(self, query: str, keys: set[str] | None = None) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for tok in tokens(query):
            post = self.postings.get(tok, {})
            idf = math.log(1 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for key, tf in post.items():
                if keys is None or key in keys:
                    norm = self.k1 * (1 - self.b + self.b * self.size[key] / max(self.avg, 1e-9))
                    scores[key] = scores.get(key, 0.0) + idf * tf * (self.k1 + 1) / (tf + norm)
        return sorted(((k, s) for k, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))


class BM25Cache:
    """BM25 per (tenant, kinds), built once and reused until `stamp` changes or `ttl` seconds pass.
    The lexical leg of an index without its own: rebuilding it per query was most of a query's time."""

    def __init__(self, ttl: float = float("inf"), size: int = 64) -> None:
        self.ttl, self.size = ttl, size
        self._c: OrderedDict[tuple[str, tuple[str, ...] | None], tuple[Any, float, BM25]] = OrderedDict()

    def get(self, tenant: str, kinds: Any, stamp: Any, load: Callable[[], dict[str, str]]) -> BM25:
        key = (tenant, tuple(sorted(kinds)) if kinds else None)
        hit = self._c.get(key)
        if hit and hit[0] == stamp and time.monotonic() - hit[1] < self.ttl:
            self._c.move_to_end(key)
            return hit[2]
        bm = BM25(load())
        self._c[key] = (stamp, time.monotonic(), bm)
        self._c.move_to_end(key)
        while len(self._c) > self.size:  # ponytail: LRU by entry count; by documents held if tenants differ wildly in size
            self._c.popitem(last=False)
        return bm

    def drop(self, tenant: str) -> None:
        for key in [k for k in self._c if k[0] == tenant]:
            del self._c[key]


def lexical(bm: BM25, text: str, k: int, where: dict[str, Any] | None) -> list[tuple[str, float]]:
    """A cached BM25's ranking, narrowed to `where["subject"]` when given."""
    subjects = set(where["subject"]) if where and where.get("subject") is not None else None
    ranked = bm.rank(text)
    return [kv for kv in ranked if subject_of(kv[0])[0] in subjects][:k] if subjects is not None else ranked[:k]


def fuse(dense: list[tuple[str, float]], lexical: list[tuple[str, float]], w_dense: float = 0.7) -> list[tuple[str, float]]:
    """Convex fusion of min-max normalized scores (Bruch et al.): weighted toward dense, it keeps
    paraphrases where RRF's equal weight lets a weak lexical leg drag them down (docs/design/semantic-evals.md)."""

    def norm(xs: list[tuple[str, float]]) -> dict[str, float]:
        if not xs:
            return {}
        hi, lo = max(v for _, v in xs), min(v for _, v in xs)
        return {k: (v - lo) / (hi - lo) if hi > lo else 1.0 for k, v in xs}

    d, lx = norm(dense), norm(lexical)
    w_lex = (1 - w_dense) if lexical else 0.0
    w_d = 1 - w_lex
    return sorted(((k, w_d * d.get(k, 0.0) + w_lex * lx.get(k, 0.0)) for k in set(d) | set(lx)), key=lambda x: (-x[1], x[0]))


def _kinds(where: dict[str, Any] | None) -> set[str] | None:
    return set(where["kind"]) if where and where.get("kind") else None


def _subjects(where: dict[str, Any] | None) -> set[str] | None:
    return set(where["subject"]) if where and where.get("subject") is not None else None


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 0.0 if not na or not nb else sum(x * y for x, y in zip(a, b)) / (na * nb)


class MemoryIndex:
    """Reference index: exact cosine over a dict. Tests, demos, small tenants."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, tuple[list[float], str]]] = {}
        self._bm25 = BM25Cache()

    def upsert(self, tenant: str, items: list[tuple[str, list[float], str]]) -> None:
        bucket = self._data.setdefault(tenant, {})
        for key, vector, doc in items:
            bucket[key] = (list(vector), doc)
        self._bm25.drop(tenant)

    def delete(self, tenant: str, keys: list[str]) -> None:
        for key in keys:
            self._data.get(tenant, {}).pop(key, None)
        self._bm25.drop(tenant)

    def query(self, tenant: str, vector: list[float], k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        kinds, subjects = _kinds(where), _subjects(where)
        scored = [(key, cosine(vector, v)) for key, (v, _) in self._data.get(tenant, {}).items()
                  if (kinds is None or kind_of(key) in kinds) and (subjects is None or subject_of(key)[0] in subjects)]
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:k]

    def text_query(self, tenant: str, text: str, k: int, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        kinds = _kinds(where)
        load = lambda: {key: doc for key, (_, doc) in self._data.get(tenant, {}).items() if kinds is None or kind_of(key) in kinds}
        return lexical(self._bm25.get(tenant, kinds, None, load), text, k, where)

    def clear(self, tenant: str) -> None:
        self._data.pop(tenant, None)
        self._bm25.drop(tenant)


class HashEmbedder:
    """Deterministic bag of words hashed into a fixed dimension: shared words mean similar vectors.
    For tests and offline demos; it knows no synonyms."""

    def __init__(self, dim: int | str = 256) -> None:
        self.dim = int(dim)  # spec params arrive as text

    def embed(self, texts: list[str], role: str = "document") -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for token in re.findall(r"[a-z0-9áéíóúñü]+", text.lower()):
                v[int(hashlib.sha1(token.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(v)
        return out


class Unavailable(RuntimeError):
    """The embeddings provider is rate limiting or down, still after the retries: try again later
    (the HTTP server answers 503 with Retry-After)."""


class OpenAIEmbedder:
    """Any OpenAI-compatible /v1/embeddings endpoint, stdlib only.

    From the environment: KEEL_EMBEDDINGS_BASE_URL (default https://api.openai.com/v1),
    KEEL_EMBEDDINGS_MODEL (default text-embedding-3-small), KEEL_EMBEDDINGS_API_KEY.
    A rate limit (429), a 5xx or a dropped connection is retried with backoff (Retry-After when sent)."""

    RETRIES = 4

    def __init__(
        self, base_url: str = "https://api.openai.com/v1", model: str = "text-embedding-3-small", api_key: str | None = None,
        batch: int | str = 128, timeout: float | str = 60.0, query_prefix: str = "", document_prefix: str = "",
    ) -> None:
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.batch, self.timeout = int(batch), float(timeout)  # spec params arrive as text
        self.prefix = {"query": query_prefix, "document": document_prefix}  # e5: "query: " / "passage: "

    @classmethod
    def from_env(cls, **overrides: Any) -> OpenAIEmbedder:
        kw: dict[str, Any] = {
            "base_url": os.environ.get("KEEL_EMBEDDINGS_BASE_URL", "https://api.openai.com/v1"),
            "model": os.environ.get("KEEL_EMBEDDINGS_MODEL", "text-embedding-3-small"),
            "api_key": os.environ.get("KEEL_EMBEDDINGS_API_KEY"),
            "query_prefix": os.environ.get("KEEL_EMBEDDINGS_QUERY_PREFIX", ""),
            "document_prefix": os.environ.get("KEEL_EMBEDDINGS_DOCUMENT_PREFIX", ""),
        }
        kw.update(overrides)
        return cls(**kw)

    def embed(self, texts: list[str], role: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        prefix = self.prefix.get(role, "")
        for i in range(0, len(texts), self.batch):
            part = [prefix + t for t in texts[i : i + self.batch]]
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(f"{self.base_url}/embeddings", data=json.dumps({"model": self.model, "input": part}).encode(), headers=headers)
            for attempt in range(self.RETRIES + 1):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as res:
                        data = json.loads(res.read())["data"]
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == self.RETRIES:
                        raise (Unavailable if exc.code in (429, 500, 502, 503, 504) else RuntimeError)(f"embeddings {exc.code}: {exc.read()[:300].decode(errors='replace')}") from exc
                    wait = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2**attempt)
                except (urllib.error.URLError, TimeoutError) as exc:
                    if attempt == self.RETRIES:
                        raise Unavailable(f"embeddings: {exc}") from exc
                    time.sleep(2**attempt)
            out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        return out


class HTTPReranker:
    """The /rerank shape shared by Cohere, Jina, Voyage and vLLM: {model, query, documents, top_n} ->
    results[{index, relevance_score}]. From the environment: KEEL_RERANK_BASE_URL, KEEL_RERANK_MODEL,
    KEEL_RERANK_API_KEY."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, path: str = "/rerank", timeout: float | str = 30.0) -> None:
        self.url, self.model, self.api_key, self.timeout = base_url.rstrip("/") + path, model, api_key, float(timeout)

    @classmethod
    def from_env(cls) -> HTTPReranker:
        return cls(os.environ["KEEL_RERANK_BASE_URL"], os.environ["KEEL_RERANK_MODEL"], os.environ.get("KEEL_RERANK_API_KEY"))

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]:
        if not docs:
            return []
        body = {"model": self.model, "query": query, "documents": [d for _, d in docs], "top_n": len(docs)}
        res = _post_json(self.url, body, self.api_key, self.timeout)
        return sorted(((docs[r["index"]][0], float(r["relevance_score"])) for r in res["results"]), key=lambda x: -x[1])


class LLMReranker:
    """Any OpenAI-compatible /chat/completions model as a listwise reranker: it scores each candidate
    0-10 for the query. From the environment: KEEL_RERANK_BASE_URL, KEEL_RERANK_MODEL, KEEL_RERANK_API_KEY."""

    PROMPT = (
        "Score how well each record answers the search query, 0 (unrelated) to 10 (exactly what is asked). "
        'Reply with JSON only: {"scores": [n, n, ...]}, one number per record, in order.'
    )

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float | str = 60.0) -> None:
        self.url, self.model, self.api_key, self.timeout = base_url.rstrip("/") + "/chat/completions", model, api_key, float(timeout)

    @classmethod
    def from_env(cls) -> LLMReranker:
        return cls(os.environ["KEEL_RERANK_BASE_URL"], os.environ["KEEL_RERANK_MODEL"], os.environ.get("KEEL_RERANK_API_KEY"))

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]:
        if not docs:
            return []
        listing = "\n\n".join(f"[{i}]\n{d[:600]}" for i, (_, d) in enumerate(docs))
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": self.PROMPT}, {"role": "user", "content": f"Query: {query}\n\nRecords:\n{listing}"}]}
        res = _post_json(self.url, body, self.api_key, self.timeout)
        scores = json.loads(res["choices"][0]["message"]["content"]).get("scores", [])
        scored = [(key, float(scores[i]) / 10 if i < len(scores) else 0.0) for i, (key, _) in enumerate(docs)]
        return sorted(scored, key=lambda x: -x[1])


class OverlapReranker:
    """Deterministic, for tests: the share of query tokens found in the document."""

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]:
        q = set(tokens(query))
        return sorted(((k, len(q & set(tokens(d))) / max(1, len(q))) for k, d in docs), key=lambda x: (-x[1], x[0]))


def _post_json(url: str, body: dict[str, Any], api_key: str | None, timeout: float) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} {exc.code}: {exc.read()[:300].decode(errors='replace')}") from exc


# ---- what gets indexed --------------------------------------------------------------------

INDEXED_SCOPES = {Scope.GLOBAL, Scope.TENANT}
DEFAULT_LABELS = ("name", "nombre", "title", "titulo", "label", "plate", "model")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_VALUE_CHARS = 300


def render(store: Store, tenant: str, subject: str, policy: dict[str, Any] | None = None) -> str | None:
    """One subject as a short document: key, attributes, open accepted facts of tenant/global scope,
    and the open relations the cartridge names. `policy` is the cartridge's `semantic:` section:

        semantic:
          kinds: [card, cliente]          # absent: every kind
          predicates: [estado, resumen]   # what to include; or exclude: [comentario]
          attrs: true                     # entity attributes (default true); or a list of names
          relations: [de_cliente]         # open relations: the other side's key and its label
          labels: [name, nombre]          # which attribute labels the other side (first present wins)
          split: {visual: [campo]}        # also one document per open fact of these predicates
          lexical_weight: 0.3             # hybrid search: weight of the lexical leg (0 = dense only)
          min_score: 0.4                  # optional: drop results below this cosine (model-specific!)

    None when there is nothing to index (not an indexed kind, or nothing left): remove it."""
    policy = policy or {}
    kind = subject.split(":", 1)[0]
    if policy.get("kinds") and kind not in policy["kinds"]:
        return None
    now = datetime.now(timezone.utc)
    ent = store.entity(tenant, subject)
    facts = [f for f in store.facts(tenant, subject, at=now) if f.status is None and f.scope in INDEXED_SCOPES]
    facts += [f for f in store.facts(GLOBAL_TENANT, subject, at=now) if f.status is None and f.scope == Scope.GLOBAL]
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
    labels = policy.get("labels", DEFAULT_LABELS)
    for rel in policy.get("relations") or []:
        for r in store.relations(tenant, subject, rel=rel, at=now):
            other = r.dst if r.src == subject else r.src
            ent2 = store.entity(tenant, other)
            label = next((str(ent2.attrs[k]) for k in labels if ent2 and ent2.attrs.get(k) is not None), None)
            lines.append(f"{rel}: {other}" + (f" ({label})" if label else ""))
    if ent is None and len(lines) == 1:
        return None
    return "\n".join(lines)


def value_hash(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def split_docs(store: Store, tenant: str, subject: str, policy: dict[str, Any] | None = None) -> tuple[list[tuple[str, str]], set[str]]:
    """The extra documents `semantic.split` asks for: one per open accepted fact of the listed predicates,
    and one per element when the value is a list (a visual with twenty fields is twenty sentences, not one
    diluted text), and every key such a document ever had for this subject, from its history: the stale
    ones to delete when a value changes."""
    preds = set(((policy or {}).get("split") or {}).get(subject.split(":", 1)[0]) or ())
    if not preds:
        return [], set()
    history = store.facts(tenant, subject) + [f for f in store.facts(GLOBAL_TENANT, subject) if f.scope == Scope.GLOBAL]
    key = lambda f, v: f"{subject}{SPLIT}{f.predicate}{SPLIT}{value_hash(v)}"
    parts = lambda f: f.value if isinstance(f.value, list) and f.value else [f.value]
    # the whole value's key too: a list indexed as one document before 0.20 is deleted when it changes
    every = {key(f, v) for f in history if f.predicate in preds for v in [f.value, *parts(f)]}
    ent = store.entity(tenant, subject)
    labels = (policy or {}).get("labels", DEFAULT_LABELS)
    label = next((str(ent.attrs[k]) for k in labels if ent and ent.attrs.get(k) is not None), None)
    now = datetime.now(timezone.utc)
    docs = {}
    for f in history:
        if f.predicate in preds and f.status is None and f.scope in INDEXED_SCOPES and f.valid_from <= now and (f.valid_to is None or f.valid_to > now):
            for v in parts(f):
                docs[key(f, v)] = f"{subject}" + (f" ({label})" if label else "") + f" {f.predicate}: {as_text(v)[:MAX_VALUE_CHARS]}"
    return list(docs.items()), every


@dataclass
class IndexReport:
    indexed: int
    deleted: int
    cursor: datetime | None  # pass it back as `since` next time


def index_tenant(
    store: Store, tenant: str, embedder: Embedder, index: VectorIndex, since: datetime | None = None, batch: int = 64,
    policy: dict[str, Any] | None = None, workers: int = 1,
) -> IndexReport:
    """Bring the index up to date for one tenant. `since=None` rebuilds it; a watermark updates the
    subjects whose facts changed after it, and both ends of every link recorded after it (an indexed
    relation names the other side). `policy` is the cartridge's `semantic:` section. Entity attributes
    and unlinks carry no transaction time, so changes to them alone are picked up by `--full`.
    `workers` embeds that many batches at once (an embeddings API is a round trip per batch); the
    index is still written one batch at a time, in order, from this thread."""
    forgotten = getattr(store, "forgotten", None)
    if forgotten and forgotten(tenant) is not None:  # erased (Store.forget): its documents go too
        index.clear(tenant)
        return IndexReport(indexed=0, deleted=0, cursor=None)
    marks = [m for m in (store.watermark(tenant), store.watermark(GLOBAL_TENANT)) if m is not None]
    cursor = max(marks) if marks else None
    # global memory is every tenant's: a global fact re-indexes the subject where this tenant knows it
    known = lambda s: store.entity(tenant, s) is not None or bool(store.facts(tenant, s))
    if since is None:
        index.clear(tenant)
        subjects = sorted({e.key for e in store.entities(tenant)} | {f.subject for f in store.changes(tenant, EPOCH)})
    else:
        changed = {f.subject for f in store.changes(tenant, since)}
        changed |= {f.subject for f in store.changes(GLOBAL_TENANT, since) if known(f.subject)}
        if (policy or {}).get("relations"):
            for r in store.relation_changes(tenant, since):
                changed |= {r.src, r.dst}
        subjects = sorted(changed)
    docs, gone, indexed = [], [], 0
    for s in subjects:
        text = render(store, tenant, s, policy)
        extra, every = split_docs(store, tenant, s, policy) if text is not None else ([], split_docs(store, tenant, s, policy)[1])
        if text is not None:
            docs.append((s, text))
            indexed += 1
        else:
            gone.append(s)
        docs += extra
        gone += sorted(every - {k for k, _ in extra})
    parts = [docs[i : i + batch] for i in range(0, len(docs), batch)]
    embed = lambda part: embed_as(embedder, [t for _, t in part], "document")
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for part, vectors in zip(parts, pool.map(embed, parts)):  # map yields in order, as each batch lands
            index.upsert(tenant, [(s, v, t) for (s, t), v in zip(part, vectors)])
    if gone:
        index.delete(tenant, gone)
    return IndexReport(indexed=indexed, deleted=len([g for g in gone if SPLIT not in g]), cursor=cursor)


# ---- specs: how a deployment names its adapters --------------------------------------------


def _factory(spec: str) -> Any:
    target, _, query = spec.partition("?")
    module, _, name = target.partition(":")
    params = dict(parse_qsl(query))
    return getattr(importlib.import_module(module), name)(**params)


def load_index(spec: str) -> VectorIndex:
    """`memory`, `chroma:///abs/path` (persistent), `chroma://host:port` (server), `chroma://` (ephemeral),
    `weaviate://host:port?grpc_port=50051&secure=true&collection=Keel` (API key from KEEL_WEAVIATE_API_KEY),
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
    if spec.startswith("weaviate:"):
        from keel.semantic_weaviate import WeaviateIndex

        u = urlparse(spec)
        return WeaviateIndex(host=u.hostname or "localhost", port=u.port or 8080, **dict(parse_qsl(u.query)))
    return _factory(spec)


def load_reranker(spec: str) -> Reranker:
    """`http` (the Cohere/Jina/Voyage /rerank shape), `llm` (any /chat/completions model), both from
    KEEL_RERANK_* env vars; `overlap` (tests); or `pkg.module:factory?param=value`."""
    if spec == "http":
        return HTTPReranker.from_env()
    if spec == "llm":
        return LLMReranker.from_env()
    if spec == "overlap":
        return OverlapReranker()
    return _factory(spec)


def load_embedder(spec: str) -> Embedder:
    """`openai` (any /v1/embeddings API, configured by KEEL_EMBEDDINGS_* env vars), `hash` (tests),
    or `pkg.module:factory?param=value` for your own adapter."""
    if spec == "openai":
        return OpenAIEmbedder.from_env()
    if spec.startswith("hash"):
        return HashEmbedder()
    return _factory(spec)
