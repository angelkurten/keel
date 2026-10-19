import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Cdc as CdcData, type Cartridge } from "../api";
import { ago, Code, Icon } from "../ui";

const DROP_SLOT = "SELECT pg_drop_replication_slot('keel');";

/** A starting map: one commented line per kind of the cartridge. */
function mapTemplate(c: Cartridge | null): string {
  const kinds = Object.keys(c?.entities || {});
  return [
    "# One entry per table: which kind its rows are, the id and tenant columns, and the columns to carry.",
    "# Each row change becomes <kind>.created, <kind>.updated or <kind>.deleted, which the cartridge must declare.",
    "# Uncomment and adapt (the table name is the key):",
    ...(kinds.length ? kinds : ["thing"]).map((k) => `# ${k}s: {kind: ${k}, id: id, tenant: tenant_id, columns: [name]}`),
    "",
  ].join("\n");
}

export function Cdc({ ctx }: { ctx: Ctx }) {
  const [data, setData] = useState<CdcData | null>(null);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let stop = false, timer = 0;
    const poll = async () => {
      if (document.visibilityState === "visible") {
        try { const d = await api.cdc(); if (!stop) { setData(d); setErr(""); } }
        catch (e) { if (!stop) setErr((e as Error).message); }
      }
      if (!stop) timer = window.setTimeout(poll, 5000);
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, []);

  if (!data) return err ? <p className="err">{err}</p> : <p className="muted">Loading…</p>;
  if (!data.configured || editing) {
    return <Setup ctx={ctx} initialMap={data.configured ? data.map : mapTemplate(ctx.cartridge)} editing={editing}
      onSaved={(d) => { setData(d); setEditing(false); }} onCancel={editing ? () => setEditing(false) : undefined} />;
  }
  return <Following data={data} err={err} onEdit={() => setEditing(true)} onStopped={() => setData({ ...data, configured: false, status: null })} />;
}

function Setup({ ctx, initialMap, editing, onSaved, onCancel }: {
  ctx: Ctx; initialMap: string; editing: boolean; onSaved: (d: CdcData) => void; onCancel?: () => void;
}) {
  const [dsn, setDsn] = useState("");
  const [map, setMap] = useState(initialMap);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr("");
    try { onSaved(await api.setCdc({ dsn: dsn.trim(), map })); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <>
      <div className="card stack-s">
        <b className="row"><Icon name="db" size={16} /> What Keel needs from your database</b>
        <ul className="reqs small-text">
          <li>Postgres 10 or later with <span className="mono">wal_level = logical</span>.</li>
          <li>A user with <span className="mono">REPLICATION</span> and <span className="mono">SELECT</span> on the tables.</li>
          <li>The database reachable from the internet.</li>
          <li>Keel creates the publication and the replication slot <span className="mono">keel</span>, and sets <span className="mono">REPLICA IDENTITY FULL</span> on tables with a tenant column.</li>
        </ul>
      </div>
      <form className="card form" onSubmit={save}>
        <label className="field">Connection string <span className="hint">{editing ? "enter it again: Keel never shows it back" : "Keel never shows it back"}</span>
          <input type="password" className="mono" required autoComplete="off" value={dsn} onChange={(e) => setDsn(e.target.value)}
            placeholder="postgresql://user:pass@host:5432/db" />
        </label>
        <label className="field">Table map <span className="hint">YAML</span>
          <textarea className="mono" rows={10} spellCheck={false} value={map} onChange={(e) => setMap(e.target.value)} />
          <span className="hint">
            Each row change becomes <span className="mono">&lt;kind&gt;.created|updated|deleted</span>; the cartridge{ctx.cartridge ? <> (<span className="mono">{ctx.cartridge.name}</span>)</> : null} must declare those event types.
          </span>
        </label>
        {err && <p className="err">{err}</p>}
        <div className="row">
          <button className="btn primary" disabled={busy || !dsn.trim()}>{busy ? "Saving…" : "Save and start following"}</button>
          {onCancel && <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>}
        </div>
      </form>
    </>
  );
}

function Following({ data, err, onEdit, onStopped }: { data: CdcData; err: string; onEdit: () => void; onStopped: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stopErr, setStopErr] = useState("");
  const s = data.status;
  const tables = s?.tables?.length ? s.tables : data.tables;

  async function stop() {
    setBusy(true); setStopErr("");
    try { await api.stopCdc(); onStopped(); }
    catch (e) { setStopErr((e as Error).message); setBusy(false); }
  }

  return (
    <>
      <div className={`card cdc-status ${s?.ok === true ? "good" : s?.ok === false ? "bad" : ""}`}>
        {s?.ok === true && (
          <span className="ok row"><span className="pulse" /> Following {tables.length} table{tables.length === 1 ? "" : "s"} · {s.events ?? 0} events{s.last_read ? ` · last read ${ago(s.last_read)}` : ""}</span>
        )}
        {s?.ok === false && (
          <div className="stack-s">
            <span className="err row"><Icon name="warn" size={14} /> Not following{s.failed_at ? ` since ${ago(s.failed_at)}` : ""}</span>
            {s.error && <pre className="code-pre err">{s.error}</pre>}
          </div>
        )}
        {(!s || s.ok === null) && <span className="muted row"><span className="spin" /> Starting…</span>}
        {tables.length > 0 && <p className="hint mono">{tables.join(" · ")}</p>}
        {err && <p className="err small-text">{err}</p>}
      </div>
      <Code title="Table map" code={data.map} extra={<button type="button" className="btn sm" onClick={onEdit}>Edit</button>} />
      <div className="card stack-s">
        {confirm ? (
          <div className="row wrap">
            <span className="warn small-text">Stop following this database?</span>
            <button type="button" className="btn sm danger" disabled={busy} onClick={stop}>{busy ? "Stopping…" : "Stop following"}</button>
            <button type="button" className="btn sm ghost" onClick={() => setConfirm(false)}>Cancel</button>
          </div>
        ) : (
          <button type="button" className="btn" onClick={() => setConfirm(true)}>Stop following</button>
        )}
        {stopErr && <p className="err">{stopErr}</p>}
        <p className="hint">A replication slot left behind makes your server keep WAL. To drop it for good, run this on your database:</p>
        <Code code={DROP_SLOT} />
      </div>
    </>
  );
}
