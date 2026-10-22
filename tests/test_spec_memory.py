"""SPEC §6, §6.1, §7.4, §8 and invariants 1, 5, 6, 10: memory scopes, policy, the agent registry, tenant
isolation, multi-valued predicates, task close and expiry. The SPEC is the oracle; a strict xfail marks
a place where Keel does not do what it says."""

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from keel import Ingestor, MemoryStore, Scope, SqliteStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.consolidate import consolidate_tenant
from keel.envelope import Event
from keel.scopes import MemoryPolicy
from keel.tools import running_from

FLEET, EVENTS = "examples/fleet", "examples/fleet/events.jsonl"
A, B = "acme-logistics", "beta-freight"
UTC = timezone.utc
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
AGENTS = {
    "tires-bot": {"writes": ["task", "agent"], "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]}},
    "copilot": {"writes": ["task", "agent", "tenant"]},
    "proposer": {"writes": ["task"]},
    "auditor": {"writes": []},
    "dispatcher": {"kind": "human", "writes": ["task", "agent", "tenant"]},
}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return MemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "keel.db")


def _events(tenant):
    with open(EVENTS, encoding="utf-8") as fh:
        return [Event(**{**json.loads(line), "tenant": tenant}) for line in fh if line.strip() and not line.startswith("#")]


def _fleet(store, *tenants, agents=None):
    c = load_cartridge(FLEET)
    if agents:
        c.agents = parse_agents(agents)
    for tenant in tenants or (A,):
        assert not Ingestor(c, store).ingest(_events(tenant), strict=True).errors
    return c


def _values(facts, predicate):
    return [f["value"] for f in facts if f["predicate"] == predicate]


def _later():
    return datetime.now(UTC) + timedelta(seconds=5)


# ---- scopes: who reads what ---------------------------------------------------------------


def test_agent_memory_reaches_only_its_author(store):
    """§6: an agent fact is returned only to a caller bound to its author; a Tools bound to no agent reads none."""
    c = _fleet(store)
    mine, other, nobody = Tools(c, store, A, agent="a1"), Tools(c, store, A, agent="a2"), Tools(c, store, A)
    mine.annotate("tire:T-9001", "note", "canary-a1", scope="agent")
    assert _values(mine.context("tire:T-9001")["facts"], "note") == ["canary-a1"]
    assert _values(mine.history("tire:T-9001"), "note") == ["canary-a1"]
    for t in (other, nobody):
        assert _values(t.context("tire:T-9001")["facts"], "note") == []
        assert _values(t.history("tire:T-9001", scope="agent"), "note") == []
        assert _values(t.history(changed_since=EPOCH), "note") == []
        assert t.search("canary-a1") == []
    nobody.annotate("tire:T-9001", "scratch", "canary-none", scope="agent")
    assert _values(nobody.context("tire:T-9001")["facts"], "scratch") == []  # bound to no agent: reads no agent memory


def test_task_memory_reaches_only_calls_passing_its_task(store):
    """§6: a task fact tied to a task is read by calls passing that task, never by the tenant at large."""
    c = _fleet(store)
    t = Tools(c, store, A, agent="a1")
    t.annotate("tire:T-9001", "note", "canary-t1", task="t1")
    assert _values(t.context("tire:T-9001", task="t1")["facts"], "note") == ["canary-t1"]
    assert _values(t.history("tire:T-9001", task="t1"), "note") == ["canary-t1"]
    assert _values(t.context("tire:T-9001")["facts"], "note") == []
    assert _values(t.context("tire:T-9001", task="t2")["facts"], "note") == []
    assert _values(t.history("tire:T-9001"), "note") == [] and _values(t.history("tire:T-9001", task="t2"), "note") == []
    assert _values(t.history(changed_since=EPOCH), "note") == []
    assert t.search("canary-t1") == []
    assert _values(t.context("unit:MXK-4821", depth=1)["neighbors"][0]["facts"], "note") == []  # not through a neighbour either


