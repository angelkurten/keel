# Design note: SDKs generated from the cartridge

Status: decided on 2026-10-14, in progress. The cartridge is the interface definition, the way a `.proto` is for gRPC; Keel generates the client code for the languages a product is written in.

## Decisions

1. **Both sides in the first version.** Producer (emit events with typed payloads) and consumer (the five tools, typed with the cartridge's kinds, relations, rules and agents).
2. **Payload notation: a subset of JSON Schema.** The short hints stay (`id: string`, `amount: number | null`) and grow enums, nested objects and arrays. Keel translates them to JSON Schema draft 2020-12. Every existing cartridge remains valid. See [SPEC §5.5](../SPEC.md#55-payload-notation).
3. **Our own generator, with templates.** `keel sdk <language> <cartridge> -o <dir>`. Templates are plain Python modules (f-strings), no new dependency; a template engine only if they get hairy.
4. **Languages: TypeScript, Python, Go and PHP.** TypeScript first, validated against Toche, then the other three from the same intermediate model.
5. **Consumer transport: Keel's own HTTP JSON API**, described by OpenAPI, next to MCP over HTTP on the same server. Same five operations, two transports; the names and semantics never change per transport (SPEC §7.6).
6. **Distribution: a published runtime per language, generated code vendored in the product.** The runtime holds the HTTP client and the envelope; the generated file holds the cartridge's types, constructors and constants, and lives in the product's repo like protoc output.
7. **Tenant and agent per request from the product, with a shared secret.** `Authorization: Bearer <secret>`, `X-Keel-Tenant`, `X-Keel-Agent`. The product authenticates its users; Keel takes the ids as given (same stance as the agent registry).

## Pipeline

```
cartridge.yaml  ->  keel schema  ->  intermediate model (JSON)  ->  template  ->  <lang> file
```

`keel schema <cartridge>` prints one JSON document: the envelope schema, every event with its subject kind, owner and payload schema, the entity kinds, relations, rules (with their `when`), agents and memory policy. It is the generator's only input and a public artifact: a third party can write a template for a fifth language without reading Keel's code.

## What a generated file contains

- A type per event payload and per entity's attributes.
- `emit.<event>(subject, payload, occurred_at)` constructors that return a valid envelope (id as content hash, when the product gives none).
- Subject helpers per kind: `subjects.tire("T-9001")`.
- Constants: rule names, agent ids, relation names, scopes.
- A `Client` bound to tenant and agent: `context`, `history`, `search`, `annotate`, `related`, plus `events(batch)` to ingest.

## HTTP API

`keel serve --transport http --db keel.db --secret-env KEEL_SECRET` serves one process for many tenants:

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /v1/events` | one envelope or a list | ingest report |
| `POST /v1/context` | `{subject, as_of?}` | the `context` object |
| `POST /v1/history` | `{subject, since?, until?, predicate?, scope?}` | facts |
| `POST /v1/search` | `{text}` | entities and facts |
| `POST /v1/annotate` | `{subject, predicate, value, scope?, evidence?, by?}` | the fact |
| `POST /v1/related` | `{subject, relation?}` | neighbours |
| `/mcp` | MCP streamable HTTP, when the server is started with `--tenant` | the same five tools |
| `GET /openapi.json` | | the contract |

Errors: 401 wrong secret, 403 a refusal (scope policy, agent registry), 400 bad input, 422 malformed envelope. Every request carries `X-Keel-Tenant`; `X-Keel-Agent` is required when the cartridge declares agents. A `Tools` instance is built per request; nothing is cached across tenants.

## Milestones

1. Payload notation and `keel schema`. Shipped.
2. HTTP transport: the API above, MCP over HTTP for one tenant, headers, OpenAPI. Shipped.
3. TypeScript template and `@keel-context/sdk` runtime. Shipped: generated for `fleet` and `finance`, exercised from Bun against `keel serve --transport http`; Toche's own cartridge is the next real test.
4. Python, Go and PHP templates and runtimes from the same model. Shipped: `keel.client` (stdlib), `sdk/go` (module `github.com/angelkurten/keel/sdk/go`), `sdk/php` (Composer `keel-context/sdk`); each exercised end to end against the server.
5. Publishing: npm (`@keel-context/sdk` 0.1.0, 2026-10-14), PyPI (inside `keel-context`), a Go module, Packagist. Registry accounts are Angel's step.

## Open

- MCP over HTTP for many tenants from one process (a path per tenant and agent, `/mcp/<tenant>/<agent>`, each with its own session manager). Not needed by Toche, whose own MCP server will proxy the five tools; one process per tenant covers the rest for now.
- Whether generated files carry a `--check` mode in CI (regenerate and diff) from the start.
- Streaming or batching for `POST /v1/events` beyond a list per request.
