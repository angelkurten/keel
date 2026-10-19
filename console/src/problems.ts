// Turning a checker's problem text into editor markers: find the line it is about in cartridge.yaml or handlers.py.

export type Marker = { line: number; message: string; severity?: "error" | "warning" };
export type Placed = { yaml: Marker[]; py: Marker[]; unmatched: string[] };

/** The line (1-based) where `name` appears, preferring a YAML key (`name:`) or a quoted use; 0 when absent. */
function findLine(text: string, name: string): number {
  const lines = text.split(/\r?\n/);
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tries = [
    new RegExp(`^\\s*["']?${esc}["']?\\s*:`),   // a YAML key
    new RegExp(`["']${esc}["']`),               // a quoted use (handlers, flow mappings)
    new RegExp(`(^|[^\\w.])${esc}($|[^\\w])`),  // anywhere, as a whole word
  ];
  for (const re of tries) {
    const i = lines.findIndex((l) => re.test(l));
    if (i >= 0) return i + 1;
  }
  return 0;
}

export function placeProblems(problems: string[], yaml: string, py: string): Placed {
  const out: Placed = { yaml: [], py: [], unmatched: [] };
  for (const p of problems) {
    const lineRef = /handlers\.py(?:[:,]|\s+line)\s*(\d+)/i.exec(p);
    if (lineRef) { out.py.push({ line: +lineRef[1], message: p }); continue; }
    const name = /['"`]([^'"`]+)['"`]/.exec(p)?.[1];
    const aboutHandlers = /handlers?\b|handlers\.py/i.test(p);
    if (name) {
      const order: ["yaml" | "py", string][] = aboutHandlers ? [["py", py], ["yaml", yaml]] : [["yaml", yaml], ["py", py]];
      const hit = order.map(([f, t]) => [f, findLine(t, name)] as const).find(([, l]) => l > 0);
      if (hit) { out[hit[0]].push({ line: hit[1], message: p }); continue; }
    }
    out.yaml.push({ line: 1, message: p });
    out.unmatched.push(p);
  }
  return out;
}
