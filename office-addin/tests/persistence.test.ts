import { describe, expect, it, vi } from "vitest";

import { verificationFromSubmission } from "../src/executor/contracts";
import {
  HostCallbackSnapshotPublisher,
  type SnapshotPublicationRequest,
} from "../src/executor/persistence";
import {
  MANIFEST,
  SHA_A,
  SHA_C,
  WORKFLOW,
  terminalSubmission,
} from "./fixtures";

function request(): SnapshotPublicationRequest {
  return {
    workflow: structuredClone(WORKFLOW),
    executionId: MANIFEST.execution_id,
    manifestRef: MANIFEST.manifest_ref,
    manifestSha256: MANIFEST.manifest_sha256,
    verification: verificationFromSubmission(terminalSubmission("session_verified")),
    workbookSaved: true,
  };
}

function publication() {
  return {
    schema_version: "audit_workbook_snapshot_publication.v1",
    bundle_id: "bundle-a",
    execution_id: MANIFEST.execution_id,
    manifest_ref: MANIFEST.manifest_ref,
    manifest_sha256: MANIFEST.manifest_sha256,
    base_snapshot_id: WORKFLOW.snapshot_id,
    base_revision_id: WORKFLOW.revision_id,
    snapshot_id: SHA_C,
    workbook_sha256: SHA_A,
    revision_id: "revision-new",
    asset_persisted: true,
    prepared_bundle_created: false,
  };
}

describe("HostCallbackSnapshotPublisher", () => {
  it("accepts only a new persisted revision of the exact bundle", async () => {
    const callback = vi.fn(async () => publication());
    const publisher = new HostCallbackSnapshotPublisher(callback);

    const result = await publisher.publishVerifiedSnapshot(request());

    expect(result.snapshot_id).toBe(SHA_C);
    expect(callback).toHaveBeenCalledOnce();
  });

  it("rejects malformed host callback output", async () => {
    const publisher = new HostCallbackSnapshotPublisher(async () => ({
      ...publication(),
      unexpected: true,
    }));

    await expect(publisher.publishVerifiedSnapshot(request())).rejects.toThrow(TypeError);
  });

  it.each([
    ["same snapshot", { snapshot_id: WORKFLOW.snapshot_id }],
    ["same workbook hash", { workbook_sha256: WORKFLOW.workbook_sha256 }],
    ["same revision", { revision_id: WORKFLOW.revision_id }],
    ["different bundle", { bundle_id: "bundle-b" }],
  ])("rejects %s as a new source snapshot", async (_label, override) => {
    const publisher = new HostCallbackSnapshotPublisher(async () => ({
      ...publication(),
      ...override,
    }));

    await expect(publisher.publishVerifiedSnapshot(request())).rejects.toThrow(TypeError);
  });
});
