// monaco-editor 0.52 ships types for its main entry only; the ESM entry points used here have the same API.
declare module "monaco-editor/esm/vs/editor/edcore.main.js" {
  export * from "monaco-editor";
}
declare module "monaco-editor/esm/vs/basic-languages/*";
