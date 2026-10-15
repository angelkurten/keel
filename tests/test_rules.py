"""Rules v2: composition, neighbours, case-insensitive and duration operators, symmetric relations, depth."""

from datetime import datetime, timezone

from keel import Ingestor, MemoryStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter
from keel.rules import Calendar, evaluate, problems
from keel.store.base import Fact

UTC = timezone.utc
T0 = datetime(2026, 10, 1, tzinfo=UTC)
AT = datetime(2026, 10, 14, 12, tzinfo=UTC)


class _Graph:
    def __init__(self, facts, edges=None, calendar=None):
        self._facts, self.edges, self.calendar = facts, edges or {}, calendar or Calendar()

    def facts(self, subject):
        return [f for f in self._facts.get(subject, []) if f.valid_to is None]

    def history(self, subject, predicate):
        return [f for f in self._facts.get(subject, []) if f.predicate == predicate]

    def neighbors(self, subject, rel, direction):
        return self.edges.get((subject, rel), [])


def _fact(subject, predicate, value, since=T0, until=None):
    return Fact(tenant="t", subject=subject, predicate=predicate, value=value, valid_from=since, valid_to=until)


def test_composition_null_propagation_and_case_insensitive_strings():
    g = _Graph({"card:1": [_fact("card:1", "estado", "On Hold")]})
    hold = {"predicate": "estado", "in": ["on hold", "bloqueada"], "ci": True}
    assert evaluate({"when": hold}, "card:1", AT, g) is True
    assert evaluate({"when": {"predicate": "estado", "in": ["on hold"]}}, "card:1", AT, g) is False  # exact by default
    assert evaluate({"when": {"not": hold}}, "card:1", AT, g) is False
    unknown = {"predicate": "prioridad", "eq": "alta"}
    assert evaluate({"when": {"all": [hold, unknown]}}, "card:1", AT, g) is None
    assert evaluate({"when": {"any": [hold, unknown]}}, "card:1", AT, g) is True
    assert evaluate({"when": {"all": [{"predicate": "estado", "eq": "Done"}, unknown]}}, "card:1", AT, g) is False
    assert evaluate({"when": {"any": [{"predicate": "estado", "eq": "Done"}, unknown]}}, "card:1", AT, g) is None


def test_via_follows_relations_the_inherited_pause():
    g = _Graph(
        {"card:bi": [_fact("card:bi", "estado", "Pendiente")], "card:cus": [_fact("card:cus", "estado", "On Hold")]},
        edges={("card:bi", "relaciona"): ["card:cus"]},
    )
    hold = {"predicate": "estado", "in": ["On Hold"]}
    paused = {"any": [hold, {"via": "relaciona", "any": hold}]}
    assert evaluate({"when": paused}, "card:bi", AT, g) is True  # paused because its linked card is
    assert evaluate({"when": {"via": "relaciona", "all": {"predicate": "estado", "eq": "Done"}}}, "card:bi", AT, g) is False
    assert evaluate({"when": {"via": "relaciona", "any": hold}}, "card:cus", AT, g) is False  # no neighbours
    assert evaluate({"when": {"via": "relaciona", "all": hold}}, "card:cus", AT, g) is True


def test_durations_in_days_and_business_days():
    g = _Graph(
        {"t:1": [_fact("t:1", "status", "open", since=datetime(2026, 10, 2, tzinfo=UTC))]},  # a Friday
        calendar=Calendar.parse({"holidays": ["2026-10-12"]}),
    )
    assert evaluate({"when": {"predicate": "status", "open_for_days": {"gte": 12}}}, "t:1", AT, g) is True
    assert evaluate({"when": {"predicate": "status", "open_for_days": {"lt": 12}}}, "t:1", AT, g) is False
    # Oct 2 (Fri), Oct 5 to 9, Oct 13 (the 12th is a holiday): seven business days before Oct 14
    assert evaluate({"when": {"predicate": "status", "open_for_business_days": {"eq": 7}}}, "t:1", AT, g) is True
    assert Calendar.parse({"weekend": ["friday", "saturday"]}).business_days(datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)) == 1


def test_validation_messages_for_the_grammar():
    rels = {"relaciona"}
    assert problems("r", {"when": {"via": "nope", "any": {"predicate": "x", "eq": 1}}}, rels) == ["rule 'r': `via` names undeclared relation 'nope'"]
    assert any("takes a mapping" in m for m in problems("r", {"when": {"predicate": "x", "open_for_days": 3}}, rels))
    assert any("unknown operator 'between'" in m for m in problems("r", {"when": {"predicate": "x", "between": [1, 2]}}, rels))
    assert any("needs a `predicate`" in m for m in problems("r", {"when": {"eq": 1}}, rels))
    assert any("exactly one of `any` or `all`" in m for m in problems("r", {"when": {"via": "relaciona"}}, rels))
    assert problems("r", {"when": {"any": [{"predicate": "x", "eq": 1}, {"not": {"predicate": "y", "ci": True, "in": ["a"]}}]}}, rels) == []
    assert load_cartridge("cartridges/fleet").validate() == []


