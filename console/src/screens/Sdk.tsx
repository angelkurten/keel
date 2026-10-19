import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Sdk as SdkFile, type SdkLanguage } from "../api";
import { SDK_LANGS, sdkExample } from "../codegen";
import { Code, CopyButton, download, Icon, store, Tabs } from "../ui";

const shaKey = (lang: string) => `keel.sdk.sha.${lang}`;

export function Sdk({ ctx }: { ctx: Ctx }) {
  const [lang, setLang] = useState<SdkLanguage>("typescript");
  const [files, setFiles] = useState<Partial<Record<SdkLanguage, SdkFile>>>({});
  const [err, setErr] = useState("");
  const [, setTick] = useState(0); // re-render after a download stores its hash
  const meta = SDK_LANGS.find((l) => l.id === lang)!;
  const file = files[lang];

  useEffect(() => {
    if (files[lang]) return;
    setErr("");
    api.sdk(lang).then((f) => setFiles((m) => ({ ...m, [lang]: f }))).catch((e) => setErr((e as Error).message));
  }, [lang]); // eslint-disable-line react-hooks/exhaustive-deps

  const stored = store.get("local", shaKey(lang));
  const fresh = file && stored ? stored === file.sha256 : null;

  function save() {
    if (!file) return;
    download(file.filename, file.code);
    store.set("local", shaKey(lang), file.sha256);
    setTick((n) => n + 1);
  }

  return (
    <div className="stack">
      <div>
        <h1 className="h1">SDK</h1>
        <p className="muted">A typed client generated from your cartridge: one function per event type, one helper per entity kind.</p>
      </div>
      {fresh === true && (
        <div className="banner ok-banner"><Icon name="check" size={14} /> <span>Up to date with the deployed cartridge.</span></div>
      )}
      {fresh === false && (
        <div className="banner warn-banner"><Icon name="warn" size={14} /> <span>The cartridge changed since you downloaded this file: download it again.</span></div>
      )}
      <Tabs label="Language" items={SDK_LANGS.map((l) => ({ id: l.id as SdkLanguage, name: l.name }))} value={lang} onChange={setLang} />
      <ol className="steps">
        <li className="card stack-s">
          <span className="step-title"><span className="num">1</span> Install the runtime</span>
          <div className="cmd"><code className="mono">{meta.install}</code><CopyButton text={meta.install} /></div>
        </li>
        <li className="card stack-s">
          <span className="step-title"><span className="num">2</span> Download your client</span>
          {err && <p className="err">{err}</p>}
          {!file && !err && <p className="muted small-text">Generating…</p>}
          {file && (
            <>
              <div className="row wrap">
                <button type="button" className="btn primary" onClick={save}><Icon name="download" size={14} /> Download {file.filename}</button>
                <CopyButton text={file.code} onCopied={() => { store.set("local", shaKey(lang), file.sha256); setTick((n) => n + 1); }} />
                <span className="hint">generated from your cartridge <span className="mono">{file.cartridge}</span></span>
              </div>
              <details className="preview">
                <summary className="small-text">Preview {file.filename}</summary>
                <pre className="code-pre">{file.code}</pre>
              </details>
              <p className="hint">
                In CI, keep it fresh with <span className="mono">keel sdk {lang} &lt;cartridge&gt; -o {file.filename} --check</span>: it exits 1 when the cartridge moved on.
              </p>
            </>
          )}
        </li>
        <li className="card stack-s">
          <span className="step-title"><span className="num">3</span> Emit an event and read the context</span>
          <Code title={lang === "go" ? "main.go" : lang === "php" ? "example.php" : lang === "python" ? "example.py" : "example.ts"}
            code={sdkExample(lang, ctx.cartridge, file?.filename || defaultName(lang, ctx.cartridge?.name || "app"), ctx.me.endpoint, ctx.tenant)} />
          <p className="hint">Set <span className="mono">KEEL_KEY</span> to a key from Keys or Connect.</p>
        </li>
      </ol>
    </div>
  );
}

function defaultName(lang: SdkLanguage, name: string): string {
  return { typescript: `keel-${name}.ts`, python: `keel_${name}.py`, go: `keel_${name}.go`, php: `${name[0].toUpperCase()}${name.slice(1)}.php` }[lang];
}
