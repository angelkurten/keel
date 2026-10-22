"""Tool-efficiency eval over the golden set (evals/efficiency/golden.py), through the MCP server as a client sees it.

    uv run --extra mcp --with tiktoken python -m evals.efficiency.run static --label baseline
    OFOX_API_KEY=... uv run --extra mcp --with tiktoken python -m evals.efficiency.run agent --label baseline [--reps 3]
    uv run python -m evals.efficiency.run compare baseline r1

static: free and deterministic; tokens are tiktoken o200k_base (a proxy for Claude's tokenizer: compare runs, not vendors).
agent: real models through an OpenAI-compatible gateway (OFOX_API_KEY; EVAL_MODELS, EVAL_JUDGE to change them); tokens
and cost are the gateway's. Results land in evals/efficiency/results/<mode>-<label>.json."""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

from mcp.client.client import Client

from evals.efficiency.golden import EXAMPLES, PROBES, QUESTIONS, TODAY
from keel import Ingestor, MemoryStore, Tools, load_cartridge, mcp_server
from keel.ingest import JsonlAdapter
from keel.mcp_server import build_server

RESULTS = Path(__file__).parent / "results"
MODELS = os.environ.get("EVAL_MODELS", "anthropic/claude-sonnet-5.5,deepseek/deepseek-v4.1-flash").split(",")
JUDGE = os.environ.get("EVAL_JUDGE", "anthropic/claude-sonnet-5.5")
RENDER = os.environ.get("EVAL_RENDER")
SYSTEM = """You are the assistant of {tenant}'s team. Today is {today}. Their data is in Keel, through five tools; subjects \
are '<kind>:<id>'. Answer the question briefly and precisely, citing ids and dates. Use your tools before answering, never \
invent facts, and if the data does not say, say so."""

try:
    import tiktoken

    _enc = tiktoken.get_encoding("o200k_base")

    def tokens(s: str) -> int:
        return len(_enc.encode(s))
except ImportError:  # ponytail: chars/4 when tiktoken is missing; the header of every result says which

    def tokens(s: str) -> int:
        return len(s) // 4


def graph(example: str) -> tuple[Any, MemoryStore]:
    path = EXAMPLES[example]
    cart, store = load_cartridge(path), MemoryStore()
    Ingestor(cart, store).ingest(JsonlAdapter(f"{path}/events.jsonl"))
    return cart, store


def call(server: Any, name: str, args: dict[str, Any]) -> tuple[str, int, bool, str]:
    """What the model reads, through a real MCP client session: the result's text blocks (joined), how many blocks,
    whether it is an error, and the structuredContent a client may also hand the model."""

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(name, args)

    res = asyncio.run(go())
    text = "\n".join(b.text for b in res.content if getattr(b, "type", "") == "text")
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    return text, len(res.content), bool(getattr(res, "is_error", False)), json.dumps(structured) if structured is not None else ""


def tool_list(server: Any) -> list[dict[str, Any]]:
    async def go() -> Any:
        async with Client(server) as client:
            return await client.list_tools()

    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in asyncio.run(go()).tools]


# ---- static ----


def static() -> dict[str, Any]:
    graphs: dict[str, tuple[Any, MemoryStore]] = {}
    probes, defs = {}, None
    for pid, example, tenant, tool, args, must in PROBES:
        if example not in graphs:
            graphs[example] = graph(example)
        cart, store = graphs[example]
        server = build_server(Tools(cart, store, tenant=tenant))
        if defs is None:
            listed = tool_list(server)
            defs = {"tokens": tokens(json.dumps(listed)), "tokens_without_output_schema": tokens(json.dumps([{k: v for k, v in t.items() if k != "outputSchema"} for t in listed]))}
        text, blocks, error, structured = call(server, tool, dict(args))
        again = call(server, tool, dict(args))[0] if tool != "annotate" else text
        probes[pid] = {"tokens": tokens(text), "structured_tokens": tokens(structured), "chars": len(text), "blocks": blocks, "error": error, "identical": text == again,
                       "missing": [m for m in must if m not in text], "head": text[:300]}
    return {"definitions": defs, "probes": probes}


# ---- agent ----


@cache
def chat_tools() -> list[dict[str, Any]]:
    cart, store = graph("fleet")
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}}
            for t in tool_list(build_server(Tools(cart, store, tenant="acme-logistics")))]


def pause(headers: Any = None) -> float:
    """Seconds to wait after a 429. The gateway counts requests per minute, team-wide, so a slot frees within seconds:
    its retry-after, else 1-3 s with jitter (a long backoff only idles while other clients take the slots)."""
    try:
        return max(1.0, float((headers or {}).get("retry-after")))  # it says 0 s when the window is about to turn
    except (TypeError, ValueError):
        return random.uniform(1, 3)


