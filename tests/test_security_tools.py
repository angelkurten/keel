"""Adversarial review of the tool layer and the agent registry (CLAUDE.md rules 5 and 8, SPEC §6, §6.1, §7).

An agent bound to `Tools(cartridge, store, tenant, agent=X)` tries to read memory or kinds it may not, write a
scope it may not, or reach another tenant or the global partition, through every parameter of the five tools.
A passing test is a defence that holds. A strict xfail marked SECURITY is a hole found by this review and
not yet fixed. A test named `test_by_design_*` pins a behaviour the SPEC accepts on purpose.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from keel import Scope, Tools, load_cartridge
from keel.agents import parse_agents
from keel.consolidate import consolidate_tenant
from keel.semantic import HashEmbedder, MemoryIndex, index_tenant

FLEET = "examples/fleet"
T, OTHER = "sec-tenant", "sec-other"
UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
AGENTS = {
    "bot": {"writes": ["task", "agent"], "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]}},
    "peer": {"writes": ["task", "agent"]},
    "copilot": {"writes": ["task", "agent", "tenant"]},
    "boss": {"kind": "human", "writes": ["task", "agent", "tenant"]},
}
SECRET_RULE = {  # a rule on a visible kind that walks a relation and reads a kind the bot may not see
    "applies_to": "unit",
    "definition": "A unit with a flagged ticket.",
    "when": {"via": "has_ticket", "direction": "out", "any": {"predicate": "status", "eq": "flagged"}},
}
MEMORY_CANARIES = ("CANARY-PEER-AGENT", "CANARY-PEER-TASK", "TASK-CANARY-77", "CANARY-PEER-PROP-AGENT",
                   "CANARY-PEER-PROP-TASK", "CANARY-MISLABELLED-GLOBAL")
HIDDEN_CANARIES = ("SECRET-STATUS", "SECRET-TICKET-TITLE", "ticket:K", "document:D1")


def _cartridge(agents=AGENTS):
    c = load_cartridge(FLEET)
    if agents:
        c.agents = parse_agents(agents)
    c.rules = {**c.rules, "unit_has_secret_ticket": SECRET_RULE}
    return c


def _graph(store, tenant=T):
    """A unit with two tires (visible to the bot), twelve tickets and a document (hidden kinds, hidden relations)."""
    store.upsert_entity(tenant, "unit", "U1", plate="ABC-123")
    for tire in ("T1", "T2"):
        store.upsert_entity(tenant, "tire", tire, position="P1")
        store.link(tenant, f"tire:{tire}", "mounted_on", "unit:U1", valid_from=T0)
        store.open_fact(tenant, f"tire:{tire}", "depth_mm", 5.0, valid_from=T0, source="event", scope=Scope.TENANT, evidence="ev:1")
    for i in range(1, 13):
        store.upsert_entity(tenant, "ticket", f"K{i}", title="SECRET-TICKET-TITLE")
        store.link(tenant, "unit:U1", "has_ticket", f"ticket:K{i}", valid_from=T0 + timedelta(minutes=i))
    store.open_fact(tenant, "ticket:K1", "status", "flagged", valid_from=T0, source="event", scope=Scope.TENANT, evidence="ev:2")
    store.open_fact(tenant, "ticket:K2", "status", "SECRET-STATUS", valid_from=T0, source="event", scope=Scope.TENANT, evidence="ev:2")
    store.upsert_entity(tenant, "document", "D1")
    store.link(tenant, "document:D1", "covers_unit", "unit:U1", valid_from=T0)


def _plant_memory(c, store):
    """What `peer` keeps to itself, and a fact mislabelled into the global partition that no reader may merge."""
    peer = Tools(c, store, T, agent="peer")
    for s in ("tire:T1", "unit:U1"):
        peer.annotate(s, "note", "CANARY-PEER-AGENT", scope="agent")
        peer.annotate(s, "tasknote", "CANARY-PEER-TASK", task="TASK-CANARY-77")
        peer.annotate(s, "idea", "CANARY-PEER-PROP-AGENT", scope="agent", status="proposed")
        peer.annotate(s, "plan", "CANARY-PEER-PROP-TASK", task="TASK-CANARY-77", status="proposed")
        store.open_fact("__global__", s, "leak", "CANARY-MISLABELLED-GLOBAL", valid_from=T0, source="annotate:bot", scope=Scope.AGENT)


def _later():
    return datetime.now(UTC) + timedelta(seconds=5)


def _calls(t: Tools):
    """Every read parameter of the five tools an agent could turn against the slice it was given."""
    later = _later()
    yield lambda: t.context("tire:T1")
    for detail in ("full", "concise", "brief"):
        yield lambda d=detail: t.context(["unit:U1", "tire:T1"], depth=1, detail=d)
    yield lambda: t.context("unit:U1", as_known_at=later, depth=1)
    yield lambda: t.context("unit:U1", task="another-task")
    yield lambda: t.history("tire:T1", limit=0)
    yield lambda: t.history("tire:T1", as_known_at=later)
    for scope in ("agent", "task", "tenant", "global"):
        yield lambda s=scope: t.history("unit:U1", scope=s)
    yield lambda: t.history(changed_since=EPOCH, limit=0)
    yield lambda: t.history(changed_since=EPOCH, limit=1)
    yield lambda: t.history(changed_since=EPOCH, as_known_at=later, limit=0)
    yield lambda: t.history(since=EPOCH, limit=0, detail="concise")
    yield lambda: t.history(since=EPOCH, as_known_at=later, limit=0)
    yield lambda: t.history(changed_since=EPOCH, predicate="note")
    for text in ("CANARY", "TASK-CANARY-77", "SECRET", "canary peer agent", "leak", "ticket"):
        for detail in ("entity", "key", "brief", "concise", "full", "count"):
            yield lambda x=text, d=detail: t.search(x, detail=d)
    yield lambda: t.search("CANARY", kind=["unit", "tire"], detail="full")
    yield lambda: t.search(kind="tire", where={"note": "CANARY-PEER-AGENT"}, detail="count")
    yield lambda: t.search(kind="tire", where={"tasknote": "CANARY-PEER-TASK"}, detail="full")
    yield lambda: t.search(kind="unit", where={"title": "SECRET-TICKET-TITLE"}, detail="count")
    yield lambda: t.search("CANARY", per_kind=1, detail="full")
    yield lambda: t.search("U1", depth=1, detail="full")
    yield lambda: t.search(kind="unit", depth=1, detail="full")
    yield lambda: t.search("", related_to="unit:U1", detail="full", limit=0)
    yield lambda: t.search(related_to="unit:U1", via="has_ticket", kind="unit", detail="count")
    yield lambda: t.search("CANARY", as_of=later, detail="full")
    yield lambda: t.search("CANARY", as_known_at=later, detail="full")
    yield lambda: t.search(["CANARY", "SECRET"], detail="full")
    yield lambda: t.related("unit:U1", depth=3, limit=0)
    yield lambda: t.related("unit:U1", limit=1)
    yield lambda: t.related("unit:U1", relation="has_ticket")
    yield lambda: t.related("tire:T1", relation=["mounted_on", "has_ticket", "covers_unit"], depth=2)


def _run(t: Tools) -> str:
    out = []
    for call in _calls(t):
        try:
            out.append(call())
        except PermissionError as e:  # a refusal is fine; what it says is checked too
            out.append(str(e))
    return json.dumps(out, default=str)


# ---- reads: memory scopes ---------------------------------------------------------------------


def test_no_read_parameter_returns_another_agents_or_tasks_memory(store):
    """§6: agent memory reaches only its author, task memory only calls passing the task; the global partition
    is merged only for global-scope facts. Checked on every parameter of every read tool, for an unrestricted
    agent and for one with a read slice."""
    c = _cartridge()
    _graph(store)
    _plant_memory(c, store)
    for agent in ("bot", "copilot", "boss"):
        text = _run(Tools(c, store, T, agent=agent))
        assert [x for x in MEMORY_CANARIES if x in text] == [], agent


def test_counts_and_more_markers_do_not_count_hidden_rows(store):
    """detail='count', `more` totals and relations_more count only what the caller may see."""
    c = _cartridge()
    _graph(store)
    _plant_memory(c, store)
    bot, copilot = Tools(c, store, T, agent="bot"), Tools(c, store, T, agent="copilot")
    for t in (bot, copilot):
        for text in ("CANARY-PEER-AGENT", "CANARY-PEER-TASK", "CANARY-MISLABELLED-GLOBAL"):
            assert [x["count"] for x in t.search(text, detail="count") if "count" in x] == [0]
        assert t.search(kind="tire", where={"note": "CANARY-PEER-AGENT"}, detail="count") == [{"count": 0}]
        assert t.search(kind="unit", text="CANARY", detail="count") == [{"count": 0}]
    assert [x["count"] for x in bot.search("SECRET", detail="count") if "count" in x] == [0]
    assert copilot.search(related_to="unit:U1", via="has_ticket", kind="ticket", detail="count") == [{"count": 12}]  # control
    assert bot.search(related_to="unit:U1", via="has_ticket", kind="unit", detail="count") == [{"count": 0}]
    visible = len(bot.history(changed_since=EPOCH, limit=0))
    cut = bot.history(changed_since=EPOCH, limit=1)
    assert cut[0]["more"]["total"] == visible
    assert "relations_more" in copilot.context("unit:U1") and "relations_more" not in bot.context("unit:U1")
    more = bot.related("unit:U1", limit=1)[0]["more"]
    assert more["total"] == 2 and set(more["by_relation"]) == {"mounted_on"}


def test_a_hidden_kind_is_refused_the_same_whether_or_not_the_subject_exists(store):
    """No existence oracle on a hidden kind: the refusal does not depend on what is stored."""
    c = _cartridge()
    _graph(store)
    bot = Tools(c, store, T, agent="bot")
    for call in (bot.context, bot.history, bot.related, lambda s: bot.annotate(s, "n", "x"), lambda s: bot.search("x", related_to=s)):
        msgs = []
        for subject in ("ticket:K1", "ticket:NOPE"):
            with pytest.raises(PermissionError) as e:
                call(subject)
            msgs.append(str(e.value))
        assert msgs[0] == msgs[1]


def test_a_bare_id_hint_never_names_a_subject_of_a_hidden_kind(store):
    c = _cartridge()
    _graph(store)
    bot = Tools(c, store, T, agent="bot")
    for call in (bot.context, bot.history, bot.related, lambda s: bot.annotate(s, "n", "x")):
        with pytest.raises(ValueError) as e:
            call("K1")
        assert "ticket:K1" not in str(e.value)


def test_hidden_kinds_never_come_back_from_semantic_search(store):
    """mode='semantic': the index holds every kind, the read slice filters what it admits, ids in the query too."""
    c = _cartridge()
    _graph(store)
    _plant_memory(c, store)
    emb, idx = HashEmbedder(), MemoryIndex()
    index_tenant(store, T, emb, idx, policy=c.semantic)
    bot = Tools(c, store, T, agent="bot", embedder=emb, index=idx)
    peer_view = Tools(c, store, T, agent="copilot", embedder=emb, index=idx)
    assert any(h.get("key", "").startswith("ticket:") for h in peer_view.search("SECRET-STATUS ticket", mode="semantic"))  # control
    calls = [lambda: bot.search("SECRET-STATUS ticket K1", mode="semantic", detail="full", depth=1),
             lambda: bot.search("ticket:K1 SECRET", mode="semantic", detail="full"),
             lambda: bot.search("SECRET ticket", mode="semantic", kind="ticket"),
             lambda: bot.search("SECRET ticket", mode="semantic", per_kind=3, detail="full"),
             lambda: bot.search("CANARY peer agent", mode="semantic", detail="full", min_score=-1.0),
             lambda: bot.search("SECRET", mode="semantic", related_to="unit:U1", detail="count")]
    out = []
    for call in calls:
        try:
            out.append(call())
        except PermissionError as e:
            out.append(str(e))
    text = json.dumps(out, default=str)
    assert [x for x in HIDDEN_CANARIES + MEMORY_CANARIES if x in text] == []


def test_a_read_slice_hides_kinds_and_relations_on_every_read_parameter(store):
    """§6.1: hidden kinds and relations never appear as subjects, neighbours, facts or attributes of any read."""
    c = _cartridge()
    _graph(store)
    text = _run(Tools(c, store, T, agent="bot"))
    assert [x for x in HIDDEN_CANARIES if x in text] == []


def test_by_design_rules_see_the_whole_tenant_so_a_verdict_can_reflect_a_hidden_kind(store):
    """Accepted (SPEC §6, tools._Graph): rules are the product's semantics and see the whole tenant, whatever the
    caller's read slice; only memory scopes apply to them. A rule that walks a hidden relation into a hidden
    kind is therefore a one-bit oracle on that kind. The cartridge author decides which rules exist."""
    c = _cartridge()
    _graph(store)
    bot = Tools(c, store, T, agent="bot")
    assert bot.context("unit:U1")["evaluation"]["unit_has_secret_ticket"] is True
    assert bot.search(rule="unit_has_secret_ticket", detail="key") == ["unit:U1"]
    store.open_fact(T, "ticket:K1", "status", "closed", valid_from=T0 + timedelta(days=1), source="event", scope=Scope.TENANT, evidence="ev:3")
    assert bot.context("unit:U1")["evaluation"]["unit_has_secret_ticket"] is not True  # the bit flips with the hidden value


def test_rules_never_read_another_agents_memory_on_a_neighbour(store):
    """§6: a rule that walks a relation reads the neighbour as the caller may see it."""
    c = _cartridge()
    _graph(store)
    Tools(c, store, T, agent="peer").annotate("tire:T1", "depth_mm", 1.0, scope="agent")
    Tools(c, store, T, agent="peer").annotate("tire:T2", "depth_mm", 1.0, task="t-peer")
    for agent in ("bot", "copilot"):
        t = Tools(c, store, T, agent=agent)
        assert t.context("unit:U1")["evaluation"]["unit_grounded"] is False
        assert t.search(rule="unit_grounded", detail="count") == [{"count": 0}]
        assert t.search(kind="unit", rule="unit_grounded", related_to="tire:T1", detail="count") == [{"count": 0}]
    assert Tools(c, store, T, agent="peer").context("unit:U1")["evaluation"]["unit_grounded"] is True  # control: its own view


# ---- writes: scopes, proposals, impersonation --------------------------------------------------


def test_a_narrow_write_never_closes_a_wider_or_anothers_fact(store):
    """Scopes never close each other; agent memory is per author, task memory per task."""
    c = _cartridge()
    _graph(store)
    bot, peer, copilot = (Tools(c, store, T, agent=a) for a in ("bot", "peer", "copilot"))
    copilot.annotate("tire:T1", "condition", "shared", scope="tenant", evidence="photo:1")
    peer.annotate("tire:T1", "condition", "peer-private", scope="agent")
    peer.annotate("tire:T1", "condition", "peer-task", task="t-peer")
    bot.annotate("tire:T1", "condition", "bot-private", scope="agent")
    bot.annotate("tire:T1", "condition", "bot-task", task="t-bot")
    bot.annotate("tire:T1", "condition", "bot-loose")
    vals = lambda t, task=None: sorted(f["value"] for f in t.context("tire:T1", task=task)["facts"] if f["predicate"] == "condition")
    assert vals(copilot) == ["bot-loose", "shared"]
    assert vals(peer, "t-peer") == ["bot-loose", "peer-private", "peer-task", "shared"]
    assert vals(bot, "t-bot") == ["bot-loose", "bot-private", "bot-task", "shared"]


@pytest.mark.parametrize("status", [None, "rejected"])
@pytest.mark.parametrize("scope", ["agent", "task"])
def test_a_private_or_task_note_never_settles_a_tenant_proposal(store, scope, status):
    c = _cartridge()
    _graph(store)
    copilot, bot, boss = (Tools(c, store, T, agent=a) for a in ("copilot", "bot", "boss"))
    copilot.annotate("tire:T1", "condition", "replace", scope="tenant", evidence="photo:1", status="proposed")
    bot.annotate("tire:T1", "condition", "my own view", scope=scope, status=status)  # bot may not write tenant
    proposals = boss.context("tire:T1").get("proposals", [])
    assert [p["value"] for p in proposals if p["predicate"] == "condition"] == ["replace"]


def test_by_never_takes_over_another_authors_proposal(store):
    c = _cartridge()
    _graph(store)
    boss, copilot = Tools(c, store, T, agent="boss"), Tools(c, store, T, agent="copilot")
    boss.annotate("tire:T1", "condition", "boss-view", scope="tenant", evidence="photo:1", status="proposed")
    copilot.annotate("tire:T1", "condition", "copilot-view", scope="tenant", evidence="photo:2", status="proposed", by="human:boss")
    values = sorted(p["value"] for p in boss.context("tire:T1").get("proposals", []) if p["predicate"] == "condition")
    assert values == ["boss-view", "copilot-view"]


def test_by_never_overwrites_another_authors_multi_value(store):
    c = _cartridge()
    c.predicates = {**c.predicates, "recommendation": {"multi": True}}
    _graph(store)
    boss, copilot = Tools(c, store, T, agent="boss"), Tools(c, store, T, agent="copilot")
    boss.annotate("tire:T1", "recommendation", "boss-rec", scope="tenant", evidence="photo:1")
    copilot.annotate("tire:T1", "recommendation", "copilot-rec", scope="tenant", evidence="photo:2", by="human:boss")
    values = sorted(f["value"] for f in boss.context("tire:T1")["facts"] if f["predicate"] == "recommendation")
    assert values == ["boss-rec", "copilot-rec"]


def test_by_never_changes_whose_memory_a_write_is_or_what_a_caller_reads(store):
    """§6.1: `by` is provenance only (corrected_by); source, and so agent-memory ownership, stay the caller's."""
    c = _cartridge()
    _graph(store)
    boss, bot = Tools(c, store, T, agent="boss"), Tools(c, store, T, agent="bot")
    boss.annotate("tire:T1", "note", "boss-private", scope="agent")
    f = bot.annotate("tire:T1", "note", "bot-as-boss", scope="agent", by="human:boss")
    assert (f["source"], f["corrected_by"]) == ("annotate:bot", "human:boss")
    notes = lambda t: sorted(x["value"] for x in t.context("tire:T1")["facts"] if x["predicate"] == "note")
    assert notes(boss) == ["boss-private"] and notes(bot) == ["bot-as-boss"]
    with pytest.raises(PermissionError):
        bot.annotate("tire:T1", "note", "x", scope="tenant", evidence="photo:1", by="human:boss")  # by grants no right


