"""The benchmark's questions over a Jira tenant, with gold answers computed from the log, never by a model.

    uv run python -m evals.jira.questions <tenant>.bitemporal.jsonl <tenant>.injections.json questions.json [--seed 7] [--check]

Templates filled with a fixed seed. The oracle below walks the event log on its own (no Keel import), so
Keel is checked against it, not graded by itself. Six types:

- current:   a field's value now.
- as_of:     a field's value at an instant (valid time).
- known_at:  a field's value at an instant, as recorded by a later instant (transaction time; the late
             arrivals and corrections of inject.py).
- links:     the issues linked to one by a relation, filtered by their state at an instant (two hops).
- rule:      a rule's verdict at an instant, the definition given in the question (blocked, stalled, urgent_unassigned).
- abstain:   an issue that does not exist, or a field the tracker does not keep: the answer is "unknown".

Instants are whole minutes at least an hour from any change to what the question reads, so rounding never
decides an answer. Gold answers: a value (None reads "none"), a sorted list of issue keys, a bool, or None
for "unknown"; `answer_type` says which.

`--check` answers every question with Keel's primitives directly (no agent) and compares: the ceiling an
agent with Keel can reach, and a test of the oracle and of Keel at once.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FIELDS = {"status": "status", "assignee": "assignee", "priority": "priority", "resolution": "resolution", "issue_type": "issue type"}
CHANGES = {"issue.status_changed": ("status", "status_category"), "issue.assigned": ("assignee",), "issue.priority_changed": ("priority",),
           "issue.resolution_changed": ("resolution",), "issue.type_changed": ("issue_type",)}
URGENT = {"Blocker", "Critical", "Highest"}
STALL = timedelta(days=30)
MARGIN = timedelta(hours=1)
RULES = {
    "blocked": "An issue is blocked while an issue that blocks it is not done.",
    "stalled": "An issue has stalled when it is in progress and its status has not changed for 30 days or more.",
    "urgent_unassigned": "An urgent issue is one with priority Blocker, Critical or Highest; it is urgent and unassigned when it is not done and nobody is assigned.",
}
LINK_ASKS = {  # relation -> (direction from the asked issue, phrase)
    "blocks": ("in", "the issues that block {key}"),
    "relates_to": ("any", "the issues related to {key}"),
    "duplicates": ("any", "the issues linked to {key} as duplicates"),
    "depends_on": ("out", "the issues {key} depends on"),
    "clones": ("any", "the issues linked to {key} as clones"),
}


def t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def fmt(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def minute(at: datetime, up: bool = False) -> datetime:
    floor = at.replace(second=0, microsecond=0)
    return floor + timedelta(minutes=1) if up and floor < at else floor


class Oracle:
    """The tenant's history from its log alone: facts by (subject, predicate), links as intervals."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.facts: dict[tuple[str, str], list[tuple[datetime, datetime, str, Any]]] = defaultdict(list)
        self.links: list[tuple[str, str, str, datetime, datetime | None]] = []
        self.created: dict[str, datetime] = {}
        open_links: dict[tuple[str, str, str], int] = {}
        for e in sorted(events, key=lambda e: (e["occurred_at"], e["id"])):
            at, rec, subject, p = t(e["occurred_at"]), t(e["recorded_at"]), e["subject"], e["payload"]
            if e["type"] == "issue.created":
                self.created[subject] = at
                for k in ("status", "status_category", "priority", "assignee", "resolution", "issue_type"):
                    self.facts[(subject, k)].append((at, rec, e["id"], p[k]))
                if p["parent"]:
                    self._link(open_links, subject, "subtask_of", f"issue:{p['parent']}", at)
            elif e["type"] in CHANGES:
                for k in CHANGES[e["type"]]:
                    self.facts[(subject, k)].append((at, rec, e["id"], p[k]))
            elif e["type"] == "issue.linked":
                self._link(open_links, subject, p["relation"], f"issue:{p['other']}", at)
            elif e["type"] == "issue.unlinked":
                i = open_links.pop((subject, p["relation"], f"issue:{p['other']}"), None)
                if i is not None:
                    src, rel, dst, start, _ = self.links[i]
                    self.links[i] = (src, rel, dst, start, at)
        self.end = max(t(e["occurred_at"]) for e in events)

    def _link(self, open_links: dict[tuple[str, str, str], int], src: str, rel: str, dst: str, at: datetime) -> None:
        if (src, rel, dst) not in open_links:  # an open link is linked once
            open_links[(src, rel, dst)] = len(self.links)
            self.links.append((src, rel, dst, at, None))

    def fact(self, subject: str, predicate: str, at: datetime | None = None, known: datetime | None = None) -> tuple[datetime, Any] | None:
        """(valid_from, value) of the fact holding at `at` as recorded by `known`: the latest start, the later record on a tie."""
        line = [f for f in self.facts.get((subject, predicate), ()) if (at is None or f[0] <= at) and (known is None or f[1] <= known)]
        if not line:
            return None
        best = max(line, key=lambda f: (f[0], f[1], f[2]))
        return best[0], best[3]

    def value(self, subject: str, predicate: str, at: datetime | None = None, known: datetime | None = None) -> Any:
        f = self.fact(subject, predicate, at, known)
        return None if f is None else f[1]

    def changes(self, subject: str, predicates: tuple[str, ...]) -> list[datetime]:
        return sorted(f[0] for p in predicates for f in self.facts.get((subject, p), ()))

    def neighbours(self, subject: str, rel: str, direction: str, at: datetime) -> list[str]:
        out = set()
        for src, r, dst, start, end in self.links:
            if r != rel or start > at or (end is not None and end <= at):
                continue
            if src == subject and direction in ("out", "any"):
                out.add(dst)
            if dst == subject and direction in ("in", "any"):
                out.add(src)
        return sorted(out)

    def link_times(self, subject: str) -> list[datetime]:
        return sorted(x for src, _, dst, start, end in self.links if subject in (src, dst) for x in (start, end) if x is not None)

    def rule(self, name: str, subject: str, at: datetime) -> bool | None:
        cat = self.value(subject, "status_category", at)
        if cat is None:
            return None
        if name == "blocked":
            blockers = [self.value(b, "status_category", at) for b in self.neighbours(subject, "blocks", "in", at)]
            return None if None in blockers else cat != "done" and any(c != "done" for c in blockers)
        if name == "stalled":
            since = self.fact(subject, "status_category", at)[0]
            return cat == "indeterminate" and at - since >= STALL
        return self.value(subject, "priority", at) in URGENT and cat != "done" and self.value(subject, "assignee", at) is None


