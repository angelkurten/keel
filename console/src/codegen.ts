// Everything the console writes as text, built client-side and deterministically: the starter system prompt,
// "same call from code" snippets, agent exports per framework, and the per-client Connect snippets.
import type { Cartridge } from "./api";

const py = (v: unknown) => JSON.stringify(v); // JSON strings are valid Python string literals
const pyDict = (o: Record<string, string>) =>
  "{" + Object.entries(o).map(([k, v]) => `${py(k)}: ${v}`).join(", ") + "}";

/** A system prompt filled from the cartridge: no LLM writes it. */
export function starterPrompt(c: Cartridge | null): string {
  if (!c) return "";
  const kinds = Object.entries(c.entities || {});
  const rels = Object.entries(c.relations || {});
  const rules = Object.entries(c.rules || {});
  const lines = [
    `You are an assistant for the ${c.name} product. You answer from Keel, a context graph of this tenant's data, through five tools: search, context, history, related and annotate.`,
    ...(c.description ? ["", c.description] : []),
    "",
    "What the graph holds:",
    ...kinds.map(([k, v]) => {
      const out = rels.filter(([, r]) => r.from === k).map(([n, r]) => `${n} -> ${r.to}`);
      return `- ${k}${v.description ? `: ${v.description}` : ""}${out.length ? ` Relations: ${out.join(", ")}.` : ""}`;
    }),
    "Subjects are keys like `<kind>:<id>`, e.g. `" + (kinds[0]?.[0] || "kind") + ":<id>`.",
  ];
  if (rules.length) {
    lines.push("", "Rules (read their definitions and current values through the tools; never redefine them):",
      ...rules.map(([n, r]) => `- ${n}${r.applies_to ? ` (${r.applies_to})` : ""}`));
  }
  lines.push(
    "",
    "How to answer:",
    "- Call search to find subjects and context to read them before answering; follow relations with related, and use history for how a value changed over time.",
    "- Cite the subjects you used and the evidence ids the tools return.",
    "- Never invent facts. If the tools do not have it, say so.",
    "- Write a learning with annotate only when the user asks you to record it, with evidence.",
  );
  return lines.join("\n");
}

export type Target = { endpoint: string; tenant: string; agent?: string };
const mcpUrl = (t: Target) => `${t.endpoint.replace(/\/$/, "")}/mcp`;

