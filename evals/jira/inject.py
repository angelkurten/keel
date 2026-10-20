"""Late arrivals and corrections, injected into a tenant's Jira events with a fixed seed.

Jira's changelog says when a change happened, not when anyone learned it, so the adapter writes
recorded_at = occurred_at. This script makes transaction time matter, reproducibly:

- late: a change reaches the log `delay` after it happened (recorded_at = occurred_at + delay).
- correction: a wrong value is recorded at the change's instant, and the right one arrives `delay`
  later with the same occurred_at; the later record wins the instant (keel.store.memory.succession).

    uv run python -m evals.jira.inject events.jsonl out.jsonl manifest.json [--seed 7] [--late 0.05] [--corrections 0.02]

The manifest lists each injection with the instants and the values an oracle expects: at `as_of` =
occurred_at, known at any time before `recorded_at` the answer is `before`, and from it on, `after`.
Expected values come from walking the log, not from Keel; `--check` then asks Keel for each window.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PREDICATE = {  # event type -> (predicate, payload key)
    "issue.status_changed": ("status", "status"),
    "issue.assigned": ("assignee", "assignee"),
    "issue.priority_changed": ("priority", "priority"),
    "issue.resolution_changed": ("resolution", "resolution"),
    "issue.type_changed": ("issue_type", "issue_type"),
}
MIN_DELAY, MAX_DELAY = timedelta(hours=1), timedelta(days=30)
EPSILON = timedelta(microseconds=1)


def delay(rng: random.Random) -> timedelta:
    """Log-uniform between an hour and a month: most late data is a little late, some very."""
    lo, hi = math.log(MIN_DELAY.total_seconds()), math.log(MAX_DELAY.total_seconds())
    return timedelta(seconds=round(math.exp(rng.uniform(lo, hi))))


def timelines(events: list[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[datetime, str, Any]]]:
    """(subject, predicate) -> [(occurred_at, event id, value)], in history order: the oracle's own walk."""
    out: dict[tuple[str, str], list[tuple[datetime, str, Any]]] = defaultdict(list)
    for e in events:
        at = datetime.fromisoformat(e["occurred_at"])
        if e["type"] == "issue.created":
            for predicate, key in PREDICATE.values():
                out[(e["subject"], predicate)].append((at, e["id"], e["payload"][key]))
        elif e["type"] in PREDICATE:
            predicate, key = PREDICATE[e["type"]]
            out[(e["subject"], predicate)].append((at, e["id"], e["payload"][key]))
    for line in out.values():
        line.sort(key=lambda x: (x[0], x[1]))
    return out


def inject(events: list[dict[str, Any]], seed: int, late: float, corrections: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    lines = timelines(events)
    values: dict[str, set[Any]] = defaultdict(set)  # predicate -> values seen in the tenant, for wrong ones
    category = {}
    for (_, predicate), line in lines.items():
        values[predicate] |= {v for _, _, v in line if v is not None}
    for e in events:
        if "status_category" in e["payload"]:
            category[e["payload"]["status"]] = e["payload"]["status_category"]
    # candidates: a change with a distinct previous value and nothing else at its instant on that predicate
    candidates = []
    for (subject, predicate), line in sorted(lines.items()):
        for i in range(1, len(line)):
            at, eid, value = line[i]
            if line[i - 1][0] < at and (i + 1 == len(line) or line[i + 1][0] > at) and line[i - 1][2] != value:
                candidates.append((subject, predicate, eid, at, value, line[i - 1][2]))
    rng.shuffle(candidates)
    by_id = {e["id"]: e for e in events}
    taken: set[tuple[str, str]] = set()  # one injection per subject and predicate: windows never overlap
    manifest, extra = [], []
    want = {"late": round(late * len(candidates)), "correction": round(corrections * len(candidates))}
    for subject, predicate, eid, at, value, before in candidates:
        kind = next((k for k, n in want.items() if n > 0), None)
        if kind is None:
            break
        if (subject, predicate) in taken:
            continue
        taken.add((subject, predicate))
        want[kind] -= 1
        e = by_id[eid]
        recorded = at + delay(rng)
        e["recorded_at"] = recorded.isoformat()
        entry = {"kind": kind, "event": eid, "subject": subject, "predicate": predicate, "occurred_at": at.isoformat(),
                 "recorded_at": recorded.isoformat(), "after": value}
        if kind == "late":
            entry["before"] = before
        else:
            wrong = rng.choice(sorted(v for v in values[predicate] if v not in (value, before)))
            payload = {**e["payload"], PREDICATE[e["type"]][1]: wrong}
            if predicate == "status":
                payload["status_category"] = category.get(wrong)
            extra.append({**e, "id": f"{eid}:wrong", "payload": payload, "recorded_at": at.isoformat()})
            entry |= {"before": wrong, "wrong_event": f"{eid}:wrong", "wrong_recorded_at": at.isoformat()}
        manifest.append(entry)
    out = sorted(events + extra, key=lambda e: (e["recorded_at"], e["id"]))  # the log in the order it was written
    manifest.sort(key=lambda m: (m["recorded_at"], m["event"]))
    return out, manifest


def check(out: list[dict[str, Any]], manifest: list[dict[str, Any]]) -> int:
    """Ask Keel, for each injection, what was known just before and at its recorded_at. Returns failures."""
    from keel import Ingestor, MemoryStore, load_cartridge
    from keel.envelope import Event

    store = MemoryStore()
    Ingestor(load_cartridge(Path(__file__).parent), store).ingest([Event(**e) for e in out], strict=True)
    tenant = out[0]["tenant"]
    bad = 0
    def value(m: dict[str, Any], known: datetime | None) -> Any:
        at = datetime.fromisoformat(m["occurred_at"])
        fs = [f for f in store.facts(tenant, m["subject"], at=at, known_at=known) if f.predicate == m["predicate"]]
        return fs[0].value if len(fs) == 1 else ("<several>" if fs else "<none>")

    for m in manifest:
        rec = datetime.fromisoformat(m["recorded_at"])
        got = (value(m, rec - EPSILON), value(m, rec), value(m, None))
        if got != (m["before"], m["after"], m["after"]):
            bad += 1
            if bad <= 5:
                print(f"  {m['kind']} {m['subject']} {m['predicate']}: Keel {got}, expected ({m['before']!r}, {m['after']!r}, {m['after']!r})")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("events", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--late", type=float, default=0.05, help="share of eligible changes that arrive late")
    ap.add_argument("--corrections", type=float, default=0.02, help="share of eligible changes recorded wrong first")
    ap.add_argument("--check", action="store_true", help="ingest the result and ask Keel about every window")
    a = ap.parse_args()
    events = [json.loads(line) for line in a.events.open()]
    out, manifest = inject(events, a.seed, a.late, a.corrections)
    with a.out.open("w") as fh:
        for e in out:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    a.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))
    kinds = {k: sum(m["kind"] == k for m in manifest) for k in ("late", "correction")}
    print(f"{len(events)} events -> {len(out)} in {a.out}; injected {kinds['late']} late, {kinds['correction']} corrections")
    if a.check:
        bad = check(out, manifest)
        print(f"Keel on {len(manifest)} windows: {'all as expected' if not bad else f'{bad} differ'}")
        raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