def test_proposals_and_keys_never_widen_a_write(store):
    """status='proposed' needs some write right, the scope's evidence, a visible kind, and never reaches global;
    a key is its author's in every scope."""
    c = _cartridge(agents={**AGENTS, "reader": {"writes": []}})
    _graph(store)
    bot, peer, reader = (Tools(c, store, T, agent=a) for a in ("bot", "peer", "reader"))
    for call in (lambda: reader.annotate("tire:T1", "n", "x", scope="tenant", evidence="photo:1", status="proposed"),
                 lambda: bot.annotate("tire:T1", "n", "x", scope="tenant", status="proposed"),
                 lambda: bot.annotate("tire:T1", "n", "x", scope="global", evidence="photo:1", status="proposed"),
                 lambda: bot.annotate("ticket:K1", "n", "x", scope="task", status="proposed"),
                 lambda: bot.annotate("tire:T1", "n", "x", scope="tenant", evidence="photo:1", status="rejected")):
        with pytest.raises(PermissionError):
            call()
    peer.annotate("tire:T1", "n", "peer", task="t1", key="k")
    got = bot.annotate("tire:T1", "n", "bot", task="t1", key="k")
    assert got["source"] == "annotate:bot" and got["value"] == "bot"
    with pytest.raises(ValueError, match="idempotency key"):
        bot.annotate("tire:T1", "n", "bot", scope="agent", key="k")  # same author, other scope: refused, not replayed
    assert store.facts("__global__", "tire:T1") == []


