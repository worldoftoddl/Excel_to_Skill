import type { VerificationSummary, WorkflowDocument } from "./contracts";

const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const HOST_SESSION_HEADER = "X-Audit-Workbook-Host-Session";
const MAX_PUBLICATION_RESPONSE_BYTES = 64 * 1024;

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
    return assertPublicationForRequest(request, publication);
  }
}

export interface AuthenticatedSnapshotPublisherOptions {
  fetch?: typeof fetch;
  currentOrigin?: string;
  mutationAttemptId?: string;
  maxNetworkAttempts?: number;
}

/** Same-origin publisher backed by the authoritative server reacquisition and source-head CAS. */
export class AuthenticatedApiSnapshotPublisher implements VerifiedSnapshotPublisher {
  readonly #origin: string;
  readonly #hostSessionId: string;
  readonly #fetch: typeof fetch;
  readonly #attemptId: string;
  readonly #maxNetworkAttempts: number;

  constructor(
    apiBaseUrl: string,
    hostSessionId: string,
    options: AuthenticatedSnapshotPublisherOptions = {},
  ) {
    const currentOrigin = exactHttpsOrigin(
      options.currentOrigin ?? globalThis.location?.origin ?? "",
    );
    const apiOrigin = exactHttpsOrigin(apiBaseUrl);
    if (apiOrigin !== currentOrigin) {
      throw new TypeError("authenticated snapshot publication must use the current origin");
    }
    if (!OPAQUE_ID.test(hostSessionId)) {
      throw new TypeError("hostSessionId must be an opaque identifier");
    }
    const attemptId = options.mutationAttemptId ?? randomAttemptId();
    if (!/^[0-9a-f]{32}$/.test(attemptId)) {
      throw new TypeError("mutationAttemptId must be 128-bit lowercase hex");
    }
    const maxNetworkAttempts = options.maxNetworkAttempts ?? 2;
    if (!Number.isInteger(maxNetworkAttempts) || maxNetworkAttempts < 1 || maxNetworkAttempts > 3) {
      throw new TypeError("maxNetworkAttempts must be an integer from 1 to 3");
    }
    this.#origin = apiOrigin;
    this.#hostSessionId = hostSessionId;
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.#attemptId = attemptId;
    this.#maxNetworkAttempts = maxNetworkAttempts;
  }

  async publishVerifiedSnapshot(
    request: SnapshotPublicationRequest,
  ): Promise<SnapshotPublication> {
    assertPublishableRequest(request);
    const path = publicationPath(request.workflow.workflow_id, request.executionId);
    const body = JSON.stringify({
      manifest_ref: request.manifestRef,
      manifest_sha256: request.manifestSha256,
    });
    const idempotency = publicationIdempotencyKey(request.executionId, this.#attemptId);
    let lastNetworkError: unknown;
    for (let attempt = 1; attempt <= this.#maxNetworkAttempts; attempt += 1) {
      try {
        const response = await this.#fetch(this.#origin + path, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          redirect: "error",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency,
            [HOST_SESSION_HEADER]: this.#hostSessionId,
          },
          body,
        });
        const value = await readBoundedJson(response);
        if (!response.ok) throw publicationHttpError(response.status, value);
        return assertPublicationForRequest(request, decodePublication(value));
      } catch (error) {
        if (
          error instanceof SnapshotPublicationApiError &&
          error.status < 500 &&
          error.code !== "COMMAND_IN_PROGRESS"
        ) {
          throw error;
        }
        lastNetworkError = error;
      }
    }

    // A committed CAS may have lost only its HTTP response. Lookup never reruns the manifest or
    // workbook write and is therefore the safe publication-only recovery path.
    for (let lookupAttempt = 1; lookupAttempt <= 5; lookupAttempt += 1) {
      try {
        return await this.lookupVerifiedSnapshot(request);
      } catch (error) {
        const pending =
          error instanceof SnapshotPublicationApiError &&
          (error.code === "PUBLICATION_NOT_READY" || error.code === "COMMAND_IN_PROGRESS");
        if (pending && lookupAttempt < 5) {
          await new Promise((resolve) => setTimeout(resolve, 200));
          continue;
        }
        if (error instanceof SnapshotPublicationApiError) throw error;
        throw new SnapshotPublicationApiError(
          "PUBLICATION_CONFIRMATION_UNKNOWN",
          0,
          "saved workbook publication 응답을 확인할 수 없습니다.",
          { cause: lastNetworkError ?? error },
        );
      }
    }
    throw new SnapshotPublicationApiError(
      "PUBLICATION_CONFIRMATION_UNKNOWN",
      0,
      "saved workbook publication 응답을 확인할 수 없습니다.",
    );
  }

  async lookupVerifiedSnapshot(
    request: SnapshotPublicationRequest,
  ): Promise<SnapshotPublication> {
    assertPublishableRequest(request);
    const response = await this.#fetch(
      this.#origin + publicationPath(request.workflow.workflow_id, request.executionId),
      {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: {
          Accept: "application/json",
          [HOST_SESSION_HEADER]: this.#hostSessionId,
        },
      },
    );
    const value = await readBoundedJson(response);
    if (!response.ok) throw publicationHttpError(response.status, value);
    return assertPublicationForRequest(request, decodePublication(value));
  }
}

