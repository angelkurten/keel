// The self-hosted build (npm run build:wheel) has no Cartridge tab, so Monaco is left out of the wheel: this
// module replaces ./monaco/setup there, and CodeEditor keeps its plain-text fallback if anything asks for it.
throw new Error("the code editor is not part of the self-hosted console");
