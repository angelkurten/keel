"""Agent registry: identity, write scopes and read slices, enforced in Tools, never by the agent."""

import json
import shutil
import sys
from dataclasses import replace

import pytest

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter

FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"
AGENTS = {
    "tires-bot": {
        "description": "Reads tire depth, writes task memory for the inspection audit.",
        "writes": ["task", "agent"],
        "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]},
    },
    "fleet-copilot": {"description": "Sees everything, writes tenant memory with evidence.", "writes": ["task", "agent", "tenant"]},
}


def _fleet_with_agents():
    c = load_cartridge(FLEET)
    c.agents = parse_agents(AGENTS)
    s = MemoryStore()
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    return c, s


def test_registry_is_validated_with_the_cartridge():
    c = load_cartridge(FLEET)
    c.agents = parse_agents({"odd": {"writes": ["global", "nope"], "reads": {"kinds": ["spaceship"], "relations": ["flies"]}}})
    msgs = c.validate()
    for expected in ("may not write scope 'global'", "unknown scope 'nope'", "undeclared kind 'spaceship'", "undeclared relation 'flies'"):
        assert any(expected in m for m in msgs), msgs
    c.agents = parse_agents(AGENTS)
    assert c.validate() == []


def test_missing_or_unknown_agent_is_refused_only_when_a_registry_exists():
    c, s = _fleet_with_agents()
    with pytest.raises(PermissionError):
        Tools(c, s, TENANT)
    with pytest.raises(PermissionError):
        Tools(c, s, TENANT, agent="someone")
    open_tools = Tools(load_cartridge(FLEET), s, TENANT, agent="anyone")  # no registry: open, the id is attribution only
    f = open_tools.annotate("tire:T-9001", "assessment", "fine", scope="tenant", evidence="ev-1")
    assert f["source"] == "annotate:anyone" and f["corrected_by"] == "anyone"


def test_reads_are_sliced_per_agent():
    c, s = _fleet_with_agents()
    bot = Tools(c, s, TENANT, agent="tires-bot")
    assert {r["rel"] for r in bot.context("unit:MXK-4821")["relations"]} == {"mounted_on", "has_inspection"}
    assert {r["subject"].split(":")[0] for r in bot.related("unit:MXK-4821")} == {"tire", "inspection"}
    with pytest.raises(PermissionError):
        bot.context("ticket:TK-3608")
    with pytest.raises(PermissionError):
        bot.history("document:POL-7731")
    assert bot.search("TK-3608") == [] and bot.search("T-9001")
    copilot = Tools(c, s, TENANT, agent="fleet-copilot")
    assert len(copilot.context("unit:MXK-4821")["relations"]) == 6 and copilot.search("TK-3608")


def test_writes_are_scoped_and_signed_per_agent():
    c, s = _fleet_with_agents()
    bot = Tools(c, s, TENANT, agent="tires-bot")
    with pytest.raises(PermissionError):
        bot.annotate("tire:T-9001", "assessment", "critical", scope="tenant", evidence="ev-1")
    with pytest.raises(PermissionError):
        bot.annotate("ticket:TK-3608", "note", "x")  # cannot write about what it cannot read
    f = bot.annotate("tire:T-9001", "assessment", "critical", scope="task")
    assert f["source"] == "annotate:tires-bot" and f["corrected_by"] == "tires-bot"
    g = bot.annotate("tire:T-9001", "assessment", "ok", scope="task", by="human:angel")
    assert g["source"] == "annotate:tires-bot" and g["corrected_by"] == "human:angel"  # the human corrected, through the bot
    copilot = Tools(c, s, TENANT, agent="fleet-copilot")
    assert copilot.annotate("tire:T-9001", "assessment", "replace", scope="tenant", evidence="photo:1")["scope"] == "tenant"
    with pytest.raises(PermissionError):
        copilot.annotate("tire:T-9001", "assessment", "x", scope="global", evidence="ev-2")  # never from an agent


