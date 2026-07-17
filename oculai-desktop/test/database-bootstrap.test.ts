import { describe, expect, it, vi } from "vitest";
import { assertSafeDatabaseName, ensureDatabaseExists } from "../src/main/database-bootstrap.js";

describe("embedded database bootstrap", () => {
  it("creates the application database on a clean cluster", async () => {
    const query = vi.fn(async () => "");
    const execute = vi.fn(async () => undefined);
    await expect(ensureDatabaseExists("oculai", query, execute)).resolves.toBe(true);
    expect(query).toHaveBeenCalledWith("SELECT 1 FROM pg_database WHERE datname = 'oculai'");
    expect(execute).toHaveBeenCalledWith('CREATE DATABASE "oculai"');
  });

  it("does not recreate an existing database", async () => {
    const execute = vi.fn(async () => undefined);
    await expect(ensureDatabaseExists("oculai", async () => "1", execute)).resolves.toBe(false);
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects unsafe identifiers before constructing SQL", () => {
    expect(() => assertSafeDatabaseName('oculai"; DROP DATABASE postgres; --')).toThrow(/unsafe/i);
  });
});
