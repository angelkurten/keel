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

// --- Events: an envelope from the catalog, and how to post it ------------------------------------
type Hint = { kind: "scalar"; type: string; nullable: boolean } | { kind: "enum"; values: unknown[]; nullable: boolean }
  | { kind: "array"; item: Hint; nullable: boolean } | { kind: "object"; fields: Record<string, Hint>; nullable: boolean };

/** The cartridge's payload notation (keel/schema.py): `string | null`, `string[]`, `[a, b]`, `{...}`, `[{...}]`. */
function parseHint(spec: unknown): Hint {
  if (Array.isArray(spec)) {
    if (spec.length === 1 && spec[0] && typeof spec[0] === "object" && !Array.isArray(spec[0])) {
      return { kind: "array", item: parseHint(spec[0]), nullable: false };
    }
    return { kind: "enum", values: spec.filter((v) => v !== null), nullable: spec.includes(null) };
  }
  if (spec && typeof spec === "object") {
    return { kind: "object", fields: Object.fromEntries(Object.entries(spec).map(([k, v]) => [k, parseHint(v)])), nullable: false };
  }
  const text = String(spec ?? "any").trim();
  const nullable = /\|\s*null$/.test(text);
  const base = text.replace(/\s*\|\s*null$/, "");
  if (base.endsWith("[]")) return { kind: "array", item: { kind: "scalar", type: base.slice(0, -2), nullable: false }, nullable };
  return { kind: "scalar", type: base, nullable };
}

const today = () => new Date().toISOString().slice(0, 10);
const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

function placeholder(h: Hint, key: string): unknown {
  switch (h.kind) {
    case "enum": return h.values[0] ?? null;
    case "array": return [placeholder(h.item, key)];
    case "object": return Object.fromEntries(Object.entries(h.fields).map(([k, v]) => [k, placeholder(v, k)]));
    case "scalar":
      switch (h.type) {
        case "int": return 1;
        case "number": return 1.5;
        case "bool": return true;
        case "date": return today();
        case "datetime": return nowIso();
        case "any": return null;
        default: return key === "id" ? "example-1" : `${key}-1`;
      }
  }
}

export function firstEvent(c: Cartridge | null): [string, NonNullable<Cartridge["events"]>[string]] | null {
  const e = Object.entries(c?.events || {});
  return e.length ? e[0] : null;
}

export function exampleEnvelope(c: Cartridge | null, tenant: string): Record<string, unknown> {
  const ev = firstEvent(c);
  const [type, spec] = ev || ["thing.happened", { subject: "thing", payload: {} }];
  const kind = spec.subject || Object.keys(c?.entities || {})[0] || "thing";
  return {
    id: "evt-0001",
    tenant,
    type,
    subject: `${kind}:example-1`,
    occurred_at: nowIso(),
    source: spec.owner || "my-product",
    payload: Object.fromEntries(Object.entries(spec.payload || {}).map(([k, v]) => [k, placeholder(parseHint(v), k)])),
  };
}

export const WEBHOOK_LANGS = ["curl", "Python", "Node"] as const;
export type WebhookLang = (typeof WEBHOOK_LANGS)[number];

export function webhookSnippet(lang: WebhookLang, endpoint: string, tenant: string, envelope: unknown): string {
  const url = `${endpoint.replace(/\/$/, "")}/v1/events`;
  const body = JSON.stringify([envelope], null, 2);
  if (lang === "curl") {
    return [
      `curl ${url} \\`,
      `  -H "Authorization: Bearer $KEEL_KEY" \\`,
      `  -H "X-Keel-Tenant: ${tenant}" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '${body.replace(/'/g, "'\\''")}'`,
    ].join("\n");
  }
  if (lang === "Python") {
    return [
      "import os, httpx",
      "",
      `events = [${pyJson(envelope)}]`,
      "r = httpx.post(",
      `    ${py(url)},`,
      `    headers={"Authorization": f"Bearer {os.environ['KEEL_KEY']}", "X-Keel-Tenant": ${py(tenant)}},`,
      "    json=events,",
      ")",
      "r.raise_for_status()",
      "print(r.json())  # {processed, duplicates, unhandled, errors, ...}",
    ].join("\n");
  }
  return [
    `const events = ${body};`,
    "",
    `const res = await fetch(${JSON.stringify(url)}, {`,
    '  method: "POST",',
    "  headers: {",
    "    Authorization: `Bearer ${process.env.KEEL_KEY}`,",
    `    "X-Keel-Tenant": ${JSON.stringify(tenant)},`,
    '    "Content-Type": "application/json",',
    "  },",
    "  body: JSON.stringify(events),",
    "});",
    "if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);",
    "console.log(await res.json()); // {processed, duplicates, unhandled, errors, ...}",
  ].join("\n");
}

// --- SDK: install, and a typed example per language (docs/sdks.md) -------------------------------
export const SDK_LANGS = [
  { id: "typescript", name: "TypeScript", install: "npm install @keel-context/sdk" },
  { id: "python", name: "Python", install: "pip install keel-context" },
  { id: "go", name: "Go", install: "go get github.com/angelkurten/keel/sdk/go" },
  { id: "php", name: "PHP", install: "composer require keel-context/sdk" },
] as const;

const pascal = (s: string) => s.split(/[._\-\s]+/).filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join("");
const camel = (s: string) => { const p = pascal(s); return p.charAt(0).toLowerCase() + p.slice(1); };
const snake = (s: string) => s.replace(/[.\-\s]+/g, "_").replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();

/** The event the typed example emits: the first one whose payload is plain scalars (enums and nested
 * objects need generated types the example would have to guess); optional fields are left out. */
