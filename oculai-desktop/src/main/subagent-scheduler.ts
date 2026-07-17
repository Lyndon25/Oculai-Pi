import { randomUUID } from "node:crypto";
import { AGENT_PROFILES, type AgentProfile } from "./agent-profiles.js";

export interface SubagentTask {
  agent: string;
  task: string;
  target?: string;
}

export interface SubagentRequest {
  agent?: string;
  task?: string;
  target?: string;
  tasks?: SubagentTask[];
  chain?: SubagentTask[];
}

export interface SubagentResult {
  agentId: string;
  agent: string;
  target: string;
  status: "done" | "error";
  output?: string;
  error?: string;
}

export interface SubagentExecutionContext {
  runId: string;
  agentId: string;
  profile: AgentProfile;
  task: string;
  signal: AbortSignal;
}

export type SubagentExecutor = (context: SubagentExecutionContext) => Promise<string>;

export interface SubagentSchedulerEvents {
  spawned(result: Pick<SubagentResult, "agentId" | "agent" | "target"> & { runId: string }): void;
  progress(event: { runId: string; agentId: string; agent: string; message: string }): void;
  completed(result: SubagentResult & { runId: string }): void;
}

interface PermitWaiter {
  resolve: () => void;
  reject: (error: Error) => void;
  signal: AbortSignal;
  abortListener: () => void;
}

export class SubagentScheduler {
  private active = 0;
  private readonly waiters: PermitWaiter[] = [];
  private readonly runControllers = new Map<string, Set<AbortController>>();

  constructor(
    private readonly maxConcurrency: number,
    private readonly executor: SubagentExecutor,
    private readonly events: SubagentSchedulerEvents,
  ) {
    if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1) {
      throw new Error("Subagent concurrency must be a positive integer");
    }
  }

  async execute(runId: string, request: SubagentRequest, parentSignal?: AbortSignal): Promise<SubagentResult[]> {
    const hasSingle = Boolean(request.agent && request.task);
    const hasParallel = Boolean(request.tasks?.length);
    const hasChain = Boolean(request.chain?.length);
    if (Number(hasSingle) + Number(hasParallel) + Number(hasChain) !== 1) {
      throw new Error("Provide exactly one subagent mode: agent+task, tasks, or chain");
    }

    if (hasSingle) {
      return [await this.runOne(runId, {
        agent: request.agent!,
        task: request.task!,
        target: request.target,
      }, parentSignal)];
    }
    if (hasParallel) {
      return Promise.all(request.tasks!.map((task) => this.runOne(runId, task, parentSignal)));
    }

    const results: SubagentResult[] = [];
    let previous = "";
    for (const item of request.chain!) {
      if (parentSignal?.aborted) break;
      const task = { ...item, task: item.task.replaceAll("{previous}", previous) };
      const result = await this.runOne(runId, task, parentSignal);
      results.push(result);
      if (result.status === "error") break;
      previous = result.output ?? "";
    }
    return results;
  }

  cancelRun(runId: string): void {
    for (const controller of this.runControllers.get(runId) ?? []) {
      controller.abort();
    }
  }

  getLoad(): { active: number; queued: number; maxConcurrency: number } {
    return { active: this.active, queued: this.waiters.length, maxConcurrency: this.maxConcurrency };
  }

  private async runOne(
    runId: string,
    task: SubagentTask,
    parentSignal?: AbortSignal,
  ): Promise<SubagentResult> {
    const profile = AGENT_PROFILES.get(task.agent);
    if (!profile) {
      throw new Error(`Unknown subagent '${task.agent}'. Available: ${Array.from(AGENT_PROFILES.keys()).join(", ")}`);
    }
    const agentId = `${runId}:${profile.name}:${randomUUID()}`;
    const target = task.target || task.task.slice(0, 120);
    const controller = new AbortController();
    const onParentAbort = () => controller.abort();
    parentSignal?.addEventListener("abort", onParentAbort, { once: true });
    if (parentSignal?.aborted) controller.abort();
    this.trackController(runId, controller);

    this.events.spawned({ runId, agentId, agent: profile.description, target });
    try {
      await this.acquire(controller.signal);
      this.events.progress({ runId, agentId, agent: profile.description, message: "Subagent started" });
      try {
        const output = await this.executor({ runId, agentId, profile, task: task.task, signal: controller.signal });
        const result: SubagentResult = {
          agentId,
          agent: profile.description,
          target,
          status: "done",
          output,
        };
        this.events.completed({ runId, ...result });
        return result;
      } finally {
        this.release();
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const result: SubagentResult = {
        agentId,
        agent: profile.description,
        target,
        status: "error",
        error: message,
      };
      this.events.completed({ runId, ...result });
      return result;
    } finally {
      parentSignal?.removeEventListener("abort", onParentAbort);
      this.untrackController(runId, controller);
    }
  }

  private trackController(runId: string, controller: AbortController): void {
    const controllers = this.runControllers.get(runId) ?? new Set<AbortController>();
    controllers.add(controller);
    this.runControllers.set(runId, controllers);
  }

  private untrackController(runId: string, controller: AbortController): void {
    const controllers = this.runControllers.get(runId);
    controllers?.delete(controller);
    if (controllers?.size === 0) this.runControllers.delete(runId);
  }

  private acquire(signal: AbortSignal): Promise<void> {
    if (signal.aborted) return Promise.reject(this.abortError());
    if (this.active < this.maxConcurrency) {
      this.active += 1;
      return Promise.resolve();
    }

    return new Promise((resolve, reject) => {
      const waiter: PermitWaiter = {
        resolve,
        reject,
        signal,
        abortListener: () => {
          const index = this.waiters.indexOf(waiter);
          if (index >= 0) this.waiters.splice(index, 1);
          reject(this.abortError());
        },
      };
      signal.addEventListener("abort", waiter.abortListener, { once: true });
      this.waiters.push(waiter);
    });
  }

  private release(): void {
    this.active -= 1;
    while (this.waiters.length > 0) {
      const waiter = this.waiters.shift()!;
      waiter.signal.removeEventListener("abort", waiter.abortListener);
      if (waiter.signal.aborted) {
        waiter.reject(this.abortError());
        continue;
      }
      this.active += 1;
      waiter.resolve();
      break;
    }
  }

  private abortError(): Error {
    const error = new Error("Subagent was cancelled");
    error.name = "AbortError";
    return error;
  }
}
