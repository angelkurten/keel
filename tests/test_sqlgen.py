"""The cartridge compiled to SQL (keel/sqlgen.py): bitemporal tables, point-in-time macros and one macro per rule.

Every example cartridge is ingested into Keel (MemoryStore) and its rows copied into the generated DuckDB schema;
then, over a grid of (subject, as_of, as_known_at), the generated macros must return the facts Keel's `context`
returns and the verdicts Keel's rule evaluation gives, and each rule macro the subjects `search(rule=)` lists.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge, sqlgen
from keel.ingest import JsonlAdapter
from keel.store.sql import INSERT_FACT, INSERT_RELATION, _fact_row, _relation_row, _ts

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ["fleet", "crm", "finance", "clinics", "relay"]


# ---- helpers: Keel on one side, the generated schema on the other ------------------------------------------


def connect(cartridge: Any) -> Any:
    con = duckdb.connect()
    con.execute(sqlgen.generate(cartridge))
    return con


def copy(con: Any, store: MemoryStore, tenant: str) -> None:
    """The tenant's rows as Keel stored them (close marks included), into the generated tables."""
    b = store.backup(tenant)
    for e in b["entities"]:
        con.execute("INSERT INTO entities (tenant, kind, id, ref, attrs) VALUES (?, ?, ?, ?, ?)", (e.tenant, e.kind, e.id, e.ref, json.dumps(e.attrs)))
    for r in b["relations"]:
        con.execute(INSERT_RELATION, _relation_row(r))
    for f in b["facts"]:
        con.execute(INSERT_FACT, _fact_row(f))


def instants(store: MemoryStore, tenant: str) -> tuple[list[datetime], list[datetime]]:
    """World instants (every valid_from / valid_to, a second either side) and transaction instants (every
    recorded_at / closed_at, a second either side): where an answer can change."""
    b = store.backup(tenant)
    world, tx = set(), set()
    for f in b["facts"] + b["relations"]:
        for t in (f.valid_from, f.valid_to):
            if t is not None:
                world |= {t, t - timedelta(seconds=1), t + timedelta(seconds=1)}
        for t in (f.recorded_at, f.closed_at):
            if t is not None:
                tx |= {t, t - timedelta(seconds=1), t + timedelta(seconds=1)}
    return sorted(world), sorted(tx)


def thin(xs: list[Any], n: int, seed: int = 7) -> list[Any]:
    """At most n of xs, deterministically (the first and last always)."""
    if len(xs) <= n:
        return xs
    rng = random.Random(seed)
    return sorted({xs[0], xs[-1], *rng.sample(xs, n - 2)})


def sql_facts(con: Any, tenant: str, t: datetime, k: datetime | None) -> dict[str, list[tuple[Any, ...]]]:
    """Every subject's facts at t as known at k, by subject."""
    rows = con.execute("SELECT subject, predicate, value, valid_from, valid_to FROM state_known_at(?, ?, k := ?)", [tenant, _ts(t), _ts(k)]).fetchall()
    out: dict[str, list[tuple[Any, ...]]] = {}
    for s, p, v, a, b in rows:
        out.setdefault(s, []).append((p, json.loads(v), datetime.fromisoformat(a), b and datetime.fromisoformat(b)))
    return {s: sorted(fs) for s, fs in out.items()}


