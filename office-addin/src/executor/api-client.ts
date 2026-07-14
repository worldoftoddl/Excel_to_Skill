import {
  decodeSubmissionResponse,
  decodeWorkflowResponse,
  type CellState,
  type SubmissionResponse,
  type WitnessInput,
  type WorkflowResponse,
} from "./contracts";

const MAX_RESPONSE_BYTES = 3 * 1024 * 1024;
const MAX_REQUEST_BYTES = 1024 * 1024;
const IDEMPOTENCY_PREFIX = "awe1";
const HOST_SESSION_HEADER = "X-Audit-Workbook-Host-Session";
const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export interface WorkbookEditApi {
  getWorkflow(workflowId: string): Promise<WorkflowResponse>;
  createPreview(
    workflowId: string,
    revisionId: string,
    worksheetId: string,
    before: CellState[],
  ): Promise<SubmissionResponse>;
  approvePreview(
    workflowId: string,
    previewRef: string,
    previewSha256: string,
  ): Promise<SubmissionResponse>;
  claimExecution(workflowId: string, sessionId: string): Promise<SubmissionResponse>;
  markApplyStarted(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
  ): Promise<SubmissionResponse>;
  verifyExecution(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
    witness: WitnessInput,
  ): Promise<SubmissionResponse>;
  abortExecution(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
  ): Promise<SubmissionResponse>;
}

export interface WorkbookEditApiClientOptions {
  fetch?: typeof fetch;
  credentials?: RequestCredentials;
  maxNetworkAttempts?: number;
  /** Server-issued host session. When set, every request is bound to the current same origin. */
  hostSessionId?: string;
  /** Explicit browser origin for non-window hosts and deterministic tests. */
  currentOrigin?: string;
  /** Unique to one user action; reuse only for in-method network retries. */
  mutationAttemptId?: string;
}

export class WorkbookEditApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "WorkbookEditApiError";
    this.code = code;
    this.status = status;
  }
}

export class WorkbookEditApiClient implements WorkbookEditApi {
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;
  readonly #credentials: RequestCredentials;
  readonly #maxNetworkAttempts: number;
  readonly #mutationAttemptId: string;
  readonly #hostSessionId: string | null;
  readonly #issuedMutationKeys = new Set<string>();

