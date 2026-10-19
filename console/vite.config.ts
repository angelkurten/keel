import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.KEEL_CONSOLE_API || "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  base: "./", // relative to <base href>: "/" hosted, "/ui/" under `keel serve --ui`
  server: { proxy: { "/api": backend, "/v1": backend, "/health": backend } },
  build: { outDir: "dist" },
});
