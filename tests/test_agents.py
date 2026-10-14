"""Agent registry: identity, write scopes and read slices, enforced in Tools, never by the agent."""

import json
import shutil
import sys

import pytest

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter

FLEET, EVENTS, TENANT = "cartridges/fleet", "examples/events/fleet.jsonl", "acme-logistics"
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
            assert {r["rel"] for r in json.loads(res.content[0].text)["relations"]} == {"mounted_on", "has_inspection"}
            res = await session.call_tool("context", {"subject": "ticket:TK-3608"})
            is_error = getattr(res, "is_error", None) or getattr(res, "isError", None)  # mcp 2.x / 1.x
            assert is_error and "may not read kind 'ticket'" in res.content[0].text

    anyio.run(run)