def test_an_agent_id_shaped_like_a_person_never_reads_or_signs_as_that_person(store):
    c = _cartridge(agents={"ana": {"kind": "human", "writes": ["agent", "task"]}, "human:ana": {"writes": ["agent", "task"]}})
    _graph(store)
    Tools(c, store, T, agent="ana").annotate("tire:T1", "note", "ANA-PRIVATE", scope="agent")
    with pytest.raises(PermissionError):  # an id with ':' would sign as annotate:human:ana, the person: refused
        Tools(c, store, T, agent="human:ana")
    assert any("human:ana" in p for p in c.validate())  # and keel validate says why


# ---- tenants and the global partition ------------------------------------------------------------


def test_no_tool_binds_to_a_global_partition_and_global_reads_are_global_scope_only(store):
    """Rule 8: a global partition is never a tenant; from it, reads merge only global-scope facts; an agent never
    writes it; another tenant never sees this tenant's memory."""
    c = _cartridge()
    for tenant in ("__global__", "acme/__global__", "/__global__", "a/b/__global__"):
        with pytest.raises(ValueError):
            Tools(c, store, tenant, agent="bot")
    _graph(store)
    _graph(store, OTHER)
    _plant_memory(c, store)
    with pytest.raises(PermissionError):
        Tools(c, store, T, agent="copilot").annotate("tire:T1", "advice", "x", scope="global", evidence="photo:1")
    Tools(c, store, T, agent="copilot").annotate("tire:T1", "advice", "CANARY-TENANT-T", scope="tenant", evidence="photo:1")
    other = json.dumps([Tools(c, store, OTHER, agent=a).context("tire:T1") for a in ("bot", "peer", "copilot")]
                       + [Tools(c, store, OTHER, agent="peer").history(changed_since=EPOCH, limit=0)], default=str)
    assert [x for x in MEMORY_CANARIES + ("CANARY-TENANT-T",) if x in other] == []


