// The related graph, ported from keel/ui.html: a force layout on a canvas, in the spirit of Obsidian's graph
// view. It starts from the subject at the chosen depth (related depth 1-3) and grows as you click: a click
// opens a node and adds its own neighbours. Node size follows its links; labels fade in with zoom.
// ponytail: repulsion on a spatial grid (only nearby nodes push); capped at MAX nodes.

export type RelatedRow = { subject: string; relation: string; direction?: "in" | "out"; via?: string };
type N = { id: string; x: number; y: number; vx: number; vy: number; deg: number; fx?: number; fy?: number };
type L = { s: N; t: N; rel: string };

export type GraphOpts = {
  related: (subject: string, depth: number) => Promise<RelatedRow[]>;
  onOpen: (subject: string) => void;
};

function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Record<string, string> = {}, ...kids: (Node | string)[]) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  n.append(...kids);
  return n;
}

export function createGraph(host: HTMLElement, opts: GraphOpts) {
  const MAX = 3000, CELL = 90;
  let nodes = new Map<string, N>(), links = new Map<string, L>(), center: string | null = null, hover: N | null = null;
  let alpha = 0, raf = 0, gen = 0, W = 0, H = 0;
  const colors: Record<string, string> = {};
  const view = { x: 0, y: 0, k: 1 };

  const canvas = el("canvas", { tabindex: "0", role: "img", "aria-label": "Graph of the subject's neighbours: click a node to open it" });
  const depth = el("select", {}, ...[1, 2, 3].map((d) => el("option", { value: String(d) }, d === 1 ? "1 hop" : `${d} hops`)));
  const filter = el("input", { type: "search", placeholder: "highlight…" });
  const labels = el("input", { type: "checkbox" });
  const countEl = el("div", { class: "g-count" });
  const fitBtn = el("button", { type: "button", class: "btn sm" }, "Fit");
  const resetBtn = el("button", { type: "button", class: "btn sm" }, "Reset");
  depth.value = "2";
  const panel = el("div", { class: "g-panel" },
    el("label", {}, "Depth", depth), filter, el("label", {}, "All labels", labels), el("div", { class: "row" }, fitBtn, resetBtn));
  const box = el("div", { class: "g-box" }, canvas, panel, countEl);
  host.replaceChildren(box);
  const ctx = canvas.getContext("2d")!;

  const readColors = () => {
    const cs = getComputedStyle(document.documentElement);
    for (const k of ["graph-bg", "node", "edge", "accent", "text", "muted", "warn"]) colors[k] = cs.getPropertyValue("--" + k).trim();
  };
  const resize = () => {
    const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    W = r.width; H = r.height; canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  const ro = new ResizeObserver(() => { resize(); wake(); });
  ro.observe(box);
  const mq = matchMedia("(prefers-color-scheme: dark)");
  const onScheme = () => { readColors(); wake(); };
  mq.addEventListener("change", onScheme);
  depth.addEventListener("change", () => center && show(center));
  filter.addEventListener("input", () => wake());
  labels.addEventListener("change", () => wake());
  fitBtn.addEventListener("click", fit);
  resetBtn.addEventListener("click", () => center && show(center));
  readColors(); resize();

  function node(id: string, near: N | null | undefined): N | undefined {
    let n = nodes.get(id);
    if (!n && nodes.size < MAX) {
      const a = Math.random() * 2 * Math.PI, d = near ? 20 + Math.random() * 30 : 0;
      n = { id, x: (near ? near.x : 0) + d * Math.cos(a), y: (near ? near.y : 0) + d * Math.sin(a), vx: 0, vy: 0, deg: 0 };
      nodes.set(id, n);
    }
    return n;
  }
  function link(a: string, b: string, rel: string) {
    const key = a < b ? `${a}\u0000${b}\u0000${rel}` : `${b}\u0000${a}\u0000${rel}`;
    const na = nodes.get(a), nb = nodes.get(b);
    if (links.has(key) || !na || !nb) return;
    links.set(key, { s: na, t: nb, rel });
    na.deg++; nb.deg++;
  }
  async function add(subject: string, d: number, heat: number) {
    const mine = gen, rows = await opts.related(subject, d);
    if (mine !== gen) return; // a newer show() started meanwhile
    node(subject, null);
    for (const r of rows) {
      const from = r.via || subject, n = node(r.subject, nodes.get(from));
      if (n) r.direction === "in" ? link(r.subject, from, r.relation) : link(from, r.subject, r.relation);
    }
    count(); wake(heat);
  }
  async function show(subject: string) {
    nodes = new Map(); links = new Map(); center = subject; hover = null; gen++;
    Object.assign(view, { x: 0, y: 0, k: 1 });
    try { await add(subject, +depth.value, 1); setTimeout(fit, 900); }
    catch (err) { countEl.textContent = (err as Error).message; }
  }
  async function expand(subject: string) {
    if (!nodes.has(subject)) return show(subject);
    center = subject;
    try { await add(subject, 1, 0.3); } catch (err) { countEl.textContent = (err as Error).message; }
  }
  function count() {
    countEl.textContent = `${nodes.size} nodes · ${links.size} links` + (nodes.size >= MAX ? ` (capped at ${MAX})` : "");
  }

  function tick() {
    const list = [...nodes.values()], grid = new Map<string, N[]>();
    for (const n of list) {
      const k = `${Math.floor(n.x / CELL)},${Math.floor(n.y / CELL)}`;
      (grid.get(k) || grid.set(k, []).get(k)!).push(n);
    }
    for (const n of list) {
      const gx = Math.floor(n.x / CELL), gy = Math.floor(n.y / CELL);
      for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) for (const m of grid.get(`${gx + i},${gy + j}`) || []) {
        if (m === n) continue;
        let dx = n.x - m.x, dy = n.y - m.y, d2 = dx * dx + dy * dy;
        if (d2 > CELL * CELL) continue;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        const f = 260 / d2;
        n.vx += dx * f * alpha; n.vy += dy * f * alpha;
      }
      n.vx -= n.x * 0.006 * alpha; n.vy -= n.y * 0.006 * alpha;
    }
    for (const { s: a, t: b } of links.values()) {
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const f = ((d - 46) / d) * 0.08 * alpha, wa = b.deg / (a.deg + b.deg);
      a.vx += dx * f * wa; a.vy += dy * f * wa; b.vx -= dx * f * (1 - wa); b.vy -= dy * f * (1 - wa);
    }
    for (const n of list) {
      if (n.fx !== undefined && n.fy !== undefined) { n.x = n.fx; n.y = n.fy; n.vx = n.vy = 0; continue; }
      n.vx *= 0.6; n.vy *= 0.6; n.x += n.vx; n.y += n.vy;
    }
    alpha *= 0.985;
  }

  const radius = (n: N) => 2.5 + Math.sqrt(n.deg) * 1.3;
  const screen = (n: N) => [n.x * view.k + view.x + W / 2, n.y * view.k + view.y + H / 2];
  function neighbours(n: N) {
    const out = new Set<N>([n]);
    for (const l of links.values()) if (l.s === n) out.add(l.t); else if (l.t === n) out.add(l.s);
    return out;
  }
  function draw() {
    const q = filter.value.trim().toLowerCase(), lit = hover ? neighbours(hover) : null;
    const match = (n: N) => !!q && n.id.toLowerCase().includes(q);
    const dim = (n: N) => (lit && !lit.has(n)) || (!!q && !match(n) && !lit);
    ctx.fillStyle = colors["graph-bg"]; ctx.fillRect(0, 0, W, H);
    ctx.save();
    ctx.translate(W / 2 + view.x, H / 2 + view.y); ctx.scale(view.k, view.k);
    ctx.lineWidth = 0.8 / view.k;
    for (const l of links.values()) {
      const on = !!lit && lit.has(l.s) && lit.has(l.t) && (l.s === hover || l.t === hover);
      ctx.strokeStyle = on ? colors.accent : colors.edge;
      ctx.globalAlpha = on ? 0.9 : dim(l.s) || dim(l.t) ? 0.15 : 0.7;
      ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
    }
    for (const n of nodes.values()) {
      ctx.globalAlpha = dim(n) ? 0.18 : 1;
      ctx.fillStyle = n.id === center ? colors.accent : match(n) ? colors.warn : n === hover ? colors.accent : colors.node;
      ctx.beginPath(); ctx.arc(n.x, n.y, radius(n), 0, 2 * Math.PI); ctx.fill();
    }
    ctx.restore();
    const fade = labels.checked ? 1 : Math.max(0, Math.min(1, (view.k - 0.9) / 0.7));
    ctx.font = "11px 'Geist Mono', ui-monospace, monospace"; ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const n of nodes.values()) {
      const always = n.id === center || n === hover || (!!lit && lit.has(n)) || match(n);
      const a = always ? 1 : dim(n) ? 0 : fade * (n.deg > 3 ? 1 : 0.75);
      if (a < 0.05) continue;
      const [x, y] = screen(n);
      if (x < -100 || x > W + 100 || y < -20 || y > H + 20) continue;
      ctx.globalAlpha = a; ctx.fillStyle = always ? colors.text : colors.muted;
      ctx.fillText(n.id.length > 40 ? "…" + n.id.slice(-39) : n.id, x, y + radius(n) * view.k + 3);
    }
    ctx.globalAlpha = 1;
  }
  function frame() {
    raf = 0;
    if (alpha > 0.004) tick();
    draw();
    if (alpha > 0.004) raf = requestAnimationFrame(frame);
  }
  function wake(heat?: number) {
    if (heat) alpha = Math.max(alpha, heat);
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function fit() {
    if (!nodes.size) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of nodes.values()) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    view.k = Math.min(2.5, 0.9 * Math.min(W / (x1 - x0 || 1), H / (y1 - y0 || 1)));
    view.x = -((x0 + x1) / 2) * view.k; view.y = -((y0 + y1) / 2) * view.k;
    wake();
  }

  function at(ev: PointerEvent | MouseEvent) {
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
    let best: N | null = null, bd = Infinity;
    for (const n of nodes.values()) {
      const [sx, sy] = screen(n), d = Math.hypot(sx - x, sy - y);
      if (d < Math.max(8, radius(n) * view.k + 4) && d < bd) { best = n; bd = d; }
    }
    return { best, x, y };
  }
  let down: { node: N | null; x: number; y: number; vx: number; vy: number; moved: boolean } | null = null;
  canvas.addEventListener("pointerdown", (ev) => {
    const { best, x, y } = at(ev);
    down = { node: best, x, y, vx: view.x, vy: view.y, moved: false };
    canvas.setPointerCapture(ev.pointerId); canvas.classList.add("drag");
  });
  canvas.addEventListener("pointermove", (ev) => {
    const { best, x, y } = at(ev);
    if (!down) { if (best !== hover) { hover = best; canvas.style.cursor = best ? "pointer" : ""; wake(); } return; }
    if (Math.hypot(x - down.x, y - down.y) > 3) down.moved = true;
    if (down.node) {
      down.node.fx = (x - W / 2 - view.x) / view.k; down.node.fy = (y - H / 2 - view.y) / view.k;
      wake(0.3);
    } else { view.x = down.vx + x - down.x; view.y = down.vy + y - down.y; wake(); }
  });
  const up = () => {
    if (down?.node) {
      delete down.node.fx; delete down.node.fy;
      if (!down.moved) { const id = down.node.id; expand(id); opts.onOpen(id); }
    }
    down = null; canvas.classList.remove("drag");
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("pointerleave", () => { if (!down && hover) { hover = null; wake(); } });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect(), x = ev.clientX - r.left - W / 2, y = ev.clientY - r.top - H / 2;
    const k = Math.max(0.1, Math.min(8, view.k * Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015))));
    view.x = x - ((x - view.x) * k) / view.k; view.y = y - ((y - view.y) * k) / view.k; view.k = k;
    wake();
  }, { passive: false });
  canvas.addEventListener("dblclick", fit);
  canvas.addEventListener("keydown", (ev) => {
    const step = 40, keys: Record<string, [number, number]> = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (keys[ev.key]) { view.x += keys[ev.key][0]; view.y += keys[ev.key][1]; }
    else if (ev.key === "+" || ev.key === "=") view.k = Math.min(8, view.k * 1.2);
    else if (ev.key === "-") view.k = Math.max(0.1, view.k / 1.2);
    else if (ev.key === "f") fit();
    else return;
    ev.preventDefault(); wake();
  });

  return {
    show,
    has: (id: string) => nodes.has(id),
    destroy() { ro.disconnect(); mq.removeEventListener("change", onScheme); cancelAnimationFrame(raf); host.replaceChildren(); },
  };
}
