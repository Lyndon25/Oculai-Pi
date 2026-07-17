/**
 * Oculai Desktop — Electron Main Process Entry Point
 *
 * Lifecycle:
 * 1. Create BrowserWindow
 * 2. Start embedded PostgreSQL
 * 3. Start Python sidecar (JSONL server)
 * 4. Initialize Pi AgentSession with Oculai tools
 * 5. Register IPC handlers
 * 6. Ready for user interaction
 */
import { app, BrowserWindow, shell, screen } from "electron";
import { dirname } from "path";
import { fileURLToPath } from "url";
import { existsSync, mkdirSync } from "fs";
import { PostgresManager } from "./postgres-manager.js";
import { ToolBridge } from "./tool-bridge.js";
import { initPiSession, disposeSession } from "./pi-session.js";
import { registerIpcHandlers } from "./ipc-handlers.js";
import { stateBus } from "./state-bus.js";
import { getSettingsStore } from "./settings-store.js";
import { packagedPreloadEntry, packagedRendererEntry } from "./application-paths.js";

// Prevent multiple instances
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

let mainWindow: BrowserWindow | null = null;
const postgresManager = new PostgresManager();
const toolBridge = new ToolBridge();

const isDev = !app.isPackaged;
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Backend lifecycle state — prevents races between start and shutdown
let backendState: "stopped" | "starting" | "running" | "stopping" = "stopped";
let backendStartPromise: Promise<void> | null = null;
let backendStopPromise: Promise<void> | null = null;
let quitAfterShutdown = false;

function createWindow(): void {
  const { workAreaSize } = screen.getPrimaryDisplay();
  const initialWidth = Math.max(1024, Math.min(1400, workAreaSize.width - 48));
  const initialHeight = Math.max(680, Math.min(900, workAreaSize.height - 48));

  mainWindow = new BrowserWindow({
    width: initialWidth,
    height: initialHeight,
    minWidth: 1024,
    minHeight: 680,
    title: "Oculai Desktop",
    titleBarStyle: "hiddenInset",
    frame: process.platform === "darwin" ? false : true,
    webPreferences: {
      preload: packagedPreloadEntry(__dirname),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  stateBus.setWindow(mainWindow);

  mainWindow.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    const levelName = ["verbose", "info", "warning", "error"][level] ?? String(level);
    console.log(`[renderer:${levelName}] ${message} (${sourceId}:${line})`);
  });

  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    console.error(`[renderer:load-failed] ${errorCode} ${errorDescription} ${validatedURL}`);
  });

  mainWindow.webContents.on("render-process-gone", (_event, details) => {
    console.error(`[renderer:gone] ${details.reason} exitCode=${details.exitCode}`);
  });

  mainWindow.webContents.on("did-finish-load", () => {
    mainWindow?.webContents.executeJavaScript(
      "({ title: document.title, rootChildren: document.getElementById('root')?.childElementCount ?? -1, bodyText: document.body.innerText.slice(0, 300) })",
    ).then((snapshot: unknown) => {
      console.log(`[renderer:loaded] ${JSON.stringify(snapshot)}`);
    }).catch((err: unknown) => {
      console.error(`[renderer:snapshot-failed] ${err instanceof Error ? err.message : String(err)}`);
    });
  });

  // Load the renderer
  if (isDev) {
    mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    mainWindow.loadFile(packagedRendererEntry(__dirname));
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // Open external links in browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
}

