/** Run-isolated Pi AgentSession orchestration with real child AgentSessions. */
import "./runtime-compat.js";
import { app } from "electron";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { Type, type TSchema } from "typebox";
import type {
  AgentSession,
  AgentSessionEvent,
  ResourceLoader,
  ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { getSettingsStore } from "./settings-store.js";
import { stateBus } from "./state-bus.js";
import { type ToolBridge } from "./tool-bridge.js";
import { getOculaiSystemPrompt } from "../shared/prompts.js";
import { OCULAI_TOOLS } from "./generated-tools.js";
import { listAgentProfiles, type AgentProfile } from "./agent-profiles.js";
import { isAgentToolAllowed } from "./agent-tool-policy.js";
import {
  createRunSettingsSnapshot,
  isSnapshotSourceEnabled,
  type RunSettingsSnapshot,
} from "./run-settings.js";
import { SchedulerRefreshGate } from "./scheduler-refresh-gate.js";
import {
  SubagentScheduler,
  type SubagentRequest,
  type SubagentResult,
} from "./subagent-scheduler.js";

type PiSdk = typeof import("@earendil-works/pi-coding-agent");

interface RunExecution {
  runId: string;
  agentId: string;
  state: "starting" | "running" | "aborting";
  session: AgentSession | null;
  promise: Promise<void>;
  settings: RunSettingsSnapshot;
}

interface SessionContext {
  runId: string;
  agentId: string;
  profile?: AgentProfile;
  allowSubagents: boolean;
  settings: RunSettingsSnapshot;
}

interface RunBudget {
  tokens: number;
  turns: number;
  maxTokens: number;
  maxTurns: number;
  exceeded?: string;
}

async function resolveConfiguredModel(provider: string, modelName: string) {
  const { getModel } = await import("@earendil-works/pi-ai/compat");
  // The SDK types enumerate built-in model ids at compile time while Settings
  // stores the same values dynamically. Keep the cast at this validation edge.
  const dynamicGetModel = getModel as unknown as (
    providerName: string,
    configuredModelName: string,
  ) => ReturnType<typeof getModel> | undefined;
  return dynamicGetModel(provider, modelName);
}

export async function validatePiModel(provider: string, modelName: string): Promise<void> {
  if (!await resolveConfiguredModel(provider, modelName)) {
    throw new Error(`Model not found: ${provider}/${modelName}`);
  }
}

let sdkPromise: Promise<PiSdk> | null = null;
let manager: PiSessionManager | null = null;

function loadPiRuntime(): Promise<PiSdk> {
  sdkPromise ??= import("@earendil-works/pi-coding-agent");
  return sdkPromise;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function textFromToolResult(value: unknown): Record<string, unknown> {
  const record = asRecord(value);
  const content = record.content;
  if (!Array.isArray(content)) return record;
  const first = asRecord(content[0]);
  if (typeof first.text !== "string") return record;
  try {
    return JSON.parse(first.text) as Record<string, unknown>;
  } catch {
    return { text: first.text };
  }
}

export class PiSessionManager {
  private readonly executions = new Map<string, RunExecution>();
  private readonly budgets = new Map<string, RunBudget>();
  private scheduler: SubagentScheduler;
  private readonly schedulerRefresh = new SchedulerRefreshGate();
  private disposed = false;
  private agentDir = "";

  constructor(private readonly bridge: ToolBridge) {
    this.scheduler = this.createScheduler();
  }

  async initialize(): Promise<void> {
    const sdk = await loadPiRuntime();
    const settings = getSettingsStore();
    const provider = settings.get("llmProvider");
    const modelName = settings.get("llmModel");
    await validatePiModel(provider, modelName);
    if (!settings.getApiKey(provider)) {
      throw new Error(`No API key configured for provider '${provider}'`);
    }
    const userData = app.getPath("userData");
    this.agentDir = join(userData, "pi-agent");
    if (!existsSync(this.agentDir)) mkdirSync(this.agentDir, { recursive: true });
    // Touch the SDK import here so Settings reports "configured" only after
    // runtime compatibility and all required constructors are actually loaded.
    void sdk;
  }

  hasActiveRun(runId: string): boolean {
    return this.executions.has(runId);
  }

  listActiveRuns(): string[] {
    return Array.from(this.executions.keys());
  }

  startRun(runId: string, prompt: string): Promise<void> {
    if (this.disposed) return Promise.reject(new Error("Pi session manager is disposed"));
    if (this.executions.has(runId)) {
      return Promise.reject(new Error(`Run '${runId}' is already active`));
    }

    const store = getSettingsStore();
    const provider = store.get("llmProvider");
    const settings = createRunSettingsSnapshot({
      llmProvider: provider,
      llmModel: store.get("llmModel"),
      thinkingLevel: store.get("thinkingLevel"),
      enabledSources: store.get("enabledSources"),
      maxIterations: store.get("maxIterations"),
      tokenBudget: store.get("tokenBudget"),
      concurrency: store.get("concurrency"),
    }, store.getApiKey(provider) ?? "");
    const execution: RunExecution = {
      runId,
      agentId: `${runId}:orchestrator`,
      state: "starting",
      session: null,
      promise: Promise.resolve(),
      settings,
    };
    this.executions.set(runId, execution); // reserve synchronously: prevents re-entry races
    this.budgets.set(runId, {
      tokens: 0,
      turns: 0,
      maxTokens: settings.tokenBudget,
      maxTurns: settings.maxIterations,
    });
    execution.promise = this.executeRun(execution, prompt);
    return execution.promise;
  }

  async abortRun(runId: string): Promise<boolean> {
    const execution = this.executions.get(runId);
    if (!execution) return false;
    execution.state = "aborting";
    this.scheduler.cancelRun(runId);
    await execution.session?.abort();
    try {
      await execution.promise;
    } catch {
      // The owner of startRun receives and persists the terminal state.
    }
    return true;
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    const runIds = this.listActiveRuns();
    await Promise.allSettled(runIds.map((runId) => this.abortRun(runId)));
  }

  /** New settings are read for every new session; update concurrency immediately when idle. */
  refreshSettings(): void {
    if (!this.schedulerRefresh.request(this.executions.size)) {
      stateBus.emitSystemLog("info", "Agent settings saved; active runs keep their current model until resumed");
      return;
    }
    this.scheduler = this.createScheduler();
  }

  private createScheduler(): SubagentScheduler {
    const concurrency = Math.max(1, getSettingsStore().get("concurrency"));
    return new SubagentScheduler(
      concurrency,
      async (context) => this.executeChildSession(context),
      {
        spawned: ({ runId, agentId, agent, target }) => {
          stateBus.emitSubagentSpawned(runId, agentId, agent, target);
        },
        progress: ({ runId, agentId, agent, message }) => {
          stateBus.emitSubagentProgress(runId, agentId, {
            timestamp: new Date().toISOString(),
            agentId,
            agentType: agent,
            action: "think",
            message,
          });
        },
        completed: ({ runId, agentId, agent, target, status, output, error }) => {
          stateBus.emitSubagentCompleted(
            runId,
            agentId,
            agent,
            target,
            status,
            output ? 1 : 0,
            error,
          );
        },
      },
    );
  }

  private async executeRun(execution: RunExecution, prompt: string): Promise<void> {
    try {
      const session = await this.createSession({
        runId: execution.runId,
        agentId: execution.agentId,
        allowSubagents: true,
        settings: execution.settings,
      });
      execution.session = session;
      if (this.executions.get(execution.runId)?.state === "aborting") {
        await session.abort();
        throw this.abortError(`Run '${execution.runId}' was aborted while starting`);
      }
      execution.state = "running";
      await session.prompt(prompt);
      this.assertSessionSucceeded(session, `Run '${execution.runId}'`);
      if (this.executions.get(execution.runId)?.state === "aborting") {
        throw this.abortError(`Run '${execution.runId}' was aborted`);
      }
      const budgetError = this.budgets.get(execution.runId)?.exceeded;
      if (budgetError) throw new Error(budgetError);
    } finally {
      execution.session?.dispose();
      execution.session = null;
      this.executions.delete(execution.runId);
      this.budgets.delete(execution.runId);
      if (!this.disposed && this.schedulerRefresh.consumeWhenIdle(this.executions.size)) {
        this.scheduler = this.createScheduler();
      }
    }
  }

  private async executeChildSession(context: {
    runId: string;
    agentId: string;
    profile: AgentProfile;
    task: string;
    signal: AbortSignal;
  }): Promise<string> {
    const runSettings = this.executions.get(context.runId)?.settings;
    if (!runSettings) throw this.abortError(`Run '${context.runId}' is no longer active`);
    const session = await this.createSession({
      runId: context.runId,
      agentId: context.agentId,
      profile: context.profile,
      allowSubagents: false,
      settings: runSettings,
    });
    let output = "";
    const unsubscribe = session.subscribe((event) => {
      if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
        output += event.assistantMessageEvent.delta;
      }
    });
    const abort = () => void session.abort();
    context.signal.addEventListener("abort", abort, { once: true });
    try {
      if (context.signal.aborted) throw this.abortError("Subagent was cancelled before start");
      await session.prompt(
        `Run ID: ${context.runId}\nAgent ID: ${context.agentId}\n\nDelegated task:\n${context.task}`,
      );
      this.assertSessionSucceeded(session, `Subagent '${context.agentId}'`);
      if (context.signal.aborted) throw this.abortError("Subagent was cancelled");
      return output;
    } finally {
      context.signal.removeEventListener("abort", abort);
      unsubscribe();
      session.dispose();
    }
  }

  private async createSession(context: SessionContext): Promise<AgentSession> {
    const sdk = await loadPiRuntime();
    const { provider, modelName } = context.settings;
    const modelRuntime = await sdk.ModelRuntime.create({
      authPath: join(this.agentDir, "auth.json"),
      modelsPath: join(this.agentDir, "models.json"),
    });
    await modelRuntime.setRuntimeApiKey(provider, context.settings.apiKey);
    const model = modelRuntime.getModel(provider, modelName)
      ?? await resolveConfiguredModel(provider, modelName);
    if (!model) throw new Error(`Model not found: ${provider}/${modelName}`);
    const settingsManager = sdk.SettingsManager.inMemory({
      compaction: { enabled: true },
      retry: { enabled: true, maxRetries: 3 },
    });

    const systemPrompt = [
      getOculaiSystemPrompt(),
      `Current run_id is '${context.runId}'. Current agent_id is '${context.agentId}'.`,
      "Never access or mutate another run. Tool arguments are runtime-enforced to this run.",
      context.profile?.systemPrompt,
    ].filter(Boolean).join("\n\n");

    const resourceLoader: ResourceLoader = {
      getExtensions: () => ({ extensions: [], errors: [], runtime: sdk.createExtensionRuntime() }),
      getSkills: () => ({ skills: [], diagnostics: [] }),
      getPrompts: () => ({ prompts: [], diagnostics: [] }),
      getThemes: () => ({ themes: [], diagnostics: [] }),
      getAgentsFiles: () => ({
        agentsFiles: listAgentProfiles().map((profile) => ({
          path: `${profile.name}.md`,
          content: `# ${profile.description}\n\n${profile.systemPrompt}`,
        })),
      }),
      getSystemPrompt: () => systemPrompt,
      getAppendSystemPrompt: () => [],
      extendResources: () => undefined,
      reload: async () => undefined,
    };

    const customTools = this.createToolDefinitions(context);
    if (context.allowSubagents) customTools.push(this.createSubagentTool(context.runId));
    const { session } = await sdk.createAgentSession({
      cwd: app.getPath("userData"),
      agentDir: this.agentDir,
      model,
      thinkingLevel: context.settings.thinkingLevel,
      modelRuntime,
      resourceLoader,
      customTools,
      noTools: "builtin",
      sessionManager: sdk.SessionManager.inMemory(app.getPath("userData")),
      settingsManager,
    });
    this.subscribeToSession(session, context);
    return session;
  }

  private createToolDefinitions(context: SessionContext): ToolDefinition[] {
    return Object.entries(OCULAI_TOOLS)
      // This governance decision is human-only. Excluding it here is the hard
      // boundary; the trusted Renderer -> IPC path invokes it directly.
      .filter(([name]) => isAgentToolAllowed(name))
      .map(([name, schema]) => ({
      name,
      label: name.replace(/^oculai_/, "").replaceAll("_", " "),
      description: schema.description,
      parameters: schema.parameters as TSchema,
      executionMode: PARALLEL_SAFE_TOOLS.has(name) ? "parallel" as const : "sequential" as const,
      execute: async (
        _toolCallId: string,
        rawParams: unknown,
        signal: AbortSignal | undefined,
      ) => {
        try {
          const params = this.enforceToolContext(name, schema.parameters, asRecord(rawParams), context);
          const result = await this.bridge.callTool(name, params, { signal });
          return {
            content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
            details: undefined,
          };
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          return {
            content: [{ type: "text" as const, text: `Error: ${message}` }],
            isError: true,
            details: undefined,
          };
        }
      },
      }));
  }

  private enforceToolContext(
    toolName: string,
    schema: Record<string, unknown>,
    rawParams: Record<string, unknown>,
    context: SessionContext,
  ): Record<string, unknown> {
    const budgetError = this.budgets.get(context.runId)?.exceeded;
    if (budgetError) throw new Error(budgetError);
    const params = Object.fromEntries(
      Object.entries(rawParams).map(([key, value]) => [key, this.parseStructuredValue(value)]),
    );
    const properties = asRecord(schema.properties);
    if ("run_id" in properties) {
      if (params.run_id && String(params.run_id) !== context.runId) {
        throw new Error(`Tool '${toolName}' attempted cross-run access`);
      }
      params.run_id = context.runId;
    }
    if ("agent_id" in properties) params.agent_id = context.agentId;
    if ("assessor_agent" in properties) params.assessor_agent = context.agentId;

    const sourceName = typeof params.source_name === "string" ? params.source_name : undefined;
    if (sourceName && !isSnapshotSourceEnabled(context.settings, sourceName)) {
      throw new Error(`Data source '${sourceName}' is disabled in Settings`);
    }
    const provider = typeof params.provider === "string" ? params.provider : undefined;
    if (provider && !isSnapshotSourceEnabled(context.settings, provider)) {
      throw new Error(`Search provider '${provider}' is disabled in Settings`);
    }
    if (toolName === "oculai_search_web" && !provider) {
      const selected = ["exa", "tavily", "firecrawl"]
        .find((candidate) => isSnapshotSourceEnabled(context.settings, candidate));
      if (!selected) throw new Error("All web search providers are disabled in Settings");
      params.provider = selected;
    }
    if (toolName === "oculai_deep_search") this.applyDeepSearchSourcePolicy(params, context.settings);
    return params;
  }

  private parseStructuredValue(value: unknown): unknown {
    if (typeof value !== "string") return value;
    const trimmed = value.trim();
    if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return value;
    try {
      return JSON.parse(trimmed) as unknown;
    } catch {
      return value;
    }
  }

  private applyDeepSearchSourcePolicy(
    params: Record<string, unknown>,
    settings: RunSettingsSnapshot,
  ): void {
    const enabledSources = Object.entries(settings.enabledSources)
      .filter(([, enabled]) => enabled)
      .map(([name]) => name);
    if (enabledSources.length === 0) throw new Error("All data sources are disabled in Settings");
    const enabled = new Set(enabledSources);
    const hypotheses = Array.isArray(params.hypotheses)
      ? params.hypotheses.map((value) => ({ ...asRecord(value) }))
      : [];
    for (const hypothesis of hypotheses) {
      if (Array.isArray(hypothesis.source_priority)) {
        hypothesis.source_priority = hypothesis.source_priority.filter(
          (source) => typeof source === "string" && enabled.has(source),
        );
      }
      const initialQueries = asRecord(hypothesis.initial_queries);
      hypothesis.initial_queries = Object.fromEntries(
        Object.entries(initialQueries).filter(([source]) => enabled.has(source)),
      );
    }
    params.hypotheses = hypotheses;

    const config = { ...asRecord(params.config) };
    const requestedBudget = asRecord(config.source_call_budget);
    config.source_call_budget = Object.fromEntries(
      enabledSources.map((source) => [source, requestedBudget[source] ?? 30]),
    );
    config.max_concurrent_sources = Math.min(
      typeof config.max_concurrent_sources === "number" ? config.max_concurrent_sources : enabledSources.length,
      settings.concurrency,
    );
    params.config = config;
  }

  private createSubagentTool(runId: string): ToolDefinition {
    const task = Type.Object({
      agent: Type.String(),
      task: Type.String(),
      target: Type.Optional(Type.String()),
    });
    const parameters = Type.Object({
      agent: Type.Optional(Type.String()),
      task: Type.Optional(Type.String()),
      target: Type.Optional(Type.String()),
      tasks: Type.Optional(Type.Array(task, { maxItems: 16 })),
      chain: Type.Optional(Type.Array(task, { maxItems: 16 })),
    });
    return {
      name: "subagent",
      label: "Subagent",
      description: "Delegate one task, parallel independent tasks, or a sequential chain to isolated Oculai AgentSessions.",
      parameters,
      executionMode: "parallel",
      execute: async (_toolCallId, params, signal) => {
        try {
          const results = await this.scheduler.execute(runId, params as SubagentRequest, signal);
          return {
            content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
            details: { results } satisfies { results: SubagentResult[] },
          };
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          return {
            content: [{ type: "text", text: `Subagent error: ${message}` }],
            isError: true,
            details: undefined,
          };
        }
      },
    };
  }

  private subscribeToSession(session: AgentSession, context: SessionContext): void {
    session.subscribe((event: AgentSessionEvent) => {
      if (event.type === "message_update") {
        const message = event.assistantMessageEvent;
        if (message.type === "text_delta") {
          stateBus.emitMessage(context.runId, context.agentId, message.delta);
        } else if (message.type === "thinking_delta") {
          stateBus.emitThinking(context.runId, context.agentId, message.delta);
        }
      } else if (event.type === "tool_execution_start") {
        const params = asRecord(event.args);
        stateBus.emitToolCall(context.runId, context.agentId, event.toolName, params);
        this.emitToolStart(context, event.toolName, params);
      } else if (event.type === "tool_execution_end") {
        const result = textFromToolResult(event.result);
        stateBus.emitToolResult(context.runId, context.agentId, event.toolName, result, event.isError);
        this.emitToolEnd(context, event.toolName, asRecord(event.result), result, event.isError);
      } else if (event.type === "turn_end") {
        const budget = this.budgets.get(context.runId);
        if (budget) {
          budget.turns += 1;
          if (budget.turns > budget.maxTurns) {
            this.exceedBudget(context.runId, `Run exceeded maxIterations (${budget.maxTurns})`);
          }
        }
      } else if (event.type === "message_end") {
        const message = asRecord(event.message);
        if (message.role !== "assistant") return;
        const usage = asRecord(message.usage);
        const input = typeof usage.input === "number" ? usage.input : 0;
        const output = typeof usage.output === "number" ? usage.output : 0;
        const budget = this.budgets.get(context.runId);
        if (budget) {
          budget.tokens += input + output;
          if (budget.tokens > budget.maxTokens) {
            this.exceedBudget(context.runId, `Run exceeded tokenBudget (${budget.maxTokens})`);
          }
        }
      }
    });
  }

  private exceedBudget(runId: string, reason: string): void {
    const budget = this.budgets.get(runId);
    if (!budget || budget.exceeded) return;
    budget.exceeded = reason;
    stateBus.emitRunError(runId, reason, "budget");
    this.scheduler.cancelRun(runId);
    void this.executions.get(runId)?.session?.abort();
  }

  private emitToolStart(context: SessionContext, name: string, params: Record<string, unknown>): void {
    const phase = TOOL_PHASES[name];
    if (phase) stateBus.emitPhaseChange(context.runId, phase);
    const action = TOOL_ACTIONS[name];
    if (!action) return;
    stateBus.emitSubagentProgress(context.runId, context.agentId, {
      timestamp: new Date().toISOString(),
      agentId: context.agentId,
      agentType: context.profile?.description ?? "Orchestrator",
      action,
      message: `Started ${name.replace(/^oculai_/, "").replaceAll("_", " ")}`,
      detail: typeof params.source_name === "string" ? params.source_name : undefined,
    });
  }

  private emitToolEnd(
    context: SessionContext,
    name: string,
    params: Record<string, unknown>,
    result: Record<string, unknown>,
    isError: boolean,
  ): void {
    const action = isError ? "error" : TOOL_ACTIONS[name];
    if (action) {
      stateBus.emitSubagentProgress(context.runId, context.agentId, {
        timestamp: new Date().toISOString(),
        agentId: context.agentId,
        agentType: context.profile?.description ?? "Orchestrator",
        action,
        message: isError ? `${name} failed` : `${name} completed`,
      });
    }
    if (!isError && name === "oculai_upsert_candidate") {
      const personData = asRecord(params.person_data);
      const personId = String(result.person_id ?? "");
      if (personId) {
        stateBus.emitCandidateUpserted(
          context.runId,
          personId,
          String(personData.name ?? result.name ?? "Unknown candidate"),
          typeof personData.institution === "string" ? personData.institution : undefined,
          typeof params.source_name === "string" ? params.source_name : undefined,
        );
      }
    }
  }

  private abortError(message: string): Error {
    const error = new Error(message);
    error.name = "AbortError";
    return error;
  }

  private assertSessionSucceeded(session: AgentSession, label: string): void {
    let assistant: Record<string, unknown> | null = null;
    for (let index = session.messages.length - 1; index >= 0; index -= 1) {
      const message = asRecord(session.messages[index]);
      if (message.role === "assistant") {
        assistant = message;
        break;
      }
    }
    if (!assistant) throw new Error(`${label} ended without an assistant response`);

    const stopReason = String(assistant.stopReason ?? "");
    const detail = typeof assistant.errorMessage === "string" && assistant.errorMessage.trim()
      ? `: ${assistant.errorMessage}`
      : "";
    if (stopReason === "aborted") throw this.abortError(`${label} was aborted${detail}`);
    if (stopReason === "error") throw new Error(`${label} model response failed${detail}`);
    if (stopReason === "length") throw new Error(`${label} stopped at the model output limit${detail}`);
  }
}

const TOOL_PHASES: Record<string, Parameters<typeof stateBus.emitPhaseChange>[1]> = {
  oculai_create_run: "init",
  oculai_list_source_capabilities: "strategy",
  oculai_checkpoint_plan: "strategy",
  oculai_search_source: "searching",
  oculai_deep_search: "searching",
  oculai_link_identity: "identity_resolution",
  oculai_get_candidate: "enrichment",
  oculai_attach_evidence: "enrichment",
  oculai_record_assessment: "evaluation",
  oculai_score_candidate: "evaluation",
  oculai_create_review_session: "audit",
  oculai_finalize_review_session: "shortlist",
  oculai_export_report: "complete",
};

const PARALLEL_SAFE_TOOLS: ReadonlySet<string> = new Set([
  "oculai_search_source",
  "oculai_fetch_source_detail",
  "oculai_search_web",
  "oculai_firecrawl_scrape",
  "oculai_crawl_site",
  "oculai_get_candidate",
  "oculai_get_evidence",
  "oculai_get_evidence_by_tier",
  "oculai_get_run_state",
  "oculai_get_search_progress",
  "oculai_get_review_progress",
  "oculai_get_broadcasts",
  "oculai_list_source_capabilities",
  "oculai_check_approval_status",
  "oculai_list_pending_approvals",
]);

const TOOL_ACTIONS: Record<string, Parameters<typeof stateBus.emitSubagentProgress>[2]["action"]> = {
  oculai_record_iteration: "think",
  oculai_search_source: "search",
  oculai_deep_search: "search",
  oculai_broadcast_discovery: "broadcast",
  oculai_upsert_candidate: "upsert",
  oculai_upsert_candidates_batch: "upsert",
  oculai_attach_evidence: "found",
  oculai_record_assessment: "score",
  oculai_score_candidate: "score",
  oculai_create_review_session: "audit",
  oculai_finalize_review_session: "audit",
  oculai_export_report: "export",
};

export async function initPiSession(bridge: ToolBridge): Promise<void> {
  if (manager) await manager.dispose();
  const next = new PiSessionManager(bridge);
  await next.initialize();
  manager = next;
  stateBus.emitSystemLog("info", "Pi multi-session runtime initialized");
}

export function getPiSessionManager(): PiSessionManager | null {
  return manager;
}

export async function disposeSession(): Promise<void> {
  const current = manager;
  manager = null;
  await current?.dispose();
}
