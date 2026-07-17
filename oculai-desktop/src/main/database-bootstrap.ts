export type SqlRunner = (sql: string) => Promise<string | void>;

export function assertSafeDatabaseName(database: string): string {
  if (!/^[a-z_][a-z0-9_]{0,62}$/i.test(database)) {
    throw new Error(`Unsafe PostgreSQL database name '${database}'`);
  }
  return database;
}

/** Ensure a target DB exists while connected to the built-in admin database. */
export async function ensureDatabaseExists(
  database: string,
  queryAdmin: SqlRunner,
  executeAdmin: SqlRunner,
): Promise<boolean> {
  const safeName = assertSafeDatabaseName(database);
  const exists = await queryAdmin(`SELECT 1 FROM pg_database WHERE datname = '${safeName}'`);
  if (String(exists ?? "").trim() === "1") return false;
  await executeAdmin(`CREATE DATABASE "${safeName}"`);
  return true;
}
