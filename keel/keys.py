"""API keys and accounts for the HTTP API (docs/design/hosted.md).

An account is the SaaS that pays; its tenants are its end-customers. A key belongs to one account and
may be narrowed to one tenant or one agent; a key created `glob=True` is the only one that may write the
account's global memory. The server prefixes the tenant with the account (`acme/cliente-42`), so a key
never reaches another account's data and nothing below the HTTP layer knows accounts exist.

    full, key = create_key(store, "acme", tenant=None, agent="tires-bot")   # full is shown once
    resolve(store, full)  # -> the ApiKey, or None when unknown or revoked

Only the SHA-256 of a key is stored. Keys live in the store (`api_keys`), so there is one backup and
rotation needs no restart.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any

from keel.envelope import utcnow
from keel.scopes import GLOBAL_TENANT
from keel.store.base import ApiKey

ACCOUNT = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")  # short: it is in every tenant key, log line and usage row
CACHE_SECONDS = 5.0


def digest(full: str) -> str:
    return hashlib.sha256(full.encode()).hexdigest()


def check_account(account: str) -> str:
    if not ACCOUNT.match(account):
        raise ValueError(f"account '{account}': lowercase letters, digits and hyphens, at most 32 characters, starting with a letter or digit")
    return account


def check_tenant(tenant: str) -> str:
    """A tenant as an account names it: the server adds the prefix, so it carries none of its own."""
    if not tenant or "/" in tenant or tenant == GLOBAL_TENANT:
        raise ValueError(f"tenant '{tenant}': give it without '/' (the server adds the account) and not '{GLOBAL_TENANT}'")
    return tenant


def create_key(store: Any, account: str, tenant: str | None = None, agent: str | None = None, glob: bool = False) -> tuple[str, ApiKey]:
    """A new key for `account`. Returns the full key, which is never stored, and the stored record."""
    check_account(account)
    if tenant is not None:
        check_tenant(tenant)
    if glob and (tenant or agent):
        raise ValueError("a global key writes the account's global memory; it is not narrowed to a tenant or an agent")
    key_id = f"kk_{secrets.token_hex(6)}"
    full = f"{key_id}_{secrets.token_urlsafe(32)}"
    key = ApiKey(id=key_id, hash=digest(full), account=account, created_at=utcnow(), tenant=tenant, agent=agent, glob=glob)
    store.put_key(key)
    return full, key


def revoke_key(store: Any, key_id: str) -> bool:
    ok = store.revoke_key(key_id, utcnow())
    _cache.clear()  # this process stops honouring it now; others within CACHE_SECONDS
    return ok


_cache: dict[str, tuple[float, ApiKey | None]] = {}


def resolve(store: Any, full: str) -> ApiKey | None:
    """The live key for a bearer token, or None. Lookups are cached for a few seconds per process."""
    # ponytail: unbounded dict pruned on expiry; an LRU if a flood of bad tokens ever shows in memory
    d, now = digest(full), time.monotonic()
    hit = _cache.get(d)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    if len(_cache) > 10_000:
        _cache.clear()
    key = store.api_key(d) if hasattr(store, "api_key") else None
    key = key if key is not None and key.revoked_at is None else None
    _cache[d] = (now, key)
    return key


def has_keys(store: Any) -> bool:
    return bool(getattr(store, "api_keys", list)())
