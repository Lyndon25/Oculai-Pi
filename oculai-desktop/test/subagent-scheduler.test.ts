import { describe, expect, it, vi } from "vitest";
import { SubagentScheduler, type SubagentSchedulerEvents } from "../src/main/subagent-scheduler.js";

function events(): SubagentSchedulerEvents {
  return { spawned: vi.fn(), progress: vi.fn(), completed: vi.fn() };
}

describe("SubagentScheduler", () => {
  it("executes parallel work up to the configured concurrency", async () => {
    let active = 0;
    let maximum = 0;
    const scheduler = new SubagentScheduler(2, async ({ task }) => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, 20));
      active -= 1;
      return task;
    }, events());

    const results = await scheduler.execute("run-a", {
      tasks: [
        { agent: "source-researcher", task: "one" },
        { agent: "source-researcher", task: "two" },
        { agent: "source-researcher", task: "three" },
      ],
    });

    expect(maximum).toBe(2);
    expect(results.map((result) => result.output)).toEqual(["one", "two", "three"]);
    expect(results.every((result) => result.agentId.startsWith("run-a:"))).toBe(true);
    expect(scheduler.getLoad()).toEqual({ active: 0, queued: 0, maxConcurrency: 2 });
  });

  it("cancels active and queued agents for only the selected run", async () => {
    const scheduler = new SubagentScheduler(1, ({ runId, signal }) => new Promise((resolve, reject) => {
      if (runId === "run-b") {
        resolve("unaffected");
        return;
      }
      signal.addEventListener("abort", () => reject(new Error("cancelled")), { once: true });
    }), events());

    const runA = scheduler.execute("run-a", {
      tasks: [
        { agent: "profile-enricher", task: "active" },
        { agent: "profile-enricher", task: "queued" },
      ],
    });
    await vi.waitFor(() => expect(scheduler.getLoad().active).toBe(1));
    scheduler.cancelRun("run-a");
    const runB = scheduler.execute("run-b", { agent: "quality-auditor", task: "audit" });

    expect((await runA).every((result) => result.status === "error")).toBe(true);
    expect((await runB)[0]).toMatchObject({ status: "done", output: "unaffected" });
  });

  it("runs chain steps sequentially and interpolates the prior output", async () => {
    const observed: string[] = [];
    const scheduler = new SubagentScheduler(4, async ({ task }) => {
      observed.push(task);
      return `result:${task}`;
    }, events());

    await scheduler.execute("run-chain", {
      chain: [
        { agent: "search-strategist", task: "plan" },
        { agent: "query-optimizer", task: "refine {previous}" },
      ],
    });

    expect(observed).toEqual(["plan", "refine result:plan"]);
  });
});
