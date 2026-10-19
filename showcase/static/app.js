// Keel showcase: a support agent on Keel (with SHOWCASE_SIDES=keel,rag, side by side with one without). Plain JS, no build step.
// Model text is never parsed as HTML: answers are rendered as DOM nodes from a small markdown subset.
"use strict";

const state = { meta: null, scenarios: [], selected: null, running: new Set(), asking: false, sides: ["keel"] };
const TITLE = { keel: "With Keel", rag: "Without Keel (RAG + tools)" };
const SHORT = { keel: "Keel", rag: "RAG+tools" };
const compare = () => state.sides.length > 1;

// --- tiny DOM helpers -------------------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k instanceof Node ? k : String(k));
  return n;
}
const $ = (id) => document.getElementById(id);

const ICONS = {
  ok: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
  no: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  part: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  none: '<circle cx="12" cy="12" r="9" stroke-dasharray="3 3"/>',
  play: '<path d="M8 5l11 7-11 7z"/>',
};
function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `ic ${cls || name}`);
  // the markup is ours (ICONS), never model text
  svg.innerHTML = ICONS[name];
  return svg;
}
const GRADE_ICON = { correct: "ok", partial: "part", wrong: "no" };

// --- markdown subset → DOM ------------------------------------------------------------------------------
function inline(text) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t[0] === "`" ? h("code", {}, t.slice(1, -1)) : h("strong", {}, t.slice(2, -2)));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function markdown(src) {
  const box = h("div", { class: "answer" });
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  let para = [], list = null;
  const flush = () => {
    if (para.length) { box.append(h("p", {}, inline(para.join(" ")))); para = []; }
    if (list) { box.append(list); list = null; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line), num = /^\s*\d+[.)]\s+(.*)$/.exec(line), head = /^\s*#{1,6}\s+(.*)$/.exec(line);
    if (!line.trim()) { flush(); continue; }
    if (head) { flush(); box.append(h("p", { class: "h" }, inline(head[1]))); continue; }
    if (bullet || num) {
      if (para.length) { box.append(h("p", {}, inline(para.join(" ")))); para = []; }
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) { if (list) box.append(list); list = h(tag); }
      list.append(h("li", {}, inline((bullet || num)[1])));
      continue;
    }
    if (list) { box.append(list); list = null; }
    para.push(line.trim());
  }
  flush();
  return box;
}

// --- formatting -------------------------------------------------------------------------------------------
const fmtDate = (iso) => {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
};
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`;
const usd = (n) => `$${(n || 0).toFixed(4)}`;
const tokensOf = (s) => (s && s.tokens ? (s.tokens.input || 0) + (s.tokens.output || 0) : 0);
const num = (n) => Number(n || 0).toLocaleString("en-US");

// --- API --------------------------------------------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`);
    e.status = res.status;
    throw e;
  }
  return data;
}
function banner(msg) {
  const b = $("banner");
  b.hidden = !msg;
  b.textContent = msg || "";
}

// --- verdicts ---------------------------------------------------------------------------------------------
/** The verdict of a side: on the run, or on its last step for a memory pair. */
function verdictOf(run, side) {
  if (!run) return null;
  if (run.steps && run.steps.length) return run.steps[run.steps.length - 1][side].verdict || run[side].verdict || null;
  return run[side] ? run[side].verdict : null;
}

const median = (xs) => {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b), m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

// --- summary band -----------------------------------------------------------------------------------------
function renderSummary() {
  const box = $("summary");
  const runs = state.scenarios.map((s) => s.result).filter(Boolean);
  box.replaceChildren();
  if (!runs.length) { box.append(h("p", { class: "muted" }, "No cached runs yet.")); return; }
  const n = state.scenarios.filter((s) => s.result).length;
  const correct = (side) => state.scenarios.filter((s) => (verdictOf(s.result, side) || {}).grade === "correct").length;
  const ms = (side) => median(runs.map((r) => r[side].ms || 0));
  const cost = (side) => runs.reduce((a, r) => a + (r[side].cost_usd || 0), 0);
  const toks = (side) => runs.reduce((a, r) => a + tokensOf(r[side]), 0);
  const stat = (label, val, badge) => h("div", { class: "stat" },
    h("span", { class: "label" }, label),
    h("div", { class: "vals" }, state.sides.map((side) =>
      h("span", {}, compare() ? h("span", { class: "who" }, SHORT[side]) : null, h("span", { class: "v" }, val(side))))),
    badge || h("span", { class: "hint" }, "\u{a0}"));
  const kc = correct("keel"), rc = compare() ? correct("rag") : 0;
  const dates = runs.map((r) => r.at).filter(Boolean).sort();
  const day = (iso) => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const span = dates.length ? (day(dates[0]) === day(dates[dates.length - 1]) ? day(dates[0]) : `${day(dates[0])} – ${day(dates[dates.length - 1])}`) : "";
  box.append(
    stat("Correct answers", (side) => `${correct(side)}/${n}`, compare()
      ? h("span", { class: kc >= rc ? "delta good" : "delta bad" }, kc === rc ? "same" : kc > rc ? `${kc - rc} more correct` : `${rc - kc} fewer correct`)
      : null),
    stat("Median time", (side) => secs(ms(side))),
    stat("Total cost", (side) => usd(cost(side))),
    stat("Total tokens", (side) => num(toks(side))),
    h("div", { class: "note" }, `Over the ${n} guided scenarios, cached runs of ${span}. Live runs below replace them in this page.`),
  );
}