def test_task_memory_with_no_task_id_is_the_tenants(store):
    """§6: a task fact with no task id is read by the whole tenant, with or without a task."""
    c = _fleet(store)
    Tools(c, store, A, agent="a1").annotate("tire:T-9001", "note", "canary-loose")
    reader = Tools(c, store, A, agent="a2")
    assert _values(reader.context("tire:T-9001")["facts"], "note") == ["canary-loose"]
    assert _values(reader.context("tire:T-9001", task="t9")["facts"], "note") == ["canary-loose"]
    assert any(h.get("value") == "canary-loose" for h in reader.search("canary-loose"))


def test_global_memory_reaches_every_tenant_and_needs_evidence(store):
    """§6: annotate(scope="global") writes the __global__ partition, read by every tenant; evidence required."""
    c = _fleet(store, A, B)
    with pytest.raises(PermissionError):
        Tools(c, store, A).annotate("tire:T-9001", "advice", "rotate", scope="global")
    f = Tools(c, store, A).annotate("tire:T-9001", "advice", "rotate every 20k km", scope="global", evidence="human:ops")
    assert f["tenant"] == "__global__" and f["expires_at"] is None
    assert [x.value for x in store.facts("__global__", "tire:T-9001")] == ["rotate every 20k km"]
    assert store.facts(A, "tire:T-9001") and all(x.predicate != "advice" for x in store.facts(A, "tire:T-9001"))
    for tenant in (A, B, "fresh-tenant"):
        assert _values(Tools(c, store, tenant).context("tire:T-9001")["facts"], "advice") == ["rotate every 20k km"]


def test_global_memory_of_an_account_stays_in_that_account(store):
    """§6: a tenant `acme/t` reads and writes `acme/__global__`, which no other account (nor a plain tenant) sees."""
    c = _fleet(store)
    Tools(c, store, "acme/t1").annotate("tire:T-1", "advice", "acme-only", scope="global", evidence="human:ops")
    assert _values(Tools(c, store, "acme/t2").history("tire:T-1"), "advice") == ["acme-only"]
    for tenant in ("beta/t1", A):
        assert _values(Tools(c, store, tenant).history("tire:T-1"), "advice") == []
        assert Tools(c, store, tenant).search("acme-only") == []
    assert store.facts("__global__", "tire:T-1") == []


def test_the_global_partition_is_never_a_tenant():
    """§6: no Tools may be bound to a global partition and no event may carry it."""
    c, s = load_cartridge(FLEET), MemoryStore()
    for tenant in ("__global__", "acme/__global__"):
        with pytest.raises(ValueError):
            Tools(c, s, tenant)
        with pytest.raises(ValueError):
            Event(tenant=tenant, type="unit.registered", subject="unit:X", occurred_at=datetime(2026, 1, 1, tzinfo=UTC), source="s", payload={})


def test_promotion_is_an_explicit_annotate_and_the_narrower_fact_keeps_its_lifetime(store):
    """§6: promotion is a new annotate with evidence; the task fact stays, with its own expiry, in its own slot."""
    c = _fleet(store, agents=AGENTS)
    bot, copilot = Tools(c, store, A, agent="tires-bot"), Tools(c, store, A, agent="copilot")
    note = bot.annotate("tire:T-9001", "assessment", "replace", task="audit")
    assert _values(copilot.context("tire:T-9001")["facts"], "assessment") == []  # not promoted by accident
    with pytest.raises(PermissionError):
        copilot.annotate("tire:T-9001", "assessment", "replace", scope="tenant")  # no evidence, no promotion
    promoted = copilot.annotate("tire:T-9001", "assessment", "replace", scope="tenant", evidence=f"task audit; {note['source']}")
    assert promoted["expires_at"] is None and promoted["scope"] == "tenant"
    task_view = bot.history("tire:T-9001", predicate="assessment", task="audit")
    assert len(task_view) == 1 and task_view[0]["valid_to"] is None and task_view[0]["expires_at"] == note["expires_at"]
    assert _values(copilot.context("tire:T-9001")["facts"], "assessment") == ["replace"]


# ---- policy: TTL, evidence, idempotency ---------------------------------------------------


