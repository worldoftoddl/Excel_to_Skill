export const MAX_EDIT_CELLS = 100;
export const MAX_SAFE_NUMBER = Number.MAX_SAFE_INTEGER;

const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const CELL = /^[A-Z]{1,3}[1-9][0-9]*$/;
const MAX_EXCEL_COLUMNS = 16_384;
const MAX_EXCEL_ROWS = 1_048_576;

export type Scalar = string | number | boolean;
export type CalculatedType = "empty" | "string" | "number" | "boolean" | "error";
export type SpillState = "none" | "parent" | "child";
export type EditKind =
  | "set_value"
  | "set_formula"
  | "set_number_format"
  | "clear_contents";
export type WorkflowState =
  | "proposed"
  | "previewed"
  | "approved"
  | "rejected"
  | "claimed"
  | "apply_started"
  | "session_verified"
  | "verification_failed"
  | "indeterminate"
  | "stale_precondition"
  | "aborted_before_apply";

export type AuthoredState =
  | { kind: "blank" }
  | { kind: "value"; value: Scalar }
  | { kind: "formula"; formula: string };

export interface TargetConstraints {
  merged: boolean;
  spill: SpillState;
  protected: boolean;
  table_member: boolean;
}

export interface CellState {
  cell: string;
  authored: AuthoredState;
  calculated_value: Scalar | null;
  calculated_type: CalculatedType;
  number_format: string;
  target_constraints: TargetConstraints;
}

export interface AuthoredCellState {
  cell: string;
  authored: AuthoredState;
  number_format: string;
}

export interface EditChange {
  change_ref: string;
  cell: string;
  kind: EditKind;
  before: CellState;
  after: AuthoredCellState;
}

export interface ProposalChange {
  change_ref: string;
  cell: string;
  kind: EditKind;
  value?: Scalar;
  formula?: string;
  number_format?: string;
}

export interface EditProposal {
  schema_version: "audit_workbook_edit_proposal.v1";
  proposal_ref: string;
  proposal_sha256: string;
  binding: WorkbookBinding;
  changes: ProposalChange[];
}

export interface WorkbookBinding {
  bundle_id: string;
  snapshot_id: string;
  workbook_sha256: string;
  scope: {
    kind: "sheet";
    sheet: string;
    id: string;
  };
}

export interface OfficeBinding {
  session_id: string;
  revision_id: string;
  worksheet_id: string;
  sheet: string;
}

export interface EditPreview {
  schema_version: "audit_workbook_edit_preview.v1";
  preview_ref: string;
  preview_sha256: string;
  proposal_ref: string;
  proposal_sha256: string;
  binding: WorkbookBinding;
  office_binding: OfficeBinding;
  before: CellState[];
  before_sha256: string;
  expected_after: AuthoredCellState[];
  expected_after_sha256: string;
  diff: EditChange[];
  diff_sha256: string;
}

export interface ApplyManifest {
  schema_version: "audit_workbook_edit_apply_manifest.v1";
  manifest_ref: string;
  manifest_sha256: string;
  proposal_ref: string;
  preview_ref: string;
  approval_ref: string;
  approval_expires_at: string;
  execution_id: string;
  fencing_token: number;
  challenge_nonce: string;
  status: "proposed";
  review_status: "unreviewed";
  application_status: "not_applied";
  outside_prepared_bundle: true;
  binding: WorkbookBinding;
  office_binding: OfficeBinding;
  before: CellState[];
  before_sha256: string;
  expected_after: AuthoredCellState[];
  expected_after_sha256: string;
  diff: EditChange[];
  diff_sha256: string;
  limitations: [string, string, string];
}

export interface WorkbookEditReceipt {
  schema_version: "audit_workbook_edit_receipt.v1";
  command_id: string;
  workflow_id: string;
  session_id: string;
  bundle_id: string;
  snapshot_id: string;
  workbook_sha256: string;
  revision_id: string;
  sheet: string;
  state: WorkflowState;
  details: Record<string, unknown>;
}

export interface SubmissionResponse {
  schema_version: "audit_workbook_edit_http_submission.v1";
  replayed: boolean;
  receipt: WorkbookEditReceipt;
}

export interface ClaimDetails {
  execution_id: string;
  fence: number;
  challenge: string;
  apply_manifest: ApplyManifest;
}

export interface StartDetails {
  execution_id: string;
  fence: number;
  manifest_ref: string;
  write_started: true;
  execution_deadline: string;
}

export interface VerificationSummary {
  schema_version: "audit_workbook_edit_verification.v1";
  verification_ref: string;
  verification_sha256: string;
  manifest_ref: string;
  witness_ref: string;
  status:
    | "session_verified"
    | "verification_failed"
    | "indeterminate"
    | "stale_precondition";
  application_status:
    | "applied_session_verified"
    | "not_applied"
    | "application_failed"
    | "indeterminate";
  asset_persisted: false;
  new_snapshot_required: boolean;
  review_status: "unreviewed";
  outside_prepared_bundle: true;
  before_matches: boolean;
  expected_after_matches: boolean | null;
  actual_after_sha256: string | null;
  limitations: [string, string, string];
}

export interface WorkflowDocument {
  schema_version: "audit_workbook_edit_workflow.v1";
  workflow_id: string;
  session_id: string;
  bundle_id: string;
  snapshot_id: string;
  workbook_sha256: string;
  revision_id: string;
  sheet: string;
  worksheet_id: string;
  state: WorkflowState;
  artifacts: {
    proposal: EditProposal;
    preview: EditPreview | null;
    approval: Record<string, unknown> | null;
    manifest: Record<string, unknown> | null;
    verification: VerificationSummary | null;
  };
}

