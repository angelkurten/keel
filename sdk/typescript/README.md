# @keel-context/sdk

Runtime for the clients `keel sdk typescript <cartridge>` generates: the event envelope and the five Keel tools over HTTP (`keel serve --transport http`).

```bash
npm install @keel-context/sdk          # or: bun add @keel-context/sdk
keel sdk typescript cartridges/fleet -o src/keel-fleet.ts   # from the Keel repo of your cartridge
```

```ts
import { client, emit, subjects } from "./keel-fleet";

const keel = client({ baseUrl: "http://keel.internal:8000", tenant: "acme-logistics", secret: process.env.KEEL_SECRET });

await keel.events([
  emit.tireDepthMeasured({
    tenant: "acme-logistics",
    subject: subjects.tire("T-9001"),
    occurredAt: new Date(),
    source: "tires.measurements",
    payload: { unit_id: "MXK-4821", position: "P3", depth_mm: 2.6 },
  }),
]);

const ctx = await keel.context(subjects.tire("T-9001"));
ctx.evaluation.tire_critical; // true: the rule lives in the cartridge, the verdict comes with the context
```

The generated file is the only thing that knows your cartridge; regenerate it when the cartridge changes (`--check` in CI tells you when). Docs: https://angelkurten.github.io/keel/
