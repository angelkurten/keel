"""Tenant-wide reads pushed down to the store, with the same answers as Tools' Python path.

A store may offer optional capabilities (feature-detected, never part of the Store protocol's required
methods): `facts_between` (the tenant's facts whose valid_from falls in a window), `subjects_where` (the
entities of some kinds matching a `where` filter at an instant, as known then) and `rule_subjects` (a rule's
holders, by its compiled macro). Tools uses them when present; hidden, Tools takes its Python path. Every
test here asks the same store both ways and wants the same bytes back: example cartridges and the Jira
mini-log with late arrivals and corrections, close marks, relation marks, agent and task memory, the global
partition, the agent registry, proposals, expired memory, multi-valued predicates, odd values (case,
non-ASCII, lists, numbers), limit/after/count and `concise`'s omission of first values.
"""

# ruff: noqa: B023  (every lambda here is called at once, inside its loop)
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from keel import Event, Ingestor, SqliteStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.ingest import JsonlAdapter
from keel.scopes import global_of

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
JIRA = ROOT / "evals" / "jira"
EXAMPLES = ["fleet", "crm", "finance", "clinics", "relay"]
CAPS = ("facts_between", "subjects_where", "rule_subjects")
PG = os.environ.get("KEEL_POSTGRES_URL")
STORES = ["sqlite", "duckdb"] + (["postgres"] if PG else [])


