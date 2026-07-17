/**
 * Preload script — exposes a typed IPC API to the renderer via contextBridge.
 */
import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS } from "../shared/ipc-channels.js";
import type {
  ExportReportPayload,
  GetCandidateDetailPayload,
  GetCandidatesPayload,
  GetRunStatePayload,
  StartRunPayload,
  DecideHumanApprovalPayload,
  RequestReportApprovalPayload,
} from "../shared/events.js";

const api = {
  // ---- Actions (renderer → main) ----
  startRun: (payload: StartRunPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.START_RUN, payload),

  resumeRun: (runId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.RESUME_RUN, { runId }),

  getRunState: (payload: GetRunStatePayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.GET_RUN_STATE, payload),

  abortRun: (runId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.ABORT_RUN, { runId }),

  getCandidates: (payload: GetCandidatesPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.GET_CANDIDATES, payload),

  getCandidateDetail: (payload: GetCandidateDetailPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.GET_CANDIDATE_DETAIL, payload),

  exportReport: (payload: ExportReportPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.EXPORT_REPORT, payload),

  // Human approval decisions deliberately have a dedicated renderer -> main
  // action. The LLM never receives this channel or the underlying tool.
  decideHumanApproval: (payload: DecideHumanApprovalPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.DECIDE_HUMAN_APPROVAL, payload),

  requestReportApproval: (payload: RequestReportApprovalPayload) =>
    ipcRenderer.invoke(IPC_CHANNELS.REQUEST_REPORT_APPROVAL, payload),

  listPendingApprovals: (runId: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.LIST_PENDING_APPROVALS, { runId }),

  listRuns: () =>
    ipcRenderer.invoke(IPC_CHANNELS.LIST_RUNS),

  getSettings: () =>
    ipcRenderer.invoke(IPC_CHANNELS.SETTINGS_GET),

  setSettings: (settings: Record<string, unknown>) =>
    ipcRenderer.invoke(IPC_CHANNELS.SETTINGS_SET, settings),

  setApiKey: (provider: string, key: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.SETTINGS_SET_API_KEY, { provider, key }),

  // ---- Events (main → renderer) ----
  // Allowlist: only these event channels may be subscribed to from the renderer.
  // All action/invoke channels are excluded — the renderer uses invoke() for those.
  on: (channel: string, callback: (...args: unknown[]) => void) => {
    const allowedEventChannels = [
      IPC_CHANNELS.RUN_CREATED,
      IPC_CHANNELS.RUN_ERROR,
      IPC_CHANNELS.ORCHESTRATOR_PHASE,
      IPC_CHANNELS.SUBAGENT_SPAWNED,
      IPC_CHANNELS.SUBAGENT_PROGRESS,
      IPC_CHANNELS.SUBAGENT_COMPLETED,
      IPC_CHANNELS.CANDIDATE_UPSERTED,
      IPC_CHANNELS.AGENT_THINKING,
      IPC_CHANNELS.AGENT_MESSAGE,
      IPC_CHANNELS.AGENT_TOOL_CALL,
      IPC_CHANNELS.AGENT_TOOL_RESULT,
      IPC_CHANNELS.REPORT_READY,
      IPC_CHANNELS.SYSTEM_STATUS,
      IPC_CHANNELS.SYSTEM_LOG,
    ];
    if (allowedEventChannels.includes(channel as never)) {
      const subscription = (_event: Electron.IpcRendererEvent, ...args: unknown[]) =>
        callback(...args);
      ipcRenderer.on(channel, subscription);
      return () => {
        ipcRenderer.removeListener(channel, subscription);
      };
    }
    return () => {};
  },

  removeAllListeners: (channel: string) => {
    const allowedEventChannels = [
      IPC_CHANNELS.RUN_CREATED,
      IPC_CHANNELS.RUN_ERROR,
      IPC_CHANNELS.ORCHESTRATOR_PHASE,
      IPC_CHANNELS.SUBAGENT_SPAWNED,
      IPC_CHANNELS.SUBAGENT_PROGRESS,
      IPC_CHANNELS.SUBAGENT_COMPLETED,
      IPC_CHANNELS.CANDIDATE_UPSERTED,
      IPC_CHANNELS.AGENT_THINKING,
      IPC_CHANNELS.AGENT_MESSAGE,
      IPC_CHANNELS.AGENT_TOOL_CALL,
      IPC_CHANNELS.AGENT_TOOL_RESULT,
      IPC_CHANNELS.REPORT_READY,
      IPC_CHANNELS.SYSTEM_STATUS,
      IPC_CHANNELS.SYSTEM_LOG,
    ];
    if (allowedEventChannels.includes(channel as never)) {
      ipcRenderer.removeAllListeners(channel);
    }
  },
};

contextBridge.exposeInMainWorld("oculai", api);

// Type declaration for renderer
declare global {
  interface Window {
    oculai: typeof api;
  }
}
