"""The per-kind views generated from declared facts (keel/sqlgen.py): `<kind>_state_at`, `<kind>_state_known_at` and
`<kind>_changes`, and the paragraph that documents them for agents (`sqlgen.describe`).

Every example cartridge, with each kind's facts declared from what its log writes, and the Jira cartridge as
committed, ingested into Keel and copied into the generated schema: over a grid of (t, k), one row per entity of
the kind with a fact at t, each declared fact's current value and since as Keel's `context` says, each `one`
relation's target; and over windows, one row per change Keel's tenant `history` reports (a replaced value, the same
one set again included; not a first value, nor a value recorded wrong and corrected).
"""

from __future__ import annotations

import copy as _copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from test_sqlgen import (
    BACKENDS,
    EXAMPLES,
    JIRA,
    ROOT,
    both_stores,
    con_of,
    connect,
    copy,
    events_of,
    instants,
    jira_log,
    late,
    tenant_of,
    thin,
)

from keel import Ingestor, MemoryStore, Tools, load_cartridge, sqlgen
from keel.cartridge import VIEW_NAMES
from keel.store.sql import _ts

UTC = timezone.utc


def declare(c: Any, store: MemoryStore, tenant: str) -> Any:
    """The cartridge with every kind's facts declared from what the log writes (types from the catalog), its
    attributes, and every relation that can be `one` marked so (a relation with several targets shows the latest)."""
    c = _copy.copy(c)
    c.entities = {k: dict(v) if isinstance(v, dict) else {} for k, v in c.entities.items()}
    c.relations = {r: dict(v) for r, v in c.relations.items()}
    c.context = {"relations": 10_000}  # context lists every relation, so the latest is among them
    b = store.backup(tenant)
    taken: dict[str, set[str]] = {k: set() for k in c.entities}
    for kind in c.entities:
        preds = sorted({f.predicate for f in b["facts"] if f.subject.split(":", 1)[0] == kind} - VIEW_NAMES)
        preds = [p for p in preds if p.isidentifier()]
        attrs = sorted({a for e in b["entities"] if e.kind == kind for a in e.attrs if a.isidentifier()} - VIEW_NAMES - set(preds))
        if preds:
            c.entities[kind]["facts"] = preds
            c.entities[kind]["attrs"] = attrs
            taken[kind] = set(preds) | set(attrs)
    for r, spec in c.relations.items():
        if not spec.get("symmetric") and r not in taken.get(spec["from"], set()) and r not in VIEW_NAMES:
            spec["one"] = True
    assert not c.validate() or all("handler" in p for p in c.validate()), c.validate()
    return c


def ts(x: Any) -> datetime | None:
    """A cell holding an instant (TIMESTAMP, or ISO text in SQLite) as an aware UTC datetime."""
    if x is None:
        return None
    if isinstance(x, str):
        x = datetime.fromisoformat(x)
    return x.replace(tzinfo=UTC) if x.tzinfo is None else x.astimezone(UTC)


def decoded(cell: Any, value: Any, cls: str) -> bool:
    """A view's cell holds Keel's value, as its declared type decodes it."""
    if value is None:
        return cell is None
    if cls == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and cell == pytest.approx(float(value)) or (
            not isinstance(value, (int, float)) and cell is None) or (isinstance(value, bool) and cell == float(value))
    if cls == "bool":
        return cell == value if isinstance(value, bool) else cell is None
    if cls == "time":
        return isinstance(value, str) and ts(cell) == ts(value) if cell is not None else not isinstance(value, str)
    if isinstance(value, str):
        return cell == value
    return cell is not None and json.loads(cell) == value


def latest(xs: list[dict[str, Any]], key: str = "valid_from") -> dict[str, Any] | None:
    """The fact (or relation) that started last; None when two start together with different values (ambiguous)."""
    if not xs:
        return None
    top = max(x[key] for x in xs)
    tied = [x for x in xs if x[key] == top]
    return tied[0] if len({json.dumps(x.get("value", x.get("dst")), sort_keys=True) for x in tied}) == 1 else None