def keel_facts(ctx: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sorted((f["predicate"], f["value"], datetime.fromisoformat(f["valid_from"]), f["valid_to"] and datetime.fromisoformat(f["valid_to"]))
                  for f in ctx["facts"])


def sql_verdicts(con: Any, cartridge: Any, tenant: str, t: datetime, k: datetime | None) -> dict[str, dict[str, Any]]:
    """Every rule's verdict for every subject of its kind at t as known at k: {subject: {rule: verdict}}."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in cartridge.rules.items():
        if spec.get("when"):
            for s, v in con.execute(f"SELECT subject, verdict FROM {sqlgen.rule_macro(name)}_verdicts(?, ?, k := ?)", [tenant, _ts(t), _ts(k)]).fetchall():
                out.setdefault(s, {})[name] = v
    return out


def sql_rule(con: Any, name: str, tenant: str, t: datetime, k: datetime | None) -> list[str]:
    return sorted(r[0] for r in con.execute(f"SELECT subject FROM {sqlgen.rule_macro(name)}_at(?, ?, k := ?)", [tenant, _ts(t), _ts(k)]).fetchall())


def agree(cartridge: Any, store: MemoryStore, con: Any, tenant: str, n_world: int = 12, n_tx: int = 6, n_subjects: int = 200) -> int:
    """Assert Keel and the generated macros agree over the grid; return how many (subject, t, k) cells were compared."""
    world, tx = instants(store, tenant)
    tools = Tools(cartridge, store, tenant)
    subjects = thin(sorted({e.key for e in store.entities(tenant)} | {f.subject for f in store.backup(tenant)["facts"]}), n_subjects)
    cells = 0
    for t in thin(world, n_world):
        for k in [None, *thin(tx, n_tx)]:
            facts, verdicts = sql_facts(con, tenant, t, k), sql_verdicts(con, cartridge, tenant, t, k)
            for s in subjects:
                ctx = tools.context(s, as_of=t, as_known_at=k)
                if ctx.get("found") is False:
                    assert facts.get(s, []) == [], (s, t, k)
                    continue
                assert facts.get(s, []) == keel_facts(ctx), (s, t, k)
                assert verdicts.get(s, {}) == ctx["evaluation"], (s, t, k)
                cells += 1
            for name, spec in cartridge.rules.items():
                if spec.get("when") and spec.get("applies_to"):
                    keel = sorted(tools.search(rule=name, as_of=t, as_known_at=k, detail="key", limit=0))
                    assert sql_rule(con, name, tenant, t, k) == keel, (name, t, k)
    return cells


def late(events: list[Event], every: int = 3, days: int = 5) -> list[Event]:
    """The same log with transaction time: every `every`-th event is recorded `days` late, the rest on time;
    delivered in recorded order (a late arrival lands after what happened later)."""
    out = [e.model_copy(update={"recorded_at": e.occurred_at + timedelta(days=days if i % every == 0 else 0)}) for i, e in enumerate(events)]
    for e in out:
        e.model_fields_set.add("recorded_at")
    return sorted(out, key=lambda e: (e.recorded_at, e.id))


def events_of(name: str) -> list[Event]:
    return list(JsonlAdapter(ROOT / "examples" / name / "events.jsonl"))


def tenant_of(events: list[Event]) -> str:
    return events[0].tenant


# ---- the generator ------------------------------------------------------------------------------------------


def test_the_generator_names_nothing_of_any_domain():
    """Rule 6: the core is domain-agnostic. No kind, relation, predicate, rule or event type of any cartridge in sqlgen.py."""
    source = (ROOT / "keel" / "sqlgen.py").read_text()
    core = {"status", "source", "value", "scope", "task", "subject", "predicate", "tenant", "id", "kind", "ref", "attrs", "evidence", "account"}  # the core's own columns
    for path in [ROOT / "examples" / n / "cartridge.yaml" for n in EXAMPLES] + [ROOT / "evals" / "jira" / "cartridge.yaml"]:
        c = load_cartridge(path.parent, handlers=False)
        names = set(c.entities) | set(c.relations) | set(c.rules) | set(c.events)
        for spec in c.rules.values():
            names |= set(re.findall(r"'predicate': '(\w+)'", str(spec.get("when"))))
        for name in sorted(names - core):
            assert not re.search(rf"\b{re.escape(name)}\b", source), f"{name} ({path.parent.name}) appears in keel/sqlgen.py"


def test_generate_emits_tables_state_macros_and_one_macro_per_rule():
    c = load_cartridge(ROOT / "examples" / "fleet")
    sql = sqlgen.generate(c)
    for name in ("facts", "relations", "entities", "state_at", "state_known_at", "relations_at", "relations_known_at"):
        assert name in sql
    for name, spec in c.rules.items():
        assert (f"{sqlgen.rule_macro(name)}_at" in sql) == bool(spec.get("when")), name
    con = connect(c)  # it runs
    assert con.execute("SELECT count(*) FROM facts").fetchone() == (0,)


def test_an_uncompilable_construct_is_named():
    c = load_cartridge(ROOT / "examples" / "fleet")
    c.rules = {"odd": {"applies_to": "tire", "when": {"predicate": "depth_mm", "eq": [1, 2]}}}
    with pytest.raises(NotImplementedError, match="eq"):
        sqlgen.generate(c)


# ---- correctness against Keel ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
@pytest.mark.parametrize("timing", ["as_ingested", "late"])
def test_generated_macros_answer_like_keel(name, timing):
    c = load_cartridge(ROOT / "examples" / name)
    events = events_of(name)
    if timing == "late":
        events = late(events)
    store = MemoryStore()
    assert not Ingestor(c, store).ingest(events, strict=True).errors
    tenant = tenant_of(events)
    con = connect(c)
    copy(con, store, tenant)
    relay = name == "relay"  # two thousand events: a sample of subjects and instants
    assert agree(c, store, con, tenant, n_world=8 if relay else 14, n_tx=3 if relay else 6, n_subjects=60 if relay else 200) > 0


def test_late_arrival_in_every_arrival_order():
    """tests/test_store.py's late arrival: A from Sep 1, B from Oct 15 (learned Oct 16), C from Oct 1 learned Oct 20."""
    c = load_cartridge(ROOT / "examples" / "fleet")
    d = lambda month, day, hour=0: datetime(2026, month, day, hour, tzinfo=UTC)
    ev = lambda tenant, occurred, recorded, depth, eid: Event(id=eid, tenant=tenant, type="tire.depth_measured", subject="tire:T-9", occurred_at=occurred,
                                                              recorded_at=recorded, source="b", payload={"unit_id": "U1", "position": "P1", "depth_mm": depth})
    rows = [(d(9, 1, 8), d(9, 1, 8), 4.2, "a"), (d(10, 15), d(10, 16, 10), 3.5, "b"), (d(10, 1), d(10, 20, 10), 3.87, "c")]
    for n, order in enumerate(itertools.permutations(rows)):
        tenant = f"order-{n}"
        store = MemoryStore()
        Ingestor(c, store).ingest([ev(tenant, *r) for r in order], strict=True)
        con = connect(c)
        copy(con, store, tenant)
        for known in (d(10, 17, 12), d(10, 18, 12), d(10, 21, 12), None):
            for at in (d(9, 15), d(10, 10), d(10, 17, 12)):
                got = [v for p, v, *_ in sql_facts(con, tenant, at, known).get("tire:T-9", []) if p == "depth_mm"]
                want = [f.value for f in store.facts(tenant, "tire:T-9", at=at, known_at=known) if f.predicate == "depth_mm"]
                assert got == want and len(got) == 1, (order, known, at, got, want)
        agree(c, store, con, tenant)


JIRA = ROOT / "evals" / "jira"


def jira_log() -> list[Event]:
    """A small Jira log: a late arrival, a correction, a link that ends, a sub-task, a stalled issue."""
    def ev(id, type, key, occurred, payload, recorded=None):
        return Event(id=id, tenant="t", type=type, subject=f"issue:{key}", source="jira", payload=payload,
                     occurred_at=f"{occurred}+00:00", recorded_at=f"{recorded or occurred}+00:00")

    def created(key, at, status="Open", cat="new", **kw):
        p = {"project": "P", "summary": f"{key} summary", "issue_type": "Bug", "status": status, "status_category": cat,
             "priority": "Major", "assignee": "user-a", "reporter": "user-r", "resolution": None, "parent": None} | kw
        return ev(f"jira:t:{key}:created", "issue.created", key, at, p)

    log = [
        created("P-1", "2020-01-01T00:00:00"),
        created("P-2", "2020-01-01T00:00:00", priority="Critical", assignee=None),
        created("P-3", "2020-01-01T00:00:00", parent="P-1"),
        ev("jira:t:P-1:h1", "issue.status_changed", "P-1", "2020-01-10T00:00:00",
           {"status": "In Progress", "status_category": "indeterminate", "by": None}, recorded="2020-01-20T00:00:00"),
        ev("jira:t:P-2:h2:wrong", "issue.resolution_changed", "P-2", "2020-02-01T00:00:00", {"resolution": "Won't Fix", "by": None}),
        ev("jira:t:P-2:h2", "issue.resolution_changed", "P-2", "2020-02-01T00:00:00", {"resolution": "Fixed", "by": None}, recorded="2020-02-05T00:00:00"),
        ev("jira:t:P-2:l1", "issue.linked", "P-2", "2020-01-05T00:00:00", {"relation": "blocks", "other": "P-1"}),
        ev("jira:t:P-2:l2", "issue.unlinked", "P-2", "2020-03-01T00:00:00", {"relation": "blocks", "other": "P-1"}),
        ev("jira:t:P-2:h3", "issue.status_changed", "P-2", "2020-02-01T00:00:00", {"status": "Closed", "status_category": "done", "by": None}),
        ev("jira:t:P-3:l3", "issue.linked", "P-3", "2020-01-02T00:00:00", {"relation": "relates_to", "other": "P-2"}),
    ]
    return sorted(log, key=lambda e: (e.recorded_at, e.id))


def test_jira_cartridge_macros_answer_like_keel():
    c = load_cartridge(JIRA)
    store = MemoryStore()
    assert not Ingestor(c, store).ingest(jira_log(), strict=True).errors
    con = connect(c)
    copy(con, store, "t")
    assert agree(c, store, con, "t", n_world=40, n_tx=12) > 0
    # the rules as the hand-written baseline states them
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)
    assert sql_rule(con, "blocked", "t", at("2020-01-15"), None) == ["issue:P-1"]
    assert sql_rule(con, "blocked", "t", at("2020-02-15"), None) == []
    assert sql_rule(con, "stalled", "t", at("2020-02-08"), None) == []
    assert sql_rule(con, "stalled", "t", at("2020-02-10"), None) == ["issue:P-1"]
    assert sql_rule(con, "urgent_unassigned", "t", at("2020-01-15"), None) == ["issue:P-2"]
    assert sql_rule(con, "urgent_unassigned", "t", at("2020-02-15"), None) == []


