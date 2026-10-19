import { useEffect, useMemo, useRef, useState } from "react";
import { CodeEditor } from "../CodeEditor";
import { placeProblems } from "../problems";
import type { Ctx } from "../App";
import { api, ApiError, type CartridgeFiles, type Example } from "../api";
import { CopyButton, Icon, store, Tabs } from "../ui";

type Mode = "generate" | "edit" | "example";
const EXAMPLES: { id: Example; text: string }[] = [
  { id: "fleet", text: "Vehicles, tires, inspections" },
  { id: "crm", text: "Accounts, contacts, deals" },
  { id: "clinics", text: "Patients, appointments, treatments" },
  { id: "finance", text: "Accounts, invoices, payments" },
];
const WARN_KEY = "keel.deployProblems";

export function CartridgeTab({ ctx }: { ctx: Ctx }) {
  const c = ctx.cartridge;
  const files = { yaml: c?.files?.["cartridge.yaml"] || "", py: c?.files?.["handlers.py"] || "" };
  const [view, setView] = useState<"cartridge.yaml" | "handlers.py">("cartridge.yaml");
  const [mode, setMode] = useState<Mode>(c?.example ? "generate" : "edit");
  const [warnings, setWarnings] = useState<string[]>(() => {
    try { return JSON.parse(store.get("session", WARN_KEY) || "[]") as string[]; } catch { return []; }
  });
  const shown = view === "cartridge.yaml" ? files.yaml : files.py;
  const n = (o?: Record<string, unknown>) => Object.keys(o || {}).length;

  /** Deploy, then hand over to the Starting screen; problems are warnings shown when the console is back. */
  async function deploy(body: CartridgeFiles | { example: Example }) {
    const r = await api.deployCartridge(body);
    store.set("session", WARN_KEY, r.problems.length ? JSON.stringify(r.problems) : null);
    ctx.refresh();
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="row wrap">
            <h1 className="h1">Cartridge <span className="mono">{c?.name}</span></h1>
            {c?.example && <span className="tag hot">example</span>}
          </div>
          <p className="muted">
            {n(c?.entities)} entity kinds · {n(c?.relations)} relations · {n(c?.events)} event types · {n(c?.rules)} rules
          </p>
        </div>
      </div>

      {warnings.length > 0 && (
        <div className="banner warn-banner">
          <div className="stack-s">
            <b>The last deploy started with warnings</b>
            <ul className="plain">{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          </div>
          <button type="button" className="btn ghost sm" aria-label="Dismiss"
            onClick={() => { setWarnings([]); store.set("session", WARN_KEY, null); }}><Icon name="x" size={14} /></button>
        </div>
      )}

      <div className="code">
        <div className="code-head">
          <Tabs label="File" items={["cartridge.yaml", "handlers.py"] as const} value={view} onChange={setView} />
          <CopyButton text={shown} />
        </div>
        <CodeEditor key={view} path="view" value={shown} readOnly language={view === "cartridge.yaml" ? "yaml" : "python"} height="min(420px, 50vh)" />
      </div>

      <div className="stack-s">
        <h2 className="h2">Change it</h2>
        <Tabs label="How" value={mode} onChange={setMode}
          items={[{ id: "generate", name: "Generate from your events" }, { id: "edit", name: "Edit or upload" }, { id: "example", name: "Start from an example" }]} />
      </div>
      {mode === "generate" && <Generate ctx={ctx} deploy={deploy} />}
      {mode === "edit" && <Editor ctx={ctx} initial={{ cartridge_yaml: files.yaml, handlers_py: files.py }} deploy={deploy} upload
        problems={warnings.length ? warnings : null} path="edit" />}
      {mode === "example" && <Examples ctx={ctx} deploy={deploy} />}
    </div>
  );
}

type Deploy = (body: CartridgeFiles | { example: Example }) => Promise<void>;

/** A deploy button that, while the example runs, asks first: the example's data goes away. */
function DeployButton({ ctx, label, disabled, onDeploy }: { ctx: Ctx; label: string; disabled?: boolean; onDeploy: () => Promise<void> }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const go = async () => {
    setBusy(true); setErr("");
    try { await onDeploy(); } catch (e) { setErr((e as Error).message); setBusy(false); setConfirm(false); }
  };
  return (
    <div className="stack-s">
      {confirm ? (
        <div className="confirm row wrap">
          <span className="warn small-text"><Icon name="warn" size={14} /> This replaces the example; its data (tenant demo) is forgotten.</span>
          <button type="button" className="btn sm danger" disabled={busy} onClick={go}>{busy ? "Deploying…" : "Replace it"}</button>
          <button type="button" className="btn sm ghost" disabled={busy} onClick={() => setConfirm(false)}>Cancel</button>
        </div>
      ) : (
        <button type="button" className="btn primary" disabled={disabled || busy}
          onClick={() => (ctx.cartridge?.example ? setConfirm(true) : go())}>{busy ? "Deploying…" : label}</button>
      )}
      {err && <p className="err">{err}</p>}
    </div>
  );
}

function Problems({ problems, okText }: { problems: string[] | null; okText: string }) {
  if (!problems) return null;
  if (!problems.length) return <p className="ok small-text"><Icon name="check" size={14} /> {okText}</p>;
  return (
    <ul className="problems">
      {problems.map((p, i) => <li key={i} className="warn">{p}</li>)}
    </ul>
  );
}

// --- edit / upload --------------------------------------------------------------------------------
function Editor({ ctx, initial, deploy, upload, problems: given, okText = "No problems found.", path }: {
  ctx: Ctx; initial: CartridgeFiles; deploy: Deploy; upload?: boolean; problems?: string[] | null; okText?: string; path: string;
}) {
  const [yaml, setYaml] = useState(initial.cartridge_yaml);
  const [py, setPy] = useState(initial.handlers_py);
  const [problems, setProblems] = useState<string[] | null>(given ?? null);
  const [checking, setChecking] = useState(false);
  const [err, setErr] = useState("");
  const [drag, setDrag] = useState(false);
  const body = { cartridge_yaml: yaml, handlers_py: py };
  const run = useRef(0);
  const edited = useRef(false);

  async function check() {
    const mine = ++run.current;
    setChecking(true); setErr("");
    try { const r = await api.checkCartridge({ cartridge_yaml: yaml, handlers_py: py }); if (mine === run.current) setProblems(r.problems); }
    catch (e) { if (mine === run.current) { setErr((e as Error).message); setProblems(null); } }
    finally { if (mine === run.current) setChecking(false); }
  }
  const checkRef = useRef(check);
  checkRef.current = check;

  // Live check: 800 ms after the last edit.
  useEffect(() => {
    if (!edited.current || !yaml.trim()) return;
    const t = window.setTimeout(() => checkRef.current(), 800);
    return () => clearTimeout(t);
  }, [yaml, py]);
  const edit = (set: (v: string) => void) => (v: string) => { edited.current = true; set(v); };

  // Problems become markers on the line they name; the rest go on line 1 and stay listed below.
  const placed = useMemo(() => placeProblems(problems || [], yaml, py), [problems]); // eslint-disable-line react-hooks/exhaustive-deps

  async function load(list: FileList | File[] | null) {
    for (const f of Array.from(list || [])) {
      const text = await f.text(), name = f.name.toLowerCase();
      if (name.endsWith(".py")) setPy(text);
      else if (name.endsWith(".yaml") || name.endsWith(".yml")) setYaml(text);
    }
    edited.current = true;
    setProblems(null);
  }

  return (
    <div className="card stack">
      {upload && (
        <div className={drag ? "drop slim on" : "drop slim"}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); load(e.dataTransfer.files); }}>
          <span className="small-text"><b>Load from files</b>: drop cartridge.yaml and handlers.py here, or pick them</span>
          <div className="row wrap">
            <label className="btn sm">cartridge.yaml<input type="file" accept=".yaml,.yml" className="sr-only" onChange={(e) => { load(e.target.files); e.target.value = ""; }} /></label>
            <label className="btn sm">handlers.py<input type="file" accept=".py" className="sr-only" onChange={(e) => { load(e.target.files); e.target.value = ""; }} /></label>
          </div>
        </div>
      )}
      <div className="files2">
        <div className="field"><span>cartridge.yaml</span>
          <CodeEditor path={path} language="yaml" value={yaml} onChange={edit(setYaml)} markers={placed.yaml} onSave={check} />
        </div>
        <div className="field"><span>handlers.py</span>
          <CodeEditor path={path} language="python" value={py} onChange={edit(setPy)} markers={placed.py} onSave={check} />
        </div>
      </div>
      {problems && problems.length > 0 && placed.unmatched.length < problems.length && (
        <p className="hint">{problems.length - placed.unmatched.length} problem{problems.length - placed.unmatched.length === 1 ? "" : "s"} marked in the editors. Cmd/Ctrl+S checks again.</p>
      )}
      <Problems problems={problems && problems.length ? placed.unmatched.length ? placed.unmatched : null : problems} okText={okText} />
      {err && <p className="err">{err}</p>}
      <div className="row wrap">
        <button type="button" className="btn" onClick={check} disabled={checking || !yaml.trim()}>
          {checking ? "Checking…" : problems ? "Check again" : "Check"}
        </button>
        <DeployButton ctx={ctx} label={`Deploy to ${ctx.me.account}`} disabled={!yaml.trim()} onDeploy={() => deploy(body)} />
      </div>
    </div>
  );
}

