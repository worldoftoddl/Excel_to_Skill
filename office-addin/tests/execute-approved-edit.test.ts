import { describe, expect, it, vi } from "vitest";

import type { WorkbookEditApi } from "../src/executor/api-client";
import { WorkbookEditApiError } from "../src/executor/api-client";
import type {
  CellState,
  SubmissionResponse,
  WitnessInput,
  WorkflowResponse,
} from "../src/executor/contracts";
import {
  approveLivePreview,
  createLivePreview,
  executeApprovedEdit,
  WorkbookEditExecutionUncertainError,
} from "../src/executor/execute-approved-edit";
import {
  OfficeBeforeWriteError,
  OfficeStartConfirmationError,
  OfficeStartRejectedError,
  type StartedExecution,
  type WorkbookOfficePort,
} from "../src/executor/office-port";
import type {
  SnapshotPublicationRequest,
  VerifiedSnapshotPublisher,
} from "../src/executor/persistence";
import {
  BLANK_STATE,
  CLAIM_SUBMISSION,
  clone,
  MANIFEST,
  PREVIEW,
  SHA_A,
  SHA_C,
  START_SUBMISSION,
  submission,
  terminalSubmission,
  VALUE_STATE,
  WORKFLOW_RESPONSE,
} from "./fixtures";

class FakeApi implements WorkbookEditApi {
  workflowResponse: WorkflowResponse = clone(WORKFLOW_RESPONSE);
  previewSubmission = submission("previewed", { preview: PREVIEW });
  approvalSubmission = submission("approved", {
    approval_ref: MANIFEST.approval_ref,
    approval_sha256: SHA_A,
    expires_at: MANIFEST.approval_expires_at,
  });
  claimSubmission = clone(CLAIM_SUBMISSION);
  startSubmission = clone(START_SUBMISSION);
  verificationSubmission = terminalSubmission("session_verified");
  abortSubmission = submission("aborted_before_apply", {
    execution_id: MANIFEST.execution_id,
    fence: MANIFEST.fencing_token,
    reason: "claim_aborted",
  });
  startError: Error | null = null;
  claimError: Error | null = null;
  verifyError: Error | null = null;
  verifyErrors: Error[] = [];
  calls: string[] = [];
  before: CellState[] | null = null;
  witness: WitnessInput | null = null;

  async getWorkflow(): Promise<WorkflowResponse> {
    this.calls.push("get");
    return clone(this.workflowResponse);
  }

  async createPreview(
    _workflowId: string,
    _revisionId: string,
    _worksheetId: string,
    before: CellState[],
  ): Promise<SubmissionResponse> {
    this.calls.push("preview");
    this.before = clone(before);
    return clone(this.previewSubmission);
  }

  async approvePreview(): Promise<SubmissionResponse> {
    this.calls.push("approve");
    return clone(this.approvalSubmission);
  }

  async claimExecution(): Promise<SubmissionResponse> {
    this.calls.push("claim");
    if (this.claimError !== null) throw this.claimError;
    return clone(this.claimSubmission);
  }

  async markApplyStarted(): Promise<SubmissionResponse> {
    this.calls.push("started");
    if (this.startError !== null) throw this.startError;
    return clone(this.startSubmission);
  }

  async verifyExecution(
    _workflowId: string,
    _executionId: string,
    _fence: number,
    _challenge: string,
    witness: WitnessInput,
  ): Promise<SubmissionResponse> {
    this.calls.push("verify");
    this.witness = clone(witness);
    const queuedError = this.verifyErrors.shift();
    if (queuedError !== undefined) throw queuedError;
    if (this.verifyError !== null) throw this.verifyError;
    return clone(this.verificationSubmission);
  }

  async abortExecution(): Promise<SubmissionResponse> {
    this.calls.push("abort");
    return clone(this.abortSubmission);
  }
}

class FakeOfficePort implements WorkbookOfficePort {
  readback: CellState[] = [clone(BLANK_STATE)];
  witness: WitnessInput = {
    outcome: "applied",
    observed_before: [clone(BLANK_STATE)],
    actual_after: [clone(VALUE_STATE)],
    recalculation: "none",
  };
  executeError: Error | null = null;
  supported = true;
  saveCalls = 0;
  saveError: Error | null = null;
  startCalls = 0;
  reads: Array<{ sheet: string; worksheetId: string; cells: string[] }> = [];