def test_a_consolidation_stays_in_its_tenant_and_never_promotes_agent_memory_unless_listed(store):
    """consolidate.py: default sources are task memory; agent memory is promoted only when the cartridge lists it;
    whatever `into.scope` says, the fact is written in the tenant's own partition."""
    c = _cartridge()
    c.consolidate = {
        "pattern": {"from": {"predicate": "obs"}, "reduce": "distinct", "min_count": 2, "into": {"predicate": "obs_pattern", "scope": "global"}},
    }
    _graph(store)
    _graph(store, OTHER)
    peer = Tools(c, store, T, agent="peer")
    for i in range(3):
        peer.annotate("tire:T1", "obs", f"PRIVATE-OBS-{i}", scope="agent")
    consolidate_tenant(c, store, T)
    assert [f for f in store.facts(T, "tire:T1") if f.predicate == "obs_pattern"] == []
    for i in range(2):
        peer.annotate("tire:T1", "obs", f"TASK-OBS-{i}", task=f"t{i}")
    consolidate_tenant(c, store, T)
    assert store.facts("__global__", "tire:T1") == []
    assert "TASK-OBS" not in json.dumps(Tools(c, store, OTHER, agent="copilot").context("tire:T1"), default=str)


def test_by_design_a_declared_consolidation_promotes_task_memory_to_tenant_without_evidence(store):
    """Accepted (CLAUDE.md, decided 2026-10-15): consolidation is a cartridge-declared job. An agent that may write
    only task memory can, through min_count notes, get its value into tenant scope without the tenant evidence
    policy, and a task's memory becomes readable by every task. The cartridge author opts in per predicate."""
    c = _cartridge()
    c.consolidate = {"last": {"from": {"predicate": "obs"}, "reduce": "latest", "min_count": 2, "into": {"predicate": "obs_last"}}}
    _graph(store)
    bot = Tools(c, store, T, agent="bot")
    bot.annotate("tire:T1", "obs", "first", task="t-private")
    bot.annotate("tire:T1", "obs", "BOT-CHOSEN", task="t-private")
    consolidate_tenant(c, store, T)
    facts = Tools(c, store, T, agent="copilot").context("tire:T1")["facts"]
    assert [(f["value"], f["scope"]) for f in facts if f["predicate"] == "obs_last"] == [("BOT-CHOSEN", "tenant")]
