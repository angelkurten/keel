"""The product's own API, as the baseline agent's tools: Relay's records in their current state, rebuilt from the same
events Keel ingests (examples/relay/events.jsonl), the way a SaaS's REST API would return them. Ticket activity and
subscription changes are listed, as such APIs usually do; business rules (SLA, refunds) are not evaluated: an agent
without Keel applies the help center's policies itself."""

from collections import defaultdict
from typing import Any

from showcase.data import events

TOOLS = [
    {"type": "function", "function": {"name": "get_account", "description": "The customer's account: plan, seats, region, CSM, sign-up date.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_subscription_changes", "description": "The customer's plan and seat changes, oldest first.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_invoices", "description": "The customer's invoices, newest first: id, period, amount, status, issued and paid dates.",
     "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["issued", "paid", "failed", "refunded"]}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "list_tickets", "description": "The customer's support tickets, newest first: id, title, priority, category, status, opened and updated dates.",
     "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "pending", "resolved"]}, "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                                                      "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "get_ticket", "description": "One ticket with its activity: every status change with its note and time.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "list_incidents", "description": "Platform incidents that affected the customer: id, title, severity, region, start and end.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_integrations", "description": "The customer's connected apps: status, last error, last change.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_contacts", "description": "The customer's contacts: name, role, email, language.",
     "parameters": {"type": "object", "properties": {}}}},
]


class Product:
    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.changes: dict[str, list] = defaultdict(list)
        self.invoices: dict[str, dict[str, dict]] = defaultdict(dict)
        self.tickets: dict[str, dict[str, dict]] = defaultdict(dict)
        self.incidents: dict[str, dict[str, dict]] = defaultdict(dict)
        self.integrations: dict[str, dict[str, dict]] = defaultdict(dict)
        self.contacts: dict[str, list] = defaultdict(list)
        for e in events():
            self._apply(e)

    def _apply(self, e: dict) -> None:
        t, p, at, sid = e["tenant"], e["payload"], e["occurred_at"], e["subject"].split(":", 1)[1]
        match e["type"]:
            case "account.created":
                self.accounts[t] = {"name": p["name"], "plan": p["plan"], "seats": p["seats"], "region": p["region"], "csm": p.get("csm"), "created_at": at}
            case "subscription.changed":
                self.accounts[t].update(plan=p["plan"], seats=p["seats"])
                self.changes[t].append({"at": at, "from": p.get("previous_plan"), "to": p["plan"], "seats": p["seats"]})
            case "contact.added":
                self.contacts[t].append({k: p[k] for k in ("name", "role", "email", "language")})
            case "invoice.issued":
                self.invoices[t][p["id"]] = {"id": p["id"], "period": p["period"], "amount": p["amount"], "description": p["description"], "prorated": p["prorated"],
                                             "status": "issued", "issued_at": at, "paid_at": None}
            case "invoice.paid":
                self.invoices[t][sid].update(status="paid", paid_at=at)
            case "payment.failed":
                self.invoices[t][sid].update(status="failed", failure_reason=p["reason"])
            case "refund.issued":
                self.invoices[t][sid].update(status="refunded", refunded_at=at)
            case "ticket.opened":
                self.tickets[t][p["id"]] = {"id": p["id"], "title": p["title"], "priority": p["priority"], "category": p["category"], "status": "open",
                                            "opened_at": at, "updated_at": at, "activity": [{"at": at, "status": "open", "note": None}]}
            case "ticket.status_changed":
                tk = self.tickets[t][sid]
                tk.update(status=p["status"], updated_at=at)
                tk["activity"].append({"at": at, "status": p["status"], "note": p.get("note")})
            case "incident.affected":
                self.incidents[t][p["id"]] = {"id": p["id"], "title": p["title"], "severity": p["severity"], "region": p["region"], "started_at": at, "resolved_at": None}
            case "incident.resolved":
                self.incidents[t][sid].update(resolved_at=at, summary=p["summary"])
            case "integration.status_changed":
                self.integrations[t][p["id"]] = {"app": p["app"], "status": p["status"], "last_error": p.get("error"), "changed_at": at}

    def call(self, tenant: str, tool: str, args: dict[str, Any]) -> Any:
        limit = max(1, min(int(args.get("limit") or 20), 100))
        match tool:
            case "get_account":
                return self.accounts[tenant]
            case "list_subscription_changes":
                return self.changes[tenant]
            case "list_invoices":
                rows = sorted(self.invoices[tenant].values(), key=lambda i: i["issued_at"], reverse=True)
                return [i for i in rows if not args.get("status") or i["status"] == args["status"]][:limit]
            case "list_tickets":
                rows = sorted(self.tickets[tenant].values(), key=lambda x: x["opened_at"], reverse=True)
                rows = [x for x in rows if (not args.get("status") or x["status"] == args["status"]) and (not args.get("priority") or x["priority"] == args["priority"])]
                return [{k: v for k, v in x.items() if k != "activity"} for x in rows[:limit]]
            case "get_ticket":
                tk = self.tickets[tenant].get(str(args.get("id", "")))
                if tk is None:
                    raise LookupError(f"no ticket {args.get('id')} for this customer")
                return tk
            case "list_incidents":
                return sorted(self.incidents[tenant].values(), key=lambda i: i["started_at"], reverse=True)
            case "list_integrations":
                return list(self.integrations[tenant].values())
            case "list_contacts":
                return self.contacts[tenant]
        raise LookupError(f"unknown tool {tool}")
