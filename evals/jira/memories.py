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

from evals.efficiency.run import pause
from evals.jira.bench import log, record

BASE = "https://api.ofox.ai/v1"
# when the extractor's provider refuses an extraction prompt (OpenAI's filter refuses some harmless Jira text that
# Haiku and DeepSeek accept, seen 2026-10-20), that one event is extracted with this model instead, and recorded
FALLBACKS = ("anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash-1017")  # in order; a model that is down is skipped
FALLBACK = FALLBACKS[0]
COOL_DOWN = 30  # seconds after a refusal: a burst of refusals trips the gateway's circuit breaker
EMBEDDING_MODEL, EMBEDDING_DIMS = "openai/text-embedding-3-small", 512
NEO4J = ("bolt://localhost:7699", "neo4j", "benchpass")


def subset(data: Path, tenant: str) -> list[dict[str, Any]]:
    """The events of the issues the one-issue questions ask about and of the issues linked to them, in log order.
    Questions over the whole tenant need the whole log, which these systems cannot ingest at a reasonable cost
    (~134 hours for Graphiti); they are measured on Keel, RAG and the API only, and the paper says so."""
    asked = {q["params"]["subject"] for q in json.loads((data / f"{tenant}.questions.json").read_text()) if "subject" in q["params"]}
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
            for wait in backoff():
                try:
                    await g.add_episode(name=e["id"], episode_body=record(e)[0], source_description="Jira change log",
                                        reference_time=datetime.fromisoformat(e["occurred_at"]), source=EpisodeType.text, group_id=tenant)
                    break
                except Exception as exc:
                    if refused(exc):
                        break
                    if not limited(exc, wait):
                        raise
                    await asyncio.sleep(delay(exc, wait))
            outcome = None
            if refused_last:
                await asyncio.sleep(COOL_DOWN)

                def use(name: str) -> None:
                    g.llm_client.model, g.llm_client.small_model = name, name

                async def add(e: dict[str, Any] = e) -> None:
                    await g.add_episode(name=e["id"], episode_body=record(e)[0], source_description="Jira change log",
                                        reference_time=datetime.fromisoformat(e["occurred_at"]), source=EpisodeType.text, group_id=tenant)

                try:
                    outcome = await fallback_async(add, use)
                finally:
                    use(model)
            note(progress, e, before, meter, start, skipped=outcome)
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
        for wait in backoff():
            try:
                m.add([{"role": "user", "content": record(e)[0]}], user_id=tenant,
                      metadata={"issue": e["subject"].split(":", 1)[1], "happened": e["occurred_at"], "recorded": e["recorded_at"]})
                break
            except Exception as exc:
                if refused(exc):
                    break
                if not limited(exc, wait):
                    raise
                time.sleep(delay(exc, wait))
        outcome = None
        if refused_last:
            time.sleep(COOL_DOWN)

            def use(name: str) -> None:
                m.llm.config.model = name

            try:
                outcome = fallback_sync(lambda e=e: m.add([{"role": "user", "content": record(e)[0]}], user_id=tenant,
                                                      metadata={"issue": e["subject"].split(":", 1)[1], "happened": e["occurred_at"], "recorded": e["recorded_at"]}), use)
            finally:
                use(model)
        note(progress, e, before, meter, start, skipped=outcome)


_mem0_cache: dict[tuple[str, str], Any] = {}


def mem0_search(data: Path, tenant: str, model: str, query: str, k: int) -> list[dict[str, Any]]:
    if (tenant, model) not in _mem0_cache:  # one client per tenant: embedded Qdrant locks its directory
        _mem0_cache[(tenant, model)] = mem0(data, tenant, model)
    hits = _mem0_cache[(tenant, model)].search(query, filters={"user_id": tenant}, top_k=k)["results"]
    return [{"memory": h["memory"], "happened": (h.get("metadata") or {}).get("happened"), "recorded": (h.get("metadata") or {}).get("recorded")} for h in hits]


# ---- ingest bookkeeping -----------------------------------------------------------------------


def backoff() -> list[float]:
    """Waits between attempts at one event when the gateway's upstream is down, ~35 minutes in all; the last raises.
    A rate limit waits only delay()'s few seconds per attempt."""
    return [5, 10, 20, 40] + [60] * 34 + [0]


