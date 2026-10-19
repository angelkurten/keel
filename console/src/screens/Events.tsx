import { useEffect, useMemo, useRef, useState } from "react";
import type { Ctx } from "../App";
import { api, ApiError, count, type EventProblem, type RecentEvent } from "../api";
import { exampleEnvelope, WEBHOOK_LANGS, webhookSnippet, type WebhookLang } from "../codegen";
import { ago, Code, Icon, Tabs } from "../ui";

type Mode = "webhook" | "upload";
const CHUNK = 500;

/** Lets a dotted type or a `kind:id` subject wrap after its dots and colons, not mid-word. */
const soft = (s: string) => s.split(/(?<=[.:_-])/).flatMap((part, i) => (i ? [<wbr key={i} />, part] : [part]));

export function Events({ ctx }: { ctx: Ctx }) {
  const [mode, setMode] = useState<Mode>("webhook");
  const tabs: { id: string; name: string; soon?: boolean }[] = [
    { id: "webhook", name: "Webhook" }, { id: "upload", name: "Upload events.jsonl" },
    { id: "cdc", name: "Postgres CDC", soon: true }, { id: "generate", name: "Generate cartridge", soon: true },
  ];
  return (
    <div className="stack">
      <div>
        <h1 className="h1">Send your events</h1>
        <p className="muted">Pick how your product's events reach Keel. You can use more than one.</p>
      </div>
      <div role="tablist" aria-label="Source" className="tabs">
        {tabs.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={t.id === mode} disabled={t.soon}
            className={t.id === mode ? "on" : ""} onClick={() => !t.soon && setMode(t.id as Mode)}>
            {t.name}{t.soon && <span className="soon inline-soon">coming soon</span>}
          </button>
        ))}
      </div>
      <div className="events-layout">
        <section className="stack">{mode === "webhook" ? <Webhook ctx={ctx} /> : <Upload ctx={ctx} />}</section>
        <Live tenant={ctx.tenant} />
      </div>
    </div>
  );
}

function Webhook({ ctx }: { ctx: Ctx }) {
  const [lang, setLang] = useState<WebhookLang>("curl");
  const envelope = useMemo(() => exampleEnvelope(ctx.cartridge, ctx.tenant), [ctx.cartridge, ctx.tenant]);
  const url = `${ctx.me.endpoint.replace(/\/$/, "")}/v1/events`;
  return (
    <>
      <div className="card stack-s">
        <div className="endpoint"><span className="method">POST</span><span className="mono ellipsis">{url}</span></div>
        <p className="muted small-text">
          Post one envelope or a list. Each envelope names its tenant; with a key narrowed to a tenant, use that tenant
          (and send it as <span className="mono">X-Keel-Tenant</span>). The <span className="mono">id</span> makes retries
          safe: an id Keel has already applied is counted as a duplicate, not applied twice.
        </p>
      </div>
      <Code title="One event, as Keel expects it" code={JSON.stringify(envelope, null, 2)} />
      <Code title="Send it" extra={<Tabs label="Language" items={WEBHOOK_LANGS} value={lang} onChange={setLang} />}
        code={webhookSnippet(lang, ctx.me.endpoint, ctx.tenant, envelope)} />
    </>
  );
}

// --- Live log --------------------------------------------------------------------------------------
const BADGE: Record<string, string> = { applied: "ok", duplicate: "muted", unhandled: "warn", skipped: "warn", error: "danger" };

