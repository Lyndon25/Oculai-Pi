import { describe, expect, it } from "vitest";
import { allowedSourcesForStatus, canTransitionRun } from "../src/main/run-lifecycle.js";

describe("run lifecycle", () => {
  it("permits normal start, pause/resume, completion, and durable abort/resume", () => {
    expect(canTransitionRun("draft", "running")).toBe(true);
    expect(canTransitionRun("running", "paused")).toBe(true);
    expect(canTransitionRun("paused", "running")).toBe(true);
    expect(canTransitionRun("running", "completed")).toBe(true);
    expect(canTransitionRun("running", "aborted")).toBe(true);
    expect(canTransitionRun("aborted", "running")).toBe(true);
  });

  it("rejects re-entry into terminal completion and duplicate transitions", () => {
    expect(canTransitionRun("completed", "running")).toBe(false);
    expect(canTransitionRun("completed", "aborted")).toBe(false);
    expect(canTransitionRun("running", "running")).toBe(false);
    expect(canTransitionRun("aborted", "aborted")).toBe(false);
    expect(allowedSourcesForStatus("completed")).toEqual(["running", "reviewing"]);
  });
});
