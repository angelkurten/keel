"""The compact MCP text (`build_server(compact=True)`, `keel serve --compact`) answers the benchmark as the JSON does.

For each of the 387 questions (three Public Jira Dataset tenants, ~/datasets/public-jira/export), the reads an agent
makes to answer it go through two MCP servers on one graph, plain and compact. The compact text must unpack to the
plain answer (times written short, the same instants) and must cost fewer tokens; and the gold answer, derived by one
deterministic reader from each, must come out the same from both, and right wherever the plain answer gives it.
No model is called.

    uv run --extra mcp pytest evals/jira/test_compact.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge
from keel.mcp_server import build_server, unpack

HERE = Path(__file__).parent
DATA = Path(os.environ.get("KEEL_JIRA_DATA", Path.home() / "datasets" / "public-jira" / "export"))
TENANTS = ("hyperledger", "mariadb", "spring")
LINKS = {"blocks": "in", "relates_to": "any", "duplicates": "any", "depends_on": "out", "clones": "any"}  # questions.LINK_ASKS

pytestmark = pytest.mark.skipif(not all((DATA / f"{t}.questions.json").exists() for t in TENANTS), reason="Public Jira Dataset export not found")


def short(text: str) -> str:
    """The plain text with its times as pack writes them."""
    text = re.sub(r'T(\d\d:\d\d):00\+00:00"', r'T\1Z"', text)
    return re.sub(r'(T\d\d:\d\d:\d\d(?:\.\d+)?)\+00:00"', r'\1Z"', text)


def ids(keys: list[Any]) -> list[str]:
    return sorted({k.split(":", 1)[1] for k in keys if isinstance(k, str)})


def answer(q: dict[str, Any], read: Any) -> Any:
    """The gold answer from what the tools return: read(tool, args) gives a tool's answer as JSON."""
    p, kind = q["params"], q["type"]
    t, k = p.get("as_of"), p.get("known_at")
    when = {"as_of": t} if t else {}
    if kind in ("current", "abstain", "as_of", "known_at"):
        ctx = read("context", {"subject": p["subject"], **when, **({"as_known_at": k} if k else {})})
        return ctx.get("facts", {}).get(p["predicate"])
    if kind == "rule":
        return read("context", {"subject": p["subject"], **when})["evaluation"][p["rule"]]
    if kind == "links":
        near = [n for n in read("related", {"subject": p["subject"], "relation": p["relation"], **when, "limit": 0}) if "subject" in n]
        near = [n["subject"] for n in near if LINKS[p["relation"]] == "any" or n["direction"] in (LINKS[p["relation"]], "both")]
        done = {n: read("context", {"subject": n, **when})["facts"].get("status_category") == "done" for n in near}
        return ids([n for n in near if done[n] == p["done"]])
    sub = p["subtype"]
    if sub in ("status_at", "known_at"):
        return ids(read("search", {"kind": "issue", "where": {"status": p["status"]}, **when, **({"as_known_at": k} if k else {}), "detail": "key", "limit": 0}))
    if sub == "count_at":
        return read("search", {"kind": "issue", "where": {"status_category": p["category"]}, **when, "detail": "count"})[0]["count"]
    if sub == "rule_at":
        return ids(read("search", {"rule": p["rule"], **when, "detail": "key", "limit": 0}))
    rows = read("history", {"since": p["since"], "until": p["until"], "predicate": p["field"], "limit": 0})
    return ids([f["subject"] for f in rows if "was" in f and not f.get("evidence", "").endswith(":wrong")])


@pytest.mark.parametrize("tenant", TENANTS)
def test_compact_text_answers_every_question_like_the_json(tenant):
    try:
        import tiktoken

        count = lambda s: len(tiktoken.get_encoding("o200k_base").encode(s))
    except ImportError:  # chars/4 without tiktoken
        count = lambda s: len(s) // 4
    c = load_cartridge(HERE)
    events = [Event(**json.loads(line)) for line in (DATA / f"{tenant}.bitemporal.jsonl").open()]
    questions = json.loads((DATA / f"{tenant}.questions.json").read_text())
    store = MemoryStore()
    assert not Ingestor(c, store).ingest(events, strict=True).errors
    now = max(e.recorded_at for e in events) + timedelta(days=1)
    tools = Tools(c, store, tenant, clock=lambda: now)
    plain, packed = build_server(tools, compact=False), build_server(tools)
    spent = {"plain": 0, "compact": 0}

    def reader(server: Any, name: str) -> Any:
        def read(tool: str, args: dict[str, Any]) -> Any:
            text = asyncio.run(server.call_tool(tool, args)).content[0].text
            spent[name] += count(text)
            if name == "plain":
                return json.loads(text)
            twin = asyncio.run(plain.call_tool(tool, args)).content[0].text
            got = unpack(json.loads(text))
            assert got == json.loads(short(twin)), (tool, args)
            return got
        return read

    right, missed = 0, []
    for q in questions:
        a, b = answer(q, reader(plain, "plain")), answer(q, reader(packed, "compact"))
        assert a == b, q["id"]
        right += a == q["answer"]
        missed += [] if a == q["answer"] else [(q["id"], a, q["answer"])]
    assert len(questions) == 129
    assert right == 129, missed
    assert spent["compact"] < spent["plain"]
    print(f"\n{tenant}: {right}/129 gold from both; tokens plain {spent['plain']:,}, compact {spent['compact']:,}; missed {missed}")


def test_short_times_are_the_same_instants():
    from keel.envelope import parse_iso

    for s in ("2026-10-03T08:00:00+00:00", "2026-10-03T08:00:13+00:00", "2026-10-26T00:46:49.050634+00:00"):
        assert parse_iso(short(f'"{s}"').strip('"')) == parse_iso(s) == datetime.fromisoformat(s).astimezone(timezone.utc)