export interface WorkflowResponse {
  schema_version: "audit_workbook_edit_http_workflow.v1";
  workflow: WorkflowDocument;
}

export interface WitnessInput {
  outcome: "applied" | "stale_precondition" | "indeterminate";
  observed_before: CellState[];
  actual_after: CellState[] | null;
  recalculation: "none" | "recalculate";
}

export class WorkbookEditContractError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "WorkbookEditContractError";
    this.code = code;
  }
}

export function decodeWorkflowResponse(value: unknown): WorkflowResponse {
  const response = record(value, "workflow response");
  exactKeys(response, ["schema_version", "workflow"], "workflow response");
  literal(response.schema_version, "audit_workbook_edit_http_workflow.v1", "schema_version");
  const workflow = decodeWorkflow(response.workflow);
  return { schema_version: "audit_workbook_edit_http_workflow.v1", workflow };
}

export function decodeSubmissionResponse(value: unknown): SubmissionResponse {
  const response = record(value, "submission response");
  exactKeys(response, ["schema_version", "replayed", "receipt"], "submission response");
  literal(response.schema_version, "audit_workbook_edit_http_submission.v1", "schema_version");
  if (typeof response.replayed !== "boolean") {
    fail("INVALID_RESPONSE", "submission replay flag가 유효하지 않습니다.");
  }
  return {
    schema_version: "audit_workbook_edit_http_submission.v1",
    replayed: response.replayed,
    receipt: decodeReceipt(response.receipt),
  };
}

export function claimDetails(submission: SubmissionResponse): ClaimDetails {
  if (submission.receipt.state !== "claimed") {
    fail("INVALID_CLAIM", "claim 응답 상태가 claimed가 아닙니다.");
  }
  const details = record(submission.receipt.details, "claim details");
  exactKeys(details, ["execution_id", "fence", "challenge", "apply_manifest"], "claim details");
  const executionId = opaque(details.execution_id, "execution_id");
  const fence = safePositiveInteger(details.fence, "fence");
  const challenge = opaque(details.challenge, "challenge");
  const manifest = decodeApplyManifest(details.apply_manifest);
  if (
    manifest.execution_id !== executionId ||
    manifest.fencing_token !== fence ||
    manifest.challenge_nonce !== challenge
  ) {
    fail("INVALID_CLAIM", "claim capability와 apply manifest가 일치하지 않습니다.");
  }
  return {
    execution_id: executionId,
    fence,
    challenge,
    apply_manifest: manifest,
  };
}

export function previewFromSubmission(submission: SubmissionResponse): EditPreview {
  if (submission.receipt.state !== "previewed") {
    fail("INVALID_PREVIEW", "preview 응답 상태가 previewed가 아닙니다.");
  }
  const details = record(submission.receipt.details, "preview details");
  exactKeys(details, ["preview"], "preview details");
  return decodePreview(details.preview);
}

export function startDetails(submission: SubmissionResponse, claim: ClaimDetails): StartDetails {
  if (submission.receipt.state !== "apply_started") {
    fail("INVALID_START", "write-start 응답 상태가 apply_started가 아닙니다.");
  }
  const details = record(submission.receipt.details, "start details");
  exactKeys(
    details,
    ["execution_id", "fence", "manifest_ref", "write_started", "execution_deadline"],
    "start details",
  );
  const result: StartDetails = {
    execution_id: opaque(details.execution_id, "execution_id"),
    fence: safePositiveInteger(details.fence, "fence"),
    manifest_ref: manifestRef(details.manifest_ref, "manifest_ref"),
    write_started: literal(details.write_started, true, "write_started"),
    execution_deadline: isoTimestamp(details.execution_deadline, "execution_deadline"),
  };
  if (
    result.execution_id !== claim.execution_id ||
    result.fence !== claim.fence ||
    result.manifest_ref !== claim.apply_manifest.manifest_ref
  ) {
    fail("INVALID_START", "write-start 응답이 claim과 일치하지 않습니다.");
  }
  return result;
}

export function verificationFromSubmission(
  submission: SubmissionResponse,
  expectedManifestRef?: string,
): VerificationSummary {
  const terminalStates = new Set([
    "session_verified",
    "verification_failed",
    "indeterminate",
    "stale_precondition",
  ]);
  if (!terminalStates.has(submission.receipt.state)) {
    fail("INVALID_VERIFICATION", "verify 응답이 terminal 상태가 아닙니다.");
  }
  const details = record(submission.receipt.details, "verification details");
  exactKeys(details, ["verification"], "verification details");
  const verification = decodeVerificationSummary(details.verification);
  if (verification.status !== submission.receipt.state) {
    fail("INVALID_VERIFICATION", "receipt와 verification 상태가 일치하지 않습니다.");
  }
  if (
    expectedManifestRef !== undefined &&
    verification.manifest_ref !== expectedManifestRef
  ) {
    fail("INVALID_VERIFICATION", "verification이 accepted claim manifest와 일치하지 않습니다.");
  }
  return verification;
}

