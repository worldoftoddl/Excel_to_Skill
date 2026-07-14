import { describe, expect, it } from "vitest";

import { resolveHostBootstrap } from "../src/host-bootstrap";

describe("host bootstrap", () => {
  it("allows manual connection only in an explicit development build", () => {
    expect(resolveHostBootstrap(undefined, true, "https://localhost:3000")).toEqual({
      mode: "development",
      apiBaseUrl: "https://localhost:3000",
      workflowId: "",
      snapshotCallback: null,
    });
  });

  it("fails closed without a production host bootstrap", () => {
    expect(resolveHostBootstrap(undefined, false, "https://addin.example").mode).toBe("invalid");
  });

  it.each([
    [{ apiBaseUrl: "https://audit.example" }],
    [{ workflowId: "workflow-a" }],
    [{ publishVerifiedSnapshot: async () => ({}) }],
  ])("rejects a partial host object even in development", (config) => {
    expect(resolveHostBootstrap(config, true, "https://localhost:3000").mode).toBe("invalid");
  });

  it("pins the exact host API and workflow while leaving snapshot publication optional", () => {
    expect(
      resolveHostBootstrap(
        { apiBaseUrl: " https://audit.example ", workflowId: " workflow-a " },
        false,
        "https://addin.example",
      ),
    ).toEqual({
      mode: "host",
      apiBaseUrl: "https://audit.example",
      workflowId: "workflow-a",
      snapshotCallback: null,
    });
  });
});
