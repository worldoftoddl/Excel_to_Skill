import type {
  ApplyManifest,
  CellState,
  EditPreview,
  SubmissionResponse,
  VerificationSummary,
  WorkflowDocument,
  WorkflowResponse,
} from "../src/executor/contracts";

export const SHA_A = "a".repeat(64);
export const SHA_B = "b".repeat(64);
export const SHA_C = "c".repeat(64);
export const SHA_D = "d".repeat(64);
export const SHA_E = "e".repeat(64);
export const SHA_F = "f".repeat(64);

export const BLANK_STATE: CellState = {
  cell: "A1",
  authored: { kind: "blank" },
  calculated_value: null,
  calculated_type: "empty",
  number_format: "General",
  target_constraints: {
    merged: false,
    spill: "none",
    protected: false,
    table_member: false,
  },
};

export const VALUE_STATE: CellState = {
  ...BLANK_STATE,
  authored: { kind: "value", value: "승인됨" },
  calculated_value: "승인됨",
  calculated_type: "string",
};

export const BINDING = {
  bundle_id: "bundle-a",
  snapshot_id: SHA_A,
  workbook_sha256: SHA_B,
  scope: { kind: "sheet" as const, sheet: "C", id: SHA_C },
};

export const OFFICE_BINDING = {
  session_id: "office-session-a",
  revision_id: "revision-a",
  worksheet_id: "worksheet-a",
  sheet: "C",
};

export const PREVIEW: EditPreview = {
  schema_version: "audit_workbook_edit_preview.v1",
  preview_ref: `edit-preview:${SHA_D}`,
  preview_sha256: SHA_D,
  proposal_ref: `edit-proposal:${SHA_C}`,
  proposal_sha256: SHA_C,
  binding: BINDING,
  office_binding: OFFICE_BINDING,
  before: [BLANK_STATE],
  before_sha256: SHA_A,
  expected_after: [
    { cell: "A1", authored: { kind: "value", value: "승인됨" }, number_format: "General" },
  ],
  expected_after_sha256: SHA_B,
  diff: [
    {
      change_ref: `edit-change:${SHA_E}`,
      cell: "A1",
      kind: "set_value",
      before: BLANK_STATE,
      after: {
        cell: "A1",
        authored: { kind: "value", value: "승인됨" },
        number_format: "General",
      },
    },
  ],
  diff_sha256: SHA_E,
};

export const MANIFEST: ApplyManifest = {
  schema_version: "audit_workbook_edit_apply_manifest.v1",
  manifest_ref: `edit-manifest:${SHA_F}`,
  manifest_sha256: SHA_F,
  proposal_ref: PREVIEW.proposal_ref,
  preview_ref: PREVIEW.preview_ref,
  approval_ref: `edit-approval:${SHA_A}`,
  approval_expires_at: "2099-07-14T00:05:00Z",
  execution_id: "execution-a",
  fencing_token: 7,
  challenge_nonce: "challenge-a",
  status: "proposed",
  review_status: "unreviewed",
  application_status: "not_applied",
  outside_prepared_bundle: true,
  binding: BINDING,
  office_binding: OFFICE_BINDING,
  before: PREVIEW.before,
  before_sha256: PREVIEW.before_sha256,
  expected_after: PREVIEW.expected_after,
  expected_after_sha256: PREVIEW.expected_after_sha256,
  diff: PREVIEW.diff,
  diff_sha256: PREVIEW.diff_sha256,
  limitations: ["bounded", "executor-readback", "outside-prepared-bundle"],
};

export const WORKFLOW: WorkflowDocument = {
  schema_version: "audit_workbook_edit_workflow.v1",
  workflow_id: "workflow-a",
  session_id: OFFICE_BINDING.session_id,
  bundle_id: BINDING.bundle_id,
  snapshot_id: BINDING.snapshot_id,
  workbook_sha256: BINDING.workbook_sha256,
  revision_id: OFFICE_BINDING.revision_id,
  sheet: OFFICE_BINDING.sheet,
  worksheet_id: OFFICE_BINDING.worksheet_id,
  state: "approved",
  artifacts: {
    proposal: {
      schema_version: "audit_workbook_edit_proposal.v1",
      proposal_ref: PREVIEW.proposal_ref,
      proposal_sha256: PREVIEW.proposal_sha256,
      binding: BINDING,
      changes: [
        {
          change_ref: PREVIEW.diff[0]!.change_ref,
          cell: "A1",
          kind: "set_value",
          value: "승인됨",
        },
      ],
    },
    preview: PREVIEW,
    approval: { approval_ref: MANIFEST.approval_ref },
    manifest: null,
    verification: null,
  },
};

export const WORKFLOW_RESPONSE: WorkflowResponse = {
  schema_version: "audit_workbook_edit_http_workflow.v1",
  workflow: WORKFLOW,
};

export const CLAIM_SUBMISSION: SubmissionResponse = submission("claimed", {
  execution_id: MANIFEST.execution_id,
  fence: MANIFEST.fencing_token,
  challenge: MANIFEST.challenge_nonce,
  apply_manifest: MANIFEST,
});

export const START_SUBMISSION: SubmissionResponse = submission("apply_started", {
  execution_id: MANIFEST.execution_id,
  fence: MANIFEST.fencing_token,
  manifest_ref: MANIFEST.manifest_ref,
  write_started: true,
  execution_deadline: "2099-07-14T00:10:00Z",
});

export function terminalSubmission(
  status: VerificationSummary["status"],
): SubmissionResponse {
  const applicationStatus =
    status === "session_verified"
      ? "applied_session_verified"
      : status === "verification_failed"
        ? "application_failed"
      : status === "stale_precondition"
        ? "not_applied"
        : "indeterminate";
  return submission(status, {
    verification: {
      schema_version: "audit_workbook_edit_verification.v1",
      verification_ref: `edit-verification:${SHA_F}`,
      verification_sha256: SHA_F,
      manifest_ref: MANIFEST.manifest_ref,
      witness_ref: `edit-witness:${SHA_E}`,
      status,
      review_status: "unreviewed",
      application_status: applicationStatus,
      asset_persisted: false,
      new_snapshot_required: status !== "stale_precondition",
      outside_prepared_bundle: true,
      before_matches: status !== "stale_precondition",
      expected_after_matches:
        status === "session_verified"
          ? true
          : status === "verification_failed"
            ? false
            : null,
      actual_after_sha256:
        status === "session_verified" || status === "verification_failed" ? SHA_D : null,
      limitations: ["bounded", "executor-readback", "outside-prepared-bundle"],
    },
  });
}

export function submission(
  state: SubmissionResponse["receipt"]["state"],
  details: Record<string, unknown>,
): SubmissionResponse {
  return {
    schema_version: "audit_workbook_edit_http_submission.v1",
    replayed: false,
    receipt: {
      schema_version: "audit_workbook_edit_receipt.v1",
      command_id: `command-${state}`,
      workflow_id: WORKFLOW.workflow_id,
      session_id: WORKFLOW.session_id,
      bundle_id: WORKFLOW.bundle_id,
      snapshot_id: WORKFLOW.snapshot_id,
      workbook_sha256: WORKFLOW.workbook_sha256,
      revision_id: WORKFLOW.revision_id,
      sheet: WORKFLOW.sheet,
      state,
      details,
    },
  };
}

export function clone<T>(value: T): T {
  return structuredClone(value);
}