def view_rows(con: Any, kind: str, tenant: str, t: datetime, k: datetime | None) -> dict[str, dict[str, Any]]:
    cur = con.execute(f"SELECT * FROM {kind}_state_known_at(?, ?, k := ?)", [tenant, _ts(t), _ts(k)])
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if getattr(cur, "description", None) else None
    if cols is None:  # SQLite's expanded query: the columns in the view's own order
        cols = sqlgen.view_columns(view_cartridge, kind)
    return {r[cols.index("subject")]: dict(zip(cols, r, strict=True)) for r in rows}


view_cartridge: Any = None  # the cartridge view_rows reads its column order from (SQLite returns tuples)


def agree_views(c: Any, store: MemoryStore, con: Any, tenant: str, n_world: int = 10, n_tx: int = 4) -> int:
    global view_cartridge
    view_cartridge = c
    tools = Tools(c, store, tenant)
    world, tx = instants(store, tenant)
    cells = 0
    for kind, facts, attrs, ones in sqlgen.kind_views(c):
        subjects = sorted({e.key for e in store.entities(tenant, kind)} | {f.subject for f in store.backup(tenant)["facts"] if f.subject.split(":", 1)[0] == kind})
        for t in thin(world, n_world):
            for k in [None, *thin(tx, n_tx)]:
                got = view_rows(con, kind, tenant, t, k)
                ctxs = tools.context(subjects, as_of=t, as_known_at=k) if subjects else []
                want = {x["subject"]: x for x in ctxs if x.get("facts")}
                assert set(got) == set(want), (kind, t, k, sorted(set(got) ^ set(want))[:5])
                for s, ctx in want.items():
                    row = got[s]
                    assert row["id"] == s.split(":", 1)[1] and row["ref"] == (ctx["entity"] or {}).get("ref"), (s, row)
                    for a in attrs:
                        v = ((ctx["entity"] or {}).get("attrs") or {}).get(a)
                        assert row[a] == v or (isinstance(row[a], str) and not isinstance(v, str) and json.loads(row[a]) == v), (s, a, row[a], v)
                    for p, spec in facts.items():
                        f = latest([x for x in ctx["facts"] if x["predicate"] == p])
                        if f is None and any(x["predicate"] == p for x in ctx["facts"]):
                            continue  # two values start together: either may show
                        assert decoded(row[p], f and f["value"], spec["cls"]), (s, p, t, k, row[p], f)
                        assert ts(row[f"{p}_since"]) == (f and ts(f["valid_from"])), (s, p, t, k)
                    for rel, to in ones:
                        r = latest([x for x in ctx["relations"] if x["rel"] == rel and x["src"] == s], "valid_from")
                        dst = r and r["dst"]
                        want_id = dst.split(":", 1)[1] if dst and dst.split(":", 1)[0] == to else dst
                        if r is None and any(x["rel"] == rel and x["src"] == s for x in ctx["relations"]):
                            continue
                        assert row[rel] == want_id, (s, rel, t, k, row[rel], want_id)
                    cells += 1
    return cells


def agree_changes(c: Any, store: MemoryStore, con: Any, tenant: str, n_windows: int = 8, n_tx: int = 4) -> int:
    tools = Tools(c, store, tenant)
    world, tx = instants(store, tenant)
    if not world:
        return 0
    lo, hi = world[0], world[-1]
    windows = [(None, None), (lo, hi + timedelta(days=1))] + [(a, b) for a, b in zip(thin(world, n_windows), thin(world, n_windows)[1:], strict=False)]
    seen = 0
    for kind, facts, _, _ in sqlgen.kind_views(c):
        for since, until in windows:
            for k in [None, *thin(tx, n_tx)]:
                rows = con.execute(f"SELECT subject, fact, old_value, new_value, valid_from, recorded_at FROM {kind}_changes(?, ?, ?, k := ?)",
                                   [tenant, _ts(since), _ts(until), _ts(k)]).fetchall()
                got = sorted((s, p, o, n, ts(a), ts(r)) for s, p, o, n, a, r in rows)
                hist = tools.history(since=since or datetime(1970, 1, 1, tzinfo=UTC), until=until or datetime(9999, 1, 1, tzinfo=UTC),
                                     as_known_at=k, limit=0)
                want = sorted((h["subject"], h["predicate"], text(h["was"]), text(h["value"]), ts(h["valid_from"]), ts(h["recorded_at"]))
                              for h in hist if h["subject"].split(":", 1)[0] == kind and h["predicate"] in facts and "was" in h
                              and (h["valid_to"] is None or h["valid_to"] > h["valid_from"])
                              and (until is None or ts(h["valid_from"]) < until))
                assert got == want, (kind, since, until, k, [x for x in got if x not in want][:3], [x for x in want if x not in got][:3])
                seen += len(got)
    return seen


