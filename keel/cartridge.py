"""Cartridge loader.

A cartridge is a folder:

    cartridges/fleet/
      cartridge.yaml   ontology, event catalog, semantic rules, memory policies
      handlers.py      a `registry = HandlerRegistry()` with one handler per event type

The core loads the YAML, validates that every cataloged event has a
handler and every relation uses declared entity kinds, and hands the
registry to the ingestor. That is the whole contract between core and
domain. If writing your second cartridge makes you touch the core, some
domain leaked in; pull it back out.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from keel.envelope import Event, parse_iso
from keel.handlers import HandlerRegistry
from keel.rules import problems as rule_problems
from keel.scopes import MemoryPolicy, Scope


@dataclass
class Cartridge:
    name: str
    path: Path
    entities: dict[str, dict[str, Any]]  # kind -> {description, ref?}
    relations: dict[str, dict[str, str]]  # rel -> {from, to}
    events: dict[str, dict[str, Any]]  # type -> {subject, owner, payload: {...}}
    rules: dict[str, Any] = field(default_factory=dict)  # semantic definitions, versioned
    policy: MemoryPolicy = field(default_factory=MemoryPolicy.default)
    registry: HandlerRegistry = field(default_factory=HandlerRegistry)

    def validate(self) -> list[str]:
        problems: list[str] = []
        for rel, spec in self.relations.items():
            for side in ("from", "to"):
                kind = spec.get(side)
                if kind not in self.entities:
                    problems.append(f"relation '{rel}' {side}='{kind}' is not a declared entity kind")
        for etype, spec in self.events.items():
            if spec.get("subject") not in self.entities:
                problems.append(f"event '{etype}' subject kind '{spec.get('subject')}' is not declared")
            if etype not in self.registry.types():
                problems.append(f"event '{etype}' has no handler in handlers.py")
        for etype in self.registry.types() - set(self.events):
            problems.append(f"handler for '{etype}' exists but the event is not in the catalog")
        for name, rule in self.rules.items():
            problems += rule_problems(name, rule)
        return problems

    def check_event(self, event: Event) -> list[str]:
        """One event against the catalog: known type, right subject kind, declared payload fields
        present with the declared type. Structural only: keys and types, never values."""
        spec = self.events.get(event.type)
        if spec is None:
            return [f"unknown event type '{event.type}'"]
        out = []
        if event.subject_kind != spec.get("subject"):
            out.append(f"'{event.type}' subject '{event.subject}' is not a '{spec.get('subject')}'")
        for name, hint in (spec.get("payload") or {}).items():
            kinds = {k.strip() for k in str(hint).split("|")}
            if name not in event.payload:
                if "null" not in kinds:
                    out.append(f"'{event.type}' payload is missing '{name}' ({hint})")
                continue
            value = event.payload[name]
            if value is None and "null" in kinds:
                continue
            if not any(_is(value, k) for k in kinds - {"null"}):
                out.append(f"'{event.type}' payload '{name}' should be {hint}, got {type(value).__name__}")
        return out


def _is(value: Any, hint: str) -> bool:
    """Does a payload value match a catalog type hint? Hints the core does not know are documentation."""
    if hint == "string":
        return isinstance(value, str)
    if hint == "bool":
        return isinstance(value, bool)
    if hint == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if hint == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if hint in ("date", "datetime"):
        try:
            parse_iso(value)
        except ValueError:
            return False
        return True
    return True


def _parse_ttl(v: Any) -> timedelta | None:
    if v in (None, "never", "forever"):
        return None
    if isinstance(v, (int, float)):
        return timedelta(days=float(v))
    s = str(v).strip()
    unit = s[-1]
    n = float(s[:-1])
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _policy_from(raw: dict[str, Any] | None) -> MemoryPolicy:
    base = MemoryPolicy.default()
    if not raw:
        return base
    ttl = dict(base.ttl)
    for k, v in (raw.get("ttl") or {}).items():
        ttl[Scope(k)] = _parse_ttl(v)
    req = {Scope(s) for s in raw.get("requires_evidence", [s.value for s in base.requires_evidence])}
    return MemoryPolicy(ttl=ttl, requires_evidence=req)


def _load_registry(path: Path) -> HandlerRegistry:
    handlers_py = path / "handlers.py"
    if not handlers_py.exists():
        return HandlerRegistry()
    spec = importlib.util.spec_from_file_location(f"keel_cartridge_{path.name}_handlers", handlers_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reg = getattr(mod, "registry", None)
    if not isinstance(reg, HandlerRegistry):
        raise TypeError(f"{handlers_py} must define `registry = HandlerRegistry()`")
    return reg


def load_cartridge(path: str | Path) -> Cartridge:
    path = Path(path)
    raw = yaml.safe_load((path / "cartridge.yaml").read_text(encoding="utf-8")) or {}
    return Cartridge(
        name=raw.get("name", path.name),
        path=path,
        entities=raw.get("entities") or {},
        relations=raw.get("relations") or {},
        events=raw.get("events") or {},
        rules=raw.get("rules") or {},
        policy=_policy_from(raw.get("memory")),
        registry=_load_registry(path),
    )