class Gateway:
    def __init__(self) -> None:
        import httpx

        self.base, self.key = os.environ.get("EVAL_BASE_URL", "https://api.ofox.ai/v1"), os.environ["OFOX_API_KEY"]
        self.http = httpx.Client(timeout=180)
        r = self.http.get(f"{self.base}/models", headers=self.auth)
        r.raise_for_status()
        models = r.json()["data"]
        r = self.http.get(f"{self.base.removesuffix('/v1')}/anthropic/v1/models", headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        models += r.json()["data"] if r.is_success else []  # Claude is listed only there
        self.prices = {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])) for m in models if m.get("pricing")}
        key = os.environ.get("ANTHROPIC_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")
        if key:  # Claude straight from Anthropic, not through the gateway (run with `--with anthropic`)
            import anthropic

            self.direct = anthropic.Anthropic(api_key=key, max_retries=8)

    direct: Any = None

    def route(self, model: str) -> str:
        return "anthropic" if model.startswith("anthropic/") and self.direct else "ofox"

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, int], float]:
        if self.route(model) == "anthropic":
            data = self.direct.messages.create(**to_anthropic(direct_id(model), messages, tools, direct=True)).to_dict()
            return from_anthropic(data), *direct_cost(model, data.get("usage") or {})
        if model.startswith("anthropic/"):  # since 2026-10-20 the gateway serves Claude only on its Anthropic endpoint
            url, body, headers = f"{self.base.removesuffix('/v1')}/anthropic/v1/messages", to_anthropic(model, messages, tools), \
                {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        else:
            url, headers = f"{self.base}/chat/completions", self.auth
            body = {"model": model, "messages": messages, "temperature": 0, **({"tools": tools} if tools else {})}
        import httpx

        for attempt in range(8):  # the gateway limits requests per minute and sometimes stalls: back off and retry
            try:
                r = self.http.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                if attempt == 7:
                    raise
                continue
            if r.status_code < 500 and r.status_code != 429:
                break
            time.sleep(pause(r.headers) if r.status_code == 429 else min(60, 5 * 2**attempt))
        if r.status_code >= 400:
            raise RuntimeError(f"{model} answered {r.status_code}: {r.text[:300]}")
        data = r.json()
        if model.startswith("anthropic/"):
            msg, usage = from_anthropic(data), data.get("usage") or {}
            # its input_tokens leaves out what the gateway cached; the prompt is all three
            i = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            o = int(usage.get("output_tokens") or 0)
        else:
            msg, usage = data["choices"][0]["message"], data.get("usage") or {}
            i, o = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        pin, pout = self.prices.get(model, (0.0, 0.0))
        # ponytail: cached tokens priced as fresh input, an upper bound; their own rates if the gap matters
        if model.startswith("anthropic/") and i > 100_000:
            pin *= 5  # the gateway bills a Claude prompt above 100K input tokens at 5x
        return msg, {"input": i, "output": o}, i * pin + o * pout


KEY_FILE = Path.home() / ".anthropic_key"
# Anthropic's list prices (2026-10-06), per token: input, output. Haiku bills a prompt above 100K tokens at 5x; a cache
# read costs 0.1x the input rate, a 5-minute cache write 1.25x.
DIRECT_PRICES = {"anthropic/claude-haiku-5.5": (0.10e-6, 0.50e-6), "anthropic/claude-sonnet-5.5": (2.00e-6, 10.00e-6)}


def direct_id(model: str) -> str:
    """The gateway's name for a Claude model as Anthropic's id: anthropic/claude-haiku-5.5 -> claude-haiku-5-5."""
    return model.split("/", 1)[1].replace(".", "-")


def direct_cost(model: str, usage: dict[str, Any]) -> tuple[dict[str, int], float]:
    fresh, read, write = (int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    o = int(usage.get("output_tokens") or 0)
    pin, pout = DIRECT_PRICES[model]
    if "haiku" in model and fresh + read + write > 100_000:
        pin, pout = pin * 5, pout * 5
    return {"input": fresh + read + write, "output": o}, fresh * pin + read * pin * 0.1 + write * pin * 1.25 + o * pout


def to_anthropic(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, direct: bool = False) -> dict[str, Any]:
    """An OpenAI chat request as an Anthropic messages request: the same prompt, turns and tools, thinking off as on the
    OpenAI endpoint. Consecutive tool results go back in one user turn, as that API wants. Straight to Anthropic, the
    Claude 5.5 models refuse a non-default temperature, Sonnet turns thinking off with between_tools, and the
    conversation is cached (each turn resends the last one)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": t["id"], "name": t["function"]["name"], "input": json.loads(t["function"]["arguments"] or "{}")}
                       for t in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": m["content"]})
    body: dict[str, Any] = {"model": model, "max_tokens": 8192, "thinking": {"type": "between_tools" if "sonnet" in model else "disabled"},
                            "messages": out}
    body |= {"cache_control": {"type": "ephemeral"}} if direct else {"temperature": 0}
    if system := "\n\n".join(m["content"] for m in messages if m["role"] == "system"):
        # its own breakpoint: the one at the end holds the question, so without this no two questions share a cache entry
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if direct else system
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"]["parameters"]}
                         for t in tools]
    return body


def from_anthropic(data: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic reply as the OpenAI message the agent loops read: its text, and its tool calls."""
    blocks = data.get("content") or []
    calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}} for b in blocks if b["type"] == "tool_use"]
    return {"role": "assistant", "content": "".join(b["text"] for b in blocks if b["type"] == "text"), **({"tool_calls": calls} if calls else {})}


