import { useEffect, useState } from "react";
import { api, ApiError, detectMode, env, type Cartridge, type Me } from "./api";
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
import { CartridgeTab } from "./screens/Cartridge";
import { SelfLanding, SECRET_KEY } from "./screens/SelfLanding";

export function App() {
  const path = usePath();
  const [mode, setMode] = useState<"hosted" | "self" | null>(null);
  const [, setConnected] = useState(0); // env.secret is not state: re-render when it changes
  useEffect(() => {
    detectMode().then((m) => {
      if (m === "self") env.secret = store.get("session", SECRET_KEY) || "";
      setMode(m);
    });
  }, []);
  if (!mode) return <div className="center muted">Loading…</div>;
  const inConsole = path === "/console" || path.startsWith("/console/");
  if (mode === "self") {
    if (!env.secret) return <SelfLanding onConnected={() => { setConnected((n) => n + 1); navigate("/console/graph", true); }} />;
    if (!inConsole) { navigate("/console/graph", true); return null; }
    return <Console path={path} self />;
  }
  return inConsole ? <Console path={path} /> : <Landing />;
}

/** Self-hosted: forget the secret and go back to "Connect to this Keel". */
export function disconnect() {
  env.secret = "";
  store.set("session", SECRET_KEY, null);
  navigate("/", true);
}

/** What every console tab gets: the account, the cartridge and the tenant picked in the sidebar. */
export type Ctx = {
  me: Me; cartridge: Cartridge | null; tenant: string;
  /** Served by `keel serve --ui`: no account, keys, usage, cartridge deploys or hosted agent chat. */
  self: boolean;
  /** The cartridge agent picked in the sidebar (sent as X-Keel-Agent); undefined when the cartridge declares none. */
  agent?: string;
  keyCreated: () => void; eventsSent: () => void;
  /** Start polling /api/me again (a cartridge deploy restarts the account) and refetch the cartridge once ready. */
  refresh: () => void;
};

const SELF_TABS = ["graph", "playground", "connect", "events", "sdk"];
const TABS = [
  { id: "graph", label: "Graph", icon: "graph" },
  { id: "playground", label: "Playground", icon: "play" },
  { id: "connect", label: "Connect", icon: "plug" },
  { id: "events", label: "Events", icon: "inbox" },
  { id: "cartridge", label: "Cartridge", icon: "box" },
  { id: "sdk", label: "SDK", icon: "code" },
  { id: "keys", label: "Keys", icon: "key" },
  { id: "usage", label: "Usage", icon: "chart" },
] as const;