def test_ttl_per_scope_comes_from_the_cartridge(store):
    """§6, invariant 6: expires_at = now + the scope's TTL; tenant and global with `never` carry none."""
    c = _fleet(store)
    c.policy = MemoryPolicy(ttl={Scope.GLOBAL: None, Scope.TENANT: None, Scope.TASK: timedelta(hours=2), Scope.AGENT: timedelta(minutes=30)},
                            requires_evidence={Scope.GLOBAL, Scope.TENANT})
    t = Tools(c, store, A, agent="a1")
    for scope, ttl in (("task", timedelta(hours=2)), ("agent", timedelta(minutes=30))):
        before = datetime.now(UTC)
        exp = datetime.fromisoformat(t.annotate("tire:T-9001", "note", scope, scope=scope)["expires_at"])
        assert before + ttl <= exp <= datetime.now(UTC) + ttl
    assert t.annotate("tire:T-9001", "note", "t", scope="tenant", evidence="photo:1")["expires_at"] is None
    assert t.annotate("tire:T-9001", "note", "g", scope="global", evidence="photo:1")["expires_at"] is None


def test_requires_evidence_follows_the_cartridge_not_a_fixed_list(store):
    """§6: the scopes in requires_evidence refuse a write without evidence with PermissionError; the others do not."""
    c = _fleet(store)
    c.policy = replace(c.policy, requires_evidence={Scope.TASK})
    t = Tools(c, store, A, agent="a1")
    with pytest.raises(PermissionError, match="requires evidence"):
        t.annotate("tire:T-9001", "note", "x", scope="task")
    assert t.annotate("tire:T-9001", "note", "x", scope="task", evidence="photo:1")["evidence"] == "photo:1"
    assert t.annotate("tire:T-9001", "note", "y", scope="tenant")["scope"] == "tenant"


def test_declared_evidence_is_an_event_this_tenant_applied_to_the_subject_or_a_declared_prefix(store):
    """Invariant 5: with `evidence` declared, an event id counts only if this tenant's log applied it to the
    subject; a ref counts only with a declared prefix."""
    c = _fleet(store, A, B)
    c.policy = replace(c.policy, evidence=("event", "human:"))
    a, b = Tools(c, store, A), Tools(c, store, B)
    mine = a.history("tire:T-9001", predicate="depth_mm")[0]["evidence"]
    theirs = b.history("tire:T-9001", predicate="depth_mm")[0]["evidence"]
    assert mine != theirs
    assert a.annotate("tire:T-9001", "assessment", "worn", scope="tenant", evidence=mine)["evidence"] == mine
    assert a.annotate("tire:T-9001", "assessment", "worn", scope="tenant", evidence="human:ana")["evidence"] == "human:ana"
    for bad in (theirs, "photo:1", "", "human", a.history("unit:MXK-4821")[0]["evidence"]):
        with pytest.raises(PermissionError):
            a.annotate("tire:T-9001", "assessment", "worn", scope="tenant", evidence=bad)
    assert a.annotate("tire:T-9001", "note", "free", scope="task")["value"] == "free"  # not required there: anything goes


def test_an_unknown_scope_is_a_value_error_naming_the_valid_ones(store):
    """§7.4: an unknown scope raises with the valid ones."""
    c = _fleet(store)
    with pytest.raises(ValueError, match="tenant"):
        Tools(c, store, A).annotate("tire:T-9001", "note", "x", scope="team")


def test_a_key_makes_the_write_idempotent_across_retries(store):
    """§7.4: the same key on the same subject returns the fact it wrote; other arguments with that key raise."""
    c = _fleet(store)
    first = Tools(c, store, A, agent="a1").annotate("tire:T-9001", "note", "once", task="t1", key="step-1")
    again = Tools(c, store, A, agent="a1").annotate("tire:T-9001", "note", "once", task="t1", key="step-1")  # a new process retrying
    assert again == first
    assert len(Tools(c, store, A).history("tire:T-9001", predicate="note", task="t1")) == 1
    for changed in ({"value": "twice"}, {"predicate": "other"}, {"task": "t2"}, {"status": "proposed"}):
        args = {"subject": "tire:T-9001", "predicate": "note", "value": "once", "task": "t1", "key": "step-1", **changed}
        with pytest.raises(ValueError, match="idempotency key"):
            Tools(c, store, A, agent="a1").annotate(**args)
    assert Tools(c, store, A, agent="a1").annotate("unit:MXK-4821", "note", "once", task="t1", key="step-1")["subject"] == "unit:MXK-4821"