export function assertManifestForWorkflow(
  manifest: ApplyManifest,
  workflow: WorkflowDocument,
): void {
  const preview = workflow.artifacts.preview;
  const approval = workflow.artifacts.approval;
  if (
    !deepJsonEqual(manifest.office_binding, {
      session_id: workflow.session_id,
      revision_id: workflow.revision_id,
      worksheet_id: workflow.worksheet_id,
      sheet: workflow.sheet,
    }) ||
    !deepJsonEqual(manifest.binding, workflow.artifacts.proposal.binding) ||
    manifest.proposal_ref !== workflow.artifacts.proposal.proposal_ref ||
    preview === null ||
    !deepJsonEqual(manifest.binding, preview.binding) ||
    !deepJsonEqual(manifest.office_binding, preview.office_binding) ||
    manifest.preview_ref !== preview.preview_ref ||
    manifest.before_sha256 !== preview.before_sha256 ||
    manifest.expected_after_sha256 !== preview.expected_after_sha256 ||
    manifest.diff_sha256 !== preview.diff_sha256 ||
    !deepJsonEqual(manifest.before, preview.before) ||
    !deepJsonEqual(manifest.expected_after, preview.expected_after) ||
    !deepJsonEqual(manifest.diff, preview.diff) ||
    approval === null ||
    manifest.approval_ref !== approval.approval_ref
  ) {
    fail("WORKBOOK_BINDING_MISMATCH", "manifest가 현재 workflow binding과 다릅니다.");
  }
}

export function assertReceiptForWorkflow(
  submission: SubmissionResponse,
  workflow: WorkflowDocument,
): void {
  const receipt = submission.receipt;
  if (
    receipt.workflow_id !== workflow.workflow_id ||
    receipt.session_id !== workflow.session_id ||
    receipt.bundle_id !== workflow.bundle_id ||
    receipt.snapshot_id !== workflow.snapshot_id ||
    receipt.workbook_sha256 !== workflow.workbook_sha256 ||
    receipt.revision_id !== workflow.revision_id ||
    receipt.sheet !== workflow.sheet
  ) {
    fail("WORKBOOK_BINDING_MISMATCH", "receipt가 불러온 workflow와 일치하지 않습니다.");
  }
}

export function assertReceiptWorkflowId(
  submission: SubmissionResponse,
  workflowId: string,
): void {
  if (submission.receipt.workflow_id !== workflowId) {
    fail("WORKBOOK_BINDING_MISMATCH", "receipt workflow ID가 요청과 일치하지 않습니다.");
  }
}

export function exactCellStates(left: CellState[], right: CellState[]): boolean {
  return deepJsonEqual(left, right);
}

export function deepJsonEqual(left: unknown, right: unknown): boolean {
  if (left === right) {
    return true;
  }
  if (
    typeof left !== "object" ||
    left === null ||
    typeof right !== "object" ||
    right === null
  ) {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((value, index) => deepJsonEqual(value, right[index]));
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord).sort();
  const rightKeys = Object.keys(rightRecord).sort();
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key, index) => key === rightKeys[index] && deepJsonEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

export function formatPreview(preview: EditPreview): string {
  return preview.diff
    .map((item) => {
      const before = authoredLabel(item.before.authored, item.before.number_format);
      const after = authoredLabel(item.after.authored, item.after.number_format);
      return `${item.cell} · ${item.kind}\n  이전: ${before}\n  이후: ${after}`;
    })
    .join("\n\n");
}

function decodeWorkflow(value: unknown): WorkflowDocument {
  const workflow = record(value, "workflow");
  const artifacts = record(workflow.artifacts, "workflow artifacts");
  const proposal = decodeProposal(artifacts.proposal);
  const preview = artifacts.preview === null ? null : decodePreview(artifacts.preview);
  const state = workflowState(workflow.state);
  const result: WorkflowDocument = {
    schema_version: literal(
      workflow.schema_version,
      "audit_workbook_edit_workflow.v1",
      "workflow.schema_version",
    ),
    workflow_id: opaque(workflow.workflow_id, "workflow_id"),
    session_id: opaque(workflow.session_id, "session_id"),
    bundle_id: opaque(workflow.bundle_id, "bundle_id"),
    snapshot_id: sha256(workflow.snapshot_id, "snapshot_id"),
    workbook_sha256: sha256(workflow.workbook_sha256, "workbook_sha256"),
    revision_id: opaque(workflow.revision_id, "revision_id"),
    sheet: sheet(workflow.sheet, "sheet"),
    worksheet_id: opaque(workflow.worksheet_id, "worksheet_id"),
    state,
    artifacts: {
      proposal,
      preview,
      approval: nullableRecord(artifacts.approval, "approval"),
      manifest: nullableRecord(artifacts.manifest, "manifest"),
      verification:
        artifacts.verification === null
          ? null
          : decodeVerificationSummary(artifacts.verification),
    },
  };
  if (
    result.bundle_id !== proposal.binding.bundle_id ||
    result.snapshot_id !== proposal.binding.snapshot_id ||
    result.workbook_sha256 !== proposal.binding.workbook_sha256 ||
    result.sheet !== proposal.binding.scope.sheet
  ) {
    fail("INVALID_RESPONSE", "workflow와 proposal binding이 일치하지 않습니다.");
  }
  return result;
}

