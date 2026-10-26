"""docs/cartridge.schema.json: the JSON Schema editors use to validate and complete cartridge.yaml.

It must accept every cartridge the code accepts and reject the shapes `keel validate` rejects."""

import json
from pathlib import Path

import pytest
import yaml

from keel.cartridge import load_cartridge

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "cartridge.schema.json").read_text(encoding="utf-8"))
CARTRIDGES = sorted(ROOT.glob("examples/*/cartridge.yaml")) + sorted(ROOT.glob("cartridges/*/cartridge.yaml"))

# every section and operator the examples do not use, so the schema is checked against them too
FULL = """
name: everything
version: 0.1
entities:
  ticket:
    description: A support ticket.
    ref: tickets.id
    attrs: [title]
    facts:
      status: [open, closed]
      amount: { type: number | null, description: What the customer owes. }
      due_on: { description: When it is due. }
      note:
  customer:
    facts: [tier, joined]
relations:
  of_customer: { from: ticket, to: customer, one: true }
  relates_to: { from: ticket, to: ticket, symmetric: true }
events:
  ticket.opened:
    subject: ticket
    owner: support
    payload:
      id: string
      amount: number | null
      status: [open, closed]
      stage: [new, done, null]
      tags: string[]
      lines: [ { id: string, qty: int } ]
      meta: { source: string, score: number | null }
      at: datetime
  customer.erased: { subject: customer, forgets: tenant }
  ticket.retained: { subject: ticket, forgets: before }
rules:
  stale:
    applies_to: ticket
    definition: A ticket is stale after a week on hold.
    days: 7
    when:
      any:
        - { predicate: status, in: [On Hold, Blocked], ci: true, held_for_business_days: { gte: 7 } }
        - { not: { predicate: status, eq: closed } }
        - { via: relates_to, direction: out, all: { predicate: status, open_for_days: { gt: 1, lte: 9 } } }
        - { all: [ { predicate: due_on, within_days: 5 }, { predicate: amount, ne: 0 } ] }
  counted: { definition: A count over time stays a definition. }
predicates:
  assessment: { multi: true, description: One per author. }
semantic:
  kinds: [ticket]
  exclude: [comment]
  attrs: [title]
  relations: [of_customer]
  labels: [name]
  lexical_weight: 0.5
  min_score: 0.4
  split: { ticket: [status] }
consolidate:
  recurring:
    from: { predicate: assessment, scopes: [task, agent] }
    applies_to: ticket
    within: 30d
    min_count: 3
    reduce: summary
    into: { predicate: recurring_issue, scope: tenant }
calendar:
  weekend: [Saturday, sunday]
  holidays: [2026-12-25]
agents:
  triage:
    description: Reads tickets.
    writes: [task, agent, tenant]
    reads: { kinds: [ticket], relations: [relates_to] }
  lead: { kind: human, writes: [tenant] }
  anyone:
memory:
  ttl: { global: never, tenant: forever, task: 14d, agent: 12h }
  requires_evidence: [global, tenant]
"""


def as_json(text: str):
    """What an editor sees: YAML dates stay strings."""
    return json.loads(json.dumps(yaml.safe_load(text), default=str))


def errors(doc) -> list[str]:
    return [e.message for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc)]


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == "https://angelkurten.github.io/keel/cartridge.schema.json"


@pytest.mark.parametrize("path", CARTRIDGES, ids=lambda p: p.parent.name)
def test_every_cartridge_validates(path):
    assert errors(as_json(path.read_text(encoding="utf-8"))) == []


def test_every_section_validates_and_the_code_agrees(tmp_path):
    assert errors(as_json(FULL)) == []
    (tmp_path / "cartridge.yaml").write_text(FULL, encoding="utf-8")
    problems = load_cartridge(tmp_path, handlers=False).validate()
    assert problems == ["event 'ticket.opened' has no handler in handlers.py"]


DELETE = object()


@pytest.mark.parametrize(
    "path, value",
    [
        (("events", "ticket.opened"), {"owner": "support"}),  # no subject
        (("events", "customer.erased", "forgets"), "always"),
        (("relations", "of_customer", "to"), DELETE),
        (("memory", "ttl", "task"), "14x"),
        (("rules", "stale", "when"), {"predicate": "status", "older_than": 3}),  # unknown operator
        (("rules", "stale", "when"), {"predicate": "status", "open_for_days": 7}),  # takes a comparison
        (("rules", "stale", "when"), {"via": "relates_to", "direction": "up", "any": {"predicate": "s", "eq": 1}}),
        (("events", "ticket.opened", "payload", "id"), "strng"),
        (("events", "ticket.opened", "payload", "tags"), ["string"]),  # written string[]
        (("consolidate", "recurring", "reduce"), "median"),
        (("agents", "triage", "writes"), ["global"]),
        (("semantic", "predicates"), ["status"]),  # together with exclude
        (("entities", "ticket", "facts", "status"), "strng"),
        (("entities", "ticket", "facts", "amount"), {"type": "number", "unit": "usd"}),
        (("entities", "ticket", "facts"), "status"),
        (("entities", "ticket", "attrs"), "title"),
        (("relations", "of_customer", "one"), "yes"),
    ],
)
def test_shapes_validate_rejects_are_rejected(path, value):
    doc = as_json(FULL)
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    assert errors(doc)


@pytest.mark.parametrize(
    "path, value, problem",
    [
        (("entities", "ticket", "facts", "status"), "strng", "entity 'ticket': fact 'status': unknown payload hint 'strng'"),
        (("entities", "ticket", "facts", "amount"), {"type": "number", "unit": "usd"},
         "entity 'ticket': fact 'amount' is a payload hint or {type, description}"),
        (("entities", "ticket", "facts"), "status", "entity 'ticket': facts is a list of predicates or a mapping predicate -> hint"),
        (("entities", "ticket", "facts", "title"), None, "entity 'ticket': 'title' is both an attribute and a fact"),
        (("entities", "ticket", "facts", "of_customer"), None, "entity 'ticket': 'of_customer' is both a fact and a relation from it marked one"),
        (("entities", "ticket", "facts", "id"), None, "entity 'ticket': 'id' is a name the generated SQL uses; name it otherwise"),
        (("entities", "ticket", "facts", "status-2"), None, "entity 'ticket': fact 'status-2' is not a plain identifier"),
        (("entities", "ticket", "attrs"), "title", "entity 'ticket': attrs is a list of attribute names"),
        (("relations", "of_customer", "one"), "yes", "relation 'of_customer': one is true or false"),
        (("relations", "relates_to", "one"), True, "relation 'relates_to': a symmetric relation cannot be one"),
    ],
)
def test_declared_facts_and_one_are_checked_by_the_code(tmp_path, path, value, problem):
    doc = yaml.safe_load(FULL)
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    (tmp_path / "cartridge.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert any(p.startswith(problem) for p in load_cartridge(tmp_path, handlers=False).validate()), problem