export class SnapshotPublicationApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "SnapshotPublicationApiError";
    this.code = code;
    this.status = status;
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

function assertPublishableRequest(request: SnapshotPublicationRequest): void {
  if (
    request.workbookSaved !== true ||
    request.verification.status !== "session_verified" ||
    request.verification.new_snapshot_required !== true ||
    !/^edit-manifest:[0-9a-f]{64}$/.test(request.manifestRef) ||
    !SHA256.test(request.manifestSha256) ||
    request.manifestRef.slice("edit-manifest:".length) !== request.manifestSha256
  ) {
    throw new TypeError("snapshot publication request is not an exact verified save");
  }
}

function assertPublicationForRequest(
  request: SnapshotPublicationRequest,
  publication: SnapshotPublication,
): SnapshotPublication {
  assertPublishableRequest(request);
  if (
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

async function readBoundedJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/json") {
    throw new TypeError("snapshot publication response must be JSON");
  }
  const declared = response.headers.get("content-length");
  if (
    declared !== null &&
    (!/^\d+$/.test(declared) || Number(declared) > MAX_PUBLICATION_RESPONSE_BYTES)
  ) {
    throw new TypeError("snapshot publication response is too large");
  }
  let bytes: Uint8Array;
  if (response.body === null) {
    bytes = new Uint8Array(await response.arrayBuffer());
  } else {
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      total += part.value.byteLength;
      if (total > MAX_PUBLICATION_RESPONSE_BYTES) {
        await reader.cancel();
        throw new TypeError("snapshot publication response is too large");
      }
      chunks.push(part.value);
    }
    bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
  }
  if (bytes.byteLength > MAX_PUBLICATION_RESPONSE_BYTES) {
    throw new TypeError("snapshot publication response is too large");
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new TypeError("snapshot publication response is not UTF-8");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new TypeError("snapshot publication response is not JSON");
  }
}

function publicationHttpError(status: number, value: unknown): SnapshotPublicationApiError {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const error = (value as Record<string, unknown>).error;
    if (typeof error === "object" && error !== null && !Array.isArray(error)) {
      const code = (error as Record<string, unknown>).code;
      const message = (error as Record<string, unknown>).message;
      if (typeof code === "string" && typeof message === "string") {
        return new SnapshotPublicationApiError(code, status, message);
      }
    }
  }
  return new SnapshotPublicationApiError(
    "INVALID_RESPONSE",
    status,
    "snapshot publication 오류 응답이 유효하지 않습니다.",
  );
}

function publicationPath(workflowId: string, executionId: string): string {
  return `/v1/audit/workbook-edit-workflows/${segment(workflowId)}/executions/${segment(executionId)}/snapshot-publication`;
}

function segment(value: string): string {
  if (!OPAQUE_ID.test(value)) throw new TypeError("publication identifier is invalid");
  return encodeURIComponent(value);
}

function publicationIdempotencyKey(executionId: string, attemptId: string): string {
  const key = `awe1.publish.${attemptId}.${executionId}`;
  if (key.length > 200) throw new TypeError("publication idempotency key is invalid");
  return key;
}

function exactHttpsOrigin(value: string): string {
  const parsed = new URL(value);
  if (
    parsed.protocol !== "https:" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw new TypeError("snapshot publication origin must be an exact HTTPS origin");
  }
  return parsed.origin;
}

function randomAttemptId(): string {
  const crypto = globalThis.crypto;
  if (crypto === undefined) throw new TypeError("secure crypto is required");
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}
