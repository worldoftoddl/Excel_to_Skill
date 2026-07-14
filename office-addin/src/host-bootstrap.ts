import type { HostSnapshotCallback } from "./executor/persistence";

export interface WorkbookEditHostConfig {
  apiBaseUrl?: string;
  workflowId?: string;
  publishVerifiedSnapshot?: HostSnapshotCallback;
}

export type HostBootstrapResolution =
  | {
      mode: "host";
      apiBaseUrl: string;
      workflowId: string;
      snapshotCallback: HostSnapshotCallback | null;
    }
  | {
      mode: "development";
      apiBaseUrl: string;
      workflowId: "";
      snapshotCallback: null;
    }
  | {
      mode: "invalid";
      reason: string;
    };

/** Production never falls back to user-controlled connection fields. */
export function resolveHostBootstrap(
  config: WorkbookEditHostConfig | undefined,
  development: boolean,
  currentOrigin: string,
): HostBootstrapResolution {
  if (config === undefined) {
    if (development) {
      return {
        mode: "development",
        apiBaseUrl: currentOrigin,
        workflowId: "",
        snapshotCallback: null,
      };
    }
    return {
      mode: "invalid",
      reason: "배포 Add-in에는 인증된 API URL과 workflow ID bootstrap이 필요합니다.",
    };
  }
  const apiBaseUrl = config.apiBaseUrl?.trim() ?? "";
  const workflowId = config.workflowId?.trim() ?? "";
  if (apiBaseUrl === "" || workflowId === "") {
    return {
      mode: "invalid",
      reason: "부분 host bootstrap은 허용되지 않습니다. API URL과 workflow ID를 함께 고정하세요.",
    };
  }
  return {
    mode: "host",
    apiBaseUrl,
    workflowId,
    snapshotCallback: config.publishVerifiedSnapshot ?? null,
  };
}