// --- scenario list ----------------------------------------------------------------------------------------
function mark(run, side, label) {
  const v = verdictOf(run, side);
  const name = v ? GRADE_ICON[v.grade] || "none" : "none";
  return h("span", { class: "mark", title: `${label}: ${v ? v.grade : "not run"}` }, label, icon(name));
}
function renderList() {
  const nav = $("list");
  nav.replaceChildren(...state.scenarios.map((s) => h("button", {
    type: "button", class: s.id === state.selected ? "scn on" : "scn", "aria-current": s.id === state.selected ? "true" : null,
    onclick: () => select(s.id),
  },
  h("span", { class: "t" }, s.title),
  h("span", { class: "meta" }, h("span", { class: "tag" }, s.strength), h("span", { class: "marks" }, state.sides.map((side) => mark(s.result, side, SHORT[side])))))));
}

function select(id) {
  state.selected = id;
  try { history.replaceState(null, "", `#${encodeURIComponent(id)}`); } catch (_) { /* file:// or sandbox */ }
  renderList();
  renderDetail();
}

// --- one side -----------------------------------------------------------------------------------------------
function traceList(trace) {
  const calls = trace || [];
  const d = h("details", { class: "trace" }, h("summary", {}, `Tool calls (${calls.length})`));
  if (!calls.length) d.append(h("div", { class: "call muted" }, "No tool calls."));
  for (const c of calls) {
    const res = String(c.result ?? "");
    d.append(h("div", { class: "call" },
      h("div", { class: "head" }, h("b", {}, c.tool), h("span", { class: c.ok ? "st" : "st bad" }, c.ok ? "ok" : "error")),
      h("pre", {}, JSON.stringify(c.arguments ?? {}, null, 0)),
      h("pre", {}, res.length > 600 ? res.slice(0, 600) + "…" : res)));
  }
  return d;
}
function metrics(side) {
  return h("div", { class: "metrics" },
    h("div", {}, h("span", {}, "time"), h("b", {}, secs(side.ms || 0))),
    h("div", {}, h("span", {}, "cost"), h("b", {}, usd(side.cost_usd))),
    h("div", {}, h("span", {}, "tokens"), h("b", {}, num(tokensOf(side)))),
    h("div", {}, h("span", {}, "tool calls"), h("b", {}, num(side.calls ?? (side.trace || []).length))));
}
function verdictBox(v) {
  if (!v) return null;
  return h("div", { class: `verdict ${v.grade}` }, h("b", {}, icon(GRADE_ICON[v.grade] || "none"), v.grade), h("span", {}, v.reason));
}
const ROLE = { support: "Support agent", billing: "Billing agent" };
/** "Acme Corp" plus the agent's role tag, when the step has one. */
function stepWho(st) {
  return h("span", { class: "stepwho" }, h("span", {}, st.customer_name || st.customer), st.role ? h("span", { class: `tag role ${st.role}` }, ROLE[st.role] || st.role) : null);
}
/** A column: for a memory pair, each step's answer in order; metrics are the run's totals. */
function column(run, side, title, withVerdict) {
  const s = run[side] || {};
  const col = h("section", { class: `card col ${side}`, "aria-label": title }, h("h3", {}, title));
  if (withVerdict) col.append(verdictBox(verdictOf(run, side)));
  if (run.steps && run.steps.length) {
    run.steps.forEach((st, i) => {
      const ss = st[side] || {};
      col.append(h("div", { class: "stepans" },
        h("div", { class: "q" }, h("span", { class: "stepn" }, `Step ${i + 1}`), stepWho(st), h("span", { class: "qt" }, st.question)),
        ss.error ? h("div", { class: "err" }, ss.error) : markdown(ss.answer),
        traceList(ss.trace)));
    });
  } else {
    col.append(s.error ? h("div", { class: "err" }, s.error) : markdown(s.answer));
  }
  col.append(metrics(s));
  if (!(run.steps && run.steps.length)) col.append(traceList(s.trace));
  return col;
}
function spinning() {
  const col = (side, title) => h("section", { class: `card col ${side}` }, h("h3", {}, title),
    h("div", { class: "spinbox" }, h("span", { class: "spin" }), "Running… this takes 10 to 60 seconds"));
  return h("div", { class: compare() ? "cols" : "cols one" }, state.sides.map((side) => col(side, TITLE[side])));
}
function result(run, withVerdict) {
  return [h("div", { class: compare() ? "cols" : "cols one" }, state.sides.map((side) => column(run, side, TITLE[side], withVerdict)))];
}

