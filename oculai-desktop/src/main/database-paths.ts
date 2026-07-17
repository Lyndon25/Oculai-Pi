import { dirname, join } from "path";

/**
 * initdb requires its target data directory to be empty.  Keep the password
 * file in the parent directory while initialization is in progress.
 */
export function initDbPasswordFile(dataDir: string, uniqueId: string): string {
  return join(dirname(dataDir), `.initdb-password-${uniqueId}.tmp`);
}
