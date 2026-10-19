import { useEffect, useRef, useState } from "react";
import type { Ctx } from "../App";
import { api } from "../api";
import { createGraph, type RelatedRow } from "../graphView";
import { day, Icon, Link, show } from "../ui";

type Fact = { predicate: string; value: unknown; valid_from?: string; valid_to?: string | null; recorded_at?: string;
  scope?: string; source?: string; corrected_by?: string | null; evidence?: unknown };
type Context = { entity?: { kind?: string; ref?: string; attrs?: Record<string, unknown> }; facts?: Fact[]; evaluation?: Record<string, unknown> };
type Hit = { key: string; kind?: string; attrs?: Record<string, unknown> };

export function Graph({ ctx }: { ctx: Ctx }) {
  const { tenant, cartridge } = ctx;
  const [q, setQ] = useState("");
  const [asOf, setAsOf] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [serr, setSerr] = useState("");
  const [subject, setSubject] = useState<string | null>(null);
  const [cx, setCx] = useState<Context | null>(null);
  const [cerr, setCerr] = useState("");
  const [hist, setHist] = useState<{ predicate: string; rows: Fact[] | null; err?: string } | null>(null);
  const graphHost = useRef<HTMLDivElement>(null);
  const graph = useRef<ReturnType<typeof createGraph> | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const live = useRef({ tenant, asOf, agent: ctx.agent });
  live.current = { tenant, asOf, agent: ctx.agent };

  const iso = (v: string) => (v ? new Date(v).toISOString() : null);
  const call = <T,>(tool: "context" | "history" | "search" | "related", body: Record<string, unknown>) =>
    api.tool<T>(tool, body, live.current.tenant, live.current.agent);

  async function open(s: string, fromGraph = false) {
    setSubject(s); setHist(null); setCerr("");
    if (!fromGraph) graph.current?.show(s);
    try { setCx(await call<Context>("context", { subject: s, as_of: iso(live.current.asOf) })); }
    catch (e) { setCx(null); setCerr((e as Error).message); }
  }
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    if (!graphHost.current) return;
    graph.current = createGraph(graphHost.current, {
      related: (s, depth) => call<RelatedRow[]>("related", { subject: s, depth, as_of: iso(live.current.asOf) }),
      onOpen: (s) => openRef.current(s, true),
    });
    return () => graph.current?.destroy();
  }, [subject !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  // Show the first subject once the graph host exists.
  useEffect(() => { if (subject && graph.current && !graph.current.has(subject)) graph.current.show(subject); }, [subject !== null]); // eslint-disable-line

  // Opening the tab, a new tenant or as-of lists the tenant's subjects and reloads what is open: nothing to type first.
  useEffect(() => {
    setSerr("");
    call<Hit[]>("search", { text: "", limit: 50, as_of: iso(asOf) }).then(setHits).catch((err) => { setHits(null); setSerr((err as Error).message); });
    if (subject) open(subject);
  }, [tenant, asOf, ctx.agent]); // eslint-disable-line

  // "/" focuses search.
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key === "/" && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) {
        e.preventDefault(); searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    const text = q.trim();
    setSerr("");
    if (/^[\w.-]+:\S+$/.test(text)) return open(text);
    try { setHits(await call<Hit[]>("search", { text, limit: 50, as_of: iso(asOf) })); }
    catch (err) { setHits(null); setSerr((err as Error).message); }
  }

  async function history(predicate: string) {
    if (!subject) return;
    setHist({ predicate, rows: null });
    try { setHist({ predicate, rows: await call<Fact[]>("history", { subject, predicate }) }); }
    catch (e) { setHist({ predicate, rows: [], err: (e as Error).message }); }
  }

  const e = cx?.entity;
  return (
    <div className="stack">
      <div className="toolbar">
        <form className="searchbox" onSubmit={search}>
          <Icon name="search" />
          <input ref={searchRef} aria-label="Search subjects" value={q} onChange={(ev) => setQ(ev.target.value)}
            placeholder="Search subjects… text, or a key like unit:MXK-4821" />
          <kbd>/</kbd>
        </form>
        <label className="asof muted">as of
          <input type="datetime-local" value={asOf} onChange={(ev) => setAsOf(ev.target.value)} />
        </label>
      </div>
      {cartridge?.example && (
        <div className="banner warn-banner">
          <span>You're looking at example data ({cartridge.name}). Replace it with your own whenever you're ready.</span>
        </div>
      )}
      <div className="graph-layout">
        <section className="card results" aria-label="Results">
          <div className="eyebrow">{hits ? `${hits.length} subjects · tenant ${tenant}` : `tenant ${tenant}`}</div>
          {serr && <p className="err">{serr}</p>}
          {!hits && !serr && <p className="muted small-text">Search, or press Enter on an empty box to list everything.</p>}
          {hits?.length === 0 && <p className="muted">No match.</p>}
          <ul className="hits">
            {hits?.map((h) => (
              <li key={h.key}>
                <button type="button" className={h.key === subject ? "hit on" : "hit"} onClick={() => open(h.key)}>
                  <span className="mono">{h.key}</span>
                  <span className="muted ellipsis">{h.attrs ? Object.values(h.attrs).map(show).join(" · ") : ""}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        <section className="detail">
          {!subject ? (
            <div className="card empty">
              <h2 className="h2">Pick a subject</h2>
              <p className="muted">Search on the left, or type a subject key{cartridge?.example ? <> such as <button type="button" className="linkish mono" onClick={() => open("unit:MXK-4821")}>unit:MXK-4821</button></> : null}.</p>
            </div>
          ) : (
            <>
              <div className="card">
                <div className="subject-head">
                  <div className="row wrap">
                    <h1 className="h1 mono">{subject}</h1>
                    {e?.kind && <span className="tag">{e.kind}</span>}
                    {Object.entries(cx?.evaluation || {}).map(([k, v]) => (
                      <span key={k} className={v === true ? "tag hot" : "tag"}>{k}: {show(v)}</span>
                    ))}
                  </div>
                  <Link to="/console/playground" className="small-text">Ask with these tools <Icon name="arrow" size={12} /></Link>
                </div>
                {cerr && <p className="err">{cerr}</p>}
                {e?.ref && <div className="muted mono small-text">ref {e.ref}</div>}
                <div className="row wrap">
                  {Object.entries(e?.attrs || {}).map(([k, v]) => <span key={k} className="tag">{k}: {show(v)}</span>)}
                </div>
              </div>
              <div className="card"><div className="eyebrow">Graph</div><div ref={graphHost} className="g-host" /></div>
              <div className="card">
                <div className="eyebrow">Facts · current</div>
                {!cx?.facts?.length ? <p className="muted">No current facts.</p> : (
                  <div className="table-wrap"><table>
                    <thead><tr><th>predicate</th><th>value</th><th>since</th><th>scope</th><th>source</th></tr></thead>
                    <tbody>
                      {cx.facts.map((f, i) => (
                        <tr key={i}>
                          <td><button type="button" className="linkish mono" onClick={() => history(f.predicate)}>{f.predicate}</button></td>
                          <td className="mono">{show(f.value)}</td><td className="mono">{day(f.valid_from)}</td>
                          <td>{f.scope || ""}</td><td>{f.corrected_by ? `${f.source} (by ${f.corrected_by})` : f.source || ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table></div>
                )}
              </div>
              <div className="card">
                <div className="eyebrow">History{hist ? ` · ${hist.predicate}` : ""}</div>
                {!hist ? <p className="muted">Click a predicate to see its history.</p>
                  : hist.err ? <p className="err">{hist.err}</p>
                  : !hist.rows ? <p className="muted">Loading…</p> : (
                    <div className="table-wrap"><table>
                      <thead><tr><th>value</th><th>valid from</th><th>valid to</th><th>recorded</th><th>source</th><th>evidence</th></tr></thead>
                      <tbody>
                        {hist.rows.map((f, i) => (
                          <tr key={i}>
                            <td className="mono">{show(f.value)}</td><td className="mono">{day(f.valid_from)}</td>
                            <td className="mono">{day(f.valid_to) || "now"}</td><td className="mono">{day(f.recorded_at)}</td>
                            <td>{f.source || ""}</td><td className="muted mono">{show(f.evidence)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table></div>
                  )}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
