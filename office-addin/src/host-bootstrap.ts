import type { WorkflowDocument } from "./executor/contracts";

const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_BOOTSTRAP_RESPONSE_BYTES = 64 * 1024;
const HOST_SESSION_HEADER = "X-Audit-Workbook-Host-Session";

export interface WorkbookEditHostConfig {
  hostSessionId: string;
}

export type PersistencePolicy = "required" | "session_only" | "unsupported";

export interface HostBootstrapDocument {
  schema_version: "audit_workbook_edit_host_bootstrap.v1";
  host_session_id: string;
  workflow_id: string;
  session_id: string;
  bundle_id: string;
  snapshot_id: string;
  workbook_sha256: string;
  revision_id: string;
  sheet: string;
  worksheet_id: string;
  binding_sha256: string;
  persistence_policy: PersistencePolicy;
  expires_at: string;
}

export type HostBootstrapResolution =
  | {
      mode: "host";
      apiBaseUrl: string;
      hostSessionId: string;
    }
  | {
      mode: "development";
      apiBaseUrl: string;
    }
  | {
      mode: "invalid";
      reason: string;
    };

export interface FetchHostBootstrapOptions {
  fetch?: typeof fetch;
  now?: () => number;
}

export class HostBootstrapError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status = 0, options?: ErrorOptions) {
    super(message, options);
    this.name = "HostBootstrapError";
    this.code = code;
    this.status = status;
  }
}

/** Production accepts only one opaque host-session handle and fixes API calls to this origin. */
export function resolveHostBootstrap(
  config: WorkbookEditHostConfig | undefined,
  development: boolean,
  currentOrigin: string,
): HostBootstrapResolution {
  if (config === undefined) {
    if (development) {
      return {
        mode: "development",
        apiBaseUrl: currentOrigin,
      };
    }
    return {
      mode: "invalid",
      reason: "배포 Add-in에는 인증된 host session bootstrap이 필요합니다.",
    };
  }
  if (!isExactRecord(config, ["hostSessionId"])) {
    return {
      mode: "invalid",
      reason: "host bootstrap 설정은 hostSessionId 하나만 포함해야 합니다.",
    };
  }
  const hostSessionId = config.hostSessionId.trim();
  if (!OPAQUE_ID.test(hostSessionId)) {
    return {
      mode: "invalid",
      reason: "hostSessionId가 유효하지 않습니다.",
    };
  }
  let apiBaseUrl: string;
  try {
    apiBaseUrl = exactHttpsOrigin(currentOrigin);
  } catch {
    return {
      mode: "invalid",
      reason: "배포 Add-in origin이 안전한 HTTPS origin이 아닙니다.",
    };
  }
  return {
    mode: "host",
    apiBaseUrl,
    hostSessionId,
  };
}

/** Fetch one short-lived, principal-scoped binding from the same authenticated origin. */
export async function fetchHostBootstrap(
  hostSessionId: string,
  currentOrigin: string,
  options: FetchHostBootstrapOptions = {},
): Promise<HostBootstrapDocument> {
  const cleanHostSessionId = opaque(hostSessionId, "host_session_id");
  const origin = exactHttpsOrigin(currentOrigin);
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  let response: Response;
  try {
    response = await fetchImpl(
      `${origin}/v1/audit/workbook-edit-host-sessions/${encodeURIComponent(cleanHostSessionId)}/bootstrap`,
      {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: {
          Accept: "application/json",
          [HOST_SESSION_HEADER]: cleanHostSessionId,
        },
      },
    );
  } catch (error) {
    throw new HostBootstrapError(
      "HOST_BOOTSTRAP_UNAVAILABLE",
      "인증된 host bootstrap 응답을 확인할 수 없습니다.",
      0,
      { cause: error },
    );
  }
  const value = await readBoundedBootstrapJson(response);
  if (!response.ok) {
    throw new HostBootstrapError(
      "HOST_BOOTSTRAP_REJECTED",
      "인증된 host session을 사용할 수 없습니다.",
      response.status,
    );
  }
  return decodeHostBootstrap(
    value,
    cleanHostSessionId,
    (options.now ?? Date.now)(),
  );
}