@pytest.mark.xfail(strict=True, reason="BUG: annotate(key=) finds another author's fact and returns it, so the second agent's private write never happens")
def test_one_agents_key_never_returns_another_agents_private_fact(store):
    """§6, §7.4: a key returns the fact *it* wrote; another agent's agent memory is never returned to it."""
    c = _fleet(store)
    Tools(c, store, A, agent="a1").annotate("tire:T-9001", "note", "same", scope="agent", key="step-1")
    a2 = Tools(c, store, A, agent="a2")
    got = a2.annotate("tire:T-9001", "note", "same", scope="agent", key="step-1")
    assert got["source"] == "annotate:a2"
    assert _values(a2.context("tire:T-9001")["facts"], "note") == ["same"]


# ---- agent registry -------------------------------------------------------------------------


def test_a_registry_refuses_a_missing_or_unknown_agent(store):
    """§6.1: with `agents:` declared, a Tools must be bound to a declared id; otherwise PermissionError at construction."""
    c = _fleet(store, agents=AGENTS)
    for agent in (None, "ghost", "", "human:dispatcher"):
        with pytest.raises(PermissionError):
            Tools(c, store, A, agent=agent)
    assert Tools(c, store, A, agent="dispatcher").agent.kind == "human"


def test_writes_outside_the_agents_scopes_are_refused(store):
    """§6.1: annotate to a scope outside `writes` raises; global never; the registry check comes before the evidence policy."""
    c = _fleet(store, agents=AGENTS)
    bot, copilot, auditor = (Tools(c, store, A, agent=a) for a in ("tires-bot", "copilot", "auditor"))
    with pytest.raises(PermissionError, match="may not write scope 'tenant'"):
        bot.annotate("tire:T-9001", "assessment", "x", scope="tenant", evidence="photo:1")
    with pytest.raises(PermissionError):
        auditor.annotate("tire:T-9001", "note", "x", scope="task")
    with pytest.raises(PermissionError):
        auditor.annotate("tire:T-9001", "note", "x", scope="task", status="proposed")  # no write right at all: no proposals
    with pytest.raises(PermissionError):
        copilot.annotate("tire:T-9001", "note", "x", scope="global", evidence="photo:1")
    assert copilot.annotate("tire:T-9001", "note", "x", scope="tenant", evidence="photo:1")["source"] == "annotate:copilot"
    assert store.facts("__global__", "tire:T-9001") == []


@pytest.mark.xfail(strict=True, reason="BUG: Tools.annotate trusts `writes: [global]` at runtime; only `keel validate` flags it")
def test_an_agent_never_writes_global_even_if_its_entry_says_so(store):
    """§6.1: `global` is never writable by an agent (§6: a privileged process, a Tools bound to no registry agent)."""
    c = _fleet(store, agents={"rogue": {"writes": ["task", "global"]}})
    with pytest.raises(PermissionError):
        Tools(c, store, A, agent="rogue").annotate("tire:T-9001", "advice", "x", scope="global", evidence="photo:1")


@pytest.mark.xfail(strict=True, reason="BUG: a proposal skips the scope right, so an agent writes a proposed fact into __global__ that every tenant reads")
def test_an_agent_cannot_reach_other_tenants_through_a_global_proposal(store):
    """§6, invariant 1: global memory holds only what a privileged process wrote; one tenant's agent never writes what another tenant reads."""
    c = _fleet(store, A, B, agents=AGENTS)
    try:
        Tools(c, store, A, agent="proposer").annotate("tire:T-9001", "advice", "canary-proposal", scope="global", evidence="photo:1", status="proposed")
    except PermissionError:
        pass
    other = Tools(c, store, B, agent="copilot")
    assert "canary-proposal" not in json.dumps(other.context("tire:T-9001"))
    assert store.facts("__global__", "tire:T-9001") == []


