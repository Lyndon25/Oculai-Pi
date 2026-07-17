import { describe, expect, it } from "vitest";
import { evaluateRunCompletion } from "../src/main/run-completion.js";

describe("evaluateRunCompletion", () => {
  it("allows completion only when every durable task succeeded", () => {
    expect(evaluateRunCompletion({
      task_stats: [
        { status: "done", cnt: 3 },
        { status: "completed", cnt: "2" },
        { status: "skipped", cnt: 1 },
      ],
    })).toMatchObject({ canComplete: true, taskCount: 6 });
  });

  it.each(["pending", "claimed", "processing", "error", "timeout", "unknown"])(
    "blocks completion for %s tasks",
    (status) => {
      expect(evaluateRunCompletion({ task_stats: [{ status, cnt: 1 }] })).toMatchObject({
        canComplete: false,
        blockingStatuses: [status],
      });
    },
  );

  it("fails closed when no tasks were recorded", () => {
    expect(evaluateRunCompletion({ task_stats: [] })).toMatchObject({
      canComplete: false,
      taskCount: 0,
    });
  });
});
