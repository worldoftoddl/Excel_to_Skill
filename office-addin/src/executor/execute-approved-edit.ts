import {
  assertManifestForWorkflow,
  assertReceiptForWorkflow,
  assertReceiptWorkflowId,
  claimDetails,
  deepJsonEqual,
  previewFromSubmission,
  startDetails,
  verificationFromSubmission,
  type ClaimDetails,
  type EditPreview,
  type SubmissionResponse,
  type WitnessInput,
  type VerificationSummary,
  type WorkflowDocument,
  WorkbookEditContractError,
} from "./contracts";
import {
  WorkbookEditApiError,
  type WorkbookEditApi,
} from "./api-client";
import {
  OfficeBeforeWriteError,
  OfficeStartConfirmationError,
  OfficeStartRejectedError,
  type WorkbookOfficePort,
} from "./office-port";
import type {
  SnapshotPublication,
  VerifiedSnapshotPublisher,
} from "./persistence";

// The Python artifact envelope is capped at 600 KB. Keep enough headroom for its
// schema/binding/digest fields and collapse large post-write readbacks to an
// indeterminate witness that can still drive the backend to a terminal quarantine.
const MAX_WITNESS_INPUT_BYTES = 550_000;

export interface ApprovedEditExecutionOptions {
  saveAfterVerify?: boolean;
  snapshotPublisher?: VerifiedSnapshotPublisher;
}

export interface ApprovedEditExecutionResult {
  workflow: WorkflowDocument;
  verification: VerificationSummary;
  verificationReceipt: SubmissionResponse;
  workbookSaved: boolean;
  snapshotPublication: SnapshotPublication | null;
}

export class WorkbookEditExecutionUncertainError extends Error {
  readonly code: string;

  constructor(code: string, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "WorkbookEditExecutionUncertainError";
    this.code = code;
  }
}

export async function createLivePreview(
  api: WorkbookEditApi,
  office: WorkbookOfficePort,
  workflowId: string,
): Promise<{ workflow: WorkflowDocument; preview: EditPreview }> {
  office.assertSupported();
  const workflow = (await api.getWorkflow(workflowId)).workflow;
  assertRequestedWorkflow(workflow, workflowId);
  if (workflow.state !== "proposed") {
    throw new WorkbookEditContractError(
      "INVALID_STATE",
      "현재 workflow는 새 preview를 만들 수 있는 proposed 상태가 아닙니다.",
    );
  }
  const cells = workflow.artifacts.proposal.changes.map((item) => item.cell);
  const before = await office.readStates(workflow.sheet, workflow.worksheet_id, cells);
  const submission = await api.createPreview(
    workflow.workflow_id,
    workflow.revision_id,
    workflow.worksheet_id,
    before,
  );
  assertReceiptForWorkflow(submission, workflow);
  const preview = previewFromSubmission(submission);
  if (
    preview.proposal_ref !== workflow.artifacts.proposal.proposal_ref ||
    !deepJsonEqual(preview.binding, workflow.artifacts.proposal.binding) ||
    !deepJsonEqual(preview.office_binding, {
      session_id: workflow.session_id,
      revision_id: workflow.revision_id,
      worksheet_id: workflow.worksheet_id,
      sheet: workflow.sheet,
    })
  ) {
    throw new WorkbookEditContractError(
      "WORKBOOK_BINDING_MISMATCH",
      "preview가 불러온 workflow와 일치하지 않습니다.",
    );
  }
  return { workflow, preview };
}

export async function approveLivePreview(
  api: WorkbookEditApi,
  workflowId: string,
  preview: EditPreview,
): Promise<SubmissionResponse> {
  const submission = await api.approvePreview(
    workflowId,
    preview.preview_ref,
    preview.preview_sha256,
  );
  assertReceiptWorkflowId(submission, workflowId);
  assertReceiptForPreview(submission, preview);
  if (submission.receipt.state !== "approved") {
    throw new WorkbookEditContractError(
      "INVALID_APPROVAL",
      "approval 응답 상태가 approved가 아닙니다.",
    );
  }
  return submission;
}

