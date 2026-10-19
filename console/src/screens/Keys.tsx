import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Key, type KeyRow } from "../api";
import { bareTenant, KeyReveal } from "../ui";

export function Keys({ ctx }: { ctx: Ctx }) {
  const account = ctx.me.account;
  const agents = Object.keys(ctx.cartridge?.agents || {});
  const [rows, setRows] = useState<KeyRow[] | null>(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState(false);
  const [tenant, setTenant] = useState(ctx.tenant);
  const [agent, setAgent] = useState("");
  const [global, setGlobal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<Key | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

  const load = () => api.keys().then(setRows).catch((e) => setErr((e as Error).message));
  useEffect(() => { load(); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const k = await api.createKey({ ...(tenant.trim() ? { tenant: tenant.trim() } : {}), ...(agent ? { agent } : {}), ...(global ? { global: true } : {}) });
      setCreated(k); setForm(false); ctx.keyCreated(); load();
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  async function revoke(id: string) {
    setErr("");
    try { await api.revokeKey(id); setConfirm(null); load(); }
    catch (e) { setErr((e as Error).message); }
  }

  const scope = (k: KeyRow) => {
    const parts: string[] = [];
    if (k.tenant) parts.push(`tenant ${bareTenant(account, k.tenant)}`);
    if (k.agent) parts.push(`agent ${k.agent}`);
    if (k.global) parts.push("global");
    return parts.length ? parts.join(" · ") : "account";
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="h1">API keys</h1>
          <p className="muted">A key belongs to <span className="mono fg">{account}</span> and can be narrowed to one tenant or one agent. Only its hash is stored.</p>
        </div>
        <button type="button" className="btn primary" onClick={() => { setForm(!form); setCreated(null); }}>New key</button>
      </div>

      {form && (
        <form className="card keyform" onSubmit={create}>
          <label className="field">Tenant <span className="hint">optional · empty means every tenant of the account</span>
            <input className="mono" value={tenant} spellCheck={false} onChange={(e) => setTenant(e.target.value)} placeholder="any tenant" />
          </label>
          {agents.length > 0 && (
            <label className="field">Agent <span className="hint">optional</span>
              <select className="mono" value={agent} onChange={(e) => setAgent(e.target.value)}>
                <option value="">any agent</option>
                {agents.map((a) => <option key={a}>{a}</option>)}
              </select>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={global} onChange={(e) => setGlobal(e.target.checked)} /> Global
            <span className="hint">may write the account's global memory</span>
          </label>
          <div className="row">
            <button className="btn primary" disabled={busy}>{busy ? "Creating…" : "Create key"}</button>
            <button type="button" className="btn ghost" onClick={() => setForm(false)}>Cancel</button>
          </div>
        </form>
      )}

      {created && (
        <KeyReveal value={created.key} title={<>Key created · {created.tenant ? `tenant ${bareTenant(account, created.tenant)}` : "account"}{agent ? ` · agent ${agent}` : ""}</>} />
      )}
      {err && <p className="err">{err}</p>}

      <div className="card">
        {!rows ? <p className="muted">Loading…</p> : rows.length === 0 ? <p className="muted">No keys yet. Create one here or in Connect.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>id</th><th>scope</th><th>created</th><th>status</th><th><span className="sr-only">actions</span></th></tr></thead>
            <tbody>
              {rows.map((k) => (
                <tr key={k.id} className={k.revoked_at ? "revoked" : ""}>
                  <td className="mono">{k.id}</td>
                  <td><span className="tag">{scope(k)}</span></td>
                  <td className="mono nowrap">{k.created_at.slice(0, 10)}</td>
                  <td>{k.revoked_at ? <span className="badge muted">revoked {k.revoked_at.slice(0, 10)}</span> : <span className="badge ok">active</span>}</td>
                  <td className="actions">
                    {!k.revoked_at && (confirm === k.id ? (
                      <span className="row">
                        <span className="small-text warn">Revoke?</span>
                        <button type="button" className="btn sm danger" onClick={() => revoke(k.id)}>Revoke</button>
                        <button type="button" className="btn sm ghost" onClick={() => setConfirm(null)}>Cancel</button>
                      </span>
                    ) : (
                      <button type="button" className="btn sm" onClick={() => setConfirm(k.id)}>Revoke</button>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
      <p className="hint">Lost a key? Revoke it and create another; agents pick up the new one on their next call.</p>
    </div>
  );
}