class Plain:
    """The same store with its push-down capabilities hidden: Tools takes its Python path."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in CAPS:
            raise AttributeError(name)
        return getattr(self._store, name)


class Spy:
    """The store, counting each capability call (to prove the push-down ran)."""

    def __init__(self, store: Any) -> None:
        self._store, self.calls = store, {c: 0 for c in CAPS}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if name in CAPS:
            def counted(*a: Any, **kw: Any) -> Any:
                self.calls[name] += 1
                return attr(*a, **kw)
            return counted
        return attr


def open_store(kind: str, tmp_path: Path) -> Any:
    if kind == "sqlite":
        return SqliteStore(tmp_path / "keel.db")
    if kind == "duckdb":
        pytest.importorskip("duckdb")
        from keel.store.duckdb import DuckdbStore

        return DuckdbStore(tmp_path / "keel.duckdb")
    from keel.store.postgres import PostgresStore

    s = PostgresStore.from_url(PG)
    for table in ("entities", "relations", "facts", "ingested", "forgotten", "api_keys", "metering", "cutoffs", "accounts"):
        s._exec(f"DELETE FROM {table}")
    return s


def late(events: list[Event], every: int = 3, days: int = 5) -> list[Event]:
    """Every `every`-th event recorded `days` late, delivered in recorded order (as tests/test_sqlgen.py)."""
    out = [e.model_copy(update={"recorded_at": e.occurred_at + timedelta(days=days if i % every == 0 else 0)}) for i, e in enumerate(events)]
    for e in out:
        e.model_fields_set.add("recorded_at")
    return sorted(out, key=lambda e: (e.recorded_at, e.id))


def jira_log() -> list[Event]:
    from test_sqlgen import jira_log as log

    return log()


def thin(xs: list[Any], n: int, seed: int = 7) -> list[Any]:
    if len(xs) <= n:
        return xs
    rng = random.Random(seed)
    return sorted({xs[0], xs[-1], *rng.sample(xs, n - 2)})


AGENTS = {"aide": {"description": "Sees everything.", "writes": ["task", "agent", "tenant"]}}


class Scenario:
    def __init__(self, kind: str, name: str, tmp_path: Path) -> None:
        path = JIRA if name == "jira" else ROOT / "examples" / name
        events = jira_log() if name == "jira" else late(list(JsonlAdapter(path / "events.jsonl")))
        self.tenant = events[0].tenant
        self.c, self.ca = load_cartridge(path), load_cartridge(path)
        for c in (self.c, self.ca):
            c.predicates = {**(c.predicates or {}), "opinion": {"multi": True}}
        kinds = sorted(self.c.entities)
        self.ca.agents = parse_agents({**AGENTS, "bot": {"description": "A slice.", "writes": ["task", "agent"],
                                                           "reads": {"kinds": kinds[: max(1, len(kinds) // 2)]}}})
        self.store = open_store(kind, tmp_path)
        assert not Ingestor(self.c, self.store).ingest(events, strict=True).errors
        self._memory()
        backup = self.store.backup(self.tenant)
        world, tx = set(), set()
        for f in backup["facts"] + backup["relations"]:
            for t in (f.valid_from, f.valid_to):
                if t is not None:
                    world |= {t, t - timedelta(seconds=1), t + timedelta(seconds=1)}
            for t in (f.recorded_at, f.closed_at):
                if t is not None:
                    tx |= {t, t - timedelta(seconds=1)}
        self.world, self.tx = sorted(world), sorted(tx)
        self.facts = [f for f in backup["facts"] if f.source != "keel:close"]
        self.entities = backup["entities"]

    def _memory(self) -> None:
        """Memory and odd values written with the primitives, at instants inside the log, with declared
        transaction times: agent and task memory, a proposal, the global partition, expired memory,
        multi-valued opinions, a close mark, values that differ only in case, non-ASCII text, lists and numbers."""
        s, t = self.store, self.tenant
        facts = [f for f in s.backup(t)["facts"] if f.source != "keel:close"]
        ents = sorted(s.backup(t)["entities"], key=lambda e: e.key)
        lo = min(f.valid_from for f in facts)
        hi = max(f.valid_from for f in facts)
        mid = lo + (hi - lo) / 2
        at = lambda frac: lo + (hi - lo) * frac
        keys = [e.key for e in ents]
        a, b, c = keys[0], keys[len(keys) // 2], keys[-1]
        strings = [f for f in facts if isinstance(f.value, str) and f.value.isascii()]
        if strings:  # the same value in another case, on another subject, and a later correction learned late
            f = strings[len(strings) // 2]
            s.open_fact(t, c, f.predicate, f.value.upper(), at(0.4), source="test", recorded_at=at(0.4))
            s.open_fact(t, c, f.predicate, f.value.lower(), at(0.6), source="test", recorded_at=at(0.9))
            s.close_fact(t, c, f.predicate, at(0.8), recorded_at=at(0.85))
            self.probes = [{f.predicate: f.value.upper()}, {f.predicate: f.value.swapcase()}]
        else:
            self.probes = []
        # instants where the planted memory changes an answer: before/after the close mark is learned, inside the expiry
        self.t_probe = [at(0.5), at(0.82), at(0.92)]
        self.k_probe = [at(0.5), at(0.87), at(0.95)]
        odd = ["straße", "K", ["alpha", "Beta"], 1, True, 2.0, None, "Plain", {"k": "v"}, "\u212a", 0, False, -0.0, 2, "STRASSE", "Ünïcode"]
        for i, v in enumerate(odd):
            s.open_fact(t, keys[i % len(keys)], "label", v, at(0.1 + 0.08 * i), source="test", recorded_at=at(0.1 + 0.08 * i))
        s.open_fact(t, a, "label", "plain", at(0.95), source="test", recorded_at=at(0.97))
        s.open_fact(t, a, "opinion", "agent-only", mid, source="annotate:aide", corrected_by="aide", scope="agent", recorded_at=mid)
        s.open_fact(t, b, "opinion", "task-only", mid, source="annotate:aide", corrected_by="aide", scope="task", task="job-1", recorded_at=mid)
        s.open_fact(t, b, "opinion", "untasked", at(0.55), source="annotate:bot", corrected_by="bot", scope="task", recorded_at=at(0.55))
        s.open_fact(t, c, "opinion", "proposed", mid, source="annotate:aide", corrected_by="aide", status="proposed", recorded_at=mid)
        s.open_fact(t, a, "opinion", "by-one", at(0.3), source="annotate:aide", corrected_by="one", recorded_at=at(0.3))
        s.open_fact(t, a, "opinion", "by-two", at(0.35), source="annotate:aide", corrected_by="two", recorded_at=at(0.5))
        s.open_fact(t, b, "opinion", "expired", at(0.2), source="annotate:aide", corrected_by="aide", recorded_at=at(0.2), expires_at=at(0.7))
        s.open_fact(global_of(t), a, "label", "Plain", at(0.45), source="annotate:root", scope="global", recorded_at=at(0.45))
        s.open_fact(global_of(t), b, "opinion", "global", at(0.5), source="annotate:root", scope="global", recorded_at=at(0.5))
        s.upsert_entity(t, *b.split(":", 1), label="PLAIN", tags=["x", "Plain"], n=1, f=2.0, flag=True, none=None)
        s.upsert_entity(t, *c.split(":", 1), label="straße")
        s.link(t, a, "x_rel", b, at(0.3), recorded_at=at(0.3))
        s.unlink(t, a, "x_rel", b, at(0.3), recorded_at=at(0.6))  # an empty period: a relation mark

    def tools(self, store: Any, agent: str | None = None) -> Tools:
        return Tools(self.ca if agent else self.c, store, self.tenant, agent=agent)


def ask(fn: Any) -> Any:
    try:
        return strip(fn())
    except Exception as exc:  # noqa: BLE001  both ways must fail alike
        return ("error", type(exc).__name__, str(exc))


def strip(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in ("changed_at", "watermark")}
    if isinstance(o, (list, tuple)):
        return [strip(v) for v in o]
    return o


def both(sc: Scenario, call: Any, agent: str | None = None, spy: Spy | None = None) -> None:
    pushed = ask(lambda: call(sc.tools(spy or sc.store, agent)))
    plain = ask(lambda: call(sc.tools(Plain(sc.store), agent)))
    assert pushed == plain


NAMES = EXAMPLES + ["jira"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("kind", STORES)
def test_history_window_pushes_down_with_the_same_output(kind, name, tmp_path):
    sc = Scenario(kind, name, tmp_path)
    spy = Spy(sc.store)
    small = name == "relay"
    w = thin(sc.world, 5 if small else 8)
    windows = [(None, w[1]), (w[0], w[-1]), (w[1], w[-2]), (w[len(w) // 2], None), (w[2], w[2] + timedelta(days=7)), (sc.t_probe[0], sc.t_probe[2])]
    predicates = [None, "opinion", "label", *thin(sorted({f.predicate for f in sc.facts}), 2 if small else 4)]
    knowns = [None, *thin(sc.tx, 2 if small else 4), *sc.k_probe]
    if kind != "sqlite":  # the whole grid on SQLite; every other known instant elsewhere, as in the where test
        knowns = knowns[::2]
    for since, until in windows:
        for p in predicates:
            for k in knowns:
                for detail in ("full", "concise"):
                    for limit in (50, 0, 3):
                        both(sc, lambda t: t.history(since=since, until=until, predicate=p, as_known_at=k, detail=detail, limit=limit), spy=spy)
                for agent, task in ((None, "job-1"), ("aide", None), ("aide", "job-1"), ("bot", None)):
                    both(sc, lambda t: t.history(since=since, until=until, predicate=p, as_known_at=k, task=task, detail="concise", limit=0), agent, spy)
    assert spy.calls["facts_between"]


def wheres(sc: Scenario) -> list[dict[str, Any]]:
    """`where` filters drawn from the data: common string values per predicate and attribute, the same in
    other case, the odd values, a value nobody has, and two entries at once."""
    out: list[dict[str, Any]] = []
    by: dict[str, list[Any]] = {}
    for f in sc.facts:
        if isinstance(f.value, str):
            by.setdefault(f.predicate, []).append(f.value)
    for e in sc.entities:
        for n, v in e.attrs.items():
            if isinstance(v, str):
                by.setdefault(n, []).append(v)
    for n in thin(sorted(by), 6):
        vals = sorted(set(by[n]), key=lambda v: (-by[n].count(v), v))[:2]
        out += [{n: v} for v in vals] + [{n: vals[0].swapcase()}]
    out += [{"label": v} for v in ("STRASSE", "k", "beta", "plain", "PLAIN", "straße", "nobody", 1, True, None, ["x"])]
    # typed wants the store now compares itself: numbers (int against float, bool against 0/1, -0.0), null, text whose
    # casefold differs from lower (ß, the Kelvin sign), non-ASCII text, lists (any of), on facts and on attributes
    out += [{"label": v} for v in (2, 2.0, 0, False, -0.0, 1.0, "\u212a", "Straße", "ünïcode", ["straße", 2], [], ["nobody", None], ["PLAIN", "k"])]
    out += [{"n": 1}, {"n": True}, {"n": 1.0}, {"flag": 1}, {"f": 2}, {"tags": "PLAIN"}, {"tags": ["y", "x"]}, {"none": None}, {"label": "straße", "n": 1}]
    out += sc.probes
    out += [{"opinion": "AGENT-ONLY"}, {"opinion": "task-only"}, {"opinion": "global"}, {"opinion": "by-two"}, {"opinion": "proposed"}]
    if len(out) > 2:
        first, second = next(iter(out[0].items())), next(iter(out[3].items())) if len(out) > 3 else ("label", "plain")
        out.append({first[0]: first[1], second[0]: second[1]})
        out.append({first[0]: first[1], "label": "plain"})
    return out


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("kind", STORES)
def test_search_where_pushes_down_with_the_same_output(kind, name, tmp_path):
    sc = Scenario(kind, name, tmp_path)
    spy = Spy(sc.store)
    small = name == "relay"
    kinds = sorted(sc.c.entities)
    listings = [*([k] for k in kinds), kinds[:2]]
    instants = [None, *thin(sc.world, 2 if small else 4), *sc.t_probe]
    knowns = [None, *thin(sc.tx, 2 if small else 3), *sc.k_probe]
    rng = random.Random(3)
    grid = [(t, k) for t in instants for k in knowns]
    for where in wheres(sc):
        for ks in listings:
            # SQLite walks the whole (as_of, as_known_at) grid; DuckDB and Postgres share its SQL builder and take a
            # seeded sample of 3 (a query costs them ~25x more)
            for t, k in grid if kind == "sqlite" else rng.sample(grid, min(3, len(grid))):
                both(sc, lambda x: x.search(kind=ks, where=where, as_of=t, as_known_at=k, detail="key", limit=0), spy=spy)
            t, k = rng.choice(instants), rng.choice(knowns)  # pages, counts and entities at one (t, k) per listing
            both(sc, lambda x: x.search(kind=ks, where=where, as_of=t, as_known_at=k, detail="count"), spy=spy)
            page = ask(lambda: sc.tools(sc.store).search(kind=ks, where=where, as_of=t, as_known_at=k, detail="key", limit=2))
            after = page[-1] if page and isinstance(page, list) and isinstance(page[-1], str) else None
            both(sc, lambda x: x.search(kind=ks, where=where, as_of=t, as_known_at=k, limit=2, after=after), spy=spy)
            both(sc, lambda x: x.search(kind=ks, where=where, as_of=t, as_known_at=k, detail="brief"), spy=spy)
            for agent in ("aide", "bot"):
                both(sc, lambda x: x.search(kind=ks, where=where, detail="key", limit=0), agent, spy)
                both(sc, lambda x: x.search(kind=ks, where=where, as_of=sc.world[len(sc.world) // 2], detail="count"), agent, spy)
    assert spy.calls["subjects_where"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("kind", STORES)
def test_search_rule_pushes_down_with_counts_pages_and_memory(kind, name, tmp_path):
    sc = Scenario(kind, name, tmp_path)
    spy = Spy(sc.store)
    rules = [r for r, spec in sc.c.rules.items() if spec.get("when") and spec.get("applies_to")]
    for rule in rules:
        for t in [None, *thin(sc.world, 4)]:
            for k in [None, *thin(sc.tx, 3)]:
                both(sc, lambda x: x.search(rule=rule, as_of=t, as_known_at=k, detail="count"), spy=spy)
                keys = ask(lambda: sc.tools(sc.store).search(rule=rule, as_of=t, as_known_at=k, detail="key", limit=0))
                both(sc, lambda x: x.search(rule=rule, as_of=t, as_known_at=k, detail="key", limit=0), spy=spy)
                both(sc, lambda x: x.search(rule=rule, as_of=t, as_known_at=k, limit=1, after=keys[0] if keys else None), spy=spy)
                both(sc, lambda x: x.search(rule=rule, as_of=t, as_known_at=k), spy=spy)
        for agent in ("aide", "bot"):
            both(sc, lambda x: x.search(rule=rule, detail="count"), agent, spy)
    assert spy.calls["rule_subjects"] or not rules


@pytest.mark.parametrize("kind", STORES)
def test_typed_wants_are_compared_by_the_store(kind, tmp_path):
    """Numbers, booleans, null, non-ASCII text and lists of them are compared in SQL too: the store answers (no
    Python sweep), every key it marks certain is one Python's `where` keeps, and every key Python keeps is listed."""
    sc = Scenario(kind, "fleet", tmp_path)
    s, kinds, now = sc.store, sorted(sc.c.entities), datetime.now(UTC)
    for where in ({"label": 1}, {"label": 2}, {"label": 0}, {"label": True}, {"label": False}, {"label": -0.0}, {"label": None},
                  {"label": "straße"}, {"label": "STRASSE"}, {"label": "\u212a"}, {"label": "ÜNÏCODE"}, {"label": ["x", 2, None]},
                  {"label": []}, {"n": 1}, {"flag": 1}, {"none": None}, {"tags": "plain"}, {"label": "straße", "n": True}):
        found = s.subjects_where(sc.tenant, sc.c, kinds, where, now)
        assert found is not None, where
        python = set(sc.tools(Plain(s)).search(kind=kinds, where=where, detail="key", limit=0))
        assert python <= set(found), where
        assert {k for k, sure in found.items() if sure} <= python, where
    sure = s.subjects_where(sc.tenant, sc.c, kinds, {"label": 2}, now)
    assert sure and all(sure.values())  # 2 and 2.0 are both certain: no Python check left


