"""TDBench (Kim et al., ICLR 2026) through Keel: a small external check of valid-time reasoning on someone else's data.

Its leaders and organizations tables become one tenant (cartridge.yaml: a term opens a holder at Start and closes it
at End), and a stratified sample of its questions on them (Allen-relation "basic" questions and "now" questions) is
asked of an agent three ways:

    keel     the five tools over that graph
    open     the office's rows pasted into the prompt (TDBench's open-book setting: retrieval done perfectly for the model)
    table    both whole tables pasted into the prompt (569 terms): what a model faces without a retrieval step
    closed   no tools (its closed-book setting; the facts are public, so models know many)

Graded by exact set match of names against TDBench's correct_rows (its answer accuracy A; its time accuracy T needs
an LLM judge and is not measured). Offices whose terms overlap in the tables are left out: `holder` is one value at
a time, so an overlap would be cut at the next term's start.

    uv run --extra mcp --with tiktoken --with anthropic python -m evals.tdbench.run \\
        --src ~/datasets/tdbench-src/repo --out evals/tdbench/results.jsonl [--reps 3] [--n 200]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from itertools import pairwise
from pathlib import Path
from threading import Lock
from typing import Any

from evals.efficiency.run import Gateway, call, tool_list
from evals.jira.bench import extract, versions
from keel import Ingestor, MemoryStore, Tools, load_cartridge, mcp_server
from keel.envelope import Event
from keel.mcp_server import build_server

HERE, TENANT = Path(__file__).parent, "tdbench"
FILES = {"basic_0": ("leaders", 90), "basic_2": ("organizations", 60), "now_0": ("leaders", 30), "now_2": ("organizations", 20)}
SYSTEM = """You answer questions about who held which office in countries and organizations. The records are current as of \
2025-01-01: "now" and "current" mean that date. Dates are UTC.
{tools}
Never invent facts. End your reply with one line, `ANSWER: <json>`, where <json> is a JSON list of the holders' names as the \
records write them, for example ["Mark Rutte"]; [] when nobody fits."""
TOOLS_TEXT = {
    "keel": "The records are in Keel, a context graph, through five tools. Offices are 'office:<Country>/<Role>' and posts "
            "'post:<Organization>/<Role>'; each has a `holder` fact over time.",
    "open": "The records for this question:\n{rows}",
    "table": "All the records, one term per line:\n{rows}",
    "closed": "You have no tools: answer from what you know.",
}


def slot(row: dict[str, str], table: str) -> str:
    return f"office:{row['Country']}/{row['Role']}" if table == "leaders" else f"post:{row['Organization_name']}/{row['Role']}"


@cache
def tables(src: Path) -> dict[str, list[dict[str, str]]]:
    return {t: list(csv.DictReader((src / f"dataset/crafted/dyknow/dyknow_{t}.csv").open())) for t in ("leaders", "organizations")}


@cache
def events(src: Path) -> tuple[list[Event], frozenset[str]]:
    """Every term as an event, in Start order per office; and the offices whose terms overlap (left out)."""
    by: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for table, rows in tables(src).items():
        for r in rows:
            by[slot(r, table)].append((table, r))
    out, overlapping = [], set()
    for s, terms in sorted(by.items()):
        terms.sort(key=lambda t: t[1]["Start"])
        if any(a["End"] and b["Start"] < a["End"] for (_, a), (_, b) in pairwise(terms)):
            overlapping.add(s)
        for table, r in terms:
            p = ({"country": r["Country"]} if table == "leaders" else {"organization": r["Organization_name"], "kind": r["Organization_type"]})
            p |= {"role": r["Role"], "holder": r["Name"], "start": r["Start"], "end": r["End"]}
            out.append(Event(id=f"tdbench:{s}:{r['Start']}:{r['Name']}", tenant=TENANT, type=f"{s.split(':')[0]}.term", subject=s,
                             occurred_at=f"{r['Start']}T00:00:00Z", source=f"tdbench.{table}", payload=p))
    return out, frozenset(overlapping)


