export interface RunCompletionDecision {
  canComplete: boolean;
  reason: string;
  taskCount: number;
  blockingStatuses: string[];
}

const TERMINAL_SUCCESS_STATUSES = new Set(["done", "completed", "skipped"]);

/**
 * Fail-closed completion gate for durable run state.
 *
 * A model ending its turn is not evidence that the pipeline finished. The run
 * may only become completed when the database reports at least one task and
 * every recorded task is in a successful terminal state.
 */
export function evaluateRunCompletion(state: unknown): RunCompletionDecision {
  const record = state && typeof state === "object" && !Array.isArray(state)
    ? state as Record<string, unknown>
    : {};
  const taskStats = Array.isArray(record.task_stats) ? record.task_stats : [];
  let taskCount = 0;
  const blockingStatuses = new Set<string>();

  for (const value of taskStats) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      blockingStatuses.add("invalid");
      continue;
    }
    const stat = value as Record<string, unknown>;
    const status = String(stat.status ?? "unknown").trim().toLowerCase() || "unknown";
    const rawCount = typeof stat.cnt === "number" ? stat.cnt : Number(stat.cnt ?? 0);
    const count = Number.isFinite(rawCount) && rawCount > 0 ? Math.floor(rawCount) : 0;
    if (count === 0) continue;
    taskCount += count;
    if (!TERMINAL_SUCCESS_STATUSES.has(status)) blockingStatuses.add(status);
  }

  if (taskCount === 0) {
    return {
      canComplete: false,
      reason: "No durable pipeline tasks were recorded",
      taskCount,
      blockingStatuses: [],
    };
  }
  if (blockingStatuses.size > 0) {
    const statuses = [...blockingStatuses].sort();
    return {
      canComplete: false,
      reason: `Pipeline tasks remain non-terminal or failed: ${statuses.join(", ")}`,
      taskCount,
      blockingStatuses: statuses,
    };
  }
  return {
    canComplete: true,
    reason: "All durable pipeline tasks completed successfully",
    taskCount,
    blockingStatuses: [],
  };
}