def generate(events: list[dict[str, Any]], injections: list[dict[str, Any]], seed: int, counts: dict[str, int]) -> list[dict[str, Any]]:
    tenant = events[0]["tenant"]
    rng = random.Random(f"{seed}:{tenant}")
    o = Oracle(events)
    issues = sorted(o.created)
    key = lambda s: s.split(":", 1)[1]
    out: list[dict[str, Any]] = []

    def add(kind: str, question: str, answer: Any, answer_type: str, **params: Any) -> None:
        out.append({"id": f"{tenant}-{kind}-{sum(q['type'] == kind for q in out) + 1:03d}", "tenant": tenant, "type": kind,
                    "question": question, "answer": answer, "answer_type": answer_type,
                    "params": {k: (fmt(v) if isinstance(v, datetime) else v) for k, v in params.items()}})

    def quiet(subject: str, around: list[datetime], lo: datetime, hi: datetime, tries: int = 50) -> datetime | None:
        """A whole minute in [lo, hi] at least MARGIN from every instant in `around`."""
        for _ in range(tries):
            if hi - lo < timedelta(minutes=2):
                return None
            at = minute(lo + (hi - lo) * rng.random())
            if all(abs(at - x) >= MARGIN for x in around):
                return at
        return None

    # current: the value now
    for subject in rng.sample(issues, counts["current"]):
        field = rng.choice(list(FIELDS))
        add("current", rng.choice(["What is the current {f} of {k}?", "What {f} does {k} have now?"]).format(f=FIELDS[field], k=key(subject)),
            o.value(subject, field), "value", subject=subject, predicate=field)

    # as_of: the value at an instant inside the field's history; mostly one that is no longer current,
    # so answering with today's value does not pass for remembering
    differ = round(counts["as_of"] * 0.8)
    while sum(q["type"] == "as_of" for q in out) < counts["as_of"]:
        subject, field = rng.choice(issues), rng.choice(["status", "assignee", "priority", "resolution"])
        around = o.changes(subject, (field,))
        at = quiet(subject, around, around[0], around[-1]) if len(around) >= 3 else None
        if at is None or ((o.value(subject, field, at) != o.value(subject, field)) != (differ > 0)):
            continue
        differ -= 1
        add("as_of", rng.choice(["What was the {f} of {k} on {at}?", "On {at}, what {f} did {k} have?"]).format(f=FIELDS[field], k=key(subject), at=fmt(at)),
            o.value(subject, field, at), "value", subject=subject, predicate=field, as_of=at)

    # known_at: one injected window each, half before the record arrived and half after
    pool = list(injections)
    rng.shuffle(pool)
    for m in pool:
        if sum(q["type"] == "known_at" for q in out) >= counts["known_at"]:
            break
        occurred, recorded = t(m["occurred_at"]), t(m["recorded_at"])
        later = [x for x in o.changes(m["subject"], (m["predicate"],)) if x > occurred]
        nxt = min(later) if later else o.end + timedelta(days=365)
        at = minute(occurred + min(timedelta(minutes=30), (nxt - occurred) / 2), up=True)
        if not occurred <= at < nxt or recorded - at < timedelta(minutes=5):
            continue
        before = len([q for q in out if q["type"] == "known_at"]) % 2 == 0
        known = minute(at + (recorded - at) * rng.uniform(0.1, 0.9)) if before else minute(recorded + timedelta(days=3) * rng.random(), up=True)
        if not (at <= known < recorded if before else known >= recorded):
            continue
        add("known_at", rng.choice(["According to what had been recorded by {k}, what was the {f} of {key} at {at}?",
                                    "As the tracker stood on {k}, what {f} did {key} have at {at}?"]).format(
                k=fmt(known), f=FIELDS[m["predicate"]], key=key(m["subject"]), at=fmt(at)),
            o.value(m["subject"], m["predicate"], at, known), "value", subject=m["subject"], predicate=m["predicate"], as_of=at, known_at=known,
            window="before" if before else "after", injection=m["kind"])

    # links: issues reached by one relation, filtered by their state at an instant
    linked = sorted({(src, rel) for src, rel, dst, _, _ in o.links if rel in LINK_ASKS} | {(dst, rel) for src, rel, dst, _, _ in o.links if rel in LINK_ASKS})
    rng.shuffle(linked)
    for subject, rel in linked:
        if sum(q["type"] == "links" for q in out) >= counts["links"]:
            break
        direction, phrase = LINK_ASKS[rel]
        around = o.link_times(subject)
        at = quiet(subject, around, min(around), o.end)
        if not at:
            continue
        others = o.neighbours(subject, rel, direction, at)
        cats = {n: o.value(n, "status_category", at) for n in others}
        if len(others) < 2 or None in cats.values() or any(abs(at - x) < MARGIN for n in others for x in o.changes(n, ("status_category",))):
            continue
        done = rng.random() < 0.5
        if all((c == "done") != done for c in cats.values()) and rng.random() < 0.8:
            done = not done  # mostly ask for the side that has issues; an empty answer stays possible
        answer = sorted(key(n) for n, c in cats.items() if (c == "done") == done)
        add("links", f"Of {phrase.format(key=key(subject))} on {fmt(at)}, which were {'done' if done else 'not done'} at that time?",
            answer, "set", subject=subject, relation=rel, as_of=at, done=done)

    # rule: a verdict at an instant, yes and no alike
    rules = [r for r in RULES if r != "blocked" or any(rel == "blocks" for _, rel, _, _, _ in o.links)]  # no blocks links, no blocked
    per_rule = {r: counts["rule"] // len(rules) + (i < counts["rule"] % len(rules)) for i, r in enumerate(rules)}
    for name, n in per_rule.items():
        want = {True: n // 2 + n % 2, False: n // 2}
        for _ in range(20000):
            if not any(want.values()):
                break
            subject = rng.choice(issues)
            around = o.changes(subject, ("status_category", "priority", "assignee")) + o.link_times(subject)
            at = quiet(subject, around, o.created[subject], o.end, tries=3)
            if at is None or (name == "blocked" and not o.neighbours(subject, "blocks", "in", at)):
                continue
            if name == "stalled" and at - o.fact(subject, "status_category", at)[0] < STALL + MARGIN and o.value(subject, "status_category", at) == "indeterminate":
                continue  # too close to the 30-day line
            verdict = o.rule(name, subject, at)
            if verdict is None or want[verdict] == 0:
                continue
            want[verdict] -= 1
            add("rule", f"{RULES[name]} Was {key(subject)} {name.replace('_', ' and ')} on {fmt(at)}?", verdict, "bool",
                subject=subject, rule=name, as_of=at)

    # abstain: what nobody recorded
    project = key(issues[0]).rsplit("-", 1)[0]
    top = max(int(key(s).rsplit("-", 1)[1]) for s in issues)
    for i in range(counts["abstain"]):
        if i % 2 == 0:
            ghost = f"{project}-{top + rng.randint(1000, 9000)}"
            add("abstain", f"What is the current status of {ghost}?", None, "unknown", subject=f"issue:{ghost}", predicate="status")
        else:
            subject = rng.choice(issues)
            field = rng.choice(["story points", "sprint", "fix version", "due date"])
            add("abstain", f"What was the {field} of {key(subject)}?", None, "unknown", subject=subject, predicate=field)
    return out


def check(events: list[dict[str, Any]], questions: list[dict[str, Any]]) -> int:
    """Keel's primitives against every gold answer. Returns the number of disagreements."""
    from keel import Ingestor, MemoryStore, Tools, load_cartridge
    from keel.envelope import Event

    cartridge = load_cartridge(Path(__file__).parent)
    store = MemoryStore()
    Ingestor(cartridge, store).ingest([Event(**e) for e in events], strict=True)
    tenant = events[0]["tenant"]
    tools = Tools(cartridge, store, tenant)
    bad = 0

    def at_(p: dict[str, Any], k: str) -> datetime | None:
        return datetime.strptime(p[k], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc) if k in p else None

    def value(subject: str, predicate: str, at: datetime | None, known: datetime | None = None) -> Any:
        fs = [f for f in store.facts(tenant, subject, at=at, known_at=known) if f.predicate == predicate and (at or f.valid_to is None)]
        return fs[0].value if len(fs) == 1 else ("<several>" if fs else "<unknown>")

    for q in questions:
        p = q["params"]
        if q["type"] in ("current", "as_of", "known_at"):
            got = value(p["subject"], p["predicate"], at_(p, "as_of"), at_(p, "known_at"))
        elif q["type"] == "links":
            at = at_(p, "as_of")
            direction = LINK_ASKS[p["relation"]][0]
            others = {r.dst if r.src == p["subject"] else r.src for r in store.relations(tenant, p["subject"], p["relation"], at=at)
                      if direction == "any" or (r.src == p["subject"]) == (direction == "out")}
            got = sorted(n.split(":", 1)[1] for n in others if (value(n, "status_category", at) == "done") == p["done"])
        elif q["type"] == "rule":
            got = tools.context(p["subject"], as_of=at_(p, "as_of"))["evaluation"][p["rule"]]
        else:
            known = store.entity(tenant, p["subject"]) is not None and p["predicate"] in FIELDS
            got = "<known>" if known else None
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
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    events = [json.loads(line) for line in a.events.open()]
    counts = {"current": 20, "as_of": 25, "known_at": 25, "links": 15, "rule": 15, "abstain": 5}
    questions = generate(events, json.loads(a.injections.read_text()), a.seed, counts)
    a.out.write_text(json.dumps(questions, ensure_ascii=False, indent=1))
    by = {k: sum(q["type"] == k for q in questions) for k in counts}
    print(f"{len(questions)} questions in {a.out}: {by}")
    if a.check:
        bad = check(events, questions)
        print(f"Keel's primitives agree with the gold on {len(questions) - bad} of {len(questions)}")
        raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
