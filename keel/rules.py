"""Rule evaluation.

A rule is a mapping the cartridge author writes. The core interprets two of its keys:
`applies_to` (the entity kind the rule is about) and `when`, an optional condition the core
evaluates against the graph. `when` names predicates and operators; what a predicate means
stays in the cartridge.

    when: { predicate: depth_mm, lt: 3 }                                  # a leaf
    when: [ { predicate: status, eq: pending }, { predicate: due_on, within_days: 5 } ]   # all of them
    when: { any: [ { predicate: status, eq: paused },
                   { via: relates_to, any: { predicate: status, eq: paused } } ] }     # neighbours too
    when: { predicate: status, in: [On Hold, Blocked], ci: true }        # strings compared case-insensitively
    when: { predicate: status, eq: open, open_for_days: { gte: 7 } }     # how long the open fact has been open
    when: { predicate: status, eq: waiting, open_for_business_days: { gte: 3 } }  # weekdays minus `calendar.holidays`

Composition: `all` / `any` (lists), `not` (one condition). Neighbours: `via: <relation>` with
`direction: out | in | any` (default any) and `any:` or `all:` holding the condition each
neighbour's open facts must meet; no neighbours makes `any` false and `all` true.

Results are true, false, or null: a leaf whose predicate has no open fact is null, and null
propagates through `all`/`any`/`not` unless another branch already decides (a false in `all`,
a true in `any`). A rule without `when` reaches the agent as a definition only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from keel.envelope import parse_iso
from keel.store.base import Fact

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


@dataclass(frozen=True)
class Calendar:
    """Business days for `open_for_business_days`: declared in the cartridge as
    `calendar: { weekend: [saturday, sunday], holidays: [2026-12-25, ...] }`."""

    weekend: frozenset[int] = frozenset({5, 6})
    holidays: frozenset[date] = frozenset()

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> Calendar:
        raw = raw or {}
        weekend = frozenset(WEEKDAYS[str(d).lower()] for d in raw.get("weekend", ["saturday", "sunday"]))
        holidays = frozenset(parse_iso(h).date() for h in raw.get("holidays", []))
        return cls(weekend, holidays)

    def business_days(self, start: datetime, end: datetime) -> int:
        """Whole business days from `start` to `end` (the start day counts once it is over)."""
        n, d, last = 0, start.date(), end.date()
        while d < last:
            if d.weekday() not in self.weekend and d not in self.holidays:
                n += 1
            d += timedelta(days=1)
        return n


class Graph(Protocol):
    """What a rule may look at: a subject's open facts, its neighbours, and the calendar."""

    calendar: Calendar

    def facts(self, subject: str) -> list[Fact]: ...

    def neighbors(self, subject: str, rel: str, direction: str) -> list[str]: ...


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
DURATION_OPS = {"open_for_days", "open_for_business_days"}
OPERATORS = frozenset(_OPS) | DURATION_OPS
LEAF_KEYS = {"predicate", "ci"}
COMPARISONS = {"lt", "lte", "gt", "gte", "eq"}


def conditions(rule: dict[str, Any]) -> list[Any]:
    when = rule.get("when")
    if not when:
        return []
    return list(when) if isinstance(when, list) else [when]


def problems(name: str, rule: dict[str, Any], relations: set[str] | None = None) -> list[str]:
    """Shape errors in a rule's `when`, reported by `keel validate`."""
    out: list[str] = []
    when = rule.get("when")
    if when:
        _check(name, {"all": when} if isinstance(when, list) else when, relations, out)
    return out