def test_rule_language_constructs_answer_like_keel():
    """Constructs no example uses: ci, in, ne, ordering on strings, not, nested via with all, held_for and
    business days with holidays, a missing predicate (null), a type mismatch (null)."""
    c = load_cartridge(ROOT / "examples" / "fleet")
    c.calendar = type(c.calendar).parse({"weekend": ["saturday", "sunday"], "holidays": ["2026-09-16"]})
    c.rules = {
        "ci_in": {"applies_to": "ticket", "when": {"predicate": "status", "in": ["OPEN", "Closed"], "ci": True}},
        "ne_status": {"applies_to": "ticket", "when": {"predicate": "status", "ne": "closed"}},
        "str_order": {"applies_to": "ticket", "when": {"predicate": "status", "gte": "m"}},
        "mismatch": {"applies_to": "tire", "when": {"predicate": "depth_mm", "lt": "3"}},
        "missing": {"applies_to": "tire", "when": {"not": {"predicate": "nonexistent", "eq": 1}}},
        "held": {"applies_to": "tire", "when": {"predicate": "depth_mm", "lt": 6, "held_for_days": {"gte": 3}}},
        "biz": {"applies_to": "ticket", "when": {"predicate": "status", "open_for_business_days": {"gte": 2}}},
        "nested": {"applies_to": "unit", "when": {"any": [{"via": "mounted_on", "direction": "in", "all": {"predicate": "depth_mm", "gt": 1}},
                                                           {"not": {"via": "has_ticket", "direction": "out", "any": {"predicate": "status", "eq": "open"}}}]}},
        "within": {"applies_to": "document", "when": [{"predicate": "expires_on", "within_days": 30}, {"not": {"predicate": "expires_on", "within_days": 1}}]},
    }
    for timing in ("as_ingested", "late"):
        events = events_of("fleet") if timing == "as_ingested" else late(events_of("fleet"), every=2, days=3)
        store = MemoryStore()
        Ingestor(c, store).ingest(events, strict=True)
        con = connect(c)
        copy(con, store, tenant_of(events))
        agree(c, store, con, tenant_of(events), n_world=30, n_tx=8)


