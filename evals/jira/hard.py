"""A harder slice of questions over a Jira tenant, gold from the log alone (questions.Oracle), never by a model.

    uv run python -m evals.jira.hard <tenant>.bitemporal.jsonl <tenant>.injections.json out.json [--seed 53] [--check]

The main set's questions read one fact, one hop or one instant; every SQL-backed condition answers 96 to 99% of them.
These five types each combine two of those steps, the kind an agent inside a product asks and a single tool call
does not answer:

- two_hop:     issues two relations away from one, both relations at an instant ("the sub-tasks of the issues that
               block X").
- known_count: how many issues had a status at an instant, as recorded by a later instant, where a late or corrected
               record makes the count differ from today's knowledge of the same instant.
- entries:     how many times one issue was moved into a status within a window (re-entries, never the creation).
- time_in:     whole days one issue spent in a status up to an instant, every spell added.
- both:        the issues in progress at two instants, where neither instant alone gives the answer.

Instants keep questions.MARGIN from every change the question reads, as in the main set. `--check` answers every
question with Keel's primitives (hard.check, no agent) and compares.
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from evals.jira.questions import MARGIN, Oracle, fmt, minute, t

TYPES = ("two_hop", "known_count", "entries", "time_in", "both")
DAY = timedelta(days=1)
# (relation, direction from the issue at hand) -> the phrase for "the issues reached from {x}"
HOPS = {
    ("subtask_of", "in"): "the sub-tasks of {x}",
    ("subtask_of", "out"): "the parents of {x}",
    ("blocks", "in"): "the issues that block {x}",
    ("blocks", "out"): "the issues blocked by {x}",
    ("relates_to", "any"): "the issues linked to {x} by a 'relates to' link (either direction)",
    ("depends_on", "any"): "the issues linked to {x} by a 'depends on' link (either direction)",
    ("duplicates", "any"): "the issues linked to {x} by a 'duplicates' link (either direction)",
}
INVERSE = {("subtask_of", "in"): ("subtask_of", "out"), ("subtask_of", "out"): ("subtask_of", "in"),
           ("blocks", "in"): ("blocks", "out"), ("blocks", "out"): ("blocks", "in")}


def two_hop(o: Oracle, subject: str, hop1: tuple[str, str], hop2: tuple[str, str], at: datetime) -> list[str]:
    mid = o.neighbours(subject, *hop1, at)
    return sorted({n for m in mid for n in o.neighbours(m, *hop2, at)} - {subject})


def alive(o: Oracle, at: datetime) -> list[str]:
    return [s for s, c in o.created.items() if c <= at]


def known_count(o: Oracle, status: str, at: datetime, known: datetime | None) -> int:
    return sum(o.value(s, "status", at, known) == status for s in alive(o, at))


def timeline(o: Oracle, subject: str) -> list[tuple[datetime, Any]]:
    """The issue's status as now known: one value per start, the later record winning a tie (Oracle.fact's rule)."""
    best: dict[datetime, tuple[datetime, str, Any]] = {}
    for start, rec, eid, value in o.facts.get((subject, "status"), ()):
        if start not in best or (rec, eid) > best[start][:2]:
            best[start] = (rec, eid, value)
    return [(start, best[start][2]) for start in sorted(best)]


def entries(o: Oracle, subject: str, status: str, a: datetime, b: datetime) -> int:
    line = timeline(o, subject)
    return sum(1 for (_, before), (start, value) in pairwise(line) if value == status != before and a <= start < b)


def time_in(o: Oracle, subject: str, status: str, at: datetime) -> timedelta:
    line = timeline(o, subject)
    ends = [start for start, _ in line[1:]] + [at]
    return sum((min(end, at) - start for (start, value), end in zip(line, ends) if value == status and start < at), timedelta(0))


def both(o: Oracle, a: datetime, b: datetime) -> list[str]:
    ip = lambda s, at: o.value(s, "status_category", at) == "indeterminate"
    return sorted(s for s in alive(o, a) if ip(s, a) and ip(s, b))


def generate(events: list[dict[str, Any]], injections: list[dict[str, Any]], seed: int, counts: dict[str, int]) -> list[dict[str, Any]]:
    tenant = events[0]["tenant"]
    rng = random.Random(f"hard:{seed}:{tenant}")
    o = Oracle(events)
    issues = sorted(o.created)
    key = lambda s: s.split(":", 1)[1]
    project = key(issues[0]).rsplit("-", 1)[0]
    progress = ", ".join(sorted({e["payload"]["status"] for e in events if e["payload"].get("status_category") == "indeterminate"}))
    instants = sorted({f[0] for line in o.facts.values() for f in line} | {x for _, _, _, a, b in o.links for x in (a, b) if x})
    recorded = sorted({f[1] for line in o.facts.values() for f in line})
    start, end = min(o.created.values()), o.end
    out: list[dict[str, Any]] = []

    def add(kind: str, question: str, answer: Any, answer_type: str, **params: Any) -> None:
        if any(q["question"] == question for q in out):
            return
        out.append({"id": f"{tenant}-{kind}-{sum(q['type'] == kind for q in out) + 1:03d}", "tenant": tenant, "type": kind,
                    "question": question, "answer": answer, "answer_type": answer_type,
                    "params": {k: (fmt(v) if isinstance(v, datetime) else v) for k, v in params.items()}})

    def clear(at: datetime, among: list[datetime]) -> bool:
        i = bisect.bisect_left(among, at - MARGIN)
        return i == len(among) or among[i] > at + MARGIN

    def pick(lo: datetime, hi: datetime, among: list[datetime]) -> datetime | None:
        among = sorted(among)
        for _ in range(50):
            if hi - lo < timedelta(minutes=2):
                return None
            at = minute(lo + (hi - lo) * rng.random())
            if clear(at, among):
                return at
        return None

    def made(kind: str) -> int:
        return sum(q["type"] == kind for q in out)

    linked = sorted({x for src, _, dst, _, _ in o.links for x in (src, dst) if x in o.created})
    pairs = [(h1, h2) for h1 in HOPS for h2 in HOPS if INVERSE.get(h1) != h2]
    for _ in range(20000 if linked else 0):
        if made("two_hop") >= counts["two_hop"]:
            break
        subject = rng.choice(linked)
        h1, h2 = rng.choice(pairs)
        at = pick(o.created[subject], end, o.link_times(subject))
        mid = o.neighbours(subject, *h1, at) if at else []
        if not mid or not all(clear(at, o.link_times(m)) for m in mid):
            continue
        answer = two_hop(o, subject, h1, h2, at)
        if not 1 <= len(answer) <= 15:
            continue
        first, then = HOPS[h1].format(x=key(subject)), HOPS[h2].format(x="any of them")
        add("two_hop", f"On {fmt(at)}, take {first}. What were {then}? Count a link only if it existed on {fmt(at)}, and leave "
            f"{key(subject)} itself out.", sorted(key(n) for n in answer), "set", subject=subject, hop1=list(h1), hop2=list(h2), as_of=at)

    pool = [m for m in injections if m["predicate"] == "status"]
    for _ in range(2000 if pool else 0):
        if made("known_count") >= counts["known_count"]:
            break
        m = rng.choice(pool)
        occurred, rec = t(m["occurred_at"]), t(m["recorded_at"])
        at = minute(occurred + MARGIN + timedelta(minutes=rng.randint(1, 120)), up=True)
        known = minute(at + (rec - at) * rng.uniform(0.2, 0.8))
        if not (at < known < rec and clear(at, instants) and clear(known, recorded)):
            continue
        status = o.value(m["subject"], "status", at, known)
        if status is None or status == o.value(m["subject"], "status", at):
            continue
        answer = known_count(o, status, at, known)
        if answer == known_count(o, status, at, None):
            continue
        add("known_count", f"According to what had been recorded by {fmt(known)}, how many issues of the {project} project had status "
            f"{status} at {fmt(at)}?", answer, "count", status=status, as_of=at, known_at=known)

    moved = [s for s in issues if len(timeline(o, s)) >= 4]
    for _ in range(5000 if moved else 0):
        if made("entries") >= counts["entries"]:
            break
        subject = rng.choice(moved)
        line = timeline(o, subject)
        status = rng.choice([v for _, v in line[1:]])
        changes = [s for s, _ in line]
        a = pick(line[0][0], end, changes)
        b = pick(a + timedelta(days=7), end, changes) if a else None
        answer = entries(o, subject, status, a, b) if b else 0
        if answer < 1 or (answer == 1 and rng.random() < 0.8):  # mostly a re-entry
            continue
        add("entries", f"Between {fmt(a)} and {fmt(b)}, how many times was {key(subject)} moved into status {status}? Count every "
            f"move into it, not the issue's creation.", answer, "count", subject=subject, status=status, since=a, until=b)

    for _ in range(5000 if moved else 0):
        if made("time_in") >= counts["time_in"]:
            break
        subject = rng.choice(moved)
        line = timeline(o, subject)
        status = rng.choice([v for _, v in line])
        at = pick(line[0][0], end, [s for s, _ in line])
        total = time_in(o, subject, status, at) if at else timedelta(0)
        if total < DAY or not MARGIN <= total % DAY <= DAY - MARGIN:  # rounding to whole days never decides it
            continue
        add("time_in", f"Up to {fmt(at)}, how many whole days in total had {key(subject)} spent in status {status}, adding every "
            f"period it was in it? Round the total down to whole days.", total // DAY, "count", subject=subject, status=status, as_of=at)

    for _ in range(2000):
        if made("both") >= counts["both"]:
            break
        a = pick(start, end, instants)
        b = pick(a + timedelta(days=7), min(end, a + timedelta(days=90)), instants) if a else None
        if b is None:
            continue
        answer = both(o, a, b)
        at_a = sorted(s for s in alive(o, a) if o.value(s, "status_category", a) == "indeterminate")
        at_b = sorted(s for s in alive(o, b) if o.value(s, "status_category", b) == "indeterminate")
        if not 1 <= len(answer) <= 15 or answer in (at_a, at_b):
            continue
        add("both", f"Which issues of the {project} project had a status among: {progress} both on {fmt(a)} and on {fmt(b)}?",
            sorted(key(s) for s in answer), "set", since=a, until=b)
    return out


def check(events: list[dict[str, Any]], questions: list[dict[str, Any]]) -> int:
    """Keel's primitives against every gold answer, written apart from the oracle. Returns the number of disagreements."""
    from keel import Ingestor, MemoryStore, load_cartridge
    from keel.envelope import Event

    store = MemoryStore()
    Ingestor(load_cartridge(Path(__file__).parent), store).ingest([Event(**e) for e in events], strict=True)
    tenant = events[0]["tenant"]
    issues = [e.key for e in store.entities(tenant, "issue")]
    bad = 0

    def at_(p: dict[str, Any], k: str) -> datetime:
        return datetime.strptime(p[k], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)

    def value(subject: str, predicate: str, at: datetime, known: datetime | None = None) -> Any:
        fs = [f for f in store.facts(tenant, subject, at=at, known_at=known) if f.predicate == predicate]
        return fs[0].value if len(fs) == 1 else ("<several>" if fs else "<unknown>")

    def hop(k: str, rel: str, direction: str, at: datetime) -> set[str]:
        return {r.dst if r.src == k else r.src for r in store.relations(tenant, k, rel, at=at)
                if direction == "any" or (r.src == k) == (direction == "out")}

    def spells(k: str) -> list[Any]:  # the status as now known: closed facts of a correction are not part of it
        return sorted((f for f in store.facts(tenant, k) if f.predicate == "status" and not (f.evidence or "").endswith(":wrong")),
                      key=lambda f: f.valid_from)

    for q in questions:
        p = q["params"]
        if q["type"] == "two_hop":
            at = at_(p, "as_of")
            got = sorted({n for m in hop(p["subject"], *p["hop1"], at) for n in hop(m, *p["hop2"], at)} - {p["subject"]})
            got = [n.split(":", 1)[1] for n in got]
        elif q["type"] == "known_count":
            at = at_(p, "as_of")
            got = sum(value(k, "status", at, at_(p, "known_at")) == p["status"] for k in issues)
        elif q["type"] == "entries":
            fs, a, b = spells(p["subject"]), at_(p, "since"), at_(p, "until")
            got = sum(1 for prev, f in pairwise(fs) if f.value == p["status"] != prev.value and a <= f.valid_from < b)
        elif q["type"] == "time_in":
            at = at_(p, "as_of")
            total = sum((min(f.valid_to or at, at) - f.valid_from for f in spells(p["subject"])
                         if f.value == p["status"] and f.valid_from < at), timedelta(0))
            got = total // DAY
        else:
            a, b = at_(p, "since"), at_(p, "until")
            got = sorted(k.split(":", 1)[1] for k in issues
                         if value(k, "status_category", a) == "indeterminate" == value(k, "status_category", b))
        if got != q["answer"]:
            bad += 1
            if bad <= 8:
                print(f"  {q['id']}: Keel {got!r}, gold {q['answer']!r} | {q['question']}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("events", type=Path)
    ap.add_argument("injections", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=53)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    events = [json.loads(line) for line in a.events.open()]
    questions = generate(events, json.loads(a.injections.read_text()), a.seed, {k: 8 for k in TYPES})
    a.out.write_text(json.dumps(questions, ensure_ascii=False, indent=1))
    print(f"{len(questions)} questions in {a.out}: {({k: sum(q['type'] == k for q in questions) for k in TYPES})}")
    if a.check:
        bad = check(events, questions)
        print(f"Keel's primitives agree with the gold on {len(questions) - bad} of {len(questions)}")
        raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
