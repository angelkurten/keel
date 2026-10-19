// Typed fetch wrappers. Hosted: the control plane (same origin, cookie session, /api/...). Self-hosted
// (`keel serve --ui`): the Keel server's own /v1 API at the origin root, with the secret as a bearer.
// JSON both ways; errors are {detail}.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** How this console is served; set once at startup by `detectMode()`. The secret lives in sessionStorage only. */
export const env: { mode: "hosted" | "self"; secret: string } = { mode: "hosted", secret: "" };
const self = () => env.mode === "self";

/** GET /health answers on both without auth: a Keel server names its cartridge; the hosted control plane does not. */
export async function detectMode(): Promise<"hosted" | "self"> {
  try {
    const res = await fetch(`${window.location.origin}/health`, { credentials: "same-origin" });
    const h = (await res.json()) as { cartridge?: unknown };
    env.mode = res.ok && h && typeof h === "object" && h.cartridge ? "self" : "hosted";
  } catch {
    env.mode = "hosted";
  }
  return env.mode;
}

async function req<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  if (self() && env.secret) headers = { Authorization: `Bearer ${env.secret}`, ...headers };
  const res = await fetch(window.location.origin + path, {
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
  status: "creating" | "starting" | "loading" | "updating" | "ready" | "failed";
  error: string | null;
  notice?: string | null;
  steps: { id: string; label: string; state: StepState }[];
};
export type Cartridge = {
  name: string;
  description?: string;
  entities?: Record<string, { description?: string; ref?: string }>;
  relations?: Record<string, { from: string; to: string }>;
  rules?: Record<string, { applies_to?: string; definition?: string }>;
  agents?: Record<string, unknown>;
  events?: Record<string, EventSpec>;
  example: boolean;
  files?: { "cartridge.yaml"?: string; "handlers.py"?: string };
  [k: string]: unknown;
};
export type EventSpec = { subject?: string; owner?: string; payload?: Record<string, unknown>; [k: string]: unknown };
export type EventStatus = "applied" | "duplicate" | "unhandled" | "skipped" | "error";
export type RecentEvent = { id: string; type: string; subject: string; occurred_at: string; received_at: string; source: string;
  status: EventStatus; reason: string | null };
export type EventProblem = { index: number; id: string | null; type: string | null; problems: string[] };
/** Keel's /v1/events returns `unhandled` and `errors` as lists (count with `count()`); numbers are accepted too. */
export type IngestReport = { processed: number; duplicates: number; unhandled: number | unknown[]; errors: number | unknown[];
  forgotten: unknown[]; skipped: number; retained: unknown[] };
export const count = (v: number | unknown[] | undefined) => (Array.isArray(v) ? v.length : v || 0);
export type CartridgeFiles = { cartridge_yaml: string; handlers_py: string };
export type Example = "fleet" | "crm" | "clinics" | "finance";
export type Generated = CartridgeFiles & { problems: string[]; attempts: number; allowance: Allowance };
export type CdcStatus = { ok: boolean | null; events?: number; last_read?: string; error?: string; failed_at?: string; tables?: string[] };
export type Cdc = { configured: boolean; map: string; tables: string[]; status: CdcStatus | null };
export type KeyRow = { id: string; tenant: string | null; agent: string | null; global: boolean; created_at: string; revoked_at: string | null };
export type UsageRow = { month: string; tenant: string; events: number; calls: Record<string, number> };
export type Usage = { account: string; endpoint: string; limits: { rps: number | null; events_month: number | null }; rows: UsageRow[] };
export type SdkLanguage = "typescript" | "python" | "go" | "php";
export type Sdk = { language: SdkLanguage; cartridge: string; filename: string; code: string; sha256: string };
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
  cartridge: () => req<Cartridge>("GET", self() ? "/v1/cartridge" : "/api/cartridge"),
  /** Self-hosted: check a secret before keeping it. */
  cartridgeWith: (secret: string) => req<Cartridge>("GET", "/v1/cartridge", undefined, { Authorization: `Bearer ${secret}` }),
  tool: <T = unknown>(tool: Tool, body: Record<string, unknown>, tenant: string, agent?: string) =>
    req<T>("POST", self() ? `/v1/${tool}` : `/api/v1/${tool}`, body, { "X-Keel-Tenant": tenant, ...(agent ? { "X-Keel-Agent": agent } : {}) }),
  createKey: (body: { tenant?: string; agent?: string; global?: boolean }) => req<Key>("POST", "/api/keys", body),
  keys: () => req<KeyRow[]>("GET", "/api/keys"),
  revokeKey: (id: string) => req<{ ok: true }>("DELETE", `/api/keys/${encodeURIComponent(id)}`),
  recentEvents: (tenant: string) => req<RecentEvent[]>("GET", self() ? "/v1/events/recent" : "/api/events/recent", undefined, { "X-Keel-Tenant": tenant }),
  checkEvents: (tenant: string, batch: unknown[]) => req<EventProblem[]>("POST", self() ? "/v1/events/check" : "/api/events/check", batch, { "X-Keel-Tenant": tenant }),
  ingestEvents: (tenant: string, batch: unknown[]) => req<IngestReport>("POST", self() ? "/v1/events" : "/api/events", batch, { "X-Keel-Tenant": tenant }),
  usage: (month?: string) => req<Usage>("GET", `/api/usage${month ? `?${q({ month })}` : ""}`),
  deployCartridge: (body: CartridgeFiles | { example: Example }) => req<{ ok: true; problems: string[] }>("PUT", "/api/cartridge", body),
  checkCartridge: (body: CartridgeFiles) => req<{ problems: string[] }>("POST", "/api/cartridge/check", body),
  generateCartridge: (body: { events: unknown[]; description: string }) => req<Generated>("POST", "/api/cartridge/generate", body),
  cdc: () => req<Cdc>("GET", "/api/cdc"),
  setCdc: (body: { dsn: string; map: string }) => req<Cdc>("PUT", "/api/cdc", body),
  stopCdc: () => req<{ ok: true }>("DELETE", "/api/cdc"),
  sdk: (language: SdkLanguage) => req<Sdk>("GET", self() ? `/v1/sdk/${language}` : `/api/sdk/${language}`),
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
