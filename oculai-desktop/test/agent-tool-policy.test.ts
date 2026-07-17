import { describe, expect, it } from "vitest";
import { HUMAN_ONLY_TOOLS, isAgentToolAllowed } from "../src/main/agent-tool-policy.js";

describe("agent tool policy", () => {
  it("keeps human approval decisions outside every AgentSession", () => {
    expect(HUMAN_ONLY_TOOLS.has("oculai_decide_human_approval")).toBe(true);
    expect(isAgentToolAllowed("oculai_decide_human_approval")).toBe(false);
    expect(isAgentToolAllowed("oculai_request_human_approval")).toBe(true);
    expect(isAgentToolAllowed("oculai_check_approval_status")).toBe(true);
  });
});
