import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.KEEL_CONSOLE_API || "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": backend } },
  build: { outDir: "dist" },
});
