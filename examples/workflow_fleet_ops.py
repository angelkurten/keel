"""A complex agentic workflow on Keel, with no LLM, to see where the context layer helps and where it hurts.

Run:  uv run python examples/workflow_fleet_ops.py

The flow (one tenant, five agents with different permissions, a human, and a verifier loop),
all writes tagged with the run's task id and an idempotency key so a replayed step is harmless:

    ingest -> triage (search(kind=unit), depth=1)
           -> tires-bot and compliance-bot in parallel threads (handoff through task memory)
           -> planner (reads every agent's task memory, resolves a conflict)
           -> verifier <-> planner loop until every grounded unit has a work order
           -> human promotes the plan to tenant memory with evidence
           -> audit: history with provenance, what the triage saw (as_known_at), changes since the run, export

Every agent is a Python function holding a `Tools` bound to its registry id; the registry decides
what it may read and write. The orchestration is a plain state machine. Written against 0.8, this
flow exposed six gaps; 0.9 closed them and the flow now uses each fix (see FINDINGS).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from keel import Event, Ingestor, SqliteStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.store.base import Store

TENANT = "acme-logistics"
NOW = datetime.now(timezone.utc).replace(microsecond=0)

AGENTS = {
    "triage-bot": {"description": "Ranks every unit each morning.", "writes": ["task"]},
    "tires-bot": {"description": "Tire work orders.", "writes": ["task"], "reads": {"kinds": ["unit", "tire", "inspection"], "relations": ["mounted_on", "has_inspection"]}},
    "compliance-bot": {"description": "Expiring documents.", "writes": ["task"], "reads": {"kinds": ["unit", "driver", "document"], "relations": ["covers_unit", "covers_driver", "assigned_to"]}},
    "planner-bot": {"description": "Turns findings into a plan.", "writes": ["task", "agent"]},
    "verifier-bot": {"description": "Checks the plan against the rules.", "writes": ["agent"]},
    "dispatcher": {"kind": "human", "description": "A person. Approves plans into tenant memory.", "writes": ["task", "tenant"]},
}


def stream() -> list[Event]:
    """Three units, dated relative to today so the rules' day windows mean something."""
    d = lambda days, hour=8: NOW - timedelta(days=days) + timedelta(hours=hour - NOW.hour)
    ev: list[Event] = []
    mk = lambda type, subject, when, source, **payload: ev.append(Event(tenant=TENANT, type=type, subject=subject, occurred_at=when, source=source, payload=payload))
    for i, plate in enumerate(["MXK-4821", "TRK-1102", "VAN-0073"]):
        mk("unit.registered", f"unit:{plate}", d(400), "fleet.vehicles", id=plate, plate=plate, model=["Cascadia", "Actros", "Sprinter"][i])
        mk("driver.assigned", f"unit:{plate}", d(300), "fleet.drivers", driver_id=f"D-{100 + i}")
        for pos in ("P1", "P2"):
            tire = f"T-{plate}-{pos}"
            mk("tire.depth_measured", f"tire:{tire}", d(60), "tires", unit_id=plate, position=pos, depth_mm=6.0)
            mk("tire.depth_measured", f"tire:{tire}", d(5), "tires", unit_id=plate, position=pos, depth_mm=2.4 if (i == 0 and pos == "P1") or (i == 2 and pos == "P2") else 4.8)
        mk("document.expiry_set", f"document:POL-{plate}", d(200), "compliance", kind="insurance_policy", covers=f"unit:{plate}", expires_on=(NOW + timedelta(days=[10, 120, 3][i])).date().isoformat())
    mk("ticket.opened", "unit:TRK-1102", d(20), "maintenance", id="TK-77", kind="corrective", urgency="medium", inspection_id=None)
    return ev


# ---- agents: each one is a function over a Tools bound to its registry id ----------------------


RUN = "fleet-ops-1"  # the task id: every write of this run carries it


