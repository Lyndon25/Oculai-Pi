import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToolBridge } from "../src/main/tool-bridge.js";

const fixture = resolve(process.cwd(), "test", "fixtures", "fake_sidecar.py");
const bridges: ToolBridge[] = [];

async function createBridge(options: ConstructorParameters<typeof ToolBridge>[0] = {}) {
  const bridge = new ToolBridge({ restartDelayMs: 25, readyTimeoutMs: 3_000, ...options });
  bridges.push(bridge);
  await bridge.start(process.platform === "win32" ? "python" : "python3", fixture);
  return bridge;
}

afterEach(async () => {
  await Promise.allSettled(bridges.splice(0).map((bridge) => bridge.stop()));
});

describe("ToolBridge", () => {
  it("applies bounded in-flight concurrency and queue backpressure", async () => {
    const bridge = await createBridge({ maxInFlight: 1, maxQueue: 1 });
    const first = bridge.callTool("echo", { value: 1, delay: 0.05 });
    const second = bridge.callTool("echo", { value: 2, delay: 0.01 });
    const rejected = bridge.callTool("echo", { value: 3 });

    expect(bridge.getLoad()).toEqual({ active: 1, queued: 1, maxInFlight: 1, maxQueue: 1 });
    await expect(rejected).rejects.toThrow("backpressure limit");
    await expect(first).resolves.toMatchObject({ value: 1 });
    await expect(second).resolves.toMatchObject({ value: 2 });
  });

  it("cancels an in-flight request through an AbortSignal", async () => {
    const bridge = await createBridge();
    const controller = new AbortController();
    const call = bridge.callTool("hang", {}, { signal: controller.signal, timeoutMs: 2_000 });
    await vi.waitFor(() => expect(bridge.getLoad().active).toBe(1));
    controller.abort();
    await expect(call).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(bridge.getLoad().active).toBe(0));
  });

  it("automatically restarts after an unexpected process exit", async () => {
    const bridge = await createBridge();
    await expect(bridge.callTool("crash", {}, 2_000)).rejects.toThrow("sidecar exited");
    await vi.waitFor(() => expect(bridge.isReady()).toBe(true), { timeout: 3_000 });
    await expect(bridge.callTool("echo", { recovered: true })).resolves.toMatchObject({ recovered: true });
  });
});
