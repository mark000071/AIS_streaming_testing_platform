import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.ENVSHIP_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": api,
      "/stream": { target: api, changeOrigin: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks: {
          map: ["maplibre-gl"],
          deck: ["@deck.gl/core", "@deck.gl/layers", "@deck.gl/mapbox"],
          charts: ["echarts"],
        },
      },
    },
  },
});
