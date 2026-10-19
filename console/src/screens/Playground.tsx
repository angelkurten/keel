import { useEffect, useMemo, useRef, useState } from "react";
import type { Ctx } from "../App";
import { api, ApiError, TOOLS, type AgentConfig, type Allowance, type ChatMessage, type Models, type OwnKey, type Tool, type TraceRow } from "../api";
import { curlFor, exportAgent, FRAMEWORKS, pythonFor, starterPrompt, type Framework } from "../codegen";
import { Code, CopyButton, download, Icon, Markdown, store, Tabs } from "../ui";

export function Playground({ ctx }: { ctx: Ctx }) {
  const [mode, setMode] = useState<"Tools" | "Agent">("Tools");
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="h1">Playground</h1>
          <p className="muted">Try tenant <span className="mono fg">{ctx.tenant}</span> with an agent, or call the five tools by hand.</p>
        </div>
        <Tabs label="Mode" items={["Tools", "Agent"] as const} value={mode} onChange={setMode} />
      </div>
      {mode === "Tools" ? <ToolsMode ctx={ctx} /> : <AgentMode ctx={ctx} />}
    </div>
  );
}

// --- Tools ------------------------------------------------------------------------------------
type FieldDef = { name: string; hint?: string; options?: string[]; kind?: "int" | "json" | "list" };
const FIELDS: Record<Tool, FieldDef[]> = {
  context: [{ name: "subject" }, { name: "as_of", hint: "optional · empty means now" }, { name: "depth", options: ["0", "1", "2"], kind: "int" },
    { name: "detail", options: ["full", "concise"] }],
  history: [{ name: "subject" }, { name: "predicate", hint: "optional" }, { name: "since", hint: "optional · ISO date" }, { name: "until", hint: "optional · ISO date" }],
  search: [{ name: "text", hint: "empty lists everything" }, { name: "kind", hint: "optional" }, { name: "rule", hint: "optional" },
    { name: "mode", options: ["text", "semantic"] }, { name: "limit", kind: "int", hint: "optional" }],
  annotate: [{ name: "subject" }, { name: "predicate" }, { name: "value", kind: "json", hint: "JSON, or plain text" },
    { name: "scope", options: ["task", "agent", "tenant", "global"] }, { name: "evidence", hint: "required for tenant and global" }],
  related: [{ name: "subject" }, { name: "relation", hint: "optional" }, { name: "depth", options: ["1", "2", "3"], kind: "int" }],
};
const DEFAULTS: Record<string, string> = { depth: "", detail: "full", mode: "text", scope: "task" };

function buildBody(tool: Tool, v: Record<string, string>): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const f of FIELDS[tool]) {
    const raw = (v[f.name] ?? "").trim();
    if (f.name === "text") { body.text = raw; continue; }
    if (!raw) continue;
    if (f.kind === "int") body[f.name] = Number(raw);
    else if (f.kind === "json") { try { body[f.name] = JSON.parse(raw); } catch { body[f.name] = raw; } }
    else body[f.name] = raw;
  }
  return body;
}