def subject_of(sql: str, table: str, src: Path) -> str | None:
    """The office a question's SQL selects, matched to the table's spelling (the SQL's case can differ)."""
    get = lambda k: (m.group(1) if (m := re.search(rf'{k} = "([^"]+)"', sql)) else None)
    key = (get("Country"), get("Role")) if table == "leaders" else (get("Organization_name"), get("Role"))
    for r in tables(src)[table]:
        s = slot(r, table)
        if s.split(":", 1)[1].casefold() == f"{key[0]}/{key[1]}".casefold():
            return s
    return None


def questions(src: Path, n: int, seed: int = 7) -> list[dict[str, Any]]:
    """A stratified sample: per file its share of n, per question type in proportion, never an overlapping office."""
    rnd, out = random.Random(seed), []
    _, overlapping = events(src)
    for f, (table, k) in FILES.items():
        rows = [json.loads(line) for line in (src / f"qa_pairs/dyknow/{f}_sql_gpt4o.jsonl").open()]
        pool = defaultdict(list)
        for q in rows:
            s = subject_of(q["sql_for_db"], table, src)
            if s and s not in overlapping:
                pool[q["qtype"]].append(q | {"subject": s, "file": f})
        total = sum(len(v) for v in pool.values())
        for qtype, qs in sorted(pool.items()):
            out += rnd.sample(qs, round(k * n / 200 * len(qs) / total))
    return [{"id": f"{q['file']}-{q['idx']}", "file": q["file"], "qtype": q["qtype"], "op": q.get("op"), "subject": q["subject"],
             "question": q["questions"][0].strip(), "gold": sorted({next(v for c, v in r.items() if c in ("Name", "Team")) for r in q["correct_rows"]})}
            for q in out]


def norm(name: Any) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return " ".join(s.casefold().replace(".", "").split())


def grade(gold: list[str], answer: Any) -> bool:
    if answer is None or answer == "unknown":
        answer = []
    if isinstance(answer, str):
        answer = [answer]
    return isinstance(answer, list) and {norm(a) for a in answer} == {norm(g) for g in gold}


