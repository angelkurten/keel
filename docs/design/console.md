# Design note: the Keel console

Status: proposed on 2026-10-18. Replaces the hosted sign-up form and usage page (`hosted/app.py`) and the read-only browser at `/ui` (`keel/ui.html`) with one console. Mockup: https://claude.ai/artifact/KbwgvjBcMGJxiWMAcNyVqF.

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
2. **Email, and an account already filled in.** The account slug is proposed from the email's domain (`angel@acme.com` → `acme`, a suffix on collision) and editable on the same screen, because it is permanent.
3. **Magic link** through Resend: a single-use token, valid 15 minutes. Clicking it opens a session (a random token, its hash in the control database, an `HttpOnly; Secure; SameSite=Lax` cookie).
4. **The account is created on that first click**, born with the fleet example and its events ingested into a tenant `demo`. A waiting screen shows the steps (database, container, example data, first key) by polling the control plane, which polls the container's `/health`.
5. **The console** opens on the graph of the example, with a checklist: Keel running, example data, connect an agent, send your own events, your cartridge.

What the user typed: an email. What they clicked: a link in their inbox.

## The console

One SPA (React + Vite), served by the control plane in hosted and by `keel serve --ui` self-hosted, where the account tabs are hidden.

| Tab | What it does |
|---|---|
| Graph | Today's `/ui`: search, subject, related graph, facts, history; the tenant picked in the sidebar. A banner while it shows example data. |
| Playground | The five tools as forms against the selected tenant, the JSON result, and the same call as curl and Python. |
| Connect | One tab per client (Claude Code, Cursor, VS Code, Claude Desktop, LangGraph, curl) with the endpoint, a key narrowed to the tenant and the tenant already in the snippet. It waits for the first tool call and says when it arrives. |
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
- **Replacing the cartridge** redeploys the container with the new files and runs `keel forget demo`.
- **Self-hosted.** The SPA's build ships inside the wheel (`keel/console/`, about 200 KB) and replaces `keel/ui.html`. Its calls carry the secret the operator types once, kept in the browser's session.

## Phases

| Phase | Items |
|---|---|
| 1. Entry | invitation link, magic link (Resend), proposed slug, account born with the example, waiting screen, SPA shell with Graph, Playground and Connect, the proxy, MCP for many tenants |
| 2. Data | Events (webhook and live log, upload), Keys, Usage |
| 3. Own cartridge | replace by upload or example, the generator, CDC in hosted |
| 4. Self-hosted | the SPA in the wheel, `keel/ui.html` removed |

## Decided on 2026-10-18

- One console for hosted and `/ui`, built as a SPA (React + Vite).
- Sign-in by magic link, sent with Resend from angelkurten.com.
- Sign-up asks for no cartridge: the account starts with the fleet example and its events.
- The invitation travels in a link.
- Connect offers both a playground and per-client snippets.
- Own data through webhook, file upload, Postgres CDC and a generated cartridge.
- The generator uses OfoxAI.
- Dark, sober developer-tool look.

## Open

- **Teams.** One email per account for now; inviting teammates comes when a pilot asks.
- **Resend DNS.** Verifying angelkurten.com takes SPF and DKIM records in Cloudflare.
