"""The two support agents. Same model, same prompt but for one paragraph, same help center; they differ only in how
they read the customer's data: Keel's five tools, or the product's own API plus similarity search over the same events
as text (RAG + tools), the strongest agent without Keel we could build.
Every model call is metered (tokens, cost at the gateway's prices) and every side is timed end to end."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from showcase import product
from showcase.data import TODAY, Chunk
from showcase.rag import Index, embed

RESULT_CHARS = 12_000
BASE = """You are a support agent for Relay, a workflow-automation SaaS (plans Starter, Pro, Enterprise). You are helping a \
support rep with the customer {name}. Today is {today}.

Answer the rep's question briefly and precisely: cite ids (invoices, tickets, incidents) and dates, and say what to do \
next when it matters. Use your tools before answering; never invent facts; if the data does not say, say so. Product \
policies (refunds, SLA, proration, setup guides) are in the help center: search_help. Earlier conversations may have \
recorded notes about this customer: look for them whenever they could change the answer.

{data}"""
KEEL_DATA = """Customer data: Keel, a context graph of this customer built from Relay's events, through five tools. Subjects \
are `<kind>:<id>` (kinds: account, contact, invoice, ticket, incident, integration); the customer is `account:{tenant}`. \
`context` returns a subject's current facts, its relations and the rules that apply with their evaluation \
(sla_breached, refund_eligible, account_at_risk, integration_failing): trust that evaluation. `history` gives a fact's \
values over time; `search` finds subjects (by text, kind or rule); `related` walks relations; `annotate` records \
what you learn, with evidence; notes recorded earlier come back in `context` of the subject they are about."""
RAG_DATA = """Customer data: Relay's own API for this customer (get_account, list_subscription_changes, list_invoices, \
list_tickets, get_ticket with its activity, list_incidents, list_integrations, list_contacts), and search_records, which \
finds this customer's records (billing, support, platform and integration events) by meaning, each with its date and \
time. Use them as often as you need. save_note stores a note about the customer for later conversations."""

HELP_TOOL = {"type": "function", "function": {"name": "search_help", "description": "Search Relay's help center (policies and how-to guides).",
             "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
RAG_TOOLS = [
    {"type": "function", "function": {"name": "search_records", "description": "Search this customer's records by meaning; returns the closest ones, each with its date.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "description": "how many (default 8, max 20)"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "save_note", "description": "Save a note about this customer, found by search_records later.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    HELP_TOOL,
]


@cache
def keel_tools() -> list[dict[str, Any]]:
    """Keel's five tools as chat functions, read from its MCP server: the definitions agents see over MCP."""
    from keel.mcp_server import build_server

    listed = asyncio.run(build_server(lambda _r: None, name="keel").list_tools())  # type: ignore[arg-type,return-value]
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "",
             "parameters": getattr(t, "input_schema", None) or t.inputSchema}} for t in listed]


@dataclass
class Settings:
    base_url: str = "https://api.ofox.ai/v1"
    key: str = ""
    model: str = "openai/gpt-6-sol"
    judge_model: str = "anthropic/claude-sonnet-5.5"
    keel_url: str = "http://127.0.0.1:8000"
    keel_key: str | None = None
    max_steps: int = 10


@dataclass
class Meter:
    prices: dict[str, tuple[float, float]]
    input: int = 0
    output: int = 0
    cost: float = 0.0

    def add(self, model: str, usage: dict[str, Any]) -> None:
        i, o = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        pin, pout = self.prices.get(model, (0.0, 0.0))
        self.input, self.output, self.cost = self.input + i, self.output + o, self.cost + i * pin + o * pout


def prices(http: Any, s: Settings) -> dict[str, tuple[float, float]]:
    """USD per token, from the gateway's catalogue."""
    r = http.get(f"{s.base_url}/models", headers={"Authorization": f"Bearer {s.key}"}, timeout=30)
    r.raise_for_status()
    return {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])) for m in r.json()["data"] if m.get("pricing")}


def chat(http: Any, s: Settings, meter: Meter, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    r = http.post(f"{s.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {s.key}"}, timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"the model answered {r.status_code}: {r.text[:300]}")
    data = r.json()
    meter.add(model, data.get("usage") or {})
    msg = data["choices"][0]["message"]
    out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                             for c in msg["tool_calls"]]
    return out


@dataclass
class Run:
    """What one side needs for one conversation: the customer, and the notes or task its memory lives in."""
    tenant: str
    name: str
    task: str | None = None  # Keel: task-scoped memory for the memory pair, so each run starts clean
    notes: list[tuple[Chunk, list[float]]] = field(default_factory=list)  # RAG: notes saved in this run


