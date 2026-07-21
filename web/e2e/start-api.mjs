import { mkdirSync, rmSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const projectDirectory = resolve(scriptDirectory, "..", "..");
const apiPort = process.env.E2E_API_PORT ?? "8012";
const webPort = process.env.E2E_WEB_PORT ?? "5176";
const managedDataRoot = resolve(projectDirectory, "data");
const requestedDataDirectory = process.env.E2E_DATA_DIR;
const dataDirectory = requestedDataDirectory
  ? resolve(
      isAbsolute(requestedDataDirectory)
        ? requestedDataDirectory
        : resolve(projectDirectory, requestedDataDirectory),
    )
  : resolve(managedDataRoot, "e2e-playwright");
const relativeDataDirectory = relative(managedDataRoot, dataDirectory);
const isManagedE2eDirectory =
  relativeDataDirectory.length > 0 &&
  !isAbsolute(relativeDataDirectory) &&
  relativeDataDirectory !== ".." &&
  !relativeDataDirectory.startsWith(`..${sep}`) &&
  relativeDataDirectory.split(sep)[0]?.startsWith("e2e-");

if (!isManagedE2eDirectory) {
  throw new Error(
    "E2E_DATA_DIR must be a managed data/e2e-* directory inside this repository.",
  );
}

if (process.env.E2E_KEEP_DATA !== "1") {
  rmSync(dataDirectory, { force: true, recursive: true });
}
mkdirSync(dataDirectory, { recursive: true });

const python = process.env.PYTHON ?? "python";
const child = spawn(
  python,
  [
    "-m",
    "uvicorn",
    "e2e.playwright_app:app",
    "--host",
    "127.0.0.1",
    "--port",
    apiPort,
    "--log-level",
    "warning",
    "--no-access-log",
  ],
  {
    cwd: projectDirectory,
    env: {
      ...process.env,
      E2E_DATA_DIR: dataDirectory,
      E2E_PUBLIC_APP_URL: process.env.E2E_PUBLIC_APP_URL ?? `http://127.0.0.1:${webPort}`,
      PYTHONUTF8: "1",
    },
    stdio: "inherit",
    windowsHide: true,
  },
);

let stopping = false;
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (stopping) return;
    stopping = true;
    child.kill(signal);
  });
}

child.on("error", (error) => {
  console.error(`Unable to start local E2E API: ${error.message}`);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (!stopping && code !== 0) {
    console.error(`Local E2E API exited unexpectedly (${signal ?? code ?? "unknown"}).`);
  }
  process.exit(code ?? 1);
});
