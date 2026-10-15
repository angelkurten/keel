"""Consolidation: episodic memory summarized into durable facts, declared in the cartridge.

    consolidate:
      recurring_issue:
        from: { predicate: assessment, scopes: [task] }          # the episodic memory (default: task; agent only when listed)
        applies_to: tire                                         # optional: one kind
        within: 30d                                              # optional: only memory whose valid_from is in this window
        min_count: 3                                             # how many observations make a pattern (default 2)
        reduce: mode                                             # mode | latest | count | min | max | avg | distinct | summary
        into: { predicate: recurring_issue, scope: tenant }      # the durable fact (scope default: tenant)

`keel consolidate` (or `keel expire --consolidate`, which runs it before dropping expired memory) is a
job, never part of ingest. The originals stay until their TTL; the consolidated fact carries
`source=consolidate:<name>`, evidence naming what it summarizes, and an idempotency key computed
from its inputs: the same inputs never write twice, new ones write a new value that closes the old.
`reduce: summary` asks a Summarizer adapter (LLMSummarizer, or yours) for a sentence; everything else
is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from keel.cartridge import Cartridge, _parse_ttl
from keel.scopes import Scope
from keel.store.base import Fact, Store

REDUCERS = ("mode", "latest", "count", "min", "max", "avg", "distinct", "summary")


class Summarizer(Protocol):
    """Writes one durable sentence from a subject's observations: (recorded_at ISO, value, source), oldest first."""

    def summarize(self, subject: str, predicate: str, items: list[tuple[str, Any, str | None]]) -> str: ...


class JoinSummarizer:
    """Deterministic, for tests and offline runs: the distinct values, joined."""

    def summarize(self, subject: str, predicate: str, items: list[tuple[str, Any, str | None]]) -> str:
        seen: list[str] = []
        for _, v, _ in items:
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            if text not in seen:
                seen.append(text)
        return "; ".join(seen)


class LLMSummarizer:
    """Any OpenAI-compatible /chat/completions model. From the environment: KEEL_SUMMARIZER_BASE_URL,
    KEEL_SUMMARIZER_MODEL, KEEL_SUMMARIZER_API_KEY."""

    PROMPT = (
        "You consolidate an AI agent's notes about one record into one durable fact. Write one short sentence "
        "(at most 30 words) stating the pattern the notes support, in the notes' language. No preamble, no speculation "
        "beyond the notes."
    )

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float | str = 60.0) -> None:
        self.url, self.model, self.api_key, self.timeout = base_url.rstrip("/") + "/chat/completions", model, api_key, float(timeout)

    @classmethod
    def from_env(cls) -> LLMSummarizer:
        return cls(os.environ["KEEL_SUMMARIZER_BASE_URL"], os.environ["KEEL_SUMMARIZER_MODEL"], os.environ.get("KEEL_SUMMARIZER_API_KEY"))

    def summarize(self, subject: str, predicate: str, items: list[tuple[str, Any, str | None]]) -> str:
        from keel.semantic import _post_json

        notes = "\n".join(f"- {t[:10]} ({src or 'unknown'}): {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for t, v, src in items)
        body = {"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": self.PROMPT},
            {"role": "user", "content": f"Record: {subject}\nNotes about '{predicate}':\n{notes}"},
        ]}
        return _post_json(self.url, body, self.api_key, self.timeout)["choices"][0]["message"]["content"].strip()


def load_summarizer(spec: str) -> Summarizer:
    """`llm` (KEEL_SUMMARIZER_* env vars), `join` (tests), or `pkg.module:factory?param=value`."""
    if spec == "llm":
        return LLMSummarizer.from_env()
    if spec == "join":
        return JoinSummarizer()
    from keel.semantic import _factory

    return _factory(spec)


# agent memory is private: it is consolidated (and so promoted) only when a cartridge lists it
DEFAULT_SCOPES = ["task"]


def problems(cartridge: Cartridge) -> list[str]:
    """Shape errors in the `consolidate:` section, reported by `keel validate`."""
    out = []
    scopes = {s.value for s in Scope}
    for name, spec in (cartridge.consolidate or {}).items():
        p = f"consolidate '{name}'"
        if not isinstance(spec, dict):
            out.append(f"{p}: a mapping with from, reduce and into")
            continue
        src, dst = spec.get("from") or {}, spec.get("into") or {}
        if not src.get("predicate") or not dst.get("predicate"):
            out.append(f"{p}: `from.predicate` and `into.predicate` are required")
        if spec.get("reduce", "mode") not in REDUCERS:
            out.append(f"{p}: reduce is one of {', '.join(REDUCERS)}")
        for s in src.get("scopes", DEFAULT_SCOPES):
            if s not in scopes:
                out.append(f"{p}: unknown scope '{s}'")
        if dst.get("scope", "tenant") not in scopes:
            out.append(f"{p}: unknown scope '{dst.get('scope')}' in into")
        if spec.get("applies_to") and spec["applies_to"] not in cartridge.entities:
            out.append(f"{p}: applies_to '{spec['applies_to']}' is not a declared kind")
        if src.get("predicate") and src.get("predicate") == dst.get("predicate"):
            out.append(f"{p}: into.predicate must differ from from.predicate")
    return out


