# Design note: hosted Keel

Status: proposed on 2026-10-18. Decided the same day: accounts, API keys and metering go into the core now, because they also serve anyone self-hosting Keel for several customers. Retention is designed as a dated `forget`. A shared database is the default, with a database per account as the enterprise plan. Keys live in a table in the store. Account names are short slugs; only a key marked global writes an account's global memory; tool calls are counted, not billed. The hosted launch itself waits for the gate below. Accounts and API keys are implemented as proposed (`keel/keys.py`, `keel keys`), metering with one change (counters, not `ingested.recorded_at`, below) (`keel/usage.py`, `keel usage`), and retention with two simplifications (below) (`forgets: before`, `keel forget --before`), shipped in 0.27.0. Quotas, a database per account and a retention policy per account (`keel/accounts.py`, `keel accounts`, `keel forget --policy`) shipped in 0.28.0, and so did Postgres CDC (`keel cdc`), SSO in front of `/ui` (`deploy/sso`), `keel serve --account` and the control plane (`hosted/`: invite-only sign-up, a database and a Coolify container per account, the usage panel). Running on Coolify since 2026-10-18: the control plane at https://keel.angelkurten.com (sign-up by invitation), a shared MySQL with daily backups, and one container per account under `*.keel.angelkurten.com`; a `demo` account was signed up, ingested the fleet events and answered `context` end to end.

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

Rule 1 says the graph must be rebuildable from events. Physical deletion of history is compatible with it if the deletion is itself recorded and applied again on every rebuild. That is what `forgets: tenant` already does for a whole tenant (0.24.0). Retention extends it:

- An event declared `forgets: before` in the catalog makes the Ingestor call `store.forget_before(tenant, event.occurred_at)` in its transaction. It deletes facts (annotations included) and relations whose `valid_to` is at or before the cutoff. Open facts, current relations and entities stay. The report says what was deleted (`retained`), and the store keeps the cutoff per tenant, the latest one winning.
- `keel forget <tenant> --before 2024-01-01 --db --yes` does the same for operators, and `--all-tenants` makes it a retention policy to schedule.
- `replay` rebuilds from the log and then applies the kept cutoff again, so what the log brought back goes again.
- Annotations follow their own TTL (`MemoryPolicy`). A retention cutoff also removes closed annotations older than it, because a contract that asks for deletion means all of it.

Two simplifications against the first proposal. Applied event ids are kept, because they are what stops an old redelivery from reviving deleted history, and they are hashes, not content. Events are not skipped by the cutoff: an event that occurred before it can still open a fact that is current today, and skipping it would lose that fact. The price is that an event arriving later with a valid time before the cutoff stays until the next run, so the policy is scheduled, not run once.

### Isolation: a container per account (decided 2026-10-18)

A cartridge carries Python (`handlers.py`). A shared server that loaded every account's cartridge would run one customer's code in a process that holds every other customer's data. So hosted runs **one Keel container per account**, with its cartridge and its own database, and a shared **control plane** (sign-up, keys, accounts, usage, a panel) on a shared database. `keel serve --account acme --db <shared>` makes a server accept only that account's keys and keep its graph in the account's database (`accounts.store`), while keys, limits and usage come from the shared one. The control plane creates each account's container on Coolify through its API. Declarative handlers, which would let one server load many cartridges safely, stay a later option.

### Control plane (`hosted/`)

Sign-up creates the account in the control database, a MySQL database and user for it, its first key (the hash written to both databases) and its container on Coolify. The account's container gets only its own database's credentials, so the key and limits it enforces are copied into that database, and the panel reads usage back from it. A customer's handlers can therefore change their own counters; acceptable for invited pilots, to be closed by metering at the edge before billing strangers.

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

**Revised on 2026-10-18 (later the same day):** the whole hosted product is built before looking for the second pilot, on the reasoning that a pilot says yes more easily to a hosted Keel it can sign up for than to a deployment it has to run. The gate still decides when hosted is announced and billed; it no longer decides when it is built. Operations run on Coolify.

## Phases

| Phase | Items | Where |
|---|---|---|
| Now | accounts as tenant prefix, `api_keys` table and `keel keys`, `metering` counters and `keel usage` | core |
| Now | `forgets: before` and `keel forget --before` | core |
| Next (revised 2026-10-18) | quotas, a database per account, a retention policy per account, Postgres CDC | core |
| Next (revised 2026-10-18) | operations on Coolify (backups, per-account jobs), self-serve sign-up, a usage panel, SSO in front of `/ui` through a proxy | infra + product |

## Decided on 2026-10-18

- **Account names** are short slugs chosen at creation and never renamed, because they appear in every tenant key.
- **The global memory of an account** is written only by a key created with `--global`, and only into `account/__global__`. The SaaS manages its own semantics; no other key of the account can touch them.
- **Tool calls** are counted per account, tenant, day and tool, for quotas, abuse and real usage. Billing stays on events ingested.


## Security review (2026-10-22)

An adversarial review (`tests/test_security_hosted.py`) closed these: an invitation opens one account (`hosted_invites`, the code its primary key, so two racing sign-ups cannot share it); failed logins and failed invitations are throttled per email and address (10 in 15 minutes, then 429), and an unknown email costs the same scrypt as a known one; a session ends when the account's password changes (`python -m hosted password`), since it keeps a fingerprint of the password it was opened with; a state-changing request whose `Origin` is not the console's own is refused, because customer containers on sibling subdomains are same-site for `SameSite=Lax`; slugs are DNS labels and infrastructure names (`www`, `admin`, `api`, `mcp`, ...) are reserved; `/api/cartridge/check` needs a session and a size limit and reports a malformed section instead of failing; the SDK proxy accepts only the four languages; a provisioning error hides the database URL and secret it may echo. Accepted: per-process throttling (one control plane), counters checked before they are written (a burst can pass a daily cap by a little), and `/api/slug` telling which slugs are taken.