class Agents:
    def __init__(self, http: Any, s: Settings, index: Index, price: dict[str, tuple[float, float]], api: product.Product | None = None) -> None:
        self.http, self.s, self.index, self.prices = http, s, index, price
        self.api = api or product.Product()

    # ---- tools ----

    def _embed(self, text: str) -> list[float]:
        return embed(self.http, self.s.base_url, self.s.key, [text])[0]

    def search_help(self, query: str) -> list[str]:
        return [c.text for c in self.index.search(self._embed(query), None, k=4)]

    def keel(self, run: Run, tool: str, args: dict[str, Any]) -> Any:
        if tool == "search_help":
            return self.search_help(str(args.get("query", "")))
        if tool in ("context", "search", "related") and not args.get("as_of"):
            args["as_of"] = TODAY.isoformat()  # today is fixed, so the rules are evaluated on the scenario's day
        if run.task and tool in ("context", "history", "annotate") and not args.get("task"):
            args["task"] = run.task
        headers = {"X-Keel-Tenant": run.tenant, **({"Authorization": f"Bearer {self.s.keel_key}"} if self.s.keel_key else {})}
        r = self.http.post(f"{self.s.keel_url}/v1/{tool}", json=args, headers=headers, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text[:200])
        return r.json()

    def rag(self, run: Run, tool: str, args: dict[str, Any]) -> Any:
        if tool == "search_help":
            return self.search_help(str(args.get("query", "")))
        if tool == "search_records":
            k = max(1, min(int(args.get("k") or 8), 20))
            return [c.text for c in self.index.search(self._embed(str(args.get("query", ""))), run.tenant, k=k, extra=run.notes)]
        if tool in {t["function"]["name"] for t in product.TOOLS}:
            return self.api.call(run.tenant, tool, args)
        if tool == "save_note":
            text = f"[note saved {TODAY:%Y-%m-%d}] {args.get('text', '')}"
            run.notes.append((Chunk(text, run.tenant, "note"), self._embed(text)))
            return {"saved": True}
        raise RuntimeError(f"unknown tool {tool}")

    # ---- one side, end to end ----

    def answer(self, side: str, run: Run, history: list[dict[str, Any]], question: str, memory: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Answer `question` after `history` (earlier turns of the same conversation). Returns the side's result and
        the conversation to continue from."""
        meter = Meter(self.prices)
        data = KEEL_DATA.format(tenant=run.tenant) if side == "keel" else RAG_DATA
        system = BASE.format(name=run.name, today=f"{TODAY:%Y-%m-%d %H:%M} UTC", data=data)
        if side == "keel":
            tools = [t for t in keel_tools() if memory or t["function"]["name"] != "annotate"] + [HELP_TOOL]
            call = lambda name, args: self.keel(run, name, args)
        else:
            tools = [*product.TOOLS, *(t for t in RAG_TOOLS if memory or t["function"]["name"] != "save_note")]
            call = lambda name, args: self.rag(run, name, args)
        allowed = {t["function"]["name"] for t in tools}
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
        trace: list[dict[str, Any]] = []
        start, error, answer = time.monotonic(), None, ""
        try:
            for _ in range(self.s.max_steps):
                msg = chat(self.http, self.s, meter, self.s.model, messages, tools)
                messages.append(msg)
                if not msg.get("tool_calls"):
                    answer = msg["content"]
                    break
                for c in msg["tool_calls"]:
                    name, args = c["function"]["name"], {}
                    try:
                        args = json.loads(c["function"]["arguments"] or "{}")
                        if name not in allowed:
                            raise RuntimeError(f"tool '{name}' is not available")
                        result, ok = call(name, args), True
                    except Exception as exc:  # noqa: BLE001 - the model reads the error, as over MCP
                        result, ok = {"error": str(exc)}, False
                    text = json.dumps(result, default=str, ensure_ascii=False)[:RESULT_CHARS]
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
                    trace.append({"tool": name, "arguments": args, "ok": ok, "result": text[:1500]})
            else:
                answer = "(stopped after the step limit)"
        except Exception as exc:  # noqa: BLE001 - shown in the side's column
            error = str(exc)
        ms = round((time.monotonic() - start) * 1000)
        result = {"answer": answer, "verdict": None, "trace": trace, "ms": ms, "tokens": {"input": meter.input, "output": meter.output},
                  "cost_usd": round(meter.cost, 6), "calls": len(trace), **({"error": error} if error else {})}
        return result, [m for m in messages[1:] if m["role"] in ("user", "assistant") and not m.get("tool_calls")]

    def judge(self, question: str, expected: str, answer: str) -> dict[str, str]:
        meter = Meter(self.prices)
        prompt = ("You grade a support agent's answer against the expected answer. Grade `correct` when it states the expected "
                  "facts and conclusion (wording may differ, extra correct detail is fine), `partial` when it is incomplete or "
                  "hedges on the key fact, `wrong` when it states something contrary to the expected answer or misses it. "
                  'Reply with JSON only: {"grade": "correct|partial|wrong", "reason": "<one sentence>"}.\n\n'
                  f"Question: {question}\nExpected: {expected}\nAnswer: {answer}")
        text = chat(self.http, self.s, meter, self.s.judge_model, [{"role": "user", "content": prompt}])["content"]
        try:
            v = json.loads(text[text.index("{") : text.rindex("}") + 1])
            if v.get("grade") in ("correct", "partial", "wrong"):
                return {"grade": v["grade"], "reason": str(v.get("reason", ""))[:400]}
        except ValueError:
            pass
        return {"grade": "partial", "reason": "the judge's answer could not be read"}