@pytest.mark.parametrize("kind", STORES)
def test_a_where_the_store_cannot_compare_exactly_falls_back(kind, tmp_path):
    """What SQL cannot compare like Python does (NaN, objects, nested lists, a name that is not plain ASCII): the store
    declines and Tools sweeps in Python."""
    sc = Scenario(kind, "fleet", tmp_path)
    s = sc.store
    for where in ({"label": float("nan")}, {"label": float("inf")}, {"label": {"k": "v"}}, {"label": [["x"]]}, {"läbel": "x"}):
        assert s.subjects_where(sc.tenant, sc.c, sorted(sc.c.entities), where, datetime.now(UTC)) is None, where
    assert s.subjects_where(sc.tenant, sc.c, sorted(sc.c.entities), {"label": "plain"}, datetime.now(UTC)) is not None


def test_a_store_whose_values_are_not_json_text_declines_where(tmp_path):
    """MySQL's JSON column hands values back in its own form, so text comparisons in SQL would not be exact."""
    from keel.store.mysql import MysqlStore

    class Normalizing(SqliteStore):
        JSON_TEXT = False

    assert MysqlStore.JSON_TEXT is False
    fleet = load_cartridge(ROOT / "examples" / "fleet")
    assert Normalizing(tmp_path / "k.db").subjects_where("t", fleet, ["tire"], {"status": "x"}, datetime.now(UTC)) is None


