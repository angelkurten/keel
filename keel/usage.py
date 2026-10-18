"""Metering (docs/design/hosted.md): what each tenant used, per day, for billing and quotas.

Two kinds of counter, both in the store's `metering` table as (tenant, day, item, n):

- `events`: events applied for the first time, counted by the Ingestor after each commit. Duplicates,
  skipped events and replays are not counted, so a retry or a rebuild is never billed twice.
- one item per tool (`context`, `history`, `search`, `annotate`, `related`): calls made through a
  transport that passes a `Meter` to `Tools` (`keel serve --transport http`, /v1 and /mcp). Counted, not billed.

    keel usage --db keel.db --month 2027-01 [--account acme]

A tenant `acme/c1` belongs to the account `acme` (keel/keys.py); a tenant with no prefix to none.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any

from keel.envelope import utcnow

FLUSH_SECONDS = 10.0


def today() -> str:
    return utcnow().date().isoformat()


class Meter:
    """Tool calls counted in memory and added to the store every few seconds, so a read is not a write.
    ponytail: a crash loses at most FLUSH_SECONDS of call counts; fine for counters that are not billed."""

    def __init__(self, store: Any, every: float = FLUSH_SECONDS) -> None:
        self.store, self.every = store, every
        self._counts: Counter[tuple[str, str, str]] = Counter()
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def __call__(self, tenant: str, tool: str) -> None:
        with self._lock:
            self._counts[(tenant, today(), tool)] += 1
            due = time.monotonic() - self._last >= self.every
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            rows, self._counts, self._last = [(*k, n) for k, n in self._counts.items()], Counter(), time.monotonic()
        if rows and hasattr(self.store, "meter"):
            self.store.meter(rows)


def report(store: Any, month: str | None = None, account: str | None = None) -> list[dict[str, Any]]:
    """One row per account, tenant and month: `events` and `calls` per tool. `month` is `YYYY-MM`."""
    until = None
    if month:
        y, m = (int(x) for x in month.split("-"))
        until = f"{y + m // 12:04d}-{m % 12 + 1:02d}"
    out: dict[tuple[str | None, str, str], dict[str, Any]] = {}
    for tenant, day, item, n in store.usage(month, until):
        acct = tenant.partition("/")[0] if "/" in tenant else None
        if account is not None and acct != account:
            continue
        row = out.setdefault((acct, tenant, day[:7]), {"account": acct, "tenant": tenant, "month": day[:7], "events": 0, "calls": {}})
        if item == "events":
            row["events"] += n
        else:
            row["calls"][item] = row["calls"].get(item, 0) + n
    return [out[k] for k in sorted(out, key=lambda k: (k[0] or "", k[1], k[2]))]
