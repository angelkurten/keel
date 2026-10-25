"""Latency of the tools on the Jira tenants, per store, no model: ingest, search(rule=) at an instant, context as known then.

    uv run python -m evals.jira.latency [~/datasets/public-jira/export]

MemoryStore and SqliteStore evaluate rules in Python; DuckdbStore pushes search(rule=) down to the rule macro
compiled from the cartridge (keel/sqlgen.py). The instants are the benchmark's own: every rule question's
as_of for search, every known_at question's (subject, as_of, known_at) for context. Each store's listings
are checked against MemoryStore's as it goes.
"""

# ruff: noqa: B023  (every lambda here is called at once, inside its loop)
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keel import Event, Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.store.duckdb import DuckdbStore

HERE = Path(__file__).parent
TENANTS = ("hyperledger", "mariadb", "spring")


def instant(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)


def timed(fn: Any) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


def main(data: Path) -> None:
    c = load_cartridge(HERE)
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="keel-latency-"))
    for tenant in TENANTS:
        events = [Event(**json.loads(line)) for line in (data / f"{tenant}.bitemporal.jsonl").open()]
        questions = json.loads((data / f"{tenant}.questions.json").read_text())
        searches = sorted({(q["params"]["rule"], q["params"]["as_of"]) for q in questions if q["params"].get("rule")})
        contexts = [(q["params"]["subject"], q["params"]["as_of"], q["params"]["known_at"]) for q in questions if q["type"] == "known_at"]
        reference: dict[tuple[str, str], list[str]] = {}
        for name, make in (("memory", MemoryStore), ("sqlite", lambda: SqliteStore(tmp / f"{tenant}.db")), ("duckdb", lambda: DuckdbStore(tmp / f"{tenant}.duckdb"))):
            store = make()
            ingest_ms, report = timed(lambda: Ingestor(c, store).ingest(events, strict=True))
            assert not report.errors
            tools = Tools(c, store, tenant)
            tools.search(rule=searches[0][0], as_of=instant(searches[0][1]), detail="key", limit=0)  # warm: macros installed, caches primed
            search_ms = []
            for rule, at in searches:
                ms, keys = timed(lambda: tools.search(rule=rule, as_of=instant(at), detail="key", limit=0))
                search_ms.append(ms)
                if name == "memory":
                    reference[(rule, at)] = keys
                assert keys == reference[(rule, at)], (name, rule, at)
            context_ms = [timed(lambda: tools.context(s, as_of=instant(a), as_known_at=instant(k)))[0] for s, a, k in contexts]
            rows.append((tenant, name, len(events), ingest_ms / 1000, statistics.median(search_ms), max(search_ms), len(search_ms),
                         statistics.median(context_ms), max(context_ms), len(context_ms)))
            print(f"{tenant:12} {name:7} ingest {ingest_ms / 1000:7.1f} s  search(rule) median {statistics.median(search_ms):8.1f} ms "
                  f"(max {max(search_ms):8.1f}, n={len(search_ms)})  context(as_known_at) median {statistics.median(context_ms):6.1f} ms "
                  f"(max {max(context_ms):6.1f}, n={len(context_ms)})", flush=True)
    print(json.dumps(rows))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "~/datasets/public-jira/export").expanduser())