function Console({ path, self = false }: { path: string; self?: boolean }) {
  const [me, setMe] = useState<Me | null>(null);
  const [cartridge, setCartridge] = useState<Cartridge | null>(null);
  const [cartLoaded, setCartLoaded] = useState(false); // tabs build their defaults from the cartridge on mount
  const [tenant, setTenant] = useState(() => store.get("local", "keel.tenant") || "demo");
  const [keyed, setKeyed] = useState(false);
  const [ownEvents, setOwnEvents] = useState(false);
  const [usageTick, setUsageTick] = useState(0);
  const [agentPick, setAgentPick] = useState(() => store.get("local", "keel.agent") || "");
  const [dismissed, setDismissed] = useState(() => store.get("local", "keel.noticeDismissed") || "");
  const [pollGen, setPollGen] = useState(0);
  const [menu, setMenu] = useState(false);
  const [err, setErr] = useState("");

  // Poll /api/me every 2s until the account is ready (or failed). Self-hosted there is no account: the
  // cartridge stands in for it, and a refused secret goes back to "Connect to this Keel".
  useEffect(() => {
    if (self) {
      api.cartridge().then((c) => {
        setCartridge(c);
        setMe({ account: c.name, email: "", endpoint: window.location.origin, status: "ready", error: null, steps: [] });
        setCartLoaded(true);
      }).catch((e) => {
        if (e instanceof ApiError && e.status === 401) disconnect();
        else setErr((e as Error).message);
      });
      return;
    }
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
  }, [pollGen]);

  const ready = me?.status === "ready";
  useEffect(() => { if (me?.account) setKeyed(store.get("local", `keel.keyCreated.${me.account}`) === "1"); }, [me?.account]);
  useEffect(() => {
    if (ready && !self) api.cartridge().then(setCartridge).catch((e) => setErr((e as Error).message)).finally(() => setCartLoaded(true));
  }, [ready, pollGen]);

  useEffect(() => { setMenu(false); }, [path]);

  // "Send your own events": any tenant but the example one has events this month.
  useEffect(() => {
    if (!ready || ownEvents || !me || self) return;
    api.usage(thisMonth())
      .then((u) => setOwnEvents(u.rows.some((r) => r.month === thisMonth() && r.events > 0 && bareTenant(me.account, r.tenant) !== "demo")))
      .catch(() => { /* the checklist just stays open */ });
  }, [ready, path, usageTick]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!me) return <div className="center muted">{err || "Loading…"}</div>;
  if (!ready) return <Starting me={me} />;
  if (!cartLoaded) return <div className="center muted">Loading…</div>;

  const agents = Object.keys(cartridge?.agents || {});
  // Hosted: one of the cartridge's agents is always picked. Self-hosted the agent is optional ("" = none).
  const agent = agents.length ? (agents.includes(agentPick) ? agentPick : self ? undefined : agents[0]) : undefined;
  const tabs = self ? TABS.filter((t) => SELF_TABS.includes(t.id)) : TABS;
  const notice = me.notice && me.notice !== dismissed ? me.notice : null;
  const tab = path.split("/")[2] || "graph";
  if (!tabs.some((t) => t.id === tab)) { navigate("/console/graph", true); return null; }
  const ctx: Ctx = {
    me, cartridge, tenant: tenant.trim() || (self ? "" : "demo"), agent, self,
    refresh: () => setPollGen((n) => n + 1),
    keyCreated: () => { setKeyed(true); store.set("local", `keel.keyCreated.${me.account}`, "1"); },
    eventsSent: () => setUsageTick((n) => n + 1),
  };
  const steps: [string, boolean, string | null][] = [
    ["Keel running", true, "/console/graph"],
    ...(cartridge?.example ? [["Example data", true, "/console/graph"] as [string, boolean, string]] : []),
    ["Connect an agent", keyed, "/console/connect"],
    ["Send your own events", ownEvents, "/console/events"],
    ["Your cartridge", !!cartridge && !cartridge.example, "/console/cartridge"],
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
            <div className="account mono" title={self ? "Cartridge" : "Account"}>{me.account}</div>
            <label className="field small">Tenant
              <input className="mono" value={tenant} spellCheck={false}
                onChange={(e) => { setTenant(e.target.value); store.set("local", "keel.tenant", e.target.value); }}
                onBlur={() => { if (!tenant.trim() && !self) setTenant("demo"); }} />
            </label>
            {agents.length > 0 && (
              <label className="field small">Agent
                <select className="mono" value={agent || ""} onChange={(e) => { setAgentPick(e.target.value); store.set("local", "keel.agent", e.target.value); }}>
                  {self && <option value="">none</option>}
                  {agents.map((a) => <option key={a}>{a}</option>)}
                </select>
              </label>
            )}
          </div>
          <div className="nav">
            {tabs.map((t) => (
              <Link key={t.id} to={`/console/${t.id}`} className={t.id === tab ? "on" : ""}>
                <Icon name={t.icon} /> {t.label}
              </Link>
            ))}
          </div>
          {!self && <div className="checklist">
            <span className="eyebrow">Get started · {done}/{steps.length}</span>
            <div className="bar"><div style={{ width: `${(done / steps.length) * 100}%` }} /></div>
            {steps.map(([label, ok, to]) => {
              const body = (<><span className={ok ? "dot ok" : "dot"}>{ok && <Icon name="check" size={10} />}</span>
                <span>{label}</span>{!to && <span className="soon">coming soon</span>}</>);
              return to ? <Link key={label} to={to} className={ok ? "step done" : "step"}>{body}</Link>
                : <span key={label} className="step later">{body}</span>;
            })}
          </div>}
          {self ? (
            <div className="side-foot">
              <span className="muted ellipsis small-text">self-hosted</span>
              <button type="button" className="btn ghost sm" onClick={disconnect}><Icon name="out" size={14} /> Disconnect</button>
            </div>
          ) : <div className="side-foot">
            <span className="muted ellipsis">{me.email}</span>
            <button type="button" className="btn ghost sm" onClick={async () => {
              try { await api.signout(); } catch { /* signed out anyway */ }
              navigate("/");
            }}><Icon name="out" size={14} /> Sign out</button>
          </div>}
        </div>
      </nav>
      <main className="main">
        {notice && (
          <div className="banner warn-banner notice" role="status">
            <span className="row"><Icon name="warn" size={14} /> {notice}</span>
            <button type="button" className="btn ghost sm" aria-label="Dismiss" onClick={() => { setDismissed(notice); store.set("local", "keel.noticeDismissed", notice); }}>
              <Icon name="x" size={14} />
            </button>
          </div>
        )}
        {err && <p className="err">{err}</p>}
        {tab === "graph" && <Graph ctx={ctx} />}
        {tab === "playground" && <Playground ctx={ctx} />}
        {tab === "connect" && <Connect ctx={ctx} />}
        {tab === "events" && <Events ctx={ctx} />}
        {tab === "cartridge" && <CartridgeTab ctx={ctx} />}
        {tab === "sdk" && <Sdk ctx={ctx} />}
        {tab === "keys" && <Keys ctx={ctx} />}
        {tab === "usage" && <Usage ctx={ctx} />}
      </main>
    </div>
  );
}
