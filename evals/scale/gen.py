"""Synthetic Jira logs for the scalability evaluation: seeded, streaming, any size.

The mix mimics the Public Jira Dataset tenants of evals/jira (Hyperledger INDY above all): issues are
created, change status, assignee, resolution, priority and type, and are linked and unlinked; late
arrivals and corrections are injected at evals/jira/inject.py's rates (5% late, 2% corrected) with its
delay distribution (log-uniform, an hour to a month). The log is written in the order it was recorded
(recorded_at, then generation order), as inject.py writes it.

    uv run python -m evals.scale.gen 1000000 out.jsonl [--seed 7] [--tenants 1] [--per-subject 7]

Memory stays bounded at any size: events are produced in world-time order and held in a heap only until
nothing later can be recorded before them (late ones wait up to a month of world time).
"""

from __future__ import annotations

import argparse
import heapq
import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evals.jira.inject import PREDICATE as CHANGES  # event type -> (predicate, payload key)
from evals.jira.inject import delay

START = datetime(2015, 1, 1, tzinfo=timezone.utc)
SPAN = timedelta(days=5 * 365)  # every size covers about five years of world time
POOL = 500  # issues in flight at once (per log): new ones are created as old ones use up their events

# Hyperledger INDY's statuses with their categories and frequencies in the real log.
STATUSES = [("Complete", "done", 3092), ("New", "new", 2384), ("In Progress", "indeterminate", 1432), ("To Test", "indeterminate", 1074),
            ("Code Review", "indeterminate", 908), ("Invalid", "done", 306), ("To Develop", "new", 294),
            ("Customer Validation", "indeterminate", 290), ("Won't Fix", "done", 146), ("Icebox", "new", 31)]
