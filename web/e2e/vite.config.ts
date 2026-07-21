import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/** Local-only Vite proxy for the isolated Playwright API process. */
const apiOrigin = `http://127.0.0.1:${process.env.E2E_API_PORT ?? "8012"}`;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": apiOrigin,
      "/health": apiOrigin,
      "/__e2e__": apiOrigin,
    },
  },
});
