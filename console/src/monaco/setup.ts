// Monaco for the Cartridge tab, loaded only through `import()` from CodeEditor: the editor core (no CDN, so the
// self-hosted wheel works offline), YAML with the cartridge's JSON Schema (monaco-yaml), Python highlighting, the
// console's themes and completions for handlers.py.
import { configureMonacoYaml } from "monaco-yaml";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import YamlWorker from "monaco-yaml/yaml.worker.js?worker";
import * as core from "./core";
import schema from "../../../docs/cartridge.schema.json";

export type Monaco = typeof import("monaco-editor");
export const monaco = core as unknown as Monaco;

(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker: (_id: string, label: string) => (label === "yaml" ? new YamlWorker() : new EditorWorker()),
};

configureMonacoYaml(monaco as never, {
  enableSchemaRequest: false,
  completion: true,
  hover: true,
  validate: true,
  schemas: [{ uri: "inmemory://keel/cartridge.schema.json", fileMatch: ["**/cartridge.yaml"], schema: schema as never }],
});

// --- themes: the console's tokens ------------------------------------------------------------------
monaco.editor.defineTheme("keel-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "comment", foreground: "6E7B88", fontStyle: "italic" },
    { token: "string", foreground: "8CC6FF" },
    { token: "keyword", foreground: "4EA8FF" },
    { token: "number", foreground: "E8C66A" },
    { token: "type", foreground: "6FD88A" },
  ],
  colors: {
    "editor.background": "#0B0F14",
    "editor.foreground": "#E6EDF3",
    "editorLineNumber.foreground": "#4A5663",
    "editorLineNumber.activeForeground": "#8B98A5",
    "editor.lineHighlightBackground": "#11161D",
    "editor.lineHighlightBorder": "#11161D",
    "editorCursor.foreground": "#4EA8FF",
    "editor.selectionBackground": "#4EA8FF33",
    "editor.inactiveSelectionBackground": "#4EA8FF1F",
    "editorIndentGuide.background1": "#1A222C",
    "editorWidget.background": "#161C24",
    "editorWidget.border": "#222B35",
    "editorSuggestWidget.background": "#161C24",
    "editorSuggestWidget.border": "#222B35",
    "editorSuggestWidget.selectedBackground": "#1A2330",
    "editorHoverWidget.background": "#161C24",
    "editorHoverWidget.border": "#222B35",
    "scrollbarSlider.background": "#2E394566",
    "editorGutter.background": "#0B0F14",
  },
});
monaco.editor.defineTheme("keel-light", {
  base: "vs",
  inherit: true,
  rules: [
    { token: "comment", foreground: "6E7781", fontStyle: "italic" },
    { token: "string", foreground: "0A3069" },
    { token: "keyword", foreground: "0969DA" },
    { token: "number", foreground: "9A6700" },
  ],
  colors: {
    "editor.background": "#FFFFFF",
    "editor.foreground": "#1F2328",
    "editorLineNumber.foreground": "#9AA4AE",
    "editorLineNumber.activeForeground": "#59636E",
    "editor.lineHighlightBackground": "#F6F8FA",
    "editor.lineHighlightBorder": "#F6F8FA",
    "editorCursor.foreground": "#0969DA",
    "editor.selectionBackground": "#0969DA26",
    "editorWidget.background": "#FFFFFF",
    "editorWidget.border": "#D8DEE4",
  },
});
const dark = window.matchMedia("(prefers-color-scheme: dark)");
export const themeName = () => (dark.matches ? "keel-dark" : "keel-light");
dark.addEventListener("change", () => monaco.editor.setTheme(themeName()));

