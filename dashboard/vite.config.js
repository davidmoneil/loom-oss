import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output is served as static files by the FastAPI gateway from
// src/loom/dashboard/static. Relative base keeps asset URLs working
// regardless of the mount path.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/loom/dashboard/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:4444",
      "/health": "http://127.0.0.1:4444",
    },
  },
});
