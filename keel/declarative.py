"""Declarative handlers: an event whose catalog entry says which payload fields become facts (`facts:`) and which
relations it opens (`relate:`), so it needs no code in handlers.py. A tenant extension (keel/extensions.py) can only
use these: it never uploads Python.

    order.observed:
      subject: order
      payload: { total: number, status: string, customer_id: string | null }
      facts: [total, status]                         # open_fact(subject, field, payload[field])
      relate: { placed_by: "customer:{customer_id}" }  # link(subject, placed_by, customer:<customer_id>)

The one place in keel/ that reads payload values, and only the fields the catalog names (CLAUDE.md). A field that is
absent is skipped; a null fact is written (the source said it is empty); a relation whose key field is absent or null
is skipped. A relation declared `one` closes the subject's other live link of that relation first.
"""

from __future__ import annotations

import re
from typing import Any

from keel.envelope import Event

_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def is_declarative(spec: Any) -> bool:
    return isinstance(spec, dict) and ("facts" in spec or "relate" in spec)


def apply(event: Event, spec: dict[str, Any], relations: dict[str, Any], store: Any) -> None:
    p, t = event.payload, event.tenant
    store.upsert_entity(t, event.subject_kind, event.subject_id)
    for name in spec.get("facts") or []:
        if name in p:
            store.open_fact(t, event.subject, name, p[name], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    for rel, template in (spec.get("relate") or {}).items():
        fields = _FIELD.findall(template)
        if any(p.get(f) is None for f in fields):
            continue
        dst = _FIELD.sub(lambda m: str(p[m.group(1)]), template)
        kind, _, id_ = dst.partition(":")
        store.upsert_entity(t, kind, id_)
        if (relations.get(rel) or {}).get("one"):
            for old in store.relations(t, event.subject, rel, at=event.occurred_at):
                if old.src == event.subject and old.dst != dst:
                    store.unlink(t, event.subject, rel, old.dst, event.occurred_at)
        store.link(t, event.subject, rel, dst, event.occurred_at, source=event.source)


def problems(etype: str, spec: dict[str, Any], relations: dict[str, Any]) -> list[str]:
    out: list[str] = []
    payload = spec.get("payload")
    fields = set(payload) if isinstance(payload, dict) else set()
    facts, relate = spec.get("facts"), spec.get("relate")
    if facts is not None and not (isinstance(facts, list) and all(isinstance(f, str) for f in facts)):
        out.append(f"event '{etype}': facts is a list of payload fields")
        facts = []
    for f in facts or []:
        if f not in fields:
            out.append(f"event '{etype}': fact '{f}' is not a payload field")
    if relate is not None and not (isinstance(relate, dict) and all(isinstance(v, str) for v in relate.values())):
        return out + [f"event '{etype}': relate maps a relation to a key template, e.g. {{placed_by: \"customer:{{customer_id}}\"}}"]
    for rel, template in (relate or {}).items():
        rs = relations.get(rel)
        if not isinstance(rs, dict):
            out.append(f"event '{etype}': relate '{rel}' is not a declared relation")
            continue
        kind, sep, rest = template.partition(":")
        if not sep or not _FIELD.search(rest) or "{" in kind:
            out.append(f"event '{etype}': relate '{rel}': '{template}' is '<kind>:{{field}}'")
            continue
        if rs.get("from") != spec.get("subject") or rs.get("to") != kind:
            out.append(f"event '{etype}': relate '{rel}' goes {rs.get('from')} -> {rs.get('to')}, not '{spec.get('subject')}' -> '{kind}'")
        for f in _FIELD.findall(rest):
            if f not in fields:
                out.append(f"event '{etype}': relate '{rel}': '{f}' is not a payload field")
    return out