def ask(gw: Gateway, model: str, q: dict[str, Any], max_steps: int = 12) -> dict[str, Any]:
    cart, store = graph(q["example"])  # a fresh graph per question: annotations never leak between runs
    server = build_server(Tools(cart, store, tenant=q["tenant"]))
    system = SYSTEM.format(tenant=q["tenant"], today=f"{TODAY:%Y-%m-%d %H:%M} UTC")
    if hasattr(mcp_server, "instructions"):  # what MCP clients put in the system prompt
        system += "\n\n" + mcp_server.instructions(cart)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": q["question"]}]
    used = {"input": 0, "output": 0}
    cost, calls, errors, result_tokens, answer, start = 0.0, [], 0, 0, "", time.monotonic()
    for _ in range(max_steps):
        msg, u, c = gw.chat(model, messages, chat_tools())
        used = {k: used[k] + u[k] for k in used}
        cost += c
        turn = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            turn["tool_calls"] = [{"id": t["id"], "type": "function", "function": {"name": t["function"]["name"], "arguments": t["function"].get("arguments") or "{}"}}
                                  for t in msg["tool_calls"]]
        messages.append(turn)
        if not msg.get("tool_calls"):
            answer = turn["content"]
            break
        for t in turn["tool_calls"]:
            name = t["function"]["name"]
            try:
                args = json.loads(t["function"]["arguments"])
            except ValueError:
                args = {}
            if RENDER and name in ("context", "history") and "detail" not in args:
                args["detail"] = RENDER  # an A/B of the default render (EVAL_RENDER=text), without touching the server
            if name in ("context", "search", "related") and not args.get("as_of"):
                args["as_of"] = TODAY.isoformat()  # the examples are read on TODAY, as in the showcase
            text, _, error, _ = call(server, name, args)
            errors += error
            result_tokens += tokens(text)
            calls.append({"tool": name, "args": args, "tokens": tokens(text), "error": error})
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": text})
    else:
        answer = "(stopped after the step limit)"
    prompt = ("You grade an assistant's answer against the expected answer. Grade `correct` when it states the expected facts and "
              "conclusion (wording may differ, extra correct detail is fine), `partial` when it is incomplete or hedges on the key fact, "
              "`wrong` when it states something contrary to the expected answer or misses it. "
              'Reply with JSON only: {"grade": "correct|partial|wrong", "reason": "<one sentence>"}.\n\n'
              f"Question: {q['question']}\nExpected: {q['expected']}\nAnswer: {answer}")
    verdict = {"grade": "partial", "reason": "unreadable verdict"}
    try:
        text = gw.chat(JUDGE, [{"role": "user", "content": prompt}])[0]["content"] or ""
        verdict = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except (ValueError, RuntimeError):
        pass
    return {"id": q["id"], "model": model, "grade": verdict.get("grade"), "reason": verdict.get("reason"), "answer": answer,
            "calls": calls, "tool_errors": errors, "result_tokens": result_tokens, "turns": len([m for m in messages if m["role"] == "assistant"]),
            "tokens": used, "cost_usd": round(cost, 6), "ms": round((time.monotonic() - start) * 1000)}


