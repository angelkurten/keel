"""Write the upgrade fixture into a SQLite file with whatever Keel is installed.

    uv run --isolated --no-project --with keel-context==0.8.0 python -I tests/upgrade/make.py tests/upgrade/keel-0.8.0.db

`--isolated` matters inside the repo: without it uv may satisfy the pin with the editable checkout.

tests/test_upgrade.py imports this module to build the same graph with the current code, so it runs
unchanged on every release it generates (0.8.0, 0.20.0, 0.32.0; 0.1.0 predates place_fact and wrote late
facts wrongly, so it is not one): the same events, the same annotate calls, through APIs that exist since 0.1.0 (no agent registry, no task=, key= or status= on annotate).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import keel.tools
from keel import Event, Ingestor, SqliteStore, Tools, load_cartridge

HERE = Path(__file__).parent
TENANT = "acme"
AGENT = "ana"  # the author of every annotation, so a reader bound to her sees the agent-scope one too
T = datetime(2026, 1, 1, tzinfo=timezone.utc)

# (when, subject, predicate, value, scope, evidence): annotate writes valid_from = its clock, pinned here
ANNOTATIONS: list[tuple[datetime, str, str, Any, str, str | None]] = [
    (T + timedelta(days=7), "machine:M2", "todo", "check the belt", "task", None),
    (T + timedelta(days=9, hours=13), "machine:M1", "note", "bearing noise", "tenant", "e07"),
    (T + timedelta(days=9, hours=14), "machine:M1", "hunch", "likely the bearing", "agent", None),
    (T + timedelta(days=20), "machine:M1", "note", "bearing replaced", "tenant", "e12"),
]

# after migration: a status that starts before M1's explicit close (2026-01-20) and after its last value
LATE = {"id": "e13", "tenant": TENANT, "type": "machine.status_changed", "subject": "machine:M1", "occurred_at": "2026-01-15T12:00:00Z",
        "recorded_at": "2026-01-21T10:00:00Z", "source": "telemetry", "payload": {"status": "inspected"}}

# after migration, meeting no close: M2 paused between its install (idle, 01-04) and running (01-06)
LATE_OPEN = {"id": "e14", "tenant": TENANT, "type": "machine.status_changed", "subject": "machine:M2", "occurred_at": "2026-01-05T08:00:00Z",
             "source": "telemetry", "payload": {"status": "paused"}}


def load() -> Any:
    return load_cartridge(HERE / "cartridge")


def _lines() -> list[dict[str, Any]]:
    return [json.loads(line) for line in (HERE / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def declared_ids() -> set[str]:
    """Events whose source declared recorded_at: the transaction time of what they set is input, not clock."""
    return {e["id"] for e in [*_lines(), LATE, LATE_OPEN] if "recorded_at" in e}


def _ingest(store: Any, lines: list[dict[str, Any]]) -> None:
    report = Ingestor(load(), store).ingest([Event(**e) for e in lines])
    assert report.processed == len(lines) and not report.errors and not report.unhandled, report


def build(store: Any) -> None:
    _ingest(store, _lines())
    cartridge, real = load(), keel.tools.utcnow
    try:
        for at, subject, predicate, value, scope, evidence in ANNOTATIONS:
            keel.tools.utcnow = lambda at=at: at  # every release reads annotate's clock from here
            Tools(cartridge, store, TENANT).annotate(subject, predicate, value, scope=scope, evidence=evidence, by=AGENT)
    finally:
        keel.tools.utcnow = real


def late(store: Any, event: dict[str, Any] = LATE) -> None:
    _ingest(store, [event])


def main(path: str) -> None:
    out = Path(path)
    for p in (out, Path(f"{out}-wal"), Path(f"{out}-shm")):
        p.unlink(missing_ok=True)
    store = SqliteStore(out)
    build(store)
    store.db.close()
    db = sqlite3.connect(out)
    db.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal beside it
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