def test_fleet_rules_follow_tires_and_count_days_and_context_brings_neighbours():
    c = load_cartridge("cartridges/fleet")
    s = MemoryStore()
    Ingestor(c, s).ingest(JsonlAdapter("examples/events/fleet.jsonl"), strict=True)
    t = Tools(c, s, "acme-logistics")
    assert t.context("unit:MXK-4821")["evaluation"] == {"unit_grounded": True}  # via the tire at 2.6 mm
    assert t.context("unit:MXK-4821", as_of=datetime(2026, 9, 20, tzinfo=UTC))["evaluation"] == {"unit_grounded": False}
    assert t.context("ticket:TK-3608", as_of=datetime(2026, 10, 25, tzinfo=UTC))["evaluation"]["ticket_stale"] is True
    assert t.context("ticket:TK-3608", as_of=datetime(2026, 10, 14, tzinfo=UTC))["evaluation"]["ticket_stale"] is False
    deep = t.context("unit:MXK-4821", depth=1)
    tire = next(n for n in deep["neighbors"] if n["subject"] == "tire:T-9001")
    assert tire["relation"] == "mounted_on" and tire["direction"] == "in" and tire["evaluation"] == {"tire_critical": True}
    assert [f["predicate"] for f in tire["facts"]] == ["depth_mm"] and "neighbors_truncated" not in deep
    assert t.related("unit:MXK-4821", "mounted_on", as_of=datetime(2026, 9, 5, tzinfo=UTC)) == []  # not mounted yet


def test_symmetric_relations_show_once_in_both_directions():
    c = load_cartridge("cartridges/fleet")
    c.relations["pairs_with"] = {"from": "unit", "to": "unit", "symmetric": True}
    s = MemoryStore()
    for u in ("A", "B"):
        s.upsert_entity("acme-logistics", "unit", u)
    s.link("acme-logistics", "unit:A", "pairs_with", "unit:B", T0)
    s.link("acme-logistics", "unit:B", "pairs_with", "unit:A", T0)  # the source records it from both ends
    t = Tools(c, s, "acme-logistics")
    assert t.related("unit:A") == [{"relation": "pairs_with", "direction": "both", "subject": "unit:B", "since": T0.isoformat()}]
    assert t.related("unit:B")[0]["subject"] == "unit:A" and len(t.context("unit:A")["relations"]) == 1
    assert t.context("unit:B", detail="concise")["relations"] == [{"relation": "pairs_with", "direction": "both", "subject": "unit:A"}]
    assert t.context("unit:A", depth=1)["neighbors"][0]["direction"] == "both"


def test_held_for_counts_across_consecutive_waiting_states():
    sep20, oct1, oct8 = (datetime(2026, m, d, tzinfo=UTC) for m, d in ((9, 20), (10, 1), (10, 8)))
    g = _Graph({"card:1": [
        _fact("card:1", "estado", "To Do", since=sep20, until=oct1),
        _fact("card:1", "estado", "On Hold", since=oct1, until=oct8),
        _fact("card:1", "estado", "Customer | On-Hold", since=oct8),
    ]})
    waiting = {"predicate": "estado", "in": ["on hold", "customer | on-hold"], "ci": True}
    assert evaluate({"when": {**waiting, "open_for_days": {"gte": 13}}}, "card:1", AT, g) is False  # the clock restarted on Oct 8
    assert evaluate({"when": {**waiting, "held_for_days": {"gte": 13}}}, "card:1", AT, g) is True  # waiting since Oct 1
    assert evaluate({"when": {**waiting, "held_for_days": {"gte": 25}}}, "card:1", AT, g) is False  # To Do does not count
    assert evaluate({"when": {**waiting, "held_for_business_days": {"eq": 9}}}, "card:1", AT, g) is True  # Oct 1 to 13, weekdays
    assert evaluate({"when": {"predicate": "estado", "held_for_days": {"gte": 20}}}, "card:1", AT, g) is True  # no comparison: any value
    assert evaluate({"when": {"predicate": "estado", "eq": "Done", "held_for_days": {"gte": 1}}}, "card:1", AT, g) is False
