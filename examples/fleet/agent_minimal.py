"""A minimal 'agent' that uses the five tools with no LLM, to show the contract.

Run:  uv run python examples/fleet/agent_minimal.py

It ingests the fleet stream, asks for the context of one unit, applies the
cartridge's rules (which live in the graph, not in the agent), and writes
what it concluded back as task-scoped memory with evidence.
"""

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter

cartridge = load_cartridge("examples/fleet")
store = MemoryStore()
Ingestor(cartridge, store).ingest(JsonlAdapter("examples/fleet/events.jsonl"))
tools = Tools(cartridge, store, tenant="acme-logistics")

unit = "unit:MXK-4821"
ctx = tools.context(unit)
print(f"{unit}: {len(ctx['relations'])} relations, {len(ctx['facts'])} open facts")

# The agent neither defines nor computes 'critical': the rule and its `when` live in the
# cartridge, and `context` returns the verdict next to the rule.
for rel in tools.related(unit, "mounted_on"):
    tire = rel["subject"]
    tire_ctx = tools.context(tire)
    if tire_ctx["evaluation"].get("tire_critical"):
        threshold = tire_ctx["rules"]["tire_critical"]["threshold_mm"]
        depth = next(f for f in tire_ctx["facts"] if f["predicate"] == "depth_mm")
        note = tools.annotate(tire, "assessment", f"critical: {depth['value']} mm < {threshold} mm", scope="task", evidence=depth["evidence"], by="agent:tires-demo")
        print("wrote:", note["subject"], note["predicate"], note["value"], "expires", note["expires_at"])

# History shows the tire was fine in September and critical in October: facts close, they are not overwritten.
for f in tools.history("tire:T-9001"):
    print(f"  {f['predicate']}={f['value']} valid {f['valid_from'][:10]} -> {f['valid_to'][:10] if f['valid_to'] else 'now'}")