CATEGORY = {s: c for s, c, _ in STATUSES}
PRIORITIES = [("Medium", 1882), ("High", 358), ("Low", 143), ("Highest", 142), ("Lowest", 13), ("Critical", 40), ("Blocker", 20)]
TYPES = [("Task", 1027), ("Bug", 875), ("Story", 360), ("Epic", 99), ("Sub-task", 81), ("New Feature", 5)]
RESOLUTIONS = [(None, 2333), ("Done", 1415), ("Invalid", 306), ("Won't Do", 213), ("Deferred", 36), ("Duplicate", 19)]
USERS = [f"user-{i:04x}" for i in range(80)]
ASSIGNEES = [(None, 400)] + [(u, 1000 // (i + 1)) for i, u in enumerate(USERS)]  # a few people take most issues
RELATIONS = [("relates_to", 75), ("blocks", 12), ("duplicates", 6), ("clones", 4), ("depends_on", 3)]
# Events after the creation, by share of the real log (status changes are ~42% of all events, ...).
NEXT = [("issue.status_changed", 420), ("issue.assigned", 230), ("issue.resolution_changed", 111), ("issue.linked", 86),
        ("issue.priority_changed", 12), ("issue.type_changed", 7), ("issue.unlinked", 6)]
VOCAB = {"status": [s for s, _, _ in STATUSES], "assignee": [u for u, _ in ASSIGNEES], "priority": [p for p, _ in PRIORITIES],
         "resolution": [r for r, _ in RESOLUTIONS], "issue_type": [t for t, _ in TYPES]}


def _picker(pairs: list[tuple[Any, int]]) -> tuple[list[Any], list[int]]:
    values, weights = zip(*pairs)
    cum, total = [], 0
    for w in weights:
        total += w
        cum.append(total)
    return list(values), cum


def _ts(d: datetime) -> str:
    return d.isoformat()


@dataclass
class _Issue:
    tenant: str
    key: str
    left: int  # events still to come
    state: dict[str, Any] = field(default_factory=dict)  # predicate -> current value
    links: list[tuple[str, str]] = field(default_factory=list)  # open (relation, other)


def generate(n: int, seed: int = 7, tenants: int = 1, per_subject: float = 7.0, late: float = 0.05, corrections: float = 0.02,
             start: datetime = START, span: timedelta = SPAN) -> Iterator[dict[str, Any]]:
    """Exactly `n` events, in the order they were recorded. `per_subject` is the mean number of events of an
    issue (its creation, its changes, its links, the wrong values injected); `tenants` splits the issues
    round-robin into tenants syn0, syn1, ..."""
    rng = random.Random(seed)
    choose = {name: _picker(p) for name, p in (("status", [(s, w) for s, _, w in STATUSES]), ("priority", PRIORITIES), ("issue_type", TYPES),
                                               ("resolution", RESOLUTIONS), ("assignee", ASSIGNEES), ("relation", RELATIONS), ("next", NEXT))}

    def pick(name: str) -> Any:
        values, cum = choose[name]
        return values[_bisect(cum, rng.random() * cum[-1])]

    pool_size = max(1, min(POOL, round(n / per_subject)))
    gap = max(1.0, span.total_seconds() / n)  # mean seconds between events; at least one, so world time never ties
    pool: list[_Issue] = []
    issues: dict[str, list[str]] = {f"syn{t}": [] for t in range(tenants)}  # every issue of a tenant, to link to
    heap: list[tuple[datetime, int, dict[str, Any]]] = []
    clock, made, seq, created = start, 0, 0, 0

    def push(e: dict[str, Any], recorded: datetime) -> None:
        nonlocal seq
        e["recorded_at"] = _ts(recorded)
        heapq.heappush(heap, (recorded, seq, e))
        seq += 1

    def event(issue: _Issue, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": f"syn:{issue.tenant}:{made:09d}", "tenant": issue.tenant, "type": type_, "subject": f"issue:{issue.key}",
                "occurred_at": _ts(clock), "source": "jira", "payload": payload}

    while made < n:
        clock += timedelta(seconds=max(1, round(rng.expovariate(1 / gap))))
        while heap and heap[0][0] <= clock:  # nothing still to come can be recorded before these
            yield heapq.heappop(heap)[2]
        if len(pool) < pool_size:
            tenant = f"syn{created % tenants}"
            key = f"SYN{created % tenants}-{created // tenants + 1}"
            created += 1
            budget = max(1, round(rng.uniform(0.5, 1.5) * per_subject))  # spread around the mean, never a single shape
            issue = _Issue(tenant, key, budget - 1)
            known = issues[tenant]
            parent = known[rng.randrange(len(known))] if known and rng.random() < 0.03 else None
            status = pick("status") if rng.random() < 0.1 else "New"
            issue.state = {"status": status, "assignee": pick("assignee"), "priority": pick("priority"), "resolution": None,
                           "issue_type": "Sub-task" if parent else pick("issue_type")}
            payload = {"project": key.split("-")[0], "summary": f"Synthetic issue {key}", "status_category": CATEGORY[status],
                       "reporter": USERS[rng.randrange(len(USERS))], "parent": parent, **issue.state}
            if parent:
                issue.links.append(("subtask_of", parent))
            push(event(issue, "issue.created", payload), clock)
            made += 1
            known.append(key)
            if issue.left > 0:
                pool.append(issue)
            continue
        i = rng.randrange(len(pool))
        issue = pool[i]
        type_ = pick("next")
        if type_ == "issue.unlinked" and not [x for x in issue.links if x[0] != "subtask_of"]:
            type_ = "issue.status_changed"
        if type_ == "issue.linked":
            known = issues[issue.tenant]
            rel, other = pick("relation"), known[rng.randrange(len(known))]
            if other == issue.key or (rel, other) in issue.links:
                type_ = "issue.status_changed"
            else:
                issue.links.append((rel, other))
                push(event(issue, type_, {"relation": rel, "other": other}), clock)
                made += 1
        elif type_ == "issue.unlinked":
            open_ = [x for x in issue.links if x[0] != "subtask_of"]
            rel, other = open_[rng.randrange(len(open_))]
            issue.links.remove((rel, other))
            push(event(issue, type_, {"relation": rel, "other": other}), clock)
            made += 1
        if type_ in CHANGES:
            predicate, key = CHANGES[type_]
            before = issue.state[predicate]
            value = pick(predicate)
            if value == before:  # a change changes something
                value = next((v for v in VOCAB[predicate][rng.randrange(len(VOCAB[predicate])):] + VOCAB[predicate] if v != before), value)
            issue.state[predicate] = value
            payload = {key: value, "by": USERS[rng.randrange(len(USERS))]}
            if predicate == "status":
                payload["status_category"] = CATEGORY[value]
            e = event(issue, type_, payload)
            r = rng.random()
            if r < late:
                push(e, clock + delay(rng))
                made += 1
            elif r < late + corrections and made + 2 <= n:
                wrongs = [v for v in VOCAB[predicate] if v not in (value, before)]
                wrong = {**payload, key: wrongs[rng.randrange(len(wrongs))]}
                if predicate == "status":
                    wrong["status_category"] = CATEGORY[wrong[key]]
                push({**e, "id": e["id"] + ":wrong", "payload": wrong}, clock)
                push(e, clock + delay(rng))
                made += 2
            else:
                push(e, clock)
                made += 1
        issue.left -= 1
        if issue.left <= 0:
            pool[i] = pool[-1]
            pool.pop()
    while heap:
        yield heapq.heappop(heap)[2]


def _bisect(cum: list[int], x: float) -> int:
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] > x:
            hi = mid
        else:
            lo = mid + 1
    return lo


def write(path: Path, events: Iterable[dict[str, Any]]) -> int:
    n = 0
    with Path(path).open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("n", type=int)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tenants", type=int, default=1)
    ap.add_argument("--per-subject", type=float, default=7.0)
    a = ap.parse_args()
    print(write(a.out, generate(a.n, a.seed, a.tenants, a.per_subject)), "events ->", a.out)


if __name__ == "__main__":
    main()