def test_read_slices_hold_on_every_tool(store):
    """§6.1: a hidden kind raises on context, history, related and annotate; search drops it; relations to it are left out."""
    c = _fleet(store, agents=AGENTS)
    bot = Tools(c, store, A, agent="tires-bot")
    for call in (lambda: bot.context("ticket:TK-3608"), lambda: bot.context(["tire:T-9001", "ticket:TK-3608"]),
                 lambda: bot.history("document:POL-7731"), lambda: bot.related("ticket:TK-3608"),
                 lambda: bot.annotate("ticket:TK-3608", "note", "x"), lambda: bot.search(kind="ticket"),
                 lambda: bot.search("x", related_to="ticket:TK-3608")):
        with pytest.raises(PermissionError):
            call()
    assert bot.search("TK-3608") == [] and bot.search("POL-7731") == []
    assert {f["subject"].split(":")[0] for f in bot.history(changed_since=EPOCH, limit=0)} <= {"unit", "tire", "inspection"}
    assert {f["subject"].split(":")[0] for f in bot.history(since=EPOCH, limit=0)} <= {"unit", "tire", "inspection"}
    ctx = bot.context("unit:MXK-4821", depth=1)
    assert {r["rel"] for r in ctx["relations"]} == {"mounted_on", "has_inspection"}
    assert {n["subject"].split(":")[0] for n in ctx["neighbors"]} == {"tire", "inspection"}
    assert {r["subject"].split(":")[0] for r in bot.related("unit:MXK-4821", depth=3, limit=0)} <= {"unit", "tire", "inspection"}


def test_a_person_signs_as_human(store):
    """§6.1: a `kind: human` entry signs annotate:human:<id>, corrected_by human:<id>, and reads its own agent memory."""
    c = _fleet(store, agents=AGENTS)
    person = Tools(c, store, A, agent="dispatcher")
    f = person.annotate("tire:T-9001", "assessment", "replace", scope="tenant", evidence="photo:1")
    assert (f["source"], f["corrected_by"]) == ("annotate:human:dispatcher", "human:dispatcher")
    person.annotate("tire:T-9001", "scratch", "mine", scope="agent")
    assert _values(person.context("tire:T-9001")["facts"], "scratch") == ["mine"]
    assert _values(Tools(c, store, A, agent="copilot").context("tire:T-9001")["facts"], "scratch") == []
    via_bot = Tools(c, store, A, agent="copilot").annotate("tire:T-9001", "note", "x", by="human:ana")
    assert (via_bot["source"], via_bot["corrected_by"]) == ("annotate:copilot", "human:ana")


def test_proposals_wait_for_someone_with_the_right_on_the_scope(store):
    """§7.4: a proposal needs some write right and the scope's evidence; it never touches accepted facts, is one
    open per author; a decision needs the right on the scope and settles every open proposal; a rejection is history only."""
    c = _fleet(store, agents=AGENTS)
    proposer, bot, person = (Tools(c, store, A, agent=a) for a in ("proposer", "tires-bot", "dispatcher"))
    person.annotate("tire:T-9001", "condition", "fine", scope="tenant", evidence="photo:0")
    with pytest.raises(PermissionError):
        proposer.annotate("tire:T-9001", "condition", "replace", scope="tenant", status="proposed")  # the evidence policy still applies
    proposer.annotate("tire:T-9001", "condition", "rotate", scope="tenant", evidence="photo:1", status="proposed")
    proposer.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence="photo:2", status="proposed")  # replaces its own
    bot.annotate("tire:T-9001", "condition", "retread", scope="tenant", evidence="photo:3", status="proposed")
    ctx = person.context("tire:T-9001")
    assert sorted(_values(ctx["proposals"], "condition")) == ["replace", "retread"] and _values(ctx["facts"], "condition") == ["fine"]
    with pytest.raises(PermissionError):
        proposer.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence="photo:2")  # may propose, not decide
    person.annotate("tire:T-9001", "condition", "replace", scope="tenant", evidence="photo:2")
    ctx = person.context("tire:T-9001")
    assert "proposals" not in ctx and _values(ctx["facts"], "condition") == ["replace"]
    proposer.annotate("tire:T-9001", "condition", "scrap", scope="tenant", evidence="photo:4", status="proposed")
    rejected = person.annotate("tire:T-9001", "condition", "no", scope="tenant", evidence="photo:5", status="rejected")
    assert rejected["valid_to"] is not None
    ctx = person.context("tire:T-9001")
    assert "proposals" not in ctx and _values(ctx["facts"], "condition") == ["replace"]
    assert [f["status"] for f in person.history("tire:T-9001", predicate="condition") if f["value"] in ("scrap", "no")] == ["proposed", "rejected"]


