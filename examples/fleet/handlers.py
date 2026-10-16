"""Fleet handlers: each one maps a payload to store primitives. Nothing else."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("unit.registered")
def unit_registered(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "unit", p["id"], ref=f"{event.source}#{p['id']}", plate=p.get("plate"), model=p.get("model"))


@registry.on("driver.assigned")
def driver_assigned(event, store):
    drv = f"driver:{event.payload['driver_id']}"
    store.upsert_entity(event.tenant, "driver", event.payload["driver_id"])
    # close any previous assignment for this unit, then open the new one
    for r in store.relations(event.tenant, event.subject, rel="assigned_to", at=event.occurred_at):
        store.unlink(event.tenant, r.src, "assigned_to", r.dst, valid_to=event.occurred_at)
    store.link(event.tenant, drv, "assigned_to", event.subject, event.occurred_at, source=event.source)


@registry.on("inspection.created")
def inspection_created(event, store):
    p = event.payload
    insp = f"inspection:{p['id']}"
    store.upsert_entity(event.tenant, "inspection", p["id"], ref=f"{event.source}#{p['id']}", checklist=p.get("checklist"), photos=p.get("photos"))
    store.link(event.tenant, event.subject, "has_inspection", insp, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, insp, "damages", p.get("damages", 0), valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("inspection.damage_marked")
def damage_marked(event, store):
    p = event.payload
    store.open_fact(event.tenant, event.subject, f"damage:{p['damage_id']}", {"kind": p["kind"], "severity": p["severity"], "photo": p.get("photo")}, valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("tire.depth_measured")
def tire_depth(event, store):
    p = event.payload
    tire_id = event.subject_id
    store.upsert_entity(event.tenant, "tire", tire_id, position=p["position"])
    store.link(event.tenant, event.subject, "mounted_on", f"unit:{p['unit_id']}", event.occurred_at, source=event.source)
    store.open_fact(event.tenant, event.subject, "depth_mm", p["depth_mm"], valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("ticket.opened")
def ticket_opened(event, store):
    p = event.payload
    tkt = f"ticket:{p['id']}"
    store.upsert_entity(event.tenant, "ticket", p["id"], ref=f"{event.source}#{p['id']}", category=p.get("kind"))
    store.link(event.tenant, event.subject, "has_ticket", tkt, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, tkt, "status", "open", valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.open_fact(event.tenant, tkt, "urgency", p.get("urgency"), valid_from=event.occurred_at, source=event.source, evidence=event.id)
    if p.get("inspection_id"):
        store.link(event.tenant, tkt, "found_in", f"inspection:{p['inspection_id']}", event.occurred_at, source=event.source)


@registry.on("ticket.status_changed")
def ticket_status(event, store):
    p = event.payload
    store.open_fact(event.tenant, event.subject, "status", p["status"], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    if p.get("workshop_id"):
        store.upsert_entity(event.tenant, "workshop", p["workshop_id"])
        # the unit that owns this ticket goes into the workshop
        for r in store.relations(event.tenant, event.subject, rel="has_ticket"):
            store.link(event.tenant, r.src, "in_workshop", f"workshop:{p['workshop_id']}", event.occurred_at, source=event.source)


@registry.on("document.expiry_set")
def document_expiry(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "document", event.subject_id, category=p["kind"])
    rel = "covers_driver" if p["covers"].startswith("driver:") else "covers_unit"
    store.link(event.tenant, event.subject, rel, p["covers"], event.occurred_at, source=event.source)
    store.open_fact(event.tenant, event.subject, "expires_on", p["expires_on"], valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("fuel.load_recorded")
def fuel_load(event, store):
    p = event.payload
    store.open_fact(event.tenant, event.subject, "last_fuel_load", {"liters": p["liters"], "odometer_km": p["odometer_km"], "price": p.get("price")}, valid_from=event.occurred_at, source=event.source, evidence=event.id)
