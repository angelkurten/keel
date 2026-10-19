"""The baseline's retrieval: every chunk embedded once (text-embedding-3-small through OfoxAI), kept in a JSON
file, and searched by cosine similarity, filtered to the customer in question. No vector database to run:
885 chunks fit in memory and a linear scan is a few milliseconds."""

import json
import math
from pathlib import Path
from typing import Any

from showcase.data import Chunk, chunks

EMBEDDING_MODEL = "openai/text-embedding-3-small"


def embed(http: Any, base_url: str, key: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 256):
        r = http.post(f"{base_url}/embeddings", json={"model": EMBEDDING_MODEL, "input": texts[i : i + 256]},
                      headers={"Authorization": f"Bearer {key}"}, timeout=120)
        r.raise_for_status()
        out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    return out


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Index:
    def __init__(self, items: list[Chunk], vectors: list[list[float]]) -> None:
        self.items, self.vectors = items, [_norm(v) for v in vectors]

    @classmethod
    def load_or_build(cls, path: Path, http: Any, base_url: str, key: str) -> "Index":
        items = chunks()
        texts = [c.text for c in items]
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get("texts") == texts:  # the data or the help center changed: embed again
                return cls(items, saved["vectors"])
        vectors = embed(http, base_url, key, texts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": EMBEDDING_MODEL, "texts": texts, "vectors": vectors}))
        return cls(items, vectors)

    def search(self, query_vector: list[float], tenant: str | None, k: int = 8, extra: list[tuple[Chunk, list[float]]] = ()) -> list[Chunk]:
        """The k chunks nearest to the query among `tenant`'s (or the help center's, with tenant None), plus `extra`
        (the notes saved during a run)."""
        q = _norm(query_vector)
        pool = [(c, v) for c, v in zip(self.items, self.vectors, strict=True) if c.tenant == tenant] + [(c, _norm(v)) for c, v in extra if c.tenant == tenant]
        scored = sorted(pool, key=lambda cv: -sum(a * b for a, b in zip(q, cv[1], strict=True)))
        return [c for c, _ in scored[:k]]
