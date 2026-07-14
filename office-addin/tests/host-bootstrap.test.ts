import { describe, expect, it, vi } from "vitest";

import {
  assertBootstrapForWorkflow,
  decodeHostBootstrap,
  fetchHostBootstrap,
  resolveHostBootstrap,
  type HostBootstrapDocument,
} from "../src/host-bootstrap";
import { clone, WORKFLOW } from "./fixtures";

const HOST_SESSION_ID = "host-session-a";
const NOW = Date.parse("2026-07-14T00:00:00Z");

function bootstrap(): HostBootstrapDocument {
  return {
    schema_version: "audit_workbook_edit_host_bootstrap.v1",
    host_session_id: HOST_SESSION_ID,
    workflow_id: WORKFLOW.workflow_id,
    session_id: WORKFLOW.session_id,
    bundle_id: WORKFLOW.bundle_id,
    snapshot_id: WORKFLOW.snapshot_id,
    workbook_sha256: WORKFLOW.workbook_sha256,
    revision_id: WORKFLOW.revision_id,
    sheet: WORKFLOW.sheet,
    worksheet_id: WORKFLOW.worksheet_id,
    binding_sha256: "f".repeat(64),
    persistence_policy: "required",
    expires_at: "2026-07-14T00:05:00Z",
  };
}

describe("host bootstrap configuration", () => {
  it("allows manual connection only in an explicit development build", () => {
    expect(resolveHostBootstrap(undefined, true, "https://localhost:3000")).toEqual({
      mode: "development",
      apiBaseUrl: "https://localhost:3000",
    });
  });

  it("fails closed without a production host session", () => {
    expect(resolveHostBootstrap(undefined, false, "https://addin.example").mode).toBe("invalid");
  });

  it.each([
    {},
    { hostSessionId: "" },
    { hostSessionId: "host-session-a", workflowId: "workflow-a" },
    { apiBaseUrl: "https://audit.example", workflowId: "workflow-a" },
  ])("rejects a missing, invalid, or non-exact host object", (config) => {
    expect(
      resolveHostBootstrap(config as never, true, "https://localhost:3000").mode,
    ).toBe("invalid");
  });

  it("fixes production API access to the current origin and one opaque host session", () => {
    expect(
      resolveHostBootstrap(
        { hostSessionId: "host-session-a" },
        false,
        "https://addin.example",
      ),
    ).toEqual({
      mode: "host",
      apiBaseUrl: "https://addin.example",
      hostSessionId: HOST_SESSION_ID,
    });
  });

  it("rejects a production origin containing a path or using HTTP", () => {
    expect(
      resolveHostBootstrap(
        { hostSessionId: HOST_SESSION_ID },
        false,
        "https://addin.example/path",
      ).mode,
    ).toBe("invalid");
    expect(
      resolveHostBootstrap(
        { hostSessionId: HOST_SESSION_ID },
        false,
        "http://addin.example",
      ).mode,
    ).toBe("invalid");
  });
});

describe("authenticated host bootstrap fetch", () => {
  it("fetches one strict binding from the same origin with the exact host-session header", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(jsonResponse(bootstrap()));

    const result = await fetchHostBootstrap(HOST_SESSION_ID, "https://addin.example", {
      fetch,
      now: () => NOW,
    });

    expect(result.workflow_id).toBe(WORKFLOW.workflow_id);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe(
      "https://addin.example/v1/audit/workbook-edit-host-sessions/host-session-a/bootstrap",
    );
    expect(init).toMatchObject({
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    const headers = new Headers(init?.headers);
    expect(headers.get("X-Audit-Workbook-Host-Session")).toBe(HOST_SESSION_ID);
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("rejects extra fields, a misrouted host session, and expired bindings", () => {
    expect(() =>
      decodeHostBootstrap({ ...bootstrap(), unexpected: true }, HOST_SESSION_ID, NOW),
    ).toThrowError(expect.objectContaining({ code: "HOST_BOOTSTRAP_INVALID" }));
    expect(() =>
      decodeHostBootstrap(
        { ...bootstrap(), host_session_id: "host-session-b" },
        HOST_SESSION_ID,
        NOW,
      ),
    ).toThrowError(expect.objectContaining({ code: "HOST_SESSION_MISMATCH" }));
    expect(() =>
      decodeHostBootstrap(
        { ...bootstrap(), expires_at: "2026-07-13T23:59:59Z" },
        HOST_SESSION_ID,
        NOW,
      ),
    ).toThrowError(expect.objectContaining({ code: "HOST_BOOTSTRAP_EXPIRED" }));
  });

  it("rejects an oversized, non-JSON, or non-success bootstrap response", async () => {
    const oversized = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response("{}", {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(65 * 1024),
        },
      }),
    );
    await expect(
      fetchHostBootstrap(HOST_SESSION_ID, "https://addin.example", {
        fetch: oversized,
        now: () => NOW,
      }),
    ).rejects.toMatchObject({ code: "HOST_BOOTSTRAP_LIMIT_EXCEEDED" });

    const rejected = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      jsonResponse({ error: "unauthorized" }, 401),
    );
    await expect(
      fetchHostBootstrap(HOST_SESSION_ID, "https://addin.example", {
        fetch: rejected,
        now: () => NOW,
      }),
    ).rejects.toMatchObject({ code: "HOST_BOOTSTRAP_REJECTED", status: 401 });
  });
});

describe("host bootstrap workflow binding", () => {
  it("accepts only the exact authenticated workflow identity", () => {
    expect(() => assertBootstrapForWorkflow(bootstrap(), WORKFLOW, NOW)).not.toThrow();

    const mismatched = clone(WORKFLOW);
    mismatched.revision_id = "revision-b";
    expect(() => assertBootstrapForWorkflow(bootstrap(), mismatched, NOW)).toThrowError(
      expect.objectContaining({ code: "HOST_WORKFLOW_BINDING_MISMATCH" }),
    );
  });

  it("rechecks expiry when the workflow is consumed", () => {
    expect(() =>
      assertBootstrapForWorkflow(
        bootstrap(),
        WORKFLOW,
        Date.parse("2026-07-14T00:05:00Z"),
      ),
    ).toThrowError(expect.objectContaining({ code: "HOST_BOOTSTRAP_EXPIRED" }));
  });
});

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
