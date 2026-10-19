import { useState } from "react";
import { api, ApiError, env, type Cartridge } from "../api";
import { Icon, store } from "../ui";

export const SECRET_KEY = "keel.secret";

/** Self-hosted (`keel serve --ui`): the secret or an API key, once per browser tab, and the tenant to look at. */
export function SelfLanding({ onConnected }: { onConnected: () => void }) {
  const [secret, setSecret] = useState("");
  const [reveal, setReveal] = useState(false);
  const [tenant, setTenant] = useState(() => store.get("local", "keel.tenant") || "");
  const [agent, setAgent] = useState(() => store.get("local", "keel.agent") || "");
  const [cartridge, setCartridge] = useState<Cartridge | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const agents = Object.keys(cartridge?.agents || {});

  function keep() {
    env.secret = secret;
    store.set("session", SECRET_KEY, secret);
    store.set("local", "keel.tenant", tenant.trim());
    store.set("local", "keel.agent", agent);
    onConnected();
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (cartridge) return keep(); // second step: the agent was picked
    setBusy(true); setErr("");
    try {
      const c = await api.cartridgeWith(secret);
      if (Object.keys(c.agents || {}).length) setCartridge(c); // let the user pick an agent first
      else keep();
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 401 ? "Wrong secret or key." : (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <div className="entry">
      <div className="entry-col">
        <span className="brand big"><Icon name="logo" size={22} className="accent" /> keel</span>
        <div className="stack-s">
          <h1 className="hero">Connect to this Keel</h1>
          <p className="muted lead">The console talks to <span className="mono fg">{window.location.origin}</span> with your secret. It stays in this tab only.</p>
        </div>
        <form className="card form" onSubmit={submit}>
          <label className="field">Secret or API key
            <span className="input-wrap">
              <input type={reveal ? "text" : "password"} required autoComplete="off" className="mono" value={secret}
                onChange={(e) => { setSecret(e.target.value); setCartridge(null); }} placeholder="KEEL_SECRET or kk_…" />
              <button type="button" className="btn ghost sm" aria-pressed={reveal} onClick={() => setReveal(!reveal)}>{reveal ? "Hide" : "Show"}</button>
            </span>
          </label>
          <label className="field">Tenant
            <input required className="mono" spellCheck={false} value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="acme-logistics" />
            <span className="hint">The end-customer whose graph you want to see (X-Keel-Tenant).</span>
          </label>
          {agents.length > 0 && (
            <label className="field">Agent <span className="hint inline">optional · the cartridge declares agents</span>
              <select className="mono" value={agents.includes(agent) ? agent : ""} onChange={(e) => setAgent(e.target.value)}>
                <option value="">none</option>
                {agents.map((a) => <option key={a}>{a}</option>)}
              </select>
            </label>
          )}
          {err && <p className="err" role="alert">{err}</p>}
          <button className="btn primary block" disabled={busy || !secret || !tenant.trim()}>
            {busy ? "Connecting…" : cartridge ? "Open the console" : "Connect"}
          </button>
        </form>
        <p className="muted small-text">No secret set on the server (localhost)? Any value works.</p>
      </div>
    </div>
  );
}
