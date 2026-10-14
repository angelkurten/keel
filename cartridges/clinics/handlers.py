"""Clinics handlers: the two from docs/first-cartridge.md, plus one for treatments."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("appointment.booked")
def booked(event, store):
    p = event.payload
    appt = f"appointment:{p['id']}"
    store.upsert_entity(event.tenant, "patient", event.subject_id)
    store.upsert_entity(event.tenant, "appointment", p["id"], starts_at=p["starts_at"])
    store.link(event.tenant, appt, "booked_by", event.subject, event.occurred_at)
    store.open_fact(event.tenant, appt, "status", "booked", valid_from=event.occurred_at, evidence=event.id)


@registry.on("appointment.no_show")
def no_show(event, store):
    store.open_fact(event.tenant, event.subject, "status", "no_show", valid_from=event.occurred_at, evidence=event.id)


@registry.on("treatment.performed")
def performed(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "treatment", p["id"], ref=f"{event.source}#{p['id']}", code=p.get("code"))
    store.link(event.tenant, f"treatment:{p['id']}", "performed_in", event.subject, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, event.subject, "status", "done", valid_from=event.occurred_at, source=event.source, evidence=event.id)
