"""Where Keel's *input* tokens go in the Jira benchmark, beyond tool results, measured offline.

Every request of the agent loop (bench.ask) sends the system prompt (bench's frame, the system's tool paragraph and, for
Keel, the MCP session instructions), the tool definitions, the question, and then the whole conversation so far: each
earlier assistant turn (its tool calls) and each tool result. This script rebuilds that from what the benchmark logs
(per run: turns, calls with their arguments and the tokens of each result, the final reply, the provider's input and
output tokens) and splits it by component; it checks the rebuild against the logged `tokens.input`; and it prices the
reduction options. No model is called. Tokens are o200k_base (tiktoken), as bench/tokens.py count them; the providers'
own tokenizers and hidden framing differ: a calibration on the runs whose shape is known (one call, two requests)
measures what one request's fixed part and one token of conversation weigh for each provider.

    uv run --extra mcp --with tiktoken --with numpy python -m evals.jira.context_tokens [--data ~/datasets/public-jira/export]

Inputs: results/ab-compact-{plain,compact}.jsonl (Keel, 387 questions x Luna and Haiku, 1 repetition) and
results/full-sql.jsonl (the SQL baseline, 3 repetitions), and the questions (<data>/<tenant>.questions.json).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from typing import Any

import tiktoken

HERE = Path(__file__).parent
RESULTS = HERE / "results"
ENC = tiktoken.get_encoding("o200k_base")
CONDITIONS = {"keel-plain": "ab-compact-plain.jsonl", "keel-compact": "ab-compact-compact.jsonl", "sql": "full-sql.jsonl"}
MODELS = {"openai/gpt-6-luna": "Luna", "anthropic/claude-haiku-5.5": "Haiku"}
# $ per input token, output token. Luna: the gateway's prices, which the logged costs fit exactly (no cache discount
# applied: bench prices every prompt token as fresh). Haiku: Anthropic's list price (evals/efficiency/run.py
# DIRECT_PRICES); its runs went straight to Anthropic with prompt caching on, billed at 0.1x for a cache read and
# 1.25x for a cache write.
PRICES = {"openai/gpt-6-luna": (0.10e-6, 0.50e-6), "anthropic/claude-haiku-5.5": (0.10e-6, 0.50e-6)}
READ, WRITE = 0.1, 1.25
TODAY = "2026-10-25"  # the prompt carries the run's date; its length is the same every day


def tokens(s: str) -> int:
    return len(ENC.encode(s))


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# ---- the fixed part of a request -------------------------------------------------------------------------


LOGGED_DESCRIPTIONS = {  # keel/tools.py DESCRIPTIONS as the A/B ran (1c58601 .. 6c7a627)
    "context": """What is true about a subject ('<kind>:<id>', or a list of them) now or at as_of: facts, relations, each rule's evaluation. depth=1 adds the neighbours. detail: brief (default; facts as {predicate: value} with `since`), concise (relations one by one), full (an audit: transaction times and ids; costly). as_known_at: what Keel knew then. task: that task's memory. exclude/only: predicates to drop/keep. watermark: the cursor for history(changed_since=).""",
    "history": """Facts over time for a subject, oldest first. Filters: since/until (ISO), predicate, scope, task; as_known_at: what Keel knew then. No subject: with since/until, what changed in the world across the tenant in that window (with predicate: e.g. whose assignee changed); with changed_since (a watermark), what Keel recorded since your last run. A fact that replaced a value says which in `was` (`was: null`: it was unset, so being set is a change); a first value has no `was`. limit keeps the most recent (0: all); a cut list starts with {"more": ...}. detail: concise (default) or full (an audit).""",
    "search": """Find subjects. text matches keys, attributes and values (a list of texts: one result list each); mode="semantic" ranks by meaning. kind (or a list) or rule: list a kind, or where a rule holds now, by key; page with limit and after. related_to (+ via): only that subject's neighbours. where={name: value}: an exact attribute or fact. detail: entity (default), key, brief (with current facts), concise, full, count ({"count": n}, nothing listed: how many, without reading them); depth=1 adds neighbours. as_of: rules, where and facts at that instant; as_known_at: as Keel knew them then. per_kind=n: the best n of each kind. exclude: predicates to drop. A cut list starts with {"more": ...}.""",
    "annotate": """Record what you learned (value: any JSON). Scopes tenant and global need evidence; task ties it to a job; key makes a retry return the first write; status="proposed" waits for a person or authorized agent. Returns a receipt (detail="full": the stored fact).""",
    "related": """Neighbours of a subject, by relation (one or a list), now or at as_of. depth 2 or 3 walks further (each entry says hops and via). At most limit (0: all); a cut list starts with {"more": ...} counting each relation.""",
}
LOGGED_RULES = "Rules, evaluated in context's `evaluation` (trust it):"  # the instructions' rules line as the A/B ran
RULES_NOW = "Rules (context's `evaluation`; search(rule=) lists where one holds; trust them):"


@cache
def keel_parts(compact: bool, now: bool = False, read_only: bool = False, project: str | None = None) -> dict[str, Any]:
    """The Keel condition's system text and tool list, built as bench.ask builds them (an empty graph: neither
    depends on the data). By default as the logged A/B ran them (its descriptions and instructions, five tools), to
    rebuild those runs; `now`: as this code sends them, `read_only` as bench --keel-read-only, and `project` the
    tenant's one project, which a bound server names in its instructions (Kinds: ..., project (project:INDY))."""
    from evals.efficiency.run import tool_list
    from evals.jira import bench
    from keel import MemoryStore, Tools, mcp_server
    from keel.mcp_server import build_server

    cart = bench.keel_cartridge()
    server = build_server(Tools(cart, MemoryStore(), tenant="hyperledger", max_list=200), compact=compact, read_only=read_only)
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", "") if now else LOGGED_DESCRIPTIONS[t["name"]],
                                               "parameters": t["inputSchema"]}} for t in tool_list(server)]
    text = mcp_server.instructions(cart, compact=compact, few={"project": [f"project:{project}"]} if project else None)
    if not now:
        text = text.replace(RULES_NOW, LOGGED_RULES)
    return {"tools_text": bench.TOOLS_TEXT["keel"] + "\n\n" + text, "tools": tools,
            "line": bench.TOOLS_TEXT["keel"], "instructions": text, "cart": cart,
            "tables": mcp_server.TABLES if compact else ""}