def text(v: Any) -> str | None:
    """A value as the changes view shows it: a string as itself, null as NULL, anything else as JSON."""
    return v if isinstance(v, str) or v is None else json.dumps(v)


def ingest(c: Any, events: list[Any]) -> MemoryStore:
    store = MemoryStore()
    assert not Ingestor(c, store).ingest(events, strict=True).errors
    return store


# ---- the declaration, compiled ----------------------------------------------------------------------------------


def test_jira_declares_its_issue_facts_and_one_relations():
    c = load_cartridge(JIRA)
    assert c.validate() == []
    [(kind, facts, attrs, ones)] = sqlgen.kind_views(c)
    assert kind == "issue" and attrs == ["summary", "reporter"]
    assert list(facts) == ["status", "status_category", "assignee", "priority", "resolution", "issue_type"]
    assert facts["status_category"]["values"] == ["new", "indeterminate", "done"]  # from the event catalog
    assert ones == [("in_project", "project"), ("subtask_of", "issue")]
    assert sqlgen.view_columns(c, "issue") == [
        "subject", "id", "ref", "summary", "reporter", "status", "status_category", "assignee", "priority", "resolution", "issue_type",
        "status_since", "status_category_since", "assignee_since", "priority_since", "resolution_since", "issue_type_since",
        "in_project", "subtask_of"]


def test_a_cartridge_without_declared_facts_compiles_as_before():
    c = load_cartridge(ROOT / "examples" / "fleet")
    assert sqlgen.kind_views(c) == []
    assert "_changes" not in sqlgen.generate(c)


def test_the_jira_issue_view_and_changes_answer_in_duckdb():
    c = load_cartridge(JIRA)
    store = ingest(c, jira_log())
    con = connect(c)
    copy(con, store, "t")
    cur = con.execute("SELECT * FROM issue_state_at('t', TIMESTAMP '2020-01-15 00:00') ORDER BY id")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    assert [r["id"] for r in rows] == ["P-1", "P-2", "P-3"]
    p1 = rows[0]
    assert p1["status"] == "In Progress" and p1["status_category"] == "indeterminate" and p1["in_project"] == "P"
    assert p1["status_since"] == datetime(2020, 1, 10) and p1["summary"] == "P-1 summary" and p1["subtask_of"] is None
    assert rows[2]["subtask_of"] == "P-1" and rows[1]["assignee"] is None
    types = dict(con.execute("SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM issue_state_at('t', '2020-01-15T00:00:00+00:00'))").fetchall())
    assert types["status_since"] == "TIMESTAMP" and types["status"] == "VARCHAR"
    # as recorded by Jan 16, the late change to In Progress was not known yet
    known = con.execute("SELECT status FROM issue_state_known_at('t', '2020-01-15', k := '2020-01-16') WHERE id = 'P-1'").fetchall()
    assert known == [("Open",)]
    assert con.execute("SELECT count(*) FROM issue_state_at('t', now())").fetchall() == [(3,)]  # now, as the paragraph says
    changes = con.execute("SELECT id, fact, old_value, new_value, valid_from, recorded_at FROM issue_changes('t', TIMESTAMP '2020-01-02', "
                          "TIMESTAMP '2020-03-01') ORDER BY id, fact").fetchall()
    assert changes == [
        ("P-1", "status", "Open", "In Progress", datetime(2020, 1, 10), datetime(2020, 1, 20)),
        ("P-1", "status_category", "new", "indeterminate", datetime(2020, 1, 10), datetime(2020, 1, 20)),
        ("P-2", "resolution", None, "Fixed", datetime(2020, 2, 1), datetime(2020, 2, 5)),  # the wrong value never held
        ("P-2", "status", "Open", "Closed", datetime(2020, 2, 1), datetime(2020, 2, 1)),
        ("P-2", "status_category", "new", "done", datetime(2020, 2, 1), datetime(2020, 2, 1)),
    ]
    # as recorded by Feb 2: the wrong value held, and the late change was known
    wrong = con.execute("SELECT new_value FROM issue_changes('t', '2020-01-02', '2020-03-01', k := '2020-02-02') WHERE fact = 'resolution'").fetchall()
    assert wrong == [("Won't Fix",)]