def test_a_rule_sqlite_cannot_compile_is_evaluated_in_python(tmp_path):
    """SqliteStore compiles rules per rule: one it cannot compile (an `eq` with a list) declines (None) and Tools
    evaluates it in Python; the others still push down."""
    sc = Scenario("sqlite", "fleet", tmp_path)
    sc.c.rules = {**sc.c.rules, "odd": {"applies_to": "tire", "when": {"predicate": "depth_mm", "eq": [1, 2]}}}
    assert sc.store.rule_subjects(sc.tenant, sc.c, "odd", datetime.now(UTC)) is None
    spy = Spy(sc.store)
    for rule in ("odd", "tire_critical"):
        both(sc, lambda x: x.search(rule=rule, detail="key", limit=0), spy=spy)
    assert spy.calls["rule_subjects"] == 2


def test_sqlite_declines_rules_with_a_table_prefix_or_an_old_library(tmp_path, monkeypatch):
    """The expanded SQL names the unprefixed tables and needs SQLite 3.39: otherwise Tools evaluates rules in Python."""
    import keel.store.sqlite as mod

    assert SqliteStore(tmp_path / "p.db", prefix="keel_").rule_subjects is None
    monkeypatch.setattr(mod.sqlite3, "sqlite_version_info", (3, 31, 1))
    assert SqliteStore(tmp_path / "o.db").rule_subjects is None
    monkeypatch.undo()
    assert SqliteStore(tmp_path / "n.db").rule_subjects is not None
