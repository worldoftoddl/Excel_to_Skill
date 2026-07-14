import { describe, expect, it, vi } from "vitest";

import {
  WorkbookEditApiClient,
  WorkbookEditApiError,
} from "../src/executor/api-client";
import {
  assertManifestForWorkflow,
  claimDetails,
  decodeSubmissionResponse,
  deepJsonEqual,
  WorkbookEditContractError,
} from "../src/executor/contracts";
import {
  CLAIM_SUBMISSION,
  clone,
  MANIFEST,
  WORKFLOW,
  WORKFLOW_RESPONSE,
} from "./fixtures";

describe("WorkbookEditApiClient", () => {
  it("uses the strict workflow endpoint without a mutation key", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    fetch.mockResolvedValue(jsonResponse(WORKFLOW_RESPONSE));
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    const result = await client.getWorkflow("workflow-a");

    expect(result.workflow.workflow_id).toBe("workflow-a");
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("https://audit.example/v1/audit/workbook-edit-workflows/workflow-a");
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).has("Idempotency-Key")).toBe(false);
  });

  it("binds every authenticated GET and POST to the exact same-origin host session", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(jsonResponse(WORKFLOW_RESPONSE))
      .mockResolvedValueOnce(jsonResponse(CLAIM_SUBMISSION));
    const client = new WorkbookEditApiClient("https://addin.example/api", {
      fetch,
      hostSessionId: "host-session-a",
      currentOrigin: "https://addin.example",
    });

    await client.getWorkflow("workflow-a");
    await client.claimExecution("workflow-a", "office-session-a");

    expect(fetch).toHaveBeenCalledTimes(2);
    for (const call of fetch.mock.calls) {
      const init = call[1]!;
      expect(init.credentials).toBe("same-origin");
      expect(init.cache).toBe("no-store");
      expect(init.redirect).toBe("error");
      expect(new Headers(init.headers).get("X-Audit-Workbook-Host-Session")).toBe(
        "host-session-a",
      );
    }
  });

  it("rejects cross-origin or weakened credential configuration for a host session", () => {
    expect(
      () =>
        new WorkbookEditApiClient("https://api.example", {
          hostSessionId: "host-session-a",
          currentOrigin: "https://addin.example",
        }),
    ).toThrow(TypeError);
    expect(
      () =>
        new WorkbookEditApiClient("https://addin.example", {
          hostSessionId: "host-session-a",
          currentOrigin: "https://addin.example",
          credentials: "include",
        }),
    ).toThrow(TypeError);
    expect(
      () =>
        new WorkbookEditApiClient("https://addin.example", {
          hostSessionId: "not valid",
          currentOrigin: "https://addin.example",
        }),
    ).toThrow(TypeError);
  });

  it("requires an explicit or browser current origin for an authenticated client", () => {
    expect(
      () =>
        new WorkbookEditApiClient("https://addin.example", {
          hostSessionId: "host-session-a",
        }),
    ).toThrow(TypeError);
  });

  it("rejects a workflow response routed from a different requested ID", async () => {
    const misrouted = clone(WORKFLOW_RESPONSE);
    misrouted.workflow.workflow_id = "workflow-b";
    const fetch = vi.fn<typeof globalThis.fetch>();
    fetch.mockResolvedValue(jsonResponse(misrouted));
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    await expect(client.getWorkflow("workflow-a")).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  it("retries a network-ambiguous claim with the exact same idempotency key and body", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(jsonResponse(CLAIM_SUBMISSION));
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    const result = await client.claimExecution("workflow-a", "office-session-a");

    expect(result.receipt.state).toBe("claimed");
    expect(fetch).toHaveBeenCalledTimes(2);
    const first = fetch.mock.calls[0]![1]!;
    const second = fetch.mock.calls[1]![1]!;
    expect(new Headers(first.headers).get("Idempotency-Key")).toBe(
      new Headers(second.headers).get("Idempotency-Key"),
    );
    expect(first.body).toBe(second.body);
  });

  it("does not expose one claim capability to a different client action scope", async () => {
    const firstFetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      jsonResponse(CLAIM_SUBMISSION),
    );
    const secondFetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      jsonResponse(CLAIM_SUBMISSION),
    );
    const first = new WorkbookEditApiClient("https://audit.example", {
      fetch: firstFetch,
      mutationAttemptId: "1".repeat(32),
    });
    const second = new WorkbookEditApiClient("https://audit.example", {
      fetch: secondFetch,
      mutationAttemptId: "2".repeat(32),
    });

    await first.claimExecution("workflow-a", "office-session-a");
    await second.claimExecution("workflow-a", "office-session-a");

    expect(new Headers(firstFetch.mock.calls[0]![1]?.headers).get("Idempotency-Key")).not.toBe(
      new Headers(secondFetch.mock.calls[0]![1]?.headers).get("Idempotency-Key"),
    );
  });

  it("does not call the same mutation method twice from one client action", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      jsonResponse(CLAIM_SUBMISSION),
    );
    const client = new WorkbookEditApiClient("https://audit.example", {
      fetch,
      mutationAttemptId: "1".repeat(32),
    });

    await client.claimExecution("workflow-a", "office-session-a");
    await expect(
      client.claimExecution("workflow-a", "office-session-a"),
    ).rejects.toMatchObject({ code: "MUTATION_REPLAY_FORBIDDEN" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("branches on the stable server error code", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    fetch.mockResolvedValue(
      jsonResponse(
        {
          schema_version: "audit_workbook_edit_http_error.v1",
          error: { code: "APPROVAL_EXPIRED", message: "expired" },
        },
        409,
      ),
    );
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    await expect(client.claimExecution("workflow-a", "office-session-a")).rejects.toMatchObject({
      code: "APPROVAL_EXPIRED",
      status: 409,
    });
  });

  it("rejects oversized or non-JSON responses", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    fetch.mockResolvedValue(
      new Response("x", {
        status: 200,
        headers: { "Content-Length": String(4 * 1024 * 1024) },
      }),
    );
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    await expect(client.getWorkflow("workflow-a")).rejects.toBeInstanceOf(WorkbookEditApiError);
  });

  it("stops an oversized streamed response without trusting Content-Length", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    fetch.mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(2 * 1024 * 1024));
            controller.enqueue(new Uint8Array(2 * 1024 * 1024));
            controller.close();
          },
        }),
        { status: 200 },
      ),
    );
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });

    await expect(client.getWorkflow("workflow-a")).rejects.toMatchObject({
      code: "RESPONSE_LIMIT_EXCEEDED",
    });
  });

  it("rejects a request above the backend 1MB HTTP limit before fetch", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const client = new WorkbookEditApiClient("https://audit.example", { fetch });
    const longValue = "가".repeat(400_000);

    await expect(
      client.createPreview("workflow-a", "revision-a", "worksheet-a", [
        {
          cell: "A1",
          authored: { kind: "value", value: longValue },
          calculated_value: longValue,
          calculated_type: "string",
          number_format: "General",
          target_constraints: {
            merged: false,
            spill: "none",
            protected: false,
            table_member: false,
          },
        },
      ]),
    ).rejects.toMatchObject({ code: "REQUEST_LIMIT_EXCEEDED" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not allow credentials in the API base URL", () => {
    expect(() => new WorkbookEditApiClient("https://token@audit.example")).toThrow(TypeError);
  });
});

