"""Graphiti and Mem0 as benchmark systems: ingest a tenant's log with an LLM extractor, then search it.

    # Neo4j for Graphiti: docker run -d --name bench-neo4j -p 7699:7687 -v <dir>:/data -e NEO4J_AUTH=neo4j/benchpass neo4j:5.26
    OFOX_API_KEY=$(cat ~/.ofox_key) uv run --extra mcp --with tiktoken --with graphiti-core==0.30.2 --with "mem0ai[extras]==2.2.1" \
        python -m evals.jira.memories ingest --system graphiti --tenant mariadb --data ~/datasets/public-jira/export \
        --model anthropic/claude-haiku-5.5 [--limit 20]

Both read the subset of the log the questions touch (the asked issues and the issues linked to them: `subset`),
in the order it was written, one record per event as RAG reads it, with when it happened as the episode's time.
Ingesting the whole log would take Graphiti ~134 hours (9.3 s and ~7.4k tokens per event, measured); the subset
gives them less noise than the other systems face, which favours them, and the paper says so.

Configured as their own guides say for an OpenAI-compatible provider: Graphiti with OpenAIGenericClient
(json_schema structured output) on Neo4j; Mem0 with its openai LLM and embedder pointed at the gateway, Qdrant
embedded on disk, BM25 on (the `extras`). Ingest is resumable: what is done is listed in a file next to the data,
with every LLM call's tokens, so the ingest cost per event is measured, not estimated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.jira.bench import log, record

BASE = "https://api.ofox.ai/v1"
EMBEDDING_MODEL, EMBEDDING_DIMS = "openai/text-embedding-3-small", 512
NEO4J = ("bolt://localhost:7699", "neo4j", "benchpass")


def subset(data: Path, tenant: str) -> list[dict[str, Any]]:
    """The events of the issues the questions ask about and of the issues linked to them, in log order."""
    asked = {q["params"]["subject"] for q in json.loads((data / f"{tenant}.questions.json").read_text())}
    events = log(data, tenant)
    keep = set(asked)
    for e in events:
        if e["type"] in ("issue.linked", "issue.unlinked"):
            ends = {e["subject"], f"issue:{e['payload']['other']}"}
            if ends & asked:
                keep |= ends
    return [e for e in events if e["subject"] in keep]


class Meter:
    """Tokens of every LLM call the library makes, through the OpenAI client it is given."""

    def __init__(self) -> None:
        self.calls = self.input = self.output = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input += getattr(usage, "prompt_tokens", 0) or 0
        self.output += getattr(usage, "completion_tokens", 0) or 0


# ---- graphiti -----------------------------------------------------------------------------


def graphiti(model: str, meter: Meter | None = None) -> Any:
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from openai import AsyncOpenAI

    key = os.environ["OFOX_API_KEY"]
    client = AsyncOpenAI(api_key=key, base_url=BASE)
    if meter is not None:
        create = client.chat.completions.create

        async def metered(*args: Any, **kw: Any) -> Any:
            r = await create(*args, **kw)
            meter.add(r.usage)
            return r

        client.chat.completions.create = metered  # type: ignore[method-assign]
    cfg = LLMConfig(api_key=key, model=model, small_model=model, base_url=BASE, temperature=0)
    return Graphiti(*NEO4J, llm_client=OpenAIGenericClient(config=cfg, client=client),
                    embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key=key, base_url=BASE, embedding_model=EMBEDDING_MODEL, embedding_dim=EMBEDDING_DIMS)),
                    cross_encoder=OpenAIRerankerClient(config=cfg, client=client))


async def graphiti_ingest(events: list[dict[str, Any]], tenant: str, model: str, done: set[str], progress: Path, meter: Meter) -> None:
    from graphiti_core.nodes import EpisodeType

    g = graphiti(model, meter)
    await g.build_indices_and_constraints()
    try:
        for e in events:
            if e["id"] in done:
                continue
            before, start = (meter.calls, meter.input, meter.output), time.monotonic()
            await g.add_episode(name=e["id"], episode_body=record(e)[0], source_description="Jira change log",
                                reference_time=datetime.fromisoformat(e["occurred_at"]), source=EpisodeType.text, group_id=tenant)
            note(progress, e, before, meter, start)
    finally:
        await g.close()


def graphiti_search(tenant: str, model: str, query: str, k: int) -> list[dict[str, Any]]:
    async def go() -> list[Any]:
        g = graphiti(model)
        try:
            return await g.search(query, group_ids=[tenant], num_results=k)
        finally:
            await g.close()

    def t(x: datetime | None) -> str | None:
        return x.strftime("%Y-%m-%d %H:%M:%S UTC") if x else None

    return [{"fact": e.fact, "valid_from": t(e.valid_at), "valid_until": t(e.invalid_at), "learned": t(e.created_at), "superseded": t(e.expired_at)}
            for e in asyncio.run(go())]


# ---- mem0 ---------------------------------------------------------------------------------


def mem0(data: Path, tenant: str, model: str) -> Any:
    from mem0 import Memory

    key = os.environ["OFOX_API_KEY"]
    store = data / "mem0" / tenant
    store.mkdir(parents=True, exist_ok=True)
    return Memory.from_config({
        "llm": {"provider": "openai", "config": {"model": model, "api_key": key, "openai_base_url": BASE, "temperature": 0}},
        "embedder": {"provider": "openai", "config": {"model": EMBEDDING_MODEL, "api_key": key, "openai_base_url": BASE, "embedding_dims": EMBEDDING_DIMS}},
        "vector_store": {"provider": "qdrant", "config": {"collection_name": tenant, "path": str(store / "qdrant"), "embedding_model_dims": EMBEDDING_DIMS, "on_disk": True}},
        "history_db_path": str(store / "history.db"),
    })


def mem0_ingest(events: list[dict[str, Any]], data: Path, tenant: str, model: str, done: set[str], progress: Path, meter: Meter) -> None:
    import openai

    create = openai.resources.chat.completions.Completions.create

    def metered(self: Any, *args: Any, **kw: Any) -> Any:
        r = create(self, *args, **kw)
        meter.add(r.usage)
        return r

    openai.resources.chat.completions.Completions.create = metered  # type: ignore[method-assign]
    m = mem0(data, tenant, model)
    for e in events:
        if e["id"] in done:
            continue
        before, start = (meter.calls, meter.input, meter.output), time.monotonic()
        # the OSS SDK takes no timestamp: time travels in the text and the metadata, as its guide does for dates
        m.add([{"role": "user", "content": record(e)[0]}], user_id=tenant,
              metadata={"issue": e["subject"].split(":", 1)[1], "happened": e["occurred_at"], "recorded": e["recorded_at"]})
        note(progress, e, before, meter, start)


_mem0_cache: dict[tuple[str, str], Any] = {}


def mem0_search(data: Path, tenant: str, model: str, query: str, k: int) -> list[dict[str, Any]]:
    if (tenant, model) not in _mem0_cache:  # one client per tenant: embedded Qdrant locks its directory
        _mem0_cache[(tenant, model)] = mem0(data, tenant, model)
    hits = _mem0_cache[(tenant, model)].search(query, filters={"user_id": tenant}, top_k=k)["results"]
    return [{"memory": h["memory"], "happened": (h.get("metadata") or {}).get("happened"), "recorded": (h.get("metadata") or {}).get("recorded")} for h in hits]


# ---- ingest bookkeeping -----------------------------------------------------------------------


def note(progress: Path, e: dict[str, Any], before: tuple[int, int, int], meter: Meter, start: float) -> None:
    with progress.open("a") as fh:
        fh.write(json.dumps({"id": e["id"], "calls": meter.calls - before[0], "input": meter.input - before[1], "output": meter.output - before[2],
                             "seconds": round(time.monotonic() - start, 2)}) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["ingest", "stats"])
    ap.add_argument("--system", choices=["graphiti", "mem0"], required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", default="anthropic/claude-haiku-5.5", help="the extractor")
    ap.add_argument("--limit", type=int, help="ingest only the first N events of the subset (a trial)")
    a = ap.parse_args()
    for name in ("httpx", "neo4j", "graphiti_core"):
        logging.getLogger(name).setLevel(logging.WARNING)
    data = a.data.expanduser()
    progress = data / f"{a.tenant}.{a.system}.{a.model.replace('/', '_')}.ingest.jsonl"
    events = subset(data, a.tenant)[: a.limit]
    done = {json.loads(line)["id"] for line in progress.open()} if progress.exists() else set()
    if a.command == "ingest":
        print(f"{a.system} {a.tenant}: {len(events)} events in the subset, {len(done & {e['id'] for e in events})} already ingested")
        meter = Meter()
        if a.system == "graphiti":
            asyncio.run(graphiti_ingest(events, a.tenant, a.model, done, progress, meter))
        else:
            mem0_ingest(events, data, a.tenant, a.model, done, progress, meter)
    rows = [json.loads(line) for line in progress.open()] if progress.exists() else []
    if rows:
        n = len(rows)
        print(f"{n} events ingested: {sum(r['calls'] for r in rows) / n:.1f} LLM calls, {sum(r['input'] for r in rows) / n:.0f} input and "
              f"{sum(r['output'] for r in rows) / n:.0f} output tokens, {sum(r['seconds'] for r in rows) / n:.1f} s per event")


if __name__ == "__main__":
    main()
