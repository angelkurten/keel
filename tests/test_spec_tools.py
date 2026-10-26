"""SPEC §5.4 and §7 as tests: rules, and the contracts of the five tools (context, history, search, related,
annotate), in every store and over the transports. Memory-scope policy is tested elsewhere."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from keel import Tools
from keel.cartridge import Cartridge
from keel.rules import evaluate
from keel.store.base import Fact

UTC = timezone.utc
T = "acme"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def d(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


RULES = {
    "blocked": {  # evals/jira's rule: not done, and an issue that blocks it is not done either
        "applies_to": "issue",
        "when": {"all": [{"not": {"predicate": "status_category", "eq": "done"}},
                         {"via": "blocks", "direction": "in", "any": {"not": {"predicate": "status_category", "eq": "done"}}}]},
    },
    "blocking": {"applies_to": "issue", "when": {"via": "blocks", "direction": "out", "any": {"predicate": "status_category", "ne": "done"}}},
    "stalled": {"applies_to": "issue", "when": {"predicate": "status_category", "eq": "indeterminate", "open_for_days": {"gte": 30}}},
    "waiting": {"applies_to": "issue", "when": {"predicate": "status", "in": ["on hold", "waiting"], "ci": True, "held_for_days": {"gte": 10}}},
    "urgent": {"applies_to": "issue", "when": [{"predicate": "priority", "eq": "high"}, {"not": {"predicate": "status_category", "eq": "done"}}]},
    "linked_open": {"applies_to": "issue", "when": {"via": "relates_to", "direction": "out", "any": {"predicate": "status_category", "ne": "done"}}},
    "velocity": {"applies_to": "issue", "definition": "Issues closed per week: the agent counts them."},
}


def cartridge() -> Cartridge:
    return Cartridge(
        name="spec", path=Path("."),
        entities={"issue": {"description": "a ticket"}, "project": {"description": "a project"}},
        relations={"blocks": {"from": "issue", "to": "issue"}, "relates_to": {"from": "issue", "to": "issue", "symmetric": True},
                   "in_project": {"from": "issue", "to": "project"}},
        events={}, rules=RULES,
    )




def fact(s, subject, predicate, value, since, **kw):
    kw.setdefault("recorded_at", since)  # known when it happened, unless a test says otherwise
    s.open_fact(T, subject, predicate, value, valid_from=since, source="test", evidence=f"ev:{subject}:{predicate}:{since.date()}", **kw)


def seed(s):
    """A small tracker. A blocks B, B blocks K, K blocks L, C (done) blocks D; A relates_to E (one end recorded),
    F relates_to G (recorded from both ends); H's status was learned late; W waited across two statuses."""
    for i, title in [("A", "Login fails"), ("B", "Release"), ("C", "Old bug"), ("D", "Docs"), ("E", "Empty"), ("F", "Pair one"),
                     ("G", "Pair two"), ("H", "Late"), ("K", "Chain"), ("L", "Chain end"), ("W", "Waiter")]:
        s.upsert_entity(T, "issue", i, ref=f"jira#{i}", title=title)
    s.upsert_entity(T, "project", "P", ref="jira#P", title="Platform")
    fact(s, "issue:A", "status_category", "indeterminate", d(9, 1))
    fact(s, "issue:A", "priority", "high", d(9, 1))
    fact(s, "issue:B", "status_category", "new", d(10, 1))
    fact(s, "issue:C", "status_category", "new", d(9, 2))
    fact(s, "issue:C", "status_category", "done", d(9, 10))
    fact(s, "issue:D", "status_category", "new", d(9, 5))
    fact(s, "issue:H", "status_category", "indeterminate", d(9, 1), recorded_at=d(10, 10))
    fact(s, "issue:K", "status_category", "new", d(9, 3))
    fact(s, "issue:L", "status_category", "done", d(9, 4))
    fact(s, "issue:W", "status", "On Hold", d(9, 1))
    fact(s, "issue:W", "status", "Waiting", d(9, 8))
    for src, rel, dst, since in [("issue:A", "blocks", "issue:B", d(9, 2)), ("issue:B", "blocks", "issue:K", d(9, 3)),
                                 ("issue:K", "blocks", "issue:L", d(9, 4)), ("issue:C", "blocks", "issue:D", d(9, 5)),
                                 ("issue:A", "relates_to", "issue:E", d(9, 6)), ("issue:F", "relates_to", "issue:G", d(9, 7)),
                                 ("issue:G", "relates_to", "issue:F", d(9, 7)), ("issue:A", "in_project", "project:P", d(9, 1))]:
        s.link(T, src, rel, dst, valid_from=since, source="test")
    return s


def tools(s, **kw):
    return Tools(cartridge(), seed(s), T, clock=lambda: NOW, **kw)


def entries(listing):
    return [x for x in listing if not (isinstance(x, dict) and ("more" in x or "note" in x))]


# ---- rules: composition truth tables (§5.4) ---------------------------------------------------------------


class _Graph:
    from keel.rules import Calendar

    calendar = Calendar()

    def facts(self, subject):
        return [Fact(tenant=T, subject=subject, predicate="x", value=1, valid_from=d(9, 1))]

    def history(self, subject, predicate):
        return self.facts(subject)

    def neighbors(self, subject, rel, direction):
        return []


LEAF = {True: {"predicate": "x", "eq": 1}, False: {"predicate": "x", "eq": 2}, None: {"predicate": "missing", "eq": 1}}
V3 = [True, False, None]


def kleene_and(a, b):
    return False if False in (a, b) else None if None in (a, b) else True


def kleene_or(a, b):
    return True if True in (a, b) else None if None in (a, b) else False


@pytest.mark.parametrize("a", V3)
@pytest.mark.parametrize("b", V3)
def test_all_any_follow_three_valued_logic(a, b):
    """`all`/`any` (and a bare list as `all`): null propagates unless a false in all or a true in any decides."""
    g = _Graph()
    assert evaluate({"when": {"all": [LEAF[a], LEAF[b]]}}, "issue:1", NOW, g) is kleene_and(a, b)
    assert evaluate({"when": [LEAF[a], LEAF[b]]}, "issue:1", NOW, g) is kleene_and(a, b)
    assert evaluate({"when": {"any": [LEAF[a], LEAF[b]]}}, "issue:1", NOW, g) is kleene_or(a, b)


