/** Concurrent, restartable JSONL transport for the Python Oculai sidecar. */
import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { createInterface, type Interface } from "node:readline";
import { stateBus } from "./state-bus.js";

export interface ToolResponse {
  ok: boolean;
  result?: Record<string, unknown>;
  error?: { code: string; message: string; traceback?: string };
}

export interface ToolCallOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
}

export interface ToolBridgeOptions {
  defaultTimeoutMs?: number;
  maxInFlight?: number;
  maxQueue?: number;
  restartDelayMs?: number;
  readyTimeoutMs?: number;
}

interface LaunchConfig {
  command: string;
  args: string[];
}

interface QueuedRequest {
  id: string;
  method: string;
  params: Record<string, unknown>;
  options: Required<Pick<ToolCallOptions, "timeoutMs">> & Pick<ToolCallOptions, "signal">;
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: Error) => void;
  abortListener?: () => void;
}

interface PendingRequest extends QueuedRequest {
  timer: NodeJS.Timeout;
}

const DEFAULT_TIMEOUT_MS = 120_000;
const SHUTDOWN_TIMEOUT_MS = 10_000;

function abortError(message: string): Error {
  const error = new Error(message);
  error.name = "AbortError";
  return error;
}

function defaultLaunchConfig(serverModule?: string): LaunchConfig {
  if (serverModule) {
    const python = process.platform === "win32" ? "python" : "python3";
    return { command: python, args: [serverModule] };
  }

  // Packaged builds ship a self-contained sidecar. Development deliberately
  // falls back to the active Python environment.
  const resourcesPath = process.resourcesPath;
  if (resourcesPath) {
    const executable = join(
      resourcesPath,
      "runtime",
      "python",
      process.platform === "win32" ? "oculai-sidecar.exe" : "oculai-sidecar",
    );
    if (existsSync(executable)) return { command: executable, args: [] };
  }

  return {
    command: process.platform === "win32" ? "python" : "python3",
    args: ["-m", "oculai_mcp.jsonl_server"],
  };
}

export class ToolBridge {
  private child: ChildProcess | null = null;
  private stdoutReader: Interface | null = null;
  private stderrReader: Interface | null = null;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly queue: QueuedRequest[] = [];
  private requestCounter = 0;
  private controlCounter = 0;
  private ready = false;
  private shouldRun = false;
  private startPromise: Promise<void> | null = null;
  private stopPromise: Promise<void> | null = null;
  private restartTimer: NodeJS.Timeout | null = null;
  private launchConfig: LaunchConfig | null = null;
  private sidecarPid: number | undefined;

  private readonly defaultTimeoutMs: number;
  private readonly maxInFlight: number;
  private readonly maxQueue: number;
  private readonly restartDelayMs: number;
  private readonly readyTimeoutMs: number;

  constructor(options: ToolBridgeOptions = {}) {
    this.defaultTimeoutMs = options.defaultTimeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxInFlight = Math.max(1, options.maxInFlight ?? 32);
    this.maxQueue = Math.max(0, options.maxQueue ?? 256);
    this.restartDelayMs = Math.max(0, options.restartDelayMs ?? 1_000);
    this.readyTimeoutMs = Math.max(100, options.readyTimeoutMs ?? 15_000);
  }

  get childPid(): number | undefined {
    return this.ready ? (this.sidecarPid ?? this.child?.pid) : undefined;
  }

  /** Start a sidecar and remember its launch command for automatic restart. */
  async start(pythonCmd?: string, serverModule?: string): Promise<void> {
    if (pythonCmd) {
      const isStandalone = !serverModule && /oculai-sidecar(?:\.exe)?$/i.test(pythonCmd);
      this.launchConfig = {
        command: pythonCmd,
        args: serverModule ? [serverModule] : isStandalone ? [] : ["-m", "oculai_mcp.jsonl_server"],
      };
    } else if (!this.launchConfig) {
      this.launchConfig = defaultLaunchConfig(serverModule);
    }

    this.shouldRun = true;
    if (this.ready && this.child) return;
    if (this.startPromise) return this.startPromise;

    this.startPromise = this.spawnAndWaitForReady().finally(() => {
      this.startPromise = null;
      if (this.shouldRun && !this.ready && !this.child) this.scheduleRestart();
    });
    return this.startPromise;
  }

