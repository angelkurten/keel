"""Where the tokens of Keel's tool results go, measured offline on the Jira benchmark's own calls.

`replay` makes every Keel tool call the agents made in the full run (results/full-{luna,haiku}.jsonl, system keel)
again, through real MCP sessions on the same graph (MemoryStore, --keel-max-list 200, a fixed clock): once on the
plain server and once on the compact one (`keel serve --compact`), and checks the compact text unpacks to the plain
answer. `report` breaks the texts down by tool, detail and field, and compares them with what the SQL baseline's
results cost on the same questions (results/full-sql.jsonl, as logged). No model is called. Tokens are o200k_base
(tiktoken), as the benchmark counts them.

    uv run --extra mcp --with tiktoken python -m evals.jira.tokens replay --data ~/datasets/public-jira/export --out /tmp/replay.jsonl
    uv run --extra mcp --with tiktoken python -m evals.jira.tokens report /tmp/replay.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

HERE = Path(__file__).parent
RESULTS = HERE / "results"
RUNS = ("full-luna.jsonl", "full-haiku.jsonl")
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)  # the replay's clock: reads without as_of agree run to run
ENC = tiktoken.get_encoding("o200k_base")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")


def tokens(s: str) -> int:
    return len(ENC.encode(s))


def keel_runs() -> list[dict[str, Any]]:
    return [r for f in RUNS for line in (RESULTS / f).open() if (r := json.loads(line))["system"] == "keel"]


def short(text: str) -> str:
    """Plain text with its UTC times as pack writes them."""
    text = re.sub(r'T(\d\d:\d\d):00\+00:00"', r'T\1Z"', text)
    return re.sub(r'(T\d\d:\d\d:\d\d(?:\.\d+)?)\+00:00"', r'\1Z"', text)


def replay(data: Path, out: Path) -> None:
    from evals.efficiency.run import call
    from keel import Ingestor, MemoryStore, Tools, load_cartridge
    from keel.envelope import Event
    from keel.mcp_server import build_server, unpack

    servers: dict[str, Any] = {}
    with out.open("w") as fh:
        for r in keel_runs():
            t = r["tenant"]
            if t not in servers:
                store, cart = MemoryStore(), load_cartridge(HERE)
                Ingestor(cart, store).ingest([Event(**json.loads(e)) for e in (data / f"{t}.bitemporal.jsonl").open()], strict=True)
                tools = Tools(cart, store, tenant=t, max_list=200, clock=lambda: NOW)
                servers[t] = (build_server(tools, compact=False), build_server(tools))
            for i, c in enumerate(r["calls"]):
                text, _, error, _ = call(servers[t][0], c["tool"], c["args"])
                packed, _, _, _ = call(servers[t][1], c["tool"], c["args"])
                if not error:
                    assert unpack(json.loads(packed)) == json.loads(short(text)), (r["id"], c)
                fh.write(json.dumps({"id": r["id"], "type": r["type"], "model": r["model"], "rep": r["rep"], "i": i, "tool": c["tool"],
                                     "args": c["args"], "logged": c["tokens"], "tokens": tokens(text), "compact": tokens(packed), "error": error,
                                     "text": text, "packed": packed}, ensure_ascii=False) + "\n")


# ---- report ------------------------------------------------------------------------------


def detail_of(c: dict[str, Any]) -> str:
    d = c["args"].get("detail")
    return d or {"context": "brief", "history": "concise", "search": "entity", "related": "-"}.get(c["tool"], "-")


def walk(v: Any, path: str, acc: Counter) -> None:
    """Each key's share: its name and its scalar values, by path ([] for a list element); punctuation is the rest."""
    if isinstance(v, dict):
        for k, x in v.items():
            p = f"{path}.{k}"
            acc[(p, "key")] += tokens(json.dumps(k) + ":")
            walk(x, p, acc)
    elif isinstance(v, list):
        for x in v:
            walk(x, path + "[]", acc)
    else:
        s = json.dumps(v, ensure_ascii=False)
        acc[(path, "time" if isinstance(v, str) and ISO.match(v) else "value")] += tokens(s)