@pytest.mark.parametrize("a", V3)
def test_not_negates_and_keeps_null(a):
    """`not` flips true and false; null stays null; a leaf on a predicate with no open fact is null."""
    assert evaluate({"when": {"not": LEAF[a]}}, "issue:1", NOW, _Graph()) is (None if a is None else not a)
    assert evaluate({"when": {"not": {"not": LEAF[a]}}}, "issue:1", NOW, _Graph()) is a


def test_a_rule_without_when_is_a_definition_only(store):
    """A rule without `when` is in `rules` but not in `evaluation`, and `search(rule=)` refuses it."""
    t = tools(store)
    ctx = t.context("issue:A")
    assert "velocity" in ctx["rules"] and "velocity" not in ctx["evaluation"]
    assert evaluate(RULES["velocity"], "issue:A", NOW, _Graph()) is None
    with pytest.raises(ValueError):
        t.search(rule="velocity")


def test_rules_evaluate_on_the_graph(store):
    """Leaves, `via` with direction in/out, composition and null on a subject with no facts, at now."""
    t = tools(store)
    ev = {s: t.context(f"issue:{s}")["evaluation"] for s in "ABCDE"}
    assert [ev[s]["blocked"] for s in "ABCDE"] == [False, True, False, False, False]  # E: not(null) is null, no blocker is false
    assert [ev[s]["blocking"] for s in "ABCDE"] == [True, True, True, False, False]  # out: whom it blocks (D is new)
    assert ev["A"]["urgent"] is True and ev["B"]["urgent"] is None and ev["C"]["urgent"] is False
    assert ev["A"]["stalled"] is True and ev["B"]["stalled"] is False and ev["E"]["stalled"] is None


def test_via_direction_in_out_any(store):
    """`direction: in` reaches who blocks the subject, `out` whom it blocks; `any` both."""
    t = tools(store)
    t.cartridge.rules = {**RULES, "near_new": {"applies_to": "issue", "when": {"via": "blocks", "any": {"predicate": "status_category", "eq": "new"}}},
                         "blocked_by_new": {"applies_to": "issue", "when": {"via": "blocks", "direction": "in", "any": {"predicate": "status_category", "eq": "new"}}},
                         "blocks_new": {"applies_to": "issue", "when": {"via": "blocks", "direction": "out", "any": {"predicate": "status_category", "eq": "new"}}}}
    ev = t.context("issue:K")["evaluation"]  # B (new) blocks K; K blocks L (done)
    assert (ev["blocked_by_new"], ev["blocks_new"], ev["near_new"]) == (True, False, True)
    ev = t.context("issue:A")["evaluation"]  # A blocks B (new); nobody blocks A
    assert (ev["blocked_by_new"], ev["blocks_new"], ev["near_new"]) == (False, True, True)


def test_via_all_with_no_neighbours_is_true(store):
    """No neighbours: `any` is false, `all` is true."""
    t = tools(store)
    t.cartridge.rules = {"all_done": {"applies_to": "issue", "when": {"via": "blocks", "direction": "in", "all": {"predicate": "status_category", "eq": "done"}}}}
    assert t.context("issue:A")["evaluation"] == {"all_done": True}  # nobody blocks A
    assert t.context("issue:D")["evaluation"] == {"all_done": True}  # C, done, blocks D
    assert t.context("issue:B")["evaluation"] == {"all_done": False}  # A blocks B and is not done


def test_symmetric_relations_ignore_direction(store):
    """A symmetric relation is reached from either end whatever `direction` says."""
    t = tools(store)
    ev = {s: t.context(f"issue:{s}")["evaluation"]["linked_open"] for s in "AEFG"}
    assert ev == {"A": None, "E": True, "F": None, "G": None}  # E reaches A (open); A reaches E (no facts: null); F/G have none


def test_open_for_days_at_as_of(store):
    """`open_for_days` measures from the open fact's valid_from to `as_of`; gte 30 holds on day 30 exactly."""
    t = tools(store)
    assert t.context("issue:A", as_of=d(10, 1))["evaluation"]["stalled"] is True
    assert t.context("issue:A", as_of=d(9, 30))["evaluation"]["stalled"] is False
    assert t.context("issue:A", as_of="2026-08-01")["evaluation"]["stalled"] is None  # nothing open yet


def test_durations_at_as_known_at(store):
    """`as_known_at` evaluates on what Keel knew then (as_of defaults to it): a late fact decides nothing before it arrived."""
    t = tools(store)
    assert t.context("issue:H", as_known_at=d(10, 5))["evaluation"]["stalled"] is None
    late = t.context("issue:H", as_known_at=d(10, 12))
    assert late["as_of"] == d(10, 12).isoformat() and late["evaluation"]["stalled"] is True


def test_held_for_survives_a_change_between_waiting_values(store):
    """`held_for_days` counts across consecutive facts that satisfy the comparison; `open_for_days` would restart."""
    t = tools(store)
    assert t.context("issue:W", as_of=d(9, 15))["evaluation"]["waiting"] is True  # held 14 days, open only 7
    assert t.context("issue:W", as_of=d(9, 10))["evaluation"]["waiting"] is False


@pytest.mark.parametrize("as_of", [None, d(9, 20), d(10, 2)])
def test_one_evaluation_in_context_search_and_count(store, as_of):
    """A rule decides the same in context, search(rule=) and search(detail="count"), at any as_of."""
    t = tools(store)
    keys = sorted(e.key for e in store.entities(T, "issue"))
    for name, rule in RULES.items():
        if "when" not in rule:
            continue
        true = [k for k in keys if t.context(k, as_of=as_of)["evaluation"][name] is True]
        assert t.search(rule=name, detail="key", as_of=as_of) == true, name
        assert t.search(rule=name, detail="count", as_of=as_of) == [{"count": len(true)}], name


# ---- search (§7.3) ---------------------------------------------------------------------------------------


def test_search_kind_list_is_ordered_by_key(store):
    """`kind` may be a list: the listing covers those kinds, ordered by key."""
    keys = tools(store).search(kind=["project", "issue"], detail="key")
    assert keys == sorted(keys) and "project:P" in keys and "issue:A" in keys and len(keys) == 12


def test_search_where_matches_attrs_and_facts_without_case(store):
    """`where` keeps entities whose attribute or current fact is the whole value, compared without case."""
    t = tools(store)
    assert t.search(kind="issue", where={"status_category": "INDETERMINATE"}, detail="key") == ["issue:A", "issue:H"]
    assert t.search(kind="issue", where={"title": "login fails"}, detail="key") == ["issue:A"]
    assert t.search(kind="issue", where={"title": "login"}, detail="key") == []  # the whole value, not a part
    assert t.search(kind="issue", where={"status_category": "new"}, as_of=d(9, 5), detail="key") == ["issue:C", "issue:D", "issue:K"]