# ---- equivalence with Keel ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", EXAMPLES + ["jira"])
@pytest.mark.parametrize("timing", ["as_ingested", "late"])
def test_kind_views_and_changes_answer_like_keel(name, timing, backend):
    if name == "jira":
        c, events, tenant = load_cartridge(JIRA), jira_log(), "t"
    else:
        c, events = load_cartridge(ROOT / "examples" / name), events_of(name)
        tenant = tenant_of(events)
    if timing == "late":
        events = late(events)
    store = ingest(c, events)
    if name != "jira":
        c = declare(c, store, tenant)
    con = connect(c, backend)
    copy(con, store, tenant)
    relay = name == "relay"
    assert agree_views(c, store, con, tenant, n_world=5 if relay else 12, n_tx=2 if relay else 5) > 0
    assert agree_changes(c, store, con, tenant, n_windows=4 if relay else 8, n_tx=2 if relay else 4) > 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_store_installs_the_views_and_they_answer_like_keel(backend):
    """Ingested straight into the DuckDB, SQLite or Postgres store (no copy), the installed views still answer like Keel."""
    c, ref, store = both_stores("jira", late(jira_log()), load_cartridge(JIRA), backend)
    store.install(c)
    assert agree_views(c, ref, con_of(store), "t") > 0
    assert agree_changes(c, ref, con_of(store), "t") > 0


def test_jira_changes_are_found():
    c = load_cartridge(JIRA)
    store = ingest(c, late(jira_log()))
    con = connect(c)
    copy(con, store, "t")
    assert agree_changes(c, store, con, "t") > 0


# ---- the paragraph ------------------------------------------------------------------------------------------------


def test_describe_documents_every_generated_view_column_relation_and_rule():
    c = load_cartridge(JIRA)
    text = sqlgen.describe(c, "acme")
    assert text == sqlgen.describe(c, "acme")  # deterministic
    assert "'acme'" in text
    for name in ["issue_state_at", "issue_state_known_at", "issue_changes", "relations_at", "<fact>_since",
                 *sqlgen.view_columns(c, "issue")[:11], "in_project", "subtask_of", "old_value", "new_value"]:
        assert name in text, name
    for rule, spec in c.rules.items():
        assert f"{rule}_at" in text and spec["definition"] in text, rule
    for rel in c.relations:
        assert rel in text, rel
    assert "new, indeterminate, done" in text  # the enum, from the catalog
    assert "indeterminate means in progress" in text  # a declared description


@pytest.mark.parametrize("backend", [b for b in BACKENDS if b != "sqlite"])
def test_views_take_a_timestamp_or_iso_text_whatever_the_session_time_zone(backend):
    """An agent writes TIMESTAMP '2020-01-10 00:00' (UTC) or ISO text, for t and k alike, in a session in another zone."""
    c = load_cartridge(JIRA)
    con = connect(c, backend)
    copy(con, ingest(c, jira_log()), "t")
    con.execute("SET TimeZone = 'America/Bogota'")
    one = lambda sql: con.execute(sql).fetchall()
    # P-1 went in progress on Jan 10 00:00 UTC (recorded Jan 20): a second before, it was Open
    for t, want in (("TIMESTAMP '2020-01-09 23:59:59'", "Open"), ("TIMESTAMP '2020-01-10 00:00:00'", "In Progress"),
                    ("'2020-01-09T23:59:59+00:00'", "Open"), ("'2020-01-10T00:00:00Z'", "In Progress"), ("'2020-01-10 00:00'", "In Progress")):
        assert one(f"SELECT status FROM issue_state_at('t', {t}) WHERE id = 'P-1'") == [(want,)], t
    assert one("SELECT status FROM issue_state_known_at('t', TIMESTAMP '2020-01-15', k := TIMESTAMP '2020-01-16') WHERE id = 'P-1'") == [("Open",)]
    assert one("SELECT count(*) FROM issue_changes('t', TIMESTAMP '2020-01-10', TIMESTAMP '2020-01-10 00:00:01')") == [(2,)]
    assert one("SELECT count(*) FROM issue_changes('t', NULL, NULL)") == [(5,)]
