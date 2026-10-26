"""Why tenant-wide Jira questions take Keel's agents ~5 requests where SQL's take ~2, and what one call can do now.

Reads the compact arm of the A/B (results/ab-compact-compact.jsonl: 144 tenant-wide runs, GPT-6 Luna and Claude
Haiku 5.5) and classifies every tool call of those runs by what it was for. Then it asks each of the 72 tenant-wide
questions the way this code lets an agent answer it in one call (a search with `where` over a list of values and
`since` on its hits, the project named in the instructions, a rule listing, a tenant history window), through a real
compact, read-only MCP server on the tenant's graph, checks every answer against the gold, and prices that path
(two requests: the call, then the answer) against the logged runs and SQL's. No model is called.

    uv run --extra mcp --with tiktoken --with numpy python -m evals.jira.requests [--data ~/datasets/public-jira/export]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evals.jira import context_tokens as ct

HERE = Path(__file__).parent
CLASSES = {  # what a call was for, in the order they are tested; the fix this code makes for each
    "error": "a call that failed (a wrong argument, a rule name that is a relation)",
    "scope lookup": "the project looked up or each issue's project checked (search kind=project, where={project: ...}, related project:X/in_project)",
    "repeat": "a search or history call made again with only the project filter, the detail or the limit changed",
    "fan-out over values": "the same search once per value of one field (a status, a priority)",
    "whole-kind listing": "every issue of the tenant read (search kind=issue without a filter, brief, >1,500 tokens)",
    "per-subject check": "context or history of one hit after a listing that already answered (verification, or 30 days in a status)",
    "tenant history dump": "the tenant's whole history of a field (history without a subject) on a question that is not about changes",
    "answer": "the call that answered, or a step towards it",
}


def subtype(q: dict[str, Any]) -> str:
    return q["params"]["subtype"]


def sig(a: dict[str, Any], drop: tuple[str, ...]) -> str:
    return json.dumps({k: v for k, v in a.items() if k not in drop}, sort_keys=True)


def classify(r: dict[str, Any], sub: str) -> list[str]:
    out, seen = [], []
    for c in r["calls"]:
        a, tool, w = c["args"], c["tool"], c["args"].get("where") or {}
        if c["error"]:
            label = "error"
        elif ((tool == "search" and (a.get("kind") == "project" or "project" in w or "in_project" in w or a.get("rule") == "in_project"))
              or (tool == "related" and (a.get("relation") == "in_project" or str(a.get("subject", "")).startswith("project:")))
              or (tool == "context" and a.get("only") == ["in_project"])):
            label = "scope lookup"
        elif any(p[0] == tool and tool in ("search", "history") and sig(a, ("related_to", "via", "detail", "limit", "scope")) == sig(p[1], ("related_to", "via", "detail", "limit", "scope"))
                 for p in seen):
            label = "repeat"
        elif tool == "search" and w and any(p[0] == "search" and set(p[1].get("where") or {}) == set(w) and sig(a, ("where", "detail", "limit")) == sig(p[1], ("where", "detail", "limit"))
                                         for p in seen):
            label = "fan-out over values"
        elif tool == "search" and not w and not a.get("rule") and not a.get("text") and c["tokens"] > 1500:
            label = "whole-kind listing"
        elif tool in ("context", "history") and a.get("subject"):
            label = "per-subject check"
        elif tool == "history" and not a.get("subject") and sub != "changed":
            label = "tenant history dump"
        else:
            label = "answer"
        seen.append((tool, a))
        out.append(label)
    return out


# ---- one call per question, through a real server --------------------------------------------------------


def at(s: str | None) -> str | None:
    return None if s is None else datetime.strptime(s, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).isoformat()


def ideal(q: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The one call that answers a tenant-wide question with this code (and, for idle, the path without the rule)."""
    p, when = q["params"], {"as_of": at(q["params"].get("as_of"))} if q["params"].get("as_of") else {}
    sub = subtype(q)
    if sub in ("status_at", "known_at"):
        return [("search", {"kind": "issue", "where": {"status": p["status"]}, **when, **({"as_known_at": at(p["known_at"])} if p.get("known_at") else {}),
                            "detail": "key", "limit": 0})]
    if sub == "count_at":
        statuses = [s.strip() for s in q["question"].rsplit("among:", 1)[1].rstrip("?").split(",")]
        return [("search", {"kind": "issue", "where": {"status": statuses}, **when, "detail": "count"})]
    if sub == "rule_at":
        return [("search", {"rule": p["rule"], **when, "detail": "key", "limit": 0})]
    return [("history", {"since": at(p["since"]), "until": at(p["until"]), "predicate": p["field"], "limit": 0})]


