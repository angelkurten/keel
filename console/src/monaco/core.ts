// The editor without its bundled languages (edcore.main = editor.main minus the CSS/HTML/JSON/TypeScript
// language services and their megabytes of workers), plus the two languages the Cartridge tab needs.
import "monaco-editor/esm/vs/basic-languages/python/python.contribution.js";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js";
export * from "monaco-editor/esm/vs/editor/edcore.main.js";
