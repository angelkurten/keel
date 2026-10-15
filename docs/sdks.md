# SDKs generated from the cartridge

The cartridge is the interface definition. `keel sdk <language> <cartridge>` writes one typed file for your product's repo, the way `protoc` does; a small published runtime per language holds the HTTP client and the envelope. Decisions and milestones: the [design note](design/sdk-generator.md).

| Language | Generator | Runtime | Status |
| --- | --- | --- | --- |
| TypeScript | `keel sdk typescript` | `@keel-context/sdk` (npm) | shipped; tested end to end from Bun against `keel serve --transport http` |
| Python | `keel sdk python` | inside `keel-context` | next |
| Go | `keel sdk go` | a Go module | next |
| PHP | `keel sdk php` | Packagist | next |

## TypeScript

```bash
uv run keel sdk typescript cartridges/fleet -o src/keel-fleet.ts     # in your product's repo
npm install @keel-context/sdk                                         # or bun add
```

The generated file has a type per event payload, an `emit` constructor per cataloged event (typed by subject kind and payload), `subjects.<kind>(id)` helpers, the rules, relations, agents and scopes as constants, and `client(options)` bound to one tenant and agent.

```ts
import { client, emit, subjects } from "./keel-fleet";

const keel = client({ baseUrl: "http://keel.internal:8000", tenant: "acme-logistics", secret: process.env.KEEL_SECRET, agent: "tires-bot" });

await keel.events([
  emit.tireDepthMeasured({ tenant: "acme-logistics", subject: subjects.tire("T-9001"), occurredAt: new Date(), source: "tires.measurements", payload: { unit_id: "MXK-4821", position: "P3", depth_mm: 2.6 } }),
]);

const ctx = await keel.context(subjects.tire("T-9001"));
ctx.evaluation.tire_critical;                                          // true
const depths = await keel.history(subjects.tire("T-9001"), { predicate: "depth_mm" });
await keel.annotate({ subject: subjects.tire("T-9001"), predicate: "assessment", value: "replace", scope: "task" });
```

A refusal from the server (scope policy, agent registry) is a `KeelError` with the status and the reason. Keep the generated file fresh with `keel sdk typescript <cartridge> -o <file> --check` in CI: it exits 1 when the cartridge moved on.

## Server side

The SDK talks to `keel serve --transport http` (see [connect](connect.md#serve-the-tools-over-http)): one process for many tenants, `X-Keel-Tenant` and `X-Keel-Agent` per request behind a shared secret. The server derives event ids (a content hash) when the product sends none, so retries are safe to reason about.
