import { readFile } from "node:fs/promises";

import { describe, expect, it } from "vitest";

import {
  decodeWorkflowResponse,
  verificationFromSubmission,
  type SubmissionResponse,
} from "../src/executor/contracts";
import { clone, submission, WORKFLOW_RESPONSE } from "./fixtures";

describe("Python backend contract fixture", () => {
  it("accepts the exact session_verified application status emitted by Python", async () => {
    const raw = await readFile(
      new URL("./fixtures/backend-verification.json", import.meta.url),
      "utf8",
    );
    const verification = JSON.parse(raw) as Record<string, unknown>;
    const response: SubmissionResponse = submission("session_verified", { verification });

    expect(verificationFromSubmission(response)).toMatchObject({
      status: "session_verified",
      application_status: "applied_session_verified",
      asset_persisted: false,
      new_snapshot_required: true,
    });
  });

  it("rejects a canonical-looking cell address outside the Excel grid", () => {
    const response = clone(WORKFLOW_RESPONSE);
    response.workflow.artifacts.proposal.changes[0]!.cell = "XFE1";

    expect(() => decodeWorkflowResponse(response)).toThrow(/Excel grid/);
  });
});