describe("closed claim contract", () => {
  it("accepts a claim whose capability exactly matches its immutable manifest", () => {
    const decoded = decodeSubmissionResponse(CLAIM_SUBMISSION);
    expect(claimDetails(decoded).apply_manifest.manifest_ref).toBe(MANIFEST.manifest_ref);
  });

  it("rejects a challenge that differs from the manifest", () => {
    const tampered = clone(CLAIM_SUBMISSION);
    tampered.receipt.details.challenge = "different-challenge";
    expect(() => claimDetails(decodeSubmissionResponse(tampered))).toThrow(
      WorkbookEditContractError,
    );
  });

  it("rejects a self-consistent manifest that differs from the human-approved preview", () => {
    const tampered = clone(MANIFEST);
    tampered.expected_after[0]!.authored = { kind: "value", value: "다른 값" };
    tampered.diff[0]!.after = clone(tampered.expected_after[0]!);
    expect(() => assertManifestForWorkflow(tampered, WORKFLOW)).toThrow(
      WorkbookEditContractError,
    );
  });

  it("rejects a manifest whose scope ID differs from the approved preview", () => {
    const tampered = clone(MANIFEST);
    tampered.binding.scope.id = "f".repeat(64);
    expect(() => assertManifestForWorkflow(tampered, WORKFLOW)).toThrow(
      WorkbookEditContractError,
    );
  });

  it("compares typed JSON structures instead of recomputing cross-language digests", () => {
    expect(deepJsonEqual({ value: 0.000001 }, { value: 0.000001 })).toBe(true);
    expect(deepJsonEqual({ value: 1 }, { value: true })).toBe(false);
  });
});

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