@cache
def sql_parts() -> dict[str, Any]:
    from evals.jira import sql_system

    return {"tools_text": sql_system.TOOLS_TEXT, "tools": sql_system.TOOLS}


def parts(cond: str) -> dict[str, Any]:
    return sql_parts() if cond == "sql" else keel_parts(cond == "keel-compact")


def system_prompt(cond: str, tenant: str, answer_type: str, tools_text: str | None = None) -> str:
    from evals.jira import bench

    instance, project = bench.INSTANCES[tenant]
    fmt = bench.FORMATS["value" if answer_type == "unknown" else answer_type]
    return bench.SYSTEM.format(project=project, instance=instance, today=TODAY,
                               tools=parts(cond)["tools_text"] if tools_text is None else tools_text, fmt=fmt)


def fixed_components(cond: str, tenant: str, answer_type: str, question: str) -> dict[str, int]:
    """One request's fixed part by component, each tokenized alone (their sum is within a few tokens of the whole)."""
    from evals.jira import bench

    p = parts(cond)
    frame = system_prompt(cond, tenant, answer_type, tools_text="")
    fmt = bench.FORMATS["value" if answer_type == "unknown" else answer_type]
    out = {"bench frame": tokens(frame) - tokens(fmt), "answer format": tokens(fmt)}
    if cond == "sql":
        out["SQL schema+macros text"] = tokens(p["tools_text"])
    else:
        out["Keel line (bench)"] = tokens(p["line"])
        out["MCP instructions"] = tokens(p["instructions"])
    out["tool definitions"] = tokens(jdump(p["tools"]))
    out["question"] = tokens(question)
    return out


# ---- the runs --------------------------------------------------------------------------------------------


