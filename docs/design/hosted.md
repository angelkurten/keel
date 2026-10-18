# Design note: hosted Keel

Status: proposed on 2026-10-18. Decided the same day: accounts, API keys and metering go into the core now, because they also serve anyone self-hosting Keel for several customers. Retention is designed as a dated `forget`. A shared database is the default, with a database per account as the enterprise plan. Keys live in a table in the store. Account names are short slugs; only a key marked global writes an account's global memory; tool calls are counted, not billed. The hosted launch itself waits for the gate below. Accounts and API keys are implemented as proposed (`keel/keys.py`, `keel keys`), and metering with one change (counters, not `ingested.recorded_at`, below) (`keel/usage.py`, `keel usage`), on master and not yet released; the rest is not implemented yet.

## Problem

Today one `keel serve` belongs to one SaaS. Every request carries the same shared secret (`_Bearer` in `keel/http.py`). The tenant arrives in `X-Keel-Tenant` and is never checked against the caller: whoever holds the secret reads every tenant. That is right when the SaaS owns the box. It is wrong once Kubbo runs one deployment for several SaaS products, which is what the 2027 roadmap promises: "Kubbo operates graph, bus and jobs; billed by events ingested per month".

Two more gaps block billing:

- **Usage cannot be counted.** `ingested` holds `(tenant, id)` with no time, so "events per month" cannot be read from the store. The event log lives at the source, not in Keel.
- **Nothing limits one customer.** One noisy SaaS slows down every other one.

## Goals

1. **Accounts.** An account is the SaaS that pays. Its tenants are its end-customers, exactly as today. A credential never reaches another account's data, whatever the request says.
2. **Keys.** API keys belong to an account and can be narrowed to one tenant or one agent. They can be created, listed and revoked without a restart.
3. **Metering.** Events applied per account, tenant and month, read from the store.
4. **Quotas.** A rate limit and a monthly event limit per account, answered with 429 and `Retry-After`.
5. **Retention.** An account can drop domain history older than a date, deterministically and with an audit trail.
6. **The core stays unaware of accounts.** `Tools`, handlers and stores keep seeing one tenant string. Rules 6, 7 and 8 hold unchanged.

## Non-goals

- **SSO for the API.** Agents authenticate with keys. SSO concerns the people who open `/ui`, and a proxy in front of it covers that (oauth2-proxy, Cloudflare Access) without code in Keel.
- **Billing.** Keel exposes usage; invoices, plans and payment live outside it.
- **Cross-account reads,** including a shared global partition across accounts.
- **Per-user permissions.** The unit is still the agent (see the [agent registry](agent-registry.md)).

## Proposal

### Accounts are a tenant prefix

An account is named by a slug chosen at creation and never renamed: lowercase letters, digits and hyphens, at most 32 characters (`acme`). It is short because it appears in every tenant key, in logs and in `keel usage`.

The HTTP layer resolves the key to an account and prefixes the tenant: `X-Keel-Tenant: cliente-42` under account `acme` becomes the tenant `acme/cliente-42`. The prefix is added by the server and cannot be chosen by the client: a tenant header that already contains `/` is refused when keys are on. Everything below the HTTP layer sees an ordinary tenant, so stores, conformance tests and replay are untouched.

The global partition follows the account: `annotate(scope="global")` writes to `acme/__global__`, and reads merge `acme/__global__`, never another account's. This is the one change inside `Tools`: the global partition's name is derived from the tenant's prefix instead of being the constant `__global__`. Without a prefix it stays `__global__`, so self-hosted deployments see no difference.

Envelopes posted to `/v1/ingest` go through the same rule: their `tenant` is prefixed, and one naming another account is refused.

### Keys

A new table, `api_keys` (`KEYS` is reserved in MySQL), sits in the same store, so there is one backup and rotation needs no restart:

| column | meaning |
|---|---|
| `id` | public prefix shown in listings (`kk_7f3a…`) |
| `hash` | SHA-256 of the full key; the key itself is shown once, at creation |
| `account` | the account it belongs to |
| `tenant` | optional: only this tenant |
| `agent` | optional: binds calls to this agent id, as `--agent` does |
| `global` | true for the account's privileged key: the only one that may `annotate(scope="global")`, and only into `account/__global__` |
| `created_at`, `revoked_at` | when it was issued and when it stopped working |