export async function executeApprovedEdit(
  api: WorkbookEditApi,
  office: WorkbookOfficePort,
  workflowId: string,
  options: ApprovedEditExecutionOptions = {},
): Promise<ApprovedEditExecutionResult> {
  if (options.snapshotPublisher !== undefined && options.saveAfterVerify !== true) {
    throw new WorkbookEditContractError(
      "SNAPSHOT_REQUIRES_VERIFIED_SAVE",
      "새 snapshot 연결은 session_verified 이후 workbook 저장과 함께만 허용됩니다.",
    );
  }
  office.assertSupported();
  const workflow = (await api.getWorkflow(workflowId)).workflow;
  assertRequestedWorkflow(workflow, workflowId);
  if (workflow.state !== "approved" && workflow.state !== "claimed") {
    throw new WorkbookEditContractError(
      "INVALID_STATE",
      "현재 workflow는 실행 가능한 approved/claimed 상태가 아닙니다.",
    );
  }

  let claimSubmission: SubmissionResponse;
  try {
    claimSubmission = await api.claimExecution(workflow.workflow_id, workflow.session_id);
  } catch (error) {
    if (isDefiniteClaimRejection(error)) throw error;
    throw new WorkbookEditExecutionUncertainError(
      "CLAIM_CONFIRMATION_UNKNOWN",
      "실행 claim 응답을 확정하지 못했습니다. workflow를 다시 조회해 조정하기 전에는 재실행하지 마세요.",
      { cause: error },
    );
  }

  let claim: ClaimDetails;
  try {
    claim = claimDetails(claimSubmission);
  } catch (error) {
    throw new WorkbookEditExecutionUncertainError(
      "CLAIM_CONFIRMATION_UNKNOWN",
      "실행 claim은 발행됐을 수 있지만 capability 응답을 검증하지 못했습니다. host 조정이 필요합니다.",
      { cause: error },
    );
  }
  try {
    assertReceiptForWorkflow(claimSubmission, workflow);
    assertManifestForWorkflow(claim.apply_manifest, workflow);
  } catch (error) {
    await abortClaim(api, workflow, claim.execution_id, claim.fence, claim.challenge);
    throw error;
  }

  let witness;
  try {
    witness = await office.executeManifest(claim.apply_manifest, async () => {
      let submission: SubmissionResponse;
      try {
        submission = await api.markApplyStarted(
          workflow.workflow_id,
          claim.execution_id,
          claim.fence,
          claim.challenge,
        );
      } catch (error) {
        if (isDefiniteStartRejection(error)) {
          throw new OfficeStartRejectedError("backend가 write-start를 명시적으로 거부했습니다.", {
            cause: error,
          });
        }
        throw error;
      }
      assertReceiptForWorkflow(submission, workflow);
      const started = startDetails(submission, claim);
      return { executionDeadline: started.execution_deadline };
    });
  } catch (error) {
    if (error instanceof OfficeStartConfirmationError) {
      throw new WorkbookEditExecutionUncertainError(
        "START_CONFIRMATION_UNKNOWN",
        "write-start 응답을 확인하지 못했습니다. workbook에는 쓰지 않았으며 host 조정이 필요합니다.",
        { cause: error },
      );
    }
    if (error instanceof OfficeStartRejectedError) {
      await abortClaim(api, workflow, claim.execution_id, claim.fence, claim.challenge);
      throw error.cause instanceof Error ? error.cause : error;
    }
    if (error instanceof OfficeBeforeWriteError) {
      await abortClaim(api, workflow, claim.execution_id, claim.fence, claim.challenge);
    }
    throw error;
  }

  witness = boundWitnessForBackend(witness);
  let verificationReceipt: SubmissionResponse;
  let verification: VerificationSummary;
  try {
    verificationReceipt = await api.verifyExecution(
      workflow.workflow_id,
      claim.execution_id,
      claim.fence,
      claim.challenge,
      witness,
    );
    assertReceiptForWorkflow(verificationReceipt, workflow);
    verification = verificationFromSubmission(
      verificationReceipt,
      claim.apply_manifest.manifest_ref,
    );
  } catch (error) {
    if (
      witness.outcome === "applied" &&
      error instanceof WorkbookEditApiError &&
      error.code === "EXECUTION_LEASE_EXPIRED"
    ) {
      const expiredWitness: WitnessInput = {
        outcome: "indeterminate",
        observed_before: witness.observed_before,
        actual_after: null,
        recalculation: witness.recalculation,
      };
      try {
        verificationReceipt = await api.verifyExecution(
          workflow.workflow_id,
          claim.execution_id,
          claim.fence,
          claim.challenge,
          expiredWitness,
        );
        assertReceiptForWorkflow(verificationReceipt, workflow);
        verification = verificationFromSubmission(
          verificationReceipt,
          claim.apply_manifest.manifest_ref,
        );
      } catch (recoveryError) {
        throw new WorkbookEditExecutionUncertainError(
          "VERIFICATION_CONFIRMATION_UNKNOWN",
          "실행 lease 만료 뒤 indeterminate 종결도 확인하지 못했습니다. 자동 재실행하지 마세요.",
          { cause: recoveryError },
        );
      }
    } else if (witness.outcome !== "stale_precondition") {
      throw new WorkbookEditExecutionUncertainError(
        "VERIFICATION_CONFIRMATION_UNKNOWN",
        "Excel write 이후 검증 응답을 확인하지 못했습니다. 자동 재실행하지 마세요.",
        { cause: error },
      );
    } else {
      throw error;
    }
  }

  let workbookSaved = false;
  let snapshotPublication: SnapshotPublication | null = null;
  if (verification.status === "session_verified") {
    if (options.saveAfterVerify === true) {
      try {
        await office.saveCurrentWorkbook();
      } catch (error) {
        throw new WorkbookEditExecutionUncertainError(
          "WORKBOOK_SAVE_CONFIRMATION_UNKNOWN",
          "셀 적용은 session_verified이지만 workbook 저장을 확인하지 못했습니다. 편집을 다시 실행하지 마세요.",
          { cause: error },
        );
      }
      workbookSaved = true;
      if (options.snapshotPublisher !== undefined) {
        try {
          snapshotPublication = await options.snapshotPublisher.publishVerifiedSnapshot({
            workflow,
            executionId: claim.execution_id,
            manifestRef: claim.apply_manifest.manifest_ref,
            manifestSha256: claim.apply_manifest.manifest_sha256,
            verification,
            workbookSaved: true,
          });
        } catch (error) {
          throw new WorkbookEditExecutionUncertainError(
            "SNAPSHOT_PUBLICATION_CONFIRMATION_UNKNOWN",
            "workbook 저장은 완료됐지만 새 snapshot 발행을 확인하지 못했습니다. 편집을 다시 실행하지 마세요.",
            { cause: error },
          );
        }
      }
    }
  }

  return {
    workflow,
    verification,
    verificationReceipt,
    workbookSaved,
    snapshotPublication,
  };
}

