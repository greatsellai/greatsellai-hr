import { defineConfig, devices } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webDirectory = dirname(fileURLToPath(import.meta.url));
const projectDirectory = resolve(webDirectory, "..");
const apiPort = process.env.E2E_API_PORT ?? "8012";
const webPort = process.env.E2E_WEB_PORT ?? "5176";
const webOrigin = `http://127.0.0.1:${webPort}`;

/**
 * Browser tests deliberately use a separate local ASGI launcher. They never
 * connect to a deployed server, a production database, IMAP, or an AI vendor.
 */
export default defineConfig({
  testDir: resolve(webDirectory, "e2e"),
  testMatch: /.*\.spec\.ts/,
  outputDir: resolve(webDirectory, "test-results"),
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  forbidOnly: Boolean(process.env.CI),
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: resolve(webDirectory, "playwright-report") }],
  ],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: webOrigin,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  webServer: [
    {
      command: "node web/e2e/start-api.mjs",
      cwd: projectDirectory,
      url: `http://127.0.0.1:${apiPort}/health`,
      timeout: 60_000,
      reuseExistingServer: false,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort --config e2e/vite.config.ts`,
      cwd: webDirectory,
      url: webOrigin,
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
