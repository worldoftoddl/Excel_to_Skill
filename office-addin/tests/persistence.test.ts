import { describe, expect, it, vi } from "vitest";

import { verificationFromSubmission } from "../src/executor/contracts";
import {
  AuthenticatedApiSnapshotPublisher,
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

describe("AuthenticatedApiSnapshotPublisher", () => {
  it("posts only exact manifest selectors with same-origin host binding", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(jsonResponse(publication()));
    const publisher = new AuthenticatedApiSnapshotPublisher(
      "https://addin.example",
      "edit-host-11111111111111111111111111111111",
      {
        fetch,
        currentOrigin: "https://addin.example",
        mutationAttemptId: "2".repeat(32),
      },
    );

    const result = await publisher.publishVerifiedSnapshot(request());

    expect(result.snapshot_id).toBe(SHA_C);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toContain(`/executions/${MANIFEST.execution_id}/snapshot-publication`);
    expect(init?.credentials).toBe("same-origin");
    expect(init?.redirect).toBe("error");
    expect(new Headers(init?.headers).get("X-Audit-Workbook-Host-Session")).toBe(
      "edit-host-11111111111111111111111111111111",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      manifest_ref: MANIFEST.manifest_ref,
      manifest_sha256: MANIFEST.manifest_sha256,
    });
  });

  it("recovers a committed publication by GET after both POST responses are lost", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockRejectedValueOnce(new TypeError("lost"))
      .mockRejectedValueOnce(new TypeError("lost again"))
      .mockResolvedValueOnce(jsonResponse(publication()));
    const publisher = new AuthenticatedApiSnapshotPublisher(
      "https://addin.example",
      "edit-host-11111111111111111111111111111111",
      {
        fetch,
        currentOrigin: "https://addin.example",
        mutationAttemptId: "3".repeat(32),
      },
    );

    const result = await publisher.publishVerifiedSnapshot(request());

    expect(result.revision_id).toBe("revision-new");
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[2]![1]?.method).toBe("GET");
    expect(fetch.mock.calls[0]![1]?.body).toBe(fetch.mock.calls[1]![1]?.body);
    expect(
      new Headers(fetch.mock.calls[0]![1]?.headers).get("Idempotency-Key"),
    ).toBe(new Headers(fetch.mock.calls[1]![1]?.headers).get("Idempotency-Key"));
  });

  it("rejects cross-origin publication before fetch", () => {
    expect(
      () =>
        new AuthenticatedApiSnapshotPublisher(
          "https://api.example",
          "edit-host-11111111111111111111111111111111",
          { currentOrigin: "https://addin.example" },
        ),
    ).toThrow(TypeError);
  });
});

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
