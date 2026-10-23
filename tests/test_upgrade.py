"""Data that survives upgrades: databases written by released versions open with the current SqliteStore
and answer exactly as a database the current code writes from the same input.

Each tests/upgrade/keel-<version>.db was written by that release from tests/upgrade/events.jsonl and the
annotate calls in tests/upgrade/make.py:

    uv run --isolated --no-project --with keel-context==<version> python -I tests/upgrade/make.py tests/upgrade/keel-<version>.db

Transaction-time bookkeeping (recorded_at, closed_at, changed_at, watermark...) is the clock of the run
that wrote it, so it is compared only where the input pins it: facts set by events that declare recorded_at.
"""

from __future__ import annotations

import importlib.util
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from keel import SqliteStore, Tools
from keel.store.base import Entity, Fact, Relation

HERE = Path(__file__).parent / "upgrade"
DBS = sorted(HERE.glob("keel-*.db"), key=lambda p: tuple(int(n) for n in p.stem.split("-")[1].split(".")))
CLOCK = {"recorded_at", "closed_at", "changed_at", "watermark", "trace", "cartridge", "expires_at", "as_of"}
AS_OF = "2026-01-16T00:00:00+00:00"  # inside the history: before the close, after the unlink


def _make() -> Any:
    spec = importlib.util.spec_from_file_location("keel_upgrade_make", HERE / "make.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


make = _make()
DECLARED = make.declared_ids()  # events whose recorded_at is part of the input


def _pinned(evidence: str | None, source: str | None) -> bool:
    """A fact an event with a declared recorded_at set (an annotation citing that event was written on the clock)."""
    return evidence in DECLARED and not (source or "").startswith("annotate:")


def _strip(o: Any) -> Any:
    """Tool answers without clock values, keeping a fact's recorded_at when its event declared it."""
    if isinstance(o, dict):
        keep = {"recorded_at"} if _pinned(o.get("evidence"), o.get("source")) and "predicate" in o else set()
        return {k: _strip(v) for k, v in o.items() if k not in CLOCK - keep}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


def _graph(store: SqliteStore) -> dict[str, list[tuple[Any, ...]]]:
    ents, rels, facts = [], [], []
    for x in store.dump(make.TENANT):
        if isinstance(x, Entity):
            ents.append((x.key, x.kind, x.ref, sorted(x.attrs.items())))
        elif isinstance(x, Relation):
            rels.append((x.src, x.rel, x.dst, x.valid_from, x.valid_to, x.source))
        elif isinstance(x, Fact):
            rec = x.recorded_at if _pinned(x.evidence, x.source) else None
            facts.append((x.subject, x.predicate, repr(x.value), x.valid_from, x.valid_to, x.scope.value, x.status, x.source,
                          x.evidence, x.task, x.corrected_by, rec))
    return {"entities": sorted(ents), "relations": sorted(rels, key=repr), "facts": sorted(facts, key=repr)}


NOW = datetime.now(timezone.utc)  # one instant for every side: "now" (to the whole second) must not tick between them


def _answers(store: SqliteStore) -> dict[str, Any]:
    tools = Tools(make.load(), store, make.TENANT, agent=make.AGENT, clock=lambda: NOW)  # the author of the agent-scope memory
    out: dict[str, Any] = {}
    for e in store.entities(make.TENANT):
        out[e.key] = {
            "context": _strip(tools.context(e.key, detail="full")),
            "context_as_of": _strip(tools.context(e.key, as_of=AS_OF, detail="full")),
            "history": _strip(tools.history(e.key, detail="full", limit=None)),
        }
    return out


@pytest.fixture
def fresh(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "fresh.db")
    make.build(store)
    return store


def _migrated(db: Path, tmp_path: Path) -> SqliteStore:
    copy = tmp_path / db.name
    shutil.copy(db, copy)
    return SqliteStore(copy)


def test_every_release_is_covered() -> None:
    assert [p.stem for p in DBS] == ["keel-0.8.0", "keel-0.20.0", "keel-0.32.0"]


def test_input_exercises_what_it_claims(fresh: SqliteStore) -> None:
    g = _graph(fresh)
    status = sorted((f for f in g["facts"] if f[:2] == ("machine:M1", "status")), key=lambda f: f[3])
    assert [f[2] for f in status] == ["'idle'", "'running'", "'maintenance'", "'stopped'"]  # the late one slotted in
    assert status[-1][4] is not None  # closed by machine.retired
    assert any(r[1] == "operated_by" and r[4] is not None for r in g["relations"])  # an unlink
    assert {f[5] for f in g["facts"]} == {"tenant", "task", "agent"}
    assert DECLARED and any(f[11] is not None for f in g["facts"])


@pytest.mark.parametrize("db", DBS, ids=lambda p: p.stem)
def test_old_database_answers_as_a_fresh_one(db: Path, tmp_path: Path, fresh: SqliteStore) -> None:
    old = _migrated(db, tmp_path)
    assert _graph(old) == _graph(fresh)
    assert _answers(old) == _answers(fresh)


@pytest.mark.parametrize("db", DBS, ids=lambda p: p.stem)
def test_old_database_takes_writes_after_migration(db: Path, tmp_path: Path, fresh: SqliteStore) -> None:
    old = _migrated(db, tmp_path)
    make.late(old)
    make.late(fresh)
    assert _graph(old) == _graph(fresh)
    assert _answers(old) == _answers(fresh)


@pytest.mark.parametrize("db", DBS, ids=lambda p: p.stem)
def test_old_database_takes_writes_that_meet_no_close(db: Path, tmp_path: Path, fresh: SqliteStore) -> None:
    """A late event and an annotate using columns the migration added (task, idempotency key) land alike."""
    old = _migrated(db, tmp_path)
    at = datetime(2026, 1, 22, tzinfo=timezone.utc)
    for store in (old, fresh):
        make.late(store, make.LATE_OPEN)
        tools = Tools(make.load(), store, make.TENANT, agent=make.AGENT, clock=lambda: at)
        first = tools.annotate("machine:M2", "todo", "belt replaced", scope="task", task="t-7", key="belt")
        assert tools.annotate("machine:M2", "todo", "belt replaced", scope="task", task="t-7", key="belt") == first  # idempotent
    assert _graph(old) == _graph(fresh)
    assert _answers(old) == _answers(fresh)
