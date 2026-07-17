/** Tools that must never be exposed to an LLM-controlled AgentSession. */
export const HUMAN_ONLY_TOOLS: ReadonlySet<string> = new Set([
  "oculai_decide_human_approval",
]);

export function isAgentToolAllowed(name: string): boolean {
  return !HUMAN_ONLY_TOOLS.has(name);
}