def test_rules_decide_on_the_callers_view_only(store):
    """§6: another agent's or another task's memory never decides a verdict."""
    c = _fleet(store)
    sep20 = running_from(datetime(2026, 9, 20, 12, tzinfo=UTC))  # depth 5.2 then: not critical
    Tools(c, store, A, agent="a1", clock=sep20).annotate("tire:T-9001", "depth_mm", 1.0, scope="agent")
    Tools(c, store, A, agent="a1", clock=sep20).annotate("tire:T-9001", "depth_mm", 1.5, task="t1")
    at = datetime(2026, 9, 21, tzinfo=UTC)
    other = Tools(c, store, A, agent="a2")
    assert other.context("tire:T-9001", as_of=at)["evaluation"] == {"tire_critical": False}
    assert other.context("tire:T-9001", as_of=at, task="t2")["evaluation"] == {"tire_critical": False}
    assert other.context("unit:MXK-4821", as_of=at)["evaluation"]["unit_grounded"] is False
    assert other.search(rule="tire_critical", as_of=at) == []


# ---- isolation: no tool returns another tenant's facts --------------------------------------


def test_no_tool_returns_another_tenants_memory_through_any_parameter(store):
    """Invariant 1, rule 8: same subjects, same agent ids and task ids in two tenants; nothing of B ever reaches A."""
    c = _fleet(store, A, B)
    c.predicates = {"recommendation": {"multi": True}}
    wb = Tools(c, store, B, agent="copilot")
    a_mark = Tools(c, store, A).context("tire:T-9001")["watermark"]
    for subject in ("tire:T-9001", "unit:MXK-4821", "inspection:INS-4821-10"):
        wb.annotate(subject, "note", "canary-b-tenant", scope="tenant", evidence="photo:b", key="k1")
        wb.annotate(subject, "note", "canary-b-task", task="t1")
        wb.annotate(subject, "note", "canary-b-loose")
        wb.annotate(subject, "note", "canary-b-agent", scope="agent")
        wb.annotate(subject, "recommendation", "canary-b-multi", scope="tenant", evidence="photo:b")
        wb.annotate(subject, "idea", "canary-b-proposal", scope="tenant", evidence="photo:b", status="proposed")
    a = Tools(c, store, A, agent="copilot")
    assert a.context("tire:T-9001")["watermark"] == a_mark  # B's writes do not move A's cursor
    a.annotate("tire:T-9001", "note", "canary-a", task="t1")
    known, ago = _later(), datetime(2026, 9, 20, tzinfo=UTC)
    reads = [
        a.context("tire:T-9001"), a.context(["tire:T-9001", "unit:MXK-4821"], task="t1", depth=1),
        a.context("unit:MXK-4821", depth=1, as_known_at=known, task="t1"), a.context("tire:T-9001", as_of=ago, detail="brief"),
        a.context("tire:T-9001", detail="concise", only=["note", "recommendation", "idea"]),
        a.history("tire:T-9001", limit=0), a.history("tire:T-9001", task="t1", as_known_at=known), a.history("unit:MXK-4821", changed_since=EPOCH),
        a.history(changed_since=EPOCH, limit=0), a.history(changed_since=EPOCH, task="t1", as_known_at=known, limit=0),
        a.history(since=EPOCH, limit=0), a.history(since=EPOCH, scope="agent", limit=0),
        a.search("canary"), a.search("canary-b"), a.search(["canary", "note"], detail="full", depth=1), a.search("canary b tenant"),
        a.search(kind="tire", text="canary"), a.search(kind=["tire", "unit"], detail="full", depth=1, limit=0),
        a.search(kind="tire", where={"note": "canary-b-tenant"}), a.search("note", related_to="unit:MXK-4821", detail="full"),
        a.search("canary", as_known_at=known, detail="full"), a.search("canary", mode="semantic"), a.search("canary", detail="count"),
        a.search(kind="tire", rule="tire_critical", detail="full", depth=1), a.search("canary", per_kind=5, detail="full"),
        a.related("unit:MXK-4821", depth=3, limit=0),
    ]
    blob = json.dumps(reads, default=str)
    assert "canary-b" not in blob and "beta-freight" not in blob
    assert "canary-a" in blob  # the reads are not vacuous
    same = a.annotate("tire:T-9001", "note", "canary-b-tenant", scope="tenant", evidence="photo:a", key="k1")  # B's key and value
    assert (same["tenant"], same["evidence"]) == (A, "photo:a")  # A wrote its own; B's fact was not returned


