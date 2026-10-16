// End to end from Bun: start `keel serve --transport http` (Python), emit typed events, ask the tools.
import { afterAll, beforeAll, expect, test } from "bun:test";
import { type Entity, KeelError } from "@keel-context/sdk";
import { client, emit, subjects } from "./keel-fleet";

const ROOT = new URL("../../..", import.meta.url).pathname;
const PORT = 20000 + Math.floor(Math.random() * 20000);
const SECRET = "s3cret";
let server: ReturnType<typeof Bun.spawn>;

beforeAll(async () => {
  server = Bun.spawn(["uv", "run", "keel", "serve", "examples/fleet", "--transport", "http", "--port", String(PORT)], {
    cwd: ROOT,
    env: { ...process.env, KEEL_SECRET: SECRET },
    stdout: "ignore",
    stderr: "ignore",
  });
  for (let i = 0; i < 200; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${PORT}/health`)).ok) return;
    } catch {}
    await Bun.sleep(100);
  }
  throw new Error("keel serve did not come up");
});

afterAll(() => server?.kill());

test("typed events in, context with the rule's verdict out", async () => {
  const keel = client({ baseUrl: `http://127.0.0.1:${PORT}`, tenant: "acme-logistics", secret: SECRET });
  const tenant = "acme-logistics";
  const report = await keel.events([
    emit.unitRegistered({ tenant, subject: subjects.unit("MXK-4821"), occurredAt: "2026-09-01T09:00:00Z", source: "fleet.vehicles", payload: { id: "MXK-4821", plate: "MXK-4821", model: "Cascadia" } }),
    emit.tireDepthMeasured({ tenant, subject: subjects.tire("T-9001"), occurredAt: "2026-09-10T08:00:00Z", source: "tires", payload: { unit_id: "MXK-4821", position: "P3", depth_mm: 5.2 } }),
    emit.tireDepthMeasured({ tenant, subject: subjects.tire("T-9001"), occurredAt: new Date("2026-10-03T08:00:00Z"), source: "tires", payload: { unit_id: "MXK-4821", position: "P3", depth_mm: 2.6 } }),
  ]);
  expect(report).toEqual({ processed: 3, duplicates: 0, unhandled: [], errors: [] });

  const ctx = await keel.context(subjects.tire("T-9001"));
  expect(ctx.evaluation).toEqual({ tire_critical: true });
  expect(ctx.rules.tire_critical.threshold_mm).toBe(3);
  const before = await keel.context(subjects.tire("T-9001"), "2026-09-20");
  expect(before.evaluation).toEqual({ tire_critical: false });
  const depths = await keel.history(subjects.tire("T-9001"), { predicate: "depth_mm" });
  expect(depths.map((f) => f.value)).toEqual([5.2, 2.6]);
  expect((await keel.related(subjects.unit("MXK-4821"), "mounted_on"))[0].subject).toBe("tire:T-9001");
  expect((await keel.search("T-9001")).length).toBeGreaterThan(0);

  const note = await keel.annotate({ subject: subjects.tire("T-9001"), predicate: "assessment", value: "replace", scope: "task", by: "agent:bun" });
  expect(note.scope).toBe("task");
  await expect(keel.annotate({ subject: subjects.tire("T-9001"), predicate: "assessment", value: "x", scope: "tenant" })).rejects.toBeInstanceOf(KeelError);
  const keyed = { subject: subjects.tire("T-9001"), predicate: "plan", value: "swap", task: "run-1", key: "k1" };
  expect((await keel.annotate(keyed)).recorded_at).toBe((await keel.annotate(keyed)).recorded_at);
  expect((await keel.search("", { kind: "tire" })).map((e) => (e as { key: string }).key)).toEqual(["tire:T-9001"]);
  expect((await keel.history(null, { changedSince: "2000-01-01T00:00:00Z", task: "run-1" })).map((f) => f.predicate)).toEqual(["plan"]);
  expect((await keel.contextConcise(subjects.tire("T-9001"))).facts).toMatchObject({ depth_mm: 2.6 });
  expect((await keel.annotate({ subject: subjects.tire("T-9001"), predicate: "swap", value: true, status: "proposed" })).status).toBe("proposed");
  // the context for a task (0.19): facts and neighbours on each result, batches, hops, hidden predicates
  const [tireHit] = (await keel.search("T-9001", { detail: "concise" })) as Entity[];
  expect(tireHit.facts).toMatchObject({ depth_mm: 2.6 });
  const [unitHit] = (await keel.search("", { kind: "unit", depth: 1, detail: "concise" })) as Entity[];
  expect(unitHit.neighbors?.find((n) => n.subject === "tire:T-9001")?.facts).toMatchObject({ depth_mm: 2.6 });
  // by the value of a fact, and why each result matched (0.20)
  expect(((await keel.search("", { kind: "tire", where: { depth_mm: 2.6 } })) as Entity[]).map((h) => h.key)).toEqual(["tire:T-9001"]);
  expect(((await keel.search("", { kind: ["unit", "tire"], where: { depth_mm: 2.6 } })) as Entity[]).map((h) => h.key)).toEqual(["tire:T-9001"]);
  const [byValue] = (await keel.search("2.6", { kind: "tire" })) as Entity[];
  expect([byValue.matched, byValue.snippet]).toEqual(["depth_mm", "2.6"]);
  const both = await keel.contexts([subjects.tire("T-9001"), subjects.unit("MXK-4821")], undefined, 0, { exclude: ["depth_mm"] });
  expect(both.map((x) => x.subject)).toEqual(["tire:T-9001", "unit:MXK-4821"]);
  expect(both[0].facts.map((f) => f.predicate)).not.toContain("depth_mm");
  const just = await keel.contexts([subjects.tire("T-9001"), subjects.unit("MXK-4821")], undefined, 0, { only: ["depth_mm"] });
  expect(just.map((x) => x.facts.map((f) => f.predicate))).toEqual([["depth_mm"], []]);
  const lists = await keel.searches(["T-9001", "MXK"], { perKind: 1 });
  expect(lists.length).toBe(2);
  await keel.events(emit.driverAssigned({ tenant, subject: subjects.unit("MXK-4821"), occurredAt: "2026-09-01T09:05:00Z", source: "core", payload: { driver_id: "D-118" } }));
  const hops = await keel.related(subjects.tire("T-9001"), ["mounted_on", "assigned_to"], undefined, 2);
  expect(hops.find((r) => r.subject === "driver:D-118")).toMatchObject({ hops: 2, via: "unit:MXK-4821" });
  const other = client({ baseUrl: `http://127.0.0.1:${PORT}`, tenant: "someone-else", secret: SECRET });
  expect((await other.context(subjects.tire("T-9001"))).entity).toBeNull();
  await expect(client({ baseUrl: `http://127.0.0.1:${PORT}`, tenant, secret: "wrong" }).search("x")).rejects.toMatchObject({ status: 401 });
});
