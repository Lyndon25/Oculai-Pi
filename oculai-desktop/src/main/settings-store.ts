/**
 * Settings Store — persistent user configuration using electron-store.
 *
 * Stores: LLM provider/model, API keys (encrypted via safeStorage), source toggles,
 * database preferences, and advanced settings.
 */
import { app, safeStorage } from "electron";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join } from "path";

export interface AppSettings {
  // LLM
  llmProvider: string;
  llmModel: string;
  thinkingLevel: "off" | "low" | "medium" | "high";

  // API keys (stored only with OS-backed encryption; never exposed to renderer)
  apiKeys: Record<string, string>;

  // Main-process-only encrypted secrets (never exposed to renderer).
  internalSecrets: Record<string, string>;

  // Source toggles
  enabledSources: Record<string, boolean>;

  // Database
  dbPort: number;
  dbAutoStart: boolean;

  // Advanced
  maxIterations: number;
  tokenBudget: number;
  concurrency: number;
}

export type SafeAppSettings = Omit<AppSettings, "apiKeys" | "internalSecrets"> & {
  apiKeyStatus: Record<string, boolean>;
};

const DEFAULT_SETTINGS: AppSettings = {
  llmProvider: "anthropic",
  llmModel: "claude-sonnet-4-20250514",
  thinkingLevel: "medium",

  apiKeys: {},
  internalSecrets: {},

  enabledSources: {
    arxiv: true,
    dblp: true,
    github: true,
    semantic_scholar: true,
    openalex: true,
    industry: true,
    acl_anthology: true,
    pmlr: true,
    conference: true,
    baidu_scholar: true,
    baidu: true,
    personal_homepage: true,
    juejin: true,
    zhihu: true,
    csdn: true,
    duckduckgo: true,
    firecrawl: true,
  },

  dbPort: 0, // 0 = auto-assign
  dbAutoStart: true,

  maxIterations: 50,
  tokenBudget: 500000,
  concurrency: 4,
};

function settingsPath(): string {
  const userData = app.getPath("userData");
  if (!existsSync(userData)) {
    mkdirSync(userData, { recursive: true });
  }
  return join(userData, "oculai-settings.json");
}

export class SettingsStore {
  private settings: AppSettings;

  constructor() {
    this.settings = this.load();
  }

  private load(): AppSettings {
    try {
      const path = settingsPath();
      if (existsSync(path)) {
        const raw = readFileSync(path, "utf-8");
        const parsed = JSON.parse(raw) as Partial<AppSettings>;
        return {
          ...DEFAULT_SETTINGS,
          ...parsed,
          apiKeys: {
            ...DEFAULT_SETTINGS.apiKeys,
            ...(parsed.apiKeys ?? {}),
          },
          internalSecrets: {
            ...DEFAULT_SETTINGS.internalSecrets,
            ...(parsed.internalSecrets ?? {}),
          },
          enabledSources: {
            ...DEFAULT_SETTINGS.enabledSources,
            ...(parsed.enabledSources ?? {}),
          },
        };
      }
    } catch {
      // Use defaults on any error
    }
    return {
      ...DEFAULT_SETTINGS,
      apiKeys: { ...DEFAULT_SETTINGS.apiKeys },
      internalSecrets: { ...DEFAULT_SETTINGS.internalSecrets },
      enabledSources: { ...DEFAULT_SETTINGS.enabledSources },
    };
  }

  save(): void {
    // API keys stored here are encrypted by Electron safeStorage when available.
    // getAll() below still strips them before data reaches the renderer.
    writeFileSync(settingsPath(), JSON.stringify(this.settings, null, 2), "utf-8");
  }

  getAll(): SafeAppSettings {
    // Never expose OS-encrypted API key values to the renderer.
    const { apiKeys, internalSecrets: _internalSecrets, ...safe } = this.settings;
    return {
      ...safe,
      enabledSources: { ...safe.enabledSources },
      apiKeyStatus: Object.fromEntries(
        Object.keys(DEFAULT_SETTINGS.apiKeys)
          .concat(["anthropic", "openai", "deepseek", "zhipu", "github", "semantic_scholar", "baidu", "tavily", "exa", "firecrawl"])
          .map((provider) => [provider, Boolean(apiKeys[provider])]),
      ),
    };
  }

  /** Get all settings including apiKeys — only for internal main-process use. */
  getAllInternal(): AppSettings {
    return { ...this.settings };
  }

  get<K extends keyof AppSettings>(key: K): AppSettings[K] {
    return this.settings[key];
  }

  set<K extends keyof AppSettings>(key: K, value: AppSettings[K]): void {
    (this.settings as unknown as Record<string, unknown>)[key] = value;
    this.save();
  }

  // ---- API key management with encryption ----

  setApiKey(provider: string, key: string): void {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("OS secure storage is unavailable; refusing to persist API credentials");
    }
    const encrypted = safeStorage.encryptString(key);
    this.settings.apiKeys[provider] = encrypted.toString("base64");
    this.save();
  }

  getApiKey(provider: string): string | null {
    const stored = this.settings.apiKeys[provider];
    if (!stored) return null;
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("OS secure storage is unavailable; cannot decrypt API credentials");
    }
    try {
      return safeStorage.decryptString(Buffer.from(stored, "base64"));
    } catch (error) {
      throw new Error(
        `Stored credential for '${provider}' is corrupt or uses the retired Base64 fallback. ` +
        `Remove and re-enter the credential: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  /** Persist a main-process secret. Unlike legacy API-key fallback behavior,
   * this strict API refuses to store plaintext-equivalent data. */
  setInternalSecret(name: string, value: string): void {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("OS secure storage is unavailable; refusing to persist internal secret");
    }
    this.settings.internalSecrets[name] = safeStorage.encryptString(value).toString("base64");
    this.save();
  }

  getInternalSecret(name: string): string | null {
    const stored = this.settings.internalSecrets[name];
    if (!stored) return null;
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("OS secure storage is unavailable; cannot decrypt internal secret");
    }
    try {
      return safeStorage.decryptString(Buffer.from(stored, "base64"));
    } catch (error) {
      throw new Error(
        `Failed to decrypt internal secret '${name}': ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  isSourceEnabled(name: string): boolean {
    return this.settings.enabledSources[name] ?? true;
  }
}

let _store: SettingsStore | null = null;

export function getSettingsStore(): SettingsStore {
  if (!_store) {
    _store = new SettingsStore();
  }
  return _store;
}