def test_search_where_a_list_of_values_is_any_of_them(store):
    """`where={name: [v1, v2]}` keeps a subject whose value is any of them: "how many had a status among A, B, C" is one
    call, not one per status (evals/jira/requests.md). A list-valued attribute or fact still matches as before: the
    same list, or a list holding the wanted value."""
    t = tools(store)
    assert t.search(kind="issue", where={"status_category": ["new", "INDETERMINATE"]}, as_of=d(9, 5), detail="key") == [
        "issue:A", "issue:C", "issue:D", "issue:H", "issue:K"]
    assert t.search(kind="issue", where={"status_category": ["new", "indeterminate"]}, as_of=d(9, 5), detail="count") == [{"count": 5}]
    assert t.search(where={"status_category": ["done"]}, detail="key") == ["issue:C", "issue:L"]
    assert t.search(kind="issue", where={"status_category": ["new", "indeterminate"], "priority": ["high", "low"]}, detail="key") == ["issue:A"]
    store.open_fact(T, "issue:E", "labels", ["ui", "auth"], valid_from=d(9, 1), source="test")
    assert t.search(kind="issue", where={"labels": ["ui", "auth"]}, detail="key") == ["issue:E"]  # the same list
    assert t.search(kind="issue", where={"labels": "auth"}, detail="key") == ["issue:E"]  # a list holding it


def test_search_where_hits_say_since_when_they_hold_the_value(store):
    """With detail brief or concise, each hit of a `where` says since when it has held the wanted value of each fact it
    filtered on (`since: {predicate: instant}`, as context's brief does): "in progress for 30 days or more" needs no
    history call per hit. Only the filtered predicates: the other facts carry no time, so the hit stays small."""
    t = tools(store)
    [a] = t.search(kind="issue", where={"title": "Login fails", "status_category": "indeterminate"}, detail="brief")
    assert a["since"] == {"status_category": d(9, 1).isoformat()}  # title is an attribute: no time to give
    hits = t.search(kind="issue", where={"status": ["waiting", "on hold"]}, as_of=d(9, 5), detail="concise")
    assert [(h["key"], h["since"]) for h in hits] == [("issue:W", {"status": d(9, 1).isoformat()})]
    assert t.search(kind="issue", where={"status": "waiting"}, detail="brief")[0]["since"] == {"status": d(9, 8).isoformat()}
    assert "since" not in t.search(kind="issue", where={"status": "waiting"})[0]  # entity: what it is, not its facts
    assert "since" not in t.search(kind="issue", where={"title": "Release"}, detail="brief")[0]


def test_search_where_on_a_kind_or_relation_name_says_how_to_filter_by_neighbour(store):
    """`where={"project": "P"}` matches nothing when `project` is a kind, not an attribute: rather than an empty answer
    the agent retries blind (evals/jira/requests.md: a project lookup, then the listing again), the empty result says
    which call filters by a neighbour. Only when nothing matched: a real attribute of that name still filters."""
    t = tools(store)
    [note] = t.search(kind="issue", where={"project": "P"}, detail="key")
    assert "related_to='project:P'" in note["note"] and "via='in_project'" in note["note"]
    assert t.search(kind="issue", where={"project": "P"}, detail="count") == [note, {"count": 0}]
    [note] = t.search(kind="issue", where={"in_project": "project:P"})
    assert "related_to='project:P'" in note["note"] and "via='in_project'" in note["note"]
    assert t.search(kind="issue", related_to="project:P", via="in_project", detail="key") == ["issue:A"]
    assert t.search(kind="issue", where={"status_category": "nothing"}, detail="key") == []  # a fact: empty is the answer
    store.upsert_entity(T, "issue", "Z", ref="jira#Z", title="Z", project="P")
    assert t.search(kind="issue", where={"project": "P"}, detail="key") == ["issue:Z"]


def test_search_where_without_kind_or_text_returns_only_the_entities_that_match(store):
    """Without a text nothing matched a fact: `where` alone answers with the entities it keeps, as with `kind`,
    and their other facts are not hits (evals/jira: 8 issues came back with 48 fact hits and "total": 56)."""
    t = tools(store)
    expected = t.search(kind="issue", where={"status_category": "indeterminate"}, detail="key")
    out = t.search(where={"status_category": "indeterminate"})
    assert [h["key"] for h in out] == expected and all("predicate" not in h for h in out)
    assert t.search(where={"status_category": "indeterminate"}, detail="count") == [{"count": len(expected)}]
    assert [h["key"] for h in t.search(where={"status_category": "new"}, as_of=d(9, 5))] == ["issue:C", "issue:D", "issue:K"]


def test_search_as_of_and_as_known_at(store):
    """`as_of` evaluates the rule at that instant; `as_known_at` reads what Keel knew then."""
    t = tools(store)
    assert t.search(rule="stalled", detail="key") == ["issue:A", "issue:H"]
    assert t.search(rule="stalled", as_of=d(9, 20), detail="key") == []
    assert t.search(rule="stalled", as_known_at=d(10, 5), detail="key") == ["issue:A"]


def bulk(s, n=120):
    for i in range(n):
        s.upsert_entity(T, "issue", f"N{i:03d}", ref=None)
    return Tools(cartridge(), s, T, clock=lambda: NOW)


def test_default_listing_pages_every_item_exactly_once(store):
    """A listing without limit returns 50 and a leading {"more": {shown, total, next}}; paging with `after`
    (the last key received) yields every entity exactly once."""
    t = bulk(store)
    page, seen = t.search(kind="issue"), []
    assert set(page[0]["more"]) == {"shown", "total", "next"} and page[0]["more"]["shown"] == 50 and page[0]["more"]["total"] == 120
    while True:
        items = entries(page)
        seen += [e["key"] for e in items]
        if "more" not in page[0]:
            break
        assert items[-1]["key"] in page[0]["more"]["next"]
        page = t.search(kind="issue", after=items[-1]["key"])
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 120


def test_limit_pages_without_marker(store):
    """A given limit is honoured without a marker; `after` continues; an id alone is accepted for one kind."""
    t = bulk(store, 20)
    seen, after = [], None
    while page := t.search(kind="issue", limit=7, after=after, detail="key"):
        assert not any(isinstance(x, dict) for x in page)
        seen += page
        after = page[-1].split(":", 1)[1]
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 20