@cache
def questions(data: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted(data.glob("*.questions.json")):
        out |= {q["id"]: q for q in json.loads(f.read_text())}
    return out


def load(cond: str) -> list[dict[str, Any]]:
    """The condition's runs; a run the provider logged no input for (one SQL run, an empty reply) is left out."""
    return [r for line in (RESULTS / CONDITIONS[cond]).open() if (r := json.loads(line))["tokens"]["input"]]


def assistant_call(c: dict[str, Any]) -> int:
    return tokens(c["tool"]) + tokens(jdump(c["args"]))


def turns_of(r: dict[str, Any], extras_first: bool) -> list[list[dict[str, Any]]]:
    """The run's calls by assistant turn. The log keeps their order but not which turn made each: every turn but the
    last made at least one call; the parallel extras go to the first tool turn or to the last one (the two bounds)."""
    calls, n = r["calls"], r["turns"] - 1
    if n <= 0:
        return []
    extra = len(calls) - n
    if extra <= 0:
        return [[c] for c in calls[:n]] + [[] for _ in range(n - len(calls))]
    if extras_first:
        return [calls[: extra + 1]] + [[c] for c in calls[extra + 1 :]]
    return [[c] for c in calls[: n - 1]] + [calls[n - 1 :]]


def rebuild(r: dict[str, Any], fixed: int, extras_first: bool) -> dict[str, Any]:
    """Each request's prompt (o200k): the fixed part plus every earlier assistant turn and tool result."""
    tt = turns_of(r, extras_first)
    calls = sum(assistant_call(c) for t in tt for c in t)
    # what the model wrote besides its calls and its last reply (thinking off): the rest of its output, spread evenly
    text = max(0, r["tokens"]["output"] - tokens(r["reply"]) - calls) / max(1, len(tt))
    prompts, acc = [], 0
    split = Counter()
    T = r["turns"]
    for j in range(T):
        prompts.append(fixed + acc)
        if j < len(tt):
            a = sum(assistant_call(c) for c in tt[j]) + text
            res = sum(c["tokens"] for c in tt[j])
            later = T - j - 1  # requests after this turn: the first carries it fresh, the others re-send it
            split["assistant, first send"] += a * min(1, later)
            split["assistant, re-sent"] += a * max(0, later - 1)
            split["tool results, first send"] += res * min(1, later)
            split["tool results, re-sent"] += res * max(0, later - 1)
            acc += a + res
    split["fixed, first request"] = fixed
    split["fixed, re-sent (requests 2..T)"] = fixed * (T - 1)
    return {"prompts": prompts, "total": sum(prompts), "split": split}


def runs(cond: str, data: Path) -> list[dict[str, Any]]:
    qs = questions(data)
    out = []
    for r in load(cond):
        q = qs[r["id"]]
        comp = fixed_components(cond, r["tenant"], q["answer_type"], q["question"])
        fixed = tokens(system_prompt(cond, r["tenant"], q["answer_type"])) + tokens(jdump(parts(cond)["tools"])) + tokens(q["question"])
        lo, hi = rebuild(r, fixed, False), rebuild(r, fixed, True)
        out.append({"r": r, "comp": comp, "fixed": fixed, "lo": lo, "hi": hi, "recon": (lo["total"] + hi["total"]) / 2,
                    "exact": len(r["calls"]) <= r["turns"] - 1,
                    "split": Counter({k: (lo["split"][k] + hi["split"][k]) / 2 for k in lo["split"] | hi["split"]})})
    return out


class Calib:
    """What the provider counts, from the runs whose shape is known (one call, then the answer: logged = 2 x fixed' +
    rc x conversation, least squares): rf, the provider's tokens per o200k token of the fixed part (tokenizer, how it
    renders the tool definitions, its tool-use preamble, message framing); rc, the same for the conversation (tool
    results and assistant turns)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        ex = [x for x in rows if x["r"]["turns"] == 2 and len(x["r"]["calls"]) == 1] or rows
        xs = [x["recon"] - x["r"]["turns"] * x["fixed"] for x in ex]
        ys = [x["r"]["tokens"]["input"] for x in ex]
        mx, my = mean(xs), mean(ys)
        self.rc = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        self.rf = (my - self.rc * mx) / mean([x["r"]["turns"] * x["fixed"] for x in ex])
        self.n = len(ex)

    def prompt(self, p: float, fixed: float) -> float:
        return self.rf * fixed + self.rc * (p - fixed)

    def run(self, x: dict[str, Any]) -> float:
        T = x["r"]["turns"]
        return T * self.rf * x["fixed"] + self.rc * (x["recon"] - T * x["fixed"])

    def prefix(self, x: dict[str, Any], fixed: float | None = None) -> float:
        """The part every question with the same tenant and answer type shares: the fixed part but the question."""
        return self.rf * ((x["fixed"] if fixed is None else fixed) - x["comp"]["question"])


def mape(rows: list[dict[str, Any]], c: Calib | None = None) -> float:
    return mean([abs((c.run(x) if c else x["recon"]) - x["r"]["tokens"]["input"]) / x["r"]["tokens"]["input"] for x in rows])


def cached_cost(prompts: list[float], prefix: float, read: float = READ, write: float = WRITE) -> float:
    """Input cost in units of the input price under prompt caching as bench's direct Anthropic route sets it: a
    breakpoint after the system prompt (tools + system: shared by every question with the same tenant and answer type,
    so read from the cache) and one at the end of each request (the next request reads the whole previous prompt)."""
    cost, prev = 0.0, prefix
    for p in prompts:
        hit = min(prev, p)
        cost += hit * read + (p - hit) * write
        prev = p
    return cost


def openai_cached_cost(prompts: list[float], prefix: float, read: float) -> float:
    """The same with automatic prefix caching and no write premium (OpenAI's scheme), at `read` x the input price."""
    return cached_cost(prompts, prefix, read=read, write=1.0)


# ---- the options -----------------------------------------------------------------------------------------



def usage(rows: list[dict[str, Any]]) -> tuple[Counter, Counter]:
    calls, params = Counter(), Counter()
    for x in rows:
        for c in x["r"]["calls"]:
            calls[c["tool"]] += 1
            for k in c["args"]:
                params[c["tool"], k] += 1
    return calls, params


def param_tokens(tool: dict[str, Any]) -> dict[str, int]:
    props = tool["function"]["parameters"].get("properties", {})
    return {k: tokens(jdump({k: v})) - 2 for k, v in props.items()}  # the braces belong to the object


def place(rows: list[dict[str, Any]], cal: Calib) -> None:
    """Where the parallel calls went: the weight w between the two bounds (0: extras on the last tool turn, 1: on the
    first) that makes the calibrated runs with parallel calls sum to their logged input. Sets each run's w, recon and
    split."""
    par = [x for x in rows if not x["exact"]]
    lo = sum(sum(cal.prompt(p, x["fixed"]) for p in x["lo"]["prompts"]) for x in par)
    hi = sum(sum(cal.prompt(p, x["fixed"]) for p in x["hi"]["prompts"]) for x in par)
    logged = sum(x["r"]["tokens"]["input"] for x in par)
    w = min(1.0, max(0.0, (logged - lo) / (hi - lo))) if hi > lo else 0.5
    for x in rows:
        x["w"] = w
        x["recon"] = (1 - w) * x["lo"]["total"] + w * x["hi"]["total"]
        x["split"] = Counter({k: (1 - w) * x["lo"]["split"][k] + w * x["hi"]["split"][k] for k in x["lo"]["split"] | x["hi"]["split"]})


def bounds(x: dict[str, Any]) -> list[tuple[float, list[float], bool]]:
    """The run's two bounds as (weight, o200k prompts, extras_first)."""
    return [(1 - x["w"], x["lo"]["prompts"], False), (x["w"], x["hi"]["prompts"], True)]


def priced(x: dict[str, Any], cal: Calib, fn: Any = cached_cost, **kw: Any) -> float:
    """The run's input under a caching scheme, in units of the input price (provider tokens)."""
    return sum(wt * fn([cal.prompt(p, x["fixed"]) for p in ps], cal.prefix(x), **kw) for wt, ps, _ in bounds(x))


def saved(x: dict[str, Any], cal: Calib, r2: dict[str, Any], newfixed: float, cached: bool) -> tuple[float, float]:
    """(provider tokens, input-price units) saved when the run is `r2` with fixed part `newfixed`; units under the
    Anthropic caching model when `cached`, else fresh."""
    tok = units = 0.0
    for wt, ps, first in bounds(x):
        old = [cal.prompt(p, x["fixed"]) for p in ps]
        new = [cal.prompt(p, newfixed) for p in rebuild(r2, newfixed, first)["prompts"]]
        tok += wt * (sum(old) - sum(new))
        units += wt * ((cached_cost(old, cal.prefix(x)) - cached_cost(new, cal.prefix(x, newfixed))) if cached else sum(old) - sum(new))
    return tok, units


# ---- the report ------------------------------------------------------------------------------------------


def table(head: list[str], rows: list[list[Any]], align: str | None = None) -> str:
    align = align or "l" + "r" * (len(head) - 1)
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---:" if a == "r" else "---" for a in align) + " |"]
    for r in rows:
        out.append("| " + " | ".join(f"{v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v) for v in r) + " |")
    return "\n".join(out)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def report(data: Path) -> str:
    out: list[str] = []
    w = out.append
    allruns = {c: runs(c, data) for c in CONDITIONS}
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c, rs in allruns.items():
        for x in rs:
            by[c, x["r"]["model"]].append(x)
    keys = [(c, m) for c in CONDITIONS for m in MODELS]
    calib = {k: Calib(by[k]) for k in keys}
    for k in keys:
        place(by[k], calib[k])

    # 1. fixed part per request
    w("## 1. The fixed part of every request\n")
    w("Sent with every request of a run, o200k tokens, mean over the runs (the question and answer format vary).\n")
    comps = ["bench frame", "answer format", "Keel line (bench)", "MCP instructions", "SQL schema+macros text", "tool definitions", "question"]
    rows = []
    for comp in comps:
        rows.append([comp] + [mean([x["comp"].get(comp, 0) for x in allruns[c]]) for c in CONDITIONS])
    rows.append(["**total (whole, tokenized together)**"] + [mean([x["fixed"] for x in allruns[c]]) for c in CONDITIONS])
    w(table(["component", *CONDITIONS], rows))
    kp, kc = keel_parts(False), keel_parts(True)
    w("\nKeel's MCP instructions, line by line (compact adds the TABLES note):\n")
    lines = kc["instructions"].split("\n")
    w(table(["line", "tokens"], [[(ln[:90] + ("..." if len(ln) > 90 else "")).replace("|", "\\|"), tokens(ln)] for ln in lines]))
    w("\nKeel's five tool definitions (as bench sends them: name, description, JSON input schema; no MCP annotations):\n")
    rows = []
    for t in kp["tools"]:
        f = t["function"]
        rows.append([f["name"], tokens(f["description"]), tokens(jdump(f["parameters"])), tokens(jdump(t)), len(f["parameters"].get("properties", {}))])
    rows.append(["**all five**", sum(r[1] for r in rows), sum(r[2] for r in rows), tokens(jdump(kp["tools"])), sum(r[4] for r in rows)])
    w(table(["tool", "description", "input schema", "whole definition", "parameters"], rows))
    st = sql_parts()["tools"][0]["function"]
    w(f"\nSQL's one tool: description {tokens(st['description'])}, schema {tokens(jdump(st['parameters']))}, whole {tokens(jdump(sql_parts()['tools']))}. "
      f"Keel's tool parameters carry no descriptions (FastMCP schemas, compacted by `_compact_schema`): every word an agent reads about a "
      f"parameter is in the tool description.\n")

    # parameter usage
    calls, params = usage(by["keel-plain", "openai/gpt-6-luna"] + by["keel-plain", "anthropic/claude-haiku-5.5"]
                          + by["keel-compact", "openai/gpt-6-luna"] + by["keel-compact", "anthropic/claude-haiku-5.5"])
    w("Each parameter's schema tokens and how often the agents passed it (both A/B arms, both models: "
      f"{sum(calls.values()):,} calls):\n")
    rows = []
    for t in kp["tools"]:
        name = t["function"]["name"]
        for k, v in param_tokens(t).items():
            rows.append([f"{name}.{k}", v, params[name, k], f"{params[name, k] / calls[name]:.0%}" if calls[name] else "-"])
    w(table(["parameter", "schema tokens", "calls passing it", "share of the tool's calls"], rows))
    unused = [(t["function"]["name"], k, v) for t in kp["tools"] for k, v in param_tokens(t).items() if params[t["function"]["name"], k] <= 5]
    unused_tok = sum(v for name, k, v in unused if name != "annotate")
    w(f"\nNever or almost never passed (5 calls or fewer), besides annotate (never called: {calls['annotate']} calls): "
      + ", ".join(f"{n}.{k}" for n, k, _ in unused if n != "annotate") + f" ({unused_tok} schema tokens).\n")

    # 2. per-run accumulation
    w("## 2. Per run: fixed part, re-sent conversation, and the rebuild against the logged tokens\n")
    w("A run's input is the sum of its requests; request k carries the fixed part plus every earlier assistant turn "
      "and tool result. Rebuilt from the log (o200k), mean per run. The log does not say which turn made each parallel "
      "call; the two bounds put the extras on the last tool turn or on the first, and the rebuild "
      "blends them by the weight w that makes the calibrated runs with parallel calls sum to their logged input (Luna: "
      "the extras sit on the last tool turn; Haiku: in between). Assistant turns: each call's name and arguments, plus the output the model wrote besides its calls and "
      "its final reply (logged output minus those), spread over its tool turns.\n")
    parts_order = ["fixed, first request", "fixed, re-sent (requests 2..T)", "tool results, first send", "tool results, re-sent",
                   "assistant, first send", "assistant, re-sent"]
    head = ["per run", *[f"{c} {MODELS[m]}" for c, m in keys]]
    rows = [["requests (turns)", *[f"{mean([x['r']['turns'] for x in by[k]]):.2f}" for k in keys]],
            ["tool calls", *[f"{mean([len(x['r']['calls']) for x in by[k]]):.2f}" for k in keys]]]
    for p in parts_order:
        rows.append([p, *[mean([x["split"][p] for x in by[k]]) for k in keys]])
    rows.append(["**rebuilt total (o200k)**", *[mean([x["recon"] for x in by[k]]) for k in keys]])
    rows.append(["bounds (parallel calls last / first)", *[f"{mean([x['lo']['total'] for x in by[k]]):,.0f} / {mean([x['hi']['total'] for x in by[k]]):,.0f}" for k in keys]])
    rows.append(["w (fitted, 0 = last)", *[f"{by[k][0]['w']:.2f}" for k in keys]])
    rows.append(["**logged input (provider)**", *[mean([x["r"]["tokens"]["input"] for x in by[k]]) for k in keys]])
    rows.append(["logged / rebuilt", *[f"{sum(x['r']['tokens']['input'] for x in by[k]) / sum(x['recon'] for x in by[k]):.2f}" for k in keys]])
    rows.append(["fixed share of rebuilt", *[f"{mean([x['split']['fixed, first request'] + x['split']['fixed, re-sent (requests 2..T)'] for x in by[k]]) / mean([x['recon'] for x in by[k]]):.0%}" for k in keys]])
    rows.append(["re-sent share of rebuilt", *[f"{mean([x['split']['fixed, re-sent (requests 2..T)'] + x['split']['tool results, re-sent'] + x['split']['assistant, re-sent'] for x in by[k]]) / mean([x['recon'] for x in by[k]]):.0%}" for k in keys]])
    w(table(head, rows))

    w("\n### How close the rebuild gets\n")
    w("The rebuild counts o200k; each provider counts its own tokens, renders the tool definitions its own way and adds "
      "framing. Calibration per condition and model, on the runs whose shape is known (one call, then the answer): "
      "logged = 2 x rf x fixed + rc x conversation. rf: provider tokens per o200k token of the fixed part; rc: per token "
      "of conversation. MAPE: mean absolute error per run, raw (o200k as is) and calibrated, over all runs and over the "
      "runs with at most one call per turn (whose every call's turn is known).\n")
    rows = []
    for k in keys:
        c = calib[k]
        ex = [x for x in by[k] if x["exact"]]
        rows.append([f"{k[0]} {MODELS[k[1]]}", len(by[k]), c.n, f"{c.rf:.2f}", f"{mean([c.rf * x['fixed'] for x in by[k]]):,.0f}", f"{c.rc:.2f}",
                     f"{mape(by[k]):.1%}", f"{mape(by[k], c):.1%}", len(ex), f"{mape(ex):.1%}", f"{mape(ex, c):.1%}",
                     f"{sum(c.run(x) for x in by[k]) / sum(x['r']['tokens']['input'] for x in by[k]) - 1:+.1%}"])
    w(table(["condition", "runs", "fit on", "rf", "fixed per request (provider)", "rc", "MAPE raw", "MAPE calibrated",
             "exact runs", "exact MAPE raw", "exact MAPE calibrated", "calibrated total vs logged"], rows))
    lk, ls = calib["keel-compact", "openai/gpt-6-luna"], calib["sql", "openai/gpt-6-luna"]
    kf = mean([x["fixed"] for x in by["keel-compact", "openai/gpt-6-luna"]])
    sf = mean([x["fixed"] for x in by["sql", "openai/gpt-6-luna"]])
    kt, stt = tokens(jdump(kc_tools := keel_parts(True)["tools"])), tokens(jdump(sql_parts()["tools"]))
    del kc_tools
    # Luna counts SQL's prose at o200k (rf ~ 1); the rest of Keel's difference is its tool definitions
    prose = (kf - kt) - (sf - stt)
    tool_ratio = (lk.rf * kf - ls.rf * sf - ls.rf * prose) / (kt - stt)
    w(f"\nOn Luna, SQL's fixed part (mostly prose) weighs what o200k says (rf {ls.rf:.2f}) and tool results weigh "
      f"rc ~1.0; Keel's fixed part weighs less (rf {lk.rf:.2f}) because the provider renders JSON tool definitions more "
      f"tightly than their JSON: taking SQL's rf for the prose, Keel's {kt:,} o200k tokens of tool definitions weigh about "
      f"{tool_ratio:.2f}x, ~{tool_ratio * kt:,.0f} Luna tokens per request. On Haiku everything weighs more (Claude's "
      f"tokenizer on JSON and text, plus Anthropic's tool-use preamble): rf ~1.8, rc ~1.4-1.5.\n")

    # Keel vs SQL gap
    w("\n### Where the gap to SQL comes from (per run, provider tokens: the rebuild weighed by the calibration)\n")
    rows = []

    def parts_of(k: tuple[str, str]) -> tuple[float, float, float]:
        c = calib[k]
        f = mean([c.rf * (x["split"]["fixed, first request"] + x["split"]["fixed, re-sent (requests 2..T)"]) for x in by[k]])
        res = mean([c.rc * (x["split"]["tool results, first send"] + x["split"]["tool results, re-sent"]) for x in by[k]])
        ast = mean([c.rc * (x["split"]["assistant, first send"] + x["split"]["assistant, re-sent"]) for x in by[k]])
        return f, res, ast

    for m, name in MODELS.items():
        fs, rs_, as_ = parts_of(("sql", m))
        for c in ("keel-plain", "keel-compact"):
            fk, rk, ak = parts_of((c, m))
            gap = (fk + rk + ak) - (fs + rs_ + as_)
            lg = mean([x["r"]["tokens"]["input"] for x in by[c, m]]) - mean([x["r"]["tokens"]["input"] for x in by["sql", m]])
            rows.append([f"{c} {name}", lg, gap, f"{fk - fs:,.0f} ({(fk - fs) / gap:.0%})", f"{rk - rs_:,.0f} ({(rk - rs_) / gap:.0%})",
                         f"{ak - as_:,.0f} ({(ak - as_) / gap:.0%})"])
    w(table(["Keel vs SQL", "logged gap", "modelled gap", "from the fixed part x requests", "from tool results (sent + re-sent)", "from assistant turns"], rows))
    w("\nThe fixed part's share splits further into its size per request and the number of requests:\n")
    rows = []
    for m, name in MODELS.items():
        s = by["sql", m]
        for c in ("keel-plain", "keel-compact"):
            k = by[c, m]
            Fk, Fs = mean([x["fixed"] for x in k]), mean([x["fixed"] for x in s])
            Tk, Ts = mean([x["r"]["turns"] for x in k]), mean([x["r"]["turns"] for x in s])
            ck, cs = calib[c, m], calib["sql", m]
            Fk, Fs = ck.rf * Fk, cs.rf * Fs
            rows.append([f"{c} {name}", f"{Fk:,.0f} vs {Fs:,.0f}", f"{Tk:.2f} vs {Ts:.2f}", (Fk - Fs) * Ts, Fk * (Tk - Ts)])
    w(table(["Keel vs SQL", "fixed per request (provider tokens)", "requests per run", "bigger fixed part x SQL's requests", "Keel's extra requests x its fixed part"], rows))
    w("\nBy number of requests (compact arm): the share of runs and of logged input. A run's k-th request re-sends "
      "everything before it, so the long runs weigh more than their count.\n")
    rows = []
    for m, name in MODELS.items():
        xs = by["keel-compact", m]
        tot = sum(x["r"]["tokens"]["input"] for x in xs)
        for T in sorted({x["r"]["turns"] for x in xs}):
            g = [x for x in xs if x["r"]["turns"] == T]
            rows.append([name, T, len(g), f"{len(g) / len(xs):.0%}", mean([x["r"]["tokens"]["input"] for x in g]),
                         f"{sum(x['r']['tokens']['input'] for x in g) / tot:.0%}", f"{mean([x['r']['correct'] for x in g]):.0%}"])
    w(table(["model", "requests", "runs", "share of runs", "mean logged input", "share of input", "accuracy"], rows))
    w("\nBy question type: mean logged input per run (compact arm against SQL) and mean requests.\n")
    rows = []
    for t in sorted({x["r"]["type"] for x in by["sql", "openai/gpt-6-luna"]}):
        row: list[Any] = [t]
        for m in MODELS:
            k = [x for x in by["keel-compact", m] if x["r"]["type"] == t]
            s_ = [x for x in by["sql", m] if x["r"]["type"] == t]
            ki, si = mean([x["r"]["tokens"]["input"] for x in k]), mean([x["r"]["tokens"]["input"] for x in s_])
            row += [f"{ki:,.0f} / {si:,.0f} ({ki / si:.1f}x)", f"{mean([x['r']['turns'] for x in k]):.1f} / {mean([x['r']['turns'] for x in s_]):.1f}"]
        rows.append(row)
    w(table(["type", "Luna input Keel / SQL", "Luna requests", "Haiku input Keel / SQL", "Haiku requests"], rows))

    # 3. cost and caching
    w("\n## 3. Cost per run and what caching does\n")
    w("Input cost per run in micro-dollars. Logged: bench's own cost (Luna: every prompt token at the gateway's input price, "
      "no cache discount; Haiku: straight to Anthropic with prompt caching, cache reads at 0.1x and writes at 1.25x). "
      "Modelled: the rebuilt prompts (scaled by the calibration), priced fresh, and priced with caching as bench's direct "
      "route sets it (the tools+system prefix read from the cache, each request reading the previous one).\n")
    rows = []
    model_cost: dict[tuple[str, str], float] = {}
    for k in keys:
        cal = calib[k]
        pin, pout = PRICES[k[1]]
        logged_in = mean([x["r"]["cost_usd"] - x["r"]["tokens"]["output"] * pout for x in by[k]])
        fresh = mean([cal.run(x) * pin for x in by[k]])

        anth = mean([priced(x, cal) * pin for x in by[k]])
        oai = mean([priced(x, cal, openai_cached_cost, read=0.1) * pin for x in by[k]])
        model_cost[k] = anth if "anthropic" in k[1] else fresh
        rows.append([f"{k[0]} {MODELS[k[1]]}", logged_in * 1e6, fresh * 1e6, anth * 1e6, oai * 1e6,
                     mean([x["r"]["cost_usd"] for x in by[k]]) * 1e6, f"{mean([x['r']['correct'] for x in by[k]]):.1%}",
                     mean([x["r"]["cost_usd"] for x in by[k]]) / mean([x["r"]["correct"] for x in by[k]]) * 1e6])
    w(table(["condition", "logged input cost", "modelled, fresh", "modelled, Anthropic caching", "modelled, OpenAI-style caching at 0.1x",
             "logged total cost", "accuracy", "total cost per correct"], rows))
    w("\nHaiku's logged input cost already sits near the cached model: its prompts are mostly cache reads. Luna's are "
      "billed as fresh; if the gateway passed on an OpenAI-style cached-input discount, its Keel input cost would fall to "
      "the last modelled column. How far Keel's fixed part is a cost depends on that: on Haiku, a token removed from the "
      "fixed part saves about 0.1x of its price per request; on Luna as billed, the full price.\n")

    # options
    w("## 4. Reduction options, priced on this eval\n")
    w("Per run, on the compact arm (the one becoming the default). Tokens: o200k saved per request x mean requests, "
      "then weighed by each model's calibration (rf for the fixed part, rc for the conversation). Cost: Luna at the full input price; Haiku through the cached model "
      "(the saving recomputed with the smaller prompts). Contract: descriptions and the instructions text are prose, "
      "outside tests/contract.json (its MCP part freezes inputSchema and annotations only), so rewording needs no "
      "refresh; removing a parameter or an enum value is breaking (SPEC §11: deprecate first); adding an opt-in is a "
      "refresh.\n")

    def saving(cond: str, delta_fixed: float = 0.0, scale_results: float = 1.0) -> dict[str, tuple[float, float]]:
        """(input tokens, $) saved per run for each model, by recomputing every run with a smaller fixed part and/or
        tool results scaled by `scale_results`."""
        res = {}
        for m in MODELS:
            k = (cond, m)
            cal = calib[k]
            pin = PRICES[m][0]
            tok, usd = [], []
            for x in by[k]:
                r = x["r"]
                newfixed = x["fixed"] - delta_fixed
                r2 = r | {"calls": [c | {"tokens": c["tokens"] * scale_results} for c in r["calls"]]}
                t, u = saved(x, cal, r2, newfixed, "anthropic" in m)
                tok.append(t)
                usd.append(u * pin)
            res[m] = (mean(tok), mean(usd))
        return res

    def fewer_turns(cond: str) -> dict[str, tuple[float, float]]:
        """Upper bound: every run that called tools does it in one turn (all its calls in parallel), then answers."""
        res = {}
        for m in MODELS:
            k = (cond, m)
            cal = calib[k]
            pin = PRICES[m][0]
            tok, usd = [], []
            for x in by[k]:
                r = x["r"]
                if r["turns"] <= 2:
                    tok.append(0.0)
                    usd.append(0.0)
                    continue
                t, u = saved(x, cal, r | {"turns": 2}, x["fixed"], "anthropic" in m)
                tok.append(t)
                usd.append(u * pin)
            res[m] = (mean(tok), mean(usd))
        return res

    cond = "keel-compact"
    frame_tok = tokens(system_prompt("sql", "hyperledger", "value", tools_text="")) - tokens(
        "the value as a JSON string (for example \"In Progress\" or \"user-1a2b3c4d\"), or null when the field is empty (unassigned, unresolved).")
    tables_tok = tokens("\n" + kc["tables"])
    rules_tok = sum(tokens(ln) for ln in lines if ln.startswith(("- ", "Rules,")))
    rels_tok = sum(tokens(ln) for ln in lines if ln.startswith("Relations"))
    drop = {(n, k) for n, k, _ in unused if n != "annotate"}

    from keel.tools import DESCRIPTIONS as now

    def trimmed(short: bool = False, no_annotate: bool = False, no_unused: bool = False, no_defaults: bool = False) -> int:
        """o200k saved per request by sending this tool list instead of today's (each change measured on the whole list)."""
        out = []
        for t in kc["tools"]:
            f = t["function"]
            if no_annotate and f["name"] == "annotate":
                continue
            props = {k: ({kk: vv for kk, vv in v.items() if kk != "default"} if no_defaults else v)
                     for k, v in f["parameters"].get("properties", {}).items() if not (no_unused and (f["name"], k) in drop)}
            out.append({"type": "function", "function": {"name": f["name"], "description": now[f["name"]] if short else f["description"],
                                                         "parameters": f["parameters"] | {"properties": props}}})
        return tokens(jdump(kc["tools"])) - tokens(jdump(out))

    schema_defaults = trimmed(no_defaults=True)
    opts = [
        ("Fixed: shorter tool descriptions (applied: keel/tools.py DESCRIPTIONS now)", trimmed(short=True), "core (keel/tools.py DESCRIPTIONS)",
         "none: prose, outside contract.json", "low-medium: the dropped clauses are the ones the agents rarely need, but each was added after a misuse"),
        ("Fixed: drop never-used parameters from the schemas (" + ", ".join(f"{n}.{k}" for n, k, _ in unused if n != "annotate") + ")",
         trimmed(no_unused=True), "core (signatures)", "breaking: deprecation first (SPEC §11)", "none on this eval; other agents lose those features"),
        ("Fixed: hide annotate from a read-only agent's tool list (applied: --read-only, or an agent declared writes: [])", trimmed(no_annotate=True),
         "core (mcp_server lists tools per agent)", "none: the default lists all five, the contract is unchanged", "none on this eval (never called)"),
        ("Fixed: drop schema defaults (they repeat the descriptions)", schema_defaults, "core (_compact_schema)",
         "breaking for the MCP inputSchema snapshot (default removed)", "low"),
        ("Fixed: the four tool-list trims above together (overlaps counted once)", trimmed(True, True, True, True), "core",
         "breaking (parameters, defaults): deprecation first", "low-medium"),
        ("Fixed: the two non-breaking ones together (short descriptions + no annotate for a read-only agent)", trimmed(True, True),
         "core", "none", "low-medium"),
        ("Fixed: drop the TABLES note (compact only)", tables_tok, "core (instructions)", "none: prose",
         "medium-high: it is how the agent reads tables; moving it into a tool description saves nothing"),
        ("Fixed: drop the rules from the instructions (context's evaluation names them)", rules_tok, "core (instructions)", "none: prose",
         "high: rule questions need the definitions to pick the rule; the SQL prompt carries them too"),
        ("Fixed: drop the relations line", rels_tok, "core (instructions)", "none: prose", "high on links questions"),
        ("Fixed: trim bench's frame (shared by every system)", frame_tok * 0.4, "harness only", "n/a",
         "low; helps SQL equally, so it does not move the ratio"),
        ("Results: compact text (already measured in the A/B; becoming the default)", None, "core (default flip, in parallel)",
         "opt-in today: default flip is behaviour, not schema", "measured: accuracy on par"),
        ("Turns: every run in at most 2 requests (upper bound; parallel calls)", None, "agent behaviour / descriptions",
         "n/a", "high if forced: later calls depend on earlier results"),
    ]
    rows = []
    for name, delta, where, contract, risk in opts:
        if name.startswith("Results"):
            # the plain -> compact difference as logged
            sv = {m: (mean([x["r"]["tokens"]["input"] for x in by["keel-plain", m]]) - mean([x["r"]["tokens"]["input"] for x in by["keel-compact", m]]),
                      mean([x["r"]["cost_usd"] for x in by["keel-plain", m]]) - mean([x["r"]["cost_usd"] for x in by["keel-compact", m]])) for m in MODELS}
            per_req = "-"
        elif name.startswith("Turns"):
            sv, per_req = fewer_turns(cond), "-"
        else:
            sv, per_req = saving(cond, delta_fixed=delta), round(delta)
        L, H = sv["openai/gpt-6-luna"], sv["anthropic/claude-haiku-5.5"]
        rows.append([name, per_req, f"{L[0]:,.0f} ({L[0] / mean([x['r']['tokens']['input'] for x in by[cond, 'openai/gpt-6-luna']]):.1%})",
                     f"{L[1] * 1e6:.1f}", f"{H[0]:,.0f} ({H[0] / mean([x['r']['tokens']['input'] for x in by[cond, 'anthropic/claude-haiku-5.5']]):.1%})",
                     f"{H[1] * 1e6:.1f}", where, contract, risk])
    # caching for Luna
    cal = calib[cond, "openai/gpt-6-luna"]
    pin = PRICES["openai/gpt-6-luna"][0]
    lx = by[cond, "openai/gpt-6-luna"]
    fresh = mean([cal.run(x) * pin for x in lx])
    for rd in (0.1, 0.25, 0.5):
        cached = mean([priced(x, cal, openai_cached_cost, read=rd) * pin for x in lx])
        rows.append([f"Cost: prompt caching on Luna's route, cached input at {rd}x (if the gateway honours it)", "-", "0 (tokens unchanged)",
                     f"{(fresh - cached) * 1e6:.1f}", "already cached", "0", "deployment / gateway", "n/a", "none"])
    w(table(["option", "o200k saved per request", "Luna input tokens saved per run", "Luna $ saved per run (micro$)",
             "Haiku input tokens saved per run", "Haiku $ saved per run (micro$)", "where", "contract", "accuracy risk"], rows, "lrrrrrlll"))
    lm, hm = by[cond, "openai/gpt-6-luna"], by[cond, "anthropic/claude-haiku-5.5"]
    w(f"\nFor scale, on the compact arm: Luna {mean([x['r']['tokens']['input'] for x in lm]):,.0f} input tokens and "
      f"{mean([x['r']['cost_usd'] for x in lm]) * 1e6:.0f} micro$ per run; Haiku {mean([x['r']['tokens']['input'] for x in hm]):,.0f} "
      f"and {mean([x['r']['cost_usd'] for x in hm]) * 1e6:.0f}. SQL: Luna {mean([x['r']['tokens']['input'] for x in by['sql', 'openai/gpt-6-luna']]):,.0f} "
      f"and {mean([x['r']['cost_usd'] for x in by['sql', 'openai/gpt-6-luna']]) * 1e6:.0f}; Haiku "
      f"{mean([x['r']['tokens']['input'] for x in by['sql', 'anthropic/claude-haiku-5.5']]):,.0f} and "
      f"{mean([x['r']['cost_usd'] for x in by['sql', 'anthropic/claude-haiku-5.5']]) * 1e6:.0f}.\n")
    w("Descriptions as logged -> now (o200k): " + ", ".join(f"{n} {tokens(LOGGED_DESCRIPTIONS[n])} -> {tokens(d)}" for n, d in now.items())
      + f"; all five {sum(tokens(d) for d in LOGGED_DESCRIPTIONS.values())} -> {sum(tokens(d) for d in now.values())}.\n")

    # 5. this code's fixed part
    from evals.jira import bench

    w("## 5. The fixed part this code sends (the new arm)\n")
    w("Built as bench now builds it: the current descriptions, the instructions the bound server says (the tenant's one "
      "project named, the rules line naming search(rule=)), with all five tools and with bench --keel-read-only (four). "
      "o200k, mean over the compact arm's questions; provider tokens by each model's compact-arm calibration (rf). Saved "
      "per run: every compact-arm run recomputed with the smaller fixed part at its logged number of requests (the "
      "requests the other changes may save come on top, evals/jira/requests.md).\n")
    qs = questions(data)
    variants = [("as logged (A/B compact arm)", {}), ("now, five tools", {"now": True}), ("now, --keel-read-only", {"now": True, "read_only": True})]
    rows, deltas = [], {}
    base = {}
    for name, kw in variants:
        per = []
        comp = Counter()
        for x in by[cond, "openai/gpt-6-luna"]:
            r = x["r"]
            q = qs[r["id"]]
            kp_ = keel_parts(True, project=bench.INSTANCES[r["tenant"]][1] if kw else None, **kw)
            fixed = tokens(system_prompt("keel-compact", r["tenant"], q["answer_type"], tools_text=kp_["tools_text"])) + tokens(jdump(kp_["tools"])) + tokens(q["question"])
            per.append(fixed)
            comp["tool definitions"] += tokens(jdump(kp_["tools"]))
            comp["descriptions"] += sum(tokens(t["function"]["description"]) for t in kp_["tools"])
            comp["instructions"] += tokens(kp_["instructions"])
        n = len(per)
        f = mean(per)
        base.setdefault("f", f)
        deltas[name] = base["f"] - f
        lr, hr = calib[cond, "openai/gpt-6-luna"].rf, calib[cond, "anthropic/claude-haiku-5.5"].rf
        rows.append([name, comp["descriptions"] / n, comp["tool definitions"] / n, comp["instructions"] / n, f, f"{lr * f:,.0f}", f"{hr * f:,.0f}"])
    w(table(["fixed part", "descriptions", "tool definitions", "MCP instructions", "total (o200k)", "Luna tokens", "Haiku tokens"], rows))
    rows = []
    for name, d in deltas.items():
        if not d:
            continue
        sv = saving(cond, delta_fixed=d)
        L, H = sv["openai/gpt-6-luna"], sv["anthropic/claude-haiku-5.5"]
        rows.append([name, round(d), f"{L[0]:,.0f} ({L[0] / mean([x['r']['tokens']['input'] for x in by[cond, 'openai/gpt-6-luna']]):.1%})", f"{L[1] * 1e6:.1f}",
                     f"{H[0]:,.0f} ({H[0] / mean([x['r']['tokens']['input'] for x in by[cond, 'anthropic/claude-haiku-5.5']]):.1%})", f"{H[1] * 1e6:.1f}"])
    w("\n" + table(["against the logged arm", "o200k saved per request", "Luna input tokens saved per run", "Luna $ saved per run (micro$)",
                     "Haiku input tokens saved per run", "Haiku $ saved per run (micro$)"], rows) + "\n")
    sq = mean([x["fixed"] for x in by["sql", "openai/gpt-6-luna"]])
    w(f"SQL's fixed part, for scale: {sq:,.0f} o200k.\n")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=Path("~/datasets/public-jira/export"))
    a = ap.parse_args()
    print(report(a.data.expanduser()))


if __name__ == "__main__":
    main()
