import { WorkbookEditApiClient, WorkbookEditApiError } from "./executor/api-client";
import {
  formatPreview,
  type EditPreview,
  type WorkflowDocument,
  WorkbookEditContractError,
} from "./executor/contracts";
import {
  approveLivePreview,
  createLivePreview,
  executeApprovedEdit,
  WorkbookEditExecutionUncertainError,
} from "./executor/execute-approved-edit";
import { OfficeJsWorkbookPort } from "./executor/office-port";
import { HostCallbackSnapshotPublisher } from "./executor/persistence";
import { resolveHostBootstrap } from "./host-bootstrap";

const apiBaseInput = element<HTMLInputElement>("api-base-url");
const workflowInput = element<HTMLInputElement>("workflow-id");
const loadButton = element<HTMLButtonElement>("load-workflow");
const previewButton = element<HTMLButtonElement>("create-preview");
const approveButton = element<HTMLButtonElement>("approve-preview");
const executeButton = element<HTMLButtonElement>("execute-edit");
const approvalConfirmed = element<HTMLInputElement>("approval-confirmed");
const saveAfterVerify = element<HTMLInputElement>("save-after-verify");
const summary = element<HTMLDivElement>("workflow-summary");
const diffOutput = element<HTMLPreElement>("diff-preview");
const statusOutput = element<HTMLOutputElement>("status");
const statusCard = statusOutput.closest<HTMLElement>(".status-card");
const apiMode = element<HTMLElement>("api-mode");

const bootstrap = resolveHostBootstrap(
  window.auditWorkbookEditHost,
  import.meta.env.DEV,
  window.location.origin,
);
const hostApiBaseUrl = bootstrap.mode === "host" ? bootstrap.apiBaseUrl : null;
const hostWorkflowId = bootstrap.mode === "host" ? bootstrap.workflowId : null;

let officePort: OfficeJsWorkbookPort | null = null;
let currentWorkflow: WorkflowDocument | null = null;
let currentPreview: EditPreview | null = null;
let busy = false;

apiBaseInput.value = bootstrap.mode === "invalid" ? "" : bootstrap.apiBaseUrl;
workflowInput.value = hostWorkflowId ?? "";
apiMode.textContent = bootstrap.mode === "host"
  ? "인증된 host가 API origin과 workflow를 고정했습니다."
  : bootstrap.mode === "development"
    ? "개발용 입력입니다. production build에서는 수동 연결이 비활성화됩니다."
    : bootstrap.reason;
refreshControls();

Office.onReady((info) => {
  if (info.host !== Office.HostType.Excel) {
    setStatus("이 Add-in은 Excel에서만 실행할 수 있습니다.", "error");
    return;
  }
  if (bootstrap.mode === "invalid") {
    setStatus(`HOST_BOOTSTRAP_REQUIRED: ${bootstrap.reason}`, "error");
    return;
  }
  try {
    officePort = new OfficeJsWorkbookPort();
    officePort.assertSupported();
    setStatus("ExcelApi 1.13 확인 완료. Workflow를 불러오세요.", "ok");
    refreshControls();
  } catch (error) {
    showError(error);
  }
});

loadButton.addEventListener("click", () => runAction(loadWorkflow));
previewButton.addEventListener("click", () => runAction(createPreview));
approveButton.addEventListener("click", () => runAction(approvePreview));
executeButton.addEventListener("click", () => runAction(executeEdit, true));
approvalConfirmed.addEventListener("change", refreshControls);
apiBaseInput.addEventListener("input", invalidateLoadedWorkflow);
workflowInput.addEventListener("input", invalidateLoadedWorkflow);

async function loadWorkflow(): Promise<void> {
  const workflow = (await client().getWorkflow(workflowId())).workflow;
  approvalConfirmed.checked = false;
  currentWorkflow = workflow;
  currentPreview = workflow.artifacts.preview;
  renderWorkflow(workflow);
  setStatus(`Workflow ${workflow.workflow_id} · ${workflow.state}`, "ok");
}

async function createPreview(): Promise<void> {
  const port = requireOfficePort();
  const result = await createLivePreview(client(), port, workflowId());
  currentWorkflow = { ...result.workflow, state: "previewed" };
  currentPreview = result.preview;
  approvalConfirmed.checked = false;
  renderPreview(result.preview);
  summary.textContent = `${result.workflow.sheet} · ${result.preview.diff.length}개 셀 · previewed`;
  setStatus("현재 Excel 셀을 재조회해 exact preview를 만들었습니다.", "ok");
}

async function approvePreview(): Promise<void> {
  if (!approvalConfirmed.checked || currentPreview === null) {
    throw new WorkbookEditContractError(
      "APPROVAL_CONFIRMATION_REQUIRED",
      "exact preview 확인 체크가 필요합니다.",
    );
  }
  const submission = await approveLivePreview(client(), workflowId(), currentPreview);
  if (currentWorkflow !== null) {
    currentWorkflow = { ...currentWorkflow, state: "approved" };
  }
  setStatus(`Preview 승인 완료 · ${submission.receipt.state}`, "ok");
}