function Live({ tenant }: { tenant: string }) {
  const [rows, setRows] = useState<RecentEvent[] | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [, setNow] = useState(0);

  useEffect(() => {
    let stop = false, timer = 0;
    setRows(null);
    const poll = async () => {
      if (document.visibilityState === "visible") {
        try { const r = await api.recentEvents(tenant); if (!stop) { setRows(r); setErr(""); } }
        catch (e) { if (!stop) setErr((e as Error).message); }
        setNow(Date.now());
      }
      if (!stop) timer = window.setTimeout(poll, 3000);
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, [tenant]);

  return (
    <section className="card live" aria-label="Live log">
      <div className="row between wrap">
        <span className="row"><span className="pulse" /> <b>Live</b></span>
        <span className="hint">recent events · tenant <span className="mono">{tenant}</span></span>
      </div>
      {err && <p className="err small-text">{err}</p>}
      {!rows && !err && <p className="muted small-text">Loading…</p>}
      {rows?.length === 0 && <p className="muted small-text">No events received yet on tenant <span className="mono">{tenant}</span>. Send one with the snippet.</p>}
      {!!rows?.length && (
        <div className="table-wrap"><table>
          <thead><tr><th>time</th><th>type</th><th>subject</th><th>status</th></tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const key = `${r.id}-${i}`;
              return (
                <tr key={key} className={r.reason ? "clickable" : ""} title={r.reason || undefined}
                  onClick={() => r.reason && setOpen(open === key ? null : key)}>
                  <td className="mono nowrap" title={r.received_at}>{ago(r.received_at)}</td>
                  <td className="mono">{soft(r.type)}</td>
                  <td className="mono">{soft(r.subject)}</td>
                  <td>
                    <span className={`badge ${BADGE[r.status] || ""}`}>{r.status}</span>
                    {open === key && r.reason && <div className="reason small-text">{r.reason}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      )}
      <p className="hint">Kept in your Keel's memory: the list starts empty again after a restart.</p>
    </section>
  );
}

// --- Upload ------------------------------------------------------------------------------------------
type Parsed = { name: string; events: Record<string, unknown>[]; lines: number[]; bad: { line: number; message: string }[] };

function parseFile(name: string, text: string): Parsed {
  const out: Parsed = { name, events: [], lines: [], bad: [] };
  const trimmed = text.trim();
  if (name.endsWith(".json") || trimmed.startsWith("[")) {
    try {
      const v = JSON.parse(trimmed);
      const list = Array.isArray(v) ? v : [v];
      list.forEach((e, i) => {
        if (e && typeof e === "object" && !Array.isArray(e)) { out.events.push(e); out.lines.push(i + 1); }
        else out.bad.push({ line: i + 1, message: "not a JSON object" });
      });
      return out;
    } catch (e) {
      if (name.endsWith(".json")) { out.bad.push({ line: 1, message: (e as Error).message }); return out; }
    }
  }
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    try {
      const v = JSON.parse(line);
      if (v && typeof v === "object" && !Array.isArray(v)) { out.events.push(v); out.lines.push(i + 1); }
      else out.bad.push({ line: i + 1, message: "not a JSON object" });
    } catch (e) { out.bad.push({ line: i + 1, message: (e as Error).message }); }
  });
  return out;
}

const chunks = <T,>(xs: T[]) => Array.from({ length: Math.ceil(xs.length / CHUNK) }, (_, i) => xs.slice(i * CHUNK, (i + 1) * CHUNK));
type Summary = { processed: number; duplicates: number; unhandled: number; errors: number; skipped: number; details: unknown[] };
const EMPTY: Summary = { processed: 0, duplicates: 0, unhandled: 0, errors: 0, skipped: 0, details: [] };

function Upload({ ctx }: { ctx: Ctx }) {
  const [file, setFile] = useState<Parsed | null>(null);
  const [problems, setProblems] = useState<(EventProblem & { line: number })[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [report, setReport] = useState<Summary | null>(null);
  const [err, setErr] = useState("");
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const tenant = ctx.tenant;

  // Re-check when the file or the tenant changes: nothing is ingested by a check.
  useEffect(() => {
    if (!file || !file.events.length) { setProblems(null); return; }
    let stop = false;
    (async () => {
      setChecking(true); setErr(""); setProblems(null);
      try {
        const all: (EventProblem & { line: number })[] = [];
        let offset = 0;
        for (const part of chunks(withTenant(file.events, tenant))) {
          const found = await api.checkEvents(tenant, part);
          for (const p of found) all.push({ ...p, line: file.lines[offset + p.index] ?? offset + p.index + 1 });
          offset += part.length;
          if (stop) return;
        }
        setProblems(all);
      } catch (e) { if (!stop) setErr((e as Error).message); }
      finally { if (!stop) setChecking(false); }
    })();
    return () => { stop = true; };
  }, [file, tenant]);

  async function pick(f: File | undefined) {
    if (!f) return;
    setReport(null); setErr(""); setProgress(null);
    setFile(parseFile(f.name.toLowerCase(), await f.text()));
  }

  async function ingest() {
    if (!file) return;
    const parts = chunks(withTenant(file.events, tenant));
    let sum = { ...EMPTY }, done = 0;
    setErr(""); setReport(null); setProgress({ done: 0, total: file.events.length });
    try {
      for (const part of parts) {
        const r = await api.ingestEvents(tenant, part);
        sum = { processed: sum.processed + r.processed, duplicates: sum.duplicates + r.duplicates,
          unhandled: sum.unhandled + count(r.unhandled), errors: sum.errors + count(r.errors), skipped: sum.skipped + (r.skipped || 0),
          details: [...sum.details, ...(Array.isArray(r.errors) ? r.errors : []), ...(Array.isArray(r.unhandled) ? r.unhandled : [])] };
        done += part.length;
        setProgress({ done, total: file.events.length });
      }
    } catch (e) {
      const limited = e instanceof ApiError && e.status === 429;
      setErr(`${limited ? "Monthly event limit reached. " : ""}${(e as Error).message} (${done} of ${file.events.length} sent)`);
    }
    setReport(sum);
    ctx.eventsSent();
  }

  const busy = !!progress && progress.done < progress.total && !report;
  const n = file?.events.length || 0;
  return (
    <>
      <label className={drag ? "drop on" : "drop"}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files[0]); }}>
        <Icon name="upload" size={22} />
        <b>Drop an events.jsonl file, or click to pick one</b>
        <span className="muted small-text">One envelope per line (lines starting with # are skipped), or a .json array. Each one is checked against the cartridge's catalog before anything is ingested.</span>
        <input ref={input} type="file" accept=".jsonl,.json,.ndjson,application/json" className="sr-only"
          onChange={(e) => { pick(e.target.files?.[0]); e.target.value = ""; }} />
      </label>

      {file && (
        <div className="card stack-s">
          <div className="row between wrap">
            <span><span className="mono">{file.name}</span> · {n} event{n === 1 ? "" : "s"}
              {file.bad.length > 0 && <span className="err"> · {file.bad.length} line{file.bad.length === 1 ? "" : "s"} not valid JSON</span>}</span>
            <button type="button" className="btn ghost sm" onClick={() => { setFile(null); setReport(null); setProgress(null); }}>Clear</button>
          </div>
          {file.bad.length > 0 && (
            <ul className="problems">
              {file.bad.slice(0, 50).map((b) => <li key={b.line}><span className="mono">line {b.line}</span> <span className="err">{b.message}</span></li>)}
            </ul>
          )}
          {checking && <p className="muted small-text"><span className="spin" /> Checking against the catalog…</p>}
          {problems && problems.length === 0 && n > 0 && <p className="ok small-text">Every event matches the catalog.</p>}
          {problems && problems.length > 0 && (
            <>
              <p className="warn small-text">{problems.length} event{problems.length === 1 ? "" : "s"} with catalog problems: they can be sent, but the ones of an unknown type stay unhandled.</p>
              <ul className="problems">
                {problems.slice(0, 100).map((p) => (
                  <li key={p.line}><span className="mono">line {p.line}</span> {p.type && <span className="mono muted">{p.type}</span>} <span className="warn">{p.problems.join("; ")}</span></li>
                ))}
                {problems.length > 100 && <li className="muted">…and {problems.length - 100} more</li>}
              </ul>
            </>
          )}
          {err && <p className="err">{err}</p>}
          <p className="hint">Each envelope's tenant is replaced by the sidebar tenant, <span className="mono">{tenant}</span>.</p>
          <button type="button" className="btn primary" disabled={!n || file.bad.length > 0 || busy || checking} onClick={ingest}>
            {busy ? "Ingesting…" : `Ingest ${n} event${n === 1 ? "" : "s"} into tenant ${tenant}`}
          </button>
          {progress && (
            <div className="stack-s">
              <div className="bar big"><div style={{ width: `${(progress.done / Math.max(1, progress.total)) * 100}%` }} /></div>
              <span className="hint">{progress.done} / {progress.total}</span>
            </div>
          )}
          {report && (
            <div className="summary">
              <Stat label="processed" n={report.processed} cls="ok" /><Stat label="duplicates" n={report.duplicates} />
              <Stat label="unhandled" n={report.unhandled} cls={report.unhandled ? "warn" : ""} />
              <Stat label="skipped" n={report.skipped} cls={report.skipped ? "warn" : ""} />
              <Stat label="errors" n={report.errors} cls={report.errors ? "err" : ""} />
            </div>
          )}
          {report && report.details.length > 0 && (
            <details className="preview">
              <summary className="small-text">Unhandled and failed events ({report.details.length})</summary>
              <pre className="code-pre">{report.details.slice(0, 200).map((d) => JSON.stringify(d)).join("\n")}</pre>
            </details>
          )}
        </div>
      )}
    </>
  );
}

const withTenant = (events: Record<string, unknown>[], tenant: string) => events.map((e) => ({ ...e, tenant }));
const Stat = ({ label, n, cls = "" }: { label: string; n: number; cls?: string }) => (
  <div className="stat"><span className={`stat-n ${cls}`}>{n}</span><span className="hint">{label}</span></div>
);
