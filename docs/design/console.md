# Design note: the Keel console

Status: proposed and built on 2026-10-18, shipped in 0.29.0. Replaces the hosted sign-up form and usage page (`hosted/app.py`) and the read-only browser at `/ui` (`keel/ui.html`) with one console. Mockup: https://claude.ai/artifact/KbwgvjBcMGJxiWMAcNyVqF.

## Problem

The premise: someone who wants to try Keel does as little as possible before it works. Today:

- **Sign-up asks for five things** before anything happens: account, email, invitation code, a cartridge choice and two files to upload.
- **The key is shown once** with no way back: no login, no rotation, and `/panel` asks for the key every time.
- **Nothing says when the Keel is up.** "It takes a minute or two", then silence.
- **The graph starts empty.** Seeing anything means posting events by hand.
- **The MCP snippet does not work.** `/mcp` is served only when the server starts with a tenant, and an account's container starts without one, so the configuration handed out at sign-up gets nothing.
- **Two disconnected UIs.** The usage page and `/ui` do not know about each other, and `/ui` asks for the secret and the tenant by hand.

## The flow

1. **Invitation link.** Each pilot gets `keel.angelkurten.com/?invite=<code>`. The code travels in the URL; no field for it.
2. **Email, an account already filled in, a password.** The account slug is proposed from the email's domain (`angel@acme.com` → `acme`, a suffix on collision) and editable on the same screen, because it is permanent. The password is stored as scrypt. Sign-up opens a session at once (a random token, its hash in the control database, an `HttpOnly; Secure; SameSite=Lax` cookie); later visits sign in with email and password.
3. **The account is created on sign-up**, born with the fleet example and its events ingested into a tenant `demo`. A waiting screen shows the steps (database, container, example data, first key) by polling the control plane, which polls the container's `/health`.
4. **The console** opens on the graph of the example, with a checklist: Keel running, example data, connect an agent, send your own events, your cartridge.

What the user typed: an email and a password. No mailbox round trip: the invitation link already vouches for them.

## The console

One SPA (React + Vite), served by the control plane in hosted and by `keel serve --ui` self-hosted, where the account tabs are hidden.

| Tab | What it does |
|---|---|
| Graph | Today's `/ui`: search, subject, related graph, facts, history; the tenant picked in the sidebar. A banner while it shows example data. |
| Playground | Two modes. **Tools:** the five tools as forms against the selected tenant, the JSON result, and the same call as curl and Python. **Agent:** a name, a model, the cartridge's agent identity, a system prompt started from the cartridge and the five tools; chat with it against the tenant, every tool call shown; then export it as code. |
| Connect | One tab per client (Claude Code, Cursor, VS Code, Claude Desktop, LangGraph, curl) with the endpoint, a key narrowed to the tenant and the tenant already in the snippet. It waits for the first tool call and says when it arrives. |
| SDK | Pick TypeScript, Python, Go or PHP: the command that installs the runtime, the typed client generated from the deployed cartridge to download, and an example with the cartridge's own kinds and events. Says when the cartridge changed and the download is stale. |
| Events | Webhook (endpoint, envelope, snippets, a live log of recent events with their errors); upload an `events.jsonl` (checked against the catalog, then ingested in batches); Postgres CDC (a URL and tables). |
| Keys | List, create (shown once), revoke. Scope: account, tenant, agent, global. |
| Usage | Events against the monthly limit, requests per second, tool calls per tenant and tool. |
| Cartridge | The current one; replace it by uploading files, by picking an example, or by generating it from a few real events. Replacing it forgets `demo`. |

Look: a sober developer tool (Linear, Vercel, Supabase), dark first, monospace for keys, endpoints, JSON and subject ids.

## How it works

