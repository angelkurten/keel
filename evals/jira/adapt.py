"""Jira issues with their changelogs -> Keel events for the jira cartridge.

Reads issues in Jira's REST shape (`key`, `fields`, `changelog.histories`): a JSON list, or a `.bson`
collection from the Public Jira Dataset's MongoDB dump (needs pymongo: `uv run --with pymongo`).

    uv run --with pymongo python -m evals.jira.adapt <issues.json|collection.bson> <tenant> out.jsonl \
        [--projects KEY,KEY] [--max-issues N]

Deterministic: the same input gives the same file. occurred_at and recorded_at are both the changelog's
time, which is when Jira recorded the change; late arrivals and corrections are injected later, on purpose.
The initial value of each field is the `from` of its first change, or its current value if it never changed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE = "jira"
FIELDS = {"status": "status", "assignee": "assignee", "priority": "priority", "resolution": "resolution", "issuetype": "issue_type"}
RELATIONS = [("subtask", "subtask_of"), ("block", "blocks"), ("duplicat", "duplicates"), ("clone", "clones"), ("depend", "depends_on")]


def parse_dt(s: str) -> datetime:
    """Jira writes 2026-09-17T17:56:59.905-0300 (no colon in the offset)."""
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def load(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix == ".bson":
        import bson  # pymongo's

        with path.open("rb") as fh:
            yield from bson.decode_file_iter(fh)
    else:
        yield from json.loads(path.read_text())


def link_kind(phrase: str) -> tuple[str, bool] | None:
    """'This issue is blocked by X' -> (blocks, inward). Inward phrases are recorded from the other end."""
    p = phrase.lower().removeprefix("this issue ").removeprefix("this work item ").rsplit(" ", 1)[0].strip()
    if not p:
        return None
    inward = p.startswith("is ") or p.endswith(" by")
    rel = next((r for word, r in RELATIONS if word in p), "relates_to")
    return rel, inward and rel != "relates_to"


def name(value: Any) -> str | None:
    return (value.get("displayName") or value.get("name")) if isinstance(value, dict) else value


def ident(value: dict[str, Any]) -> str | None:
    """What the changelog's `from`/`to` hold: an id, or an account id or user name for people."""
    v = value.get("id") or value.get("accountId") or value.get("key") or value.get("name")
    return None if v is None else str(v)


def events_of(issue: dict[str, Any], tenant: str, names: dict[str, dict[str, str]], categories: dict[str, str | None],
              alias: dict[str, str]) -> list[dict[str, Any]]:
    key, f = issue["key"], issue["fields"]
    changes = sorted(
        ((parse_dt(h["created"]), f"{int(h['id']):012d}", n, it, name(h.get("author"))) for h in (issue.get("changelog") or {}).get("histories") or []
         for n, it in enumerate(h.get("items") or [])),
        key=lambda c: (c[0], c[1], c[2]),
    )

    def ev(eid: str, typ: str, when: datetime, payload: dict[str, Any], subject: str = key) -> dict[str, Any]:
        at = when.isoformat()
        return {"id": f"jira:{tenant}:{eid}", "tenant": tenant, "type": typ, "subject": f"issue:{subject}",
                "occurred_at": at, "recorded_at": at, "source": SOURCE, "payload": payload}

    def named(field: str, side: str, item: dict[str, Any]) -> str | None:
        """A value named as it is today: Jira renames statuses and types (Done -> Finalizada), the changelog keeps the old name."""
        return names[field].get(str(item.get(side)), item.get(side + "String"))

    def initial(field: str) -> str | None:
        first = next((c[3] for c in changes if c[3]["field"] == field), None)
        return named(field, "from", first) if first else name(f.get(field))

    status = initial("status")
    created = parse_dt(f["created"])
    parent = (f.get("parent") or {}).get("key")
    out = [ev(f"{key}:created", "issue.created", created, {
        "project": key.rsplit("-", 1)[0],
        "summary": f.get("summary") or "",
        "issue_type": initial("issuetype"),
        "status": status,
        "status_category": categories.get(status),
        "priority": initial("priority"),
        "assignee": initial("assignee"),
        "reporter": name(f.get("reporter")),
        "resolution": initial("resolution"),
        "parent": alias.get(parent, parent),
    })]
    seen_links = set()
    for when, hid, n, it, by in changes:
        eid, field = f"{key}:h{hid}:{n}", it["field"]
        if field == "status":
            status = named(field, "to", it)
            out.append(ev(eid, "issue.status_changed", when, {"status": status, "status_category": categories.get(status), "by": by}))
        elif field == "assignee":
            out.append(ev(eid, "issue.assigned", when, {"assignee": named(field, "to", it), "by": by}))
        elif field == "priority":
            out.append(ev(eid, "issue.priority_changed", when, {"priority": named(field, "to", it), "by": by}))
        elif field == "resolution":
            out.append(ev(eid, "issue.resolution_changed", when, {"resolution": named(field, "to", it), "by": by}))
        elif field == "issuetype" and it.get("toString"):
            out.append(ev(eid, "issue.type_changed", when, {"issue_type": named(field, "to", it), "by": by}))
        elif field == "Link":
            added = bool(it.get("to"))
            kind = link_kind((it.get("toString") if added else it.get("fromString")) or "")
            other = it.get("to") or it.get("from")
            if kind and other and not kind[1]:  # outward: the other end records the inward phrase
                other = alias.get(other, other)
                seen_links.add((kind[0], other))
                out.append(ev(eid, "issue.linked" if added else "issue.unlinked", when, {"relation": kind[0], "other": other}))
    for link in f.get("issuelinks") or []:  # links the changelog never recorded (imported, or older than it)
        if "outwardIssue" in link:
            kind = link_kind("x " + link["type"]["outward"] + " x")
            other = alias.get(link["outwardIssue"]["key"], link["outwardIssue"]["key"])
            if kind and (kind[0], other) not in seen_links:
                out.append(ev(f"{key}:link:{kind[0]}:{other}", "issue.linked", created, {"relation": kind[0], "other": other}))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("tenant")
    ap.add_argument("out", type=Path)
    ap.add_argument("--projects", help="comma-separated project keys to keep")
    ap.add_argument("--max-issues", type=int, help="keep the N oldest issues (by creation), after --projects")
    a = ap.parse_args()
    keep = set(a.projects.split(",")) if a.projects else None
    issues = [i for i in load(a.source) if keep is None or i["key"].rsplit("-", 1)[0] in keep]
    issues.sort(key=lambda i: (parse_dt(i["fields"]["created"]), i["key"]))
    issues = issues[: a.max_issues] if a.max_issues else issues
    # each field's values by id, as named today; the status category (new, indeterminate, done) is only on current statuses
    names: dict[str, dict[str, str]] = {field: {} for field in FIELDS}
    for i in issues:
        for field in FIELDS:
            v = i["fields"].get(field)
            if isinstance(v, dict) and ident(v):
                names[field][ident(v)] = name(v)
    categories = {i["fields"]["status"]["name"]: (i["fields"]["status"].get("statusCategory") or {}).get("key") for i in issues}
    # an issue that moved project changed key; old links point at the old key
    alias = {it["fromString"]: i["key"] for i in issues for h in (i.get("changelog") or {}).get("histories") or []
             for it in h.get("items") or [] if it["field"] == "Key" and it.get("fromString")}
    events = sorted((e for i in issues for e in events_of(i, a.tenant, names, categories, alias)), key=lambda e: (e["occurred_at"], e["id"]))
    with a.out.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(issues)} issues -> {len(events)} events in {a.out}")


if __name__ == "__main__":
    main()
