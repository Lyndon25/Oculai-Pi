import { useEffect, useState } from "react";
import { Download, FileText, RefreshCcw, ShieldCheck, X } from "lucide-react";
import { useStore } from "../../store/index.js";
import { EmptyState, LoadingInline } from "../ui/primitives.js";

interface PendingApproval {
  approval_id?: string;
  action_type?: string;
  action_context?: { format?: string };
}

export function findHtmlExportApproval(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const approvals = (value as { pending_approvals?: unknown }).pending_approvals;
  if (!Array.isArray(approvals)) return null;
  const approval = approvals.find((item) => {
    const candidate = item as PendingApproval;
    return candidate.action_type === "export_report" &&
      candidate.action_context?.format === "html" &&
      typeof candidate.approval_id === "string";
  }) as PendingApproval | undefined;
  return approval?.approval_id ?? null;
}

export function ReportTab() {
  const reportHtml = useStore((s) => s.reportHtml);
  const activeRunId = useStore((s) => s.activeRunId);
  const setReportHtml = useStore((s) => s.setReportHtml);
  const [pendingApprovalId, setPendingApprovalId] = useState<string | null>(null);
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [reviewNotes, setReviewNotes] = useState("");
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPendingApprovalId(null);
    setError(null);
    if (!activeRunId) return () => { cancelled = true; };

    void window.oculai.listPendingApprovals(activeRunId)
      .then((result) => {
        if (!cancelled) setPendingApprovalId(findHtmlExportApproval(result));
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [activeRunId]);

  const requestApproval = async () => {
    if (!activeRunId || pendingApprovalId) return;
    setApprovalLoading(true);
    setError(null);
    try {
      const result = await window.oculai.requestReportApproval({
        runId: activeRunId,
        format: "html",
      }) as { approval_id?: string };
      if (!result.approval_id) throw new Error("审批服务未返回审批编号");
      setPendingApprovalId(result.approval_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setApprovalLoading(false);
    }
  };

  const decideApproval = async (decision: "approved" | "denied") => {
    if (!activeRunId || !pendingApprovalId) return;
    if (!reviewNotes.trim()) {
      setError("请填写人工审核说明后再批准或拒绝");
      return;
    }
    setApprovalLoading(true);
    setError(null);
    const approvalId = pendingApprovalId;
    try {
      await window.oculai.decideHumanApproval({
        approvalId,
        decision,
        reviewNotes: reviewNotes.trim(),
      });
      setPendingApprovalId(null);
      setReviewNotes("");
      if (decision === "denied") return;

      setExporting(true);
      const result = await window.oculai.exportReport({
        runId: activeRunId,
        format: "html",
        approvalId,
      });
      const data = result as { html_content?: string; html?: string };
      const html = data.html_content ?? data.html;
      if (!html) throw new Error("报告导出成功，但未返回 HTML 内容");
      setReportHtml(html);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
      setApprovalLoading(false);
    }
  };

  const handleDownload = () => {
    if (!reportHtml) return;
    const blob = new Blob([reportHtml], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `oculai-report-${activeRunId?.slice(0, 8)}.html`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const approvalAction = pendingApprovalId ? (
    <div className="flex items-center gap-2">
      <label className="sr-only" htmlFor="report-review-notes">人工审核说明</label>
      <input
        id="report-review-notes"
        className="min-w-52 rounded border border-rule bg-surface px-2.5 py-1.5 text-[12px] text-ink outline-none focus:border-accent"
        value={reviewNotes}
        onChange={(event) => setReviewNotes(event.target.value)}
        placeholder="填写人工审核说明（必填）"
        disabled={approvalLoading || exporting}
      />
      <div className="flex items-center gap-2">
        <button
          className="btn-primary"
          onClick={() => void decideApproval("approved")}
          disabled={approvalLoading || exporting || !reviewNotes.trim()}
          type="button"
        >
          {approvalLoading || exporting ? (
            <LoadingInline label="处理中" />
          ) : (
            <>
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              批准并生成
            </>
          )}
        </button>
        <button
          className="btn-secondary"
          onClick={() => void decideApproval("denied")}
          disabled={approvalLoading || exporting || !reviewNotes.trim()}
          type="button"
        >
          <X className="h-4 w-4" aria-hidden="true" />
          拒绝
        </button>
      </div>
    </div>
  ) : (
    <button
      className="btn-primary"
      onClick={() => void requestApproval()}
      disabled={!activeRunId || approvalLoading || exporting}
      type="button"
    >
      {approvalLoading ? (
        <LoadingInline label="申请中" />
      ) : (
        <>
          <ShieldCheck className="h-4 w-4" aria-hidden="true" />
          申请生成报告
        </>
      )}
    </button>
  );

  if (!reportHtml) {
    return (
      <div className="h-full">
        <EmptyState
          icon={FileText}
          title="报告尚未生成"
          description={pendingApprovalId
            ? "报告导出需要人工批准。请审核后批准生成，或拒绝本次操作。"
            : "评估与审计完成后可申请生成报告。每次导出都需要一次人工批准。"}
          action={activeRunId ? approvalAction : undefined}
        />
        {error && (
          <p className="px-6 text-center text-[13px] text-error" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-rule bg-surface px-4 py-2.5">
        <div>
          <span className="text-[13px] font-semibold text-ink tracking-tight">HTML 报告预览</span>
          <p className="text-[10px] text-ink-muted mt-0.5">沙箱预览；下载后可独立打开。</p>
        </div>
        <div className="flex items-center gap-2">
          {error && (
            <span className="text-[12px] text-error" role="alert">
              {error}
            </span>
          )}
          {pendingApprovalId ? approvalAction : (
            <button
              className="btn-secondary text-[12px]"
              onClick={() => void requestApproval()}
              disabled={approvalLoading || exporting}
              type="button"
            >
              {approvalLoading ? (
                <LoadingInline label="申请中" />
              ) : (
                <>
                  <RefreshCcw className="h-3.5 w-3.5" aria-hidden="true" />
                  申请重新生成
                </>
              )}
            </button>
          )}
          <button className="btn-primary text-[12px]" onClick={handleDownload} type="button">
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            下载 HTML
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 bg-white">
        <iframe
          srcDoc={reportHtml}
          className="h-full w-full border-0"
          sandbox="allow-same-origin"
          title="Oculai Report"
        />
      </div>
    </div>
  );
}