# ---- option A: the DuckDB store, ingesting through Keel, and search(rule=) pushed down to the rule macro ----------


def both_stores(name: str, events: list[Event], cartridge: Any = None) -> tuple[Any, MemoryStore, Any]:
    """The same log (with declared transaction times, so both stores hold the same instants) in Keel's reference
    store and in the DuckDB store."""
    from keel.store.duckdb import DuckdbStore

    c = cartridge or load_cartridge(ROOT / "examples" / name)
    ref, duck = MemoryStore(), DuckdbStore()
    for s in (ref, duck):
        assert not Ingestor(c, s).ingest(events, strict=True).errors
    return c, ref, duck


@pytest.mark.parametrize("name", EXAMPLES)
def test_the_duckdb_store_ingests_into_the_generated_schema(name):
    """Ingested straight into the DuckDB store (no copy), the generated macros still answer like Keel."""
    c, ref, duck = both_stores(name, late(events_of(name)))
    duck.install(c)
    relay = name == "relay"
    assert agree(c, ref, duck.db, tenant_of(events_of(name)), n_world=6 if relay else 10, n_tx=2 if relay else 4, n_subjects=40 if relay else 200) > 0


def sweep(tools: Tools, cartridge: Any, t: datetime, k: datetime | None) -> dict[str, Any]:
    """Every way search(rule=) lists: all of it, a page, the page after, the count, with entities."""
    out = {}
    for name, spec in cartridge.rules.items():
        if spec.get("when") and spec.get("applies_to"):
            keys = tools.search(rule=name, as_of=t, as_known_at=k, detail="key", limit=0)
            out[name] = (keys, tools.search(rule=name, as_of=t, as_known_at=k, limit=2),
                         tools.search(rule=name, as_of=t, as_known_at=k, detail="key", limit=2, after=keys[0] if keys else None),
                         tools.search(rule=name, as_of=t, as_known_at=k, detail="count"))
    return out


