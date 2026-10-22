"""TDBench handlers: a term opens its holder at Start and closes it at End (no End: still in office)."""

from datetime import datetime, timezone

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


def _term(event, store, **attrs):
    p = event.payload
    store.upsert_entity(event.tenant, event.subject.split(":", 1)[0], event.subject_id, ref=f"{event.source}#{event.subject_id}", **attrs)
    store.open_fact(event.tenant, event.subject, "holder", p["holder"], valid_from=event.occurred_at, source=event.source, evidence=event.id)
    if p["end"]:
        end = datetime.fromisoformat(p["end"]).replace(tzinfo=timezone.utc)
        store.close_fact(event.tenant, event.subject, "holder", valid_to=end, value=p["holder"])  # this term, not a later one


@registry.on("office.term")
def office_term(event, store):
    _term(event, store, country=event.payload["country"], role=event.payload["role"])


@registry.on("post.term")
def post_term(event, store):
    _term(event, store, organization=event.payload["organization"], org_type=event.payload["kind"], role=event.payload["role"])