  constructor(baseUrl: string, options: WorkbookEditApiClientOptions = {}) {
    const parsed = parseBaseUrl(baseUrl);
    const hostSessionId = options.hostSessionId;
    if (hostSessionId === undefined) {
      this.#hostSessionId = null;
    } else {
      if (!OPAQUE_ID.test(hostSessionId)) {
        throw new TypeError("hostSessionId must be an exact opaque identifier");
      }
      const currentOrigin = parseCurrentOrigin(
        options.currentOrigin ?? globalThis.location?.origin,
      );
      if (parsed.origin !== currentOrigin) {
        throw new TypeError("authenticated workbook edit API must use the current origin");
      }
      if (options.credentials !== undefined && options.credentials !== "same-origin") {
        throw new TypeError("authenticated workbook edit API requires same-origin credentials");
      }
      this.#hostSessionId = hostSessionId;
    }
    this.#baseUrl = parsed.toString().replace(/\/$/, "");
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.#credentials = options.credentials ?? "same-origin";
    this.#maxNetworkAttempts = options.maxNetworkAttempts ?? 2;
    this.#mutationAttemptId = options.mutationAttemptId ?? randomMutationAttemptId();
    if (
      !Number.isInteger(this.#maxNetworkAttempts) ||
      this.#maxNetworkAttempts < 1 ||
      this.#maxNetworkAttempts > 3
    ) {
      throw new TypeError("maxNetworkAttempts must be an integer from 1 to 3");
    }
    if (!/^[0-9a-f]{32}$/.test(this.#mutationAttemptId)) {
      throw new TypeError("mutationAttemptId must be 128-bit lowercase hex");
    }
  }

  async getWorkflow(workflowId: string): Promise<WorkflowResponse> {
    const value = await this.#request("GET", workflowPath(workflowId));
    const result = decodeWorkflowResponse(value);
    if (result.workflow.workflow_id !== workflowId) {
      throw new WorkbookEditApiError(
        "INVALID_RESPONSE",
        200,
        "요청한 workflow와 API 응답 workflow가 일치하지 않습니다.",
      );
    }
    return result;
  }

  async createPreview(
    workflowId: string,
    revisionId: string,
    worksheetId: string,
    before: CellState[],
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/previews`,
      idempotencyKey("preview", workflowId, this.#mutationAttemptId),
      {
        office_revision_id: revisionId,
        worksheet_id: worksheetId,
        before,
      },
    );
  }

  async approvePreview(
    workflowId: string,
    previewRef: string,
    previewSha256: string,
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/previews/${segment(previewRef)}/approve`,
      idempotencyKey("approve", workflowId, this.#mutationAttemptId),
      { preview_sha256: previewSha256, confirmed: true },
    );
  }

  async claimExecution(
    workflowId: string,
    sessionId: string,
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/executions/claim`,
      idempotencyKey("claim", workflowId, this.#mutationAttemptId),
      { session_id: sessionId },
    );
  }

  async markApplyStarted(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/executions/${segment(executionId)}/started`,
      idempotencyKey("start", executionId, this.#mutationAttemptId),
      { fence, challenge },
    );
  }

  async verifyExecution(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
    witness: WitnessInput,
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/executions/${segment(executionId)}/verify`,
      idempotencyKey(`verify-${witness.outcome}`, executionId, this.#mutationAttemptId),
      { fence, challenge, witness },
    );
  }

  async abortExecution(
    workflowId: string,
    executionId: string,
    fence: number,
    challenge: string,
  ): Promise<SubmissionResponse> {
    return this.#mutation(
      `${workflowPath(workflowId)}/executions/${segment(executionId)}/abort`,
      idempotencyKey("abort", executionId, this.#mutationAttemptId),
      { fence, challenge },
    );
  }

  async #mutation(
    path: string,
    key: string,
    body: Record<string, unknown>,
  ): Promise<SubmissionResponse> {
    if (this.#issuedMutationKeys.has(key)) {
      throw new WorkbookEditApiError(
        "MUTATION_REPLAY_FORBIDDEN",
        0,
        "같은 client action에서 mutation method를 두 번 실행할 수 없습니다.",
      );
    }
    this.#issuedMutationKeys.add(key);
    const value = await this.#request("POST", path, body, key);
    return decodeSubmissionResponse(value);
  }

  async #request(
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>,
    idempotency?: string,
  ): Promise<unknown> {
    const serializedBody = body === undefined ? undefined : JSON.stringify(body);
    if (
      serializedBody !== undefined &&
      new TextEncoder().encode(serializedBody).byteLength > MAX_REQUEST_BYTES
    ) {
      throw new WorkbookEditApiError(
        "REQUEST_LIMIT_EXCEEDED",
        0,
        "workbook edit API 요청이 1MB 상한을 초과했습니다.",
      );
    }
    let lastNetworkError: unknown;
    for (let attempt = 1; attempt <= this.#maxNetworkAttempts; attempt += 1) {
      try {
        const response = await this.#fetch(this.#baseUrl + path, {
          method,
          credentials: this.#credentials,
          cache: "no-store",
          headers: {
            Accept: "application/json",
            ...(this.#hostSessionId === null
              ? {}
              : { [HOST_SESSION_HEADER]: this.#hostSessionId }),
            ...(body === undefined ? {} : { "Content-Type": "application/json" }),
            ...(idempotency === undefined ? {} : { "Idempotency-Key": idempotency }),
          },
          redirect: "error",
          ...(serializedBody === undefined ? {} : { body: serializedBody }),
        });
        const value = await readBoundedJson(response);
        if (!response.ok) {
          throw apiError(response.status, value);
        }
        return value;
      } catch (error) {
        if (error instanceof WorkbookEditApiError) {
          throw error;
        }
        lastNetworkError = error;
      }
    }
    throw new WorkbookEditApiError(
      "NETWORK_UNAVAILABLE",
      0,
      "workbook edit API 응답을 확인할 수 없습니다.",
      { cause: lastNetworkError },
    );
  }
}

async function readBoundedJson(response: Response): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (
    declared !== null &&
    (!/^\d+$/.test(declared) || Number(declared) > MAX_RESPONSE_BYTES)
  ) {
    throw new WorkbookEditApiError("RESPONSE_LIMIT_EXCEEDED", response.status, "응답이 너무 큽니다.");
  }
  let text: string;
  if (response.body === null) {
    text = await response.text();
  } else {
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      total += part.value.byteLength;
      if (total > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new WorkbookEditApiError(
          "RESPONSE_LIMIT_EXCEEDED",
          response.status,
          "응답이 너무 큽니다.",
        );
      }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      throw new WorkbookEditApiError("INVALID_RESPONSE", response.status, "UTF-8 응답이 아닙니다.");
    }
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new WorkbookEditApiError("INVALID_RESPONSE", response.status, "JSON 응답이 아닙니다.");
  }
}

function apiError(status: number, value: unknown): WorkbookEditApiError {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const error = (value as Record<string, unknown>).error;
    if (typeof error === "object" && error !== null && !Array.isArray(error)) {
      const code = (error as Record<string, unknown>).code;
      const message = (error as Record<string, unknown>).message;
      if (typeof code === "string" && typeof message === "string") {
        return new WorkbookEditApiError(code, status, message);
      }
    }
  }
  return new WorkbookEditApiError("INVALID_RESPONSE", status, "API 오류 응답이 유효하지 않습니다.");
}

function workflowPath(workflowId: string): string {
  return `/v1/audit/workbook-edit-workflows/${segment(workflowId)}`;
}

function segment(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) {
    throw new TypeError("identifier is not an opaque workbook-edit ID");
  }
  return encodeURIComponent(value);
}

function idempotencyKey(stage: string, identifier: string, attemptId: string): string {
  const key = `${IDEMPOTENCY_PREFIX}.${stage}.${attemptId}.${identifier}`;
  if (key.length > 200 || /\s/.test(key)) {
    throw new TypeError("derived idempotency key is invalid");
  }
  return key;
}

function randomMutationAttemptId(): string {
  const crypto = globalThis.crypto;
  if (crypto === undefined) {
    throw new TypeError("secure crypto is required for workbook edit mutation scope");
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function parseBaseUrl(value: string): URL {
  const parsed = new URL(value, globalThis.location?.origin ?? "https://localhost");
  if (parsed.protocol !== "https:") {
    throw new TypeError("workbook edit API requires HTTPS");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new TypeError("base URL must not contain credentials, query, or fragment");
  }
  return parsed;
}

function parseCurrentOrigin(value: string | undefined): string {
  if (value === undefined) {
    throw new TypeError("currentOrigin is required for an authenticated host session");
  }
  const parsed = new URL(value);
  if (
    parsed.protocol !== "https:" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw new TypeError("currentOrigin must be an exact HTTPS origin");
  }
  return parsed.origin;
}