def test_maintenance_jobs_stay_in_their_tenant(store):
    """Invariant 1: close_task, expire and consolidate act on one tenant; the same task id elsewhere is untouched."""
    c = _fleet(store, A, B)
    c.consolidate = {"recurring": {"from": {"predicate": "assessment"}, "min_count": 2, "into": {"predicate": "recurring_issue"}}}
    for tenant in (A, B):
        t = Tools(c, store, tenant, agent="x")
        t.annotate("tire:T-9001", "note", f"job-{tenant}", task="t1")
        t.annotate("tire:T-9001", "assessment", "leak", by="a")
    Tools(c, store, B, agent="x").annotate("tire:T-9001", "assessment", "leak", by="b")
    assert store.close_task(B, "t1") == 1
    assert _values(Tools(c, store, A, agent="x").context("tire:T-9001", task="t1")["facts"], "note") == [f"job-{A}"]
    assert _values(Tools(c, store, B, agent="x").context("tire:T-9001", task="t1")["facts"], "note") == []
    assert store.expire(B, now=_later()) >= 1
    assert len(store.facts(A, "tire:T-9001", known_at=EPOCH)) == 0 and store.facts(A, "tire:T-9001")
    assert consolidate_tenant(c, store, A).written == {"recurring": 0}  # A has one assessment: B's do not count
    assert consolidate_tenant(c, store, B).written == {"recurring": 1}
    assert _values(Tools(c, store, A).context("tire:T-9001")["facts"], "recurring_issue") == []


# ---- multi-valued predicates ----------------------------------------------------------------


def test_a_multi_valued_predicate_keeps_one_slot_per_author_and_reports_conflicts(store):
    """§3.3, §7.1: a multi predicate keeps one open value per author; a rewrite closes only its author's; context
    lists the conflict; as known before the rewrite, each author's value then. A plain predicate is last-writer-wins."""
    c = _fleet(store)
    c.predicates = {"recommendation": {"multi": True}}
    a1, a2, reader = Tools(c, store, A, agent="a1"), Tools(c, store, A, agent="a2"), Tools(c, store, A, agent="r")
    a1.annotate("unit:MXK-4821", "recommendation", "replace tire", scope="tenant", evidence="photo:1")
    a2.annotate("unit:MXK-4821", "recommendation", "renew policy", scope="tenant", evidence="photo:2")
    between = datetime.now(UTC)
    time.sleep(0.002)
    a1.annotate("unit:MXK-4821", "recommendation", "ground unit", scope="tenant", evidence="photo:3")
    ctx = reader.context("unit:MXK-4821")
    assert sorted(_values(ctx["facts"], "recommendation")) == ["ground unit", "renew policy"]
    assert sorted((x["value"], x["by"]) for x in ctx["conflicts"]["recommendation"]) == [("ground unit", "a1"), ("renew policy", "a2")]
    brief = reader.context("unit:MXK-4821", detail="brief")["facts"]["recommendation"]
    assert sorted((x["value"], x["by"]) for x in brief) == [("ground unit", "a1"), ("renew policy", "a2")]
    then = reader.context("unit:MXK-4821", as_known_at=between)
    assert sorted(_values(then["facts"], "recommendation")) == ["renew policy", "replace tire"]
    closed = [f for f in reader.history("unit:MXK-4821", predicate="recommendation") if f["valid_to"] is not None]
    assert [f["value"] for f in closed] == ["replace tire"]
    a1.annotate("unit:MXK-4821", "verdict", "a", scope="tenant", evidence="photo:4")
    a2.annotate("unit:MXK-4821", "verdict", "b", scope="tenant", evidence="photo:5")
    assert _values(reader.context("unit:MXK-4821")["facts"], "verdict") == ["b"] and "verdict" not in reader.context("unit:MXK-4821").get("conflicts", {})


