"""The adapter's check: replaying its events must end where Jira is today.

    uv run --with pymongo python -m evals.jira.check <issues.json|collection.bson> events.jsonl [--projects ...]

For every issue in the events, the open status, assignee, priority, resolution and type after replay must equal
the issue's current fields. A mismatch means the changelog walk is wrong (or Jira's changelog has a gap).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from evals.jira.adapt import load, name
from keel import Ingestor, MemoryStore, load_cartridge
from keel.envelope import Event

HERE = Path(__file__).resolve().parent
FIELDS = {"status": "status", "assignee": "assignee", "priority": "priority", "resolution": "resolution", "issue_type": "issuetype"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("events", type=Path)
    a = ap.parse_args()
    events = [Event(**json.loads(line)) for line in a.events.open()]
    store = MemoryStore()
    Ingestor(load_cartridge(HERE), store).ingest(events, strict=True)
    tenant = events[0].tenant
    keys = {e.subject_id for e in events if e.type == "issue.created"}
    bad, checked = Counter(), 0
    for issue in load(a.source):
        if issue["key"] not in keys:
            continue
        checked += 1
        facts = {f.predicate: f.value for f in store.facts(tenant, f"issue:{issue['key']}")}
        for predicate, field in FIELDS.items():
            if field not in issue["fields"]:  # not fetched: nothing to compare with
                continue
            want = name(issue["fields"].get(field))
            if predicate == "status":
                want = issue["fields"]["status"]["name"]
            if facts.get(predicate) != want:
                bad[predicate] += 1
                if bad[predicate] <= 3:
                    print(f"  {issue['key']} {predicate}: graph {facts.get(predicate)!r}, Jira {want!r}")
    print(f"{checked} issues checked; mismatches: {dict(bad) or 'none'}")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
