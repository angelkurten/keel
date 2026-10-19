import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon, navigate } from "../ui";

const MIN_PASSWORD = 10;

export function Landing() {
  const params = new URLSearchParams(window.location.search);
  const [invite] = useState(params.get("invite") || "");
  const [mode, setMode] = useState<"signup" | "login">(invite ? "signup" : "login");
  const [email, setEmail] = useState("");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [edited, setEdited] = useState(false);
  const [slug, setSlug] = useState<{ available: boolean; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(params.get("error") || "");
  const seq = useRef(0);
  const signup = mode === "signup";

  // Already signed in: straight to the console.
  useEffect(() => {
    api.me().then(() => navigate("/console", true)).catch(() => { /* not signed in */ });
  }, []);

  // Creating an account: propose it from the email's domain, until the user edits it.
  useEffect(() => {
    if (!signup || edited || !/^\S+@\S+\.\S+$/.test(email)) return;
    const mine = ++seq.current;
    const t = setTimeout(async () => {
      try {
        const r = await api.slugForEmail(email);
        if (mine !== seq.current) return;
        setAccount(r.account);
        setSlug(r);
      } catch { /* leave it for the user */ }
    }, 350);
    return () => clearTimeout(t);
  }, [email, signup, edited]);

  // Re-check availability when the user edits the account.
  useEffect(() => {
    if (!signup || !edited) return;
    if (!account) { setSlug(null); return; }
    const mine = ++seq.current;
    const t = setTimeout(async () => {
      try {
        const r = await api.slugCheck(account);
        if (mine === seq.current) setSlug(r);
      } catch (e) { if (mine === seq.current) setSlug({ available: false, error: (e as Error).message }); }
    }, 350);
    return () => clearTimeout(t);
  }, [account, signup, edited]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      if (signup) await api.signup({ email, account, password, invite });
      else await api.login({ email, password });
      navigate("/console");
    } catch (e) {
      setErr((e as Error).message);
    } finally { setBusy(false); }
  }

  const short = signup && password.length > 0 && password.length < MIN_PASSWORD;
  const blocked = busy || !email || !password
    || (signup && (!account || slug?.available === false || password.length < MIN_PASSWORD));
  const switchTo = (m: "signup" | "login") => { setMode(m); setErr(""); };

  return (
    <div className="entry">
      <div className="entry-col">
        <span className="brand big"><Icon name="logo" size={22} className="accent" /> keel</span>
        <div className="stack-s">
          <h1 className="hero">A context graph for your agents, running in a minute.</h1>
          <p className="muted lead">Your account starts with example data, so you can query it before sending a single event.</p>
        </div>
        {signup && (
          <span className="pill ok"><Icon name="check" size={14} /> Invitation <span className="mono">{invite}</span> applied</span>
        )}
        <form className="card form" onSubmit={submit}>
          <h2 className="h2">{signup ? "Create your Keel" : "Sign in"}</h2>
          <label className="field">{signup ? "Work email" : "Email"}
            <input type="email" required autoComplete={signup ? "email" : "username"} value={email}
              onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
          </label>
          {signup && (
            <label className="field">Account
              <input className="mono" value={account} spellCheck={false} placeholder="acme" autoComplete="off"
                onChange={(e) => { setEdited(true); setAccount(e.target.value.toLowerCase()); }} />
              <span className="hint">
                {slug?.error ? <span className="warn">{slug.error}</span>
                  : slug && !slug.available ? <span className="warn">Taken. Pick another.</span>
                  : "From your email's domain · permanent, appears in every tenant key"}
              </span>
            </label>
          )}
          <label className="field">Password
            <span className="input-wrap">
              <input type={reveal ? "text" : "password"} required value={password}
                autoComplete={signup ? "new-password" : "current-password"}
                minLength={signup ? MIN_PASSWORD : undefined} onChange={(e) => setPassword(e.target.value)} />
              <button type="button" className="btn ghost sm" aria-pressed={reveal} onClick={() => setReveal(!reveal)}>
                {reveal ? "Hide" : "Show"}
              </button>
            </span>
            {signup && <span className={short ? "hint warn" : "hint"}>At least {MIN_PASSWORD} characters</span>}
          </label>
          {err && <p className="err" role="alert">{err}</p>}
          <button className="btn primary block" disabled={blocked}>
            {busy ? (signup ? "Creating…" : "Signing in…") : signup ? "Create my Keel" : "Sign in"}
          </button>
        </form>
        {signup ? (
          <p className="muted small-text">Already have an account?{" "}
            <button type="button" className="linkish" onClick={() => switchTo("login")}>Sign in</button></p>
        ) : (
          <div className="stack-s">
            {invite && (
              <p className="muted small-text">New here?{" "}
                <button type="button" className="linkish" onClick={() => switchTo("signup")}>Create your Keel</button></p>
            )}
            <p className="muted small-text">Forgot your password? Write to <a href="mailto:angel@angelkurten.com">angel@angelkurten.com</a></p>
            {!invite && (
              <p className="muted small-text">Keel is invite-only during the pilots — write to <a href="mailto:angel@angelkurten.com">angel@angelkurten.com</a></p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