@dataclass
class ConsolidationReport:
    written: dict[str, int] = field(default_factory=dict)  # consolidation -> facts written
    unchanged: dict[str, int] = field(default_factory=dict)  # consolidation -> subjects already up to date


def _value(reduce: str, facts: list[Fact], subject: str, predicate: str, summarizer: Summarizer | None) -> Any:
    values = [f.value for f in facts]  # oldest first
    if reduce == "latest":
        return values[-1]
    if reduce == "count":
        return len(values)
    if reduce in ("min", "max", "avg"):
        nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums:
            return None
        return min(nums) if reduce == "min" else max(nums) if reduce == "max" else round(sum(nums) / len(nums), 6)
    if reduce == "distinct":
        out: list[Any] = []
        for v in values:
            if v not in out:
                out.append(v)
        return out
    if reduce == "summary":
        if summarizer is None:
            raise ValueError("reduce: summary needs a summarizer (keel consolidate --summarizer llm, or your adapter)")
        return summarizer.summarize(subject, predicate, [(f.valid_from.isoformat(), f.value, f.source) for f in facts])
    counts = Counter(json.dumps(v, sort_keys=True, default=str) for v in values)  # mode; a tie goes to the latest
    best = max(counts.values())
    return next(v for v in reversed(values) if counts[json.dumps(v, sort_keys=True, default=str)] == best)


def consolidate_tenant(
    cartridge: Cartridge, store: Store, tenant: str, summarizer: Summarizer | None = None, now: datetime | None = None,
    only: list[str] | None = None,
) -> ConsolidationReport:
    """Run every consolidation the cartridge declares (or `only` those) for one tenant."""
    now = now or datetime.now(timezone.utc)
    report = ConsolidationReport()
    facts = [o for o in store.dump(tenant) if isinstance(o, Fact)]  # expired-but-not-yet-dropped memory counts too
    for name, spec in (cartridge.consolidate or {}).items():
        if only and name not in only:
            continue
        src, dst = spec["from"], spec["into"]
        scopes = {Scope(s) for s in src.get("scopes", DEFAULT_SCOPES)}
        window = _parse_ttl(spec["within"]) if spec.get("within") else None
        since = now - window if window else None
        kind = spec.get("applies_to")
        by_subject: dict[str, list[Fact]] = {}
        for f in facts:
            if f.predicate != src["predicate"] or f.scope not in scopes or f.status is not None:
                continue
            if kind and f.subject.split(":", 1)[0] != kind:
                continue
            if since and f.valid_from < since:  # world time: a reingested history keeps its dates
                continue
            by_subject.setdefault(f.subject, []).append(f)
        written = unchanged = 0
        reduce = spec.get("reduce", "mode")
        for subject, group in sorted(by_subject.items()):
            if len(group) < int(spec.get("min_count", 2)):
                continue
            group.sort(key=lambda f: (f.valid_from, f.recorded_at or now))
            digest = hashlib.sha256(json.dumps([(f.valid_from.isoformat(), f.source, f.value) for f in group], default=str).encode()).hexdigest()[:16]
            key = f"consolidate:{name}:{digest}"
            if any(f.idempotency_key == key for f in store.facts(tenant, subject)):
                unchanged += 1
                continue
            value = _value(reduce, group, subject, src["predicate"], summarizer)
            evidences = list(dict.fromkeys(f.evidence for f in group if f.evidence))
            first, last = group[0].valid_from, group[-1].valid_from
            evidence = f"{len(group)} {src['predicate']} facts, {first.isoformat()} to {last.isoformat()}"
            if evidences:
                evidence += "; evidence: " + ", ".join(evidences[:20]) + (" ..." if len(evidences) > 20 else "")
            store.open_fact(
                tenant, subject, dst["predicate"], value, valid_from=now, source=f"consolidate:{name}",
                scope=Scope(dst.get("scope", "tenant")), evidence=evidence, idempotency_key=key, corrected_by=f"job:consolidate:{name}",
            )
            written += 1
        report.written[name], report.unchanged[name] = written, unchanged
    return report