async function abortClaim(
  api: WorkbookEditApi,
  workflow: WorkflowDocument,
  executionId: string,
  fence: number,
  challenge: string,
): Promise<void> {
  try {
    const submission = await api.abortExecution(
      workflow.workflow_id,
      executionId,
      fence,
      challenge,
    );
    assertReceiptForWorkflow(submission, workflow);
    if (submission.receipt.state !== "aborted_before_apply") {
      throw new WorkbookEditContractError(
        "INVALID_ABORT",
        "abort 응답 상태가 aborted_before_apply가 아닙니다.",
      );
    }
  } catch (abortError) {
    if (abortError instanceof WorkbookEditApiError && abortError.code === "RETRY_FORBIDDEN") {
      throw new WorkbookEditExecutionUncertainError(
        "ABORT_REJECTED_AFTER_START",
        "backend가 이미 write-start 상태입니다. 자동 재실행하지 마세요.",
        { cause: abortError },
      );
    }
    throw new WorkbookEditExecutionUncertainError(
      "ABORT_CONFIRMATION_UNKNOWN",
      "write 전 실패 뒤 claim 해제를 확인하지 못했습니다.",
      { cause: abortError },
    );
  }
}

function isDefiniteStartRejection(error: unknown): boolean {
  return (
    error instanceof WorkbookEditApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.code !== "COMMAND_IN_PROGRESS" &&
    error.code !== "RETRY_FORBIDDEN"
  );
}

function isDefiniteClaimRejection(error: unknown): boolean {
  if (!(error instanceof WorkbookEditApiError)) return false;
  if (error.status < 400 || error.status >= 500) return false;
  return !new Set([
    "ACTIVE_EXECUTION_CONFLICT",
    "APPROVAL_REPLAY",
    "COMMAND_IN_PROGRESS",
    "EDIT_CONFLICT",
    "INVALID_STATE",
    "RETRY_FORBIDDEN",
  ]).has(error.code);
}

function assertRequestedWorkflow(workflow: WorkflowDocument, workflowId: string): void {
  if (workflow.workflow_id !== workflowId) {
    throw new WorkbookEditContractError(
      "WORKBOOK_BINDING_MISMATCH",
      "요청한 workflow와 응답 workflow가 일치하지 않습니다.",
    );
  }
}

function assertReceiptForPreview(
  submission: SubmissionResponse,
  preview: EditPreview,
): void {
  const receipt = submission.receipt;
  if (
    receipt.session_id !== preview.office_binding.session_id ||
    receipt.bundle_id !== preview.binding.bundle_id ||
    receipt.snapshot_id !== preview.binding.snapshot_id ||
    receipt.workbook_sha256 !== preview.binding.workbook_sha256 ||
    receipt.revision_id !== preview.office_binding.revision_id ||
    receipt.sheet !== preview.office_binding.sheet
  ) {
    throw new WorkbookEditContractError(
      "WORKBOOK_BINDING_MISMATCH",
      "approval receipt가 exact preview binding과 일치하지 않습니다.",
    );
  }
}

function boundWitnessForBackend(witness: WitnessInput): WitnessInput {
  if (jsonBytes(witness) <= MAX_WITNESS_INPUT_BYTES) return witness;
  if (witness.outcome === "stale_precondition") {
    throw new WorkbookEditContractError(
      "WITNESS_LIMIT_EXCEEDED",
      "write 전 stale witness가 backend artifact 상한을 초과했습니다.",
    );
  }
  const bounded: WitnessInput = {
    outcome: "indeterminate",
    observed_before: witness.observed_before,
    actual_after: null,
    recalculation: witness.recalculation,
  };
  if (jsonBytes(bounded) > MAX_WITNESS_INPUT_BYTES) {
    throw new WorkbookEditExecutionUncertainError(
      "WITNESS_LIMIT_EXCEEDED_AFTER_START",
      "write 이후 최소 indeterminate witness도 backend 상한을 초과했습니다. 자동 재실행하지 마세요.",
    );
  }
  return bounded;
}

function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}