async function startBackend(): Promise<void> {
  if (backendState !== "stopped") {
    stateBus.emitSystemLog("warn", `startBackend called while state=${backendState}, ignoring`);
    return;
  }
  backendState = "starting";
  let databaseReady = false;

  // 1. Start PostgreSQL
  try {
    stateBus.emitSystemStatus({ db: "connecting", python: "stopped", llm: "unconfigured" });
    await postgresManager.initialize();
    const dbConfig = postgresManager.getConfig();
    stateBus.emitSystemStatus({
      db: "connected",
      python: "stopped",
      llm: "unconfigured",
      dbPort: dbConfig.port,
    });
    stateBus.emitSystemLog("info", `PostgreSQL ready on port ${dbConfig.port}`);
    databaseReady = true;

    // Set DB env for Python sidecar
    process.env.DB_HOST = dbConfig.host;
    process.env.DB_PORT = String(dbConfig.port);
    process.env.DB_NAME = dbConfig.database;
    process.env.DB_USER = dbConfig.user;
    process.env.DB_PASSWORD = dbConfig.password;
  } catch (err) {
    stateBus.emitSystemLog("error", `PostgreSQL failed: ${err}`);
    stateBus.emitSystemStatus({ db: "error", python: "stopped", llm: "unconfigured" });
    // Continue without DB — user can configure later
  }

  // Propagate source API keys from the settings store to process.env so the
  // Python sidecar inherits them (tool-bridge.ts spawns with {...process.env}).
  // Only assign when a key is non-null, so an unset key never overwrites a
  // value already present in process.env (e.g. from the shell or .env).
  const sourceKeyEnvMap: Array<[string, string]> = [
    ["firecrawl", "FIRECRAWL_API_KEY"],
    ["tavily", "TAVILY_API_KEY"],
    ["exa", "EXA_API_KEY"],
    ["github", "GITHUB_TOKEN"],
    ["semantic_scholar", "SEMANTIC_SCHOLAR_API_KEY"],
    ["baidu", "BAIDU_API_KEY"],
  ];
  for (const [provider, envVar] of sourceKeyEnvMap) {
    try {
      const key = getSettingsStore().getApiKey(provider);
      if (key) process.env[envVar] = key;
    } catch (error) {
      stateBus.emitSystemLog(
        "error",
        `Credential '${provider}' is unavailable: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  // 2. Start Python sidecar
  const dbStatus = databaseReady ? "connected" as const : "error" as const;
  try {
    stateBus.emitSystemStatus({
      db: dbStatus,
      python: "starting",
      llm: "unconfigured",
    });

    await toolBridge.start();
    stateBus.emitSystemStatus({
      db: dbStatus,
      python: "ready",
      llm: "unconfigured",
      pythonPid: toolBridge.childPid,
    });
  } catch (err) {
    stateBus.emitSystemLog("error", `Python sidecar failed: ${err}`);
    stateBus.emitSystemStatus({ db: dbStatus, python: "error", llm: "unconfigured" });
    // Continue — user can retry
  }

  // 3. Initialize Pi session (if API keys configured)
  const settings = getSettingsStore();
  let apiKey: string | null = null;
  let credentialError: Error | null = null;
  try {
    apiKey = settings.getApiKey(settings.get("llmProvider"));
  } catch (error) {
    credentialError = error instanceof Error ? error : new Error(String(error));
    stateBus.emitSystemLog("error", credentialError.message);
  }
  if (apiKey) {
    try {
      await initPiSession(toolBridge);
      stateBus.emitSystemStatus({
        db: dbStatus,
        python: "ready",
        llm: "configured",
      });
    } catch (err) {
      stateBus.emitSystemLog("error", `Pi session init failed: ${err}`);
      stateBus.emitSystemStatus({ db: dbStatus, python: "ready", llm: "error" });
    }
  } else if (credentialError) {
    stateBus.emitSystemStatus({ db: dbStatus, python: "ready", llm: "error" });
  } else {
    stateBus.emitSystemLog("warn", "No API key configured. Set one in Settings to enable AI agent.");
    stateBus.emitSystemStatus({ db: dbStatus, python: "ready", llm: "unconfigured" });
  }

  // 4. IPC handlers are registered during app startup before slow backend work.

  backendState = "running";
  stateBus.emitSystemLog("info", "Oculai Desktop backend ready");
}

async function shutdownBackend(): Promise<void> {
  if (backendStopPromise) return backendStopPromise;
  if (backendState === "starting" && backendStartPromise) {
    try {
      await backendStartPromise;
    } catch {
      // Startup failures are reflected in backend state/status; cleanup still runs.
    }
  }
  if (backendState === "stopped") return;
  if (backendState === "stopping") return backendStopPromise ?? Promise.resolve();

  backendState = "stopping";
  backendStopPromise = (async () => {
    stateBus.emitSystemLog("info", "Shutting down...");
    try {
      await disposeSession();
    } catch (error) {
      stateBus.emitSystemLog("error", `Pi session shutdown failed: ${String(error)}`);
    }
    try {
      await toolBridge.stop();
    } catch (error) {
      stateBus.emitSystemLog("error", `Python sidecar shutdown failed: ${String(error)}`);
    }
    try {
      await postgresManager.stop();
    } catch (error) {
      stateBus.emitSystemLog("error", `PostgreSQL shutdown failed: ${String(error)}`);
    }
    stateBus.emitSystemLog("info", "Shutdown complete");
  })().finally(() => {
    backendState = "stopped";
    backendStopPromise = null;
  });
  return backendStopPromise;
}

// ---- App Lifecycle ----

app.whenReady().then(async () => {
  // Ensure user data directory exists
  const userData = app.getPath("userData");
  if (!existsSync(userData)) {
    mkdirSync(userData, { recursive: true });
  }

  createWindow();
  registerIpcHandlers(toolBridge, postgresManager);
  const closeDuringStartMs = Number(process.env.OCULAI_SMOKE_CLOSE_DURING_START_MS ?? 0);
  if (
    Number.isFinite(closeDuringStartMs)
    && closeDuringStartMs >= 100
    && closeDuringStartMs <= 300_000
  ) {
    console.log(`[smoke] scheduling close during backend startup in ${closeDuringStartMs}ms`);
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
    }, closeDuringStartMs);
  }
  backendStartPromise = startBackend();
  try {
    await backendStartPromise;
  } finally {
    backendStartPromise = null;
  }

  // Deterministic packaged-app lifecycle smoke hook.  It is disabled unless
  // the launcher explicitly supplies a bounded delay, and closes the window
  // through Electron so the normal shutdownBackend path is exercised.
  const smokeExitMs = Number(process.env.OCULAI_SMOKE_EXIT_MS ?? 0);
  if (Number.isFinite(smokeExitMs) && smokeExitMs >= 1000 && smokeExitMs <= 300_000) {
    console.log(`[smoke] scheduling graceful window close in ${smokeExitMs}ms`);
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
      else app.quit();
    }, smokeExitMs);
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", async () => {
  await shutdownBackend();
  if (process.platform !== "darwin") {
    quitAfterShutdown = true;
    app.quit();
  }
});

app.on("before-quit", (event) => {
  if (quitAfterShutdown || backendState === "stopped") return;
  event.preventDefault();
  void shutdownBackend().finally(() => {
    quitAfterShutdown = true;
    app.quit();
  });
});

// Handle second instance
app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});
