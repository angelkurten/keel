"""The showcase's data, as both sides see it: Relay's events (examples/relay/events.jsonl) and its help center.

The RAG side gets one text chunk per event, with its date and the customer's name: the same input Keel ingests,
written the way a team would index an event export. The help center is shared by both sides.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "examples" / "relay" / "events.jsonl"
HELP = Path(__file__).resolve().parent / "help"
TODAY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@dataclass
class Chunk:
    text: str
    tenant: str | None  # None: the help center, for every customer
    ref: str  # an event id or a help article


def events() -> list[dict]:
    return [json.loads(line) for line in EVENTS.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def customers() -> list[dict]:
    """[{tenant, name, plan}] with the plan as of TODAY."""
    out: dict[str, dict] = {}
    for e in events():
        if e["type"] == "account.created":
            out[e["tenant"]] = {"tenant": e["tenant"], "name": e["payload"]["name"], "plan": e["payload"]["plan"]}
        elif e["type"] == "subscription.changed":
            out[e["tenant"]]["plan"] = e["payload"]["plan"]
    return sorted(out.values(), key=lambda c: c["name"].lower())


def sentence(e: dict, name: str) -> str:
    p, when, sid = e["payload"], e["occurred_at"].replace("T", " ").replace("Z", " UTC"), e["subject"].split(":", 1)[1]
    t = e["type"]
    text = {
        "account.created": lambda: f"{name} signed up on the {p['plan']} plan with {p['seats']} seats, region {p['region']}" + (f", CSM {p['csm']}" if p.get("csm") else "") + ".",
        "contact.added": lambda: f"Contact {p['name']} ({p['role']}, {p['email']}, language {p['language']}) added for {name}.",
        "subscription.changed": lambda: f"{name} changed plan from {p.get('previous_plan')} to {p['plan']}, {p['seats']} seats.",
        "invoice.issued": lambda: f"Invoice {p['id']} issued to {name}: {p['amount']} {p['currency']} for {p['period']}, {p['description']}" + (" (prorated)." if p["prorated"] else "."),
        "invoice.paid": lambda: f"{name} paid invoice {sid}: {p['amount']} USD.",
        "payment.failed": lambda: f"Payment of invoice {sid} for {name} failed: {p['reason']}.",
        "refund.issued": lambda: f"Refund of {p['amount']} USD on invoice {sid} for {name}: {p['reason']}.",
        "ticket.opened": lambda: f"{name} opened ticket {p['id']} ({p['priority']} priority, {p['category']}): {p['title']}.",
        "ticket.status_changed": lambda: f"Ticket {sid} of {name} set to {p['status']}" + (f": {p['note']}" if p.get("note") else "."),
        "incident.affected": lambda: f"Incident {p['id']} ({p['severity']}, region {p['region']}) started affecting {name}: {p['title']}.",
        "incident.resolved": lambda: f"Incident {sid} resolved for {name}: {p['summary']}",
        "integration.status_changed": lambda: f"{name}'s {p['app']} integration ({p['id']}) is {p['status']}" + (f": {p['error']}." if p.get("error") else "."),
    }[t]()
    return f"[{when}] {text}"


def chunks() -> list[Chunk]:
    names = {c["tenant"]: c["name"] for c in customers()}
    out = [Chunk(sentence(e, names[e["tenant"]]), e["tenant"], e["id"]) for e in events()]
    for article in sorted(HELP.glob("*.md")):
        for part in article.read_text(encoding="utf-8").split("\n\n"):
            if part.strip():
                out.append(Chunk(part.strip(), None, f"help/{article.stem}"))
    return out
