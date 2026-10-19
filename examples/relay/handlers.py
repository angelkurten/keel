"""Relay handlers: each one maps a payload to store primitives. Nothing else."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


def _fact(store, event, subject, predicate, value):
    store.open_fact(event.tenant, subject, predicate, value, valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("account.created")
def account_created(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "account", event.subject_id, ref=f"{event.source}#{event.subject_id}", name=p["name"])
    for k in ("plan", "seats", "region", "csm"):
        if p.get(k) is not None:
            _fact(store, event, event.subject, k, p[k])


@registry.on("contact.added")
def contact_added(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "contact", p["id"], ref=f"{event.source}#{p['id']}", name=p["name"], role=p["role"], email=p["email"], language=p["language"])
    store.link(event.tenant, event.subject, "has_contact", f"contact:{p['id']}", event.occurred_at, source=event.source)


@registry.on("subscription.changed")
def subscription_changed(event, store):
    _fact(store, event, event.subject, "plan", event.payload["plan"])
    _fact(store, event, event.subject, "seats", event.payload["seats"])


@registry.on("invoice.issued")
def invoice_issued(event, store):
    p = event.payload
    inv = f"invoice:{p['id']}"
    store.upsert_entity(event.tenant, "invoice", p["id"], ref=f"{event.source}#{p['id']}", period=p["period"], description=p["description"], prorated=p["prorated"])
    store.link(event.tenant, event.subject, "has_invoice", inv, event.occurred_at, source=event.source)
    _fact(store, event, inv, "amount", p["amount"])
    _fact(store, event, inv, "status", "issued")


@registry.on("invoice.paid")
def invoice_paid(event, store):
    _fact(store, event, event.subject, "status", "paid")


@registry.on("payment.failed")
def payment_failed(event, store):
    _fact(store, event, event.subject, "status", "failed")
    _fact(store, event, event.subject, "failure_reason", event.payload["reason"])


@registry.on("refund.issued")
def refund_issued(event, store):
    _fact(store, event, event.subject, "status", "refunded")
    _fact(store, event, event.subject, "refunded_amount", event.payload["amount"])


@registry.on("ticket.opened")
def ticket_opened(event, store):
    p = event.payload
    tkt = f"ticket:{p['id']}"
    store.upsert_entity(event.tenant, "ticket", p["id"], ref=f"{event.source}#{p['id']}", title=p["title"], category=p["category"])
    store.link(event.tenant, event.subject, "has_ticket", tkt, event.occurred_at, source=event.source)
    _fact(store, event, tkt, "status", "open")
    _fact(store, event, tkt, "priority", p["priority"])


@registry.on("ticket.status_changed")
def ticket_status_changed(event, store):
    _fact(store, event, event.subject, "status", event.payload["status"])
    if event.payload.get("note"):
        _fact(store, event, event.subject, "last_note", event.payload["note"])


@registry.on("incident.affected")
def incident_affected(event, store):
    p = event.payload
    inc = f"incident:{p['id']}"
    store.upsert_entity(event.tenant, "incident", p["id"], ref=f"{event.source}#{p['id']}", title=p["title"], region=p["region"])
    store.link(event.tenant, event.subject, "affected_by", inc, event.occurred_at, source=event.source)
    _fact(store, event, inc, "severity", p["severity"])
    _fact(store, event, inc, "status", "ongoing")


@registry.on("incident.resolved")
def incident_resolved(event, store):
    _fact(store, event, event.subject, "status", "resolved")
    _fact(store, event, event.subject, "summary", event.payload["summary"])


@registry.on("integration.status_changed")
def integration_status_changed(event, store):
    p = event.payload
    integ = f"integration:{p['id']}"
    if store.entity(event.tenant, integ) is None:
        store.upsert_entity(event.tenant, "integration", p["id"], ref=f"{event.source}#{p['id']}", app=p["app"])
        store.link(event.tenant, event.subject, "uses", integ, event.occurred_at, source=event.source)
    _fact(store, event, integ, "status", p["status"])
    if p.get("error"):
        _fact(store, event, integ, "last_error", p["error"])
