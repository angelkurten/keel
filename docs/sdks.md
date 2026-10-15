# SDKs generated from the cartridge

The cartridge is the interface definition. `keel sdk <language> <cartridge>` writes one typed file for your product's repo, the way `protoc` does; a small published runtime per language holds the HTTP client and the envelope. Decisions and milestones: the [design note](design/sdk-generator.md).

| Language | Generator | Runtime | Status |
| --- | --- | --- | --- |
| TypeScript | `keel sdk typescript` | `@keel-context/sdk` (npm) | shipped; tested end to end from Bun against `keel serve --transport http` |
| Python | `keel sdk python` | `keel.client` inside `keel-context` (stdlib only) | shipped; tested end to end in pytest |
| Go | `keel sdk go --package <pkg>` | `github.com/angelkurten/keel/sdk/go` | shipped; tested end to end with `go test` |
| PHP | `keel sdk php --package '<Namespace>'` | `keel-context/sdk` (Composer; Packagist listing pending) | shipped; tested end to end on PHP 8.3 |

Every generator reads the same `keel schema` document and writes the same things: a type per event payload, an `emit` constructor per cataloged event, subject helpers, the rules and constants, and a client factory. `--check` on any of them exits 1 when the cartridge moved on, for CI.

## Python

```bash
keel sdk python cartridges/fleet -o app/keel_fleet.py
```

```python
from app import keel_fleet as kf

keel = kf.client("http://keel.internal:8000", "acme-logistics", secret=os.environ["KEEL_SECRET"])
keel.events([kf.emit.tire_depth_measured("acme-logistics", kf.subjects.tire("T-9001"), datetime.now(timezone.utc), "tires", {"unit_id": "MXK-4821", "position": "P3", "depth_mm": 2.6})])
keel.context(kf.subjects.tire("T-9001"))["evaluation"]["tire_critical"]   # True
```

Payloads are `TypedDict`s, so your editor checks the keys; a refusal raises `keel.client.KeelError` with `status` and `detail`. A Python product that already imports `keel` can skip HTTP and call `Tools` directly; the generated module is for the ones that want the server boundary.

## Go

```bash
go get github.com/angelkurten/keel/sdk/go
keel sdk go cartridges/fleet -o internal/keelfleet/keel_fleet.go --package keelfleet
```

Payloads are structs with JSON tags, enums are typed string constants, nullable fields are pointers, and `Emit<Event>(tenant, subject, occurredAt, source, payload)` returns a `keel.Envelope`. Errors are `*keel.Error` with `Status` and `Detail`. The output is `gofmt` and `go vet` clean.

## PHP

```bash
composer require keel-context/sdk
keel sdk php cartridges/fleet -o src/Keel/Fleet.php --package 'App\Keel\Fleet'
```

Payloads are readonly value objects with `toArray()`, enums become PHP 8.1 enums, `Emit::<event>(...)` returns the envelope as an array and `Keel::client(...)` the client. Errors are `Keel\Sdk\KeelError`.

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

## Runtime options

Every runtime takes the same connection options besides the base URL, tenant, secret and agent: a `traceparent` sent as the W3C header on every call (Keel stores it with every write) and a request timeout.

| Runtime | Traceparent | Timeout |
| --- | --- | --- |
| Python `keel.client.Client` | `traceparent=` | `timeout=` seconds (30) |
| TypeScript `client({...})` | `traceparent` | none built in; pass your own `fetch` (one that sets an `AbortSignal`, or any wrapper) |
| Go `keel.Options` | `Traceparent` | `HTTPClient` (default `&http.Client{Timeout: 30 * time.Second}`), plus the `context.Context` of each call |
| PHP `Keel\Sdk\Client` | `traceparent:` | `timeout:` seconds (30) |

The generated Python `client()` and PHP `Keel::client()` take `timeout` and `traceparent` too (`m.client(url, tenant, secret=..., timeout=10, traceparent=tp)`, `Keel::client($url, $tenant, $secret, $agent, 10, $tp)`). The runtime client works the same, and the generated `emit` and `subjects` work with it unchanged:

```python
from keel.client import Client

keel = Client("http://keel.internal:8000", "acme-logistics", secret=os.environ["KEEL_SECRET"], agent="tires-bot", timeout=10, traceparent=traceparent)
```

```php
$keel = new \Keel\Sdk\Client('http://keel.internal:8000', 'acme-logistics', getenv('KEEL_SECRET'), 'tires-bot', timeout: 10, traceparent: $traceparent);
```

Reads follow the memory scopes: agent-scope memory comes back only to the agent that wrote it, so set `agent` to read your own, and task memory tied to a task id only to calls that pass that `task` ([details](connect.md#serve-the-tools-over-http)).

## Server side

The SDK talks to `keel serve --transport http` (see [connect](connect.md#serve-the-tools-over-http)): one process for many tenants, `X-Keel-Tenant` and `X-Keel-Agent` per request behind a shared secret. The server derives event ids (a content hash) when the product sends none, so retries are safe to reason about.
