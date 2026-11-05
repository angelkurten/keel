"""Tenant extensions: an additive cartridge fragment layered on the base cartridge for one tenant only.

    extends: crm@0.1.0          # the base cartridge (its name must match) and the version it was written against
    version: 3                  # the extension's own version, increasing
    entities: {...}  relations: {...}  events: {...}  rules: {...}  predicates: {...}
    retired: [deal]             # kinds hidden from reads; their events still apply, so a replay rebuilds the same graph

Rules: an extension only adds (no name of the base, no `forgets`), carries no code (every event is declarative,
keel/declarative.py) and is monotone: a new version keeps every name of the one before, and an event or relation once
published keeps its shape. A kind goes by retiring it. That is what lets a replay apply the live version to the whole
log and rebuild the same graph. Rules, predicates and descriptions only shape reads, so they may change.

It is stored per tenant (`put_extension`, bitemporal like stored agents) and read back through `for_tenant`, which the
Ingestor and Tools call for every tenant they work on: no other tenant ever sees its names.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from keel.declarative import is_declarative

log = logging.getLogger("keel.extensions")
SECTIONS = ("entities", "relations", "events", "rules", "predicates")
KEYS = frozenset({"extends", "version", "retired", *SECTIONS})
LIMITS = {"entities": 50, "relations": 100, "events": 100, "rules": 50}  # ponytail: fixed; a setting if a host needs more
_EVENT_SHAPE = ("subject", "payload", "facts", "relate")
_RELATION_SHAPE = ("from", "to", "one", "symmetric")


def _sections(ext: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s: ext.get(s) if isinstance(ext.get(s), dict) else {} for s in SECTIONS}


def merge(base: Any, ext: dict[str, Any] | None, hide: bool = True) -> Any:
    """The base cartridge with the extension layered on, a new Cartridge (the base is never changed). `hide`: retired
    kinds, the relations that touch them and the rules about them are left out of what tools read; events stay."""
    if not ext:
        return base
    # a name the base also has stays the base's: a base upgrade that collides never alters base behaviour (keel validate --db reports it)
    sec = {s: {**names, **getattr(base, s)} for s, names in _sections(ext).items()}
    retired = set(ext.get("retired") or []) - set(base.entities) if hide else set()
    entities = {k: v for k, v in {**base.entities, **sec["entities"]}.items() if k not in retired}
    relations = {r: v for r, v in {**base.relations, **sec["relations"]}.items()
                 if not (isinstance(v, dict) and {v.get("from"), v.get("to")} & retired)}
    rules = {n: v for n, v in {**base.rules, **sec["rules"]}.items() if not (isinstance(v, dict) and v.get("applies_to") in retired)}
    return dataclasses.replace(base, entities=entities, relations=relations, events={**base.events, **sec["events"]}, rules=rules,
                               predicates={**base.predicates, **sec["predicates"]})


def problems(base: Any, ext: Any, previous: dict[str, Any] | None = None) -> list[str]:
    """Everything wrong with `ext` as an extension of `base`, and as the version after `previous` (the live one)."""
    if not isinstance(ext, dict):
        return ["an extension is a mapping"]
    out = [f"unknown key '{k}' (an extension has {', '.join(sorted(KEYS))})" for k in sorted(set(ext) - KEYS)]
    extends = ext.get("extends")
    if not isinstance(extends, str) or extends.partition("@")[0] != base.name:
        out.append(f"extends is '{base.name}@<version>', the base cartridge it was written against")
    version = ext.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        out.append("version is a whole number of at least 1, increasing with every change")
    elif previous and isinstance(previous.get("version"), int) and version <= previous["version"]:
        out.append(f"version {version} is not after the live version {previous['version']}")
    for s in SECTIONS:
        if s in ext and not isinstance(ext[s], dict):
            out.append(f"{s} is a mapping of names")
    sec = _sections(ext)
    for s, names in sec.items():
        out += [f"{s[:-1] if s != 'entities' else 'entity'} '{n}' collides with the base cartridge" for n in sorted(set(names) & set(getattr(base, s)))]
        if s in LIMITS and len(names) > LIMITS[s]:
            out.append(f"{s}: at most {LIMITS[s]} in one extension")
    own_facts = {f for spec in sec["entities"].values() if isinstance(spec, dict) for f in (spec.get("facts") or [])}
    own_facts |= {f for spec in sec["events"].values() if isinstance(spec, dict) and isinstance(spec.get("facts"), list) for f in spec["facts"]}
    for name in sorted(set(sec["predicates"]) - own_facts):  # a base predicate's write policy is the base's
        out.append(f"predicate '{name}' is not a fact of the extension's kinds or events; the base's predicates keep their policy")
    for etype, spec in sorted(sec["events"].items()):
        if isinstance(spec, dict) and spec.get("subject") not in sec["entities"]:
            out.append(f"event '{etype}': its subject is one of the extension's kinds; it never writes on the base's ('{spec.get('subject')}')")
        for rel in (spec.get("relate") or {}) if isinstance(spec, dict) and isinstance(spec.get("relate"), dict) else ():
            if rel not in sec["relations"]:
                out.append(f"event '{etype}': relate '{rel}' is one of the extension's relations, never the base's")
        if not is_declarative(spec):
            out.append(f"event '{etype}' is not declarative: an extension carries no code, so its events say `facts:` and/or `relate:`")
        if isinstance(spec, dict) and "forgets" in spec:
            out.append(f"event '{etype}': forgets belongs to the base cartridge, not to an extension")
    retired = ext.get("retired")
    if retired is not None and not (isinstance(retired, list) and all(isinstance(r, str) for r in retired)):
        out.append("retired is a list of the extension's entity kinds")
    else:
        out += [f"retired '{r}' is not an entity kind of the extension" for r in retired or [] if r not in sec["entities"]]
    # the merged cartridge passes the cartridge checks (§5); what the base alone already reports is not the extension's
    known = set(base.validate())
    out += [p for p in merge(base, ext, hide=False).validate() if p not in known]
    if previous:
        out += _monotone(_sections(previous), sec)
    return out


def _monotone(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for s in SECTIONS:
        for name in sorted(set(before[s]) - set(after[s])):
            hint = "retire the kind instead (retired: [...])" if s == "entities" else "an extension only adds"
            out.append(f"{s}: '{name}' was in the live version and is missing; {hint}")
    for shape, s in ((_EVENT_SHAPE, "events"), (_RELATION_SHAPE, "relations")):
        for name in sorted(set(before[s]) & set(after[s])):
            old, new = before[s][name] or {}, after[s][name] or {}
            changed = [k for k in shape if old.get(k) != new.get(k)]
            if changed:
                out.append(f"{s}: '{name}' changed {', '.join(changed)}; a published one keeps its shape, so a replay rebuilds the same graph")
    return out


def collisions(base: Any, ext: dict[str, Any] | None) -> list[str]:
    """The extension's names the base also has: after a base upgrade, what makes the extension not load."""
    return [f"{s}.{n}" for s, names in _sections(ext or {}).items() for n in sorted(set(names) & set(getattr(base, s)))]


