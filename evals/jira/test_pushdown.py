"""The benchmark's reads with the store's push-down and without it: the same tool outputs, question by question.

For each of the 387 questions (three Public Jira Dataset tenants, ~/datasets/public-jira/export), the tool calls an
agent would make to answer it (context, related, history of the tenant window, search by where / rule, counts and
key listings) are made through Tools on SqliteStore, DuckdbStore and (with KEEL_POSTGRES_URL) PostgresStore, once
with the store's optional capabilities (facts_between, subjects_where, rule_subjects) and once with them hidden;
every output must be identical. No model is called.

    uv run --extra postgres pytest evals/jira/test_pushdown.py -q        # KEEL_POSTGRES_URL=... adds Postgres
"""

# ruff: noqa: B023  (every lambda here is called at once, inside its loop)
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from keel import Event, Ingestor, SqliteStore, Tools, load_cartridge

HERE = Path(__file__).parent
DATA = Path(os.environ.get("KEEL_JIRA_DATA", Path.home() / "datasets" / "public-jira" / "export"))
TENANTS = ("hyperledger", "mariadb", "spring")
PG = os.environ.get("KEEL_POSTGRES_URL")
sys.path.insert(0, str(HERE.parent.parent / "tests"))
from test_pushdown import Plain, Spy, ask  # noqa: E402

pytestmark = pytest.mark.skipif(not all((DATA / f"{t}.questions.json").exists() for t in TENANTS), reason="Public Jira Dataset export not found")


def at(s: str | None) -> datetime | None:
    return None if s is None else datetime.strptime(s, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)


def reads(q: dict[str, Any]) -> list[tuple[str, Any]]:
    """The tool calls an agent makes for a question: (label, fn(tools))."""
    p, kind = q["params"], q["type"]
    t, k = at(p.get("as_of")), at(p.get("known_at"))
    if kind in ("current", "abstain", "as_of", "known_at", "rule"):
        return [("context", lambda x: x.context(p["subject"], as_of=t, as_known_at=k))]
    if kind == "links":
        def links(x: Tools) -> Any:
            near = x.related(p["subject"], p["relation"], as_of=t, limit=0)
            return [near, [x.context(n["subject"], as_of=t, detail="concise") for n in near if "subject" in n]]
        return [("related", links)]
    sub = p["subtype"]
    if sub in ("status_at", "known_at"):
        return [("search_where", lambda x: x.search(kind="issue", where={"status": p["status"]}, as_of=t, as_known_at=k, detail="key", limit=0)),
                ("search_where_count", lambda x: x.search(kind="issue", where={"status": p["status"]}, as_of=t, as_known_at=k, detail="count")),
                ("search_where_page", lambda x: x.search(kind="issue", where={"status": p["status"]}, as_of=t, as_known_at=k, detail="brief", limit=5))]
    if sub == "count_at":
        return [("search_count", lambda x: x.search(kind="issue", where={"status_category": p["category"]}, as_of=t, detail="count"))]
    if sub == "rule_at":
        return [("search_rule", lambda x: x.search(rule=p["rule"], as_of=t, detail="key", limit=0)),
                ("search_rule_count", lambda x: x.search(rule=p["rule"], as_of=t, detail="count")),
                ("search_rule_known", lambda x: x.search(rule=p["rule"], as_of=t, as_known_at=t + timedelta(days=3), detail="count"))]
    a, b = at(p["since"]), at(p["until"])
    return [("history_window", lambda x: x.history(since=a, until=b, predicate=p["field"], limit=0)),
            ("history_window_concise", lambda x: x.history(since=a, until=b, predicate=p["field"], limit=0, detail="concise")),
            ("history_window_known", lambda x: x.history(since=a, until=b, predicate=p["field"], as_known_at=b + timedelta(days=2), limit=0, detail="concise"))]


def open_store(kind: str, tmp_path: Path, tenant: str) -> Any:
    if kind == "sqlite":
        return SqliteStore(tmp_path / f"{tenant}.db")
    if kind == "duckdb":
        from keel.store.duckdb import DuckdbStore

        return DuckdbStore(tmp_path / f"{tenant}.duckdb")
    from keel.store.postgres import PostgresStore

    s = PostgresStore.from_url(PG)
    s._exec("TRUNCATE entities, relations, facts, ingested, forgotten, api_keys, metering, cutoffs, accounts")
    return s


@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("kind", ["sqlite", "duckdb"] + (["postgres"] if PG else []))
def test_every_question_reads_the_same_with_and_without_push_down(kind, tenant, tmp_path):
    c = load_cartridge(HERE)
    events = [Event(**json.loads(line)) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]
    questions = json.loads((DATA / f"{tenant}.questions.json").read_text())
    store = open_store(kind, tmp_path, tenant)
    assert not Ingestor(c, store).ingest(events, strict=True).errors
    now = max(e.recorded_at for e in events) + timedelta(days=1)  # one clock for both ways: `now` reads agree to the byte
    spy = Spy(store)
    pushed, plain = Tools(c, spy, tenant, clock=lambda: now), Tools(c, Plain(store), tenant, clock=lambda: now)
    compared = 0
    for q in questions:
        for label, fn in reads(q):
            got, want = ask(lambda: fn(pushed)), ask(lambda: fn(plain))
            assert got == want, (q["id"], label)
            assert not (isinstance(got, tuple) and got[0] == "error"), (q["id"], label, got)
            compared += 1
    assert len(questions) == 129 and compared >= 129
    assert spy.calls["facts_between"] and spy.calls["subjects_where"]
    assert spy.calls["rule_subjects"] or kind == "sqlite"