// --- selected scenario ----------------------------------------------------------------------------------------
function renderDetail() {
  const box = $("detail");
  const s = state.scenarios.find((x) => x.id === state.selected);
  if (!s) { box.replaceChildren(); return; }
  const running = state.running.has(s.id);
  const left = state.meta ? state.meta.limits.live_left : 0;
  const head = h("div", { class: "qhead" });
  if (s.steps && s.steps.length) {
    head.append(h("p", { class: "question" }, "Two agents, one memory"),
      h("span", { class: "who" }, "Run in order, each step in a new conversation"),
      h("ol", { class: "steps" }, s.steps.map((st) => h("li", {}, h("span", { class: "who" }, stepWho(st)), st.question))));
  } else {
    head.append(h("span", { class: "who" }, `Customer: ${s.customer_name || s.customer}`), h("p", { class: "question" }, s.question));
  }
  head.append(h("div", { class: "expected" }, h("span", { class: "lbl" }, "Expected"), s.expected));
  if (s.steps && s.steps.length) {
    head.append(h("p", { class: "hint" }, compare()
      ? "With Keel both agents read the same graph; without Keel each agent keeps its own notes, as two agents built separately do."
      : "Both agents read and write the same graph: what the first one learns, the second one finds."));
  }
  const when = s.result ? (s._live ? "live, just now" : `cached run of ${fmtDate(s.result.at)}`) : "not run yet";
  const runbar = h("div", { class: "runbar" },
    h("div", { class: "left" }, h("span", { class: "tag" }, s.strength), h("span", { class: "hint" }, when)),
    h("div", { class: "left" },
      h("span", { class: "hint" }, `${left} live run${left === 1 ? "" : "s"} left today`),
      h("button", { type: "button", class: "btn primary", disabled: running || left <= 0, onclick: () => runLive(s.id) },
        icon("play", "ic"), running ? "Running…" : "Run it live")));
  box.replaceChildren(head, runbar, ...(running ? [spinning()] : s.result ? result(s.result, true) : [h("p", { class: "muted" }, "No cached run for this scenario yet. Run it live.")]));
}

async function runLive(id) {
  if (state.running.has(id)) return;
  state.running.add(id);
  banner("");
  renderDetail();
  try {
    const run = await api("POST", `/api/scenarios/${encodeURIComponent(id)}/run`);
    const s = state.scenarios.find((x) => x.id === id);
    s.result = run;
    s._live = true;
    if (state.meta) state.meta.limits.live_left = Math.max(0, state.meta.limits.live_left - 1);
    renderSummary();
    renderList();
  } catch (e) {
    if (e.status === 429 && state.meta) state.meta.limits.live_left = 0;
    banner(e.status === 429 ? `Today's live runs are used up: ${e.message}` : `The live run failed: ${e.message}`);
  } finally {
    state.running.delete(id);
    if (state.selected === id) renderDetail();
  }
}

