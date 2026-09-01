/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api/v1/case-bundles": "http://localhost:18080",
      "/api/v1/preflight": "http://localhost:18080",
      "/api/v1/campaigns": "http://localhost:18080",
      "/api/v1/artifacts": "http://localhost:18080",
      "/api/v1/matrices": "http://localhost:18080",
      "/api": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
  },
});
