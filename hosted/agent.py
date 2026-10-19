"""The playground's agent: a model with Keel's five tools, every call sent to the account's Keel.

The loop runs in the control plane because it is a try-out; the agent a customer ships lives in their
product (the console exports it as code). Models are reached through any OpenAI-compatible chat API:
OfoxAI with the control plane's key and a daily allowance, or the customer's own OpenAI or Anthropic key.
"""

import asyncio
import json
from collections.abc import Callable
from functools import cache
from typing import Any

BASE_URLS = {"ofox": "https://api.ofox.ai/v1", "openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1"}
Chat = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]  # (messages, tool definitions) -> assistant message
RESULT_CHARS = 20_000  # what one tool result may put in front of the model


@cache
def specs() -> dict[str, dict[str, Any]]:
    """The five tools as chat-API function definitions, read from the MCP server so names, descriptions and
    parameters never drift from what agents see over MCP. No tenant is needed to list them."""
    from keel.mcp_server import build_server

    listed = asyncio.run(build_server(lambda _request: None, name="keel").list_tools())  # type: ignore[arg-type, return-value]
    return {t.name: {"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                      "parameters": getattr(t, "input_schema", None) or t.inputSchema}} for t in listed}


def model_for(model: str, provider: str) -> str:
    """The model id a provider expects: OfoxAI takes `anthropic/claude-sonnet-5.5`, Anthropic `claude-sonnet-5-5`."""
    if provider == "ofox":
        return model
    owner, _, name = model.partition("/")
    if owner != provider:
        raise ValueError(f"pick a model from {provider} to use your {provider} key")
    return name.replace(".", "-") if provider == "anthropic" else name


def chat_api(http: Any, provider: str, key: str, model: str) -> Chat:
    url = f"{BASE_URLS[provider]}/chat/completions"
    model = model_for(model, provider)

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        r = http.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"the model answered {r.status_code}: {r.text[:300]}")
        msg = r.json()["choices"][0]["message"]
        out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                                 for c in msg["tool_calls"]]
        return out

    return chat


def run(chat: Chat, call_tool: Callable[[str, dict[str, Any]], Any], system: str,
        messages: list[dict[str, Any]], tools: list[str], max_steps: int = 8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the conversation until the model answers without calling a tool. Returns the new messages and a trace
    of the tool calls. A tool that fails answers the model with its error, the way an MCP client would."""
    allowed = specs()
    defs = [allowed[t] for t in tools if t in allowed]
    history = [{"role": "system", "content": system}, *messages] if system else list(messages)
    new: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for _ in range(max_steps):
        msg = chat(history + new, defs)
        new.append(msg)
        if not msg.get("tool_calls"):
            break
        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                if name not in tools:
                    raise PermissionError(f"tool '{name}' is not enabled for this agent")
                result, ok = call_tool(name, args), True
            except Exception as exc:  # noqa: BLE001 - the model reads the error and may retry
                args = args if "args" in locals() and isinstance(args, dict) else {}
                result, ok = {"error": str(exc)}, False
            text = json.dumps(result, default=str)[:RESULT_CHARS]
            new.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            trace.append({"tool": name, "arguments": args, "ok": ok, "result": text[:2000]})
    else:
        new.append({"role": "assistant", "content": f"(stopped after {max_steps} steps)"})
    return new, trace