def triage(t: Tools) -> tuple[dict[str, Any], str]:
    """Every unit, with its neighbours in one call, ranked by the cartridge's own verdicts. Returns
    the ranking and the time it decided, so an audit can ask what it saw."""
    ranked, seen_at = {}, ""
    for unit in [e["key"] for e in t.search(kind="unit")]:
        ctx = t.context(unit, depth=1)
        seen_at = ctx["as_of"]
        reasons = [k for k, v in ctx["evaluation"].items() if v]
        for n in ctx["neighbors"]:
            reasons += [f"{n['subject']}:{k}" for k, v in n["evaluation"].items() if v]
        priority = "high" if reasons else "normal"
        t.annotate(unit, "triage", {"priority": priority, "reasons": reasons}, scope="task", evidence=f"context@{ctx['as_of']}", task=RUN, key=f"{RUN}:triage:{unit}")
        ranked[unit] = priority
    return ranked, seen_at


def tires(t: Tools, units: list[str]) -> list[str]:
    """A partial specialist on purpose: it only looks at front tires, so the verifier has something to catch."""
    orders = []
    for unit in units:
        for n in t.context(unit, depth=1)["neighbors"]:
            if n["relation"] == "mounted_on" and n["evaluation"].get("tire_critical") and n["entity"]["attrs"].get("position") == "P1":
                depth = next(f for f in n["facts"] if f["predicate"] == "depth_mm")
                t.annotate(unit, "recommendation", {"by": "tires", "action": "replace_tire", "tire": n["subject"], "depth_mm": depth["value"]}, scope="task", evidence=depth["evidence"], task=RUN, key=f"{RUN}:tires:{n['subject']}")
                orders.append(n["subject"])
    return orders


def compliance(t: Tools, units: list[str]) -> list[str]:
    flagged = []
    for unit in units:
        for n in t.context(unit, depth=1)["neighbors"]:
            if n["relation"] == "covers_unit" and n["evaluation"].get("document_critical"):
                exp = next(f for f in n["facts"] if f["predicate"] == "expires_on")
                t.annotate(unit, "recommendation", {"by": "compliance", "action": "renew_document", "document": n["subject"], "expires_on": exp["value"]}, scope="task", evidence=exp["evidence"], task=RUN, key=f"{RUN}:compliance:{n['subject']}")
                flagged.append(n["subject"])
    return flagged


