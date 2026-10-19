import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.KEEL_CONSOLE_API || "http://127.0.0.1:8080";

const stub = new URL("./src/monaco/stub.ts", import.meta.url).pathname;

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  // the wheel (`--mode wheel`) has no Cartridge tab: Monaco stays out of it (~4.8 MB)
  resolve: { alias: mode === "wheel" ? [{ find: /^\.\/monaco\/setup$/, replacement: stub }] : [] },
  base: "./", // relative to <base href>: "/" hosted, "/ui/" under `keel serve --ui`
  server: { fs: { allow: [".."] }, proxy: { "/api": backend, "/v1": backend, "/health": backend } },
  // Monaco (the Cartridge tab) is one lazy ~4 MB chunk by itself; the main chunk stays ~250 kB.
  build: { outDir: "dist", chunkSizeWarningLimit: 4500 },
}));