def test_max_list_ceiling_and_count_ignores_it(store):
    """With max_list, asking for all (limit=0) or more returns the ceiling and a `more`; a count is never capped."""
    t = Tools(cartridge(), bulk(store).store, T, clock=lambda: NOW, max_list=10)
    page = t.search(kind="issue", limit=0)
    assert page[0]["more"]["shown"] == 10 and page[0]["more"]["total"] == 120 and len(entries(page)) == 10
    assert len(entries(t.search(kind="issue", limit=500))) == 10
    assert len(t.search(kind="issue", limit=5)) == 5
    assert t.search(kind="issue", detail="count") == [{"count": 120}]


def test_max_list_ceiling_on_history_and_related(store):
    """history and related take the ceiling too: "all" returns it, with a leading `more`."""
    t = tools(store, max_list=2)
    assert len(t.history("issue:C", limit=None)) == 2  # exactly the ceiling: nothing cut, no marker
    assert t.history(since=d(1, 1), limit=0)[0]["more"]["shown"] == 2 and t.related("issue:A", depth=3, limit=0)[0]["more"]["shown"] == 2


def test_max_list_caps_the_default_listing(store):
    """The ceiling is on one listing: the default page of 50 is more than a ceiling of 10."""
    t = Tools(cartridge(), bulk(store).store, T, clock=lambda: NOW, max_list=10)
    assert len(entries(t.search(kind="issue"))) == 10


def test_search_detail_modes(store):
    """key: strings; entity: the entity with `key`; brief: key, attrs and facts as {predicate: value}, no tenant/kind/id/ref;
    concise: the entity plus facts as a mapping; full: facts as fact records."""
    t = tools(store)
    assert t.search(kind="issue", where={"title": "Login fails"}, detail="key") == ["issue:A"]
    [ent] = t.search(kind="issue", where={"title": "Login fails"})
    assert ent["key"] == "issue:A" and ent["kind"] == "issue" and ent["ref"] == "jira#A" and "facts" not in ent
    [brief] = t.search(kind="issue", where={"title": "Login fails"}, detail="brief")
    assert brief["key"] == "issue:A" and brief["facts"] == {"status_category": "indeterminate", "priority": "high"}
    assert not {"tenant", "kind", "id", "ref"} & set(brief)
    [concise] = t.search(kind="issue", where={"title": "Login fails"}, detail="concise")
    assert concise["kind"] == "issue" and concise["facts"]["priority"] == "high"
    [full] = t.search(kind="issue", where={"title": "Login fails"}, detail="full")
    assert {f["predicate"] for f in full["facts"]} == {"status_category", "priority"} and all("valid_from" in f for f in full["facts"])


def test_search_text_default_cap_is_20(store):
    """A text search without limit returns its first 20 results and a `more` that says the total."""
    t = bulk(store, 30)
    page = t.search("N0")
    assert page[0]["more"]["shown"] == 20 and page[0]["more"]["total"] == 30 and len(entries(page)) == 20


def test_search_related_to_and_depth(store):
    """`related_to` (+ `via`) keeps the subject's neighbours; `depth=1` adds neighbours with direction."""
    t = tools(store)
    assert t.search(kind="issue", related_to="issue:A", detail="key") == ["issue:B", "issue:E"]
    assert t.search(kind="issue", related_to="issue:A", via="blocks", detail="key") == ["issue:B"]
    [b] = t.search(kind="issue", where={"title": "Release"}, depth=1)
    assert {(n["relation"], n["direction"], n["subject"]) for n in b["neighbors"]} == {("blocks", "in", "issue:A"), ("blocks", "out", "issue:K")}


def test_search_unknown_via_suggests(store):
    """A relation the cartridge does not declare raises ValueError with the closest name and the valid ones."""
    with pytest.raises(ValueError, match="did you mean 'blocks'"):
        tools(store).search(kind="issue", related_to="issue:A", via="block")


# ---- related (§7.5) --------------------------------------------------------------------------------------


def test_related_direction_and_symmetric_dedupe(store):
    """Direction is relative to the subject; a symmetric relation recorded from both ends is one entry, `both`."""
    t = tools(store)
    got = {(r["relation"], r["direction"], r["subject"]) for r in t.related("issue:B")}
    assert got == {("blocks", "in", "issue:A"), ("blocks", "out", "issue:K")}
    assert t.related("issue:F") == [{"relation": "relates_to", "direction": "both", "subject": "issue:G", "since": d(9, 7).isoformat()}]
    assert [r["direction"] for r in t.related("issue:E")] == ["both"]


def test_related_depth_walks_each_subject_once(store):
    """depth 2-3 keeps walking, nearest first, each subject once; every entry has `hops` and `via`."""
    t = tools(store)
    walk = t.related("issue:A", relation="blocks", depth=3)
    assert [(r["subject"], r["hops"], r["via"], r["direction"]) for r in walk] == [
        ("issue:B", 1, "issue:A", "out"), ("issue:K", 2, "issue:B", "out"), ("issue:L", 3, "issue:K", "out")]
    two = t.related("issue:B", depth=2)
    assert len({r["subject"] for r in two}) == len(two) and "issue:B" not in {r["subject"] for r in two}
    assert {r["subject"] for r in two if r["hops"] == 2} == {"issue:E", "issue:L", "project:P"}


def test_related_order_and_limit_keeps_nearest(store):
    """Ordered by hop, relation, link time, key; a limit keeps the nearest hops, and `more` counts by relation."""
    t = tools(store)
    walk = t.related("issue:A", depth=3)
    assert [r["hops"] for r in walk] == sorted(r["hops"] for r in walk)
    cut = t.related("issue:A", depth=3, limit=3)
    more = cut[0]["more"]
    assert set(more) == {"shown", "total", "omitted", "by_relation", "next"} and more["shown"] == 3 and more["total"] == len(walk)
    assert sum(more["by_relation"].values()) == len(walk)
    assert all(r["hops"] == 1 for r in entries(cut))  # A has three hop-1 links: blocks, relates_to, in_project


def test_related_errors(store):
    """An undeclared relation or a depth outside 1-3 raises ValueError."""
    t = tools(store)
    with pytest.raises(ValueError, match="did you mean 'blocks'"):
        t.related("issue:A", relation="block")
    with pytest.raises(ValueError):
        t.related("issue:A", depth=4)


def test_related_as_of(store):
    """Open relations at `as_of`: a link made later is not there yet."""
    t = tools(store)
    assert [r["subject"] for r in t.related("issue:A", as_of=d(9, 3))] == ["issue:B", "project:P"]


