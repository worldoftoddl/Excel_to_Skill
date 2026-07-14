import type { VerificationSummary, WorkflowDocument } from "./contracts";

const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;

export interface SnapshotPublicationRequest {
  workflow: WorkflowDocument;
  executionId: string;
  manifestRef: string;
  manifestSha256: string;
  verification: VerificationSummary;
  workbookSaved: true;
}

export interface SnapshotPublication {
  schema_version: "audit_workbook_snapshot_publication.v1";
  bundle_id: string;
  execution_id: string;
  manifest_ref: string;
  manifest_sha256: string;
  base_snapshot_id: string;
  base_revision_id: string;
  snapshot_id: string;
  workbook_sha256: string;
  revision_id: string;
  asset_persisted: true;
  prepared_bundle_created: boolean;
}

export interface VerifiedSnapshotPublisher {
  publishVerifiedSnapshot(request: SnapshotPublicationRequest): Promise<SnapshotPublication>;
}

export type HostSnapshotCallback = (
  request: SnapshotPublicationRequest,
) => Promise<unknown>;

/**
 * Product hosts can inject a callback that reacquires the just-saved cloud workbook, hashes it,
 * stores an immutable asset, and registers the resulting server-owned snapshot. The host must
 * retain a workbook-level publication lease or enforce revision CAS from verification through
 * reacquisition; a callback response alone cannot make save/publication atomic. The Add-in never
 * accepts or manufactures bundle/hash/revision identities itself.
 */
export class HostCallbackSnapshotPublisher implements VerifiedSnapshotPublisher {
  readonly #callback: HostSnapshotCallback;

  constructor(callback: HostSnapshotCallback) {
    if (typeof callback !== "function") {
      throw new TypeError("snapshot publisher callback must be a function");
    }
    this.#callback = callback;
  }

  async publishVerifiedSnapshot(
    request: SnapshotPublicationRequest,
  ): Promise<SnapshotPublication> {
    const value = await this.#callback(request);
    const publication = decodePublication(value);
    if (
      request.verification.status !== "session_verified" ||
      request.verification.new_snapshot_required !== true ||
      publication.bundle_id !== request.workflow.bundle_id ||
      publication.execution_id !== request.executionId ||
      publication.manifest_ref !== request.manifestRef ||
      publication.manifest_sha256 !== request.manifestSha256 ||
      publication.base_snapshot_id !== request.workflow.snapshot_id ||
      publication.base_revision_id !== request.workflow.revision_id ||
      publication.snapshot_id === request.workflow.snapshot_id ||
      publication.workbook_sha256 === request.workflow.workbook_sha256 ||
      publication.revision_id === request.workflow.revision_id
    ) {
      throw new TypeError("snapshot publication is not a new exact revision of this bundle");
    }
    return publication;
  }
}

function decodePublication(value: unknown): SnapshotPublication {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError("snapshot publication response must be an object");
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  const expected = [
    "asset_persisted",
    "bundle_id",
    "execution_id",
    "manifest_ref",
    "manifest_sha256",
    "base_snapshot_id",
    "base_revision_id",
    "prepared_bundle_created",
    "revision_id",
    "schema_version",
    "snapshot_id",
    "workbook_sha256",
  ].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new TypeError("snapshot publication response fields are invalid");
  }
  if (
    record.schema_version !== "audit_workbook_snapshot_publication.v1" ||
    record.asset_persisted !== true ||
    typeof record.prepared_bundle_created !== "boolean" ||
    typeof record.bundle_id !== "string" ||
    !OPAQUE_ID.test(record.bundle_id) ||
    typeof record.execution_id !== "string" ||
    !OPAQUE_ID.test(record.execution_id) ||
    typeof record.manifest_ref !== "string" ||
    !/^edit-manifest:[0-9a-f]{64}$/.test(record.manifest_ref) ||
    typeof record.manifest_sha256 !== "string" ||
    !SHA256.test(record.manifest_sha256) ||
    typeof record.base_snapshot_id !== "string" ||
    !SHA256.test(record.base_snapshot_id) ||
    typeof record.base_revision_id !== "string" ||
    !OPAQUE_ID.test(record.base_revision_id) ||
    typeof record.revision_id !== "string" ||
    !OPAQUE_ID.test(record.revision_id) ||
    typeof record.snapshot_id !== "string" ||
    !SHA256.test(record.snapshot_id) ||
    typeof record.workbook_sha256 !== "string" ||
    !SHA256.test(record.workbook_sha256)
  ) {
    throw new TypeError("snapshot publication response values are invalid");
  }
  return {
    schema_version: "audit_workbook_snapshot_publication.v1",
    bundle_id: record.bundle_id,
    execution_id: record.execution_id,
    manifest_ref: record.manifest_ref,
    manifest_sha256: record.manifest_sha256,
    base_snapshot_id: record.base_snapshot_id,
    base_revision_id: record.base_revision_id,
    snapshot_id: record.snapshot_id,
    workbook_sha256: record.workbook_sha256,
    revision_id: record.revision_id,
    asset_persisted: true,
    prepared_bundle_created: record.prepared_bundle_created,
  };
}
