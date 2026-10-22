"""Handlers that run unchanged on every release since 0.1.0.

Releases before 0.33 did not stamp a declared `recorded_at` on what a handler writes, so open_fact passes
it explicitly when the event declares one (the current Ingestor does the same): that keeps the transaction
time of those facts part of the input, comparable across versions.
"""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


def _rec(event):
    return {"recorded_at": event.recorded_at} if "recorded_at" in event.model_fields_set else {}


@registry.on("site.opened")
def site_opened(event, store):
    store.upsert_entity(event.tenant, "site", event.subject_id, ref=f"{event.source}#{event.subject_id}", name=event.payload["name"])


@registry.on("machine.installed")
def machine_installed(event, store):
    p = event.payload
    m = f"machine:{p['id']}"
    store.upsert_entity(event.tenant, "machine", p["id"], ref=f"{event.source}#{p['id']}", model=p["model"])
    store.link(event.tenant, m, "located_at", event.subject, event.occurred_at, source=event.source)
    store.open_fact(event.tenant, m, "status", "idle", valid_from=event.occurred_at, source=event.source, evidence=event.id, **_rec(event))


@registry.on("operator.assigned")
def operator_assigned(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "operator", p["operator"], ref=f"{event.source}#{p['operator']}", name=p["name"])
    store.link(event.tenant, event.subject, "operated_by", f"operator:{p['operator']}", event.occurred_at, source=event.source)


@registry.on("operator.released")
def operator_released(event, store):
    store.unlink(event.tenant, event.subject, "operated_by", f"operator:{event.payload['operator']}", event.occurred_at)


@registry.on("machine.status_changed")
def status_changed(event, store):
    store.open_fact(event.tenant, event.subject, "status", event.payload["status"], valid_from=event.occurred_at,
                    source=event.source, evidence=event.id, **_rec(event))


@registry.on("machine.retired")
def machine_retired(event, store):
    store.close_fact(event.tenant, event.subject, "status", event.occurred_at)