def report(path: Path) -> None:
    rows = [json.loads(line) for line in path.open()]
    total, after = sum(r["tokens"] for r in rows), sum(r["compact"] for r in rows)
    print(f"{len(rows)} calls; {total:,} tokens plain ({sum(r['logged'] for r in rows):,} logged in the run), {after:,} compact "
          f"({1 - after / total:.1%} fewer)\n")
    n, by_tool, by_detail = Counter(), Counter(), Counter()
    for r in rows:
        n[r["tool"]] += 1
        by_tool[r["tool"]] += r["tokens"]
        by_tool[r["tool"], "compact"] += r["compact"]
        by_detail[r["tool"], detail_of(r)] += r["tokens"]
        by_detail[r["tool"], detail_of(r), "compact"] += r["compact"]
    print("| tool | calls | plain | share | compact | change |\n| --- | ---: | ---: | ---: | ---: | ---: |")
    for t in sorted(n, key=lambda t: -by_tool[t]):
        print(f"| {t} | {n[t]} | {by_tool[t]:,} | {by_tool[t] / total:.1%} | {by_tool[t, 'compact']:,} | {by_tool[t, 'compact'] / by_tool[t] - 1:+.0%} |")
    print("\n| tool, detail | plain | share | compact | change |\n| --- | ---: | ---: | ---: | ---: |")
    for key, v in sorted(((k, v) for k, v in by_detail.items() if len(k) == 2), key=lambda kv: -kv[1]):
        c = by_detail[(*key, "compact")]
        print(f"| {' '.join(key)} | {v:,} | {v / total:.1%} | {c:,} | {c / v - 1:+.0%} |")
    fields: Counter = Counter()
    parts: Counter = Counter()
    for r in rows:
        if r["error"]:
            parts["error text"] += r["tokens"]
            continue
        acc: Counter = Counter()
        walk(json.loads(r["text"]), r["tool"], acc)
        fields.update(acc)
        for (_, part), v in acc.items():
            parts[part] += v
        parts["punctuation"] += r["tokens"] - sum(acc.values())
    # each piece is tokenized alone, which counts more than the same piece in context: shares are of the pieces' sum
    whole = sum(v for k, v in parts.items() if k != "punctuation")
    print("\n| part (plain) | share |\n| --- | ---: |")
    for part, v in parts.most_common():
        if part != "punctuation":
            print(f"| {'key names' if part == 'key' else part} | {v / whole:.1%} |")
    print("\n| path (plain) | part | share |\n| --- | --- | ---: |")
    for (p, part), v in fields.most_common(30):
        print(f"| `{p}` | {part} | {v / whole:.1%} |")
    sql = [json.loads(line) for line in (RESULTS / "full-sql.jsonl").open()]
    s = sum(c["tokens"] for r in sql for c in r["calls"]) / len(sql)
    runs = len(keel_runs())
    print(f"\nper run (a question, a model, a repetition): Keel {total / runs:,.0f} tokens of tool results plain, {after / runs:,.0f} compact; "
          f"SQL {s:,.0f}: {total / runs / s:.1f}x and {after / runs / s:.1f}x")
    for m in sorted({r["model"] for r in rows}):
        k = [r for r in rows if r["model"] == m]
        km = len([r for r in keel_runs() if r["model"] == m])
        sm = [r for r in sql if r["model"] == m]
        sv = sum(c["tokens"] for r in sm for c in r["calls"]) / len(sm)
        print(f"  {m}: Keel {sum(r['tokens'] for r in k) / km:,.0f} plain, {sum(r['compact'] for r in k) / km:,.0f} compact; SQL {sv:,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("replay")
    a.add_argument("--data", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("report")
    b.add_argument("path", type=Path)
    args = ap.parse_args()
    replay(args.data, args.out) if args.cmd == "replay" else report(args.path)


if __name__ == "__main__":
    main()