def idle_by_since(q: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Idle without the rule: one search over the statuses the question lists, read with each hit's `since`."""
    statuses = [s.strip() for s in q["question"].split("is one of ", 1)[1].split(" and its status", 1)[0].split(",")]
    return ("search", {"kind": "issue", "where": {"status": statuses}, "as_of": at(q["params"]["as_of"]), "detail": "brief", "limit": 0})


def read_answer(q: dict[str, Any], result: Any) -> Any:
    from keel.mcp_server import unpack

    data = unpack(result)
    sub = subtype(q)
    ids = lambda keys: sorted({k.split(":", 1)[1] for k in keys if isinstance(k, str)})
    if sub == "count_at":
        return data[-1]["count"]
    if sub == "changed":  # a change replaced a value (`was`); a correction of a wrong record is not one
        return ids([f["subject"] for f in data if "was" in f and not str(f.get("evidence", "")).endswith(":wrong")])
    return ids(data)


def run_ideal(data: Path) -> list[dict[str, Any]]:
    from evals.efficiency.run import Client
    from evals.jira import bench
    from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge
    from keel.mcp_server import build_server

    out = []
    cart = load_cartridge(HERE)
    for tenant in bench.INSTANCES:
        events = [Event(**json.loads(line)) for line in (data / f"{tenant}.bitemporal.jsonl").open()]
        store = MemoryStore()
        Ingestor(cart, store).ingest(events, strict=True)
        now = max(e.recorded_at for e in events) + timedelta(days=1)
        server = build_server(Tools(cart, store, tenant, clock=lambda now=now: now, max_list=200), compact=True, read_only=True)

        async def call(name: str, args: dict[str, Any], server: Any = server) -> str:
            async with Client(server) as client:
                res = await client.call_tool(name, args)
            return res.content[0].text

        for q in json.loads((data / f"{tenant}.questions.json").read_text()):
            if q["type"] != "tenant":
                continue
            [(tool, args)] = ideal(q)
            text = asyncio.run(call(tool, args))
            row = {"q": q, "tool": tool, "args": args, "text": text, "answer": read_answer(q, json.loads(text))}
            if q["params"].get("rule") == "stalled":  # idle without the rule: since on the hits, 30 days back
                t2, a2 = idle_by_since(q)
                from keel.mcp_server import unpack

                hits = unpack(json.loads(asyncio.run(call(t2, a2))))
                cut = datetime.fromisoformat(a2["as_of"]) - timedelta(days=30)
                row["since_answer"] = sorted(h["key"].split(":", 1)[1] for h in hits if isinstance(h, dict) and "key" in h
                                             and datetime.fromisoformat(h["since"]["status"].replace("Z", "+00:00")) <= cut)
            out.append(row)
    return out


# ---- the report -------------------------------------------------------------------------------------------


def report(data: Path) -> str:
    from evals.jira import bench

    out: list[str] = []
    w = out.append
    qs = ct.questions(data)
    rows = [x for x in ct.runs("keel-compact", data) if x["r"]["type"] == "tenant"]
    allk = ct.runs("keel-compact", data)
    sql = [x for x in ct.runs("sql", data) if x["r"]["type"] == "tenant"]
    calib = {m: ct.Calib([x for x in allk if x["r"]["model"] == m]) for m in ct.MODELS}

    labels = {id(x): classify(x["r"], subtype(qs[x["r"]["id"]])) for x in rows}
    w("## 1. What the calls were for\n")
    w("Every tool call of the 144 tenant-wide runs of the compact arm, by what it did (tested in this order):\n")
    w(ct.table(["class", "what it is"], [[k, v] for k, v in CLASSES.items()], "ll"))
    w("")
    head = ["class"] + [f"{n} calls" for n in ct.MODELS.values()] + [f"{n} tokens of results" for n in ct.MODELS.values()] + ["runs with one (both)"]
    table = []
    for k in CLASSES:
        row: list[Any] = [k]
        for m in ct.MODELS:
            row.append(sum(lab == k for x in rows if x["r"]["model"] == m for lab in labels[id(x)]))
        for m in ct.MODELS:
            row.append(sum(c["tokens"] for x in rows if x["r"]["model"] == m for c, lab in zip(x["r"]["calls"], labels[id(x)]) if lab == k))
        row.append(f"{sum(k in labels[id(x)] for x in rows)} / {len(rows)}")
        table.append(row)
    table.append(["**all**", *[sum(len(x["r"]["calls"]) for x in rows if x["r"]["model"] == m) for m in ct.MODELS],
                  *[sum(c["tokens"] for x in rows if x["r"]["model"] == m for c in x["r"]["calls"]) for m in ct.MODELS], ""])
    w(ct.table(head, table))
    w("\nBy question type (both models), calls per class:\n")
    subs = ["status_at", "count_at", "rule_at", "changed", "known_at"]
    table = []
    for s in subs:
        xs = [x for x in rows if subtype(qs[x["r"]["id"]]) == s]
        c = Counter(lab for x in xs for lab in labels[id(x)])
        table.append([s, len(xs), *[c[k] for k in CLASSES]])
    w(ct.table(["type", "runs", *CLASSES], table))

    w("\n## 2. Requests per run\n")
    w("Mean requests (model turns) per tenant-wide run, logged; SQL from results/full-sql.jsonl (3 repetitions). A run "
      "needs at least two: the call and the answer.\n")
    table = []
    for s in subs:
        row = [s]
        for m in ct.MODELS:
            k = [x["r"]["turns"] for x in rows if x["r"]["model"] == m and subtype(qs[x["r"]["id"]]) == s]
            q_ = [x["r"]["turns"] for x in sql if x["r"]["model"] == m and subtype(qs[x["r"]["id"]]) == s]
            row += [f"{ct.mean(k):.1f} / {ct.mean(q_):.1f}"]
        table.append(row)
    table.append(["**all**", *[f"{ct.mean([x['r']['turns'] for x in rows if x['r']['model'] == m]):.1f} / {ct.mean([x['r']['turns'] for x in sql if x['r']['model'] == m]):.1f}" for m in ct.MODELS]])
    w(ct.table(["type", *[f"{n} Keel / SQL" for n in ct.MODELS.values()]], table))
    over = {m: sum(max(0, x["r"]["turns"] - 2) for x in rows if x["r"]["model"] == m) for m in ct.MODELS}
    w("\nRequests beyond two: " + ", ".join(f"{n} {over[m]} over {sum(x['r']['model'] == m for x in rows)} runs" for m, n in ct.MODELS.items()) + ".\n")

    # 3. one call per question
    w("## 3. One call per question, with this code\n")
    w("Each tenant-wide question asked once through a compact, read-only MCP server bound to its tenant (this code), with "
      "the call below; the answer read off the result by a fixed reader and compared with the gold.\n")
    w("| type | the call |\n| --- | --- |\n"
      "| status_at, known_at | `search(kind=\"issue\", where={\"status\": S}, as_of=T[, as_known_at=K], detail=\"key\", limit=0)` |\n"
      "| count_at | `search(kind=\"issue\", where={\"status\": [the statuses the question lists]}, as_of=T, detail=\"count\")` (new: a list is any of them) |\n"
      "| rule_at | `search(rule=R, as_of=T, detail=\"key\", limit=0)`; idle also without the rule: `search(kind=\"issue\", where={\"status\": [...]}, as_of=T, detail=\"brief\", limit=0)`, kept where `since.status` is 30 days or more before T (new: since on where hits) |\n"
      "| changed | `history(since=A, until=B, predicate=F, limit=0)`, the subjects of entries with `was` |\n")
    ideal_rows = run_ideal(data)
    right = sum(r["answer"] == r["q"]["answer"] for r in ideal_rows)
    idle = [r for r in ideal_rows if "since_answer" in r]
    w(f"Gold reached: **{right} / {len(ideal_rows)}** with one call each; idle without the rule, from `since`: "
      f"{sum(r['since_answer'] == r['q']['answer'] for r in idle)} / {len(idle)}.\n")
    missed = [(r["q"]["id"], r["answer"], r["q"]["answer"]) for r in ideal_rows if r["answer"] != r["q"]["answer"]]
    if missed:
        w(f"Missed: {missed}\n")

    w("Cost of that path against the logged runs, per run: two requests, each carrying the fixed part this code sends "
      "(bench --keel-read-only: current descriptions, four tools, the server's instructions), the second also the call "
      "and its result. o200k; provider tokens weighed by each model's compact-arm calibration (rf for the fixed part, rc "
      "for the conversation). Logged: the compact arm's provider input, and SQL's.\n")
    by_id = {r["q"]["id"]: r for r in ideal_rows}
    fixed: dict[str, float] = {}
    for r in ideal_rows:
        q = r["q"]
        kp = ct.keel_parts(True, now=True, read_only=True, project=bench.INSTANCES[q["tenant"]][1])
        f = ct.tokens(ct.system_prompt("keel-compact", q["tenant"], q["answer_type"], tools_text=kp["tools_text"])) + ct.tokens(ct.jdump(kp["tools"])) + ct.tokens(q["question"])
        fixed[q["id"]] = f
        r["conv"] = ct.tokens(r["tool"]) + ct.tokens(ct.jdump(r["args"])) + ct.tokens(r["text"])
        r["o200k"] = 2 * f + r["conv"]
    table = []
    for s in [*subs, None]:
        row: list[Any] = [s or "**all**"]
        for m in ct.MODELS:
            cal = calib[m]
            xs = [x for x in rows if x["r"]["model"] == m and (s is None or subtype(qs[x["r"]["id"]]) == s)]
            ids = [x["r"]["id"] for x in xs]
            new = ct.mean([2 * cal.rf * fixed[i] + cal.rc * by_id[i]["conv"] for i in ids])
            logged = ct.mean([x["r"]["tokens"]["input"] for x in xs])
            sq = ct.mean([x["r"]["tokens"]["input"] for x in sql if x["r"]["model"] == m and (s is None or subtype(qs[x["r"]["id"]]) == s)])
            row += [f"{logged:,.0f}", f"{new:,.0f} ({new / logged - 1:+.0%})", f"{sq:,.0f}"]
        table.append(row)
    w(ct.table(["type", *[f"{n} {c}" for n in ct.MODELS.values() for c in ("logged", "one call", "SQL logged")]], table))
    w(f"\nThe one-call path's result tokens (o200k, mean): {ct.mean([ct.tokens(r['text']) for r in ideal_rows]):,.0f}; "
      f"its fixed part {ct.mean(list(fixed.values())):,.0f} per request.\n")

    # 4. the logged runs without the classes the changes target
    w("## 4. The logged runs without the calls these changes target\n")
    w("The one-call path is what the tools now allow, not what an agent will do. Closer to the logs: each logged run "
      "replayed without its calls of the classes this code targets (scope lookup, repeat, fan-out, whole-kind listing, "
      "per-subject check, tenant history dump), its other calls and answer kept, its requests cut in proportion to the "
      "calls removed (at least two), with this code's fixed part. Provider tokens by the same calibration.\n")
    targeted = {"scope lookup", "repeat", "fan-out over values", "whole-kind listing", "per-subject check", "tenant history dump"}
    table = []
    scen: dict[str, dict[str, float]] = {}
    for s in [*subs, None]:
        row = [s or "**all**"]
        for m in ct.MODELS:
            cal = calib[m]
            xs = [x for x in rows if x["r"]["model"] == m and (s is None or subtype(qs[x["r"]["id"]]) == s)]
            new = []
            for x in xs:
                r, lab = x["r"], labels[id(x)]
                kept = [c for c, lb in zip(r["calls"], lab) if lb not in targeted]
                gone = len(r["calls"]) - len(kept)
                T = r["turns"]
                T2 = max(2 if kept or r["calls"] else 1, T - round(gone * (T - 1) / max(1, len(r["calls"]))))
                r2 = r | {"calls": kept, "turns": T2}
                f = fixed[r["id"]]
                recon = (ct.rebuild(r2, f, False)["total"] + ct.rebuild(r2, f, True)["total"]) / 2
                new.append(T2 * cal.rf * f + cal.rc * (recon - T2 * f))
            logged = ct.mean([x["r"]["tokens"]["input"] for x in xs])
            row += [f"{logged:,.0f}", f"{ct.mean(new):,.0f} ({ct.mean(new) / logged - 1:+.0%})"]
            if s is None:
                scen[m] = {"logged": logged, "new": ct.mean(new)}
        table.append(row)
    w(ct.table(["type", *[f"{n} {c}" for n in ct.MODELS.values() for c in ("logged", "without the targeted calls")]], table))
    w("\nWhat that is over the whole benchmark (387 questions; tenant-wide are 72 of them), mean logged input per run:\n")
    table = []
    for m, n in ct.MODELS.items():
        everything = [x for x in allk if x["r"]["model"] == m]
        tot = ct.mean([x["r"]["tokens"]["input"] for x in everything])
        share = len([x for x in rows if x["r"]["model"] == m]) / len(everything)
        mid = tot - share * (scen[m]["logged"] - scen[m]["new"])
        table.append([n, f"{tot:,.0f}", f"{share * scen[m]['logged'] / tot:.0%}", f"{mid:,.0f} ({mid / tot - 1:+.0%})"])
    w(ct.table(["model", "all questions, logged", "tenant-wide share of it", "with tenant-wide runs without the targeted calls"], table))
    w("\nIt still assumes every targeted call goes away: an upper bound on what these changes do through the agent, which the paid A/B measures. The tenant-wide rows include this code's fixed part; the other questions gain it too (context_tokens.md section 5), not counted here.\n")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=Path("~/datasets/public-jira/export"))
    a = ap.parse_args()
    print(report(a.data.expanduser()))


if __name__ == "__main__":
    main()