function sdkEvent(c: Cartridge | null): { type: string; kind: string; source: string; fields: [string, string, unknown][] } | null {
  for (const [type, spec] of Object.entries(c?.events || {})) {
    const hints = Object.entries(spec.payload || {}).map(([k, v]) => [k, parseHint(v)] as const);
    if (!spec.subject || hints.some(([, h]) => h.kind !== "scalar" || h.type === "any")) continue;
    const fields = hints.filter(([, h]) => !h.nullable)
      .map(([k, h]) => [k, (h as { type: string }).type, placeholder(h, k)] as [string, string, unknown]);
    return { type, kind: spec.subject, source: spec.owner || "my-product", fields };
  }
  return null;
}

/** Struct literal fields, aligned as gofmt does. */
function goFields(fields: [string, string, unknown][]): string[] {
  const w = Math.max(0, ...fields.map(([f]) => pascal(f).length));
  return fields.map(([f, , v]) => `\t\t${(pascal(f) + ":").padEnd(w + 1)} ${JSON.stringify(v)},`);
}

const phpLit = (v: unknown) => (typeof v === "string" ? `'${v.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'` : typeof v === "boolean" ? String(v) : String(v));
const pyLit = (v: unknown) => (typeof v === "boolean" ? (v ? "True" : "False") : JSON.stringify(v));

export function sdkExample(lang: string, c: Cartridge | null, filename: string, endpoint: string, tenant: string): string {
  const ev = sdkEvent(c);
  const name = c?.name || "app";
  const base = endpoint.replace(/\/$/, "");
  const sid = "example-1";
  if (!ev) return "# The cartridge has no event with a plain payload to show here; see docs/sdks.md for the full API.";
  const { type, kind, source, fields } = ev;
  if (lang === "typescript") {
    const mod = "./" + filename.replace(/\.ts$/, "");
    return [
      `import { client, emit, subjects } from "${mod}";`,
      "",
      `const keel = client({ baseUrl: ${JSON.stringify(base)}, tenant: ${JSON.stringify(tenant)}, secret: process.env.KEEL_KEY });`,
      "",
      "await keel.events([",
      `  emit.${camel(type)}({`,
      `    tenant: ${JSON.stringify(tenant)},`,
      `    subject: subjects.${kind}(${JSON.stringify(sid)}),`,
      "    occurredAt: new Date(),",
      `    source: ${JSON.stringify(source)},`,
      `    payload: { ${fields.map(([k, , v]) => `${k}: ${JSON.stringify(v)}`).join(", ")} },`,
      "  }),",
      "]);",
      "",
      `const ctx = await keel.context(subjects.${kind}(${JSON.stringify(sid)}));`,
      "console.log(ctx.facts, ctx.evaluation);",
    ].join("\n");
  }
  if (lang === "python") {
    const mod = filename.replace(/\.py$/, "");
    return [
      "import os",
      "from datetime import datetime, timezone",
      "",
      `import ${mod} as k`,
      "",
      `keel = k.client(${py(base)}, ${py(tenant)}, secret=os.environ["KEEL_KEY"])`,
      "",
      "keel.events([",
      `    k.emit.${snake(type)}(${py(tenant)}, k.subjects.${kind}(${py(sid)}), datetime.now(timezone.utc), ${py(source)},`,
      `        {${fields.map(([f, , v]) => `${py(f)}: ${pyLit(v)}`).join(", ")}}),`,
      "])",
      "",
      `print(keel.context(k.subjects.${kind}(${py(sid)})))`,
    ].join("\n");
  }
  if (lang === "go") {
    const pkg = `keel${name}`.replace(/[-_]/g, "");
    return [
      "package main",
      "",
      "import (",
      '\t"context"',
      '\t"fmt"',
      '\t"os"',
      '\t"time"',
      "",
      '\tkeel "github.com/angelkurten/keel/sdk/go"',
      `\t"yourmodule/internal/${pkg}" // where you put ${filename} (package ${pkg})`,
      ")",
      "",
      "func main() {",
      "\tctx := context.Background()",
      `\tc := ${pkg}.NewClient(keel.Options{BaseURL: ${JSON.stringify(base)}, Tenant: ${JSON.stringify(tenant)}, Secret: os.Getenv("KEEL_KEY")})`,
      `\tsubject := ${pkg}.Subject${pascal(kind)}(${JSON.stringify(sid)})`,
      "",
      `\tevent := ${pkg}.Emit${pascal(type)}(${JSON.stringify(tenant)}, subject, time.Now(), ${JSON.stringify(source)}, ${pkg}.${pascal(type)}Payload{`,
      ...goFields(fields),
      "\t})",
      "\tif _, err := c.Events(ctx, []keel.Envelope{event}); err != nil {",
      "\t\tpanic(err)",
      "\t}",
      "",
      "\tout, err := c.Context(ctx, string(subject), nil)",
      "\tif err != nil {",
      "\t\tpanic(err)",
      "\t}",
      '\tfmt.Printf("%+v\\n", out)',
      "}",
    ].join("\n");
  }
  const ns = `Keel\\${pascal(name)}`;
  return [
    "<?php",
    "require 'vendor/autoload.php';",
    `require '${filename}';`,
    "",
    `use ${ns}\\{Emit, Keel, Subjects, ${pascal(type)}Payload};`,
    "",
    `$keel = Keel::client('${base}', '${tenant}', getenv('KEEL_KEY'));`,
    "",
    "$keel->events([",
    `    Emit::${camel(type)}('${tenant}', Subjects::${camel(kind)}('${sid}'), new DateTimeImmutable(), '${source}',`,
    `        new ${pascal(type)}Payload(${fields.map(([f, , v]) => `${f}: ${phpLit(v)}`).join(", ")})),`,
    "]);",
    "",
    `print_r($keel->context(Subjects::${camel(kind)}('${sid}')));`,
  ].join("\n");
}