def for_tenant(base: Any, store: Any, tenant: str, hide: bool = True) -> Any:
    """The cartridge a tenant works with: the base and its live extension, cached a few seconds on the store like
    stored agents, so a new version applies without a restart. `hide=False` (the Ingestor): retired kinds stay, so
    their events apply as they always did. An extension a base upgrade collides with does not load at all (the base
    alone, and a warning; `keel validate --db` lists it): never half of it, never a base name redefined."""
    from keel.keys import CACHE_SECONDS

    if getattr(store, "extension", None) is None:
        return base
    # on the store itself, never keyed by id(): a freed store's id is reused, and its tenants are not this one's
    cache: dict[tuple[str, bool], tuple[float, Any, Any]] = store.__dict__.setdefault("_extensions_cache", {})
    now = time.monotonic()
    hit = cache.get((tenant, hide))
    if hit and hit[1] is base and now - hit[0] < CACHE_SECONDS:
        return hit[2]
    if len(cache) > 10_000:  # ponytail: cleared wholesale, an LRU if many tenants churn through one process
        cache.clear()
    ext = store.extension(tenant)
    clash = collisions(base, ext)
    if clash:
        log.warning("tenant '%s': its extension collides with the base cartridge (%s) and is not loaded; see keel validate --db",
                    tenant, ", ".join(clash))
    merged = base if clash else merge(base, ext, hide=hide)
    cache[(tenant, hide)] = (now, base, merged)
    return merged


def put_extension(base: Any, store: Any, tenant: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and store a tenant's extension, the next version of the live one. Raises ValueError with every problem;
    nothing invalid is stored."""
    from keel.envelope import utcnow

    if getattr(store, "put_extension", None) is None:
        raise ValueError(f"{type(store).__name__} does not keep tenant extensions (put_extension, extension)")
    previous = store.extension(tenant)
    found = problems(base, spec, previous)
    if found:
        raise ValueError("; ".join(found))
    # compare-and-set: the store replaces exactly the version validated against, or refuses (a concurrent PUT won)
    store.put_extension(tenant, spec, utcnow(), previous=previous)
    store.__dict__.pop("_extensions_cache", None)  # this process applies it now; others within CACHE_SECONDS
    return spec