- **The console never holds a user's key.** It calls the control plane, which proxies `/v1/*` to the account's container with that container's `KEEL_SECRET` (generated at sign-up, already in its environment), prefixing the tenant with the account. The secret is kept in the control database; it is a credential of a container we run, not of the customer.
- **MCP for many tenants in one server.** Connect needs `/mcp` per tenant in an account's container. The server builds the MCP `Tools` per request from a key narrowed to a tenant (and agent), instead of once at start-up from `--tenant`. A core change in `keel/http.py` and `keel/mcp_server.py`; `--tenant` keeps working as today.
- **Example data.** After the container answers `/health`, the control plane posts `examples/fleet/events.jsonl` under tenant `demo`.
- **Live event log.** The container keeps the last 200 ingest results per tenant in memory (applied, duplicate, refused with the reason) behind `GET /v1/events/recent`. Not a sixth tool: an operator endpoint like `/v1/events`.
- **Upload.** `Cartridge.check_event` per line before anything is posted; the report lists what would be refused.
- **CDC in hosted.** The control plane keeps the Postgres URL and tables; the provisioner adds `keel cdc` as a second process in the account's container.
- **Cartridge generator.** The user pastes two or three real events; the control plane asks a model through OfoxAI (the gateway already used) for `cartridge.yaml` and `handlers.py`, checks the YAML with `check_cartridge`, shows both for review, and deploys them to the account's own container on request. Handlers run only there, as uploaded ones do. A limit of generations per account and day. An LLM here is fine: it writes the cartridge once, it is never in the ingest path (rule 10).
- **The agent in the playground** is tried in the console and exported, never hosted: Keel stays out of the agent framework business. The control plane runs the loop: the model through OfoxAI (an allowance of messages per account and day) or the user's own Anthropic or OpenAI key, the five tools as function definitions, each call sent through the proxy. The starter prompt is a template filled from the cartridge (kinds, relations, rules, "call `context` before answering"); no LLM writes it. Agent configurations are kept per account in the control database. Export writes one runnable file per framework, connected to the account's `/mcp` with the key from an environment variable: Claude Agent SDK, LangGraph, OpenAI Agents SDK, Google ADK.
- **SDKs.** `load_cartridge` imports `handlers.py`, so the control plane never generates them itself: the account's container, which already runs that code, serves `GET /v1/sdk/<language>` (the operator secret only) with `keel sdk`'s output, and the console compares its hash with the last download to say it is stale.
- **Replacing the cartridge** updates the container's files and redeploys it; the control plane waits for `/health` to report the new cartridge's `revision` (a hash of its two files), puts the previous cartridge back if the new one does not start, and forgets tenant `demo` when the example goes. Problems are found without importing `handlers.py`: its `@registry.on` types are read with `ast`.
- **Self-hosted.** The SPA's build ships inside the wheel (`keel/console/`, about 200 KB) and replaces `keel/ui.html`. Its calls carry the secret the operator types once, kept in the browser's session.

## Phases

All four shipped together in 0.29.0.

| Phase | Items |
|---|---|
| 1. Entry | invitation link, email and password, proposed slug, account born with the example, waiting screen, SPA shell with Graph, Playground (tools and agent, with export) and Connect, the proxy, MCP for many tenants |
| 2. Data | Events (webhook and live log, upload), Keys, Usage, SDK |
| 3. Own cartridge | replace by upload or example, the generator, CDC in hosted |
| 4. Self-hosted | the SPA in the wheel, `keel/ui.html` removed |

## Decided on 2026-10-18

- One console for hosted and `/ui`, built as a SPA (React + Vite).
- Sign-in by email and password (first proposed as a magic link; changed the same day). No reset by email yet: the operator sets a password with `python -m hosted password <account>`.
- Sign-up asks for no cartridge: the account starts with the fleet example and its events.
- The invitation travels in a link.
- Connect offers both a playground and per-client snippets.
- Own data through webhook, file upload, Postgres CDC and a generated cartridge.
- The generator uses OfoxAI.
- The playground also configures an agent (prompt, model, the five tools), tried in the console and exported to Claude Agent SDK, LangGraph, OpenAI Agents SDK or Google ADK; never hosted.
- The playground's model runs on OfoxAI with a daily allowance per account, or on the user's own key.
- SDKs are a console tab: install command, download of the generated client, a typed example, a stale warning.
- Dark, sober developer-tool look.

## Open

- **Teams.** One email per account for now; inviting teammates comes when a pilot asks.
- **Password reset by email.** Needs a mail provider (Resend was the candidate); until then the operator resets.