function decodeReceipt(value: unknown): WorkbookEditReceipt {
  const receipt = record(value, "receipt");
  exactKeys(
    receipt,
    [
      "schema_version",
      "command_id",
      "workflow_id",
      "session_id",
      "bundle_id",
      "snapshot_id",
      "workbook_sha256",
      "revision_id",
      "sheet",
      "state",
      "details",
    ],
    "receipt",
  );
  return {
    schema_version: literal(
      receipt.schema_version,
      "audit_workbook_edit_receipt.v1",
      "receipt.schema_version",
    ),
    command_id: opaque(receipt.command_id, "command_id"),
    workflow_id: opaque(receipt.workflow_id, "workflow_id"),
    session_id: opaque(receipt.session_id, "session_id"),
    bundle_id: opaque(receipt.bundle_id, "bundle_id"),
    snapshot_id: sha256(receipt.snapshot_id, "snapshot_id"),
    workbook_sha256: sha256(receipt.workbook_sha256, "workbook_sha256"),
    revision_id: opaque(receipt.revision_id, "revision_id"),
    sheet: sheet(receipt.sheet, "sheet"),
    state: workflowState(receipt.state),
    details: record(receipt.details, "receipt.details"),
  };
}

function decodeApplyManifest(value: unknown): ApplyManifest {
  const manifest = record(value, "apply manifest");
  exactKeys(
    manifest,
    [
      "schema_version",
      "manifest_ref",
      "manifest_sha256",
      "proposal_ref",
      "preview_ref",
      "approval_ref",
      "approval_expires_at",
      "execution_id",
      "fencing_token",
      "challenge_nonce",
      "status",
      "review_status",
      "application_status",
      "outside_prepared_bundle",
      "binding",
      "office_binding",
      "before",
      "before_sha256",
      "expected_after",
      "expected_after_sha256",
      "diff",
      "diff_sha256",
      "limitations",
    ],
    "apply manifest",
  );
  const before = stateList(manifest.before, "before");
  const expectedAfter = authoredList(manifest.expected_after, "expected_after");
  const diff = diffList(manifest.diff, "diff");
  const limitations = stringList(manifest.limitations, "limitations", 3, 3) as [
    string,
    string,
    string,
  ];
  const result: ApplyManifest = {
    schema_version: literal(
      manifest.schema_version,
      "audit_workbook_edit_apply_manifest.v1",
      "manifest.schema_version",
    ),
    manifest_ref: manifestRef(manifest.manifest_ref, "manifest_ref"),
    manifest_sha256: sha256(manifest.manifest_sha256, "manifest_sha256"),
    proposal_ref: prefixedSha(manifest.proposal_ref, "edit-proposal:", "proposal_ref"),
    preview_ref: prefixedSha(manifest.preview_ref, "edit-preview:", "preview_ref"),
    approval_ref: prefixedSha(manifest.approval_ref, "edit-approval:", "approval_ref"),
    approval_expires_at: isoTimestamp(manifest.approval_expires_at, "approval_expires_at"),
    execution_id: opaque(manifest.execution_id, "execution_id"),
    fencing_token: safePositiveInteger(manifest.fencing_token, "fencing_token"),
    challenge_nonce: opaque(manifest.challenge_nonce, "challenge_nonce"),
    status: literal(manifest.status, "proposed", "status"),
    review_status: literal(manifest.review_status, "unreviewed", "review_status"),
    application_status: literal(
      manifest.application_status,
      "not_applied",
      "application_status",
    ),
    outside_prepared_bundle: literal(
      manifest.outside_prepared_bundle,
      true,
      "outside_prepared_bundle",
    ),
    binding: decodeBinding(manifest.binding),
    office_binding: decodeOfficeBinding(manifest.office_binding),
    before,
    before_sha256: sha256(manifest.before_sha256, "before_sha256"),
    expected_after: expectedAfter,
    expected_after_sha256: sha256(
      manifest.expected_after_sha256,
      "expected_after_sha256",
    ),
    diff,
    diff_sha256: sha256(manifest.diff_sha256, "diff_sha256"),
    limitations,
  };
  assertExactDiff(result.before, result.expected_after, result.diff);
  if (
    result.binding.scope.sheet !== result.office_binding.sheet ||
    result.before.some((item) => !isSafeTarget(item.target_constraints))
  ) {
    fail("UNSAFE_MANIFEST", "manifest target 또는 sheet binding이 안전하지 않습니다.");
  }
  return result;
}

function decodeProposal(value: unknown): EditProposal {
  const proposal = record(value, "proposal");
  const changesRaw = array(proposal.changes, "proposal.changes");
  if (changesRaw.length < 1 || changesRaw.length > MAX_EDIT_CELLS) {
    fail("INVALID_RESPONSE", "proposal change 개수가 유효하지 않습니다.");
  }
  const changes = changesRaw.map((item) => decodeProposalChange(item));
  uniqueCells(changes.map((item) => item.cell), "proposal");
  return {
    schema_version: literal(
      proposal.schema_version,
      "audit_workbook_edit_proposal.v1",
      "proposal.schema_version",
    ),
    proposal_ref: prefixedSha(proposal.proposal_ref, "edit-proposal:", "proposal_ref"),
    proposal_sha256: sha256(proposal.proposal_sha256, "proposal_sha256"),
    binding: decodeBinding(proposal.binding),
    changes,
  };
}

