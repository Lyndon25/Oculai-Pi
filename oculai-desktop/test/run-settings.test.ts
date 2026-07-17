import { describe, expect, it } from "vitest";
import { createRunSettingsSnapshot, isSnapshotSourceEnabled } from "../src/main/run-settings.js";

describe("run settings snapshot", () => {
  it("keeps model and source policy stable after live settings mutate", () => {
    const live = {
      llmProvider: "anthropic",
      llmModel: "model-a",
      thinkingLevel: "medium" as const,
      enabledSources: { github: true },
      maxIterations: 10,
      tokenBudget: 1_000,
      concurrency: 2,
    };
    const snapshot = createRunSettingsSnapshot(live, "secret");
    live.llmModel = "model-b";
    live.enabledSources.github = false;
    expect(snapshot.modelName).toBe("model-a");
    expect(isSnapshotSourceEnabled(snapshot, "github")).toBe(true);
    expect(snapshot.concurrency).toBe(2);
  });
});
