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
import {
  AuthenticatedApiSnapshotPublisher,
  type SnapshotPublication,
  type SnapshotPublicationRequest,
  SnapshotPublicationApiError,
} from "./executor/persistence";
import {
  assertBootstrapForWorkflow,
  fetchHostBootstrap,
  type HostBootstrapDocument,
  HostBootstrapError,
  resolveHostBootstrap,
} from "./host-bootstrap";

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
let hostBootstrap: HostBootstrapDocument | null = null;

let officePort: OfficeJsWorkbookPort | null = null;
let currentWorkflow: WorkflowDocument | null = null;
let currentPreview: EditPreview | null = null;
let busy = false;

apiBaseInput.value = bootstrap.mode === "invalid" ? "" : bootstrap.apiBaseUrl;
workflowInput.value = "";
apiMode.textContent = bootstrap.mode === "host"
  ? "인증된 host session을 확인하고 있습니다."
  : bootstrap.mode === "development"
    ? "개발용 입력입니다. production build에서는 수동 연결이 비활성화됩니다."
    : bootstrap.reason;
refreshControls();

Office.onReady(async (info) => {
  if (info.host !== Office.HostType.Excel) {
    setStatus("이 Add-in은 Excel에서만 실행할 수 있습니다.", "error");
    return;
  }
  if (bootstrap.mode === "invalid") {
    setStatus(`HOST_BOOTSTRAP_REQUIRED: ${bootstrap.reason}`, "error");
    return;
  }
  try {
    if (bootstrap.mode === "host") {
      hostBootstrap = await fetchHostBootstrap(
        bootstrap.hostSessionId,
        bootstrap.apiBaseUrl,
      );
      saveAfterVerify.checked = hostBootstrap.persistence_policy === "required";
      workflowInput.value = hostBootstrap.workflow_id;
      const workflow = (await client().getWorkflow(hostBootstrap.workflow_id)).workflow;
      assertBootstrapForWorkflow(hostBootstrap, workflow);
      currentWorkflow = workflow;
      currentPreview = workflow.artifacts.preview;
      renderWorkflow(workflow);
      apiMode.textContent = "인증된 host session과 workflow binding을 확인했습니다.";
    }
    officePort = new OfficeJsWorkbookPort();
    officePort.assertSupported();
    setStatus(
      bootstrap.mode === "host"
        ? "ExcelApi 1.13 및 host workflow binding 확인 완료."
        : "ExcelApi 1.13 확인 완료. Workflow를 불러오세요.",
      "ok",
    );
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
  if (hostBootstrap !== null) assertBootstrapForWorkflow(hostBootstrap, workflow);
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
  const policy = hostBootstrap?.persistence_policy;
  const saveWorkbook = policy === "required"
    ? true
    : policy === "unsupported"
      ? false
      : saveAfterVerify.checked;
  const publisher = policy === "required" && hostBootstrap !== null && hostApiBaseUrl !== null
    ? new AuthenticatedApiSnapshotPublisher(
        hostApiBaseUrl,
        hostBootstrap.host_session_id,
        { currentOrigin: hostApiBaseUrl },
      )
    : undefined;
  if (currentWorkflow?.state === "session_verified") {
    if (publisher === undefined) {
      throw new WorkbookEditContractError(
        "PUBLICATION_RESUME_UNAVAILABLE",
        "검증된 workbook의 snapshot 발행을 재개할 authenticated publisher가 없습니다.",
      );
    }
    const request = verifiedPublicationRequest(currentWorkflow);
    try {
      const existing = await publisher.lookupVerifiedSnapshot(request);
      setStatus(`새 snapshot ${existing.snapshot_id.slice(0, 12)}… 연결 완료`, "ok");
      return;
    } catch (error) {
      if (
        !(error instanceof SnapshotPublicationApiError) ||
        (error.code !== "PUBLICATION_NOT_READY" && error.code !== "PUBLICATION_NOT_FOUND")
      ) {
        throw error;
      }
    }
    // A resumed pane must not save again: unrelated coauthor changes may have happened after the
    // execution's original save. The server-owned reacquirer must locate the direct provider
    // transition from the pinned base revision or fail closed for host reconciliation.
    let publication: SnapshotPublication;
    try {
      publication = await publisher.publishVerifiedSnapshot(request);
    } catch (error) {
      throw new WorkbookEditExecutionUncertainError(
        "SNAPSHOT_PUBLICATION_CONFIRMATION_UNKNOWN",
        "workbook 저장 뒤 snapshot 발행을 확인하지 못했습니다. 편집을 다시 실행하지 마세요.",
        { cause: error },
      );
    }
    setStatus(`새 snapshot ${publication.snapshot_id.slice(0, 12)}… 연결 완료`, "ok");
    return;
  }
  const result = await executeApprovedEdit(client(), port, workflowId(), {
    saveAfterVerify: saveWorkbook,
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
  saveAfterVerify.disabled =
    busy || hostBootstrap?.persistence_policy === "required" ||
    hostBootstrap?.persistence_policy === "unsupported";
  loadButton.disabled = !ready;
  previewButton.disabled = !ready || state !== "proposed";
  approvalConfirmed.disabled = !ready || currentPreview === null || state === "approved";
  approveButton.disabled =
    !ready || currentPreview === null || !approvalConfirmed.checked || state === "approved";
  const publicationResume = state === "session_verified" &&
    hostBootstrap?.persistence_policy === "required";
  executeButton.disabled = !ready || (
    state !== "approved" && state !== "claimed" && !publicationResume
  );
  executeButton.textContent = publicationResume ? "Snapshot 발행 재개" : "승인된 변경 실행";
}

function verifiedPublicationRequest(workflow: WorkflowDocument): SnapshotPublicationRequest {
  const manifest = workflow.artifacts.manifest;
  const verification = workflow.artifacts.verification;
  const executionId = manifest?.execution_id;
  const manifestRef = manifest?.manifest_ref;
  const manifestSha256 = manifest?.manifest_sha256;
  if (
    workflow.state !== "session_verified" ||
    manifest === null ||
    verification === null ||
    typeof executionId !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(executionId) ||
    typeof manifestRef !== "string" ||
    !/^edit-manifest:[0-9a-f]{64}$/.test(manifestRef) ||
    typeof manifestSha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(manifestSha256) ||
    manifestRef.slice("edit-manifest:".length) !== manifestSha256 ||
    verification.status !== "session_verified" ||
    verification.new_snapshot_required !== true
  ) {
    throw new WorkbookEditContractError(
      "PUBLICATION_RESUME_INVALID",
      "workflow에 snapshot 발행 재개를 위한 exact verification basis가 없습니다.",
    );
  }
  return {
    workflow,
    executionId,
    manifestRef,
    manifestSha256,
    verification,
    workbookSaved: true,
  };
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
  if (bootstrap.mode === "host") {
    if (hostBootstrap === null) {
      throw new HostBootstrapError(
        "HOST_BOOTSTRAP_REQUIRED",
        "인증된 host session이 아직 준비되지 않았습니다.",
      );
    }
    return new WorkbookEditApiClient(bootstrap.apiBaseUrl, {
      hostSessionId: hostBootstrap.host_session_id,
      currentOrigin: bootstrap.apiBaseUrl,
    });
  }
  return new WorkbookEditApiClient(apiBaseInput.value.trim());
}

function workflowId(): string {
  const value = hostBootstrap?.workflow_id ?? workflowInput.value.trim();
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
    error instanceof WorkbookEditExecutionUncertainError ||
    error instanceof HostBootstrapError ||
    error instanceof SnapshotPublicationApiError
  ) {
    setStatus(`${error.code}: ${error.message}`, "error");
    return;
  }
  setStatus("UNEXPECTED_ERROR: 편집 작업을 안전하게 완료하지 못했습니다.", "error");
}

function connectionKey(): string {
  return JSON.stringify([
    hostApiBaseUrl ?? apiBaseInput.value.trim(),
    hostBootstrap?.host_session_id ?? null,
    hostBootstrap?.binding_sha256 ?? null,
    hostBootstrap?.workflow_id ?? workflowInput.value.trim(),
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