function decodeProposalChange(value: unknown): ProposalChange {
  const change = record(value, "proposal change");
  const kind = editKind(change.kind);
  const base = {
    change_ref: prefixedSha(change.change_ref, "edit-change:", "change_ref"),
    cell: cell(change.cell, "cell"),
    kind,
  };
  if (kind === "set_value") {
    exactKeys(change, ["change_ref", "cell", "kind", "value"], "set_value change");
    return { ...base, value: scalar(change.value, "value") };
  }
  if (kind === "set_formula") {
    exactKeys(change, ["change_ref", "cell", "kind", "formula"], "set_formula change");
    return { ...base, formula: formula(change.formula, "formula") };
  }
  if (kind === "set_number_format") {
    exactKeys(
      change,
      ["change_ref", "cell", "kind", "number_format"],
      "set_number_format change",
    );
    return { ...base, number_format: numberFormat(change.number_format, "number_format") };
  }
  exactKeys(change, ["change_ref", "cell", "kind"], "clear_contents change");
  return base;
}

function decodePreview(value: unknown): EditPreview {
  const preview = record(value, "preview");
  const result: EditPreview = {
    schema_version: literal(
      preview.schema_version,
      "audit_workbook_edit_preview.v1",
      "preview.schema_version",
    ),
    preview_ref: prefixedSha(preview.preview_ref, "edit-preview:", "preview_ref"),
    preview_sha256: sha256(preview.preview_sha256, "preview_sha256"),
    proposal_ref: prefixedSha(preview.proposal_ref, "edit-proposal:", "proposal_ref"),
    proposal_sha256: sha256(preview.proposal_sha256, "proposal_sha256"),
    binding: decodeBinding(preview.binding),
    office_binding: decodeOfficeBinding(preview.office_binding),
    before: stateList(preview.before, "before"),
    before_sha256: sha256(preview.before_sha256, "before_sha256"),
    expected_after: authoredList(preview.expected_after, "expected_after"),
    expected_after_sha256: sha256(preview.expected_after_sha256, "expected_after_sha256"),
    diff: diffList(preview.diff, "diff"),
    diff_sha256: sha256(preview.diff_sha256, "diff_sha256"),
  };
  assertExactDiff(result.before, result.expected_after, result.diff);
  return result;
}

function decodeBinding(value: unknown): WorkbookBinding {
  const binding = record(value, "binding");
  const scope = record(binding.scope, "scope");
  return {
    bundle_id: opaque(binding.bundle_id, "bundle_id"),
    snapshot_id: sha256(binding.snapshot_id, "snapshot_id"),
    workbook_sha256: sha256(binding.workbook_sha256, "workbook_sha256"),
    scope: {
      kind: literal(scope.kind, "sheet", "scope.kind"),
      sheet: sheet(scope.sheet, "scope.sheet"),
      id: sha256(scope.id, "scope.id"),
    },
  };
}

function decodeOfficeBinding(value: unknown): OfficeBinding {
  const binding = record(value, "office binding");
  exactKeys(
    binding,
    ["session_id", "revision_id", "worksheet_id", "sheet"],
    "office binding",
  );
  return {
    session_id: opaque(binding.session_id, "session_id"),
    revision_id: opaque(binding.revision_id, "revision_id"),
    worksheet_id: opaque(binding.worksheet_id, "worksheet_id"),
    sheet: sheet(binding.sheet, "sheet"),
  };
}

function decodeVerificationSummary(value: unknown): VerificationSummary {
  const verification = record(value, "verification");
  exactKeys(
    verification,
    [
      "schema_version",
      "verification_ref",
      "verification_sha256",
      "manifest_ref",
      "witness_ref",
      "status",
      "review_status",
      "application_status",
      "asset_persisted",
      "new_snapshot_required",
      "outside_prepared_bundle",
      "before_matches",
      "expected_after_matches",
      "actual_after_sha256",
      "limitations",
    ],
    "verification",
  );
  const status = verification.status;
  if (
    status !== "session_verified" &&
    status !== "verification_failed" &&
    status !== "indeterminate" &&
    status !== "stale_precondition"
  ) {
    fail("INVALID_RESPONSE", "verification status가 유효하지 않습니다.");
  }
  const applicationStatus = verification.application_status;
  if (
    applicationStatus !== "applied_session_verified" &&
    applicationStatus !== "not_applied" &&
    applicationStatus !== "application_failed" &&
    applicationStatus !== "indeterminate"
  ) {
    fail("INVALID_RESPONSE", "application status가 유효하지 않습니다.");
  }
  literal(verification.asset_persisted, false, "asset_persisted");
  if (typeof verification.new_snapshot_required !== "boolean") {
    fail("INVALID_RESPONSE", "new_snapshot_required가 유효하지 않습니다.");
  }
  if (typeof verification.before_matches !== "boolean") {
    fail("INVALID_RESPONSE", "before_matches가 유효하지 않습니다.");
  }
  if (
    verification.expected_after_matches !== null &&
    typeof verification.expected_after_matches !== "boolean"
  ) {
    fail("INVALID_RESPONSE", "expected_after_matches가 유효하지 않습니다.");
  }
  const actualAfterSha256 = verification.actual_after_sha256 === null
    ? null
    : sha256(verification.actual_after_sha256, "actual_after_sha256");
  const limitations = stringList(verification.limitations, "limitations", 3, 3) as [
    string,
    string,
    string,
  ];
  if (new Set(limitations).size !== limitations.length) {
    fail("INVALID_RESPONSE", "verification limitations가 중복됩니다.");
  }
  assertVerificationStatus(status, applicationStatus, verification.new_snapshot_required);
  const result: VerificationSummary = {
    schema_version: literal(
      verification.schema_version,
      "audit_workbook_edit_verification.v1",
      "verification.schema_version",
    ),
    verification_ref: prefixedSha(
      verification.verification_ref,
      "edit-verification:",
      "verification_ref",
    ),
    verification_sha256: sha256(
      verification.verification_sha256,
      "verification_sha256",
    ),
    manifest_ref: manifestRef(verification.manifest_ref, "manifest_ref"),
    witness_ref: prefixedSha(verification.witness_ref, "edit-witness:", "witness_ref"),
    status,
    application_status: applicationStatus,
    asset_persisted: false,
    new_snapshot_required: verification.new_snapshot_required,
    review_status: literal(verification.review_status, "unreviewed", "review_status"),
    outside_prepared_bundle: literal(
      verification.outside_prepared_bundle,
      true,
      "outside_prepared_bundle",
    ),
    before_matches: verification.before_matches,
    expected_after_matches: verification.expected_after_matches,
    actual_after_sha256: actualAfterSha256,
    limitations,
  };
  if (result.verification_ref.slice("edit-verification:".length) !== result.verification_sha256) {
    fail("INVALID_RESPONSE", "verification ref와 digest가 일치하지 않습니다.");
  }
  if (
    (status === "session_verified" &&
      (!result.before_matches ||
        result.expected_after_matches !== true ||
        result.actual_after_sha256 === null)) ||
    (status === "stale_precondition" &&
      (result.before_matches ||
        result.expected_after_matches !== null ||
        result.actual_after_sha256 !== null))
  ) {
    fail("INVALID_RESPONSE", "verification status와 evidence 필드 조합이 유효하지 않습니다.");
  }
  return result;
}

