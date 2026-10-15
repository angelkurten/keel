// @keel-context/sdk: the envelope and the five Keel tools over HTTP.
// Generated files (`keel sdk typescript <cartridge>`) import this and add the cartridge's types.

export type Scope = "global" | "tenant" | "task" | "agent";

/** The standard event envelope. `id` is optional: the server derives a content hash when absent. */
export type Envelope = {
  id?: string;
  tenant: string;
  type: string;
  subject: string;
  occurred_at: string;
  recorded_at?: string;
  source: string;
  payload: Record<string, unknown>;
};

export type Entity = { tenant: string; kind: string; id: string; ref: string | null; attrs: Record<string, unknown> };
export type Fact = {
  tenant: string;
  subject: string;
  predicate: string;
  value: unknown;
  valid_from: string;
  valid_to: string | null;
  recorded_at: string | null;
  source: string | null;
  scope: Scope;
  corrected_by: string | null;
  evidence: string | null;
  expires_at: string | null;
};
export type RelationRow = {
  tenant: string;
  src: string;
  rel: string;
  dst: string;
  valid_from: string;
  valid_to: string | null;
  recorded_at: string | null;
  source: string | null;
};
export type Context = {
  subject: string;
  entity: Entity | null;
  facts: Fact[];
  relations: RelationRow[];
  rules: Record<string, Record<string, unknown>>;
  evaluation: Record<string, boolean | null>;
  as_of: string;
  /** Present with `depth: 1`: each neighbour's entity, open facts and evaluation (capped; see neighbors_truncated). */
  neighbors?: Neighbor[];
  neighbors_truncated?: boolean;
};
export type Related = { relation: string; direction: "in" | "out" | "both"; subject: string; since: string };
export type Neighbor = {
  relation: string;
  direction: "in" | "out" | "both";
  subject: string;
  entity: Entity | null;
  facts: Fact[];
  evaluation: Record<string, boolean | null>;
};
export type IngestReport = { processed: number; duplicates: number; unhandled: string[]; errors: Array<[string, string]> };

export type ClientOptions = {
  /** Where `keel serve --transport http` listens, e.g. http://keel.internal:8000 */
  baseUrl: string;
  /** The end-customer every call is scoped to (X-Keel-Tenant). */
  tenant: string;
  /** The shared secret (KEEL_SECRET on the server). */
  secret?: string;
  /** The declared agent acting (X-Keel-Agent); required when the cartridge declares agents. */
  agent?: string;
  fetch?: typeof fetch;
};

export class KeelError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`keel ${status}: ${detail}`);
    this.name = "KeelError";
  }
}

const iso = (d: Date | string): string => (d instanceof Date ? d.toISOString() : d);

export type EnvelopeArgs = {
  type: string;
  tenant: string;
  subject: string;
  occurredAt: Date | string;
  source: string;
  payload: Record<string, unknown>;
  id?: string;
  recordedAt?: Date | string;
};

/** Build an envelope; dates become ISO 8601. The generated `emit.*` constructors call this. */
export function envelope(a: EnvelopeArgs): Envelope {
  return {
    ...(a.id ? { id: a.id } : {}),
    tenant: a.tenant,
    type: a.type,
    subject: a.subject,
    occurred_at: iso(a.occurredAt),
    ...(a.recordedAt ? { recorded_at: iso(a.recordedAt) } : {}),
    source: a.source,
    payload: a.payload,
  };
}

/** The five tools plus `events` (ingest), bound to one tenant and optionally one agent. */
export class Client<S extends string = string> {
  constructor(private readonly options: ClientOptions) {}

  private async post<T>(path: string, body: unknown): Promise<T> {
    const o = this.options;
    const doFetch = o.fetch ?? fetch;
    const res = await doFetch(`${o.baseUrl.replace(/\/+$/, "")}${path}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-keel-tenant": o.tenant,
        ...(o.agent ? { "x-keel-agent": o.agent } : {}),
        ...(o.secret ? { authorization: `Bearer ${o.secret}` } : {}),
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail: unknown = res.statusText;
      try {
        detail = ((await res.json()) as { detail?: unknown }).detail ?? detail;
      } catch {
        // not JSON: keep the status text
      }
      throw new KeelError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return (await res.json()) as T;
  }

  /** What is true about a subject now, or at `asOf`: facts, relations, rules and their verdicts. `depth: 1` adds the neighbours. */
  context(subject: S, asOf?: Date | string, depth = 0): Promise<Context> {
    return this.post("/v1/context", { subject, as_of: asOf ? iso(asOf) : undefined, depth });
  }

  /** Facts over time, oldest first, closed ones included. */
  history(subject: S, f: { since?: Date | string; until?: Date | string; predicate?: string; scope?: Scope } = {}): Promise<Fact[]> {
    return this.post("/v1/history", {
      subject,
      since: f.since ? iso(f.since) : undefined,
      until: f.until ? iso(f.until) : undefined,
      predicate: f.predicate,
      scope: f.scope,
    });
  }

  search(text: string): Promise<Array<Entity | Fact>> {
    return this.post("/v1/search", { text });
  }

  /** Write earned memory. The server enforces the scope policy and the agent registry. */
  annotate(a: { subject: S; predicate: string; value: unknown; scope?: Scope; evidence?: string; by?: string }): Promise<Fact> {
    return this.post("/v1/annotate", a);
  }

  /** Neighbours by relation type, now or at `asOf`. */
  related(subject: S, relation?: string, asOf?: Date | string): Promise<Related[]> {
    return this.post("/v1/related", { subject, relation, as_of: asOf ? iso(asOf) : undefined });
  }

  /** Ingest one envelope or a batch. At-least-once is yours: retry on network errors, the server dedupes nothing. */
  events(batch: Envelope | Envelope[]): Promise<IngestReport> {
    return this.post("/v1/events", batch);
  }
}
