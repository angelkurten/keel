"""Rule evaluation.

A rule is a mapping the cartridge author writes. The core interprets two of its
keys: `applies_to` (the entity kind the rule is about) and `when`, an optional
condition the core can check against the subject's open facts. `when` names a
predicate and an operator; what the predicate means stays in the cartridge.

    tire_critical:
      applies_to: tire
      definition: "A tire is critical when tread depth is below 3 mm."
      threshold_mm: 3
      when: { predicate: depth_mm, lt: 3 }

    due_critical:
      applies_to: due
      when:
        - { predicate: status, eq: pending }
        - { predicate: due_on, within_days: 5 }

A rule without `when` reaches the agent as a definition only; the agent applies
it. A rule with `when` is also evaluated by `context`: true, false, or null when
a predicate has no open fact (or the values do not compare).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from keel.envelope import parse_iso
from keel.store.base import Fact


def _as_datetime(v: Any) -> datetime:
    if isinstance(v, date) and not isinstance(v, datetime):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    return parse_iso(v)


_OPS = {
    "lt": lambda v, a, now: v < a,
    "lte": lambda v, a, now: v <= a,
    "gt": lambda v, a, now: v > a,
    "gte": lambda v, a, now: v >= a,
    "eq": lambda v, a, now: v == a,
    "ne": lambda v, a, now: v != a,
    "in": lambda v, a, now: v in a,
    "within_days": lambda v, a, now: _as_datetime(v) <= now + timedelta(days=a),  # includes dates already past
}
OPERATORS = frozenset(_OPS)


def conditions(rule: dict[str, Any]) -> list[Any]:
    when = rule.get("when")
    if not when:
        return []
    return list(when) if isinstance(when, list) else [when]


def problems(name: str, rule: dict[str, Any]) -> list[str]:
    """Shape errors in a rule's `when`, reported by `keel validate`."""
    out = []
    for cond in conditions(rule):
        if not isinstance(cond, dict) or "predicate" not in cond:
            out.append(f"rule '{name}': every `when` condition needs a `predicate`")
            continue
        for op in sorted(set(cond) - {"predicate"} - OPERATORS):
            out.append(f"rule '{name}': unknown operator '{op}' (one of {', '.join(sorted(OPERATORS))})")
    return out


def evaluate(rule: dict[str, Any], facts: Iterable[Fact], as_of: datetime) -> bool | None:
    """Check a rule's `when` against open facts. None = a predicate has no open fact, or values do not compare."""
    current = {f.predicate: f.value for f in facts}
    try:
        for cond in conditions(rule):
            if cond["predicate"] not in current:
                return None
            value = current[cond["predicate"]]
            if not all(_OPS[op](value, arg, as_of) for op, arg in cond.items() if op != "predicate"):
                return False
    except (TypeError, ValueError):
        return None
    return True