# ---- history (§7.2) --------------------------------------------------------------------------------------


def test_history_subject_oldest_first_with_closed_facts(store):
    """A subject's facts, oldest first, closed ones included; the first closes at the second's valid_from."""
    h = tools(store).history("issue:C")
    assert [f["value"] for f in h] == ["new", "done"] and h[0]["valid_to"] == h[1]["valid_from"]
    for key in ("valid_from", "valid_to", "recorded_at", "closed_at", "source", "evidence", "scope", "corrected_by", "task"):
        assert key in h[0]


def test_history_tenant_window(store):
    """No subject with since/until: the tenant's facts that started in that window, oldest first; predicate filters."""
    t = tools(store)
    window = t.history(since=d(9, 2), until=d(9, 5))
    assert [(f["subject"], f["valid_from"]) for f in window] == [
        ("issue:C", d(9, 2).isoformat()), ("issue:K", d(9, 3).isoformat()), ("issue:L", d(9, 4).isoformat()), ("issue:D", d(9, 5).isoformat())]
    assert [f["subject"] for f in t.history(since=d(9, 1), until=d(9, 1), predicate="priority")] == ["issue:A"]
    with pytest.raises(ValueError):
        t.history()


def test_history_says_which_value_each_fact_replaced(store):
    """`was` is the value that held the predicate until the fact started; a first value has no `was`. So "whose
    status changed this week" needs no guess from evidence (evals/jira: creations counted as changes, changes
    dropped as creations), even when the window or `since` leaves the replaced fact out, and a null is a value."""
    t = tools(store)
    fact(store, "issue:B", "assignee", None, d(10, 1))
    fact(store, "issue:B", "assignee", "ana", d(10, 3))
    for detail in ("full", "concise"):
        h = t.history("issue:C", detail=detail)
        assert "was" not in h[0] and h[1]["was"] == "new"
        assert [f.get("was", "-") for f in t.history("issue:B", predicate="assignee", detail=detail)] == ["-", None]
    late = t.history(since=d(9, 5), until=d(9, 10))  # C's "new" started before the window
    assert [(f["subject"], f.get("was", "-")) for f in late] == [("issue:D", "-"), ("issue:W", "On Hold"), ("issue:C", "new")]
    window = t.history(since=d(9, 2), until=d(9, 10))
    assert [(f["subject"], f.get("was", "-")) for f in window] == [
        ("issue:C", "-"), ("issue:K", "-"), ("issue:L", "-"), ("issue:D", "-"), ("issue:W", "On Hold"), ("issue:C", "new")]
    assert "was" not in t.history("issue:H", as_known_at=d(10, 1)) and t.history("issue:W", as_known_at=NOW)[1]["was"] == "On Hold"
    t.annotate("issue:A", "note", "first", scope="tenant", evidence="ev:issue:A:priority:2026-09-01")
    assert "was" not in t.history("issue:A", predicate="note")[0]


def test_history_predicate_and_scope_filters(store):
    """`predicate` and `scope` filter; an unknown scope raises with the valid ones."""
    t = tools(store)
    t.annotate("issue:A", "note", "looked at it", scope="tenant", evidence="ev:issue:A:priority:2026-09-01")
    t.annotate("issue:A", "hunch", "auth", scope="task")
    assert [f["predicate"] for f in t.history("issue:A", scope="tenant")] == ["status_category", "priority", "note"]
    assert [f["value"] for f in t.history("issue:A", scope="task")] == ["auth"]
    assert [f["value"] for f in t.history("issue:A", predicate="note")] == ["looked at it"]
    with pytest.raises(ValueError, match="agent, task, tenant, global|global"):
        t.history("issue:A", scope="team")


def test_history_limit_keeps_most_recent(store):
    """`limit` keeps the most recent n, still oldest first, with a leading {"more": {shown, total, omitted, next}}."""
    t = tools(store)
    for i in range(5):
        fact(store, "issue:B", "points", i, d(10, 2 + i))
    cut = t.history("issue:B", predicate="points", limit=2)
    assert set(cut[0]["more"]) == {"shown", "total", "omitted", "next"} and (cut[0]["more"]["shown"], cut[0]["more"]["total"]) == (2, 5)
    assert [f["value"] for f in cut[1:]] == [3, 4]
    assert len(t.history("issue:B", predicate="points", limit=0)) == 5


def test_history_changed_since_round_trip(store):
    """The context watermark, passed back exactly, returns only what was written after it, per subject or tenant-wide."""
    t = tools(store)
    wm = t.context("issue:A")["watermark"]
    assert wm is not None and t.history(changed_since=wm) == []
    fact(store, "issue:D", "status_category", "done", d(10, 14))
    changed = t.history(changed_since=wm)
    assert [(f["subject"], f["value"]) for f in changed] == [("issue:D", "new"), ("issue:D", "done")]  # the close is a change too
    assert [f["value"] for f in t.history("issue:D", changed_since=wm)] == ["new", "done"]
    assert t.history("issue:A", changed_since=wm) == []
    assert t.history(changed_since=t.context("issue:A")["watermark"]) == []


def test_history_concise_is_lean(store):
    """concise drops nulls, tenant and transaction-time bookkeeping; subject only when none was given."""
    t = tools(store)
    [f] = t.history("issue:B", detail="concise")
    assert not {"tenant", "recorded_at", "closed_at", "subject", "scope", "valid_to"} & set(f)
    assert {"predicate", "value", "valid_from", "source", "evidence"} <= set(f)
    rows = [f for f in t.history(since=d(9, 2), detail="concise") if not {"initial", "more"} & set(f)]  # past the counts
    assert rows and all("subject" in f for f in rows)


# ---- context (§7.1) --------------------------------------------------------------------------------------


def test_context_full_shape(store):
    """full: subject, entity (with key), open facts, relations, applicable rules, evaluation, as_of, watermark."""
    ctx = tools(store).context("issue:A")
    assert {"subject", "entity", "facts", "relations", "rules", "evaluation", "as_of", "watermark"} <= set(ctx)
    assert ctx["entity"]["key"] == "issue:A" and ctx["entity"]["ref"] == "jira#A"
    assert {f["predicate"] for f in ctx["facts"]} == {"status_category", "priority"}
    assert set(ctx["evaluation"]) == {r for r, spec in RULES.items() if "when" in spec}