// --- ask your own --------------------------------------------------------------------------------------------
function renderAskLeft() {
  const n = state.meta ? state.meta.limits.ask_left : 0;
  $("ask-left").textContent = `${n} question${n === 1 ? "" : "s"} left today`;
  $("ask-btn").disabled = state.asking || n <= 0;
  $("ask-btn").textContent = state.asking ? "Asking…" : compare() ? "Ask both" : "Ask";
}
async function ask(e) {
  e.preventDefault();
  const customer = $("ask-customer").value, question = $("ask-question").value.trim();
  if (!customer || !question || state.asking) return;
  state.asking = true;
  renderAskLeft();
  const out = $("ask-result");
  out.replaceChildren(spinning());
  try {
    const run = await api("POST", "/api/ask", { customer, question });
    if (state.meta) state.meta.limits.ask_left = Math.max(0, state.meta.limits.ask_left - 1);
    out.replaceChildren(h("p", { class: "hint" }, `live, just now · ${fmtDate(run.at || new Date().toISOString())}`), ...result(run, false));
  } catch (err) {
    if (err.status === 429 && state.meta) state.meta.limits.ask_left = 0;
    out.replaceChildren(h("div", { class: "err" }, err.status === 429 ? `The daily limit for questions is reached: ${err.message}` : err.message));
  } finally {
    state.asking = false;
    renderAskLeft();
  }
}

// --- what it took to build ---------------------------------------------------------------------------------
const CAPS = [
  ["Business rules (SLA, refunds, at risk)", ["ok", "Declared once, evaluated by Keel"], ["no", "The agent applies the help center's policy from the prompt"]],
  ["History of every fact", ["ok", "Every value over time, as of any instant"], ["part", "Only what the API logs (ticket activity, plan changes)"]],
  ["Memory shared between agents", ["ok", "Scoped, with evidence"], ["no", "Each agent keeps its own notes"]],
  ["Customer isolation", ["ok", "Enforced in the tools"], ["part", "A filter in the code"]],
  ["A new agent", ["ok", "Reads the same graph, nothing to build"], ["part", "Reuses the API; its memory and rules are its own to build"]],
];
const CAP_LABEL = { ok: "yes", part: "partial", no: "no" };
function buildCard(title, side, files) {
  const total = (files || []).reduce((a, f) => a + (f.lines || 0), 0);
  return h("section", { class: `card col ${side}` },
    h("h3", {}, title, h("span", { class: "mono lines" }, `${num(total)} lines`)),
    h("ul", { class: "files" }, (files || []).map((f) => h("li", {},
      h("div", { class: "fhead" },
        f.url ? h("a", { href: f.url, class: "mono", rel: "noopener" }, f.path) : h("span", { class: "mono" }, f.path),
        h("span", { class: "mono hint" }, `${num(f.lines)} lines`)),
      f.what ? h("p", { class: "hint" }, f.what) : null))));
}
function capCell([kind, text]) {
  return h("td", {}, h("span", { class: "cap" }, icon(kind), h("span", { class: "sr-only" }, `${CAP_LABEL[kind]}: `), text));
}
function renderBuild(b) {
  const box = $("build-body");
  box.replaceChildren(
    h("div", { class: "cols" }, buildCard("With Keel", "keel", b.keel), buildCard("Without Keel (RAG + tools)", "rag", b.rag)),
    h("div", { class: "card table-card" }, h("table", { class: "caps" },
      h("thead", {}, h("tr", {}, h("th", {}, h("span", { class: "sr-only" }, "Capability")), h("th", {}, "With Keel"), h("th", {}, "Without Keel (RAG + tools)"))),
      h("tbody", {}, CAPS.map(([what, keel, rag]) => h("tr", {}, h("th", { scope: "row" }, what), capCell(keel), capCell(rag)))))),
    h("p", { class: "honest" }, "The line counts are close. The difference is what the lines buy."));
}

// --- start ------------------------------------------------------------------------------------------------------
async function main() {
  $("ask-form").addEventListener("submit", ask);
  try {
    const [meta, scenarios] = await Promise.all([api("GET", "/api/meta"), api("GET", "/api/scenarios")]);
    state.meta = meta;
    state.scenarios = scenarios;
    state.sides = meta.sides || ["keel"];
    $("model").textContent = meta.model;
    $("ask-customer").replaceChildren(...meta.customers.map((c) => h("option", { value: c.tenant }, `${c.name}${c.plan ? ` (${c.plan})` : ""}`)));
    renderAskLeft();
    renderSummary();
    if (compare()) api("GET", "/api/build").then(renderBuild).catch(() => { $("build").hidden = true; });
    else $("build").hidden = true;
    const fromHash = decodeURIComponent(location.hash.slice(1));
    select(scenarios.some((s) => s.id === fromHash) ? fromHash : (scenarios[0] || {}).id);
  } catch (e) {
    banner(`Could not load the showcase: ${e.message}`);
    $("summary").replaceChildren(h("p", { class: "muted" }, "Nothing to show yet."));
  }
}
main();
