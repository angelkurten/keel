// Typed fetch wrappers for the control plane (same origin, cookie session, JSON; errors are {detail}).

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = (data as { detail?: unknown }).detail;
    throw new ApiError(res.status, typeof d === "string" ? d : d ? JSON.stringify(d) : `${res.status} ${res.statusText}`);
  }
  return data as T;
}

export type Slug = { account: string; available: boolean; error?: string };
export type StepState = "done" | "active" | "pending" | "failed";
export type Me = {
  email: string;
  account: string;
  endpoint: string;
  status: "creating" | "starting" | "loading" | "ready" | "failed";
  error: string | null;
  steps: { id: string; label: string; state: StepState }[];
};
export type Cartridge = {
  name: string;
  description?: string;
  entities?: Record<string, { description?: string; ref?: string }>;
  relations?: Record<string, { from: string; to: string }>;
  rules?: Record<string, { applies_to?: string; definition?: string }>;
  agents?: Record<string, unknown>;
  events?: Record<string, unknown>;
  example: boolean;
  [k: string]: unknown;
};
export type Key = { key: string; id: string; tenant: string; endpoint: string; mcp: string };
export type Model = { id: string; label: string };
export type Allowance = { used: number; limit: number };
export type Models = { models: Model[]; default: string; allowance: Allowance };
export type AgentConfig = { model: string; system: string; tools: string[]; agent?: string; tenant: string; name: string };
export type ChatMessage = {
  role: "user" | "assistant" | "tool" | "system";
  content: string | null;
  tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[];
  tool_call_id?: string;
};
export type TraceRow = { tool: string; arguments: unknown; ok: boolean; result: unknown };
export type ChatOut = { messages: ChatMessage[]; trace: TraceRow[]; allowance: Allowance };
export type OwnKey = { provider: "openai" | "anthropic"; key: string };

export const TOOLS = ["context", "history", "search", "annotate", "related"] as const;
export type Tool = (typeof TOOLS)[number];

const q = (o: Record<string, string>) => new URLSearchParams(o).toString();

export const api = {
  slugForEmail: (email: string) => req<Slug>("GET", `/api/slug?${q({ email })}`),
  slugCheck: (account: string) => req<Slug>("GET", `/api/slug?${q({ account })}`),
  signup: (body: { email: string; account: string; password: string; invite: string }) =>
    req<{ ok: true }>("POST", "/api/signup", body),
  login: (body: { email: string; password: string }) => req<{ ok: true }>("POST", "/api/login", body),
  signout: () => req<{ ok: true }>("POST", "/api/signout", {}),
  me: () => req<Me>("GET", "/api/me"),
  cartridge: () => req<Cartridge>("GET", "/api/cartridge"),
  tool: <T = unknown>(tool: Tool, body: Record<string, unknown>, tenant: string, agent?: string) =>
    req<T>("POST", `/api/v1/${tool}`, body, { "X-Keel-Tenant": tenant, ...(agent ? { "X-Keel-Agent": agent } : {}) }),
  createKey: (body: { tenant?: string; agent?: string }) => req<Key>("POST", "/api/keys", body),
  models: () => req<Models>("GET", "/api/agent/models"),
  agents: () => req<{ name: string; config: AgentConfig }[]>("GET", "/api/agents"),
  saveAgent: (name: string, config: AgentConfig) => req<unknown>("PUT", `/api/agents/${encodeURIComponent(name)}`, { config }),
  deleteAgent: (name: string) => req<unknown>("DELETE", `/api/agents/${encodeURIComponent(name)}`),
  chat: (body: {
    model: string;
    system: string;
    tools: string[];
    tenant: string;
    agent?: string;
    messages: ChatMessage[];
    own_key?: OwnKey;
  }) => req<ChatOut>("POST", "/api/agent/chat", body),
};
