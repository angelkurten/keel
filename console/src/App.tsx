import { useEffect, useState } from "react";
import { api, ApiError, type Cartridge, type Me } from "./api";
import { bareTenant, Icon, Link, navigate, store, thisMonth, usePath } from "./ui";
import { Landing } from "./screens/Landing";
import { Starting } from "./screens/Starting";
import { Graph } from "./screens/Graph";
import { Playground } from "./screens/Playground";
import { Connect } from "./screens/Connect";
import { Events } from "./screens/Events";
import { Sdk } from "./screens/Sdk";
import { Keys } from "./screens/Keys";
import { Usage } from "./screens/Usage";

export function App() {
  const path = usePath();
  if (path === "/console" || path.startsWith("/console/")) return <Console path={path} />;
  return <Landing />;
}

/** What every console tab gets: the account, the cartridge and the tenant picked in the sidebar. */
export type Ctx = { me: Me; cartridge: Cartridge | null; tenant: string; keyCreated: () => void; eventsSent: () => void };

const TABS = [
  { id: "graph", label: "Graph", icon: "graph" },
  { id: "playground", label: "Playground", icon: "play" },
  { id: "connect", label: "Connect", icon: "plug" },
  { id: "events", label: "Events", icon: "inbox" },
  { id: "sdk", label: "SDK", icon: "code" },
  { id: "keys", label: "Keys", icon: "key" },
  { id: "usage", label: "Usage", icon: "chart" },
] as const;

function Console({ path }: { path: string }) {
  const [me, setMe] = useState<Me | null>(null);
  const [cartridge, setCartridge] = useState<Cartridge | null>(null);
  const [cartLoaded, setCartLoaded] = useState(false); // tabs build their defaults from the cartridge on mount
  const [tenant, setTenant] = useState(() => store.get("local", "keel.tenant") || "demo");
  const [keyed, setKeyed] = useState(() => store.get("local", "keel.keyCreated") === "1");
  const [ownEvents, setOwnEvents] = useState(false);
  const [usageTick, setUsageTick] = useState(0);
  const [menu, setMenu] = useState(false);
  const [err, setErr] = useState("");

  // Poll /api/me every 2s until the account is ready (or failed).
  useEffect(() => {
    let stop = false, timer = 0;
    const poll = async () => {
      try {
        const m = await api.me();
        if (stop) return;
        setMe(m);
        if (m.status !== "ready" && m.status !== "failed") timer = window.setTimeout(poll, 2000);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return navigate("/", true);
        if (!stop) { setErr((e as Error).message); timer = window.setTimeout(poll, 4000); }
      }
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, []);

  const ready = me?.status === "ready";
  useEffect(() => {
    if (ready) api.cartridge().then(setCartridge).catch((e) => setErr((e as Error).message)).finally(() => setCartLoaded(true));
  }, [ready]);

  useEffect(() => { setMenu(false); }, [path]);

  // "Send your own events": any tenant but the example one has events this month.
  useEffect(() => {
    if (!ready || ownEvents || !me) return;
    api.usage(thisMonth())
      .then((u) => setOwnEvents(u.rows.some((r) => r.month === thisMonth() && r.events > 0 && bareTenant(me.account, r.tenant) !== "demo")))
      .catch(() => { /* the checklist just stays open */ });
  }, [ready, path, usageTick]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!me) return <div className="center muted">{err || "Loading…"}</div>;
  if (!ready) return <Starting me={me} />;
  if (!cartLoaded) return <div className="center muted">Loading…</div>;

  const tab = path.split("/")[2] || "graph";
  if (!TABS.some((t) => t.id === tab)) { navigate("/console/graph", true); return null; }
  const ctx: Ctx = {
    me, cartridge, tenant: tenant.trim() || "demo",
    keyCreated: () => { setKeyed(true); store.set("local", "keel.keyCreated", "1"); },
    eventsSent: () => setUsageTick((n) => n + 1),
  };
  const steps: [string, boolean, string | null][] = [
    ["Keel running", true, "/console/graph"],
    ["Example data", !!cartridge?.example, "/console/graph"],
    ["Connect an agent", keyed, "/console/connect"],
    ["Send your own events", ownEvents, "/console/events"],
    ["Your cartridge", false, null],
  ];
  const done = steps.filter((s) => s[1]).length;

  return (
    <div className="shell">
      <nav className={menu ? "side open" : "side"} aria-label="Console">
        <div className="side-top">
          <span className="brand"><Icon name="logo" size={18} className="accent" /> keel</span>
          <button type="button" className="btn ghost menu-btn" aria-label="Menu" aria-expanded={menu} onClick={() => setMenu(!menu)}>
            <Icon name={menu ? "x" : "menu"} />
          </button>
        </div>
        <div className="side-body">
          <div className="stack-s">
            <div className="account mono" title="Account">{me.account}</div>
            <label className="field small">Tenant
              <input className="mono" value={tenant} spellCheck={false}
                onChange={(e) => { setTenant(e.target.value); store.set("local", "keel.tenant", e.target.value); }}
                onBlur={() => { if (!tenant.trim()) setTenant("demo"); }} />
            </label>
          </div>
          <div className="nav">
            {TABS.map((t) => (
              <Link key={t.id} to={`/console/${t.id}`} className={t.id === tab ? "on" : ""}>
                <Icon name={t.icon} /> {t.label}
              </Link>
            ))}
          </div>
          <div className="checklist">
            <span className="eyebrow">Get started · {done}/{steps.length}</span>
            <div className="bar"><div style={{ width: `${(done / steps.length) * 100}%` }} /></div>
            {steps.map(([label, ok, to]) => {
              const body = (<><span className={ok ? "dot ok" : "dot"}>{ok && <Icon name="check" size={10} />}</span>
                <span>{label}</span>{!to && <span className="soon">coming soon</span>}</>);
              return to ? <Link key={label} to={to} className={ok ? "step done" : "step"}>{body}</Link>
                : <span key={label} className="step later">{body}</span>;
            })}
          </div>
          <div className="side-foot">
            <span className="muted ellipsis">{me.email}</span>
            <button type="button" className="btn ghost sm" onClick={async () => {
              try { await api.signout(); } catch { /* signed out anyway */ }
              navigate("/");
            }}><Icon name="out" size={14} /> Sign out</button>
          </div>
        </div>
      </nav>
      <main className="main">
        {err && <p className="err">{err}</p>}
        {tab === "graph" && <Graph ctx={ctx} />}
        {tab === "playground" && <Playground ctx={ctx} />}
        {tab === "connect" && <Connect ctx={ctx} />}
        {tab === "events" && <Events ctx={ctx} />}
        {tab === "sdk" && <Sdk ctx={ctx} />}
        {tab === "keys" && <Keys ctx={ctx} />}
        {tab === "usage" && <Usage ctx={ctx} />}
      </main>
    </div>
  );
}