function ToolsMode({ ctx }: { ctx: Ctx }) {
  const [tool, setTool] = useState<Tool>("context");
  const [values, setValues] = useState<Record<Tool, Record<string, string>>>(() => {
    const ex = ctx.cartridge?.example;
    return {
      context: { subject: ex ? "tire:T-9001" : "", detail: "full" },
      history: { subject: ex ? "tire:T-9001" : "", predicate: ex ? "depth_mm" : "" },
      search: { text: "", mode: "text", rule: ex ? "tire_critical" : "" },
      annotate: { subject: "", predicate: "", scope: "task" },
      related: { subject: ex ? "unit:MXK-4821" : "", depth: "1" },
    };
  });
  const [out, setOut] = useState<{ status: string; ms: number; json: string; err: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [lang, setLang] = useState<"curl" | "Python">("curl");
  const v = values[tool];
  const body = buildBody(tool, v);
  const target = { endpoint: ctx.me.endpoint, tenant: ctx.tenant, agent: ctx.agent };

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const t0 = performance.now();
    try {
      const r = await api.tool(tool, body, ctx.tenant, ctx.agent);
      setOut({ status: "200", ms: Math.round(performance.now() - t0), json: JSON.stringify(r, null, 2), err: false });
    } catch (err) {
      setOut({ status: err instanceof ApiError ? String(err.status) : "error", ms: Math.round(performance.now() - t0), json: (err as Error).message, err: true });
    } finally { setBusy(false); }
  }

  return (
    <div className="stack">
      <Tabs label="Tool" items={TOOLS} value={tool} onChange={(t) => { setTool(t); setOut(null); }} />
      <div className="split">
        <form className="card form" onSubmit={run}>
          {FIELDS[tool].map((f) => (
            <label key={f.name} className="field">
              <span>{f.name} {f.hint && <span className="hint inline">{f.hint}</span>}</span>
              {f.options ? (
                <select className="mono" value={v[f.name] ?? DEFAULTS[f.name] ?? ""} onChange={(e) => setValues({ ...values, [tool]: { ...v, [f.name]: e.target.value } })}>
                  {f.name === "depth" && tool === "context" && <option value="">default</option>}
                  {f.options.map((o) => <option key={o}>{o}</option>)}
                </select>
              ) : f.kind === "json" ? (
                <textarea className="mono" rows={3} value={v[f.name] ?? ""} onChange={(e) => setValues({ ...values, [tool]: { ...v, [f.name]: e.target.value } })} />
              ) : (
                <input className="mono" value={v[f.name] ?? ""} spellCheck={false}
                  placeholder={f.name === "as_of" ? "2026-10-03T00:00:00Z" : ""}
                  onChange={(e) => setValues({ ...values, [tool]: { ...v, [f.name]: e.target.value } })} />
              )}
            </label>
          ))}
          <button className="btn primary" disabled={busy}><Icon name="play" size={14} /> {busy ? "Running…" : `Run ${tool}`}</button>
          <span className="hint">Playground calls count as tool calls in your usage.</span>
        </form>
        <section className="stack">
          <div className="code">
            <div className="code-head"><span>Result</span>{out && <span className={out.err ? "warn mono" : "ok mono"}>{out.status} · {out.ms} ms</span>}</div>
            <pre className={out?.err ? "err" : ""}>{out ? out.json : "Run the tool to see its answer."}</pre>
          </div>
          <Code title="Same call from code"
            extra={<Tabs label="Language" items={["curl", "Python"] as const} value={lang} onChange={setLang} />}
            code={lang === "curl" ? curlFor(target, tool, body) : pythonFor(target, tool, body)} />
        </section>
      </div>
    </div>
  );
}

// --- Agent ------------------------------------------------------------------------------------
type Turn = { role: "user" | "assistant" | "error"; text: string; trace?: TraceRow[] };

