import { Client, envelope, KeelError } from "@keel-context/sdk";
const e = envelope({ type: "x.y", tenant: "t", subject: "k:1", occurredAt: new Date("2026-10-14T00:00:00Z"), source: "s", payload: { a: 1 } });
console.log("envelope ok:", e.occurred_at, typeof Client, KeelError.name);
