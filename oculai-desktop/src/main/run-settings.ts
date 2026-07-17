import type { AppSettings } from "./settings-store.js";

export interface RunSettingsSnapshot {
  provider: string;
  modelName: string;
  thinkingLevel: AppSettings["thinkingLevel"];
  apiKey: string;
  enabledSources: Readonly<Record<string, boolean>>;
  maxIterations: number;
  tokenBudget: number;
  concurrency: number;
}

export function createRunSettingsSnapshot(
  settings: Pick<
    AppSettings,
    "llmProvider" | "llmModel" | "thinkingLevel" | "enabledSources" |
    "maxIterations" | "tokenBudget" | "concurrency"
  >,
  apiKey: string,
): RunSettingsSnapshot {
  if (!apiKey) throw new Error(`No API key configured for provider '${settings.llmProvider}'`);
  return Object.freeze({
    provider: settings.llmProvider,
    modelName: settings.llmModel,
    thinkingLevel: settings.thinkingLevel,
    apiKey,
    enabledSources: Object.freeze({ ...settings.enabledSources }),
    maxIterations: Math.max(1, settings.maxIterations),
    tokenBudget: Math.max(1, settings.tokenBudget),
    concurrency: Math.max(1, settings.concurrency),
  });
}

export function isSnapshotSourceEnabled(snapshot: RunSettingsSnapshot, name: string): boolean {
  return snapshot.enabledSources[name] ?? true;
}
