"""Finance handlers: each one maps a payload to store primitives. Nothing else."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("account.opened")
def account_opened(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "household", event.subject_id)
    store.upsert_entity(event.tenant, "account", p["id"], ref=f"{event.source}#{p['id']}", name=p.get("name"), type=p.get("kind"), currency=p.get("currency"))
    store.link(event.tenant, f"account:{p['id']}", "held_by", event.subject, event.occurred_at, source=event.source)


@registry.on("budget_line.planned")
def budget_line_planned(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "budget_line", event.subject_id, ref=f"{event.source}#{event.subject_id}", name=p.get("name"), group=p.get("group"))
    # a new plan closes the previous one; earlier months keep the plan that ruled them
    store.open_fact(event.tenant, event.subject, "planned_cop", p["amount_cop"], valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("debt.snapshot")
def debt_snapshot(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "debt", event.subject_id, ref=f"{event.source}#{event.subject_id}", creditor=p.get("creditor"), product=p.get("product"))
    if not store.relations(event.tenant, event.subject, rel="owed_by", at=event.occurred_at):
        store.link(event.tenant, event.subject, "owed_by", f"household:{event.tenant}", event.occurred_at, source=event.source)
    for predicate in ("balance_cop", "rate_ea_pct", "installment_cop"):
        store.open_fact(event.tenant, event.subject, predicate, p[predicate], valid_from=event.occurred_at, source=event.source, evidence=event.id)


@registry.on("due.scheduled")
def due_scheduled(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "due", event.subject_id, ref=f"{event.source}#{event.subject_id}", concept=p.get("concept"))
    store.open_fact(event.tenant, event.subject, "status", "pending", valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.open_fact(event.tenant, event.subject, "due_on", p["due_on"], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.open_fact(event.tenant, event.subject, "amount_cop", p["amount_cop"], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    if p.get("debt_id"):
        store.link(event.tenant, event.subject, "for_debt", f"debt:{p['debt_id']}", event.occurred_at, source=event.source)


@registry.on("due.paid")
def due_paid(event, store):
    p = event.payload
    store.open_fact(event.tenant, event.subject, "status", "paid", valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.link(event.tenant, event.subject, "paid_from", f"account:{p['account_id']}", event.occurred_at, source=event.source)


@registry.on("movement.imported")
def movement_imported(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "movement", event.subject_id, ref=f"{event.source}#{event.subject_id}", description=p.get("description"))
    store.open_fact(event.tenant, event.subject, "amount_cop", p["amount_cop"], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    store.link(event.tenant, event.subject, "in_account", f"account:{p['account_id']}", event.occurred_at, source=event.source)
    if p.get("line_id"):
        store.link(event.tenant, event.subject, "in_line", f"budget_line:{p['line_id']}", event.occurred_at, source=event.source)