// --- Playground: the same tool call from code --------------------------------------------------
export function curlFor(t: Target, tool: string, body: unknown): string {
  return [
    `curl ${t.endpoint.replace(/\/$/, "")}/v1/${tool} \\`,
    `  -H "Authorization: Bearer $KEEL_KEY" \\`,
    `  -H "X-Keel-Tenant: ${t.tenant}" \\`,
    ...(t.agent ? [`  -H "X-Keel-Agent: ${t.agent}" \\`] : []),
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify(body).replace(/'/g, "'\\''")}'`,
  ].join("\n");
}

export function pythonFor(t: Target, tool: string, body: unknown): string {
  const headers: Record<string, string> = { Authorization: 'f"Bearer {os.environ[\'KEEL_KEY\']}"', "X-Keel-Tenant": py(t.tenant) };
  if (t.agent) headers["X-Keel-Agent"] = py(t.agent);
  return [
    "import os, httpx",
    "",
    "r = httpx.post(",
    `    ${py(`${t.endpoint.replace(/\/$/, "")}/v1/${tool}`)},`,
    `    headers=${pyDict(headers)},`,
    `    json=${pyJson(body)},`,
    ")",
    "r.raise_for_status()",
    "print(r.json())",
  ].join("\n");
}

function pyJson(v: unknown): string {
  return JSON.stringify(v).replace(/\btrue\b/g, "True").replace(/\bfalse\b/g, "False").replace(/\bnull\b/g, "None");
}

// --- Agent export -------------------------------------------------------------------------------
export type AgentSpec = Target & { name: string; system: string; model: string; tools: string[] };
export const FRAMEWORKS = [
  { id: "claude", name: "Claude Agent SDK", file: "agent_claude.py" },
  { id: "langgraph", name: "LangGraph", file: "agent_langgraph.py" },
  { id: "openai", name: "OpenAI Agents SDK", file: "agent_openai.py" },
  { id: "adk", name: "Google ADK", file: "agent.py" },
] as const;
export type Framework = (typeof FRAMEWORKS)[number]["id"];

function header(deps: string[], doc: string[]): string[] {
  return [
    "# /// script",
    '# requires-python = ">=3.10"',
    `# dependencies = [${deps.map(py).join(", ")}]`,
    "# ///",
    '"""' + doc[0],
    ...doc.slice(1),
    '"""',
    "",
  ];
}

function mcpHeaders(s: AgentSpec): string {
  const h: Record<string, string> = { Authorization: 'f"Bearer {KEEL_KEY}"', "X-Keel-Tenant": py(s.tenant) };
  if (s.agent) h["X-Keel-Agent"] = py(s.agent);
  return pyDict(h);
}

const pyStr = (s: string) => (s.includes('"""') || s.includes("\\") ? py(s) : `"""\\\n${s}\n"""`);

/** The chosen model id for OpenAI-compatible frameworks: openai/<id> keeps <id>, else a default. */
function openaiModel(model: string): { id: string; note?: string } {
  if (model.startsWith("openai/")) return { id: model.slice("openai/".length) };
  return { id: "gpt-4.1-mini", note: `# The playground ran ${model}; this file uses an OpenAI model. Any OpenAI-compatible model works.` };
}

export function exportAgent(fw: Framework, s: AgentSpec): string {
  const url = mcpUrl(s);
  const question = "What needs attention right now? Cite subjects and evidence.";
  const common = [
    `KEEL_URL = ${py(url)}`,
    'KEEL_KEY = os.environ["KEEL_KEY"]  # a key from the Keel console (Connect)',
    `SYSTEM = ${pyStr(s.system)}`,
  ];
  const askLine = `QUESTION = " ".join(sys.argv[1:]) or ${py(question)}`;

  if (fw === "claude") {
    return [
      ...header(["claude-agent-sdk"], [
        `${s.name}: a Claude Agent SDK agent on Keel's five MCP tools.`,
        "",
        "Run:  KEEL_KEY=... ANTHROPIC_API_KEY=... uv run agent_claude.py \"your question\"",
      ]),
      "import asyncio",
      "import os",
      "import sys",
      "",
      "from claude_agent_sdk import ClaudeAgentOptions, query",
      "",
      ...common,
      askLine,
      "",
      "options = ClaudeAgentOptions(",
      "    system_prompt=SYSTEM,",
      '    model="claude-sonnet-5-5",',
      `    mcp_servers={"keel": {"type": "http", "url": KEEL_URL, "headers": ${mcpHeaders(s)}}},`,
      `    allowed_tools=[${s.tools.map((t) => py(`mcp__keel__${t}`)).join(", ")}],`,
      ")",
      "",
      "",
      "async def main() -> None:",
      "    async for message in query(prompt=QUESTION, options=options):",
      "        print(message)",
      "",
      "",
      'if __name__ == "__main__":',
      "    asyncio.run(main())",
      "",
    ].join("\n");
  }

  if (fw === "langgraph") {
    const m = openaiModel(s.model);
    return [
      ...header(["langchain>=1.0", "langchain-openai>=1.0", "langchain-mcp-adapters>=0.1"], [
        `${s.name}: a LangGraph agent (LangChain's create_agent) on Keel's five MCP tools.`,
        "",
        "Run:  KEEL_KEY=... OPENAI_API_KEY=... uv run agent_langgraph.py \"your question\"",
        "Any OpenAI-compatible gateway: set OPENAI_BASE_URL as well.",
      ]),
      "import asyncio",
      "import os",
      "import sys",
      "",
      "from langchain.agents import create_agent",
      "from langchain_mcp_adapters.client import MultiServerMCPClient",
      "from langchain_openai import ChatOpenAI",
      "",
      ...common,
      ...(m.note ? [m.note] : []),
      `MODEL = ${py(m.id)}`,
      `TOOLS = {${s.tools.map(py).join(", ")}}`,
      askLine,
      "",
      "",
      "async def main() -> None:",
      "    keel = MultiServerMCPClient({",
      `        "keel": {"transport": "streamable_http", "url": KEEL_URL, "headers": ${mcpHeaders(s)}},`,
      "    })",
      "    tools = [t for t in await keel.get_tools() if t.name in TOOLS]",
      "    agent = create_agent(ChatOpenAI(model=MODEL, temperature=0), tools, system_prompt=SYSTEM)",
      '    result = await agent.ainvoke({"messages": [{"role": "user", "content": QUESTION}]})',
      '    for m in result["messages"]:',
      '        for call in getattr(m, "tool_calls", None) or []:',
      "            print(f\"-> {call['name']}({call['args']})\")",
      '    print("\\n" + result["messages"][-1].text)  # .text joins content blocks',
      "",
      "",
      'if __name__ == "__main__":',
      "    asyncio.run(main())",
      "",
    ].join("\n");
  }

  if (fw === "openai") {
    const m = openaiModel(s.model);
    return [
      ...header(["openai-agents"], [
        `${s.name}: an OpenAI Agents SDK agent on Keel's five MCP tools.`,
        "",
        "Run:  KEEL_KEY=... OPENAI_API_KEY=... uv run agent_openai.py \"your question\"",
      ]),
      "import asyncio",
      "import os",
      "import sys",
      "",
      "from agents import Agent, Runner",
      "from agents.mcp import MCPServerStreamableHttp, create_static_tool_filter",
      "",
      ...common,
      ...(m.note ? [m.note] : []),
      `MODEL = ${py(m.id)}`,
      askLine,
      "",
      "",
      "async def main() -> None:",
      "    async with MCPServerStreamableHttp(",
      `        params={"url": KEEL_URL, "headers": ${mcpHeaders(s)}},`,
      `        tool_filter=create_static_tool_filter(allowed_tool_names=[${s.tools.map(py).join(", ")}]),`,
      "    ) as keel:",
      `        agent = Agent(name=${py(s.name)}, instructions=SYSTEM, mcp_servers=[keel], model=MODEL)`,
      "        result = await Runner.run(agent, QUESTION)",
      "        print(result.final_output)",
      "",
      "",
      'if __name__ == "__main__":',
      "    asyncio.run(main())",
      "",
    ].join("\n");
  }

  // Google ADK
  const ident = s.name.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "keel_agent";
  return [
    ...header(["google-adk"], [
      `${s.name}: a Google ADK agent on Keel's five MCP tools.`,
      "",
      "ADK loads `root_agent` from a package: put this file as agent.py in a folder (e.g. keel_agent/) with an",
      "__init__.py containing `from . import agent`, then from the parent folder:",
      "",
      "    KEEL_KEY=... GOOGLE_API_KEY=... adk run keel_agent     # in the terminal",
      "    KEEL_KEY=... GOOGLE_API_KEY=... adk web                # in the browser",
    ]),
    "import os",
    "",
    "from google.adk.agents import LlmAgent",
    "from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams",
    "",
    ...common,
    "",
    "root_agent = LlmAgent(",
    '    model="gemini-2.5-flash",',
    `    name=${py(ident)},`,
    "    instruction=SYSTEM,",
    "    tools=[",
    "        McpToolset(",
    `            connection_params=StreamableHTTPConnectionParams(url=KEEL_URL, headers=${mcpHeaders(s)}),`,
    `            tool_filter=[${s.tools.map(py).join(", ")}],`,
    "        )",
    "    ],",
    ")",
    "",
  ].join("\n");
}

// --- Connect: per-client snippets --------------------------------------------------------------
export const CLIENTS = ["Claude Code", "Cursor", "VS Code", "Claude Desktop", "LangGraph", "curl"] as const;
export type Client = (typeof CLIENTS)[number];

export function connectHeaders(key: string, tenant: string): Record<string, string> {
  return { Authorization: `Bearer ${key}`, "X-Keel-Tenant": tenant };
}

export function cursorDeeplink(endpoint: string, key: string, tenant: string): string {
  const config = JSON.stringify({ url: mcpUrl({ endpoint, tenant }), headers: connectHeaders(key, tenant) });
  return `cursor://anysphere.cursor-deeplink/mcp/install?name=keel&config=${encodeURIComponent(btoa(config))}`;
}

export function connectSnippet(c: Client, endpoint: string, key: string, tenant: string): { title: string; code: string; note?: string } {
  const url = mcpUrl({ endpoint, tenant });
  const headers = connectHeaders(key, tenant);
  const base = endpoint.replace(/\/$/, "");
  switch (c) {
    case "Claude Code":
      return {
        title: "Terminal",
        code: `claude mcp add --transport http keel ${url} \\\n  --header "Authorization: Bearer ${key}" \\\n  --header "X-Keel-Tenant: ${tenant}"`,
      };
    case "Cursor":
      return { title: "~/.cursor/mcp.json", code: JSON.stringify({ mcpServers: { keel: { url, headers } } }, null, 2) };
    case "VS Code":
      return { title: ".vscode/mcp.json", code: JSON.stringify({ servers: { keel: { type: "http", url, headers } } }, null, 2) };
    case "Claude Desktop":
      return {
        title: "claude_desktop_config.json",
        note: `Settings → Connectors → Add custom connector takes the URL (${url}) but no headers. To send the key and tenant, use mcp-remote in the JSON config instead:`,
        code: JSON.stringify({
          mcpServers: {
            keel: {
              command: "npx",
              args: ["mcp-remote", url, "--header", `Authorization: Bearer ${key}`, "--header", `X-Keel-Tenant: ${tenant}`],
            },
          },
        }, null, 2),
      };
    case "LangGraph":
      return {
        title: "agent.py",
        code: [
          "# uv add langchain langchain-openai langchain-mcp-adapters",
          "import asyncio",
          "from langchain.agents import create_agent",
          "from langchain_mcp_adapters.client import MultiServerMCPClient",
          "",
          "async def main():",
          "    keel = MultiServerMCPClient({\"keel\": {",
          `        "transport": "streamable_http", "url": ${py(url)},`,
          `        "headers": ${JSON.stringify(headers)},`,
          "    }})",
          '    agent = create_agent("openai:gpt-4.1-mini", await keel.get_tools())',
          '    out = await agent.ainvoke({"messages": [{"role": "user", "content": "What do you know about unit MXK-4821?"}]})',
          '    print(out["messages"][-1].text)',
          "",
          "asyncio.run(main())",
        ].join("\n"),
      };
    case "curl":
      return {
        title: "Terminal",
        note: `The /v1 API lives at the endpoint itself (${base}/v1/…), next to /mcp.`,
        code: `curl ${base}/v1/search \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "X-Keel-Tenant: ${tenant}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"text": ""}'`,
      };
  }
}
