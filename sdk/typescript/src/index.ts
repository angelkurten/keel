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
  /** why it is a result: the attribute or predicate whose value matched, or "key" */
  matched?: string;
  /** the part of that value that matched (of a list, the element) */
  snippet?: string;
  /** search with detail "concise" or "brief" ({predicate: value}) or "full" (facts) */
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
  /** Annotations: the cartridge they were made under, `name@version#rules` (the hash changes when a rule does). */
  cartridge?: string | null;
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
/** What `context` cut: per (relation, direction), how many are shown of how many. */
export type CapNote = { relation: string; direction: "in" | "out" | "both"; shown: number; total: number };
export type Context = {
  subject: string;
  /** false when the subject is well formed but nothing is known about it: then only subject, as_of and watermark come back */
  found?: false;
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
  /** Relations left out of `relations` (the most recent are kept); the rest is one `related` away. */
  relations_more?: CapNote[];
  /** Present with `depth: 1`: each neighbour's entity, open facts and evaluation (capped; see neighbors_truncated). */
  neighbors?: Neighbor[];
  neighbors_truncated?: boolean;
  /** With `depth: 1`: neighbours left out per relation (they are still in `relations`). */
  neighbors_more?: CapNote[];
};
/** `contextConcise`: the same answer in about a third of the tokens, for a prompt. */
export type ConciseContext = {
  subject: string;
  attrs: Record<string, unknown>;
  facts: Record<string, unknown>;
  /** when each value became true: {predicate: valid_from} */
  since: Record<string, string>;
  relations: Array<{ relation: string; direction: "in" | "out"; subject: string }>;
  evaluation: Record<string, boolean | null>;
  as_of: string;
  watermark: string | null;
  as_known_at?: string;
  proposals?: Array<{ predicate: string; value: unknown; by: string | null; evidence: string | null }>;
  conflicts?: Context["conflicts"];
  neighbors?: Array<{ relation: string; direction: string; subject: string; facts: Record<string, unknown>; evaluation: Record<string, boolean | null> }>;
  neighbors_truncated?: boolean;
  neighbors_more?: CapNote[];
  relations_more?: CapNote[];
  /** false when nothing is known about the subject (then only subject, as_of and watermark come back) */
  found?: false;
};
/** `contextBrief`: `ConciseContext` with relations as {relation: [subjects]}, an incoming one written `<-relation`. */
export type BriefContext = Omit<ConciseContext, "relations"> & { relations: Record<string, string[]> };
export type Related = {
  relation: string;
  direction: "in" | "out" | "both";
  subject: string;
  since: string;
  /** related with depth 2 or 3: how far, and the subject it was reached from (direction is relative to it) */
  hops?: number;
  via?: string;
};
export type ContextOptions = { asKnownAt?: Date | string; task?: string; /** predicates to leave out of what is returned */ exclude?: string[]; /** just these predicates */ only?: string[] };
export type SearchOptions<S extends string = string> = {
  /** one kind, or several: the listing covers them all, a semantic ranking only them, perKind the best of each */
  kind?: string | string[];
  rule?: string;
  /** omitted: the whole listing (sent as 0, no cap); a semantic ranking still returns its first 10 */
  limit?: number;
  after?: string;
  mode?: "text" | "semantic";
  minScore?: number;
  relatedTo?: S;
  via?: string;
  /** "concise": each result carries its current facts as {predicate: value}; "brief": the same, leaner; "full": as facts,
   * and fact hits as whole facts (lean otherwise). searchKeys for keys only. */
  detail?: "entity" | "concise" | "brief" | "full";
  /** 1: each result carries its neighbours */
  depth?: 0 | 1;
  /** the best n of every kind instead of one ranking: with relatedTo and mode "semantic", the context for a task */
  perKind?: number;
  /** predicates to leave out (and fact hits on them dropped) */
  exclude?: string[];
  /** evaluate rules, neighbours and facts at this instant */
  asOf?: Date | string;
  /** only entities whose attribute or current fact has this value (or a list holding it; text without case) */
  where?: Record<string, unknown>;
};
export type Neighbor = {
  relation: string;
  direction: "in" | "out" | "both";
  subject: string;
  entity: Entity | null;
  facts: Fact[];
  evaluation: Record<string, boolean | null>;
};
export type IngestReport = {
  processed: number; duplicates: number; unhandled: string[]; errors: Array<[string, string]>;
  forgotten: Array<{ tenant: string } & Record<string, number | string>>; // tenants erased by a `forgets: tenant` event, with what was deleted
  skipped: number; // events for a forgotten tenant, never applied
  retained?: Array<{ tenant: string; before: string } & Record<string, number | string>>; // `forgets: before` events: the cutoff and what was deleted
};

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

