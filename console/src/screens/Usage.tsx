import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Usage as UsageData } from "../api";
import { bareTenant, thisMonth } from "../ui";

const fmt = (n: number) => n.toLocaleString("en-US");
const sum = (o: Record<string, number>) => Object.values(o).reduce((a, b) => a + b, 0);

export function Usage({ ctx }: { ctx: Ctx }) {
  const [data, setData] = useState<UsageData | null>(null);
  const [err, setErr] = useState("");
  const [month, setMonth] = useState(thisMonth());

  useEffect(() => { api.usage().then(setData).catch((e) => setErr((e as Error).message)); }, []);

  const months = [...new Set([thisMonth(), ...(data?.rows.map((r) => r.month) || [])])].sort().reverse();
  const rows = (data?.rows || []).filter((r) => r.month === month);
  const events = rows.reduce((a, r) => a + r.events, 0);
  const byTool: Record<string, number> = {};
  for (const r of rows) for (const [t, n] of Object.entries(r.calls || {})) byTool[t] = (byTool[t] || 0) + n;
  const calls = sum(byTool);
  const limit = data?.limits.events_month ?? null;
  const pct = limit ? Math.min(100, (events / limit) * 100) : 0;
  const topTool = Math.max(1, ...Object.values(byTool));

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="h1">Usage</h1>
          <p className="muted">Events are what counts toward the plan. Tool calls are shown, not billed.</p>
        </div>
        <label className="field small month">Month
          <select className="mono" value={month} onChange={(e) => setMonth(e.target.value)}>
            {months.map((m) => <option key={m}>{m}</option>)}
          </select>
        </label>
      </div>
      {err && <p className="err">{err}</p>}
      {!data && !err && <p className="muted">Loading…</p>}
      {data && (
        <>
          <div className="cards3">
            <div className="card metric">
              <span className="hint">Events ingested</span>
              <span className="metric-n">{fmt(events)}{limit !== null && <span className="muted metric-of"> / {fmt(limit)}</span>}</span>
              {limit !== null && <div className="bar"><div className={pct > 80 ? "hot" : ""} style={{ width: `${pct}%` }} /></div>}
              {limit === null && <span className="hint">no monthly limit</span>}
            </div>
            <div className="card metric">
              <span className="hint">Tool calls</span>
              <span className="metric-n">{fmt(calls)}</span>
              <span className="hint">from agents, the playground, the graph and curl</span>
            </div>
            <div className="card metric">
              <span className="hint">Rate limit</span>
              <span className="metric-n">{data.limits.rps === null ? "none" : <>{fmt(data.limits.rps)} <span className="muted metric-of">req/s</span></>}</span>
              <span className="hint">over it, calls get 429 with Retry-After</span>
            </div>
          </div>
          <div className="usage-layout">
            <div className="card">
              <div className="eyebrow">By tenant</div>
              {rows.length === 0 ? <p className="muted small-text">Nothing recorded in {month}.</p> : (
                <div className="table-wrap"><table>
                  <thead><tr><th>tenant</th><th className="num-col">events</th><th className="num-col">tool calls</th></tr></thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.tenant}>
                        <td className="mono">{bareTenant(ctx.me.account, r.tenant)}</td>
                        <td className="mono num-col">{fmt(r.events)}</td>
                        <td className="mono num-col">{fmt(sum(r.calls || {}))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table></div>
              )}
            </div>
            <div className="card">
              <div className="eyebrow">By tool</div>
              {calls === 0 ? <p className="muted small-text">No tool calls in {month}.</p> : (
                <div className="toolbars">
                  {Object.entries(byTool).sort((a, b) => b[1] - a[1]).map(([t, n]) => (
                    <div key={t} className="toolbar-row">
                      <span className="mono">{t}</span>
                      <div className="bar"><div style={{ width: `${(n / topTool) * 100}%` }} className="accent-bar" /></div>
                      <span className="mono num-col">{fmt(n)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
          <p className="hint">Calls from the playground and the graph count as tool calls too.</p>
        </>
      )}
    </div>
  );
}