def limited(exc: Exception, wait: float) -> bool:
    """Retry this exception? A rate limit (429), or the gateway's upstream account outage (a 401 that says
    'deactivated', which came and went on 2026-10-20), and not after the last wait. A retried event may have
    written part of its extraction before failing; Graphiti and Mem0 deduplicate what the retry writes again."""
    return wait > 0 and (transient(exc) or type(exc).__name__ == "RateLimitError")


refused_last = False  # whether the last event was refused; one ingest per process, so a module flag is enough


def refused(exc: Exception) -> bool:
    """The provider's content filter refused the extraction prompt (it carries memories retrieved for context, so a
    harmless event can trip it). The event is recorded as not stored, and the ingest goes on: a loss to measure."""
    global refused_last
    refused_last = "content safety" in str(exc) or "content_filter" in str(exc)
    return refused_last


OUTAGES = ("has been deactivated", "circuit breaker", "not allowed for this account", "protocol_not_supported")  # the gateway's upstream account, down for a while


def fallback_sync(add: Any, use: Any) -> str:
    """Extract one refused event with the FALLBACKS in order: a model the filter refuses, or one that is down, is passed
    over for the next; only when every one is down at once does it wait. Returns `fallback:<model>`, or
    `content_filter` when every model refused it (not stored)."""
    refused_by: set[str] = set()
    last: Exception | None = None
    for wait in backoff():
        for name in FALLBACKS:
            if name in refused_by:
                continue
            use(name)
            try:
                add()
                return f"fallback:{name}"
            except Exception as exc:
                if refused(exc):
                    refused_by.add(name)
                elif not transient(exc):
                    raise
                last = exc
        if len(refused_by) == len(FALLBACKS):
            return "content_filter"
        if not wait:
            raise RuntimeError("every fallback model stayed down")
        time.sleep(delay(last, wait))
    return "content_filter"


async def fallback_async(add: Any, use: Any) -> str:
    """fallback_sync for Graphiti's async writes."""
    refused_by: set[str] = set()
    last: Exception | None = None
    for wait in backoff():
        for name in FALLBACKS:
            if name in refused_by:
                continue
            use(name)
            try:
                await add()
                return f"fallback:{name}"
            except Exception as exc:
                if refused(exc):
                    refused_by.add(name)
                elif not transient(exc):
                    raise
                last = exc
        if len(refused_by) == len(FALLBACKS):
            return "content_filter"
        if not wait:
            raise RuntimeError("every fallback model stayed down")
        await asyncio.sleep(delay(last, wait))
    return "content_filter"


def delay(exc: Exception | None, wait: float) -> float:
    """The wait before retrying after `exc`: backoff()'s long `wait` for an outage, a few seconds for a rate limit."""
    if exc is None or any(o in str(exc) for o in OUTAGES):
        return wait
    return pause(getattr(getattr(exc, "response", None), "headers", None))


def transient(exc: Exception) -> bool:
    """A rate limit (429), or the gateway's upstream accounts down (OpenAI deactivated, Anthropic not allowed, a
    circuit breaker; each came and went on 2026-10-20): waited out, never counted against a system."""
    return "429" in str(exc) or any(o in str(exc) for o in OUTAGES)



def note(progress: Path, e: dict[str, Any], before: tuple[int, int, int], meter: Meter, start: float, skipped: str | None = None) -> None:
    global refused_last
    refused_last = False
    row = {"id": e["id"], "calls": meter.calls - before[0], "input": meter.input - before[1], "output": meter.output - before[2],
           "seconds": round(time.monotonic() - start, 2)}
    with progress.open("a") as fh:  # "fallback[:<model>]": extracted by a fallback after a refusal; "content_filter": refused by all, not stored
        fh.write(json.dumps(row | ({"refused": skipped} if skipped else {})) + "\n")


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
    os.environ.setdefault("SEMAPHORE_LIMIT", "3")  # Graphiti's concurrency: six ingests share the gateway's per-minute limit
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
        refusals = [r.get("refused") for r in rows if r.get("refused")]
        fell = [x for x in refusals if x.startswith("fallback")]
        print(f"{n} events ingested ({len(fell)} extracted by a fallback after a refusal: { {x: fell.count(x) for x in set(fell)} }; "
              f"{refusals.count('content_filter')} not stored): "
              f"{sum(r['calls'] for r in rows) / n:.1f} LLM calls, "
              f"{sum(r['input'] for r in rows) / n:.0f} input and {sum(r['output'] for r in rows) / n:.0f} output tokens, {sum(r['seconds'] for r in rows) / n:.1f} s per event")


if __name__ == "__main__":
    main()
