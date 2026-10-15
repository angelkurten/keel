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

from keel.agents import AgentSpec, parse_agents
from keel.agents import problems as agent_problems
from keel.envelope import Event
from keel.handlers import HandlerRegistry
from keel.rules import Calendar
from keel.rules import problems as rule_problems
from keel.schema import check, parse, payload_schema
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
    agents: dict[str, AgentSpec] = field(default_factory=dict)  # empty = no registry, any caller
    calendar: Calendar = field(default_factory=Calendar)  # business days for duration rules
    predicates: dict[str, dict[str, Any]] = field(default_factory=dict)  # write policy per predicate: {multi: true}
    semantic: dict[str, Any] = field(default_factory=dict)  # what keel index renders: kinds, predicates|exclude, attrs, relations
    version: str | None = None

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
        for etype, spec in self.events.items():
            try:
                parse(spec.get("payload") or {})
            except ValueError as exc:
                problems.append(f"event '{etype}': {exc}")
        for name, rule in self.rules.items():
            problems += rule_problems(name, rule, set(self.relations))
        problems += agent_problems(self.agents, self.entities, self.relations)
        sem = self.semantic
        for key in sorted(set(sem) - {"kinds", "predicates", "exclude", "attrs", "relations"}):
            problems.append(f"semantic: unknown key '{key}' (kinds, predicates, exclude, attrs, relations)")
        if "predicates" in sem and "exclude" in sem:
            problems.append("semantic: use `predicates` (what to include) or `exclude`, not both")
        for k in sorted(set(sem.get("kinds") or []) - set(self.entities)):
            problems.append(f"semantic: kind '{k}' is not a declared entity kind")
        for r in sorted(set(sem.get("relations") or []) - set(self.relations)):
            problems.append(f"semantic: relation '{r}' is not declared")
        for name, spec in self.predicates.items():
            if not isinstance(spec, dict) or set(spec) - {"multi", "description"}:
                problems.append(f"predicate '{name}': only `multi` (true/false) and `description` are understood")
        return problems

    def schema(self) -> dict[str, Any]:
        """The cartridge as one JSON document: envelope schema, events with payload schemas (§5.5),
        kinds, relations, rules, agents, memory policy. What `keel schema` prints and every SDK
        template reads."""
        from dataclasses import asdict
        from importlib.metadata import PackageNotFoundError, version

        try:
            keel_version = version("keel-context")
        except PackageNotFoundError:  # pragma: no cover
            keel_version = "0"
        return {
            "cartridge": self.name,
            "version": self.version,
            "keel": keel_version,
            "envelope": Event.model_json_schema(),
            "entities": self.entities,
            "relations": self.relations,
            "events": {
                t: {**{k: v for k, v in s.items() if k != "payload"}, "payload": payload_schema(s.get("payload"))}
                for t, s in self.events.items()
            },
            "rules": self.rules,
            "predicates": self.predicates,
            "semantic": self.semantic,
            "agents": {
                a.id: {
                    **asdict(a),
                    "writes": sorted(a.writes),
                    "kinds": sorted(a.kinds) if a.kinds is not None else None,
                    "relations": sorted(a.relations) if a.relations is not None else None,
                }
                for a in self.agents.values()
            },
            "memory": {
                "ttl": {s.value: (None if d is None else d.total_seconds()) for s, d in self.policy.ttl.items()},
                "requires_evidence": sorted(s.value for s in self.policy.requires_evidence),
            },
        }

    def check_event(self, event: Event) -> list[str]:
        """One event against the catalog: known type, right subject kind, declared payload fields
        present with the declared shape (keel.schema). Structural only: keys and types, never values."""
        spec = self.events.get(event.type)
        if spec is None:
            return [f"unknown event type '{event.type}'"]
        out = []
        if event.subject_kind != spec.get("subject"):
            out.append(f"'{event.type}' subject '{event.subject}' is not a '{spec.get('subject')}'")
        for path, message in check(event.payload, parse(spec.get("payload") or {}), ""):
            verb = "is missing" if message.startswith("is missing") else ""
            out.append(f"'{event.type}' payload is missing '{path}' {message[len('is missing '):]}" if verb else f"'{event.type}' payload '{path}' {message}")
        return out


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
        version=str(raw["version"]) if raw.get("version") is not None else None,
        entities=raw.get("entities") or {},
        relations=raw.get("relations") or {},
        events=raw.get("events") or {},
        rules=raw.get("rules") or {},
        policy=_policy_from(raw.get("memory")),
        registry=_load_registry(path),
        agents=parse_agents(raw.get("agents")),
        calendar=Calendar.parse(raw.get("calendar")),
        predicates=raw.get("predicates") or {},
        semantic=raw.get("semantic") or {},
    )
