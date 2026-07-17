import { describe, expect, it } from "vitest";
import { SchedulerRefreshGate } from "../src/main/scheduler-refresh-gate.js";

describe("SchedulerRefreshGate", () => {
  it("applies a refresh exactly once after all active runs finish", () => {
    const gate = new SchedulerRefreshGate();
    expect(gate.request(1)).toBe(false);
    expect(gate.consumeWhenIdle(1)).toBe(false);
    expect(gate.consumeWhenIdle(0)).toBe(true);
    expect(gate.consumeWhenIdle(0)).toBe(false);
  });

  it("allows immediate refresh while idle", () => {
    expect(new SchedulerRefreshGate().request(0)).toBe(true);
  });
});
