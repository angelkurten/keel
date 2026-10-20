"""Jira handlers: each one maps a payload to store primitives. Nothing else."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


def _fact(store, event, predicate, value):
    # recorded_at from the event, so "what was known at T" follows the log, not the ingest clock.
    # A null value (unassigned, unresolved) is a fact too: close_fact would stamp the ingest clock.
    store.open_fact(event.tenant, event.subject, predicate, value, valid_from=event.occurred_at,
                    recorded_at=event.recorded_at, source=event.source, evidence=event.id)


@registry.on("issue.created")
def issue_created(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "issue", event.subject_id, ref=f"{event.source}#{event.subject_id}", summary=p["summary"], reporter=p["reporter"])
    store.upsert_entity(event.tenant, "project", p["project"], ref=f"{event.source}#{p['project']}")
    store.link(event.tenant, event.subject, "in_project", f"project:{p['project']}", event.occurred_at, source=event.source)
    if p["parent"]:
        store.link(event.tenant, event.subject, "subtask_of", f"issue:{p['parent']}", event.occurred_at, source=event.source)
    for k in ("status", "status_category", "priority", "assignee", "resolution", "issue_type"):
        _fact(store, event, k, p[k])


@registry.on("issue.status_changed")
def status_changed(event, store):
    _fact(store, event, "status", event.payload["status"])
    _fact(store, event, "status_category", event.payload["status_category"])


@registry.on("issue.assigned")
def assigned(event, store):
    _fact(store, event, "assignee", event.payload["assignee"])


@registry.on("issue.priority_changed")
def priority_changed(event, store):
    _fact(store, event, "priority", event.payload["priority"])


@registry.on("issue.resolution_changed")
def resolution_changed(event, store):
    _fact(store, event, "resolution", event.payload["resolution"])


@registry.on("issue.type_changed")
def type_changed(event, store):
    _fact(store, event, "issue_type", event.payload["issue_type"])


@registry.on("issue.linked")
def linked(event, store):
    store.link(event.tenant, event.subject, event.payload["relation"], f"issue:{event.payload['other']}", event.occurred_at, source=event.source)


@registry.on("issue.unlinked")
def unlinked(event, store):
    store.unlink(event.tenant, event.subject, event.payload["relation"], f"issue:{event.payload['other']}", event.occurred_at)