// --- generate ------------------------------------------------------------------------------------------
function parseEvents(text: string): { events: Record<string, unknown>[]; error?: string } {
  const t = text.trim();
  if (!t) return { events: [] };
  try {
    if (t.startsWith("[")) {
      const v = JSON.parse(t);
      if (!Array.isArray(v)) return { events: [], error: "Expected a JSON array of events." };
      return { events: v };
    }
    const out: Record<string, unknown>[] = [];
    for (const [i, raw] of t.split(/\r?\n/).entries()) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      try { out.push(JSON.parse(line)); } catch (e) { return { events: [], error: `Line ${i + 1}: ${(e as Error).message}` }; }
    }
    return { events: out };
  } catch (e) { return { events: [], error: (e as Error).message }; }
}

const PLACEHOLDER = `[
  {"type": "order.placed", "subject": "order:A-1001", "occurred_at": "2026-10-01T09:00:00Z", "source": "shop", "payload": {"customer_id": "C-7", "total": 129.9}},
  {"type": "order.shipped", "subject": "order:A-1001", "occurred_at": "2026-10-02T15:30:00Z", "source": "shop", "payload": {"carrier": "DHL"}}
]`;

function Generate({ ctx, deploy }: { ctx: Ctx; deploy: Deploy }) {
  const [text, setText] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [err, setErr] = useState("");
  const [result, setResult] = useState<{ files: CartridgeFiles; problems: string[]; attempts: number; gen: number } | null>(null);
  const [allowance, setAllowance] = useState<{ used: number; limit: number } | null>(null);
  const parsed = parseEvents(text);
  const n = parsed.events.length;
  const bad = parsed.error || (text.trim() && (n < 1 || n > 20) ? `Paste between 1 and 20 events (${n} now).` : "");

  async function propose() {
    setBusy(true); setErr(""); setStage("Writing your cartridge…");
    const t = window.setTimeout(() => setStage("Checking it against your events…"), 15000);
    try {
      const r = await api.generateCartridge({ events: parsed.events, description: description.trim() });
      setAllowance(r.allowance);
      setResult({ files: { cartridge_yaml: r.cartridge_yaml, handlers_py: r.handlers_py }, problems: r.problems, attempts: r.attempts, gen: Date.now() });
    } catch (e) {
      const s = e instanceof ApiError ? e.status : 0;
      setErr(s === 429 ? `Today's generations are used up. ${(e as Error).message}`
        : s === 409 ? `Generation is not available on this server. ${(e as Error).message}` : (e as Error).message);
    } finally { clearTimeout(t); setBusy(false); setStage(""); }
  }

  return (
    <div className="stack">
      <div className="card stack">
        <label className="field">A few real events <span className="hint">2–20, as a JSON array or one per line; the tenant is optional</span>
          <textarea className="mono" rows={9} spellCheck={false} value={text} placeholder={PLACEHOLDER} onChange={(e) => setText(e.target.value)} />
          {bad ? <span className="hint warn">{bad}</span> : n > 0 && <span className="hint">{n} event{n === 1 ? "" : "s"}</span>}
        </label>
        <label className="field">What your product does <span className="hint">one line</span>
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="An online shop for bike parts: orders, customers, shipments" />
        </label>
        <div className="row wrap">
          <button type="button" className="btn primary" disabled={busy || !n || !!bad || !description.trim()} onClick={propose}>
            {busy ? "Proposing…" : result ? "Propose again" : "Propose a cartridge"}
          </button>
          {allowance && <span className="hint">{allowance.used}/{allowance.limit} generations today</span>}
          {busy && <span className="muted small-text"><span className="spin" /> {stage} This takes 20 to 60 seconds.</span>}
        </div>
        {err && <p className="err">{err}</p>}
      </div>
      {result && (
        <>
          <p className="hint">Proposed in {result.attempts} attempt{result.attempts === 1 ? "" : "s"}. Review and edit both files before deploying.</p>
          <Editor key={result.gen} path={`gen${result.gen}`} ctx={ctx} initial={result.files} deploy={deploy} problems={result.problems}
            okText="Checked against your events: no problems" />
        </>
      )}
    </div>
  );
}

// --- examples --------------------------------------------------------------------------------------------
function Examples({ ctx, deploy }: { ctx: Ctx; deploy: Deploy }) {
  const current = ctx.cartridge?.example ? ctx.cartridge.name : null;
  return (
    <div className="examples">
      {EXAMPLES.map((ex) => (
        <div key={ex.id} className={ex.id === current ? "card example on" : "card example"}>
          <div className="row between">
            <b className="mono">{ex.id}</b>
            {ex.id === current && <span className="tag">current</span>}
          </div>
          <p className="muted small-text">{ex.text}</p>
          <DeployButton ctx={ctx} label={ex.id === current ? "Running" : `Use ${ex.id}`} disabled={ex.id === current}
            onDeploy={() => deploy({ example: ex.id })} />
        </div>
      ))}
    </div>
  );
}
