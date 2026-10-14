# Your first cartridge in 20 minutes

A cartridge teaches Keel your vertical. It is a folder with two files. The core never changes.

```
cartridges/clinics/
  cartridge.yaml
  handlers.py
```

## 1. Scaffold it (1 min)

```bash
uv run keel init cartridges/clinics
```

## 2. Declare the ontology (5 min)

List the kinds of things your product is about and how they relate. Use your product's own names.

```yaml
entities:
  patient:     { description: A person treated at the clinic., ref: patients.id }
  appointment: { description: A scheduled visit., ref: appointments.id }
  treatment:   { description: A procedure performed., ref: treatments.id }

relations:
  booked_by:   { from: appointment, to: patient }
  performed_in:{ from: treatment,   to: appointment }
```

`ref` is a pointer to your table. Keel stores pointers and meaning, never copies of your rows.

## 3. Catalog the events (5 min)

Write down the 10 to 20 things that happen in your product. Each has a subject kind, an owner team, and a payload shape. This list is the least technical part and the one that saves the most arguments later.

```yaml
events:
  appointment.booked:
    subject: patient
    owner: scheduling
    payload: { id: string, starts_at: datetime, treatment: string }
  appointment.no_show:
    subject: appointment
    owner: scheduling
    payload: {}
```

## 4. Write one handler per event (5 min)

A handler maps a payload to four primitives: `upsert_entity`, `link`, `open_fact`, `close_fact`. Ten lines each.

```python
from keel.handlers import HandlerRegistry
registry = HandlerRegistry()

@registry.on("appointment.booked")
def booked(event, store):
    p = event.payload
    appt = f"appointment:{p['id']}"
    store.upsert_entity(event.tenant, "appointment", p["id"], starts_at=p["starts_at"])
    store.link(event.tenant, appt, "booked_by", event.subject, event.occurred_at)
    store.open_fact(event.tenant, appt, "status", "booked", valid_from=event.occurred_at, evidence=event.id)

@registry.on("appointment.no_show")
def no_show(event, store):
    store.open_fact(event.tenant, event.subject, "status", "no_show", valid_from=event.occurred_at, evidence=event.id)
```

`open_fact` closes the previous value of the same predicate instead of overwriting it. That is what makes the graph bitemporal: you can ask what was true on any date.

## 5. Add your semantics (2 min)

Definitions your agents should never invent for themselves:

```yaml
rules:
  at_risk_patient:
    applies_to: patient
    definition: "A patient is at risk of churn after two no-shows in 90 days."
    no_shows: 2
    days: 90
```

The `context` tool returns the rules that apply to a subject's kind, so every agent reads the same definition.

## 6. Validate and run (2 min)

```bash
uv run keel validate cartridges/clinics
uv run keel ingest cartridges/clinics examples/events/clinics.jsonl
uv run keel context cartridges/clinics examples/events/clinics.jsonl patient:P-1
```

`validate` fails if an event has no handler, a handler has no event, or a relation uses an undeclared kind.

## 7. Point an agent at it

```bash
uv sync --extra mcp
uv run keel serve cartridges/clinics --tenant clinic-centro --events examples/events/clinics.jsonl
```

Any MCP client now sees `context`, `history`, `search`, `annotate`, `related`. Your second agent uses the same five tools; there is no second pipeline.

## The test that it worked

Did you edit anything under `keel/`? If yes, some domain leaked into the core. Open an issue; that is exactly the kind of thing we want to fix.