# ---- task close and expiry ------------------------------------------------------------------


def test_closing_a_task_hides_its_memory_now_but_not_as_known_before(store):
    """§3.3, §7.6: close_task sets expires_at to the close; reads stop seeing it, a read as known before the
    close still does; another task's memory is untouched."""
    c = _fleet(store)
    t = Tools(c, store, A, agent="a1")
    t.annotate("tire:T-9001", "note", "audit note", task="t1")
    t.annotate("tire:T-9001", "note", "other job", task="t2")
    before = datetime.now(UTC)
    time.sleep(0.002)  # the close strictly after `before`: memory that expires at t is gone at t
    assert store.close_task(A, "t1") == 1
    assert _values(t.context("tire:T-9001", task="t1")["facts"], "note") == []
    assert _values(t.history("tire:T-9001", task="t1"), "note") == []
    assert _values(t.context("tire:T-9001", task="t2")["facts"], "note") == ["other job"]
    assert _values(t.context("tire:T-9001", task="t1", as_known_at=before)["facts"], "note") == ["audit note"]
    assert _values(t.history("tire:T-9001", task="t1", as_known_at=before), "note") == ["audit note"]
    assert store.close_task(A, "t1") == 0  # already closed
    assert store.expire(A) == 1
    assert _values(t.history("tire:T-9001", task="t1", as_known_at=before), "note") == []  # physically dropped now


@pytest.mark.xfail(strict=True, reason="BUG: history() with no subject drops memory expired by real now before applying as_known_at")
def test_tenant_wide_history_as_known_before_a_task_closed_still_has_its_memory(store):
    """§7.6: memory counts when it had not expired by t, on the tenant-wide history as on a subject's."""
    c = _fleet(store)
    t = Tools(c, store, A, agent="a1")
    t.annotate("tire:T-9001", "note", "audit note", task="t1")
    before = datetime.now(UTC)
    time.sleep(0.002)
    store.close_task(A, "t1")
    assert _values(t.history(changed_since=EPOCH, task="t1", as_known_at=before), "note") == ["audit note"]


def test_reads_hide_expired_memory_before_the_job_drops_it(store):
    """§6: reads exclude expired facts even before expire runs; expire then drops exactly those."""
    c = _fleet(store)
    t = Tools(c, store, A, agent="a1")
    note = t.annotate("tire:T-9001", "note", "fleeting", scope="agent")
    after = datetime.fromisoformat(note["expires_at"]) + timedelta(seconds=1)
    assert _values(t.context("tire:T-9001")["facts"], "note") == ["fleeting"]
    assert _values(t.context("tire:T-9001", as_known_at=after)["facts"], "note") == []
    assert store.expire(A, now=after - timedelta(days=2)) == 0
    assert store.expire(A, now=after) == 1


@pytest.mark.xfail(strict=True, reason="BUG: store.search scans every fact with no expiry filter, and Tools.search does not drop expired hits")
def test_text_search_hides_expired_memory(store):
    """§6: reads MUST exclude expired facts on every path that returns facts, text search included."""
    c = _fleet(store)
    past = datetime(2026, 9, 2, tzinfo=UTC)
    store.open_fact(A, "tire:T-9001", "note", "canary-expired", valid_from=datetime(2026, 9, 1, tzinfo=UTC), scope=Scope.TASK, expires_at=past)
    t = Tools(c, store, A)
    assert _values(t.history("tire:T-9001"), "note") == []  # the other reads already hide it
    assert t.search("canary-expired") == []
