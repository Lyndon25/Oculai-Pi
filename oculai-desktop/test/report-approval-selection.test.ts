import { describe, expect, it } from "vitest";
import { findHtmlExportApproval } from "../src/renderer/components/report/ReportTab.js";

describe("report approval recovery", () => {
  it("reuses only an HTML export approval", () => {
    expect(findHtmlExportApproval({
      pending_approvals: [
        { approval_id: "markdown-id", action_type: "export_report", action_context: { format: "markdown" } },
        { approval_id: "html-id", action_type: "export_report", action_context: { format: "html" } },
      ],
    })).toBe("html-id");
  });

  it("does not reuse legacy or context-mismatched approvals", () => {
    expect(findHtmlExportApproval({
      pending_approvals: [{ approval_id: "legacy-id", action_type: "export_report" }],
    })).toBeNull();
  });
});
