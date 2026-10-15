"""Payload notation: the short hints in cartridge.yaml, their JSON Schema, and the checker.

    payload:
      id: string                           # string | int | number | bool | date | datetime | any
      amount: number | null                # `| null`: optional and nullable
      status: [pending, paid]              # a list of literals: enum
      tags: string[]                       # array of scalars
      lines: [ { id: string, qty: int } ]  # a one-element list holding a mapping: array of objects
      meta: { source: string, score: number }   # a mapping: nested object

`keel schema <cartridge>` prints the JSON Schema (draft 2020-12) of every payload, the envelope
and the ontology: the one input every SDK template reads. `keel validate --events` checks a
stream with the same notation. Unknown hints are an error, so a typo cannot pass as "any".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keel.envelope import parse_iso

SCALARS: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "int": {"type": "integer"},
    "number": {"type": "number"},
    "bool": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "any": {},
}


@dataclass(frozen=True)
class Hint:
    kind: str  # a SCALARS key, "enum", "object" or "array"
    text: str  # the hint as written, for messages
    nullable: bool = False
    values: tuple[Any, ...] = ()  # enum
    fields: dict[str, Hint] = field(default_factory=dict)  # object
    items: Hint | None = None  # array


def parse(spec: Any) -> Hint:
    """One field's spec, as written in the YAML, to a Hint. Raises ValueError on an unknown hint."""
    if isinstance(spec, dict):
        return Hint("object", "object", fields={k: parse(v) for k, v in spec.items()})
    if isinstance(spec, list):
        if len(spec) == 1 and isinstance(spec[0], dict):
            return Hint("array", "array of objects", items=parse(spec[0]))
        if spec and all(isinstance(v, (str, int, float, bool)) for v in spec):
            if all(isinstance(v, str) and v in SCALARS for v in spec):
                raise ValueError(f"unsupported payload hint {spec!r}: an array of {spec[0]} is written '{spec[0]}[]'; a list is an enum of literal values")
            return Hint("enum", "one of " + ", ".join(map(str, spec)), values=tuple(spec))
        raise ValueError(f"unsupported payload hint {spec!r}: a list is an enum of literals or [ {{...}} ] for an array of objects")
    text = str(spec).strip()
    parts = [p.strip() for p in text.split("|")]
    nullable = "null" in parts
    base = [p for p in parts if p != "null"]
    if len(base) != 1:
        raise ValueError(f"unsupported payload hint {text!r}")
    name = base[0]
    if name.endswith("[]"):
        return Hint("array", text, nullable, items=_scalar(name[:-2], name[:-2]))
    return _scalar(name, text, nullable)


def _scalar(name: str, text: str, nullable: bool = False) -> Hint:
    if name not in SCALARS:
        raise ValueError(f"unknown payload hint '{name}' (string, int, number, bool, date, datetime, any, T[], [literals], {{...}})")
    return Hint(name, text, nullable)


def to_schema(h: Hint) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a Hint."""
    if h.kind == "enum":
        schema: dict[str, Any] = {"enum": list(h.values)}
    elif h.kind == "object":
        schema = {
            "type": "object",
            "properties": {k: to_schema(v) for k, v in h.fields.items()},
            "required": [k for k, v in h.fields.items() if not v.nullable],
        }
    elif h.kind == "array":
        schema = {"type": "array", "items": to_schema(h.items) if h.items else {}}
    else:
        schema = dict(SCALARS[h.kind])
    if h.nullable and "type" in schema:
        schema["type"] = [schema["type"], "null"]
    return schema


def payload_schema(spec: dict[str, Any] | None) -> dict[str, Any]:
    """The JSON Schema of one event's `payload:` mapping. Extra fields are allowed: the catalog documents what handlers need, not everything the source sends."""
    return to_schema(parse(spec or {}))


_PY = {"string": str, "int": int, "number": (int, float), "bool": bool, "date": str, "datetime": str}


def check(value: Any, h: Hint, path: str) -> list[tuple[str, str]]:
    """(path, message) for every mismatch between a value and its hint."""
    if value is None:
        return [] if h.nullable or h.kind == "any" else [(path, f"should be {h.text}, got null")]
    if h.kind == "any":
        return []
    if h.kind == "enum":
        return [] if value in h.values else [(path, f"should be {h.text}, got {value!r}")]
    if h.kind == "object":
        if not isinstance(value, dict):
            return [(path, f"should be an object, got {type(value).__name__}")]
        out = []
        for name, sub in h.fields.items():
            if name not in value:
                if not sub.nullable:
                    out.append((f"{path}.{name}" if path else name, f"is missing ({sub.text})"))
                continue
            out += check(value[name], sub, f"{path}.{name}" if path else name)
        return out
    if h.kind == "array":
        if not isinstance(value, list):
            return [(path, f"should be {h.text}, got {type(value).__name__}")]
        return [m for i, v in enumerate(value) for m in check(v, h.items, f"{path}[{i}]")] if h.items else []
    ok = isinstance(value, _PY[h.kind]) and not (h.kind in ("int", "number") and isinstance(value, bool))
    if ok and h.kind in ("date", "datetime"):
        try:
            parse_iso(value)
        except ValueError:
            ok = False
    return [] if ok else [(path, f"should be {h.text}, got {type(value).__name__}")]