def test_context_concise_and_brief_shapes(store):
    """concise: facts as {predicate: value}, since, relations one by one, no rule definitions; brief groups
    relations by name, incoming as `<-relation`, symmetric without an arrow."""
    t = tools(store)
    c = t.context("issue:B", detail="concise")
    assert c["facts"] == {"status_category": "new"} and c["since"] == {"status_category": d(10, 1).isoformat()}
    assert "rules" not in c and "entity" not in c and c["attrs"] == {"title": "Release"}
    assert {(r["relation"], r["direction"], r["subject"]) for r in c["relations"]} == {("blocks", "in", "issue:A"), ("blocks", "out", "issue:K")}
    b = t.context("issue:B", detail="brief")
    assert b["relations"] == {"<-blocks": ["issue:A"], "blocks": ["issue:K"]}
    assert t.context("issue:E", detail="brief")["relations"] == {"relates_to": ["issue:A"]}
    assert t.context("issue:F", detail="concise")["relations"] == [{"relation": "relates_to", "direction": "both", "subject": "issue:G"}]


def test_context_as_of_default_rounds_up_to_the_second(store):
    """Without as_of, context answers at now rounded up to the next whole second, in the same bytes twice."""
    t = Tools(cartridge(), seed(store), T, clock=lambda: NOW + timedelta(microseconds=250))
    a, b = t.context("issue:A"), t.context("issue:A")
    assert a["as_of"] == (NOW + timedelta(seconds=1)).isoformat() and json.dumps(a) == json.dumps(b)


def test_context_depth_one_neighbours(store):
    """depth=1 carries each neighbour's entity, open facts and rule evaluation."""
    ctx = tools(store).context("issue:B", depth=1)
    near = {n["subject"]: n for n in ctx["neighbors"]}
    assert set(near) == {"issue:A", "issue:K"} and near["issue:A"]["direction"] == "in"
    assert near["issue:A"]["entity"]["key"] == "issue:A" and near["issue:A"]["evaluation"]["stalled"] is True
    assert [f["value"] for f in near["issue:K"]["facts"]] == ["new"]
    assert "neighbors" not in tools(store).context("issue:B")


def test_context_many_subjects_in_order(store):
    """A list of subjects answers one per subject, in order, the same as one call each."""
    t = tools(store)
    keys = ["issue:L", "issue:A", "project:P", "issue:ZZ"]
    many = t.context(keys, detail="concise")
    assert [c["subject"] for c in many] == keys and many == [t.context(k, detail="concise") for k in keys]


def test_context_unknown_subject_is_not_found(store):
    """A well-formed subject Keel knows nothing about is {subject, found: false, as_of, watermark}, at any detail."""
    t = tools(store)
    for detail in ("full", "concise", "brief"):
        assert set(t.context("issue:ZZ", detail=detail)) == {"subject", "found", "as_of", "watermark"}
        assert t.context("issue:ZZ", detail=detail)["found"] is False


def test_context_exclude_and_only(store):
    """exclude hides predicates from facts and neighbours, rules still see them; only keeps just those."""
    t = tools(store)
    ctx = t.context("issue:B", depth=1, exclude=["status_category"], detail="concise")
    assert ctx["facts"] == {} and all("status_category" not in n["facts"] for n in ctx["neighbors"])
    assert ctx["evaluation"]["blocked"] is True  # the verdict is on the whole truth
    assert t.context("issue:A", only=["priority"], detail="concise")["facts"] == {"priority": "high"}
    assert t.context("issue:A", only=["priority"], exclude=["priority"], detail="concise")["facts"] == {}
    with pytest.raises(ValueError):
        t.context("issue:A", only=[])


def test_wrong_names_raise_value_errors_with_suggestions(store):
    """A key without a kind, an undeclared kind, rule or scope, and a non-ISO time are ValueErrors that help."""
    t = tools(store)
    with pytest.raises(ValueError, match="did you mean 'issue:A'"):
        t.context("A")
    with pytest.raises(ValueError, match="did you mean 'issue'"):
        t.context("isue:A")
    with pytest.raises(ValueError, match="did you mean 'issue'"):
        t.search(kind="isue")
    with pytest.raises(ValueError, match="did you mean 'stalled'"):
        t.search(rule="staled")
    with pytest.raises(ValueError, match="ISO 8601"):
        t.context("issue:A", as_of="yesterday")
    with pytest.raises(ValueError, match="ISO 8601"):
        t.history("issue:A", since="last week")
    with pytest.raises(ValueError, match="tenant"):
        t.annotate("issue:A", "note", "x", scope="team")


def test_time_arguments_take_people_times(store):
    """Every time argument takes a datetime, ISO text, or "2026-10-01 00:00 UTC"."""
    t = tools(store)
    a = t.context("issue:A", as_of=d(10, 1))
    assert t.context("issue:A", as_of="2026-10-01T00:00:00Z") == a == t.context("issue:A", as_of="2026-10-01 00:00 UTC")


# ---- annotate (§7.4), outside scope policy ---------------------------------------------------------------


def test_annotate_returns_the_stored_fact_and_ack(store):
    """full returns the fact with expires_at, status, trace and cartridge; ack only what was written where."""
    t = tools(store)
    full = t.annotate("issue:A", "hunch", "auth", scope="task", task="triage")
    assert {"expires_at", "status", "trace", "cartridge", "value"} <= set(full) and full["expires_at"] is not None
    ack = t.annotate("issue:A", "hunch", "db", scope="task", task="triage", detail="ack")
    assert set(ack) <= {"subject", "predicate", "scope", "valid_from", "valid_to", "expires_at", "task", "status"}
    assert ack["task"] == "triage" and "value" not in ack


def test_annotate_key_is_idempotent(store):
    """The same key returns the fact it wrote; the same key with other arguments raises."""
    t = tools(store)
    first = t.annotate("issue:A", "hunch", "auth", key="k1")
    assert t.annotate("issue:A", "hunch", "auth", key="k1") == first
    assert len(t.history("issue:A", predicate="hunch", scope="task")) == 1
    with pytest.raises(ValueError):
        t.annotate("issue:A", "hunch", "db", key="k1")


def test_proposal_waits_and_a_decision_closes_it(store):
    """A proposal never touches accepted facts and waits in `proposals`; an accepted write closes it."""
    t = tools(store)
    t.annotate("issue:A", "priority", "low", scope="tenant", evidence="ev:issue:A:priority:2026-09-01", status="proposed")
    ctx = t.context("issue:A", detail="concise")
    assert ctx["facts"]["priority"] == "high" and [p["value"] for p in ctx["proposals"]] == ["low"]
    t.annotate("issue:A", "priority", "medium", scope="tenant", evidence="ev:issue:A:priority:2026-09-01")
    ctx = t.context("issue:A", detail="concise", as_of=NOW + timedelta(seconds=2))
    assert ctx["facts"]["priority"] == "medium" and "proposals" not in ctx


