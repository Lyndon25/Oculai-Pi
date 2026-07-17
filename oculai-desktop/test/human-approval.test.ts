import { describe, expect, it } from "vitest";
import { buildHumanApprovalDecision } from "../src/main/human-approval.js";

describe("buildHumanApprovalDecision", () => {
  it("uses trusted OS identity and preserves an explicit human audit note", () => {
    expect(buildHumanApprovalDecision({
      approvalId: "approval-1",
      decision: "approved",
      reviewNotes: "Reviewed candidate evidence and approved export.",
    }, "desktop-user")).toEqual({
      approval_id: "approval-1",
      decision: "approved",
      reviewer_id: "desktop-user",
      reviewer_role: "human_reviewer",
      review_notes: "Reviewed candidate evidence and approved export.",
    });
  });

  it("rejects an empty audit note", () => {
    expect(() => buildHumanApprovalDecision({
      approvalId: "approval-1",
      decision: "denied",
      reviewNotes: "   ",
    }, "desktop-user")).toThrow(/review notes/i);
  });
});
