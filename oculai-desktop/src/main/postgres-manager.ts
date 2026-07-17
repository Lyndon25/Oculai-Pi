/**
 * PostgreSQL Manager — manages an embedded PostgreSQL 16 + pgvector instance.
 *
 * Uses pg_ctl for lifecycle management. On first launch, initializes a data
 * directory and runs schema migration SQL files.
 */
import { app } from "electron";
import { ChildProcess, spawn } from "child_process";
import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:net";
import { existsSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "fs";
import { delimiter, dirname, join } from "path";
import { stateBus } from "./state-bus.js";
import { getSettingsStore } from "./settings-store.js";
import { allowedSourcesForStatus, RUN_STATUS_TRANSITIONS, type PersistedRunStatus } from "./run-lifecycle.js";
import { ensureDatabaseExists } from "./database-bootstrap.js";
import { initDbPasswordFile } from "./database-paths.js";

export interface PostgresConfig {
  port: number;
  host: string;
  user: string;
  password: string;
  database: string;
}

export class PostgresManager {
  private process: ChildProcess | null = null;
  private ownsServer = false;
  private dataDir: string;
  private binDir: string | null;
  private config: PostgresConfig = {
    port: 0,
    host: "localhost",
    user: "oculai",
    password: "",
    database: "oculai",
  };

  constructor() {
    const userData = app.getPath("userData");
    this.dataDir = join(userData, "postgres", "data");
    const packagedRuntime = process.resourcesPath
      ? join(process.resourcesPath, "runtime", "postgres")
      : "";
    const configuredBin = process.env.OCULAI_POSTGRES_BIN;
    this.binDir = configuredBin && existsSync(configuredBin)
      ? configuredBin
      : packagedRuntime && existsSync(join(packagedRuntime, "bin"))
        ? join(packagedRuntime, "bin")
        : null; // Development fallback: pg_ctl/psql resolved from PATH.
  }

  getConfig(): PostgresConfig {
    return { ...this.config };
  }

  /** Full lifecycle: init if needed, start, migrate schema. */
  async initialize(): Promise<void> {
    const settings = getSettingsStore();

    // Determine port
    this.config.port = settings.get("dbPort") || (await this.findFreePort());

    if (!settings.get("dbAutoStart")) {
      this.config = {
        host: process.env.DB_HOST || "localhost",
        port: Number(process.env.DB_PORT || settings.get("dbPort") || 5432),
        database: process.env.DB_NAME || "oculai",
        user: process.env.DB_USER || "oculai",
        password: process.env.DB_PASSWORD || "",
      };
      stateBus.emitSystemLog("info", `dbAutoStart disabled; connecting to PostgreSQL on port ${this.config.port}`);
      if (!await this.healthCheck()) {
        throw new Error(
          `dbAutoStart is disabled and PostgreSQL is not reachable on ${this.config.host}:${this.config.port}`,
        );
      }
      await this.runSchemaMigration(false);
      return;
    }

    // Ensure directories exist
    if (!existsSync(this.dataDir)) {
      mkdirSync(this.dataDir, { recursive: true });
    }

    // Initialize data directory if needed
    const isNew = !existsSync(join(this.dataDir, "PG_VERSION"));
    let password = settings.getInternalSecret("embeddedPostgresPassword");
    if (isNew && !password) {
      password = randomBytes(32).toString("base64url");
      settings.setInternalSecret("embeddedPostgresPassword", password);
    } else if (!isNew && !password) {
      throw new Error(
        "Existing embedded PostgreSQL data has no securely stored password. " +
        "Migrate the database credentials or back up and reset the embedded postgres/data directory; " +
        "the retired default password is intentionally not retried.",
      );
    }
    if (!password) throw new Error("Embedded PostgreSQL password initialization failed");
    this.config.password = password;
    if (isNew) {
      stateBus.emitSystemLog("info", "Initializing PostgreSQL data directory...");
      await this.initDB();
    }

    // Start PostgreSQL
    stateBus.emitSystemLog("info", `Starting PostgreSQL on port ${this.config.port}...`);
    await this.start();
    this.ownsServer = true;

    // initdb creates the postgres/template databases, not the application DB.
    // Bootstrap it through the admin database before any health/schema call
    // attempts to connect to `oculai`.
    const createdDatabase = await ensureDatabaseExists(
      this.config.database,
      (sql) => this.querySQL(sql, "postgres"),
      (sql) => this.execSQL(sql, "postgres"),
    );
    if (createdDatabase) stateBus.emitSystemLog("info", `Created PostgreSQL database '${this.config.database}'`);

    // Run incremental schema migration on every startup
    await this.runSchemaMigration(isNew);
  }

  private async initDB(): Promise<void> {
    const passwordFile = this.writePwFile();
    try {
      await new Promise<void>((resolve, reject) => {
        const proc = spawn(
          this.initDbPath(),
          [
            "-D", this.dataDir,
            "--username", this.config.user,
            "--pwfile", passwordFile,
            "--encoding=UTF8",
            "--locale=C",
          ],
          { shell: false, stdio: "pipe", windowsHide: true, env: this.postgresEnv() },
        );

        let stderr = "";
        proc.stderr.on("data", (d) => (stderr += d.toString()));

        proc.on("close", (code) => {
          if (code === 0) {
            stateBus.emitSystemLog("info", "PostgreSQL data directory initialized");
            resolve();
          } else {
            reject(new Error(`initdb failed (code ${code}): ${stderr}`));
          }
        });

        proc.on("error", reject);
      });
    } finally {
      try { unlinkSync(passwordFile); } catch { /* already removed or never created */ }
    }
  }

  private writePwFile(): string {
    const nonce = `${process.pid}-${randomBytes(12).toString("hex")}`;
    const pwPath = initDbPasswordFile(this.dataDir, nonce);
    writeFileSync(pwPath, this.config.password, { encoding: "utf-8", mode: 0o600, flag: "wx" });
    return pwPath;
  }

  private async start(): Promise<void> {
    return new Promise((resolve, reject) => {
      const logPath = join(this.dataDir, "..", "pg.log");
      this.process = spawn(
        this.pgCtlPath(),
        [
          "start",
          "-D", this.dataDir,
          "-l", logPath,
          "-o", `-p ${this.config.port}`,
        ],
        // A daemonized postgres process can inherit pg_ctl's stdout/stderr
        // handles on Windows. Capturing those handles in pipes makes Node wait
        // forever for EOF after pg_ctl exits. PostgreSQL already writes its
        // diagnostics to logPath, so discard pg_ctl's inherited stdio here.
        { shell: false, stdio: "ignore", windowsHide: true, env: this.postgresEnv() }
      );

      const timeout = setTimeout(() => {
        reject(new Error("PostgreSQL start timed out"));
      }, 30000);

      this.process.on("close", (code) => {
        clearTimeout(timeout);
        if (code === 0) {
          stateBus.emitSystemLog("info", `PostgreSQL started on port ${this.config.port}`);
          resolve();
        } else {
          let diagnostics = "";
          try {
            diagnostics = readFileSync(logPath, "utf-8").slice(-4000);
          } catch {
            // The log may not exist when pg_ctl fails before postgres starts.
          }
          reject(new Error(`pg_ctl start failed (code ${code}): ${diagnostics}`));
        }
      });

      this.process.on("error", (err) => {
        clearTimeout(timeout);
        reject(err);
      });
    });
  }

  /** Stop PostgreSQL gracefully. Always spawns pg_ctl stop regardless of internal state. */
  async stop(): Promise<void> {
    // this.process references the pg_ctl start process, which exits immediately
    // after daemonizing PG. We intentionally do NOT guard on `!this.process` here
    // because we always want to issue pg_ctl stop against the running daemon.
    if (!this.ownsServer) return;
    stateBus.emitSystemLog("info", "Stopping PostgreSQL...");

    return new Promise((resolve) => {
      const stopper = spawn(
        this.pgCtlPath(),
        ["stop", "-D", this.dataDir, "-m", "fast"],
        { shell: false, stdio: "pipe", windowsHide: true, env: this.postgresEnv() }
      );

      const timeout = setTimeout(() => {
        stopper.kill("SIGKILL");
        resolve();
      }, 15000);

      stopper.on("close", () => {
        clearTimeout(timeout);
        stateBus.emitSystemLog("info", "PostgreSQL stopped");
        this.ownsServer = false;
        resolve();
      });

      stopper.on("error", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
  }

  /** Run incremental schema SQL migration files. Only applies previously-unapplied migrations. */
  private async runSchemaMigration(isNew: boolean): Promise<void> {
    const schemaDir = this.findSchemaDir();
    if (!schemaDir || !existsSync(schemaDir)) {
      const message = "Schema directory not found; database migration cannot be verified or applied";
      if (app.isPackaged || getSettingsStore().get("dbAutoStart")) {
        throw new Error(message);
      }
      stateBus.emitSystemLog("warn", `${message}. External development database is left unchanged.`);
      return;
    }

    // Find migrations directory (check both schema/ and schema/migrations/)
    const migrationsDir = join(schemaDir, "migrations");
    const migrationsExist = existsSync(migrationsDir);

    // 1. For fresh installs, run baseline schema files first (idempotent CREATE IF NOT EXISTS)
    if (isNew) {
      const baselineFiles = readdirSync(schemaDir)
        .filter((f) => f.endsWith(".sql") && !f.startsWith("postgresql"))
        .sort();

      stateBus.emitSystemLog("info", `Running ${baselineFiles.length} baseline schema files...`);

      for (const file of baselineFiles) {
        // Skip migration files in the schema root (they're in the migrations/ subdir)
        if (file.startsWith("00") && file.includes("migration")) continue;
        const sql = readFileSync(join(schemaDir, file), "utf-8");
        await this.execSQL(sql);
        stateBus.emitSystemLog("debug", `  ✓ ${file}`);
      }
    }

    // 2. Ensure schema_version tracking table exists
    const ensureTracking = `
      CREATE TABLE IF NOT EXISTS schema_version (
        version     TEXT PRIMARY KEY,
        applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        checksum    TEXT,
        description TEXT
      );
      ALTER TABLE schema_version ADD COLUMN IF NOT EXISTS checksum TEXT;
      ALTER TABLE schema_version ADD COLUMN IF NOT EXISTS description TEXT;
    `;
    await this.execSQL(ensureTracking);

    // 3. Run incremental migrations from schema/migrations/
    if (migrationsExist) {
      const migrationFiles = readdirSync(migrationsDir)
        .filter((f) => f.endsWith(".sql"))
        .sort();

      if (migrationFiles.length === 0) {
        stateBus.emitSystemLog("info", "No migration files found in migrations/");
        return;
      }

      // Validate the exact bytes of every already-applied migration. Older
      // installations may have NULL checksums; they are backfilled once so any
      // subsequent same-version rewrite is rejected deterministically.
      const appliedResult = await this.querySQL(
        "SELECT version, COALESCE(checksum, '') FROM schema_version ORDER BY version",
      );
      const applied = new Map<string, string>();
      for (const line of appliedResult.split(/\r?\n/).filter(Boolean)) {
        const separator = line.indexOf("|");
        applied.set(separator >= 0 ? line.slice(0, separator) : line, separator >= 0 ? line.slice(separator + 1) : "");
      }

      const migrationChecksums = new Map<string, string>();
      for (const file of migrationFiles) {
        const version = file.replace(/\.sql$/, "");
        const bytes = readFileSync(join(migrationsDir, file));
        const checksum = createHash("sha256").update(bytes).digest("hex");
        migrationChecksums.set(version, checksum);
        const stored = applied.get(version);
        if (stored === undefined) continue;
        const escapedVersion = version.replaceAll("'", "''");
        if (!stored) {
          await this.execSQL(
            `UPDATE schema_version SET checksum = '${checksum}' WHERE version = '${escapedVersion}' AND checksum IS NULL`,
          );
          applied.set(version, checksum);
          stateBus.emitSystemLog("warn", `Backfilled missing checksum for migration ${version}`);
        } else if (stored !== checksum) {
          throw new Error(
            `Migration checksum mismatch for '${version}': applied=${stored}, packaged=${checksum}. ` +
            "Never rewrite an applied migration; add a new migration instead.",
          );
        }
      }

      const pending = migrationFiles.filter((f) => {
        const version = f.replace(/\.sql$/, "");
        return !applied.has(version);
      });

      if (pending.length === 0) {
        stateBus.emitSystemLog("info", `All ${migrationFiles.length} migrations already applied.`);
        return;
      }

      stateBus.emitSystemLog(
        "info",
        `Found ${migrationFiles.length} total, ${pending.length} pending migrations.`
      );

      for (const file of pending) {
        const version = file.replace(/\.sql$/, "");
        const sql = readFileSync(join(migrationsDir, file), "utf-8");
        const checksum = migrationChecksums.get(version);
        if (!checksum) throw new Error(`Checksum was not calculated for migration '${version}'`);
        const escapedVersion = version.replaceAll("'", "''");
        stateBus.emitSystemLog("debug", `  Applying ${version}...`);

        // Apply migration and record it in a single transaction
        const wrappedSQL = `
          BEGIN;
          ${sql}
          INSERT INTO schema_version (version, checksum, description)
          VALUES ('${escapedVersion}', '${checksum}', 'Migration ${escapedVersion}')
          ON CONFLICT (version) DO UPDATE SET
            applied_at = now(),
            checksum = EXCLUDED.checksum,
            description = EXCLUDED.description;
          COMMIT;
        `;
        await this.execSQL(wrappedSQL);
        stateBus.emitSystemLog("debug", `  ✓ ${version}`);
      }

      stateBus.emitSystemLog("info", `Applied ${pending.length} migrations.`);
    }
  }

  /** Execute a SQL query and return stdout (for SELECT queries). */
  private async querySQL(sql: string, database = this.config.database): Promise<string> {
    return new Promise((resolve, reject) => {
      const psql = spawn(
        this.psqlPath(),
        [
          "-h", this.config.host,
          "-p", String(this.config.port),
          "-U", this.config.user,
          "-d", database,
          "-X",
          "-v", "ON_ERROR_STOP=1",
          "-t", "-A",  // tuples only, unaligned output
          "-c", sql,
        ],
        {
          shell: false,
          windowsHide: true,
          stdio: "pipe",
          env: {
            ...this.postgresEnv(),
            PGPASSWORD: this.config.password,
            PGCLIENTENCODING: "UTF8",
            PGOPTIONS: "-c client_encoding=UTF8",
          },
        }
      );

      let stdout = "";
      let stderr = "";
      psql.stdout.on("data", (d) => (stdout += d.toString()));
      psql.stderr.on("data", (d) => (stderr += d.toString()));

      psql.on("close", (code) => {
        if (code === 0) {
          resolve(stdout.trim());
        } else {
          reject(new Error(`psql query failed (code ${code}): ${stderr}`));
        }
      });

      psql.on("error", reject);
    });
  }

  /** Execute SQL via psql. */
  private async execSQL(sql: string, database = this.config.database): Promise<void> {
    return new Promise((resolve, reject) => {
      const psql = spawn(
        this.psqlPath(),
        [
          "-h", this.config.host,
          "-p", String(this.config.port),
          "-U", this.config.user,
          "-d", database,
          "-X",
          "-v", "ON_ERROR_STOP=1",
          "-f", "-",
        ],
        {
          shell: false,
          windowsHide: true,
          stdio: "pipe",
          env: {
            ...this.postgresEnv(),
            PGPASSWORD: this.config.password,
            PGCLIENTENCODING: "UTF8",
            PGOPTIONS: "-c client_encoding=UTF8",
          },
        }
      );

      let stdout = "";
      let stderr = "";
      psql.stdout.on("data", (d) => (stdout += d.toString()));
      psql.stderr.on("data", (d) => (stderr += d.toString()));

      psql.on("close", (code) => {
        if (code === 0) {
          resolve();
        } else {
          // NOTICE/WARNING output still exits 0. Any non-zero exit means psql
          // did not guarantee that the command completed successfully.
          reject(new Error(`psql command failed (code ${code}): ${stderr || stdout}`));
        }
      });

      psql.on("error", reject);
      // Send SQL as explicit UTF-8 bytes. Passing schema text through `-c`
      // puts it on the Windows command line, where non-ASCII comments and
      // literals can be converted through the active ANSI code page.
      psql.stdin.on("error", (error) => {
        stderr += `\nstdin: ${error.message}`;
      });
      psql.stdin.end(Buffer.from(sql, "utf-8"));
    });
  }

  /** Check if PostgreSQL is accepting connections. */
  async healthCheck(): Promise<boolean> {
    try {
      await this.execSQL("SELECT 1");
      return true;
    } catch {
      return false;
    }
  }

  /** Atomically persist a legal run lifecycle transition. */
  async updateRunStatus(runId: string, target: PersistedRunStatus): Promise<void> {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(runId)) {
      throw new Error(`Invalid run id '${runId}'`);
    }
    if (!(target in RUN_STATUS_TRANSITIONS)) throw new Error(`Invalid run status '${String(target)}'`);

    const allowedFrom = allowedSourcesForStatus(target);
    if (allowedFrom.length === 0) throw new Error(`No lifecycle transition may enter '${target}'`);
    const fromSql = allowedFrom.map((status) => `'${status}'`).join(",");
    const result = await this.querySQL(
      `UPDATE sourcingrun SET status = '${target}', updated_at = now(), updated_by_agent = 'oculai-desktop' ` +
      `WHERE run_id = '${runId}'::uuid AND status IN (${fromSql}) RETURNING status`,
    );
    if (!result.split(/\r?\n/).includes(target)) {
      throw new Error(`Run '${runId}' cannot transition to '${target}' from its current state`);
    }
  }

  private pgCtlPath(): string {
    return this.binDir ? join(this.binDir, process.platform === "win32" ? "pg_ctl.exe" : "pg_ctl") : "pg_ctl";
  }

  private initDbPath(): string {
    return this.binDir ? join(this.binDir, process.platform === "win32" ? "initdb.exe" : "initdb") : "initdb";
  }

  private postgresEnv(): NodeJS.ProcessEnv {
    if (!this.binDir) return { ...process.env };
    const runtimeRoot = dirname(this.binDir);
    const libraryDir = join(runtimeRoot, "lib");
    const shareDir = join(runtimeRoot, "share");
    return {
      ...process.env,
      PATH: `${this.binDir}${delimiter}${process.env.PATH ?? ""}`,
      LD_LIBRARY_PATH: `${libraryDir}${delimiter}${process.env.LD_LIBRARY_PATH ?? ""}`,
      DYLD_LIBRARY_PATH: `${libraryDir}${delimiter}${process.env.DYLD_LIBRARY_PATH ?? ""}`,
      PGSHAREDIR: shareDir,
    };
  }

  private psqlPath(): string {
    return this.binDir ? join(this.binDir, process.platform === "win32" ? "psql.exe" : "psql") : "psql";
  }

  private findSchemaDir(): string | null {
    // Check multiple locations for schema files
    const candidates = [
      process.resourcesPath ? join(process.resourcesPath, "schema") : "",
      join(app.getAppPath(), "resources", "schema"),
      join(app.getAppPath(), "..", "resources", "schema"),
      join(process.cwd(), "resources", "schema"),
      // Fallback: relative to project root during development
      join(process.cwd(), "..", "oculai-db", "schema"),
    ];
    for (const c of candidates) {
      if (c && existsSync(c)) return c;
    }
    return null;
  }

  private async findFreePort(): Promise<number> {
    const settings = getSettingsStore();
    const configured = settings.get("dbPort");
    if (configured && configured > 0) return configured;
    return new Promise((resolve, reject) => {
      const server = createServer();
      server.unref();
      server.once("error", reject);
      server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, () => {
        const address = server.address();
        if (!address || typeof address === "string") {
          server.close();
          reject(new Error("Failed to allocate a local PostgreSQL port"));
          return;
        }
        const port = address.port;
        server.close((error) => error ? reject(error) : resolve(port));
      });
    });
  }
}