async function executeEdit(): Promise<void> {
  const port = requireOfficePort();
  const callback = bootstrap.mode === "host" ? bootstrap.snapshotCallback : null;
  const publisher = callback === null || !saveAfterVerify.checked
    ? undefined
    : new HostCallbackSnapshotPublisher(callback);
  const result = await executeApprovedEdit(client(), port, workflowId(), {
    saveAfterVerify: saveAfterVerify.checked,
    ...(publisher === undefined ? {} : { snapshotPublisher: publisher }),
  });
  currentWorkflow = { ...result.workflow, state: result.verification.status };
  const persistence = result.snapshotPublication
    ? `새 snapshot ${result.snapshotPublication.snapshot_id.slice(0, 12)}… 연결 완료`
    : result.workbookSaved
      ? "현재 workbook 저장 완료 · 새 snapshot host 미연결"
      : "workbook 저장 안 함";
  setStatus(
    `검증: ${result.verification.status}\n적용: ${result.verification.application_status}\n${persistence}`,
    result.verification.status === "session_verified" ? "ok" : "error",
  );
}

function renderWorkflow(workflow: WorkflowDocument): void {
  summary.textContent = [
    `${workflow.sheet} · ${workflow.artifacts.proposal.changes.length}개 셀`,
    `state=${workflow.state}`,
    `snapshot=${workflow.snapshot_id.slice(0, 12)}…`,
  ].join(" · ");
  if (workflow.artifacts.preview === null) {
    diffOutput.textContent = workflow.artifacts.proposal.changes
      .map((change) => `${change.cell} · ${change.kind}`)
      .join("\n");
  } else {
    renderPreview(workflow.artifacts.preview);
  }
}

function renderPreview(preview: EditPreview): void {
  diffOutput.textContent = formatPreview(preview);
}

async function runAction(
  action: () => Promise<void>,
  invalidateOnFailure = false,
): Promise<void> {
  if (busy) return;
  const expectedConnection = connectionKey();
  busy = true;
  refreshControls();
  try {
    await action();
    if (connectionKey() !== expectedConnection) {
      invalidateLoadedWorkflow();
      throw new WorkbookEditContractError(
        "CONNECTION_CHANGED",
        "요청 중 연결 정보가 변경되어 응답을 폐기했습니다. workflow를 다시 불러오세요.",
      );
    }
  } catch (error) {
    if (invalidateOnFailure) invalidateLoadedWorkflow();
    showError(error);
  } finally {
    busy = false;
    refreshControls();
  }
}

function refreshControls(): void {
  const state = currentWorkflow?.state;
  const ready = bootstrap.mode !== "invalid" && officePort !== null && !busy;
  apiBaseInput.disabled = busy || bootstrap.mode !== "development";
  workflowInput.disabled = busy || bootstrap.mode !== "development";
  saveAfterVerify.disabled = busy;
  loadButton.disabled = !ready;
  previewButton.disabled = !ready || state !== "proposed";
  approvalConfirmed.disabled = !ready || currentPreview === null || state === "approved";
  approveButton.disabled =
    !ready || currentPreview === null || !approvalConfirmed.checked || state === "approved";
  executeButton.disabled = !ready || (state !== "approved" && state !== "claimed");
}

function invalidateLoadedWorkflow(): void {
  currentWorkflow = null;
  currentPreview = null;
  approvalConfirmed.checked = false;
  summary.textContent = "Workflow를 다시 불러오세요.";
  diffOutput.textContent = "";
  refreshControls();
}

function client(): WorkbookEditApiClient {
  if (bootstrap.mode === "invalid") throw new Error(bootstrap.reason);
  return new WorkbookEditApiClient(hostApiBaseUrl ?? apiBaseInput.value.trim());
}

function workflowId(): string {
  const value = hostWorkflowId ?? workflowInput.value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) {
    throw new TypeError("Workflow ID가 유효하지 않습니다.");
  }
  return value;
}

function requireOfficePort(): OfficeJsWorkbookPort {
  if (officePort === null) {
    throw new Error("Office가 아직 초기화되지 않았습니다.");
  }
  return officePort;
}

function showError(error: unknown): void {
  if (error instanceof WorkbookEditExecutionUncertainError) {
    currentWorkflow = null;
    currentPreview = null;
    approvalConfirmed.checked = false;
    summary.textContent = "실행 상태 조정이 필요합니다. 편집을 다시 실행하지 마세요.";
    diffOutput.textContent = "";
  }
  if (
    error instanceof WorkbookEditApiError ||
    error instanceof WorkbookEditContractError ||
    error instanceof WorkbookEditExecutionUncertainError
  ) {
    setStatus(`${error.code}: ${error.message}`, "error");
    return;
  }
  setStatus("UNEXPECTED_ERROR: 편집 작업을 안전하게 완료하지 못했습니다.", "error");
}

function connectionKey(): string {
  return JSON.stringify([
    hostApiBaseUrl ?? apiBaseInput.value.trim(),
    hostWorkflowId ?? workflowInput.value.trim(),
  ]);
}

function setStatus(message: string, kind: "ok" | "error"): void {
  statusOutput.textContent = message;
  if (statusCard !== null) {
    statusCard.dataset.kind = kind;
  }
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) {
    throw new Error(`missing taskpane element: ${id}`);
  }
  return found as T;
}