def agent(reps: int, earlier: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every (model, question) `reps` times; with `earlier` (the same label's results), only its failed runs again."""
    gw = Gateway()
    by_id = {q["id"]: q for q in QUESTIONS}
    kept = [r for r in earlier["runs"] if r["grade"] != "error"] if earlier else []
    jobs = [(r["model"], by_id[r["id"]]) for r in earlier["runs"] if r["grade"] == "error"] if earlier else \
        [(m, q) for m in MODELS for q in QUESTIONS for _ in range(reps)]
    with ThreadPoolExecutor(4) as pool:
        runs = kept + list(pool.map(lambda job: _safe(gw, *job), jobs))
    return {"models": MODELS, "judge": JUDGE, "reps": earlier["reps"] if earlier else reps, "runs": runs, "summary": summarize(runs)}


def _safe(gw: Gateway, model: str, q: dict[str, Any]) -> dict[str, Any]:
    try:
        return ask(gw, model, q)
    except Exception as exc:  # noqa: BLE001 - one failed run is reported, not fatal
        return {"id": q["id"], "model": model, "grade": "error", "reason": str(exc)[:300], "calls": [], "tool_errors": 0, "result_tokens": 0,
                "turns": 0, "tokens": {"input": 0, "output": 0}, "cost_usd": 0.0, "ms": 0}


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for model in sorted({r["model"] for r in runs}):
        rs = [r for r in runs if r["model"] == model and r["grade"] != "error"]
        score = {"correct": 1.0, "partial": 0.5}
        out[model] = {"runs": len(rs), "failed": sum(r["model"] == model and r["grade"] == "error" for r in runs),
                      "accuracy": round(sum(score.get(r["grade"], 0) for r in rs) / max(len(rs), 1), 3),
                      "correct": sum(r["grade"] == "correct" for r in rs),
                      "calls": round(statistics.mean(len(r["calls"]) for r in rs), 2) if rs else 0,
                      "turns": round(statistics.mean(r["turns"] for r in rs), 2) if rs else 0,
                      "tool_errors": sum(r["tool_errors"] for r in rs),
                      "result_tokens": round(statistics.mean(r["result_tokens"] for r in rs)) if rs else 0,
                      "input_tokens": round(statistics.mean(r["tokens"]["input"] for r in rs)) if rs else 0,
                      "output_tokens": round(statistics.mean(r["tokens"]["output"] for r in rs)) if rs else 0,
                      "cost_usd": round(sum(r["cost_usd"] for r in rs), 4)}
    return out


# ---- compare ----


def compare(a: str, b: str) -> None:
    for mode in ("static", "agent"):
        pa, pb = RESULTS / f"{mode}-{a}.json", RESULTS / f"{mode}-{b}.json"
        if not (pa.exists() and pb.exists()):
            continue
        x, y = json.loads(pa.read_text()), json.loads(pb.read_text())
        if mode == "static":
            print(f"{'probe':32} {a:>8} {b:>8}  change   blocks  same-bytes  missing")
            for pid, p in y["probes"].items():
                o = x["probes"].get(pid, {})
                t0, t1 = o.get("tokens", 0), p["tokens"]
                ch = f"{(t1 - t0) / t0:+.0%}" if t0 else "new"
                print(f"{pid:32} {t0:>8} {t1:>8}  {ch:>6}   {o.get('blocks', '-')}->{p['blocks']:<4} {p['identical']!s:>10}  {','.join(p['missing']) or '-'}")
            tot0, tot1 = sum(p["tokens"] for p in x["probes"].values()), sum(p["tokens"] for p in y["probes"].values())
            print(f"{'TOTAL':32} {tot0:>8} {tot1:>8}  {(tot1 - tot0) / tot0:+.0%}")
            print(f"{'tool definitions':32} {x['definitions']['tokens']:>8} {y['definitions']['tokens']:>8}")
        else:
            keys = ("accuracy", "correct", "calls", "turns", "tool_errors", "result_tokens", "input_tokens", "output_tokens", "cost_usd")
            for model, s in y["summary"].items():
                o = x["summary"].get(model, {})
                print(f"\n{model}")
                for k in keys:
                    print(f"  {k:14} {o.get(k, '-')!s:>10} {s[k]!s:>10}")


def main(argv: list[str]) -> None:
    mode = argv[0] if argv else "static"
    if mode == "compare":
        compare(argv[1], argv[2])
        return
    label = argv[argv.index("--label") + 1] if "--label" in argv else "latest"
    reps = int(argv[argv.index("--reps") + 1]) if "--reps" in argv else 3
    earlier = json.loads((RESULTS / f"agent-{label}.json").read_text()) if "--retry-failed" in argv else None
    out = static() if mode == "static" else agent(reps, earlier)
    out["tokenizer"] = "o200k_base" if "tiktoken" in sys.modules else "chars/4"
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{mode}-{label}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    if mode == "static":
        for pid, p in out["probes"].items():
            print(f"{pid:32} {p['tokens']:>7} tok {p['blocks']:>4} blocks  same-bytes={p['identical']!s:5} error={p['error']!s:5} missing={p['missing'] or '-'}")
        print("tool definitions:", out["definitions"])
    else:
        print(json.dumps(out["summary"], indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