def _check(name: str, cond: Any, relations: set[str] | None, out: list[str]) -> None:
    prefix = f"rule '{name}'"
    if not isinstance(cond, dict):
        out.append(f"{prefix}: a condition is a mapping, got {cond!r}")
        return
    if "all" in cond or "any" in cond or "not" in cond:
        if "via" in cond:
            pass
        elif len(cond) != 1:
            out.append(f"{prefix}: `all`, `any` and `not` stand alone in their condition")
        if "not" in cond and "via" not in cond:
            _check(name, cond["not"], relations, out)
            return
        for key in ("all", "any"):
            if key in cond and "via" not in cond:
                if not isinstance(cond[key], list):
                    out.append(f"{prefix}: `{key}` takes a list of conditions")
                else:
                    for c in cond[key]:
                        _check(name, c, relations, out)
                return
    if "via" in cond:
        if relations is not None and cond["via"] not in relations:
            out.append(f"{prefix}: `via` names undeclared relation '{cond['via']}'")
        if cond.get("direction", "any") not in ("out", "in", "any"):
            out.append(f"{prefix}: `direction` is out, in or any")
        inner = [k for k in ("any", "all") if k in cond]
        if len(inner) != 1 or set(cond) - {"via", "direction", "any", "all"}:
            out.append(f"{prefix}: `via` takes exactly one of `any` or `all` with the neighbour's condition")
            return
        _check(name, cond[inner[0]], relations, out)
        return
    if "predicate" not in cond:
        out.append(f"{prefix}: every leaf condition needs a `predicate`")
        return
    for op in sorted(set(cond) - LEAF_KEYS - OPERATORS):
        out.append(f"{prefix}: unknown operator '{op}' (one of {', '.join(sorted(OPERATORS))}, or all/any/not/via)")
    for op in DURATION_OPS & set(cond):
        spec = cond[op]
        if not isinstance(spec, dict) or not spec or set(spec) - COMPARISONS:
            out.append(f"{prefix}: `{op}` takes a mapping of {', '.join(sorted(COMPARISONS))}, for example {{ gte: 3 }}")


def evaluate(rule: dict[str, Any], subject: str, as_of: datetime, graph: Graph) -> bool | None:
    """The rule's `when` against the graph at `as_of`: true, false, or null (undecidable)."""
    when = rule.get("when")
    if not when:
        return None
    return _eval({"all": when} if isinstance(when, list) else when, subject, as_of, graph)


def _combine(results: list[bool | None], every: bool) -> bool | None:
    if every:
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if any(r is True for r in results):
        return True
    return None if any(r is None for r in results) else False


def _eval(cond: dict[str, Any], subject: str, as_of: datetime, graph: Graph) -> bool | None:
    if "via" in cond:
        every = "all" in cond
        inner = cond["all"] if every else cond["any"]
        results = [_eval(inner, n, as_of, graph) for n in graph.neighbors(subject, cond["via"], cond.get("direction", "any"))]
        if not results:
            return every
        return _combine(results, every)
    if "all" in cond:
        return _combine([_eval(c, subject, as_of, graph) for c in cond["all"]], every=True)
    if "any" in cond:
        return _combine([_eval(c, subject, as_of, graph) for c in cond["any"]], every=False)
    if "not" in cond:
        r = _eval(cond["not"], subject, as_of, graph)
        return None if r is None else not r
    current = {f.predicate: f for f in graph.facts(subject)}
    fact = current.get(cond["predicate"])
    if fact is None:
        return None
    try:
        return all(_holds(op, fact, arg, as_of, graph.calendar, bool(cond.get("ci"))) for op, arg in cond.items() if op not in LEAF_KEYS)
    except (TypeError, ValueError):
        return None


def _holds(op: str, fact: Fact, arg: Any, as_of: datetime, calendar: Calendar, ci: bool) -> bool:
    if op == "open_for_days":
        return _compare((as_of - fact.valid_from).total_seconds() / 86400, arg)
    if op == "open_for_business_days":
        return _compare(calendar.business_days(fact.valid_from, as_of), arg)
    value = fact.value
    if ci and isinstance(value, str):
        value = value.casefold()
        if op == "in":
            arg = [a.casefold() if isinstance(a, str) else a for a in arg]
        elif isinstance(arg, str):
            arg = arg.casefold()
    return bool(_OPS[op](value, arg, as_of))


def _compare(x: float, spec: dict[str, Any]) -> bool:
    return all(_OPS[k](x, v, None) for k, v in spec.items())
