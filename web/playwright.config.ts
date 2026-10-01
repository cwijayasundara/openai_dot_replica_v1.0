import { defineConfig, devices } from "@playwright/test";

// The UI against the scripted API (tests/support/web_e2e_server.py): real routes,
// graph, policy and approvals; a scripted model and no network.
const API_PORT = 8766;
const WEB_PORT = 3100;

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "line" : "list",
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run python -m tests.support.web_e2e_server ${API_PORT}`,
      cwd: "..",
      env: { PYTHONPATH: "src:." },
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `pnpm exec next dev -p ${WEB_PORT}`,
      env: { DOT_API_URL: `http://127.0.0.1:${API_PORT}`, DOT_WEB_DIST_DIR: ".next-e2e" },
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});

export const E2E_API = `http://127.0.0.1:${API_PORT}`;