# ---- transports (§7.7) -----------------------------------------------------------------------------------


def _http(s):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    client = TestClient(build_http_app(cartridge(), seed(s), secret="s", clock=lambda: NOW))
    h = {"Authorization": "Bearer s", "X-Keel-Tenant": T}
    return lambda tool, **body: client.post(f"/v1/{tool}", json=body, headers=h)


def test_http_answers_like_python(store):
    """POST /v1/<tool> returns what the Python tool returns, given the same arguments and detail."""
    call = _http(store)
    t = Tools(cartridge(), store, T, clock=lambda: NOW)
    at = "2026-10-02T00:00:00+00:00"
    assert call("context", subject="issue:B", as_of=at, depth=1, detail="full").json() == t.context("issue:B", as_of=at, depth=1)
    assert call("context", subject="issue:B", as_of=at).json() == t.context("issue:B", as_of=at, detail="brief")  # brief by default
    assert call("history", subject="issue:C").json() == t.history("issue:C", detail="concise")  # concise by default
    assert call("search", rule="blocked", detail="key", as_of=at).json() == t.search(rule="blocked", detail="key", as_of=at)
    assert call("search", rule="stalled", detail="count").json() == t.search(rule="stalled", detail="count")
    assert call("related", subject="issue:A", depth=3).json() == t.related("issue:A", depth=3)
    assert call("context", subject="A").status_code == 400  # a wrong name is an error, not an empty answer


def _mcp(s):
    pytest.importorskip("mcp")
    from keel.mcp_server import build_server

    server = build_server(Tools(cartridge(), seed(s), T, clock=lambda: NOW), compact=False)  # the JSON text, to compare with Python

    def call(tool, **args):
        res = asyncio.run(server.call_tool(tool, args))
        return json.loads(res.content[0].text)

    return call


def test_mcp_answers_like_python(store):
    """Over MCP: one compact JSON block with the same data; context brief and history concise by default."""
    call = _mcp(store)
    t = Tools(cartridge(), store, T, clock=lambda: NOW)
    at = "2026-10-02T00:00:00+00:00"
    assert call("context", subject="issue:B", as_of=at, detail="full") == t.context("issue:B", as_of=at)
    assert call("context", subject="issue:B", as_of=at) == t.context("issue:B", as_of=at, detail="brief")
    assert call("history", subject="issue:C") == t.history("issue:C", detail="concise")
    assert call("search", rule="blocked", detail="key", as_of=at) == t.search(rule="blocked", detail="key", as_of=at)
    assert call("related", subject="issue:A", depth=2) == t.related("issue:A", depth=2)


def test_mcp_annotate_takes_any_json_value(store):
    """`annotate`'s value is any JSON over MCP, null included, as in Python and over HTTP."""
    call = _mcp(store)
    assert call("annotate", subject="issue:A", predicate="assignee", value=None, scope="task")["predicate"] == "assignee"


def test_pack_writes_times_short_and_lists_of_records_as_tables():
    """The compact text form (`build_server(compact=True)`): UTC times without `+00:00` or zero seconds, and a list of
    records as one table, `all` holding what every record shares, nested maps as `a.b` columns, a record's own
    fields in a trailing object, and the list's leading markers kept."""
    from keel.mcp_server import pack

    assert pack("2026-10-03T08:00:00+00:00") == "2026-10-03T08:00Z"
    assert pack("2026-10-03T08:00:13+00:00") == "2026-10-03T08:00:13Z"
    assert pack("2026-10-26T00:46:49.050634+00:00") == "2026-10-26T00:46:49.050634Z"  # a watermark keeps every digit
    assert pack("2026-10-03T08:00:00-05:00") == "2026-10-03T08:00:00-05:00" and pack("since 2026-10-03T08:00:00+00:00") == "since 2026-10-03T08:00:00+00:00"
    history = [
        {"more": {"shown": 2, "total": 3}},
        {"predicate": "status", "value": "New", "valid_from": "2026-09-01T00:00:00+00:00", "valid_to": "2026-09-02T10:00:00+00:00", "source": "jira"},
        {"predicate": "status", "value": "Done", "valid_from": "2026-09-02T10:00:00+00:00", "source": "jira", "was": "New"},
    ]
    assert pack(history) == {
        "more": {"shown": 2, "total": 3},
        "all": {"predicate": "status", "source": "jira"},
        "cols": ["value", "valid_from"],
        "rows": [["New", "2026-09-01T00:00Z", {"valid_to": "2026-09-02T10:00Z"}], ["Done", "2026-09-02T10:00Z", {"was": "New"}]],
    }
    hits = [{"key": "issue:A", "attrs": {"title": "Login"}, "facts": {"status": "open", "owner": None}},
            {"key": "issue:B", "attrs": {"title": "Release"}, "facts": {"owner": "ana", "status": "open", "due": "2026-10-01"}}]
    assert pack(hits) == {"all": {"facts.status": "open"}, "cols": ["key", "attrs.title", "facts.owner"],
                          "rows": [["issue:A", "Login", None], ["issue:B", "Release", "ana", {"facts.due": "2026-10-01"}]]}
    assert pack(["issue:A", "issue:B"]) == ["issue:A", "issue:B"] and pack([{"count": 3}]) == [{"count": 3}]  # nothing to share


def test_unpack_restores_what_pack_wrote():
    """The compact form is lossless: every field, null or absent (a first value has no `was`; `was: null` was unset),
    nesting, order of records and markers comes back; only the times are written short (the same instants)."""
    from keel.envelope import parse_iso
    from keel.mcp_server import pack, unpack

    cases = [
        [{"predicate": "p", "value": None, "valid_from": "2026-09-01T00:00:00+00:00"}, {"predicate": "p", "value": 1, "valid_from": "2026-09-02T00:00:00+00:00", "was": None}],
        [{"initial": {"omitted": 4}}, {"subject": "a:1", "was": "x"}, {"subject": "a:2", "was": "y", "value": [{"k": 1}, {"k": 2}]}],
        [{"key": "project:P", "attrs": {}}, {"key": "issue:A", "attrs": {"title": "x"}}],
        [[{"a": 1}, {"a": 2}], []],
        {"subject": "issue:A", "relations": [{"relation": "blocks", "direction": "out", "subject": "issue:B"}, {"relation": "blocks", "direction": "in", "subject": "issue:C"}]},
        [{"cols": [1], "rows": [2]}, {"~": 1}, {"x.y": 1}],  # data that looks like the form's own keys stays data
        [{"a": {"x.y": 1}}, {"a": {"x.y": 2}}],
        [{"a": 1, "b": 2}, {"a": 1, "b": 2}],  # every field shared: rows of nothing
        {"facts": {"due": "2026-10-03T08:00:00+00:00"}, "watermark": "2026-10-26T00:46:49.050634+00:00"},
    ]
    for case in cases:
        packed = json.loads(json.dumps(pack(case)))
        assert unpack(packed) == json.loads(json.dumps(case).replace("T00:00:00+00:00", "T00:00Z").replace("T08:00:00+00:00", "T08:00Z")
                                            .replace(".050634+00:00", ".050634Z")), case
    assert parse_iso(pack("2026-10-03T08:00:00+00:00")) == parse_iso("2026-10-03T08:00:00+00:00")


