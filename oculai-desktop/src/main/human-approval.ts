import type { DecideHumanApprovalPayload } from "../shared/events.js";

export interface HumanApprovalToolParams extends Record<string, unknown> {
  approval_id: string;
  decision: "approved" | "denied";
  reviewer_id: string;
  reviewer_role: "human_reviewer";
  review_notes: string;
}

/** Build the privileged tool call exclusively from trusted desktop identity. */
export function buildHumanApprovalDecision(
  payload: DecideHumanApprovalPayload,
  desktopReviewerId: string,
): HumanApprovalToolParams {
  const approvalId = payload.approvalId.trim();
  const reviewNotes = payload.reviewNotes.trim();
  const reviewerId = desktopReviewerId.trim();
  if (!approvalId || !["approved", "denied"].includes(payload.decision)) {
    throw new Error("A valid approval id and approved/denied decision are required");
  }
  if (!reviewNotes) throw new Error("Human review notes are required for the audit trail");
  if (!reviewerId) throw new Error("The operating-system reviewer identity is unavailable");
  return {
    approval_id: approvalId,
    decision: payload.decision,
    reviewer_id: reviewerId,
    reviewer_role: "human_reviewer",
    review_notes: reviewNotes,
  };
}