function stateList(value: unknown, field: string): CellState[] {
  const values = array(value, field);
  if (values.length < 1 || values.length > MAX_EDIT_CELLS) {
    fail("INVALID_RESPONSE", `${field} cell 개수가 유효하지 않습니다.`);
  }
  const states = values.map((item) => decodeCellState(item));
  uniqueCells(states.map((item) => item.cell), field);
  return states;
}

function authoredList(value: unknown, field: string): AuthoredCellState[] {
  const values = array(value, field);
  if (values.length < 1 || values.length > MAX_EDIT_CELLS) {
    fail("INVALID_RESPONSE", `${field} cell 개수가 유효하지 않습니다.`);
  }
  const states = values.map((item) => {
    const state = record(item, field);
    exactKeys(state, ["cell", "authored", "number_format"], field);
    return {
      cell: cell(state.cell, "cell"),
      authored: decodeAuthored(state.authored),
      number_format: numberFormat(state.number_format, "number_format"),
    };
  });
  uniqueCells(states.map((item) => item.cell), field);
  return states;
}

function diffList(value: unknown, field: string): EditChange[] {
  const values = array(value, field);
  if (values.length < 1 || values.length > MAX_EDIT_CELLS) {
    fail("INVALID_RESPONSE", `${field} change 개수가 유효하지 않습니다.`);
  }
  return values.map((item) => {
    const change = record(item, field);
    exactKeys(change, ["change_ref", "cell", "kind", "before", "after"], field);
    return {
      change_ref: prefixedSha(change.change_ref, "edit-change:", "change_ref"),
      cell: cell(change.cell, "cell"),
      kind: editKind(change.kind),
      before: decodeCellState(change.before),
      after: authoredList([change.after], "diff.after")[0]!,
    };
  });
}

function decodeCellState(value: unknown): CellState {
  const state = record(value, "cell state");
  exactKeys(
    state,
    [
      "cell",
      "authored",
      "calculated_value",
      "calculated_type",
      "number_format",
      "target_constraints",
    ],
    "cell state",
  );
  const authored = decodeAuthored(state.authored);
  const calculatedType = calculatedTypeValue(state.calculated_type);
  const calculatedValue = nullableScalar(state.calculated_value, "calculated_value");
  assertCalculatedValue(authored, calculatedType, calculatedValue);
  return {
    cell: cell(state.cell, "cell"),
    authored,
    calculated_value: calculatedValue,
    calculated_type: calculatedType,
    number_format: numberFormat(state.number_format, "number_format"),
    target_constraints: decodeConstraints(state.target_constraints),
  };
}

function decodeAuthored(value: unknown): AuthoredState {
  const authored = record(value, "authored");
  if (authored.kind === "blank") {
    exactKeys(authored, ["kind"], "blank authored");
    return { kind: "blank" };
  }
  if (authored.kind === "value") {
    exactKeys(authored, ["kind", "value"], "value authored");
    return { kind: "value", value: scalar(authored.value, "authored.value") };
  }
  if (authored.kind === "formula") {
    exactKeys(authored, ["kind", "formula"], "formula authored");
    return { kind: "formula", formula: formula(authored.formula, "authored.formula") };
  }
  return fail("INVALID_RESPONSE", "authored kind가 유효하지 않습니다.");
}

function decodeConstraints(value: unknown): TargetConstraints {
  const constraints = record(value, "target constraints");
  exactKeys(
    constraints,
    ["merged", "spill", "protected", "table_member"],
    "target constraints",
  );
  if (
    typeof constraints.merged !== "boolean" ||
    typeof constraints.protected !== "boolean" ||
    typeof constraints.table_member !== "boolean" ||
    (constraints.spill !== "none" &&
      constraints.spill !== "parent" &&
      constraints.spill !== "child")
  ) {
    return fail("INVALID_RESPONSE", "target constraint 값이 유효하지 않습니다.");
  }
  return {
    merged: constraints.merged,
    spill: constraints.spill,
    protected: constraints.protected,
    table_member: constraints.table_member,
  };
}