def test_mcp_server_is_bound_to_one_agent(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cart = tmp_path / "fleet"
    shutil.copytree(FLEET, cart)
    with (cart / "cartridge.yaml").open("a", encoding="utf-8") as fh:
        fh.write("\nagents:\n  tires-bot:\n    writes: [task, agent]\n    reads: { kinds: [unit, tire, inspection], relations: [mounted_on, has_inspection] }\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "keel.cli", "serve", str(cart), "--tenant", TENANT, "--events", EVENTS, "--agent", "tires-bot"],
    )

    async def run():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 5
            res = await session.call_tool("context", {"subject": "unit:MXK-4821"})
            assert set(json.loads(res.content[0].text)["relations"]) == {"<-mounted_on", "has_inspection"}  # brief: grouped by relation
            res = await session.call_tool("context", {"subject": "ticket:TK-3608"})
            is_error = getattr(res, "is_error", None) or getattr(res, "isError", None)  # mcp 2.x / 1.x
            assert is_error and "may not read kind 'ticket'" in res.content[0].text

    anyio.run(run)


def test_declared_evidence_must_be_an_applied_event_or_a_declared_ref():
    c, s = _fleet_with_agents()
    open_tools = Tools(load_cartridge(FLEET), s, TENANT, agent="anyone")
    open_tools.annotate("tire:T-9001", "depth_mm", 9.9, scope="tenant", evidence="trust-me")  # undeclared: any string, as before
    c.policy = replace(c.policy, evidence=("event", "photo:"))
    copilot = Tools(c, s, TENANT, agent="fleet-copilot")
    event_id = copilot.history("tire:T-9001", predicate="depth_mm", detail="full")[0]["evidence"]
    with pytest.raises(PermissionError, match="not one this cartridge accepts"):
        copilot.annotate("tire:T-9001", "depth_mm", 1.0, scope="tenant", evidence="trust-me")
    with pytest.raises(PermissionError, match="not one this cartridge accepts"):  # another tenant's event is no evidence here
        Tools(c, s, "other-tenant", agent="fleet-copilot").annotate("tire:T-9001", "depth_mm", 1.0, scope="tenant", evidence=event_id)
    other = copilot.history("unit:MXK-4821", detail="full")[0]["evidence"]  # a real event, about another subject
    with pytest.raises(PermissionError, match="not one this cartridge accepts"):
        copilot.annotate("tire:T-9001", "depth_mm", 1.0, scope="tenant", evidence=other)
    assert copilot.annotate("tire:T-9001", "depth_mm", 2.5, scope="tenant", evidence=event_id)["value"] == 2.5
    assert copilot.annotate("tire:T-9001", "depth_mm", 2.4, scope="tenant", evidence="photo:12")["value"] == 2.4
    assert copilot.annotate("tire:T-9001", "note", "x", scope="task")["value"] == "x"  # task needs no evidence


def _listed(server):
    import asyncio

    return sorted(t.name for t in asyncio.run(server.list_tools()))


def test_a_read_only_server_lists_four_tools_and_refuses_annotate():
    """`build_server(read_only=True)` (keel serve --read-only) serves the four read tools: an agent that never writes
    does not pay for annotate's definition on every request (evals/jira/context_tokens.md). A call to annotate is
    refused, saying why. The default still lists all five."""
    pytest.importorskip("mcp")
    import asyncio

    from keel.mcp_server import build_server

    c, s = _fleet_with_agents()
    c.agents = {}
    t = Tools(c, s, TENANT)
    assert _listed(build_server(t)) == ["annotate", "context", "history", "related", "search"]
    server = build_server(t, read_only=True)
    assert _listed(server) == ["context", "history", "related", "search"]
    with pytest.raises(Exception, match="read-only"):
        asyncio.run(server.call_tool("annotate", {"subject": "tire:T-9001", "predicate": "note", "value": "x"}))
    assert [f for f in s.facts(TENANT, "tire:T-9001") if f.predicate == "note"] == []


def test_an_agent_that_may_write_no_scope_gets_the_read_tools_only():
    """The registry says it plainly with `writes: []`: such an agent is served read-only without a flag. An agent that
    may write some scope (the default [task, agent]) keeps annotate listed."""
    pytest.importorskip("mcp")
    from keel.mcp_server import build_server

    c, s = _fleet_with_agents()
    c.agents = parse_agents(AGENTS | {"reporter": {"description": "Reads only.", "writes": []}})
    assert _listed(build_server(Tools(c, s, TENANT, agent="reporter"))) == ["context", "history", "related", "search"]
    assert "annotate" in _listed(build_server(Tools(c, s, TENANT, agent="tires-bot")))


# ---- stored agents (registry: store): per tenant, each extends a cartridge profile and only narrows it ----

PROFILES = {
    "team-member": {"writes": ["task", "agent"], "reads": {"kinds": ["unit", "tire", "inspection"]}},
    "lead": {"writes": ["task", "agent", "tenant"], "query": True},
    "approver": {"kind": "human", "writes": ["task", "tenant"]},
}


def _fleet_with_profiles():
    c, s = _fleet_with_agents()
    c.agents, c.agent_registry = parse_agents(PROFILES), "store"
    return c, s


def test_a_stored_agent_inherits_its_profile_and_may_only_narrow_it():
    from keel.agents import put_agent

    c, s = _fleet_with_profiles()
    put_agent(c, s, TENANT, "writer-7f3a", {"extends": "team-member", "description": "Writes scripts", "reads": {"kinds": ["tire"]}})
    t = Tools(c, s, TENANT, agent="writer-7f3a")
    assert (t.agent.writes, t.agent.kinds, t.agent.description) == (frozenset({"task", "agent"}), frozenset({"tire"}), "Writes scripts")
    with pytest.raises(PermissionError):
        t.context("unit:MXK-4821")  # its profile reads units; it narrowed that away
    assert t.annotate("tire:T-9001", "note", "x", scope="task", task="t1")["source"] == "annotate:writer-7f3a"
    put_agent(c, s, TENANT, "ana", {"extends": "approver"})
    assert Tools(c, s, TENANT, agent="ana").author == "human:ana"  # a person, from a human profile
    for bad, why in [
        ({"description": "no profile"}, "extends"),
        ({"extends": "nobody"}, "profile 'nobody'"),
        ({"extends": "team-member", "writes": ["tenant"]}, "widens"),
        ({"extends": "team-member", "reads": {"kinds": ["ticket"]}}, "widens"),
        ({"extends": "team-member", "reads": {"kinds": ["spaceship"]}}, "undeclared kind 'spaceship'"),
        ({"extends": "team-member", "query": True}, "widens"),
        ({"extends": "team-member", "kind": "human"}, "kind"),
    ]:
        with pytest.raises(ValueError, match=why):
            put_agent(c, s, TENANT, "x", bad)
    for bad_id in ("lead", "human:x", ""):  # a profile id would be shadowed; a ':' signs as someone else
        with pytest.raises(ValueError):
            put_agent(c, s, TENANT, bad_id, {"extends": "lead"})


def test_stored_agents_resolve_per_tenant_and_apply_without_a_restart():
    from keel.agents import put_agent, remove_agent

    c, s = _fleet_with_profiles()
    put_agent(c, s, TENANT, "bot", {"extends": "lead"})
    assert Tools(c, s, TENANT, agent="bot").agent.query
    with pytest.raises(PermissionError):
        Tools(c, s, "other-tenant", agent="bot")  # another tenant's agent is not this tenant's
    put_agent(c, s, TENANT, "bot", {"extends": "lead", "query": False})
    assert not Tools(c, s, TENANT, agent="bot").agent.query  # this process sees an edit at once
    assert remove_agent(s, TENANT, "bot") and not remove_agent(s, TENANT, "bot")
    with pytest.raises(PermissionError):
        Tools(c, s, TENANT, agent="bot")
    assert Tools(c, s, TENANT, agent="lead").agent.id == "lead"  # a cartridge entry binds as before


def test_without_registry_store_stored_agents_are_ignored():
    from keel.agents import put_agent

    c, s = _fleet_with_profiles()
    put_agent(c, s, TENANT, "bot", {"extends": "lead"})
    c.agent_registry = None
    with pytest.raises(PermissionError):
        Tools(c, s, TENANT, agent="bot")  # 0.36 behaviour: only the cartridge's entries
    with pytest.raises(ValueError, match="registry: store"):
        put_agent(c, s, TENANT, "bot2", {"extends": "lead"})


def test_registry_store_is_validated_with_the_cartridge():
    c = load_cartridge(FLEET)
    c.agent_registry = "store"
    assert any("registry: store" in m for m in c.validate())  # no profiles to extend
    c.agent_registry = "elsewhere"
    assert any("registry" in m for m in c.validate())
    c.agents, c.agent_registry = parse_agents(PROFILES), "store"
    assert c.validate() == []


def test_stored_agents_over_http():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app
    from keel.keys import create_key

    c, s = _fleet_with_profiles()
    acme, _ = create_key(s, "acme")
    bot_key, _ = create_key(s, "acme", agent="lead")
    client = TestClient(build_http_app(c, s, secret="s3cret"))
    h = {"Authorization": f"Bearer {acme}", "X-Keel-Tenant": "c1"}
    res = client.put("/v1/agents/writer", json={"extends": "team-member", "description": "Writes"}, headers=h)
    assert res.status_code == 200 and res.json() == {"id": "writer", "extends": "team-member", "description": "Writes"}
    assert s.agent("acme/c1", "writer")  # under the account, as every tenant
    assert client.put("/v1/agents/x", json={"extends": "nobody"}, headers=h).status_code == 422
    assert client.get("/v1/agents", headers=h).json() == [{"id": "writer", "extends": "team-member", "description": "Writes"}]
    assert client.get("/v1/agents/writer", headers=h).json()["extends"] == "team-member"
    assert client.get("/v1/agents/writer", headers={**h, "X-Keel-Tenant": "c2"}).status_code == 404
    res = client.post("/v1/search", json={"text": "T-9001"}, headers={**h, "X-Keel-Agent": "writer"})
    assert res.status_code == 200  # the stored agent calls the tools
    assert client.get("/v1/agents", headers={"Authorization": f"Bearer {bot_key}", "X-Keel-Tenant": "c1"}).status_code == 403
    assert client.delete("/v1/agents/writer", headers=h).status_code == 200
    assert client.delete("/v1/agents/writer", headers=h).status_code == 404
    c.agent_registry = None
    off = TestClient(build_http_app(c, s, secret="s3cret"))
    assert off.get("/v1/agents", headers={"Authorization": "Bearer s3cret", "X-Keel-Tenant": "c1"}).status_code == 404
