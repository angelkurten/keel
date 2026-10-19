// Monaco, loaded on first use (a separate chunk), with a plain <pre> standing in while it loads or if it fails.
import { useEffect, useRef, useState } from "react";
import type { Marker } from "./problems";

type Setup = typeof import("./monaco/setup");
type Editor = import("monaco-editor").editor.IStandaloneCodeEditor;

let loading: Promise<Setup> | null = null;
const load = () => (loading ??= import("./monaco/setup"));
let seq = 0;

export function CodeEditor({ value, onChange, language, readOnly = false, markers = [], height = "50vh", path, onSave }: {
  value: string;
  onChange?: (v: string) => void;
  language: "yaml" | "python";
  readOnly?: boolean;
  markers?: Marker[];
  height?: string | number;
  /** The model's folder: a cartridge.yaml and a handlers.py under the same folder see each other (completions). */
  path?: string;
  /** Cmd/Ctrl+S. */
  onSave?: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [m, setM] = useState<Setup | null>(null);
  const [failed, setFailed] = useState(false);
  const editor = useRef<Editor | null>(null);
  const cb = useRef({ onChange, onSave });
  cb.current = { onChange, onSave };
  const folder = useRef(path || `e${++seq}`);

  useEffect(() => { load().then(setM, () => setFailed(true)); }, []);

  useEffect(() => {
    if (!m || !host.current) return;
    const { monaco, themeName } = m;
    const uri = monaco.Uri.from({ scheme: "file", path: `/${folder.current}/${language === "yaml" ? "cartridge.yaml" : "handlers.py"}` });
    monaco.editor.getModel(uri)?.dispose();
    const model = monaco.editor.createModel(value, language, uri);
    const ed = monaco.editor.create(host.current, {
      model, readOnly, theme: themeName(), automaticLayout: true, minimap: { enabled: false }, wordWrap: "on",
      fontFamily: "'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13, lineHeight: 20,
      scrollBeyondLastLine: false, tabSize: language === "python" ? 4 : 2, renderLineHighlight: readOnly ? "none" : "line",
      padding: { top: 10, bottom: 10 }, fixedOverflowWidgets: true, quickSuggestions: { other: true, strings: true, comments: false },
      wordBasedSuggestions: language === "yaml" ? "off" : "currentDocument",
    });
    ed.onDidChangeModelContent(() => cb.current.onChange?.(model.getValue()));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => cb.current.onSave?.());
    editor.current = ed;
    return () => { ed.dispose(); model.dispose(); editor.current = null; };
  }, [m, language, readOnly]); // eslint-disable-line react-hooks/exhaustive-deps

  // Outside changes (a file loaded, a new proposal) replace the text; typing does not come back here as a change.
  useEffect(() => {
    const model = editor.current?.getModel();
    if (model && model.getValue() !== value) model.setValue(value);
  }, [value]);

  useEffect(() => {
    const model = editor.current?.getModel();
    if (!m || !model) return;
    const S = m.monaco.MarkerSeverity;
    m.monaco.editor.setModelMarkers(model, "keel", markers.map((k) => {
      const line = Math.min(Math.max(1, k.line), model.getLineCount());
      return {
        startLineNumber: line, endLineNumber: line, startColumn: model.getLineFirstNonWhitespaceColumn(line) || 1,
        endColumn: model.getLineMaxColumn(line), message: k.message, severity: k.severity === "error" ? S.Error : S.Warning,
      };
    }));
  }, [m, markers]);

  return (
    <div className="monaco-box" style={{ height }}>
      {(!m || failed) && <pre className="monaco-fallback">{value || " "}</pre>}
      <div ref={host} className="monaco-host" style={{ display: m && !failed ? "block" : "none" }} />
    </div>
  );
}