function assertExactDiff(
  before: CellState[],
  expected: AuthoredCellState[],
  diff: EditChange[],
): void {
  if (before.length !== expected.length || before.length !== diff.length) {
    fail("INVALID_RESPONSE", "manifest cell 집합 크기가 일치하지 않습니다.");
  }
  for (let index = 0; index < before.length; index += 1) {
    const beforeItem = before[index]!;
    const expectedItem = expected[index]!;
    const diffItem = diff[index]!;
    if (
      beforeItem.cell !== expectedItem.cell ||
      beforeItem.cell !== diffItem.cell ||
      !deepJsonEqual(beforeItem, diffItem.before) ||
      !deepJsonEqual(expectedItem, diffItem.after)
    ) {
      fail("INVALID_RESPONSE", "manifest exact diff가 일치하지 않습니다.");
    }
    const beforeProjection: AuthoredCellState = {
      cell: beforeItem.cell,
      authored: beforeItem.authored,
      number_format: beforeItem.number_format,
    };
    if (deepJsonEqual(beforeProjection, expectedItem)) {
      fail("INVALID_RESPONSE", "manifest에 no-op edit가 있습니다.");
    }
    if (diffItem.kind === "set_value") {
      if (
        expectedItem.authored.kind !== "value" ||
        expectedItem.number_format !== beforeItem.number_format ||
        (typeof expectedItem.authored.value === "string" &&
          (expectedItem.authored.value === "" ||
            /^[\s]*[=+\-@]/.test(expectedItem.authored.value)))
      ) {
        fail("UNSAFE_MANIFEST", "set_value manifest semantics가 안전하지 않습니다.");
      }
    } else if (diffItem.kind === "set_formula") {
      if (
        expectedItem.authored.kind !== "formula" ||
        expectedItem.number_format !== beforeItem.number_format
      ) {
        fail("UNSAFE_MANIFEST", "set_formula manifest semantics가 안전하지 않습니다.");
      }
    } else if (diffItem.kind === "set_number_format") {
      if (!deepJsonEqual(expectedItem.authored, beforeItem.authored)) {
        fail("UNSAFE_MANIFEST", "format edit가 authored content를 변경합니다.");
      }
    } else if (
      expectedItem.authored.kind !== "blank" ||
      expectedItem.number_format !== beforeItem.number_format
    ) {
      fail("UNSAFE_MANIFEST", "clear_contents manifest semantics가 안전하지 않습니다.");
    }
  }
}

function assertCalculatedValue(
  authored: AuthoredState,
  type: CalculatedType,
  value: Scalar | null,
): void {
  const typeMatches =
    (type === "empty" && value === null) ||
    ((type === "string" || type === "error") && typeof value === "string") ||
    (type === "number" && typeof value === "number") ||
    (type === "boolean" && typeof value === "boolean");
  if (!typeMatches || (type === "error" && !(value as string).startsWith("#"))) {
    fail("INVALID_RESPONSE", "calculated type/value가 일치하지 않습니다.");
  }
  if (authored.kind === "blank" && (type !== "empty" || value !== null)) {
    fail("INVALID_RESPONSE", "blank cell 계산값이 유효하지 않습니다.");
  }
  if (authored.kind === "value") {
    const expectedType =
      typeof authored.value === "boolean"
        ? "boolean"
        : typeof authored.value === "number"
          ? "number"
          : "string";
    if (type !== expectedType || !deepJsonEqual(authored.value, value)) {
      fail("INVALID_RESPONSE", "literal authored/calculated 값이 일치하지 않습니다.");
    }
  }
}

function assertVerificationStatus(
  status: VerificationSummary["status"],
  applicationStatus: VerificationSummary["application_status"],
  newSnapshotRequired: boolean,
): void {
  const expected = {
    session_verified: ["applied_session_verified", true],
    stale_precondition: ["not_applied", false],
    verification_failed: ["application_failed", true],
    indeterminate: ["indeterminate", true],
  }[status] as [VerificationSummary["application_status"], boolean];
  if (applicationStatus !== expected[0] || newSnapshotRequired !== expected[1]) {
    fail("INVALID_VERIFICATION", "verification trust status 조합이 유효하지 않습니다.");
  }
}

function isSafeTarget(value: TargetConstraints): boolean {
  return (
    value.merged === false &&
    value.spill === "none" &&
    value.protected === false &&
    value.table_member === false
  );
}

function authoredLabel(authored: AuthoredState, format: string): string {
  if (authored.kind === "blank") {
    return `(빈 셀, 서식 ${format})`;
  }
  if (authored.kind === "formula") {
    return `${authored.formula} (서식 ${format})`;
  }
  return `${JSON.stringify(authored.value)} (서식 ${format})`;
}

function workflowState(value: unknown): WorkflowState {
  const values: WorkflowState[] = [
    "proposed",
    "previewed",
    "approved",
    "rejected",
    "claimed",
    "apply_started",
    "session_verified",
    "verification_failed",
    "indeterminate",
    "stale_precondition",
    "aborted_before_apply",
  ];
  if (!values.includes(value as WorkflowState)) {
    return fail("INVALID_RESPONSE", "workflow state가 유효하지 않습니다.");
  }
  return value as WorkflowState;
}

function editKind(value: unknown): EditKind {
  if (
    value !== "set_value" &&
    value !== "set_formula" &&
    value !== "set_number_format" &&
    value !== "clear_contents"
  ) {
    return fail("INVALID_RESPONSE", "edit kind가 유효하지 않습니다.");
  }
  return value;
}