def test_mcp_compact_text_carries_the_same_answer_in_fewer_tokens(store):
    """`build_server(tools)`: what the model reads is packed by default (unpack gives the answer back);
    structuredContent, what programs read, is the JSON as ever; `compact=False` keeps the JSON text."""
    pytest.importorskip("mcp")
    from keel.mcp_server import build_server, instructions, unpack

    t = Tools(cartridge(), seed(store), T, clock=lambda: NOW)
    plain, packed = build_server(t, compact=False), build_server(t)
    at = "2026-10-02T00:00:00+00:00"
    reads = [("context", {"subject": "issue:B", "as_of": at, "depth": 1}), ("context", {"subject": "issue:B", "detail": "full"}),
             ("history", {"subject": "issue:W"}), ("history", {"subject": "issue:W", "detail": "full"}),
             ("search", {"kind": "issue", "detail": "brief"}), ("search", {"kind": "issue", "detail": "full", "limit": 3}),
             ("related", {"subject": "issue:A", "depth": 2}), ("search", {"rule": "blocked", "detail": "count"})]
    for tool, args in reads:
        a, b = asyncio.run(plain.call_tool(tool, args)), asyncio.run(packed.call_tool(tool, args))
        want = json.loads(a.content[0].text)
        sc = lambda r: getattr(r, "structured_content", None) or getattr(r, "structuredContent", None)  # mcp 2.x / 1.x
        assert sc(b) == sc(a), tool
        got = unpack(json.loads(b.content[0].text))
        assert json.dumps(got, sort_keys=True) == re.sub(r'(T\d\d:\d\d:\d\d(\.\d+)?)\+00:00"', r'\1Z"', re.sub(r'T(\d\d:\d\d):00\+00:00"', r'T\1Z"', json.dumps(want, sort_keys=True))), (tool, args)
        assert len(b.content[0].text) <= len(a.content[0].text), (tool, args)
    assert "cols" in instructions(cartridge()) and "cols" not in instructions(cartridge(), compact=False)


def test_a_bound_server_names_the_subjects_of_kinds_with_few(store):
    """A server bound to a tenant names the subjects of a kind that has only a few (a project, a site, an account): an
    agent asked about "the P project" calls related_to='project:P' directly instead of looking the key up first, or
    learns there is only one (evals/jira/requests.md: 171 calls looked a project up or checked membership). Kinds with
    more are named alone. The rules line says search(rule=) lists where a rule holds."""
    pytest.importorskip("mcp")
    from keel.mcp_server import build_server, few_subjects, instructions

    t = Tools(cartridge(), seed(store), T, clock=lambda: NOW)
    assert few_subjects(t) == {"project": ["project:P"]}
    text = build_server(t).instructions
    assert "Kinds: issue, project (project:P)." in text and "search(rule=)" in text
    assert text == instructions(cartridge(), few={"project": ["project:P"]})
    assert "Kinds: issue, project." in instructions(cartridge())  # no tenant, no subjects to name
    for i in range(4):
        store.upsert_entity(T, "project", f"Q{i}", ref=f"jira#Q{i}")
    assert few_subjects(t) == {}


def test_history_tenant_window_marks_first_values_and_concise_leaves_them_out(store):
    """A window over the tenant lists what started in it; a first value (nothing held the predicate right before
    it: an issue created in the window) is marked `initial`, and the concise form, what an agent reads to answer
    "whose status changed", leaves those out and says how many (evals/jira: up to 121 creations around 9 changes,
    counted as changes or burying them). A subject's own history is unchanged."""
    t = tools(store)
    full = t.history(since=d(9, 2), until=d(9, 10))
    assert [(f["subject"], f.get("initial", False)) for f in full] == [
        ("issue:C", True), ("issue:K", True), ("issue:L", True), ("issue:D", True), ("issue:W", False), ("issue:C", False)]
    concise = t.history(since=d(9, 2), until=d(9, 10), detail="concise")
    assert concise[0] == {"initial": {"omitted": 4, "list": "detail='full' lists them, marked initial"}}
    assert [(f["subject"], f["was"]) for f in concise[1:]] == [("issue:W", "On Hold"), ("issue:C", "new")]
    assert [f["subject"] for f in t.history(since=d(9, 6), until=d(9, 10), detail="concise")] == ["issue:W", "issue:C"]  # none to omit
    assert "initial" not in t.history("issue:C")[0]


def test_tool_descriptions_are_short_and_keep_what_agents_rely_on():
    """Every request carries the five descriptions (evals/jira/context_tokens.md: 553 o200k tokens of a 1,672 fixed
    part). They stay near 300 tokens (~4 characters each) and keep the guidance the agents act on: time (as_of,
    as_known_at), the cheap details (key, count), paging (`more`), the search filters and the history windows."""
    from keel.tools import DESCRIPTIONS

    assert sum(len(v) for v in DESCRIPTIONS.values()) <= 1300
    needs = {
        "context": ["as_of", "as_known_at", "since", "depth=1", "brief", "full"],
        "history": ["since/until", "predicate", "was", "as_known_at", "changed_since", "limit", "0: all"],
        "search": ["kind", "rule", "where", "any of", "since", "related_to", "via", "as_of", "as_known_at", "key", "count", "brief",
                   "limit", "after", '{"more"'],
        "annotate": ["evidence", "scope"],
        "related": ["as_of", "depth", "limit", '{"more"'],
    }
    for tool, words in needs.items():
        missing = [w for w in words if w not in DESCRIPTIONS[tool]]
        assert not missing, f"{tool} lost {missing}"
