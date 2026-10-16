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

export type Entity = {
  tenant: string;
  kind: string;
  id: string;
  key: string;
  ref: string | null;
  attrs: Record<string, unknown>;
  /** with mode: "semantic" */
  score?: number;
  similarity?: number | null;
  match?: "key";
  /** semantic, with the cartridge's semantic.split: the predicate whose own document ranked this result */
  matched?: string;
  /** search with detail "concise" ({predicate: value}) or "full" (facts) */
  facts?: Record<string, unknown> | Fact[];
  /** search with depth: 1 */
  neighbors?: SearchNeighbor[];
  neighbors_truncated?: boolean;
};
/** A neighbour of a search result (depth: 1); facts when detail asks for them. */
export type SearchNeighbor = { relation: string; direction: "in" | "out" | "both"; subject: string; facts?: Record<string, unknown> | Fact[] };
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
  task?: string | null;
  idempotency_key?: string | null;
  /** When Keel learned the fact closed (transaction time). */
  closed_at?: string | null;
  /** null = accepted; "proposed" waits for a decision; "rejected" records one. */
  status?: "proposed" | "rejected" | null;
  /** The caller's traceparent / tool call id at write time. */
  trace?: string | null;
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
  /** Present when asked with `asKnownAt`. */
  as_known_at?: string;
  /** The tenant's latest transaction time: pass it to `history(null, { changedSince })` next run. */
  watermark?: string | null;
  /** Pending proposals on this subject. */
  proposals?: Fact[];
  /** Predicates with more than one open value (multi-valued predicates written by several authors). */
  conflicts?: Record<string, Array<{ value: unknown; source: string | null; recorded_at: string | null }>>;
  /** Present with `depth: 1`: each neighbour's entity, open facts and evaluation (capped; see neighbors_truncated). */
  neighbors?: Neighbor[];
  neighbors_truncated?: boolean;
};
/** `contextConcise`: the same answer in about a third of the tokens, for a prompt. */
export type ConciseContext = {
  subject: string;
  attrs: Record<string, unknown>;
  facts: Record<string, unknown>;
  relations: Array<{ relation: string; direction: "in" | "out"; subject: string }>;
  evaluation: Record<string, boolean | null>;
  as_of: string;
  watermark: string | null;
  as_known_at?: string;
  proposals?: Array<{ predicate: string; value: unknown; by: string | null; evidence: string | null }>;
  conflicts?: Context["conflicts"];
  neighbors?: Array<{ relation: string; direction: string; subject: string; facts: Record<string, unknown>; evaluation: Record<string, boolean | null> }>;
  neighbors_truncated?: boolean;
};
export type Related = {
  relation: string;
  direction: "in" | "out" | "both";
  subject: string;
  since: string;
  /** related with depth 2 or 3: how far, and the subject it was reached from (direction is relative to it) */
  hops?: number;
  via?: string;
};
export type ContextOptions = { asKnownAt?: Date | string; task?: string; /** predicates to leave out of what is returned */ exclude?: string[] };
export type SearchOptions<S extends string = string> = {
  kind?: string;
  rule?: string;
  limit?: number;
  after?: string;
  mode?: "text" | "semantic";
  minScore?: number;
  relatedTo?: S;
  via?: string;
  /** "concise": each result carries its current facts as {predicate: value}; "full": as facts */
  detail?: "entity" | "concise" | "full";
  /** 1: each result carries its neighbours */
  depth?: 0 | 1;
  /** the best n of every kind instead of one ranking: with relatedTo and mode "semantic", the context for a task */
  perKind?: number;
  /** predicates to leave out (and fact hits on them dropped) */
  exclude?: string[];
  /** evaluate rules, neighbours and facts at this instant */
  asOf?: Date | string;
};
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
  /** W3C traceparent sent with every call; Keel stores it with every write. */
  traceparent?: string;
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
        ...(o.traceparent ? { traceparent: o.traceparent } : {}),
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

  /** What is true about a subject now, or at `asOf`: facts, relations, rules and their verdicts. `depth: 1` adds the neighbours;
   * `opts.asKnownAt` answers what Keel knew then; `opts.task` keeps one task's memory. */
  context(subject: S, asOf?: Date | string, depth = 0, opts: ContextOptions = {}): Promise<Context> {
    return this.post("/v1/context", this.contextBody(subject, asOf, depth, opts, "full"));
  }

  /** `context` in about a third of the tokens: facts as {predicate: value}, no rule definitions. For prompts. */
  contextConcise(subject: S, asOf?: Date | string, depth = 0, opts: ContextOptions = {}): Promise<ConciseContext> {
    return this.post("/v1/context", this.contextBody(subject, asOf, depth, opts, "concise"));
  }

  /** `context` for many subjects in one call, answers in order. */
  contexts(subjects: S[], asOf?: Date | string, depth = 0, opts: ContextOptions = {}): Promise<Context[]> {
    return this.post("/v1/context", this.contextBody(subjects, asOf, depth, opts, "full"));
  }

  /** `contextConcise` for many subjects in one call, answers in order. */
  contextsConcise(subjects: S[], asOf?: Date | string, depth = 0, opts: ContextOptions = {}): Promise<ConciseContext[]> {
    return this.post("/v1/context", this.contextBody(subjects, asOf, depth, opts, "concise"));
  }

  private contextBody(subject: S | S[], asOf: Date | string | undefined, depth: number, opts: ContextOptions, detail: string) {
    return {
      subject,
      as_of: asOf ? iso(asOf) : undefined,
      depth,
      as_known_at: opts.asKnownAt ? iso(opts.asKnownAt) : undefined,
      task: opts.task,
      detail,
      exclude: opts.exclude,
    };
  }

  /** Facts over time, oldest first, closed ones included. `subject: null` with `changedSince` lists the tenant's changes. */
  history(
    subject: S | null,
    f: {
      since?: Date | string;
      until?: Date | string;
      predicate?: string;
      scope?: Scope;
      asKnownAt?: Date | string;
      task?: string;
      changedSince?: Date | string;
      limit?: number;
    } = {},
  ): Promise<Fact[]> {
    return this.post("/v1/history", {
      subject,
      since: f.since ? iso(f.since) : undefined,
      until: f.until ? iso(f.until) : undefined,
      predicate: f.predicate,
      scope: f.scope,
      as_known_at: f.asKnownAt ? iso(f.asKnownAt) : undefined,
      task: f.task,
      changed_since: f.changedSince ? iso(f.changedSince) : undefined,
      limit: f.limit,
    });
  }

  /** Text search; with `kind` or `rule`, the entities of a kind or where a rule fires now, paged by `limit`/`after`.
   * `detail: "concise"` puts each result's facts in it, `depth: 1` its neighbours, `perKind` the best of every kind. */
  search(text = "", opts: SearchOptions<S> = {}): Promise<Array<Entity | Fact>> {
    return this.post("/v1/search", this.searchBody(text, opts));
  }

  /** `search` for many texts in one call: one result list per text, in order. */
  searches(texts: string[], opts: SearchOptions<S> = {}): Promise<Array<Array<Entity | Fact>>> {
    return this.post("/v1/search", this.searchBody(texts, opts));
  }

  private searchBody(text: string | string[], opts: SearchOptions<S>) {
    const { minScore, relatedTo, perKind, asOf, ...rest } = opts;
    return { text, ...rest, min_score: minScore, related_to: relatedTo, per_kind: perKind, as_of: asOf ? iso(asOf) : undefined };
  }

  /** Write earned memory. The server enforces the scope policy and the agent registry. `task` ties it to a job;
   * `key` makes a retried write return the first one. */
  annotate(a: {
    subject: S;
    predicate: string;
    value: unknown;
    scope?: Scope;
    evidence?: string;
    by?: string;
    task?: string;
    key?: string;
    /** "proposed" waits for a decision; "rejected" settles a proposal. */
    status?: "proposed" | "rejected";
  }): Promise<Fact> {
    return this.post("/v1/annotate", a);
  }

  /** Neighbours by relation type (one or a list), now or at `asOf`. `depth` 2 or 3 walks further through those
   * relations; each entry then carries `hops` and `via`. */
  related(subject: S, relation?: string | string[], asOf?: Date | string, depth = 1): Promise<Related[]> {
    return this.post("/v1/related", { subject, relation, as_of: asOf ? iso(asOf) : undefined, depth });
  }

  /** Ingest one envelope or a batch. At-least-once is yours: retry on network errors, the server dedupes nothing. */
  events(batch: Envelope | Envelope[]): Promise<IngestReport> {
    return this.post("/v1/events", batch);
  }
}