function calculatedTypeValue(value: unknown): CalculatedType {
  if (
    value !== "empty" &&
    value !== "string" &&
    value !== "number" &&
    value !== "boolean" &&
    value !== "error"
  ) {
    return fail("INVALID_RESPONSE", "calculated type이 유효하지 않습니다.");
  }
  return value;
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return fail("INVALID_RESPONSE", `${field}가 객체가 아닙니다.`);
  }
  return value as Record<string, unknown>;
}

function nullableRecord(value: unknown, field: string): Record<string, unknown> | null {
  return value === null ? null : record(value, field);
}

function array(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) {
    return fail("INVALID_RESPONSE", `${field}가 배열이 아닙니다.`);
  }
  return value;
}

function exactKeys(value: Record<string, unknown>, keys: string[], field: string): void {
  const found = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (found.length !== expected.length || found.some((key, index) => key !== expected[index])) {
    fail("INVALID_RESPONSE", `${field} 필드가 폐쇄형 계약과 다릅니다.`);
  }
}

function opaque(value: unknown, field: string): string {
  if (typeof value !== "string" || !OPAQUE_ID.test(value)) {
    return fail("INVALID_RESPONSE", `${field} 식별자가 유효하지 않습니다.`);
  }
  return value;
}

function sha256(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) {
    return fail("INVALID_RESPONSE", `${field} digest가 유효하지 않습니다.`);
  }
  return value;
}

function prefixedSha(value: unknown, prefix: string, field: string): string {
  if (
    typeof value !== "string" ||
    !value.startsWith(prefix) ||
    !SHA256.test(value.slice(prefix.length))
  ) {
    return fail("INVALID_RESPONSE", `${field} ref가 유효하지 않습니다.`);
  }
  return value;
}

function manifestRef(value: unknown, field: string): string {
  return prefixedSha(value, "edit-manifest:", field);
}

function safePositiveInteger(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    return fail("INVALID_RESPONSE", `${field} 값이 유효하지 않습니다.`);
  }
  return value as number;
}

function sheet(value: unknown, field: string): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 31 ||
    value.trim() !== value ||
    /[\[\]:*?/\\]/.test(value)
  ) {
    return fail("INVALID_RESPONSE", `${field} sheet가 유효하지 않습니다.`);
  }
  return value;
}

function cell(value: unknown, field: string): string {
  if (typeof value !== "string" || !CELL.test(value)) {
    return fail("INVALID_RESPONSE", `${field} cell 주소가 유효하지 않습니다.`);
  }
  const match = /^([A-Z]+)([0-9]+)$/.exec(value)!;
  let column = 0;
  for (const character of match[1]!) {
    column = column * 26 + character.charCodeAt(0) - 64;
  }
  if (column > MAX_EXCEL_COLUMNS || Number(match[2]) > MAX_EXCEL_ROWS) {
    return fail("INVALID_RESPONSE", `${field} cell 주소가 Excel grid 밖입니다.`);
  }
  return value;
}

function formula(value: unknown, field: string): string {
  if (
    typeof value !== "string" ||
    value.length < 2 ||
    value.length > 8192 ||
    !value.startsWith("=")
  ) {
    return fail("INVALID_RESPONSE", `${field} formula가 유효하지 않습니다.`);
  }
  return value;
}

function numberFormat(value: unknown, field: string): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 500 ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return fail("INVALID_RESPONSE", `${field} number format이 유효하지 않습니다.`);
  }
  return value;
}

function scalar(value: unknown, field: string): Scalar {
  if (typeof value === "string") {
    if (value.length > 32767) {
      return fail("INVALID_RESPONSE", `${field} 문자열이 너무 깁니다.`);
    }
    return value;
  }
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= MAX_SAFE_NUMBER) {
    return value;
  }
  return fail("INVALID_RESPONSE", `${field} scalar가 유효하지 않습니다.`);
}

function nullableScalar(value: unknown, field: string): Scalar | null {
  return value === null ? null : scalar(value, field);
}

function isoTimestamp(value: unknown, field: string): string {
  if (
    typeof value !== "string" ||
    value.length < 20 ||
    value.length > 64 ||
    !value.endsWith("Z") ||
    Number.isNaN(Date.parse(value))
  ) {
    return fail("INVALID_RESPONSE", `${field} timestamp가 유효하지 않습니다.`);
  }
  return value;
}

function stringList(
  value: unknown,
  field: string,
  minimum: number,
  maximum: number,
): string[] {
  const values = array(value, field);
  if (values.length < minimum || values.length > maximum) {
    return fail("INVALID_RESPONSE", `${field} 항목 수가 유효하지 않습니다.`);
  }
  return values.map((item) => {
    if (typeof item !== "string" || item.length < 1 || item.length > 300) {
      return fail("INVALID_RESPONSE", `${field} 문자열이 유효하지 않습니다.`);
    }
    return item;
  });
}

function uniqueCells(cells: string[], field: string): void {
  if (new Set(cells).size !== cells.length) {
    fail("INVALID_RESPONSE", `${field}에 중복 cell이 있습니다.`);
  }
}

function literal<T extends string | boolean>(value: unknown, expected: T, field: string): T {
  if (value !== expected) {
    return fail("INVALID_RESPONSE", `${field} 값이 유효하지 않습니다.`);
  }
  return expected;
}

function fail(code: string, message: string): never {
  throw new WorkbookEditContractError(code, message);
}
