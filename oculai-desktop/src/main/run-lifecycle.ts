export type PersistedRunStatus = "draft" | "running" | "paused" | "reviewing" | "completed" | "aborted";

export const RUN_STATUS_TRANSITIONS: Readonly<Record<PersistedRunStatus, readonly PersistedRunStatus[]>> = {
  draft: ["running", "aborted"],
  running: ["paused", "reviewing", "completed", "aborted"],
  paused: ["running", "aborted"],
  reviewing: ["running", "completed", "aborted"],
  completed: [],
  // An aborted interactive run is resumable from its durable checkpoints.
  aborted: ["running"],
};

export function allowedSourcesForStatus(target: PersistedRunStatus): PersistedRunStatus[] {
  return (Object.keys(RUN_STATUS_TRANSITIONS) as PersistedRunStatus[])
    .filter((source) => RUN_STATUS_TRANSITIONS[source].includes(target));
}

export function canTransitionRun(source: PersistedRunStatus, target: PersistedRunStatus): boolean {
  return RUN_STATUS_TRANSITIONS[source].includes(target);
}
