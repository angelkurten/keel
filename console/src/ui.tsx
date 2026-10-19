// Small shared pieces: a pushState router, safe browser storage, icons, copy buttons, tabs.
import { useEffect, useState, type ReactNode } from "react";

// --- router -------------------------------------------------------------------------------
export function navigate(to: string, replace = false) {
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const on = () => setPath(window.location.pathname);
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return path;
}

export function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

// --- storage: may throw (private windows, blocked site data) --------------------------------
export const store = {
  get(kind: "local" | "session", k: string): string | null {
    try {
      return (kind === "local" ? localStorage : sessionStorage).getItem(k);
    } catch {
      return null;
    }
  },
  set(kind: "local" | "session", k: string, v: string | null) {
    try {
      const s = kind === "local" ? localStorage : sessionStorage;
      if (v === null) s.removeItem(k);
      else s.setItem(k, v);
    } catch {
      /* not persisted */
    }
  },
};

// --- icons: inline stroke SVG -----------------------------------------------------------------
const paths: Record<string, ReactNode> = {
  logo: (<><path d="M12 3v14" /><path d="M5 13c0 4 3 7 7 7s7-3 7-7" /><path d="M9 6h6" /></>),
  check: <path d="M5 12l5 5L20 7" />,
  search: (<><circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" /></>),
  graph: (<><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="8" r="2.5" /><circle cx="10" cy="18" r="2.5" /><path d="M8.2 7.2l7.5 0.6M7 8.3l2.2 7.3M16.5 10l-4.8 6.2" /></>),
  play: <path d="M7 5l12 7-12 7z" />,
  plug: (<><path d="M9 3v5M15 3v5" /><path d="M6 8h12v3a6 6 0 0 1-12 0z" /><path d="M12 17v4" /></>),
  copy: (<><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h8" /></>),
  download: (<><path d="M12 4v11" /><path d="M7 10l5 5 5-5" /><path d="M5 20h14" /></>),
  x: <path d="M6 6l12 12M18 6L6 18" />,
  arrow: (<><path d="M5 12h14" /><path d="M13 6l6 6-6 6" /></>),
  chev: <path d="M9 6l6 6-6 6" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  out: (<><path d="M15 4h4v16h-4" /><path d="M10 8l-4 4 4 4" /><path d="M6 12h10" /></>),
  warn: (<><path d="M12 4l9 16H3z" /><path d="M12 10v4M12 17v.5" /></>),
  mail: (<><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></>),
  send: (<><path d="M4 12l16-8-6 16-3-7z" /></>),
};

export function Icon({ name, size = 16, className }: { name: keyof typeof paths | string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>
      {paths[name]}
    </svg>
  );
}

// --- copy / download --------------------------------------------------------------------------
export function CopyButton({ text, label = "Copy", onCopied }: { text: string; label?: string; onCopied?: () => void }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          onCopied?.();
          setTimeout(() => setDone(false), 1500);
        } catch {
          /* clipboard blocked */
        }
      }}
    >
      <Icon name={done ? "check" : "copy"} size={14} /> {done ? "Copied" : label}
    </button>
  );
}

export function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function Code({ title, code, extra }: { title?: string; code: string; extra?: ReactNode }) {
  return (
    <div className="code">
      <div className="code-head">
        <span>{title}</span>
        <span className="row">{extra}<CopyButton text={code} /></span>
      </div>
      <pre>{code}</pre>
    </div>
  );
}

export function Tabs<T extends string>({ items, value, onChange, label }: {
  items: readonly T[] | { id: T; name: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  const list = (items as (T | { id: T; name: string })[]).map((i) => (typeof i === "string" ? { id: i, name: i } : i));
  return (
    <div role="tablist" aria-label={label} className="tabs">
      {list.map((i) => (
        <button key={i.id} type="button" role="tab" aria-selected={i.id === value} className={i.id === value ? "on" : ""}
          onClick={() => onChange(i.id)}>
          {i.name}
        </button>
      ))}
    </div>
  );
}

export const show = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
export const day = (s?: string | null) => (s ? s.replace("T", " ").slice(0, 16) : "");

/** The few markdown forms models answer with: paragraphs, lists, **bold** and `code`. Built as elements, never as HTML. */
export function Markdown({ text }: { text: string }) {
  const inline = (line: string) =>
    line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) =>
      part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={i}>{part.slice(2, -2)}</strong>
        : part.startsWith("`") && part.endsWith("`") && part.length > 2 ? <code key={i}>{part.slice(1, -1)}</code>
          : part);
  const blocks = text.trim().split(/\n{2,}/);
  return (
    <div className="md">
      {blocks.map((b, i) => {
        const lines = b.split("\n");
        if (lines.every((l) => /^\s*[-*] /.test(l))) return <ul key={i}>{lines.map((l, j) => <li key={j}>{inline(l.replace(/^\s*[-*] /, ""))}</li>)}</ul>;
        if (lines.every((l) => /^\s*\d+[.)] /.test(l))) return <ol key={i}>{lines.map((l, j) => <li key={j}>{inline(l.replace(/^\s*\d+[.)] /, ""))}</li>)}</ol>;
        return <p key={i}>{lines.map((l, j) => <span key={j}>{j > 0 && <br />}{inline(l.replace(/^#+\s*/, ""))}</span>)}</p>;
      })}
    </div>
  );
}
