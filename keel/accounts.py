"""Accounts: what each hosted account may use (docs/design/hosted.md). Keys say who an account is
(keel/keys.py); this says how much it may do and where its data lives.

    keel accounts set acme --db keel.db --rps 20 --events-month 1000000 --retention 3y
    keel accounts list --db keel.db

Quotas are enforced by the HTTP transport: requests per second per account (a token bucket in each
server process) and events applied per calendar month (from the metering counters, keel/usage.py).
Both answer 429 with Retry-After. An account with no row, or a limit left empty, is not limited.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from keel.envelope import utcnow
from keel.keys import check_account
from keel.store.base import Account

CACHE_SECONDS = 5.0
RETENTION = re.compile(r"^(\d+)([ymd])$")


def parse_retention(value: str) -> str:
    """`3y`, `18m` or `90d`; returns it normalized, or raises."""
    if not RETENTION.match(value.strip().lower()):
        raise ValueError(f"retention '{value}': a number and y, m or d, like 3y, 18m or 90d")
    return value.strip().lower()


def cutoff_for(retention: str, now: datetime | None = None) -> datetime:
    """The instant before which closed history goes, for a retention like `3y`."""
    now = now or utcnow()
    n, unit = int(RETENTION.match(retention).group(1)), RETENTION.match(retention).group(2)
    if unit == "d":
        return now - timedelta(days=n)
    months = n * 12 if unit == "y" else n
    y, m = divmod(now.year * 12 + now.month - 1 - months, 12)
    day = min(now.day, 28)  # ponytail: clamps to the 28th so every month has the day; a few days of slack at most
    return now.replace(year=y, month=m + 1, day=day)


def set_account(graph: Any, account: str, **limits: Any) -> Account:
    """Create or update an account in `graph` (the shared store). A limit passed as 0 or "" clears it; one not
    passed keeps its value. `store=` is the account's own database, a limit like the others."""
    check_account(account)
    current = graph.account(account) or Account(id=account)
    for k, v in limits.items():
        if v is None:
            continue
        if k == "retention" and v:
            v = parse_retention(v)
        setattr(current, k, v or None)
    graph.put_account(current)
    return current


def _month(now: datetime) -> tuple[str, str, float]:
    """This month's prefix, the next month's, and seconds until it starts."""
    y, m = now.year + now.month // 12, now.month % 12 + 1
    nxt = datetime(y, m, 1, tzinfo=timezone.utc)
    return now.strftime("%Y-%m"), nxt.strftime("%Y-%m"), (nxt - now).total_seconds()


class Quotas:
    """Per-process enforcement. ponytail: each server process keeps its own token buckets, so N processes
    allow up to N times `rps`; a shared counter (Redis, the database) if that ever matters."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._accounts: dict[str, tuple[float, Account | None]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}  # account -> (tokens, last refill)
        self._month: dict[str, tuple[float, str, int]] = {}  # account -> (read at, month, events counted)

    def account(self, account: str) -> Account | None:
        now = time.monotonic()
        hit = self._accounts.get(account)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        got = self.store.account(account) if hasattr(self.store, "account") else None
        self._accounts[account] = (now, got)
        return got

    def request(self, account: str) -> float | None:
        """None when the request may go ahead; else the seconds to wait."""
        acct = self.account(account)
        if acct is None or not acct.rps:
            return None
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(account, (float(acct.rps), now))
            tokens = min(float(acct.rps), tokens + (now - last) * acct.rps)
            if tokens < 1:
                self._buckets[account] = (tokens, now)
                return (1 - tokens) / acct.rps
            self._buckets[account] = (tokens - 1, now)
        return None

    def events(self, account: str, n: int) -> float | None:
        """None when `n` more events fit this month's limit (and they are counted); else seconds to next month."""
        acct = self.account(account)
        if acct is None or not acct.events_month:
            return None
        month, nxt, wait = _month(utcnow())
        with self._lock:
            at, seen_month, used = self._month.get(account, (-1e9, "", 0))
            if seen_month != month or time.monotonic() - at > CACHE_SECONDS * 2:
                prefix = f"{account}/"
                used = sum(c for t, _, item, c in self.store.usage(month, nxt) if item == "events" and t.startswith(prefix))
                at = time.monotonic()
            if used + n > acct.events_month:
                self._month[account] = (at, month, used)
                return wait
            self._month[account] = (at, month, used + n)  # counted now: the metering row lands after the commit
        return None