def ask(gw: Gateway, src: Path, system: str, model: str, q: dict[str, Any], max_steps: int = 12) -> dict[str, Any]:
    from evals.jira.memories import backoff, delay, transient

    server, tools = None, None
    text = TOOLS_TEXT[system]
    if system == "keel":
        c, store = load_cartridge(HERE), MemoryStore()
        Ingestor(c, store).ingest(events(src)[0], strict=True)
        server = build_server(Tools(c, store, tenant=TENANT), compact=False)
        tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}}
                 for t in tool_list(server)]
        text += "\n\n" + mcp_server.instructions(c, compact=False)
    if system == "open":
        rows = [e.payload for e in events(src)[0] if e.subject == q["subject"]]
        text = text.format(rows="\n".join(f"{r['holder']}, {r['role']}, from {r['start']} to {r['end'] or 'now'}" for r in rows))
    if system == "table":
        text = text.format(rows="\n".join(f"{e.payload.get('country') or e.payload['organization']}, {e.payload['role']}: {e.payload['holder']}, "
                                          f"from {e.payload['start']} to {e.payload['end'] or 'now'}" for e in events(src)[0]))
    messages = [{"role": "system", "content": SYSTEM.format(tools=text)}, {"role": "user", "content": q["question"]}]
    used, cost, calls, reply, start = {"input": 0, "output": 0}, 0.0, [], "", time.monotonic()
    for _ in range(max_steps):
        msg, u, c_ = gw.chat(model, messages, tools)
        used, cost = {k: used[k] + u[k] for k in used}, cost + c_
        turn = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            turn["tool_calls"] = [{"id": t["id"], "type": "function", "function": {"name": t["function"]["name"], "arguments": t["function"].get("arguments") or "{}"}}
                                  for t in msg["tool_calls"]]
        messages.append(turn)
        if not msg.get("tool_calls"):
            reply = turn["content"]
            break
        for t in turn["tool_calls"]:
            try:
                args = json.loads(t["function"]["arguments"])
            except ValueError:
                args = {}
            for wait in backoff():
                try:
                    out, _, error, _ = call(server, t["function"]["name"], args)
                    break
                except Exception as exc:  # noqa: BLE001 - a bad argument is the agent's to read
                    if wait and transient(exc):
                        time.sleep(delay(exc, wait))
                        continue
                    out, error = json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True
                    break
            calls.append({"tool": t["function"]["name"], "args": args, "error": bool(error)})
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": out})
    else:
        reply = "(stopped after the step limit)"
    answer, parsed = extract(reply)
    return {"id": q["id"], "file": q["file"], "qtype": q["qtype"], "op": q["op"], "system": system, "model": model, "gold": q["gold"],
            "answer": answer, "parsed": parsed, "correct": parsed and grade(q["gold"], answer), "reply": reply[-1500:], "calls": calls,
            "tokens": used, "cost_usd": round(cost, 6), "ms": round((time.monotonic() - start) * 1000)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True, help="a clone of github.com/ssoy0701/tdbench")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--systems", default="keel,open,closed")
    ap.add_argument("--models", default="openai/gpt-6-luna,anthropic/claude-haiku-5.5")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-usd", type=float)
    a = ap.parse_args()
    src, gw = a.src.expanduser(), Gateway()
    qs = questions(src, a.n)
    done = {(r["id"], r["system"], r["model"], r["rep"]) for r in map(json.loads, a.out.open())} if a.out.exists() else set()
    jobs = [(q, s, m, r) for r in range(a.reps) for m in a.models.split(",") for s in a.systems.split(",") for q in qs if (q["id"], s, m, r) not in done]
    print(f"{len(qs)} questions, {len(jobs)} runs ({len(done)} already in {a.out})")
    lock, spent, vers = Lock(), [0.0], versions()

    def run(job: tuple[dict[str, Any], str, str, int]) -> None:
        from evals.jira.memories import OUTAGES, backoff, delay, transient

        q, s, m, r = job
        if a.max_usd is not None and spent[0] >= a.max_usd:
            return
        for wait in backoff():
            try:
                res = ask(gw, src, s, m, q) | {"rep": r, "versions": vers}
                break
            except Exception as exc:  # outages are left out for a rerun, as in the Jira bench
                if wait and transient(exc):
                    time.sleep(delay(exc, wait))
                    continue
                print(f"  {q['id']} {s} {m}: {type(exc).__name__}: {str(exc)[:200]}")
                if any(o in str(exc) for o in OUTAGES) or transient(exc):
                    return
                raise
        with lock, a.out.open("a") as fh:
            fh.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            spent[0] += res["cost_usd"]

    with ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(run, jobs))
    report(a.out)


def report(*paths: Path) -> None:
    from evals.jira.bench import bootstrap, per_question

    rows = [json.loads(line) for path in paths for line in path.open()]
    for m in sorted({r["model"] for r in rows}):
        for s in ("keel", "open", "table", "closed"):
            rs = [r for r in rows if r["model"] == m and r["system"] == s]
            if not rs:
                continue
            acc, lo, hi, _ = bootstrap(list(per_question(rs).values()))
            by = {t: sum(r["correct"] for r in rs if r["qtype"] == t) / max(1, sum(r["qtype"] == t for r in rs))
                  for t in ("basic_unique", "basic_multiple", "basic_none", "now")}
            print(f"{m:<28} {s:<7} n={len(rs):<4} acc {acc:.2f} [{lo:.2f}, {hi:.2f}]  "
                  + "  ".join(f"{t.replace('basic_', '')} {v:.2f}" for t, v in by.items())
                  + f"  in {sum(r['tokens']['input'] for r in rs) / len(rs) / 1000:.1f}k  usd {sum(r['cost_usd'] for r in rs):.2f}")


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["report"]:
        report(*map(Path, sys.argv[2:]))
    else:
        main()