export function decodeHostBootstrap(
  value: unknown,
  expectedHostSessionId: string,
  now = Date.now(),
): HostBootstrapDocument {
  const expected = opaque(expectedHostSessionId, "expected host_session_id");
  const record = exactRecord(value, [
    "schema_version",
    "host_session_id",
    "workflow_id",
    "session_id",
    "bundle_id",
    "snapshot_id",
    "workbook_sha256",
    "revision_id",
    "sheet",
    "worksheet_id",
    "binding_sha256",
    "persistence_policy",
    "expires_at",
  ]);
  if (record.schema_version !== "audit_workbook_edit_host_bootstrap.v1") {
    return fail("HOST_BOOTSTRAP_INVALID", "host bootstrap schema가 유효하지 않습니다.");
  }
  const hostSessionId = opaque(record.host_session_id, "host_session_id");
  if (hostSessionId !== expected) {
    return fail("HOST_SESSION_MISMATCH", "요청한 host session과 bootstrap이 다릅니다.");
  }
  const expiresAt = timestamp(record.expires_at, "expires_at");
  if (!Number.isFinite(now) || now >= Date.parse(expiresAt)) {
    return fail("HOST_BOOTSTRAP_EXPIRED", "host bootstrap이 만료되었습니다.");
  }
  const policy = record.persistence_policy;
  if (policy !== "required" && policy !== "session_only" && policy !== "unsupported") {
    return fail("HOST_BOOTSTRAP_INVALID", "persistence_policy가 유효하지 않습니다.");
  }
  return {
    schema_version: "audit_workbook_edit_host_bootstrap.v1",
    host_session_id: hostSessionId,
    workflow_id: opaque(record.workflow_id, "workflow_id"),
    session_id: opaque(record.session_id, "session_id"),
    bundle_id: opaque(record.bundle_id, "bundle_id"),
    snapshot_id: sha256(record.snapshot_id, "snapshot_id"),
    workbook_sha256: sha256(record.workbook_sha256, "workbook_sha256"),
    revision_id: opaque(record.revision_id, "revision_id"),
    sheet: exactSheet(record.sheet),
    worksheet_id: opaque(record.worksheet_id, "worksheet_id"),
    binding_sha256: sha256(record.binding_sha256, "binding_sha256"),
    persistence_policy: policy,
    expires_at: expiresAt,
  };
}

/** Recheck the server bootstrap against the exact workflow before any Office read or mutation. */
export function assertBootstrapForWorkflow(
  bootstrap: HostBootstrapDocument,
  workflow: WorkflowDocument,
  now = Date.now(),
): void {
  if (now >= Date.parse(bootstrap.expires_at)) {
    fail("HOST_BOOTSTRAP_EXPIRED", "host bootstrap이 만료되었습니다.");
  }
  if (
    bootstrap.workflow_id !== workflow.workflow_id ||
    bootstrap.session_id !== workflow.session_id ||
    bootstrap.bundle_id !== workflow.bundle_id ||
    bootstrap.snapshot_id !== workflow.snapshot_id ||
    bootstrap.workbook_sha256 !== workflow.workbook_sha256 ||
    bootstrap.revision_id !== workflow.revision_id ||
    bootstrap.sheet !== workflow.sheet ||
    bootstrap.worksheet_id !== workflow.worksheet_id
  ) {
    fail(
      "HOST_WORKFLOW_BINDING_MISMATCH",
      "인증된 host bootstrap과 workbook edit workflow가 일치하지 않습니다.",
    );
  }
}

async function readBoundedBootstrapJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/json") {
    return fail("HOST_BOOTSTRAP_INVALID", "host bootstrap 응답은 JSON이어야 합니다.");
  }
  const declared = response.headers.get("content-length");
  if (
    declared !== null &&
    (!/^\d+$/.test(declared) || Number(declared) > MAX_BOOTSTRAP_RESPONSE_BYTES)
  ) {
    return fail("HOST_BOOTSTRAP_LIMIT_EXCEEDED", "host bootstrap 응답이 너무 큽니다.");
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
      if (total > MAX_BOOTSTRAP_RESPONSE_BYTES) {
        await reader.cancel();
        return fail("HOST_BOOTSTRAP_LIMIT_EXCEEDED", "host bootstrap 응답이 너무 큽니다.");
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
  if (bytes.byteLength > MAX_BOOTSTRAP_RESPONSE_BYTES) {
    return fail("HOST_BOOTSTRAP_LIMIT_EXCEEDED", "host bootstrap 응답이 너무 큽니다.");
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return fail("HOST_BOOTSTRAP_INVALID", "host bootstrap 응답이 UTF-8이 아닙니다.");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return fail("HOST_BOOTSTRAP_INVALID", "host bootstrap 응답이 유효한 JSON이 아닙니다.");
  }
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
    throw new TypeError("current origin must be an exact HTTPS origin");
  }
  return parsed.origin;
}

function exactRecord(value: unknown, keys: string[]): Record<string, unknown> {
  if (!isExactRecord(value, keys)) {
    return fail("HOST_BOOTSTRAP_INVALID", "host bootstrap 응답 필드가 유효하지 않습니다.");
  }
  return value;
}

function isExactRecord(value: unknown, keys: string[]): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function opaque(value: unknown, field: string): string {
  if (typeof value !== "string" || !OPAQUE_ID.test(value)) {
    return fail("HOST_BOOTSTRAP_INVALID", `${field}가 유효하지 않습니다.`);
  }
  return value;
}

function sha256(value: unknown, field: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) {
    return fail("HOST_BOOTSTRAP_INVALID", `${field}가 유효하지 않습니다.`);
  }
  return value;
}

function exactSheet(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 31 ||
    value !== value.trim() ||
    /[\[\]:*?/\\]/.test(value)
  ) {
    return fail("HOST_BOOTSTRAP_INVALID", "sheet가 유효하지 않습니다.");
  }
  return value;
}

function timestamp(value: unknown, field: string): string {
  if (
    typeof value !== "string" ||
    value.length < 20 ||
    value.length > 64 ||
    !value.endsWith("Z") ||
    !Number.isFinite(Date.parse(value))
  ) {
    return fail("HOST_BOOTSTRAP_INVALID", `${field}가 유효하지 않습니다.`);
  }
  return value;
}

function fail(code: string, message: string): never {
  throw new HostBootstrapError(code, message);
}
