"""Strict HTTP boundary for the approved Office.js workbook-edit workflow.

The framework-neutral adapter accepts only bounded JSON documents and opaque identifiers.  It
does not accept workbook paths, providers, JavaScript, credentials, or server-owned workbook
identity fields.  Authentication and Office session registration remain host responsibilities.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Mapping

import jsonschema
from referencing import Registry, Resource

from ..resources import SCHEMA_DIR
from .service import ServicePrincipal
from .workbook_edit_host import (
    WorkbookEditHostServiceError,
    WorkbookEditHostSessionService,
)
from .workbook_edit_service import (
    WorkbookEditService,
    WorkbookEditServiceError,
)


_SCHEMA_FILES = {
    "propose": "audit_workbook_edit_http_propose_request.schema.json",
    "preview": "audit_workbook_edit_http_preview_request.schema.json",
    "approve": "audit_workbook_edit_http_approve_request.schema.json",
    "reject": "audit_workbook_edit_http_reject_request.schema.json",
    "claim": "audit_workbook_edit_http_claim_request.schema.json",
    "execution": "audit_workbook_edit_http_execution_request.schema.json",
    "verify": "audit_workbook_edit_http_verify_request.schema.json",
    "publication": "audit_workbook_snapshot_publication_request.schema.json",
}
_RESPONSE_SCHEMA_FILES = {
    "error": "audit_workbook_edit_http_error_response.schema.json",
    "receipt": "audit_workbook_edit_receipt.schema.json",
    "submission": "audit_workbook_edit_http_submission_response.schema.json",
    "workflow": "audit_workbook_edit_http_workflow_response.schema.json",
    "bootstrap": "audit_workbook_edit_host_bootstrap.schema.json",
    "publication": "audit_workbook_snapshot_publication.schema.json",
}
_REFERENCED_SCHEMA_FILES = {
    "audit_workbook_edit_proposal.schema.json",
    "audit_workbook_edit_preview.schema.json",
    "audit_workbook_edit_approval.schema.json",
    "audit_workbook_edit_apply_manifest.schema.json",
    "audit_workbook_edit_verification.schema.json",
}
_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
}
_SERVICE_ERROR_STATUS = {
    "MISSING_IDEMPOTENCY_KEY": 400,
    "INVALID_REQUEST": 400,
    "INVALID_IDEMPOTENCY_KEY": 400,
    "SESSION_NOT_FOUND": 404,
    "WORKFLOW_NOT_FOUND": 404,
    "PRINCIPAL_MISMATCH": 403,
    "SESSION_MISMATCH": 409,
    "BUNDLE_MISMATCH": 409,
    "WORKBOOK_MISMATCH": 409,
    "STALE_REVISION": 409,
    "WORKSHEET_MISMATCH": 409,
    "INVALID_STATE": 409,
    "EDIT_CONFLICT": 409,
    "ACTIVE_EXECUTION_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "COMMAND_IN_PROGRESS": 409,
    "APPROVAL_CONFIRMATION_REQUIRED": 400,
    "PREVIEW_MISMATCH": 409,
    "APPROVAL_REPLAY": 409,
    "APPROVAL_EXPIRED": 409,
    "EXECUTION_CLAIM_MISMATCH": 409,
    "EXECUTION_LEASE_EXPIRED": 409,
    "RETRY_FORBIDDEN": 409,
    "FORMULA_INJECTION_BLOCKED": 400,
    "UNSAFE_FORMULA": 400,
    "DUPLICATE_CELL": 400,
    "UNSAFE_TARGET": 409,
    "NO_OP_EDIT": 409,
    "LIMIT_EXCEEDED": 413,
    "PUBLICATION_UNAVAILABLE": 503,
    "PUBLICATION_NOT_READY": 409,
    "PUBLICATION_NOT_FOUND": 404,
    "PUBLICATION_BASIS_MISMATCH": 409,
    "PUBLICATION_CONFLICT": 409,
    "EDIT_CONTRACT_MISMATCH": 500,
    "SERVICE_UNAVAILABLE": 503,
}
_HOST_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "HOST_SESSION_NOT_FOUND": 404,
    "HOST_BINDING_NOT_FOUND": 404,
    "PRINCIPAL_MISMATCH": 403,
    "WORKFLOW_MISMATCH": 409,
    "SESSION_MISMATCH": 409,
    "BINDING_MISMATCH": 409,
    "HOST_SESSION_EXPIRED": 410,
    "HOST_SESSION_REVOKED": 410,
    "SERVICE_UNAVAILABLE": 503,
}
# Core artifacts are capped at 600 KB; this bound leaves room for the HTTP wrapper and JSON
# escaping without allowing an unbounded request to reach the worker pool.
_MAX_HTTP_BODY_BYTES = 1024 * 1024
# A workflow GET deliberately combines several independently bounded review artifacts.  Keep the
# response contract larger than the 600 KB artifact cap while still preventing an implementation
# or repository defect from producing an unbounded public response.
_MAX_HTTP_RESPONSE_BYTES = 3 * 1024 * 1024
# Keep both parsing and framework-neutral direct-object validation below the Python/JSON Schema
# recursion boundary.  Valid workbook-edit documents are much shallower than this; deeper input
# is therefore an invalid transport document rather than useful contract data.
_MAX_JSON_NESTING_DEPTH = 32
_MAX_JSON_TRAVERSAL_NODES = 250_000
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HOST_SESSION_HEADER = "X-Audit-Workbook-Host-Session"


def _load_schemas(
    files: Mapping[str, str],
    *,
    failure_message: str,
) -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    try:
        for name, filename in files.items():
            value = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError(filename)
            validator_type = jsonschema.validators.validator_for(value)
            validator_type.check_schema(value)
            schemas[name] = value
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, jsonschema.SchemaError) as exc:
        raise RuntimeError(failure_message) from exc
    return schemas


REQUEST_SCHEMAS = _load_schemas(
    _SCHEMA_FILES,
    failure_message="audit workbook edit HTTP request schemas are unavailable",
)
PROPOSE_REQUEST_SCHEMA = REQUEST_SCHEMAS["propose"]
PREVIEW_REQUEST_SCHEMA = REQUEST_SCHEMAS["preview"]
APPROVE_REQUEST_SCHEMA = REQUEST_SCHEMAS["approve"]
REJECT_REQUEST_SCHEMA = REQUEST_SCHEMAS["reject"]
CLAIM_REQUEST_SCHEMA = REQUEST_SCHEMAS["claim"]
EXECUTION_REQUEST_SCHEMA = REQUEST_SCHEMAS["execution"]
VERIFY_REQUEST_SCHEMA = REQUEST_SCHEMAS["verify"]
PUBLICATION_REQUEST_SCHEMA = REQUEST_SCHEMAS["publication"]
_VALIDATORS = {
    name: jsonschema.Draft202012Validator(schema)
    for name, schema in REQUEST_SCHEMAS.items()
}

RESPONSE_SCHEMAS = _load_schemas(
    _RESPONSE_SCHEMA_FILES,
    failure_message="audit workbook edit HTTP response schemas are unavailable",
)
ERROR_RESPONSE_SCHEMA = RESPONSE_SCHEMAS["error"]
RECEIPT_SCHEMA = RESPONSE_SCHEMAS["receipt"]
SUBMISSION_RESPONSE_SCHEMA = RESPONSE_SCHEMAS["submission"]
WORKFLOW_RESPONSE_SCHEMA = RESPONSE_SCHEMAS["workflow"]
HOST_BOOTSTRAP_RESPONSE_SCHEMA = RESPONSE_SCHEMAS["bootstrap"]
PUBLICATION_RESPONSE_SCHEMA = RESPONSE_SCHEMAS["publication"]

_REFERENCE_SCHEMAS = _load_schemas(
    {filename: filename for filename in sorted(_REFERENCED_SCHEMA_FILES)},
    failure_message="audit workbook edit artifact schemas are unavailable",
)
_RESPONSE_REGISTRY = Registry()
for _schema_name, _schema_value in {**_REFERENCE_SCHEMAS, **{
    filename: RESPONSE_SCHEMAS[name]
    for name, filename in _RESPONSE_SCHEMA_FILES.items()
}}.items():
    _RESPONSE_REGISTRY = _RESPONSE_REGISTRY.with_resource(
        _schema_name,
        Resource.from_contents(_schema_value),
    )
_RESPONSE_VALIDATORS = {
    name: jsonschema.Draft202012Validator(schema, registry=_RESPONSE_REGISTRY)
    for name, schema in RESPONSE_SCHEMAS.items()
}

_OPENAPI_COMPONENT_NAMES = {
    "audit_workbook_edit_http_propose_request.schema.json": "AuditWorkbookEditProposeRequest",
    "audit_workbook_edit_http_preview_request.schema.json": "AuditWorkbookEditPreviewRequest",
    "audit_workbook_edit_http_approve_request.schema.json": "AuditWorkbookEditApproveRequest",
    "audit_workbook_edit_http_reject_request.schema.json": "AuditWorkbookEditRejectRequest",
    "audit_workbook_edit_http_claim_request.schema.json": "AuditWorkbookEditClaimRequest",
    "audit_workbook_edit_http_execution_request.schema.json": "AuditWorkbookEditExecutionRequest",
    "audit_workbook_edit_http_verify_request.schema.json": "AuditWorkbookEditVerifyRequest",
    "audit_workbook_snapshot_publication_request.schema.json": "AuditWorkbookSnapshotPublicationRequest",
    "audit_workbook_edit_http_error_response.schema.json": "AuditWorkbookEditErrorResponse",
    "audit_workbook_edit_receipt.schema.json": "AuditWorkbookEditReceipt",
    "audit_workbook_edit_http_submission_response.schema.json": "AuditWorkbookEditSubmissionResponse",
    "audit_workbook_edit_http_workflow_response.schema.json": "AuditWorkbookEditWorkflowResponse",
    "audit_workbook_edit_host_bootstrap.schema.json": "AuditWorkbookEditHostBootstrap",
    "audit_workbook_snapshot_publication.schema.json": "AuditWorkbookSnapshotPublication",
    "audit_workbook_edit_proposal.schema.json": "AuditWorkbookEditProposal",
    "audit_workbook_edit_preview.schema.json": "AuditWorkbookEditPreview",
    "audit_workbook_edit_approval.schema.json": "AuditWorkbookEditApproval",
    "audit_workbook_edit_apply_manifest.schema.json": "AuditWorkbookEditApplyManifest",
    "audit_workbook_edit_verification.schema.json": "AuditWorkbookEditVerification",
}
_OPENAPI_SCHEMA_DOCUMENTS = {
    **{
        filename: REQUEST_SCHEMAS[name]
        for name, filename in _SCHEMA_FILES.items()
    },
    **{
        filename: RESPONSE_SCHEMAS[name]
        for name, filename in _RESPONSE_SCHEMA_FILES.items()
    },
    **_REFERENCE_SCHEMAS,
}


def _openapi_component_schema(filename: str) -> dict[str, object]:
    component_name = _OPENAPI_COMPONENT_NAMES[filename]

    def rewrite(value: object) -> object:
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str):
                    if item.startswith("#/"):
                        result[key] = (
                            f"#/components/schemas/{component_name}" + item[1:]
                        )
                    elif item in _OPENAPI_COMPONENT_NAMES:
                        result[key] = (
                            "#/components/schemas/" + _OPENAPI_COMPONENT_NAMES[item]
                        )
                    else:
                        raise RuntimeError(
                            "audit workbook edit OpenAPI schema has an unresolved reference"
                        )
                else:
                    result[key] = rewrite(item)
            return result
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return copy.deepcopy(value)

    rewritten = rewrite(_OPENAPI_SCHEMA_DOCUMENTS[filename])
    if not isinstance(rewritten, dict):
        raise RuntimeError("audit workbook edit OpenAPI schema is invalid")
    return rewritten


_OPENAPI_COMPONENTS = {
    component_name: _openapi_component_schema(filename)
    for filename, component_name in _OPENAPI_COMPONENT_NAMES.items()
}


def _openapi_ref(filename: str) -> dict[str, str]:
    return {"$ref": "#/components/schemas/" + _OPENAPI_COMPONENT_NAMES[filename]}


class WorkbookEditWebAdapterUnavailableError(RuntimeError):
    """The optional web framework is not installed or could not be initialized."""


class _WorkbookEditResponseContractError(RuntimeError):
    """A service result did not match the closed public response contract."""


class _WorkbookEditResponseLimitError(RuntimeError):
    """A valid public response exceeded its explicit serialized byte bound."""


@dataclass(frozen=True)
class WorkbookEditHttpResponse:
    """Small HTTP-shaped value usable without FastAPI."""

    status_code: int
    body: dict[str, object]
    headers: Mapping[str, str]


def _validated_response_body(name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict) or _contains_non_finite(value):
        raise _WorkbookEditResponseContractError("invalid workbook edit response")
    try:
        _RESPONSE_VALIDATORS[name].validate(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (
        jsonschema.ValidationError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise _WorkbookEditResponseContractError(
            "invalid workbook edit response"
        ) from exc
    if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
        raise _WorkbookEditResponseLimitError("workbook edit response is too large")
    return copy.deepcopy(value)


def _fixed_error(
    code: str,
    message: str,
    *,
    status_code: int,
) -> WorkbookEditHttpResponse:
    body = _validated_response_body(
        "error",
        {
            "schema_version": "audit_workbook_edit_http_error.v1",
            "error": {"code": code, "message": message},
        },
    )
    return WorkbookEditHttpResponse(
        status_code=status_code,
        body=body,
        headers=dict(_JSON_HEADERS),
    )


def _error_response(error: WorkbookEditServiceError) -> WorkbookEditHttpResponse:
    status_code = _SERVICE_ERROR_STATUS.get(error.code)
    if status_code is None or error.status_code != status_code:
        return _internal_error()
    return _fixed_error(
        error.code,
        "The workbook edit request could not be completed.",
        status_code=status_code,
    )


def _host_error_response(error: WorkbookEditHostServiceError) -> WorkbookEditHttpResponse:
    status_code = _HOST_ERROR_STATUS.get(error.code)
    if status_code is None or error.status_code != status_code:
        return _internal_error()
    return _fixed_error(
        error.code,
        "The authenticated workbook host session could not be used.",
        status_code=status_code,
    )


def _invalid_request() -> WorkbookEditHttpResponse:
    return _fixed_error(
        "INVALID_REQUEST",
        "The JSON request does not match the workbook edit contract.",
        status_code=400,
    )


def _internal_error() -> WorkbookEditHttpResponse:
    return _fixed_error(
        "INTERNAL_ERROR",
        "The workbook edit service could not complete the request.",
        status_code=500,
    )


def _response_limit_error() -> WorkbookEditHttpResponse:
    return _fixed_error(
        "LIMIT_EXCEEDED",
        "The workbook edit response exceeds the supported limit.",
        status_code=413,
    )


def _idempotency_header(
    headers: Mapping[str, object] | list[tuple[object, object]] | tuple[tuple[object, object], ...],
) -> str:
    if isinstance(headers, Mapping):
        items = list(headers.items())
    elif isinstance(headers, (list, tuple)) and all(
        isinstance(item, tuple) and len(item) == 2 for item in headers
    ):
        items = list(headers)
    else:
        items = []
    values = [
        value
        for name, value in items
        if isinstance(name, str) and name.lower() == "idempotency-key"
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise WorkbookEditServiceError(
            "MISSING_IDEMPOTENCY_KEY",
            "Exactly one Idempotency-Key header is required.",
            status_code=400,
        )
    return values[0]


def _host_session_header(
    headers: Mapping[str, object] | list[tuple[object, object]] | tuple[tuple[object, object], ...],
) -> str:
    if isinstance(headers, Mapping):
        items = list(headers.items())
    elif isinstance(headers, (list, tuple)) and all(
        isinstance(item, tuple) and len(item) == 2 for item in headers
    ):
        items = list(headers)
    else:
        items = []
    values = [
        value
        for name, value in items
        if isinstance(name, str) and name.lower() == _HOST_SESSION_HEADER.lower()
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise WorkbookEditHostServiceError(
            "INVALID_REQUEST",
            "Exactly one workbook host-session header is required.",
            status_code=400,
        )
    return values[0]


def _contains_non_finite(value: object) -> bool:
    """Return true for non-finite or structurally unsafe JSON-like values.

    This intentionally uses an iterative, bounded walk.  The adapter also accepts direct Python
    objects in framework-neutral use, so it cannot rely solely on the raw HTTP nesting scan to
    protect ``json.dumps`` and JSON Schema validation from recursion exhaustion.  Repeated cyclic
    containers naturally exceed the depth bound and fail closed.
    """

    stack: list[tuple[object, int]] = [(value, 0)]
    visited_nodes = 0
    while stack:
        item, parent_depth = stack.pop()
        visited_nodes += 1
        if visited_nodes > _MAX_JSON_TRAVERSAL_NODES:
            return True
        if isinstance(item, float) and not math.isfinite(item):
            return True
        if isinstance(item, dict):
            depth = parent_depth + 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                return True
            for key, nested in item.items():
                stack.append((key, depth))
                stack.append((nested, depth))
        elif isinstance(item, (list, tuple)):
            depth = parent_depth + 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                return True
            for nested in item:
                stack.append((nested, depth))
    return False


def _raw_json_within_nesting_limit(raw: bytes) -> bool:
    """Check container nesting without mistaking delimiters inside JSON strings for structure."""

    containers: list[int] = []
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ or {
            if len(containers) >= _MAX_JSON_NESTING_DEPTH:
                return False
            containers.append(byte)
        elif byte == 0x5D:  # ]
            if not containers or containers.pop() != 0x5B:
                return False
        elif byte == 0x7D:  # }
            if not containers or containers.pop() != 0x7B:
                return False
    return not in_string and not containers


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object member")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise ValueError("non-finite JSON number")


def _document(name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict) or _contains_non_finite(value):
        raise ValueError("invalid request")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid request") from exc
    if len(encoded) > _MAX_HTTP_BODY_BYTES:
        raise ValueError("invalid request")
    try:
        _VALIDATORS[name].validate(value)
    except (jsonschema.ValidationError, RecursionError) as exc:
        raise ValueError("invalid request") from exc
    return copy.deepcopy(value)


def _receipt_document(submission: object) -> tuple[dict[str, object], bool]:
    receipt_value = getattr(submission, "receipt", None)
    replayed = getattr(submission, "replayed", None)
    if receipt_value is None or not isinstance(replayed, bool):
        raise TypeError("invalid workbook edit submission")
    to_dict = getattr(receipt_value, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("invalid workbook edit receipt")
    receipt = to_dict()
    receipt = _validated_response_body("receipt", receipt)
    workflow_id = receipt.get("workflow_id")
    if not isinstance(workflow_id, str) or _OPAQUE_ID_RE.fullmatch(workflow_id) is None:
        raise TypeError("invalid workbook edit workflow")
    return receipt, replayed


def _public_document(value: object) -> dict[str, object]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise TypeError("invalid workbook edit workflow")
    # The response validator performs the bounded structural walk before making its defensive
    # copy.  Copying an untrusted deeply nested service value here would reintroduce recursion.
    return value


def _submission_response(
    submission: object,
    *,
    created_status: int,
) -> WorkbookEditHttpResponse:
    receipt, replayed = _receipt_document(submission)
    workflow_id = receipt["workflow_id"]
    body = _validated_response_body(
        "submission",
        {
            "schema_version": "audit_workbook_edit_http_submission.v1",
            "replayed": replayed,
            "receipt": receipt,
        },
    )
    return WorkbookEditHttpResponse(
        status_code=200 if replayed else created_status,
        body=body,
        headers={
            **_JSON_HEADERS,
            "Location": f"/v1/audit/workbook-edit-workflows/{workflow_id}",
            "Idempotency-Replayed": "true" if replayed else "false",
        },
    )


class AuditWorkbookEditHttpAdapter:
    """Framework-neutral strict adapter for one workbook-edit service."""

    def __init__(
        self,
        service: WorkbookEditService,
        *,
        host_sessions: WorkbookEditHostSessionService | None = None,
    ) -> None:
        if not isinstance(service, WorkbookEditService):
            raise TypeError("service must be a WorkbookEditService")
        if host_sessions is not None and not isinstance(
            host_sessions, WorkbookEditHostSessionService
        ):
            raise TypeError("host_sessions must be a WorkbookEditHostSessionService")
        self._service = service
        self._host_sessions = host_sessions

    def _authorize_host(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        headers,
        session_id: str | None = None,
        stored_read: bool = False,
    ) -> dict[str, object] | None:
        if self._host_sessions is None:
            return None
        host_session_id = _host_session_header(headers)
        resolver = (
            self._host_sessions.resolve_stored
            if stored_read
            else self._host_sessions.resolve
        )
        document = resolver(
            principal=principal,
            host_session_id=host_session_id,
        )
        if document.get("workflow_id") != workflow_id:
            raise WorkbookEditHostServiceError(
                "WORKFLOW_MISMATCH",
                "The request does not match the host workflow.",
                status_code=409,
            )
        if session_id is not None and document.get("session_id") != session_id:
            raise WorkbookEditHostServiceError(
                "SESSION_MISMATCH",
                "The request does not match the Office session.",
                status_code=409,
            )
        return document

    def _authorize_published_host(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        headers,
        execution_id: str | None = None,
    ) -> dict[str, object]:
        """Authorize immutable replay/recovery after the raw source head has advanced."""

        if self._host_sessions is None:
            raise WorkbookEditHostServiceError(
                "HOST_SESSION_NOT_FOUND",
                "The authenticated workbook host session could not be used.",
                status_code=404,
            )
        host_session_id = _host_session_header(headers)
        document = self._host_sessions.resolve_stored(
            principal=principal,
            host_session_id=host_session_id,
        )
        if document.get("workflow_id") != workflow_id:
            raise WorkbookEditHostServiceError(
                "WORKFLOW_MISMATCH",
                "The request does not match the host workflow.",
                status_code=409,
            )
        try:
            workflow = self._service.get_workflow(
                principal=principal,
                workflow_id=workflow_id,
            )
            stored_execution_id = workflow.get("execution_id")
            if not isinstance(stored_execution_id, str):
                raise WorkbookEditServiceError(
                    "PUBLICATION_NOT_FOUND",
                    "The workbook snapshot publication was not found.",
                    status_code=404,
                )
            if execution_id is not None and stored_execution_id != execution_id:
                raise WorkbookEditServiceError(
                    "PUBLICATION_NOT_FOUND",
                    "The workbook snapshot publication was not found.",
                    status_code=404,
                )
            self._service.get_snapshot_publication(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=stored_execution_id,
            )
        except WorkbookEditServiceError:
            raise WorkbookEditHostServiceError(
                "BINDING_MISMATCH",
                "The live workbook binding no longer matches the host session.",
                status_code=409,
            ) from None
        return document

    def _mutate(self, call, *, created_status: int) -> WorkbookEditHttpResponse:
        try:
            return _submission_response(call(), created_status=created_status)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except _WorkbookEditResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()

    def propose(self, *, principal: ServicePrincipal, headers, json_body: object) -> WorkbookEditHttpResponse:
        try:
            body = _document("propose", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.propose(
                principal=principal,
                session_id=body["session_id"],
                proposal_input={"changes": body["changes"]},
                idempotency_key=idempotency_key,
            ),
            created_status=201,
        )

    def preview(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            )
            body = _document("preview", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.preview(
                principal=principal,
                workflow_id=workflow_id,
                preview_input=body,
                idempotency_key=idempotency_key,
            ),
            created_status=201,
        )

    def approve(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        preview_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            )
            body = _document("approve", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.approve(
                principal=principal,
                workflow_id=workflow_id,
                preview_id=preview_id,
                preview_sha256=body["preview_sha256"],
                confirmed=body["confirmed"],
                idempotency_key=idempotency_key,
            ),
            created_status=200,
        )

    def reject(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        preview_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            )
            _document("reject", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.reject(
                principal=principal,
                workflow_id=workflow_id,
                preview_id=preview_id,
                idempotency_key=idempotency_key,
            ),
            created_status=200,
        )

    def claim_execution(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            body = _document("claim", json_body)
            host = self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
                session_id=body["session_id"],
            )
            require_publication = (
                None if host is None else host.get("persistence_policy") == "required"
            )
            if require_publication is True and not self._service.snapshot_publication_enabled:
                raise WorkbookEditHostServiceError(
                    "SERVICE_UNAVAILABLE",
                    "Required workbook publication is not configured.",
                    status_code=503,
                )
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.claim_execution(
                principal=principal,
                workflow_id=workflow_id,
                session_id=body["session_id"],
                idempotency_key=idempotency_key,
                publication_required=require_publication,
            ),
            created_status=201,
        )

    def mark_apply_started(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        return self._execution_mutation(
            method="mark_apply_started",
            principal=principal,
            workflow_id=workflow_id,
            execution_id=execution_id,
            headers=headers,
            json_body=json_body,
        )

    def abort_claim(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        return self._execution_mutation(
            method="abort_claim",
            principal=principal,
            workflow_id=workflow_id,
            execution_id=execution_id,
            headers=headers,
            json_body=json_body,
        )

    def _execution_mutation(
        self,
        *,
        method: str,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            )
            body = _document("execution", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        service_method = getattr(self._service, method)
        return self._mutate(
            lambda: service_method(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                fence=body["fence"],
                challenge=body["challenge"],
                idempotency_key=idempotency_key,
            ),
            created_status=200,
        )

    def verify_execution(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            )
            body = _document("verify", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        return self._mutate(
            lambda: self._service.verify_execution(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                fence=body["fence"],
                challenge=body["challenge"],
                witness=body["witness"],
                idempotency_key=idempotency_key,
            ),
            created_status=200,
        )

    def get_workflow(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        headers=(),
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
                stored_read=True,
            )
            workflow = _public_document(
                self._service.get_workflow(
                    principal=principal,
                    workflow_id=workflow_id,
                )
            )
            body = _validated_response_body(
                "workflow",
                {
                    "schema_version": "audit_workbook_edit_http_workflow.v1",
                    "workflow": workflow,
                },
            )
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except _WorkbookEditResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookEditHttpResponse(
            status_code=200,
            body=body,
            headers=dict(_JSON_HEADERS),
        )

    def get_host_bootstrap(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
        headers,
    ) -> WorkbookEditHttpResponse:
        if self._host_sessions is None:
            return _fixed_error(
                "HOST_SESSION_NOT_FOUND",
                "The authenticated workbook host session could not be used.",
                status_code=404,
            )
        try:
            if _host_session_header(headers) != host_session_id:
                raise WorkbookEditHostServiceError(
                    "INVALID_REQUEST",
                    "The host-session header does not match the route.",
                    status_code=400,
                )
            try:
                document = self._host_sessions.resolve(
                    principal=principal,
                    host_session_id=host_session_id,
                )
            except WorkbookEditHostServiceError as error:
                if error.code not in {"BINDING_MISMATCH", "HOST_BINDING_NOT_FOUND"}:
                    raise
                document = self._authorize_published_host(
                    principal=principal,
                    workflow_id=str(
                        self._host_sessions.resolve_stored(
                            principal=principal,
                            host_session_id=host_session_id,
                        )["workflow_id"]
                    ),
                    headers=headers,
                )
            if (
                document.get("persistence_policy") == "required"
                and not self._service.snapshot_publication_enabled
            ):
                raise WorkbookEditHostServiceError(
                    "SERVICE_UNAVAILABLE",
                    "Required workbook publication is not configured.",
                    status_code=503,
                )
            body = _validated_response_body("bootstrap", document)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except _WorkbookEditResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookEditHttpResponse(
            status_code=200,
            body=body,
            headers=dict(_JSON_HEADERS),
        )

    def publish_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers,
        json_body: object,
    ) -> WorkbookEditHttpResponse:
        try:
            try:
                host = self._authorize_host(
                    principal=principal,
                    workflow_id=workflow_id,
                    headers=headers,
                )
            except WorkbookEditHostServiceError as error:
                if error.code not in {"BINDING_MISMATCH", "HOST_BINDING_NOT_FOUND"}:
                    raise
                host = self._authorize_published_host(
                    principal=principal,
                    workflow_id=workflow_id,
                    headers=headers,
                    execution_id=execution_id,
                )
            if host is not None and host.get("persistence_policy") != "required":
                raise WorkbookEditHostServiceError(
                    "WORKFLOW_MISMATCH",
                    "The host session does not authorize durable publication.",
                    status_code=409,
                )
            body = _document("publication", json_body)
            idempotency_key = _idempotency_header(headers)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except ValueError:
            return _invalid_request()
        try:
            submission = self._service.publish_verified_snapshot(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                manifest_ref=body["manifest_ref"],
                manifest_sha256=body["manifest_sha256"],
                idempotency_key=idempotency_key,
            )
            publication = submission.receipt.details.get("publication")
            body_out = _validated_response_body("publication", publication)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except _WorkbookEditResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookEditHttpResponse(
            status_code=200,
            body=body_out,
            headers=dict(_JSON_HEADERS),
        )

    def get_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        headers=(),
    ) -> WorkbookEditHttpResponse:
        try:
            self._authorize_host(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
                stored_read=True,
            )
            publication = self._service.get_snapshot_publication(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
            )
            body = _validated_response_body("publication", publication)
        except WorkbookEditHostServiceError as error:
            return _host_error_response(error)
        except WorkbookEditServiceError as error:
            return _error_response(error)
        except _WorkbookEditResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookEditHttpResponse(
            status_code=200,
            body=body,
            headers=dict(_JSON_HEADERS),
        )


def create_workbook_edit_fastapi_app(
    adapter: AuditWorkbookEditHttpAdapter,
    *,
    principal_dependency,
    executor_workers: int = 4,
):
    """Create the optional FastAPI application without importing it at module import time."""

    if not isinstance(adapter, AuditWorkbookEditHttpAdapter):
        raise TypeError("adapter must be an AuditWorkbookEditHttpAdapter")
    if not callable(principal_dependency):
        raise TypeError("principal_dependency must be callable")
    if (
        not isinstance(executor_workers, int)
        or isinstance(executor_workers, bool)
        or not 1 <= executor_workers <= 32
    ):
        raise ValueError("executor_workers must be an integer from 1 to 32")
    try:
        parameters = tuple(inspect.signature(principal_dependency).parameters.values())
    except (TypeError, ValueError) as exc:
        raise TypeError("principal_dependency must have an inspectable signature") from exc
    if len(parameters) > 1 or any(
        parameter.kind
        not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ):
        raise TypeError("principal_dependency must accept zero arguments or one request")
    principal_accepts_request = len(parameters) == 1

    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise WorkbookEditWebAdapterUnavailableError(
            "FastAPI is optional; install it in the hosting web application."
        ) from exc

    @asynccontextmanager
    async def lifespan(app):
        executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="audit-edit-web",
        )
        app.state.audit_edit_executor = executor
        app.state.audit_edit_executor_gate = asyncio.Semaphore(executor_workers * 2)
        try:
            yield
        finally:
            app.state.audit_edit_executor = None
            app.state.audit_edit_executor_gate = None
            executor.shutdown(wait=True, cancel_futures=True)

    async def offload(request, call):
        executor = getattr(request.app.state, "audit_edit_executor", None)
        gate = getattr(request.app.state, "audit_edit_executor_gate", None)
        if not isinstance(executor, ThreadPoolExecutor) or not isinstance(
            gate, asyncio.Semaphore
        ):
            raise RuntimeError("audit edit executor is outside its application lifespan")
        await gate.acquire()
        loop = asyncio.get_running_loop()
        try:
            future = executor.submit(call)
        except BaseException:
            gate.release()
            raise

        def release_gate(_future):
            try:
                loop.call_soon_threadsafe(gate.release)
            except RuntimeError:
                pass

        future.add_done_callback(release_gate)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(wrapped), timeout=0.05)
                except TimeoutError:
                    if future.done():
                        return future.result()
        except asyncio.CancelledError:
            # Cancelling a client must not release a slot while an already-running mutation can
            # still consume its one-time claim or publish a receipt.
            future.cancel()
            raise

    async def resolve_principal(request):
        def call_dependency():
            if principal_accepts_request:
                return principal_dependency(request)
            return principal_dependency()

        if inspect.iscoroutinefunction(principal_dependency):
            principal = call_dependency()
        else:
            principal = await offload(request, call_dependency)
        if inspect.isawaitable(principal):
            principal = await principal
        return principal

    async def request_document(request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None
        content_lengths = [
            value
            for name, value in request.scope.get("headers", ())
            if isinstance(name, bytes)
            and isinstance(value, bytes)
            and name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            return None
        declared_length = None
        if content_lengths:
            raw_length = content_lengths[0]
            if not raw_length or not raw_length.isdigit():
                return None
            declared_length = int(raw_length)
            if declared_length < 1 or declared_length > _MAX_HTTP_BODY_BYTES:
                return None
        buffered = bytearray()
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                return None
            if not chunk:
                continue
            if len(buffered) + len(chunk) > _MAX_HTTP_BODY_BYTES:
                return None
            buffered.extend(chunk)
        raw = bytes(buffered)
        if not raw or (declared_length is not None and len(raw) != declared_length):
            return None
        if not _raw_json_within_nesting_limit(raw):
            return None
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return None
        return value

    def header_items(request):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.scope.get("headers", ())
            if isinstance(name, bytes) and isinstance(value, bytes)
        ]

    def response_value(response):
        return JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=dict(response.headers),
        )

    async def mutation(request, call):
        # Authenticate before reading and validating up to 1 MB of attacker-controlled JSON.
        # The hosting application remains responsible for its concrete cookie/token boundary.
        principal = await resolve_principal(request)
        body = await request_document(request)
        if body is None:
            return response_value(_invalid_request())
        headers = header_items(request)
        response = await offload(request, lambda: call(principal, headers, body))
        return response_value(response)

    async def propose_endpoint(request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.propose(
                principal=principal, headers=headers, json_body=body
            ),
        )

    async def preview_endpoint(workflow_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.preview(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def approve_endpoint(workflow_id, preview_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.approve(
                principal=principal,
                workflow_id=workflow_id,
                preview_id=preview_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def reject_endpoint(workflow_id, preview_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.reject(
                principal=principal,
                workflow_id=workflow_id,
                preview_id=preview_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def claim_endpoint(workflow_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.claim_execution(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def started_endpoint(workflow_id, execution_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.mark_apply_started(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def verify_endpoint(workflow_id, execution_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.verify_execution(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def abort_endpoint(workflow_id, execution_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.abort_claim(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def get_endpoint(workflow_id, request):
        principal = await resolve_principal(request)
        headers = header_items(request)
        response = await offload(
            request,
            lambda: adapter.get_workflow(
                principal=principal,
                workflow_id=workflow_id,
                headers=headers,
            ),
        )
        return response_value(response)

    async def host_bootstrap_endpoint(host_session_id, request):
        principal = await resolve_principal(request)
        headers = header_items(request)
        response = await offload(
            request,
            lambda: adapter.get_host_bootstrap(
                principal=principal,
                host_session_id=host_session_id,
                headers=headers,
            ),
        )
        return response_value(response)

    async def publication_endpoint(workflow_id, execution_id, request):
        return await mutation(
            request,
            lambda principal, headers, body: adapter.publish_snapshot(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                headers=headers,
                json_body=body,
            ),
        )

    async def get_publication_endpoint(workflow_id, execution_id, request):
        principal = await resolve_principal(request)
        headers = header_items(request)
        response = await offload(
            request,
            lambda: adapter.get_snapshot_publication(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                headers=headers,
            ),
        )
        return response_value(response)

    request_annotation = {"request": Request}
    propose_endpoint.__annotations__ = request_annotation
    preview_endpoint.__annotations__ = {"workflow_id": str, "request": Request}
    approve_endpoint.__annotations__ = {
        "workflow_id": str,
        "preview_id": str,
        "request": Request,
    }
    reject_endpoint.__annotations__ = dict(approve_endpoint.__annotations__)
    claim_endpoint.__annotations__ = {"workflow_id": str, "request": Request}
    execution_annotations = {
        "workflow_id": str,
        "execution_id": str,
        "request": Request,
    }
    started_endpoint.__annotations__ = execution_annotations
    verify_endpoint.__annotations__ = dict(execution_annotations)
    abort_endpoint.__annotations__ = dict(execution_annotations)
    get_endpoint.__annotations__ = {"workflow_id": str, "request": Request}
    host_bootstrap_endpoint.__annotations__ = {
        "host_session_id": str,
        "request": Request,
    }
    publication_annotations = {
        "workflow_id": str,
        "execution_id": str,
        "request": Request,
    }
    publication_endpoint.__annotations__ = publication_annotations
    get_publication_endpoint.__annotations__ = dict(publication_annotations)

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, error):
        del request, error
        return response_value(_invalid_request())

    base = "/v1/audit/workbook-edit-workflows"
    request_body = lambda filename: {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _openapi_ref(filename)}},
        }
    }

    def documented_response(schema, description):
        return {
            "description": description,
            "content": {"application/json": {"schema": schema}},
        }

    error_responses = {
        status: documented_response(
            _openapi_ref(_RESPONSE_SCHEMA_FILES["error"]),
            "Workbook edit error",
        )
        for status in (400, 403, 404, 409, 410, 413, 422, 500, 503)
    }

    def mutation_responses(*success_codes):
        return {
            **{
                status: documented_response(
                    _openapi_ref(_RESPONSE_SCHEMA_FILES["submission"]),
                    "Workbook edit submission",
                )
                for status in success_codes
            },
            **error_responses,
        }
    app.add_api_route(
        base,
        propose_endpoint,
        methods=["POST"],
        status_code=201,
        responses=mutation_responses(200, 201),
        openapi_extra=request_body(_SCHEMA_FILES["propose"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/previews",
        preview_endpoint,
        methods=["POST"],
        status_code=201,
        responses=mutation_responses(200, 201),
        openapi_extra=request_body(_SCHEMA_FILES["preview"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/previews/{preview_id}/approve",
        approve_endpoint,
        methods=["POST"],
        responses=mutation_responses(200),
        openapi_extra=request_body(_SCHEMA_FILES["approve"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/previews/{preview_id}/reject",
        reject_endpoint,
        methods=["POST"],
        responses=mutation_responses(200),
        openapi_extra=request_body(_SCHEMA_FILES["reject"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/executions/claim",
        claim_endpoint,
        methods=["POST"],
        status_code=201,
        responses=mutation_responses(200, 201),
        openapi_extra=request_body(_SCHEMA_FILES["claim"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/executions/{execution_id}/started",
        started_endpoint,
        methods=["POST"],
        responses=mutation_responses(200),
        openapi_extra=request_body(_SCHEMA_FILES["execution"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/executions/{execution_id}/verify",
        verify_endpoint,
        methods=["POST"],
        responses=mutation_responses(200),
        openapi_extra=request_body(_SCHEMA_FILES["verify"]),
    )
    app.add_api_route(
        base + "/{workflow_id}/executions/{execution_id}/abort",
        abort_endpoint,
        methods=["POST"],
        responses=mutation_responses(200),
        openapi_extra=request_body(_SCHEMA_FILES["execution"]),
    )
    app.add_api_route(
        base + "/{workflow_id}",
        get_endpoint,
        methods=["GET"],
        responses={
            200: documented_response(
                _openapi_ref(_RESPONSE_SCHEMA_FILES["workflow"]),
                "Workbook edit workflow",
            ),
            **error_responses,
        },
    )
    app.add_api_route(
        "/v1/audit/workbook-edit-host-sessions/{host_session_id}/bootstrap",
        host_bootstrap_endpoint,
        methods=["GET"],
        responses={
            200: documented_response(
                _openapi_ref(_RESPONSE_SCHEMA_FILES["bootstrap"]),
                "Authenticated workbook host bootstrap",
            ),
            **error_responses,
        },
    )
    publication_path = (
        base + "/{workflow_id}/executions/{execution_id}/snapshot-publication"
    )
    app.add_api_route(
        publication_path,
        publication_endpoint,
        methods=["POST"],
        responses={
            200: documented_response(
                _openapi_ref(_RESPONSE_SCHEMA_FILES["publication"]),
                "Published raw workbook source snapshot",
            ),
            **error_responses,
        },
        openapi_extra=request_body(_SCHEMA_FILES["publication"]),
    )
    app.add_api_route(
        publication_path,
        get_publication_endpoint,
        methods=["GET"],
        responses={
            200: documented_response(
                _openapi_ref(_RESPONSE_SCHEMA_FILES["publication"]),
                "Published raw workbook source snapshot",
            ),
            **error_responses,
        },
    )

    original_openapi = app.openapi

    def workbook_edit_openapi():
        document = original_openapi()
        components = document.setdefault("components", {}).setdefault("schemas", {})
        for name, schema in _OPENAPI_COMPONENTS.items():
            existing = components.get(name)
            if existing is not None and existing != schema:
                raise RuntimeError("audit workbook edit OpenAPI component name conflict")
            components[name] = copy.deepcopy(schema)
        return document

    app.openapi = workbook_edit_openapi
    return app


# Keep the familiar module-local factory name used by ``audit.web`` while retaining an explicit
# name for hosts that mount both applications.
create_fastapi_app = create_workbook_edit_fastapi_app


__all__ = [
    "APPROVE_REQUEST_SCHEMA",
    "AuditWorkbookEditHttpAdapter",
    "CLAIM_REQUEST_SCHEMA",
    "ERROR_RESPONSE_SCHEMA",
    "EXECUTION_REQUEST_SCHEMA",
    "HOST_BOOTSTRAP_RESPONSE_SCHEMA",
    "PREVIEW_REQUEST_SCHEMA",
    "PROPOSE_REQUEST_SCHEMA",
    "PUBLICATION_REQUEST_SCHEMA",
    "PUBLICATION_RESPONSE_SCHEMA",
    "RECEIPT_SCHEMA",
    "REJECT_REQUEST_SCHEMA",
    "REQUEST_SCHEMAS",
    "RESPONSE_SCHEMAS",
    "SUBMISSION_RESPONSE_SCHEMA",
    "VERIFY_REQUEST_SCHEMA",
    "WORKFLOW_RESPONSE_SCHEMA",
    "WorkbookEditHttpResponse",
    "WorkbookEditWebAdapterUnavailableError",
    "create_fastapi_app",
    "create_workbook_edit_fastapi_app",
]
