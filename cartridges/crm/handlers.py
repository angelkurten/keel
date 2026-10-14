from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("customer.created")
def customer_created(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "business", event.subject_id)
    store.upsert_entity(event.tenant, "customer", p["id"], ref=f"{event.source}#{p['id']}", name=p.get("name"), phone=p.get("phone"))
    store.link(event.tenant, f"customer:{p['id']}", "customer_of", event.subject, event.occurred_at, source=event.source)


@registry.on("appointment.booked")
def appointment_booked(event, store):
    p = event.payload
    appt = f"appointment:{p['id']}"
    store.upsert_entity(event.tenant, "appointment", p["id"], service=p.get("service"), starts_at=p.get("starts_at"))
    store.link(event.tenant, appt, "booked_by", event.subject, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, appt, "status", "booked", valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.open_fact(event.tenant, event.subject, "last_appointment", p.get("starts_at"), valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("appointment.status_changed")
def appointment_status(event, store):
    store.open_fact(event.tenant, event.subject, "status", event.payload["status"], valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("payment.received")
def payment_received(event, store):
    p = event.payload
    pay = f"payment:{p['id']}"
    store.upsert_entity(event.tenant, "payment", p["id"], amount=p["amount"], currency=p.get("currency"))
    store.link(event.tenant, pay, "pays_for", event.subject, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, event.subject, "paid", True, valid_from=event.occurred_at, source=event.source, evidence=event.id)