// --- handlers.py completions ---------------------------------------------------------------------------
/** The event types declared under `events:` in a cartridge.yaml (top-level keys of that block). */
export function eventTypes(yaml: string): string[] {
  const out: string[] = [];
  let inside = false, indent = -1;
  for (const line of yaml.split(/\r?\n/)) {
    if (/^events:\s*(#.*)?$/.test(line)) { inside = true; continue; }
    if (!inside || !line.trim() || line.trim().startsWith("#")) continue;
    if (/^\S/.test(line)) break;
    const m = /^(\s+)(["']?)([\w.\-]+)\2\s*:/.exec(line);
    if (!m) continue;
    if (indent < 0) indent = m[1].length;
    if (m[1].length === indent) out.push(m[3]);
  }
  return out;
}

const PRIMITIVES: [string, string, string][] = [
  ["upsert_entity", 'store.upsert_entity(event.tenant, "${1:kind}", ${2:p["id"]}, ref=${3:None}${4:, name=p.get("name")})',
    "upsert_entity(tenant, kind, id, ref=None, **attrs) -> Entity"],
  ["link", 'store.link(event.tenant, ${1:event.subject}, "${2:relation}", ${3:dst}, event.occurred_at, source=event.source)',
    "link(tenant, src, rel, dst, valid_from, source=None) -> Relation"],
  ["unlink", 'store.unlink(event.tenant, ${1:src}, "${2:relation}", ${3:dst}, valid_to=event.occurred_at)',
    "unlink(tenant, src, rel, dst, valid_to) -> None"],
  ["open_fact", 'store.open_fact(event.tenant, ${1:event.subject}, "${2:predicate}", ${3:value}, valid_from=event.occurred_at, source=event.source, evidence=event.id)',
    "open_fact(tenant, subject, predicate, value, valid_from, **kw) -> Fact"],
  ["close_fact", 'store.close_fact(event.tenant, ${1:event.subject}, "${2:predicate}", valid_to=event.occurred_at)',
    "close_fact(tenant, subject, predicate, valid_to, status=None, scope=Scope.TENANT) -> None"],
];
const EVENT_ATTRS: [string, string][] = [
  ["tenant", "the tenant the event belongs to"], ["subject", "the subject key, `<kind>:<id>`"],
  ["payload", "the payload dict"], ["occurred_at", "world time (datetime, UTC)"], ["source", "where the event came from"],
  ["id", "the event id (use it as evidence)"], ["type", "the dotted event type"],
];
const fnName = (t: string) => t.replace(/[^\w]+/g, "_").replace(/^(\d)/, "_$1");

monaco.languages.registerCompletionItemProvider("python", {
  triggerCharacters: [".", "@", '"'],
  provideCompletionItems(model, position) {
    const word = model.getWordUntilPosition(position);
    const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
    const before = model.getLineContent(position.lineNumber).slice(0, position.column - 1);
    const K = monaco.languages.CompletionItemKind, Snip = monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet;
    if (/\bevent\.\w*$/.test(before)) {
      return { suggestions: EVENT_ATTRS.map(([a, doc]) => ({ label: a, kind: K.Property, insertText: a, documentation: doc, range })) };
    }
    if (/\bstore\.\w*$/.test(before)) {
      return { suggestions: PRIMITIVES.map(([name, snip, sig]) => ({
        label: name, kind: K.Method, insertText: snip.replace(/^store\./, ""), insertTextRules: Snip, detail: sig, range })) };
    }
    // the cartridge.yaml next to this handlers.py (same folder in the model URI)
    const dir = model.uri.path.replace(/\/[^/]*$/, "");
    const yaml = monaco.editor.getModel(monaco.Uri.from({ scheme: model.uri.scheme, path: `${dir}/cartridge.yaml` }))?.getValue() || "";
    const types = eventTypes(yaml);
    const onDecorator = types.map((t) => ({
      label: `@registry.on("${t}")`, kind: K.Snippet, detail: "handler for a cataloged event",
      insertText: `@registry.on("${t}")\ndef ${fnName(t)}(event, store):\n    p = event.payload\n    $0`,
      insertTextRules: Snip, filterText: `@registry.on("${t}")`,
      range: before.trim().startsWith("@") ? new monaco.Range(position.lineNumber, before.indexOf("@") + 1, position.lineNumber, position.column) : range,
    }));
    return {
      suggestions: [
        ...onDecorator,
        ...PRIMITIVES.map(([name, snip, sig]) => ({ label: `store.${name}`, kind: K.Function, insertText: snip, insertTextRules: Snip, detail: sig, range })),
      ],
    };
  },
});