@pytest.mark.parametrize("name", EXAMPLES + ["jira"])
def test_search_by_rule_pushes_down_to_the_rule_macro_with_the_same_results(name, monkeypatch):
    from keel.store.duckdb import DuckdbStore

    if name == "jira":
        c, ref, duck = both_stores(name, jira_log(), load_cartridge(JIRA))
        tenant = "t"
    else:
        events = late(events_of(name))
        c, ref, duck = both_stores(name, events)
        tenant = tenant_of(events)
    pushed: list[str] = []
    real = DuckdbStore.rule_subjects
    monkeypatch.setattr(DuckdbStore, "rule_subjects", lambda self, *a, **kw: pushed.append(a[2]) or real(self, *a, **kw))
    a, b = Tools(c, ref, tenant), Tools(c, duck, tenant)
    world, tx = instants(ref, tenant)
    for t in thin(world, 10):
        for k in [None, *thin(tx, 4)]:
            assert _strip(sweep(a, c, t, k)) == _strip(sweep(b, c, t, k)), (t, k)
    assert pushed or not any(spec.get("when") and spec.get("applies_to") for spec in c.rules.values())


def test_search_by_rule_falls_back_to_python_when_it_cannot_push_down(monkeypatch):
    """A text or `where` filter: evaluated in Python, as before, with the same answer."""
    from keel.store.duckdb import DuckdbStore

    c, ref, duck = both_stores("fleet", late(events_of("fleet")))
    monkeypatch.setattr(DuckdbStore, "rule_subjects", lambda *a, **kw: pytest.fail("pushed down"))
    a, b = Tools(c, ref, "acme-logistics"), Tools(c, duck, "acme-logistics")
    for kw in ({"rule": "tire_critical", "text": "T-9"}, {"rule": "tire_critical", "where": {"position": "P1"}}):
        assert _strip(a.search(**kw)) == _strip(b.search(**kw))


def test_push_down_sees_memory_as_the_caller_does():
    """A rule reads agent memory only for its author and task memory only for its task, pushed down or not."""
    c = load_cartridge(ROOT / "examples" / "fleet")
    c.rules = {**c.rules, "flagged": {"applies_to": "tire", "when": {"predicate": "flag", "eq": "yes"}}}
    c, ref, duck = both_stores("fleet", late(events_of("fleet")), c)
    for s in (ref, duck):
        Tools(c, s, "acme-logistics", agent="bot-a").annotate("tire:T-9001", "flag", "yes", scope="agent")
        Tools(c, s, "acme-logistics", agent="bot-b").annotate("tire:T-9002", "flag", "yes", scope="task", task="job-1")
    for agent in (None, "bot-a", "bot-b"):
        got = [Tools(c, s, "acme-logistics", agent=agent).search(rule="flagged", detail="key") for s in (ref, duck)]
        assert got[0] == got[1], (agent, got)
    assert Tools(c, duck, "acme-logistics", agent="bot-a").search(rule="flagged", detail="key") == ["tire:T-9001"]


def _strip(o: Any) -> Any:  # the ingest clock (changed_at, watermark) differs between the two stores
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in ("changed_at", "watermark")}
    if isinstance(o, (list, tuple)):
        return [_strip(v) for v in o]
    return o