def plan(t: Tools, units: list[str], feedback: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Reads every agent's task memory on each unit; two agents may have written the same predicate."""
    out: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        recs = [f["value"] for f in t.history(unit, predicate="recommendation", task=RUN)]  # only this run's
        if feedback and unit in feedback:  # the verifier said this unit is grounded and unplanned
            recs.append({"by": "planner", "action": "ground_and_inspect"})
        out[unit] = recs
        t.annotate(unit, "plan", recs, scope="task", evidence="recommendation facts", task=RUN)
    t.annotate("unit:MXK-4821", "scratch", {"iteration": 1 if not feedback else 2}, scope="agent", task=RUN)  # private working state
    return out


def verify(t: Tools, plan_: dict[str, list[dict[str, Any]]]) -> str | None:
    """Evaluator: every grounded unit needs a work order. An aggregate the rules cannot express."""
    missing = [u for u in plan_ if t.context(u)["evaluation"].get("unit_grounded") and not any(r["action"] in ("replace_tire", "ground_and_inspect") for r in plan_[u])]
    return ", ".join(missing) if missing else None


# ---- the orchestration ---------------------------------------------------------------------------


def run(store: Store | None = None, quiet: bool = False) -> dict[str, Any]:
    c = load_cartridge("cartridges/fleet")
    c.agents = parse_agents(AGENTS)  # declared here instead of in cartridge.yaml so the reference cartridge stays open
    store = store or SqliteStore()
    events = stream()
    Ingestor(c, store).ingest(events, strict=True, batch=500)
    bots = {a: Tools(c, store, TENANT, agent=a) for a in AGENTS}
    log: list[str] = []
    say = (lambda *_: None) if quiet else print

    stale = Tools(c, store, TENANT, agent="planner-bot")  # yesterday's run left memory behind: a different task id
    stale.annotate("unit:MXK-4821", "recommendation", {"by": "tires", "action": "ignore_me"}, scope="task", task="fleet-ops-0")
    cursor = bots["triage-bot"].context("unit:MXK-4821")["watermark"]

    ranked, triage_seen_at = triage(bots["triage-bot"])
    ranked_again, _ = triage(bots["triage-bot"])  # a durable-execution engine replaying the step: the keys make it a no-op
    assert ranked_again == ranked
    units = sorted(ranked)
    say("triage:", ranked)

    with ThreadPoolExecutor() as pool:  # two specialists at once, on the same store
        f1 = pool.submit(tires, bots["tires-bot"], units)
        f2 = pool.submit(compliance, bots["compliance-bot"], units)
        orders, flagged = f1.result(), f2.result()
    say("tires-bot work orders:", orders, "| compliance flags:", flagged)

    try:  # a specialist stepping outside its slice is refused by the layer, not by convention
        bots["compliance-bot"].context("ticket:TK-77")
    except PermissionError as exc:
        log.append(str(exc))

    feedback, rounds = None, 0
    while True:  # evaluator-optimizer loop
        rounds += 1
        plan_ = plan(bots["planner-bot"], units, feedback)
        feedback = verify(bots["verifier-bot"], plan_)
        say(f"plan round {rounds}:", json.dumps(plan_, ensure_ascii=False)[:300], "| verifier:", feedback or "ok")
        if not feedback or rounds >= 3:
            break

    review_started = datetime.now(timezone.utc)  # the audit will ask what was known when the person started reviewing
    human = Tools(c, store, TENANT, agent="dispatcher")  # a person is a declared principal: signs as human:dispatcher
    promoted = {}
    for unit, recs in plan_.items():
        if recs:
            task_facts = [f["evidence"] for f in human.history(unit, predicate="recommendation", task=RUN)]
            promoted[unit] = human.annotate(unit, "approved_plan", recs, scope="tenant", evidence=";".join(task_facts) or "plan")["source"]
    changed = bots["planner-bot"].history(changed_since=datetime.fromisoformat(cursor))  # everything the run wrote
    closed = store.close_task(TENANT, RUN)  # the run is over: its task memory expires, the approved plans stay

    # audit: provenance of the conflict, what the triage saw when it decided, everything that changed during the run
    conflict = sorted((f["value"]["by"], f["source"]) for f in bots["planner-bot"].history("unit:MXK-4821", predicate="recommendation", as_known_at=review_started) if f["task"] == RUN)  # read back after the task closed
    seen_by_triage = bots["triage-bot"].context("unit:MXK-4821", as_known_at=datetime.fromisoformat(triage_seen_at))["evaluation"]
    sweep = [e["id"] for e in bots["tires-bot"].search(rule="tire_critical")]
    export = list(store.dump(TENANT))
    return {"ranked": ranked, "orders": orders, "flagged": flagged, "rounds": rounds, "promoted": promoted, "conflict": conflict,
            "refusals": log, "triage_saw": seen_by_triage, "task_facts_closed": closed, "changed_during_run": sorted({f["predicate"] for f in changed}),
            "critical_tires": sweep, "export_records": len(export)}


FINDINGS = """
What this flow taught about Keel (docs/design/agentic-workflows.md), and what 0.9 changed:
1. Listing subjects by kind needed search("unit:") plus a filter        -> search(kind=), search(rule=), limit/after
2. Task memory had no task id; yesterday's run leaked into today's plan  -> annotate(task=), history(task=), close_task
3. A human approving through an agent's console inherited its scopes    -> registry `kind: human`, signed human:<id>
4. "What did the agent see" was only valid time (as_of)                -> context/history(as_known_at=), closed_at
5. "What changed since my last run" was per subject                    -> history(changed_since=) and context's watermark
6. A replayed step would write twice                                    -> annotate(key=)
Still open: which of two agents' recommendations wins (a conflict policy per predicate), detail levels, trace ids.
"""

if __name__ == "__main__":
    report = run()
    print(json.dumps({k: v for k, v in report.items() if k != "export_records"}, ensure_ascii=False, indent=1, default=str)[:2500])
    print(FINDINGS)
