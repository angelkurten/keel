# /// script
# requires-python = ">=3.10"
# dependencies = ["keel-context[mcp]>=0.27", "langchain>=1.0", "langchain-openai>=1.0", "langchain-mcp-adapters>=0.1"]
# ///
"""A LangGraph agent (LangChain's create_agent) that reads the fleet graph through Keel's five MCP tools.

Run (any OpenAI-compatible model):

    OPENAI_API_KEY=... uv run examples/fleet/agent_langgraph.py
    OPENAI_BASE_URL=https://your-gateway/v1 KEEL_DEMO_MODEL=some/model OPENAI_API_KEY=... uv run examples/fleet/agent_langgraph.py

The agent gets no fleet code and no prompt about tires: it finds the unit, its tires, the rule that says
what "critical" means and the history, all through the tools, and writes its conclusion back as task memory.
"""

import asyncio
import os
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

HERE = Path(__file__).resolve().parent
QUESTION = (
    "Is any tire on unit MXK-4821 critical? Since when, and what was its depth on 2026-09-20? "
    "Quote the threshold from the rule, then record your assessment on the tire as task memory, "
    "with the depth fact's evidence."
)


async def main() -> None:
    keel = MultiServerMCPClient({
        "keel": {  # one stdio server bound to one tenant; it ingests the demo stream at start
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "keel.cli", "serve", str(HERE), "--tenant", "acme-logistics", "--events", str(HERE / "events.jsonl")],
        }
    })
    tools = await keel.get_tools()
    model = ChatOpenAI(model=os.environ.get("KEEL_DEMO_MODEL", "gpt-4.1-mini"), temperature=0)
    agent = create_agent(model, tools, system_prompt=(
        "You answer from Keel's tools only. Subjects are keys like `<kind>:<id>`: find them with `search` before "
        "you read them, and follow relations with `related`. Cite dates and values exactly as the tools return them."
    ))
    result = await agent.ainvoke({"messages": [{"role": "user", "content": QUESTION}]})
    for m in result["messages"]:
        for call in getattr(m, "tool_calls", None) or []:
            print(f"→ {call['name']}({call['args']})")
    print("\n" + result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
