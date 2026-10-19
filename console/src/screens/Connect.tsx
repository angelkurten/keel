import { useState } from "react";
import type { Ctx } from "../App";
import { api, type Key } from "../api";
import { CLIENTS, connectSnippet, cursorDeeplink, type Client } from "../codegen";
import { Code, CopyButton, Icon, Tabs } from "../ui";

const PLACEHOLDER = "<your key>";

export function Connect({ ctx }: { ctx: Ctx }) {
  const [key, setKey] = useState<Key | null>(null);
  const [client, setClient] = useState<Client>("Claude Code");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const live = key && key.tenant.split("/").pop() === ctx.tenant ? key : null; // a key made for another tenant does not apply
  const endpoint = live?.endpoint || ctx.me.endpoint;
  const secret = live?.key || PLACEHOLDER;
  const snip = connectSnippet(client, endpoint, secret, ctx.tenant);

  async function create() {
    setBusy(true); setErr("");
    try { setKey(await api.createKey({ tenant: ctx.tenant })); ctx.keyCreated(); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <div className="stack">
      <div>
        <h1 className="h1">Connect an agent</h1>
        <p className="muted">Everything below is already filled in. Copy, paste, ask.</p>
      </div>
      <ol className="strip">
        <li><span className="num">1</span><span>Tenant <span className="mono fg">{ctx.tenant}</span> <span className="hint inline">change it in the sidebar</span></span></li>
        <li><span className="num">2</span><span>Copy the snippet for your client</span></li>
        <li><span className="num">3</span><span>Ask your agent <span className="mono fg">"{ctx.cartridge?.example ? "Is any tire on unit MXK-4821 critical?" : "What do you know about …?"}"</span></span></li>
      </ol>

      <section className="card stack-s">
        <div className="row wrap between">
          <div>
            <b>Key for tenant <span className="mono">{ctx.tenant}</span></b>
            <p className="muted small-text">Narrowed to this tenant: it cannot read any other.</p>
          </div>
          <button type="button" className="btn primary" onClick={create} disabled={busy}>
            {busy ? "Creating…" : live ? "Create another key" : "Create a key for this tenant"}
          </button>
        </div>
        {err && <p className="err">{err}</p>}
        {live && (
          <div className="keybox">
            <div className="row between wrap">
              <code className="mono key">{live.key}</code>
              <CopyButton text={live.key} />
            </div>
            <p className="warn small-text"><Icon name="warn" size={14} /> Shown once. Copy it now: Keel keeps only its hash.</p>
          </div>
        )}
      </section>

      <section className="card stack-s">
        <Tabs label="Client" items={CLIENTS} value={client} onChange={setClient} />
        {snip.note && <p className="muted small-text">{snip.note}</p>}
        <Code title={snip.title} code={snip.code} />
        {client === "Cursor" && (
          <div className="row wrap between subcard">
            <div><b>Using Cursor?</b><p className="muted small-text">One click installs the server with this key and tenant.</p></div>
            {live ? <a className="btn" href={cursorDeeplink(endpoint, live.key, ctx.tenant)}>Add to Cursor</a>
              : <span className="hint">Create a key first.</span>}
          </div>
        )}
        <p className="hint">MCP endpoint <span className="mono">{endpoint.replace(/\/$/, "")}/mcp</span></p>
      </section>
    </div>
  );
}
