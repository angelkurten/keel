"""Memory governance as a property (paper experiment E5a): random writes and reads across tenants, agents,
tasks and scopes, checked against a reference model of who may see what. Every write carries a unique
canary value; a read leaks when a canary it may not see appears anywhere in its output, and misses when
one it may see is absent from the subject's context. Both are counted and must be zero, before and after
a replay of both tenants from their logs.

KEEL_PROPS_WRITES=2000 KEEL_PROPS_READS=20000 uv run pytest tests/test_memory_properties.py -s   # the paper's run
"""

import json
import os
import random
from dataclasses import dataclass

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.envelope import Event
from keel.ingest import JsonlAdapter

FLEET, EVENTS = "examples/fleet", "examples/fleet/events.jsonl"
TENANTS = ("acme-logistics", "beta-freight")
TASKS = (None, "audit-1", "audit-2")
AGENTS = {
    "tires-bot": {"writes": ["task", "agent"], "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]}},
    "copilot": {"writes": ["task", "agent", "tenant"]},
    "auditor": {"writes": []},
}


@dataclass(frozen=True)
class Write:
    canary: str
    tenant: str  # the writer's; global memory is every tenant's
    scope: str
    author: str | None
    task: str | None
    subject: str


def may_see(w: Write, tenant: str, agent: str | None, task: str | None, kinds: frozenset[str] | None) -> bool:
    """The reference model: CLAUDE.md rules 5 and 8, written out once more."""
    if kinds is not None and w.subject.split(":")[0] not in kinds:
        return False
    if w.scope == "global":
        return True
    if w.tenant != tenant:
        return False
    if w.scope == "task":
        return w.task is None or w.task == task
    if w.scope == "agent":
        return w.author == agent
    return True


def test_no_read_sees_what_it_may_not_and_none_misses_what_it_may():
    n_writes, n_reads = int(os.environ.get("KEEL_PROPS_WRITES", "300")), int(os.environ.get("KEEL_PROPS_READS", "3000"))
    rnd = random.Random(7)
    governed, privileged = load_cartridge(FLEET), load_cartridge(FLEET)
    governed.agents = parse_agents(AGENTS)
    store = MemoryStore()
    logs = {t: [Event(**(e.model_dump() | {"tenant": t})) for e in JsonlAdapter(EVENTS)] for t in TENANTS}
    for t in TENANTS:
        Ingestor(governed, store).ingest(logs[t], strict=True)
    subjects = [f"{e.kind}:{e.id}" for e in store.entities(TENANTS[0])]
    spec = governed.agents

    writes: list[Write] = []
    for i in range(n_writes):
        tenant, canary = rnd.choice(TENANTS), f"CANARY-{i:05d}"
        if rnd.random() < 0.05:  # a privileged process (no registry) writes global memory
            subject = rnd.choice(subjects)
            Tools(privileged, store, tenant).annotate(subject, f"note_{i}", canary, scope="global", evidence="human:ops")
            writes.append(Write(canary, tenant, "global", None, None, subject))
            continue
        agent = rnd.choice(["tires-bot", "copilot"])
        scope = rnd.choice(sorted(spec[agent].writes))
        task = rnd.choice(TASKS) if scope == "task" else None
        subject = rnd.choice([s for s in subjects if spec[agent].sees_kind(s.split(":")[0])])
        Tools(governed, store, tenant, agent=agent).annotate(subject, f"note_{i}", canary, scope=scope, task=task,
                                                              evidence="human:review" if scope == "tenant" else None)
        writes.append(Write(canary, tenant, scope, agent, task, subject))

    def check(reads: int) -> tuple[int, int, int]:
        leaks = misses = done = refused = 0
        for _ in range(reads):
            tenant, agent, task = rnd.choice(TENANTS), rnd.choice(sorted(AGENTS)), rnd.choice(TASKS)
            kinds = spec[agent].kinds
            tools = Tools(governed, store, tenant, agent=agent)
            subject = rnd.choice(subjects)  # kinds it may not read too: those must be refused
            probe = rnd.choice(["context", "context1", "history", "changes", "search"])
            try:
                out = read(tools, probe, subject, task)
            except PermissionError:
                out = None
                refused += 1
            text = json.dumps(out, default=str)
            leaks += sum(w.canary in text for w in writes if not may_see(w, tenant, agent, task, kinds))
            if probe == "context" and out is not None:
                misses += sum(w.canary not in text for w in writes if w.subject == subject and may_see(w, tenant, agent, task, kinds))
            done += 1
        return leaks, misses, done, refused

    def read(tools: Tools, probe: str, subject: str, task: str | None) -> object:
        if probe == "context":
            return tools.context(subject, task=task)
        if probe == "context1":
            return tools.context(subject, task=task, depth=1)
        if probe == "history":
            return tools.history(subject, task=task, limit=0)
        if probe == "changes":
            return tools.history(changed_since="2000-01-01T00:00:00Z", task=task, limit=0)
        return tools.search(rnd.choice(writes).canary)

    leaks, misses, done, refused = check(n_reads)
    for t in TENANTS:  # annotations are not events: a replay keeps them, and the rules still hold
        Ingestor(governed, store).replay(t, events=logs[t])
    leaks_after, misses_after, done_after, _ = check(n_reads // 3)
    print(f"\nE5a: {n_writes} writes, {done} reads (+{done_after} after replay): leaks {leaks}+{leaks_after}, misses {misses}+{misses_after}, refused reads {refused}")
    assert (leaks, misses, leaks_after, misses_after) == (0, 0, 0, 0)