/** A list as its type declares it: without the `{more}` (what a cap cut) or `{note}` entries the server may put first. */
const items = <T>(list: T[]): T[] => {
  const marker = (x: unknown) => typeof x === "object" && x !== null && !Array.isArray(x) && Object.keys(x).length === 1 && ("more" in x || "note" in x);
  let i = 0;
  while (i < list.length && marker(list[i])) i++;
  return i ? list.slice(i) : list;
};

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

  /** `contextConcise` with relations as {relation: [subjects]}: the cheapest answer, what the HTTP API gives an agent by default. */
  contextBrief(subject: S, asOf?: Date | string, depth = 0, opts: ContextOptions = {}): Promise<BriefContext> {
    return this.post("/v1/context", this.contextBody(subject, asOf, depth, opts, "brief"));
  }

  private contextBody(subject: S | S[], asOf: Date | string | undefined, depth: number, opts: ContextOptions, detail: "full" | "concise" | "brief") {
    return {
      subject,
      as_of: asOf ? iso(asOf) : undefined,
      depth,
      as_known_at: opts.asKnownAt ? iso(opts.asKnownAt) : undefined,
      task: opts.task,
      detail,
      exclude: opts.exclude,
      only: opts.only,
    };
  }

  /** Facts over time, oldest first, closed ones included. `subject: null` with `changedSince` lists the tenant's changes.
   * Every fact unless `limit` (then the most recent n); `detail: "concise"` returns lean facts instead of `Fact`s. */
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
      detail?: "full" | "concise";
    } = {},
  ): Promise<Fact[]> {
    return this.post<Fact[]>("/v1/history", {
      subject,
      since: f.since ? iso(f.since) : undefined,
      until: f.until ? iso(f.until) : undefined,
      predicate: f.predicate,
      scope: f.scope,
      as_known_at: f.asKnownAt ? iso(f.asKnownAt) : undefined,
      task: f.task,
      changed_since: f.changedSince ? iso(f.changedSince) : undefined,
      limit: f.limit ?? 0,
      detail: f.detail ?? "full",
    }).then(items);
  }

  /** Text search; with `kind` or `rule`, the entities of a kind or where a rule fires now, paged by `limit`/`after`.
   * `detail: "concise"` puts each result's facts in it, `depth: 1` its neighbours, `perKind` the best of every kind. */
  search(text = "", opts: SearchOptions<S> = {}): Promise<Array<Entity | Fact>> {
    return this.post<Array<Entity | Fact>>("/v1/search", this.searchBody(text, opts)).then(items);
  }

  /** `search` that returns only the keys (`detail: "key"`), each once: the cheapest listing, e.g. every table of a model. */
  searchKeys(text = "", opts: Omit<SearchOptions<S>, "detail" | "depth"> = {}): Promise<S[]> {
    return this.post<S[]>("/v1/search", { ...this.searchBody(text, opts), detail: "key" }).then(items);
  }

  /** `search` for many texts in one call: one result list per text, in order. */
  searches(texts: string[], opts: SearchOptions<S> = {}): Promise<Array<Array<Entity | Fact>>> {
    return this.post<Array<Array<Entity | Fact>>>("/v1/search", this.searchBody(texts, opts)).then((lists) => lists.map(items));
  }

  private searchBody(text: string | string[], opts: SearchOptions<S>) {
    const { minScore, relatedTo, perKind, asOf, ...rest } = opts;
    return { text, ...rest, limit: rest.limit ?? 0, min_score: minScore, related_to: relatedTo, per_kind: perKind, as_of: asOf ? iso(asOf) : undefined };
  }

  /** Write earned memory. The server enforces the scope policy and the agent registry. `task` ties it to a job;
   * `key` makes a retried write return the first one. Returns the whole fact; `detail: "ack"` only a short receipt. */
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
    detail?: "full" | "ack";
  }): Promise<Fact> {
    return this.post("/v1/annotate", { ...a, detail: a.detail ?? "full" });
  }

  /** Neighbours by relation type (one or a list), now or at `asOf`. `depth` 2 or 3 walks further through those
   * relations; each entry then carries `hops` and `via`. Every neighbour unless `limit` (then the nearest, most recent n). */
  related(subject: S, relation?: string | string[], asOf?: Date | string, depth = 1, limit?: number): Promise<Related[]> {
    return this.post<Related[]>("/v1/related", { subject, relation, as_of: asOf ? iso(asOf) : undefined, depth, limit: limit ?? 0 }).then(items);
  }

  /** Ingest one envelope or a batch. Retrying on a network error is safe: the server skips ids it already applied, and
   * derives the id from the content when an envelope has none. */
  events(batch: Envelope | Envelope[]): Promise<IngestReport> {
    return this.post("/v1/events", batch);
  }
}