function AgentMode({ ctx }: { ctx: Ctx }) {
  const agents = Object.keys(ctx.cartridge?.agents || {});
  const starter = useMemo(() => starterPrompt(ctx.cartridge), [ctx.cartridge]);
  const [name, setName] = useState(ctx.cartridge ? `${cap(ctx.cartridge.name)} assistant` : "Assistant");
  const [models, setModels] = useState<Models | null>(null);
  const [model, setModel] = useState(ctx.self ? "openai/gpt-4.1-mini" : "");
  const [allowance, setAllowance] = useState<Allowance | null>(null);
  const [own, setOwn] = useState<OwnKey | null>(() => {
    try { const s = store.get("session", "keel.ownKey"); return s ? (JSON.parse(s) as OwnKey) : null; } catch { return null; }
  });
  const [ownOpen, setOwnOpen] = useState(!!own);
  const [identity, setIdentity] = useState(ctx.agent || (ctx.self ? "" : agents[0]) || "");
  const [system, setSystem] = useState(starter);
  const [tools, setTools] = useState<string[]>([...TOOLS]);
  const [saved, setSaved] = useState<{ name: string; config: AgentConfig }[]>([]);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [msg, setMsg] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ctx.self) return; // no control plane: no hosted models or saved agents
    api.models().then((m) => { setModels(m); setModel((cur) => cur || m.default); setAllowance(m.allowance); }).catch((e) => setMsg((e as Error).message));
    api.agents().then(setSaved).catch(() => {});
  }, []);
  useEffect(() => { if (!system && starter) setSystem(starter); }, [starter]); // eslint-disable-line
  useEffect(() => { if (!identity && agents[0] && !ctx.self) setIdentity(agents[0]); }, [agents.join()]); // eslint-disable-line
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }); }, [turns, busy]);
  useEffect(() => { store.set("session", "keel.ownKey", own && own.key ? JSON.stringify(own) : null); }, [own]);

  const config = (): AgentConfig => ({ name, model, system, tools, tenant: ctx.tenant, ...(identity ? { agent: identity } : {}) });
  function load(c: AgentConfig) {
    setName(c.name); setModel(c.model); setSystem(c.system); setTools(c.tools); setIdentity(c.agent || ctx.agent || agents[0] || "");
    setHistory([]); setTurns([]);
  }
  async function save() {
    try { await api.saveAgent(name, config()); setSaved(await api.agents()); setMsg(`Saved "${name}".`); }
    catch (e) { setMsg((e as Error).message); }
  }
  async function remove(n: string) {
    try { await api.deleteAgent(n); setSaved(await api.agents()); } catch (e) { setMsg((e as Error).message); }
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    const next: ChatMessage[] = [...history, { role: "user", content: text }];
    setInput(""); setBusy(true); setTurns((t) => [...t, { role: "user", text }]);
    try {
      const r = await api.chat({
        model, system, tools, tenant: ctx.tenant, messages: next,
        ...(identity ? { agent: identity } : {}),
        ...(ownOpen && own?.key ? { own_key: own } : {}),
      });
      setHistory([...next, ...r.messages]);
      setAllowance(r.allowance);
      const answer = [...r.messages].reverse().find((m) => m.role === "assistant" && m.content)?.content || "";
      setTurns((t) => [...t, { role: "assistant", text: answer, trace: r.trace }]);
    } catch (err) {
      setHistory(history);
      const limited = err instanceof ApiError && err.status === 429;
      if (limited) setOwnOpen(true);
      setTurns((t) => [...t, { role: "error", text: limited ? `${(err as Error).message} Use your own key to keep going.` : (err as Error).message }]);
    } finally { setBusy(false); }
  }

  const spec = { endpoint: ctx.me.endpoint, tenant: ctx.tenant, agent: identity || undefined, name, system, model, tools };

  return (
    <div className="stack">
      <div className="row wrap between">
        <div className="row wrap">
          {!ctx.self && <>
          {saved.length > 0 && (
            <select aria-label="Load a saved agent" value="" onChange={(e) => { const s = saved.find((x) => x.name === e.target.value); if (s) load(s.config); }}>
              <option value="">Load…</option>
              {saved.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
          )}
          <button type="button" className="btn" onClick={save} disabled={!name.trim()}>Save</button>
          {saved.some((s) => s.name === name) && <button type="button" className="btn ghost" onClick={() => remove(name)}>Delete</button>}
          </>}
          {msg && <span className="muted small-text">{msg}</span>}
        </div>
        <button type="button" className="btn primary" onClick={() => setExporting(!exporting)}><Icon name="download" size={14} /> Export agent</button>
      </div>
      {exporting && <ExportPanel spec={spec} onClose={() => setExporting(false)} />}
      <div className="split agent">
        <form className="card form" onSubmit={(e) => e.preventDefault()}>
          <label className="field">Name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
          {ctx.self ? (
            <label className="field">Model <span className="hint inline">for the exported file</span>
              <input className="mono" value={model} spellCheck={false} onChange={(e) => setModel(e.target.value)} placeholder="openai/gpt-4.1-mini" />
            </label>
          ) : <label className="field">Model
            <select value={model} onChange={(e) => setModel(e.target.value)} disabled={!models}>
              {models?.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <span className="hint row between">
              <span>{allowance ? `${allowance.used}/${allowance.limit} messages today` : ""}</span>
              <button type="button" className="linkish" onClick={() => setOwnOpen(!ownOpen)}>{ownOpen ? "Use Keel's allowance" : "Use your own key"}</button>
            </span>
          </label>}
          {ownOpen && !ctx.self && (
            <div className="subcard stack-s">
              <label className="field small">Provider
                <select value={own?.provider || "openai"} onChange={(e) => setOwn({ provider: e.target.value as OwnKey["provider"], key: own?.key || "" })}>
                  <option value="openai">openai</option><option value="anthropic">anthropic</option>
                </select>
              </label>
              <label className="field small">API key
                <input type="password" className="mono" autoComplete="off" value={own?.key || ""}
                  onChange={(e) => setOwn({ provider: own?.provider || "openai", key: e.target.value })} />
              </label>
              <span className="hint">Kept in this tab only and sent with each message; never stored by Keel.</span>
            </div>
          )}
          {agents.length > 0 && (
            <label className="field">Agent identity <span className="hint inline">from the cartridge's agents</span>
              <select className="mono" value={identity} onChange={(e) => setIdentity(e.target.value)}>
                {ctx.self && <option value="">none</option>}
                {agents.map((a) => <option key={a}>{a}</option>)}
              </select>
            </label>
          )}
          <label className="field">
            <span className="row between">
              <span>System prompt <span className="hint inline">starter from the {ctx.cartridge?.name || ""} cartridge</span></span>
              {system !== starter && <button type="button" className="linkish small-text" onClick={() => setSystem(starter)}>Reset</button>}
            </span>
            <textarea className="mono" rows={12} value={system} onChange={(e) => setSystem(e.target.value)} />
          </label>
          <fieldset className="tools-set">
            <legend>Tools</legend>
            {TOOLS.map((t) => (
              <label key={t} className="check mono">
                <input type="checkbox" checked={tools.includes(t)}
                  onChange={(e) => setTools(e.target.checked ? TOOLS.filter((x) => x === t || tools.includes(x)) : tools.filter((x) => x !== t))} /> {t}
              </label>
            ))}
          </fieldset>
        </form>
        {ctx.self ? (
          <section className="card empty" aria-label="Chat">
            <h2 className="h2">Chatting needs a model</h2>
            <p className="muted">Chatting with an agent needs a model; export it and run it with your own key. The console never calls a model from your browser.</p>
            <button type="button" className="btn primary" onClick={() => setExporting(true)}><Icon name="download" size={14} /> Export agent</button>
          </section>
        ) : <section className="card chat" aria-label="Chat">
          <div className="chat-head">
            <b>{name}</b>
            <span className="muted mono small-text">tenant {ctx.tenant}{identity ? ` · agent ${identity}` : ""}</span>
            {turns.length > 0 && <button type="button" className="btn ghost sm" onClick={() => { setHistory([]); setTurns([]); }}>Clear</button>}
          </div>
          <div className="chat-body" ref={scroller}>
            {turns.length === 0 && <p className="muted small-text">Ask something about this tenant's data. Every tool call shows up here.</p>}
            {turns.map((t, i) => (
              <div key={i} className={`turn ${t.role}`}>
                {t.trace && t.trace.length > 0 && <Trace rows={t.trace} />}
                {t.text && <div className="bubble">{t.role === "assistant" ? <Markdown text={t.text} /> : t.text}</div>}
              </div>
            ))}
            {busy && <div className="turn assistant"><div className="bubble muted"><span className="spin" /> thinking…</div></div>}
          </div>
          <form className="chat-input" onSubmit={send}>
            <input aria-label="Message" value={input} onChange={(e) => setInput(e.target.value)}
              placeholder={ctx.cartridge?.example ? "Is any tire on unit MXK-4821 critical?" : "Ask about your data…"} />
            <button className="btn primary" disabled={busy || !input.trim() || !model}><Icon name="send" size={14} /> Send</button>
          </form>
        </section>}
      </div>
    </div>
  );
}

function Trace({ rows }: { rows: TraceRow[] }) {
  return (
    <details className="trace">
      <summary>{rows.length} tool call{rows.length === 1 ? "" : "s"}{rows.some((r) => !r.ok) ? " · with errors" : ""}</summary>
      {rows.map((r, i) => (
        <details key={i} className="trace-row">
          <summary className="mono">
            <span className={r.ok ? "ok" : "warn"}>{r.ok ? "ok" : "error"}</span> {r.tool}({excerpt(r.arguments, 80)})
          </summary>
          <pre>{JSON.stringify(r.arguments, null, 2)}</pre>
          <pre>{excerpt(r.result, 2000)}</pre>
        </details>
      ))}
    </details>
  );
}

function ExportPanel({ spec, onClose }: { spec: Parameters<typeof exportAgent>[1]; onClose: () => void }) {
  const [fw, setFw] = useState<Framework>("claude");
  const meta = FRAMEWORKS.find((f) => f.id === fw)!;
  const code = exportAgent(fw, spec);
  return (
    <section className="card export" aria-label="Export agent">
      <div className="row wrap between">
        <Tabs label="Framework" items={FRAMEWORKS.map((f) => ({ id: f.id, name: f.name }))} value={fw} onChange={setFw} />
        <div className="row">
          <button type="button" className="btn sm" onClick={() => download(meta.file, code)}><Icon name="download" size={14} /> {meta.file}</button>
          <CopyButton text={code} />
          <button type="button" className="btn ghost sm" aria-label="Close export" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
      </div>
      <pre className="code-pre">{code}</pre>
      <p className="hint">The prompt, identity, tools and tenant travel with the file. Set <span className="mono">KEEL_KEY</span> to a key from Connect.</p>
    </section>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
function excerpt(v: unknown, n: number): string {
  const s = typeof v === "string" ? v : JSON.stringify(v) ?? "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}