  private async spawnAndWaitForReady(): Promise<void> {
    if (this.child) return this.waitForReady(this.readyTimeoutMs);
    const launch = this.launchConfig ?? defaultLaunchConfig();
    this.launchConfig = launch;
    stateBus.emitSystemLog("info", `Starting Python sidecar: ${launch.command} ${launch.args.join(" ")}`);

    const child = spawn(launch.command, launch.args, {
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    this.child = child;
    this.ready = false;
    this.sidecarPid = undefined;

    this.stdoutReader = createInterface({ input: child.stdout! });
    this.stdoutReader.on("line", (line) => this.handleResponseLine(line));

    this.stderrReader = createInterface({ input: child.stderr! });
    this.stderrReader.on("line", (line) => this.handleSystemLine(line));

    child.once("error", (error) => {
      stateBus.emitSystemLog("error", `Python sidecar error: ${error.message}`);
      this.handleExit(child, null, error);
    });
    child.once("close", (code) => this.handleExit(child, code, undefined));

    try {
      await this.waitForReady(this.readyTimeoutMs);
      this.drainQueue();
    } catch (error) {
      if (this.child === child && !child.killed) child.kill("SIGKILL");
      throw error;
    }
  }

  private handleResponseLine(line: string): void {
    if (!line.trim()) return;
    try {
      const response = JSON.parse(line) as ToolResponse & { id?: string };
      if (!response.id) return;
      const request = this.pending.get(response.id);
      if (!request) return; // late response to a timed out/cancelled request
      this.pending.delete(response.id);
      clearTimeout(request.timer);
      this.cleanupAbortListener(request);

      if (!response.ok) {
        const error = response.error ?? { code: "UNKNOWN", message: "Unknown sidecar error" };
        const failure = new Error(`Tool '${request.method}' failed: [${error.code}] ${error.message}`);
        if (error.code === "CANCELLED") failure.name = "AbortError";
        request.reject(failure);
      } else {
        request.resolve(response.result ?? {});
      }
      this.drainQueue();
    } catch (error) {
      stateBus.emitSystemLog(
        "warn",
        `Ignored invalid sidecar stdout: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  private handleSystemLine(line: string): void {
    if (!line.trim()) return;
    try {
      const message = JSON.parse(line) as Record<string, unknown>;
      if (message.type === "ready") {
        this.ready = true;
        const reportedPid = Number(message.pid);
        this.sidecarPid = Number.isSafeInteger(reportedPid) && reportedPid > 0
          ? reportedPid
          : this.child?.pid;
        stateBus.emitSystemLog(
          "info",
          `Python sidecar ready: ${String(message.tools)} tools, pid ${String(message.pid)}`,
        );
        this.drainQueue();
      } else if (message.type === "shutdown") {
        this.ready = false;
        stateBus.emitSystemLog("info", `Python sidecar shutdown: ${String(message.reason)}`);
      }
    } catch {
      stateBus.emitSystemLog("debug", `[python] ${line}`);
    }
  }

  private handleExit(child: ChildProcess, code: number | null, cause?: Error): void {
    if (this.child !== child) return;
    this.child = null;
    this.ready = false;
    this.sidecarPid = undefined;
    this.stdoutReader?.close();
    this.stderrReader?.close();
    this.stdoutReader = null;
    this.stderrReader = null;

    const reason = cause?.message ?? `exit code ${String(code)}`;
    stateBus.emitSystemLog(
      !this.shouldRun && !cause && code === 0 ? "info" : "warn",
      `Python sidecar stopped (${reason})`,
    );
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      this.cleanupAbortListener(request);
      request.reject(new Error(`Python sidecar exited while running '${request.method}' (${reason})`));
    }
    this.pending.clear();

    if (this.shouldRun) this.scheduleRestart();
  }

  private scheduleRestart(): void {
    if (this.restartTimer || this.startPromise || !this.shouldRun) return;
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      void this.start().catch((error: unknown) => {
        stateBus.emitSystemLog(
          "error",
          `Python sidecar restart failed: ${error instanceof Error ? error.message : String(error)}`,
        );
        this.scheduleRestart();
      });
    }, this.restartDelayMs);
  }

  private waitForReady(timeoutMs: number): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.ready) {
        resolve();
        return;
      }
      const startedAt = Date.now();
      const timer = setInterval(() => {
        if (this.ready) {
          clearInterval(timer);
          resolve();
        } else if (!this.child) {
          clearInterval(timer);
          reject(new Error("Python sidecar exited before becoming ready"));
        } else if (Date.now() - startedAt >= timeoutMs) {
          clearInterval(timer);
          reject(new Error(`Python sidecar did not become ready within ${timeoutMs}ms`));
        }
      }, 25);
    });
  }

  /** Queue a tool call with explicit timeout, cancellation, and backpressure. */
  async callTool(
    method: string,
    params: Record<string, unknown> = {},
    optionsOrTimeout: ToolCallOptions | number = {},
  ): Promise<Record<string, unknown>> {
    const options = typeof optionsOrTimeout === "number"
      ? { timeoutMs: optionsOrTimeout }
      : optionsOrTimeout;
    if (options.signal?.aborted) throw abortError(`Tool '${method}' was cancelled before dispatch`);

    if (!this.ready || !this.child) {
      await this.start();
    }

    if (this.queue.length >= this.maxQueue && this.pending.size >= this.maxInFlight) {
      throw new Error(
        `Python sidecar backpressure limit reached (${this.maxInFlight} active, ${this.maxQueue} queued)`,
      );
    }

    const id = `req-${++this.requestCounter}`;
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const request: QueuedRequest = {
        id,
        method,
        params,
        options: { timeoutMs: options.timeoutMs ?? this.defaultTimeoutMs, signal: options.signal },
        resolve,
        reject,
      };
      if (options.signal) {
        request.abortListener = () => this.cancelRequest(request, "aborted by caller");
        options.signal.addEventListener("abort", request.abortListener, { once: true });
      }
      this.queue.push(request);
      this.drainQueue();
    });
  }

  private drainQueue(): void {
    if (!this.ready || !this.child?.stdin?.writable) return;
    while (this.pending.size < this.maxInFlight && this.queue.length > 0) {
      const request = this.queue.shift()!;
      if (request.options.signal?.aborted) {
        this.cleanupAbortListener(request);
        request.reject(abortError(`Tool '${request.method}' was cancelled before dispatch`));
        continue;
      }

      const timer = setTimeout(() => {
        this.cancelRequest(request, `timed out after ${request.options.timeoutMs}ms`);
      }, request.options.timeoutMs);
      this.pending.set(request.id, { ...request, timer });

      const line = JSON.stringify({ id: request.id, method: request.method, params: request.params }) + "\n";
      this.child.stdin.write(line, (error) => {
        if (!error) return;
        const active = this.pending.get(request.id);
        if (!active) return;
        clearTimeout(active.timer);
        this.pending.delete(request.id);
        this.cleanupAbortListener(active);
        active.reject(new Error(`Failed to write tool '${request.method}' to sidecar: ${error.message}`));
        this.drainQueue();
      });
    }
  }

  private cancelRequest(request: QueuedRequest, reason: string): void {
    const queuedIndex = this.queue.findIndex((item) => item.id === request.id);
    if (queuedIndex >= 0) {
      const [queued] = this.queue.splice(queuedIndex, 1);
      this.cleanupAbortListener(queued);
      queued.reject(abortError(`Tool '${queued.method}' ${reason}`));
      return;
    }

    const active = this.pending.get(request.id);
    if (!active) return;
    clearTimeout(active.timer);
    this.pending.delete(request.id);
    this.cleanupAbortListener(active);
    active.reject(abortError(`Tool '${active.method}' ${reason}`));
    this.sendCancelFrame(active.id);
    this.drainQueue();
  }

  private sendCancelFrame(requestId: string): void {
    if (!this.ready || !this.child?.stdin?.writable) return;
    const line = JSON.stringify({
      id: `cancel-${++this.controlCounter}`,
      method: "$cancel",
      params: { request_id: requestId },
    }) + "\n";
    this.child.stdin.write(line);
  }

  private cleanupAbortListener(request: QueuedRequest): void {
    if (request.abortListener && request.options.signal) {
      request.options.signal.removeEventListener("abort", request.abortListener);
    }
  }

  isReady(): boolean {
    return this.ready;
  }

  getLoad(): { active: number; queued: number; maxInFlight: number; maxQueue: number } {
    return {
      active: this.pending.size,
      queued: this.queue.length,
      maxInFlight: this.maxInFlight,
      maxQueue: this.maxQueue,
    };
  }

  /** Gracefully stop and reject queued work. Automatic restart is disabled. */
  async stop(): Promise<void> {
    this.shouldRun = false;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (this.stopPromise) return this.stopPromise;

    for (const request of this.queue.splice(0)) {
      this.cleanupAbortListener(request);
      request.reject(abortError(`Tool '${request.method}' cancelled because sidecar is stopping`));
    }
    const child = this.child;
    if (!child) return;

    this.stopPromise = new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        if (!child.killed) child.kill("SIGKILL");
        resolve();
      }, SHUTDOWN_TIMEOUT_MS);
      child.once("close", () => {
        clearTimeout(timeout);
        resolve();
      });
      child.stdin?.end();
    }).finally(() => {
      this.stopPromise = null;
    });

    return this.stopPromise;
  }
}