`keel keys create <account> [--tenant] [--agent] [--global]`, `keel keys list <account>` and `keel keys revoke <id>` manage the table. The middleware looks a key up by hash, refuses revoked ones, and caches lookups for a few seconds. When the table is empty, the single secret of today keeps working, so nothing changes for anyone who never creates a key. Once keys exist, the secret stays the operator's credential: it reaches every tenant by its full name.

### Metering

Counters per tenant, day and item in a `metering` table: `(tenant, day, item, n)`, where item is `events` or a tool's name. `keel usage [--month 2027-01] [--account acme]` groups them by account (the tenant prefix), tenant and month.

- **Events** are counted by the Ingestor after each commit: events applied for the first time, from any path. Duplicates and events for a forgotten tenant are not counted, and neither is `replay`. The first proposal, a `recorded_at` on `ingested`, would have billed a replay twice: `reset` deletes a tenant's applied ids so the log can be applied again. Counting after the commit means a crash in between undercounts, never bills twice.
- **Tool calls** are counted but not billed: `Tools(meter=)` calls the meter once per tool call, and the HTTP transport passes one that keeps counts in memory and writes them every ten seconds and at shutdown (`/v1` and `/mcp`). It feeds quotas, abuse detection and the plain question of whether agents use Keel at all.

### Quotas

They go in the same middleware as the keys: a token bucket per account for requests per second, and a check of the month's count against the account's event limit before `/v1/ingest` applies a batch. Both answer 429 with `Retry-After`, the way an exhausted embeddings provider already answers 503. Limits are columns on an `accounts` table that holds only the account id and its limits.

### Retention as a dated forget

Rule 1 says the graph must be rebuildable from events. Physical deletion of history is compatible with it if the deletion is itself recorded, applied deterministically, and replays to the same result. That is what `forgets: tenant` already does for a whole tenant (0.24.0). Retention extends it:

- An event declared `forgets: before` in the catalog makes the Ingestor call `store.forget(tenant, before=event.occurred_at)` in its transaction. It deletes facts and relations whose `valid_to` is earlier than the cutoff, along with the applied ids of events that occurred before it. Open facts and entities with a current fact stay. The report says what was deleted, and a tombstone records the cutoff.
- `keel forget <tenant> --before 2024-01-01 --db --yes` does the same for operators. An account-wide retention policy is a job that runs it per tenant.
- Replay from a log trimmed at the same cutoff rebuilds the same graph. Replay from an untrimmed log re-applies the `forgets: before` event and reaches it too.
- Annotations follow their own TTL (`MemoryPolicy`). A retention cutoff also removes closed annotations older than the cutoff, because a contract that asks for deletion means all of it.

### Storage per account

By default every account shares one MySQL database, isolated by the tenant prefix. An enterprise account can get its own database: the server maps the account to a store URL (`accounts.store`), opened lazily and kept in a small pool. The store is pluggable already, so this is configuration in the HTTP layer, not a change to any store.

## Gate before launching hosted

The goal of 0.1 is "used by two SaaS products that are not ours". Building the hosted shell before the demand is the main risk. Hosted launches once all of these hold:

- [x] A design customer with evaluations in Keel's favour (kagent: +0.35 on the dev assistant, +0.80 on technical instructions).
- [x] Production wired at that customer (22,285 entities, 79,220 facts, 36,796 events).
- [ ] Real usage measured in production (today, a weekend of traffic).
- [ ] A second SaaS pilot with real events.
- [ ] A customer who asks to pay so as not to run it.

Accounts, keys, metering and dated forget do not wait for the gate. They are small, and they are useful to self-hosted deployments too.

## Phases

| Phase | Items | Where |
|---|---|---|
| Now | accounts as tenant prefix, `api_keys` table and `keel keys`, `metering` counters and `keel usage` | core |
| Now | `forgets: before` and `keel forget --before` | core |
| With the second hosted customer | quotas, a database per account, operations (backups, per-account jobs, usage panel), Postgres CDC | core + infra |
| By contract | SSO in front of `/ui`, a retention policy per account | infra |

## Decided on 2026-10-18

- **Account names** are short slugs chosen at creation and never renamed, because they appear in every tenant key.
- **The global memory of an account** is written only by a key created with `--global`, and only into `account/__global__`. The SaaS manages its own semantics; no other key of the account can touch them.
- **Tool calls** are counted per account, tenant, day and tool, for quotas, abuse and real usage. Billing stays on events ingested.