  assertSupported(): void {
    if (!this.supported) throw new Error("unsupported");
  }

  async readStates(sheet: string, worksheetId: string, cells: string[]): Promise<CellState[]> {
    this.reads.push({ sheet, worksheetId, cells: [...cells] });
    return clone(this.readback);
  }

  async executeManifest(
    _manifest: typeof MANIFEST,
    markStarted: () => Promise<StartedExecution>,
  ): Promise<WitnessInput> {
    if (this.executeError !== null) throw this.executeError;
    if (this.witness.outcome !== "stale_precondition") {
      this.startCalls += 1;
      try {
        await markStarted();
      } catch (error) {
        if (error instanceof OfficeStartRejectedError) throw error;
        throw new OfficeStartConfirmationError("unknown", { cause: error });
      }
    }
    return clone(this.witness);
  }

  async saveCurrentWorkbook(): Promise<void> {
    this.saveCalls += 1;
    if (this.saveError !== null) throw this.saveError;
  }
}

describe("approved workbook edit orchestration", () => {
  it("reads exact proposal cells and creates a live preview", async () => {
    const api = new FakeApi();
    api.workflowResponse.workflow.state = "proposed";
    api.workflowResponse.workflow.artifacts.preview = null;
    const office = new FakeOfficePort();

    const result = await createLivePreview(api, office, "workflow-a");

    expect(office.reads).toEqual([
      { sheet: "C", worksheetId: "worksheet-a", cells: ["A1"] },
    ]);
    expect(api.before).toEqual([BLANK_STATE]);
    expect(result.preview.preview_ref).toBe(PREVIEW.preview_ref);
    expect(api.calls).toEqual(["get", "preview"]);
  });

  it("requires the exact preview approval response", async () => {
    const api = new FakeApi();
    const result = await approveLivePreview(api, "workflow-a", PREVIEW);
    expect(result.receipt.state).toBe("approved");
    expect(api.calls).toEqual(["approve"]);
  });

  it("rejects an approval receipt from a different exact preview binding", async () => {
    const api = new FakeApi();
    api.approvalSubmission.receipt.revision_id = "revision-other";

    await expect(approveLivePreview(api, "workflow-a", PREVIEW)).rejects.toMatchObject({
      code: "WORKBOOK_BINDING_MISMATCH",
    });
  });

  it("rejects a custom API implementation that returns another workflow", async () => {
    const api = new FakeApi();
    api.workflowResponse.workflow.workflow_id = "workflow-b";
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "WORKBOOK_BINDING_MISMATCH",
    });
    expect(api.calls).toEqual(["get"]);
  });

  it("reports stale before state without starting or saving", async () => {
    const api = new FakeApi();
    api.verificationSubmission = terminalSubmission("stale_precondition");
    const office = new FakeOfficePort();
    office.witness = {
      outcome: "stale_precondition",
      observed_before: [{ ...clone(BLANK_STATE), number_format: "0.00" }],
      actual_after: null,
      recalculation: "none",
    };

    const result = await executeApprovedEdit(api, office, "workflow-a", {
      saveAfterVerify: true,
    });

    expect(office.startCalls).toBe(0);
    expect(office.saveCalls).toBe(0);
    expect(api.calls).toEqual(["get", "claim", "verify"]);
    expect(api.witness?.outcome).toBe("stale_precondition");
    expect(result.verification.status).toBe("stale_precondition");
  });

  it("starts, verifies, saves, then publishes a new host-owned snapshot", async () => {
    const api = new FakeApi();
    const office = new FakeOfficePort();
    const publish = vi.fn(async (_request: SnapshotPublicationRequest) => ({
      schema_version: "audit_workbook_snapshot_publication.v1" as const,
      bundle_id: "bundle-a",
      execution_id: MANIFEST.execution_id,
      manifest_ref: MANIFEST.manifest_ref,
      manifest_sha256: MANIFEST.manifest_sha256,
      base_snapshot_id: WORKFLOW_RESPONSE.workflow.snapshot_id,
      base_revision_id: WORKFLOW_RESPONSE.workflow.revision_id,
      snapshot_id: SHA_C,
      workbook_sha256: SHA_A,
      revision_id: "revision-new",
      asset_persisted: true as const,
      prepared_bundle_created: false,
    }));
    const publisher: VerifiedSnapshotPublisher = { publishVerifiedSnapshot: publish };

    const result = await executeApprovedEdit(api, office, "workflow-a", {
      saveAfterVerify: true,
      snapshotPublisher: publisher,
    });

    expect(api.calls).toEqual(["get", "claim", "started", "verify"]);
    expect(api.witness).toEqual(office.witness);
    expect(office.saveCalls).toBe(1);
    expect(publish).toHaveBeenCalledOnce();
    expect(publish.mock.calls[0]![0].manifestSha256).toBe(MANIFEST.manifest_sha256);
    expect(result.snapshotPublication?.asset_persisted).toBe(true);
  });

  it("aborts a claim when Office fails before write start", async () => {
    const api = new FakeApi();
    const office = new FakeOfficePort();
    office.executeError = new OfficeBeforeWriteError("read failed");

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toThrow("read failed");
    expect(api.calls).toEqual(["get", "claim", "abort"]);
  });

  it("marks a network-ambiguous claim response as uncertain", async () => {
    const api = new FakeApi();
    api.claimError = new WorkbookEditApiError("NETWORK_UNAVAILABLE", 0, "unknown");
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "CLAIM_CONFIRMATION_UNKNOWN",
    });
    expect(api.calls).toEqual(["get", "claim"]);
  });

  it("aborts a parsed claim whose immutable manifest is not the approved preview", async () => {
    const api = new FakeApi();
    const tampered = clone(api.claimSubmission);
    const manifest = tampered.receipt.details.apply_manifest as typeof MANIFEST;
    manifest.office_binding.revision_id = "revision-other";
    api.claimSubmission = tampered;
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "WORKBOOK_BINDING_MISMATCH",
    });
    expect(api.calls).toEqual(["get", "claim", "abort"]);
  });

  it("requires host reconciliation when a claimed capability cannot be decoded", async () => {
    const api = new FakeApi();
    const malformed = clone(api.claimSubmission);
    malformed.receipt.details.challenge = "different-challenge";
    api.claimSubmission = malformed;
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "CLAIM_CONFIRMATION_UNKNOWN",
    });
    expect(api.calls).toEqual(["get", "claim"]);
  });

  it("does not abort or write when write-start confirmation is ambiguous", async () => {
    const api = new FakeApi();
    api.startError = new WorkbookEditApiError("NETWORK_UNAVAILABLE", 0, "unknown");
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "START_CONFIRMATION_UNKNOWN",
    });
    expect(api.calls).toEqual(["get", "claim", "started"]);
  });

  it("aborts the claim when write-start is explicitly rejected", async () => {
    const api = new FakeApi();
    api.startError = new WorkbookEditApiError("APPROVAL_EXPIRED", 409, "expired");
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "APPROVAL_EXPIRED",
    });
    expect(api.calls).toEqual(["get", "claim", "started", "abort"]);
  });

  it("marks a lost post-write verification response as uncertain", async () => {
    const api = new FakeApi();
    api.verifyError = new WorkbookEditApiError("NETWORK_UNAVAILABLE", 0, "unknown");
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toBeInstanceOf(
      WorkbookEditExecutionUncertainError,
    );
    expect(api.calls).toEqual(["get", "claim", "started", "verify"]);
    expect(office.saveCalls).toBe(0);
  });

  it("marks a malformed post-write verification response as uncertain", async () => {
    const api = new FakeApi();
    api.verificationSubmission = submission("session_verified", { verification: {} });
    const office = new FakeOfficePort();

    await expect(executeApprovedEdit(api, office, "workflow-a")).rejects.toMatchObject({
      code: "VERIFICATION_CONFIRMATION_UNKNOWN",
    });
    expect(api.calls).toEqual(["get", "claim", "started", "verify"]);
  });

  it("does not save a terminal verification routed from another manifest", async () => {
    const api = new FakeApi();
    const mismatched = terminalSubmission("session_verified");
    const verification = mismatched.receipt.details.verification as Record<string, unknown>;
    verification.manifest_ref = `edit-manifest:${"a".repeat(64)}`;
    api.verificationSubmission = mismatched;
    const office = new FakeOfficePort();

    await expect(
      executeApprovedEdit(api, office, "workflow-a", { saveAfterVerify: true }),
    ).rejects.toMatchObject({ code: "VERIFICATION_CONFIRMATION_UNKNOWN" });
    expect(office.saveCalls).toBe(0);
  });

  it("converts an expired applied witness into a terminal indeterminate verification", async () => {
    const api = new FakeApi();
    api.verifyErrors.push(
      new WorkbookEditApiError("EXECUTION_LEASE_EXPIRED", 409, "expired"),
    );
    api.verificationSubmission = terminalSubmission("indeterminate");
    const office = new FakeOfficePort();

    const result = await executeApprovedEdit(api, office, "workflow-a");

    expect(result.verification.status).toBe("indeterminate");
    expect(api.calls).toEqual(["get", "claim", "started", "verify", "verify"]);
    expect(api.witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
  });

  it("collapses an oversized post-write readback to a terminal indeterminate witness", async () => {
    const api = new FakeApi();
    api.verificationSubmission = terminalSubmission("indeterminate");
    const office = new FakeOfficePort();
    const longValue = "가".repeat(200_000);
    const after = clone(VALUE_STATE);
    after.authored = { kind: "value", value: longValue };
    after.calculated_value = longValue;
    office.witness.actual_after = [after];

    const result = await executeApprovedEdit(api, office, "workflow-a");

    expect(result.verification.status).toBe("indeterminate");
    expect(api.witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
  });

  it("never saves a verification_failed execution", async () => {
    const api = new FakeApi();
    api.verificationSubmission = terminalSubmission("verification_failed");
    const office = new FakeOfficePort();

    const result = await executeApprovedEdit(api, office, "workflow-a", {
      saveAfterVerify: true,
    });

    expect(result.verification.status).toBe("verification_failed");
    expect(office.saveCalls).toBe(0);
  });

  it("fails before any API or Office call when snapshot publication is requested without save", async () => {
    const api = new FakeApi();
    const office = new FakeOfficePort();
    const publisher: VerifiedSnapshotPublisher = {
      publishVerifiedSnapshot: vi.fn(),
    };

    await expect(
      executeApprovedEdit(api, office, "workflow-a", { snapshotPublisher: publisher }),
    ).rejects.toMatchObject({ code: "SNAPSHOT_REQUIRES_VERIFIED_SAVE" });
    expect(api.calls).toEqual([]);
    expect(office.startCalls).toBe(0);
  });

  it("does not invite re-execution when workbook save confirmation is lost", async () => {
    const api = new FakeApi();
    const office = new FakeOfficePort();
    office.saveError = new Error("save lost");

    await expect(
      executeApprovedEdit(api, office, "workflow-a", { saveAfterVerify: true }),
    ).rejects.toMatchObject({ code: "WORKBOOK_SAVE_CONFIRMATION_UNKNOWN" });
    expect(api.calls).toEqual(["get", "claim", "started", "verify"]);
  });

  it("reports snapshot publication separately after verified save", async () => {
    const api = new FakeApi();
    const office = new FakeOfficePort();
    const publisher: VerifiedSnapshotPublisher = {
      publishVerifiedSnapshot: vi.fn(async () => {
        throw new Error("callback lost");
      }),
    };

    await expect(
      executeApprovedEdit(api, office, "workflow-a", {
        saveAfterVerify: true,
        snapshotPublisher: publisher,
      }),
    ).rejects.toMatchObject({ code: "SNAPSHOT_PUBLICATION_CONFIRMATION_UNKNOWN" });
    expect(office.saveCalls).toBe(1);
  });
});
