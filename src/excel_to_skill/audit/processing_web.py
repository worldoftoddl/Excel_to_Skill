"""Strict HTTP boundary for raw-workbook processing jobs.

The framework-neutral adapter accepts only opaque route identifiers, one exact scope-selection
document, and an authenticated :class:`ServicePrincipal`.  Filesystem paths, provider objects,
model settings, and package locations remain owned by :mod:`processing` and never cross this
boundary.  FastAPI is imported lazily so the core conversion package does not require the web
extra.
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
from .model import json_sha256
from .processing import (
    ProcessingScopeSelection,
    ProcessingServiceError,
    _public_scope_plan,
)
from .service import ServicePrincipal


_SCHEMA_FILES = {
    "job": "audit_processing_job.schema.json",
    "scope_selection": "audit_processing_scope_selection_request.schema.json",
    "submission": "audit_processing_submission.schema.json",
    "status": "audit_processing_status.schema.json",
    "error": "audit_processing_http_error.schema.json",
}
_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
}
_SERVICE_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "WORKBOOK_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "JOB_IN_PROGRESS": 409,
    "SCOPE_NOT_READY": 409,
    "SCOPE_CONFLICT": 409,
    "PLAN_CHANGED": 409,
    "INVALID_SCOPE_SELECTION": 422,
    "PROCESSING_PROFILE_CHANGED": 409,
    "DETERMINISTIC_PROCESSING_FAILED": 422,
    "PREPARATION_FAILED": 422,
    "AGGREGATION_FAILED": 422,
    "PUBLICATION_FAILED": 503,
    "BUNDLE_UNAVAILABLE": 503,
    "SERVICE_UNAVAILABLE": 503,
}

_MAX_HTTP_REQUEST_BYTES = 16 * 1024
_MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 24
_MAX_JSON_NODES = 400_000
_IDEMPOTENCY_KEY_MAX_LENGTH = 128
_WORKBOOK_ID_RE = re.compile(r"\Aworkbook-[0-9a-f]{48}\Z")
_JOB_ID_RE = re.compile(r"\Aprocess-[0-9a-f]{48}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_EMPTY_HTTP_BODY = object()


class ProcessingWebAdapterUnavailableError(RuntimeError):
    """FastAPI is not installed in the hosting application."""


class _ProcessingResponseContractError(RuntimeError):
    """A service result did not match the closed public contract."""


class _ProcessingResponseLimitError(RuntimeError):
    """A public response exceeded its fixed serialized byte limit."""


class _ProcessingTransportError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AuditProcessingHttpResponse:
    status_code: int
    body: dict[str, object]
    headers: Mapping[str, str]


def _load_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    try:
        for name, filename in _SCHEMA_FILES.items():
            value = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError(filename)
            validator_type = jsonschema.validators.validator_for(value)
            validator_type.check_schema(value)
            schemas[name] = value
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        jsonschema.SchemaError,
    ) as exc:
        raise RuntimeError("audit processing HTTP schemas are unavailable") from exc
    return schemas


_SCHEMAS = _load_schemas()
PROCESSING_JOB_SCHEMA = _SCHEMAS["job"]
PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA = _SCHEMAS["scope_selection"]
PROCESSING_SUBMISSION_SCHEMA = _SCHEMAS["submission"]
PROCESSING_STATUS_SCHEMA = _SCHEMAS["status"]
PROCESSING_HTTP_ERROR_SCHEMA = _SCHEMAS["error"]

_SCHEMA_REGISTRY = Registry()
for _schema_name, _schema_filename in _SCHEMA_FILES.items():
    _SCHEMA_REGISTRY = _SCHEMA_REGISTRY.with_resource(
        _schema_filename,
        Resource.from_contents(_SCHEMAS[_schema_name]),
    )
_VALIDATORS = {
    name: jsonschema.Draft202012Validator(schema, registry=_SCHEMA_REGISTRY)
    for name, schema in _SCHEMAS.items()
}


def _safe_json_shape(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, parent_depth = stack.pop()
        visited += 1
        if visited > _MAX_JSON_NODES:
            return False
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, dict):
            depth = parent_depth + 1
            if depth > _MAX_JSON_DEPTH:
                return False
            for key, nested in item.items():
                if not isinstance(key, str):
                    return False
                stack.append((nested, depth))
        elif isinstance(item, (list, tuple)):
            depth = parent_depth + 1
            if depth > _MAX_JSON_DEPTH:
                return False
            for nested in item:
                stack.append((nested, depth))
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            return False
    return True


def _encoded_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validated_response_body(name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _ProcessingResponseContractError("invalid processing response")
    if not _safe_json_shape(value):
        raise _ProcessingResponseLimitError("processing response is too large")
    try:
        _VALIDATORS[name].validate(value)
        encoded = _encoded_json(value)
    except (
        jsonschema.ValidationError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise _ProcessingResponseContractError("invalid processing response") from exc
    if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
        raise _ProcessingResponseLimitError("processing response is too large")
    return copy.deepcopy(value)


def _selection_document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not _safe_json_shape(value):
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
    try:
        encoded = _encoded_json(value)
        if len(encoded) > _MAX_HTTP_REQUEST_BYTES:
            raise _ProcessingTransportError("LIMIT_EXCEEDED", status_code=413)
        _VALIDATORS["scope_selection"].validate(value)
    except _ProcessingTransportError:
        raise
    except (
        jsonschema.ValidationError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ):
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400) from None
    return copy.deepcopy(value)


def _start_document(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or value:
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)


def _fixed_error(code: str, message: str, *, status_code: int) -> AuditProcessingHttpResponse:
    body = _validated_response_body(
        "error",
        {
            "schema_version": "audit_processing_http_error.v1",
            "error": {"code": code, "message": message},
        },
    )
    return AuditProcessingHttpResponse(
        status_code=status_code,
        body=body,
        headers=dict(_JSON_HEADERS),
    )


def _transport_error_response(error: _ProcessingTransportError) -> AuditProcessingHttpResponse:
    messages = {
        "INVALID_REQUEST": "The processing request is invalid.",
        "MISSING_IDEMPOTENCY_KEY": "Exactly one valid Idempotency-Key is required.",
        "UNSUPPORTED_MEDIA_TYPE": "A non-empty processing request must use application/json.",
        "LIMIT_EXCEEDED": "The processing request or response exceeds the supported limit.",
    }
    message = messages.get(error.code)
    if message is None:
        return _internal_error()
    return _fixed_error(error.code, message, status_code=error.status_code)


def _service_error_response(error: ProcessingServiceError) -> AuditProcessingHttpResponse:
    status_code = _SERVICE_ERROR_STATUS.get(getattr(error, "code", None))
    if status_code is None or getattr(error, "status_code", None) != status_code:
        return _internal_error()
    return _fixed_error(
        error.code,
        "The processing request could not be completed.",
        status_code=status_code,
    )


def _internal_error() -> AuditProcessingHttpResponse:
    return _fixed_error(
        "INTERNAL_ERROR",
        "The processing service could not complete the request.",
        status_code=500,
    )


def _response_limit_error() -> AuditProcessingHttpResponse:
    return _fixed_error(
        "LIMIT_EXCEEDED",
        "The processing request or response exceeds the supported limit.",
        status_code=413,
    )


def _header_values(headers: object, name: str) -> list[str]:
    if isinstance(headers, Mapping):
        items = list(headers.items())
    elif isinstance(headers, (list, tuple)) and all(
        isinstance(item, tuple) and len(item) == 2 for item in headers
    ):
        items = list(headers)
    else:
        return []
    return [
        value
        for header_name, value in items
        if isinstance(header_name, str)
        and header_name.lower() == name.lower()
        and isinstance(value, str)
    ]


def _validate_idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _IDEMPOTENCY_KEY_MAX_LENGTH
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise _ProcessingTransportError("MISSING_IDEMPOTENCY_KEY", status_code=400)
    return value


def _idempotency_key(
    headers: object,
    direct_value: object | None,
) -> str:
    if direct_value is not None:
        if headers is not None:
            raise _ProcessingTransportError("MISSING_IDEMPOTENCY_KEY", status_code=400)
        return _validate_idempotency_key(direct_value)
    values = _header_values(headers, "Idempotency-Key")
    if len(values) != 1:
        raise _ProcessingTransportError("MISSING_IDEMPOTENCY_KEY", status_code=400)
    return _validate_idempotency_key(values[0])


def _job_document(job: object) -> dict[str, object]:
    to_public_dict = getattr(job, "to_public_dict", None)
    if not callable(to_public_dict):
        raise _ProcessingResponseContractError("invalid processing job")
    document = _validated_response_body("job", to_public_dict())
    _validate_job_semantics(document)
    return document


def _validate_job_semantics(document: dict[str, object]) -> None:
    """Close cross-field invariants that JSON Schema cannot express safely."""

    try:
        status = document["status"]
        plan_sha = document["scope_plan_sha256"]
        plan = document["scope_plan"]
        selection_doc = document["selection"]
        progress = document["progress"]
        result = document["result"]
        failure = document["failure"]
        if not isinstance(progress, dict):
            raise ValueError

        normalized_plan: dict[str, object] | None = None
        if plan is None:
            if plan_sha is not None:
                raise ValueError
        else:
            if not isinstance(plan, dict) or not isinstance(plan_sha, str):
                raise ValueError
            normalized_plan = _public_scope_plan(plan)
            if normalized_plan != plan or json_sha256(plan) != plan_sha:
                raise ValueError

        selection: ProcessingScopeSelection | None = None
        if selection_doc is not None:
            if not isinstance(selection_doc, dict) or normalized_plan is None:
                raise ValueError
            scopes = selection_doc.get("scopes")
            if not isinstance(scopes, list) or any(not isinstance(item, dict) for item in scopes):
                raise ValueError
            selection = ProcessingScopeSelection(
                mode=selection_doc.get("mode"),  # type: ignore[arg-type]
                scope_plan_sha256=selection_doc.get("scope_plan_sha256"),  # type: ignore[arg-type]
                scope_ids=tuple(item.get("scope_id") for item in scopes),  # type: ignore[arg-type]
                sheets=tuple(item.get("sheet") for item in scopes),  # type: ignore[arg-type]
            )
            if selection.identity() != selection_doc or selection.scope_plan_sha256 != plan_sha:
                raise ValueError
            plan_entries = normalized_plan["sheets"]
            if not isinstance(plan_entries, list):
                raise ValueError
            analyzable = [item for item in plan_entries if item["analyzable"] is True]
            expected_pairs = [
                (item["scope"]["id"], item["scope"]["sheet"]) for item in analyzable
            ]
            selected_pairs = list(zip(selection.scope_ids, selection.sheets, strict=True))
            if selection.mode == "all_sheets" and selected_pairs != expected_pairs:
                raise ValueError
            if selection.mode == "selected_sheets":
                selected_set = set(selected_pairs)
                if (
                    not selected_set.issubset(set(expected_pairs))
                    or selected_pairs != [pair for pair in expected_pairs if pair in selected_set]
                ):
                    raise ValueError

        state_shape = {
            "planning": (False, False, False, False),
            "awaiting_scope": (True, False, False, False),
            "preparing": (True, True, False, False),
            "aggregating": (True, True, False, False),
            "published": (True, True, True, False),
            "failed": (plan is not None, selection is not None, False, True),
        }[status]
        observed_shape = (
            plan is not None,
            selection is not None,
            result is not None,
            failure is not None,
        )
        if observed_shape != state_shape:
            raise ValueError

        total = progress.get("total_scopes")
        completed = progress.get("completed_scopes")
        current = progress.get("current_scope_ids")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or not isinstance(completed, int)
            or isinstance(completed, bool)
            or not isinstance(current, list)
            or completed > total
        ):
            raise ValueError
        if selection is None:
            if (total, completed, current) != (0, 0, []):
                raise ValueError
        else:
            expected_total = 1 if selection.mode == "workbook" else len(selection.scope_ids)
            if total != expected_total:
                raise ValueError
            if selection.mode == "workbook":
                if current:
                    raise ValueError
            elif (
                not set(current).issubset(set(selection.scope_ids))
                or completed + len(current) != total
            ):
                raise ValueError
            if status in {"aggregating", "published"} or (
                status == "failed"
                and isinstance(failure, dict)
                and failure.get("stage") in {"aggregating", "publishing"}
            ):
                if completed != total or current:
                    raise ValueError

        if status == "failed":
            if not isinstance(failure, dict):
                raise ValueError
            stage = failure.get("stage")
            if stage == "planning" and (plan is not None or selection is not None):
                raise ValueError
            if stage != "planning" and selection is None:
                raise ValueError

        if result is not None:
            if not isinstance(result, dict) or selection is None or normalized_plan is None:
                raise ValueError
            if result.get("selection_sha256") != selection.selection_sha256:
                raise ValueError
            binding = result.get("chat_binding")
            coverage = result.get("coverage")
            if not isinstance(binding, dict) or not isinstance(coverage, dict):
                raise ValueError
            all_sheets_skipped = (
                [
                    item["scope"]["sheet"]
                    for item in normalized_plan["sheets"]
                    if item["analyzable"] is False
                ]
                if selection.mode == "all_sheets"
                else []
            )
            if selection.mode == "workbook":
                expected_binding = {"sheet": None, "aggregate_id": None}
                expected_included = [item["scope"]["sheet"] for item in normalized_plan["sheets"]]
                expected_skipped: list[str] = []
            elif len(selection.sheets) == 1:
                expected_binding = {"sheet": selection.sheets[0], "aggregate_id": None}
                expected_included = list(selection.sheets)
                expected_skipped = all_sheets_skipped
            else:
                expected_binding = {"sheet": None, "aggregate_id": binding.get("aggregate_id")}
                if not isinstance(expected_binding["aggregate_id"], str):
                    raise ValueError
                expected_included = list(selection.sheets)
                expected_skipped = all_sheets_skipped
            if binding != expected_binding:
                raise ValueError
            if coverage != {
                "included_sheets": expected_included,
                "skipped_empty_sheets": expected_skipped,
            }:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise _ProcessingResponseContractError("invalid processing response") from None


def _submission_document(submission: object) -> tuple[dict[str, object], bool]:
    replayed = getattr(submission, "replayed", None)
    if not isinstance(replayed, bool):
        raise _ProcessingResponseContractError("invalid processing submission")
    job = _job_document(getattr(submission, "job", None))
    body = _validated_response_body(
        "submission",
        {
            "schema_version": "audit_processing_submission.v1",
            "replayed": replayed,
            "job": job,
        },
    )
    return body, replayed


def _valid_principal(principal: object) -> bool:
    return isinstance(principal, ServicePrincipal)


class AuditProcessingHttpAdapter:
    """Framework-neutral adapter over a strict processing-service callable contract."""

    def __init__(self, service: object) -> None:
        for name in ("start", "select_scope", "get_job"):
            if not callable(getattr(service, name, None)):
                raise TypeError("service must implement start, select_scope, and get_job")
        self._service = service

    def start(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
        headers: object = None,
        json_body: object = None,
        idempotency_key: object | None = None,
    ) -> AuditProcessingHttpResponse:
        try:
            if (
                not _valid_principal(principal)
                or not isinstance(workbook_id, str)
                or _WORKBOOK_ID_RE.fullmatch(workbook_id) is None
                or not isinstance(raw_snapshot_id, str)
                or _SHA256_RE.fullmatch(raw_snapshot_id) is None
            ):
                raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
            _start_document(json_body)
            key = _idempotency_key(headers, idempotency_key)
            submission = self._service.start(
                principal=principal,
                workbook_id=workbook_id,
                raw_snapshot_id=raw_snapshot_id,
                idempotency_key=key,
            )
            body, replayed = _submission_document(submission)
            job = body["job"]
            if (
                not isinstance(job, dict)
                or job.get("workbook_id") != workbook_id
                or job.get("raw_snapshot_id") != raw_snapshot_id
            ):
                raise _ProcessingResponseContractError("processing start route mismatch")
            job_id = job["job_id"]
        except _ProcessingTransportError as error:
            return _transport_error_response(error)
        except ProcessingServiceError as error:
            return _service_error_response(error)
        except _ProcessingResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return AuditProcessingHttpResponse(
            status_code=200 if replayed else 201,
            body=body,
            headers={
                **_JSON_HEADERS,
                "Location": f"/v1/audit/processing-jobs/{job_id}",
                "Idempotency-Replayed": "true" if replayed else "false",
            },
        )

    def select_scope(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        headers: object = None,
        json_body: object,
        idempotency_key: object | None = None,
    ) -> AuditProcessingHttpResponse:
        try:
            if (
                not _valid_principal(principal)
                or not isinstance(job_id, str)
                or _JOB_ID_RE.fullmatch(job_id) is None
            ):
                raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
            document = _selection_document(json_body)
            key = _idempotency_key(headers, idempotency_key)
            submission = self._service.select_scope(
                principal=principal,
                job_id=job_id,
                mode=document["mode"],
                scope_plan_sha256=document["scope_plan_sha256"],
                scope_ids=document["scope_ids"],
                idempotency_key=key,
            )
            body, replayed = _submission_document(submission)
            job = body["job"]
            if not isinstance(job, dict) or job.get("job_id") != job_id:
                raise _ProcessingResponseContractError("processing selection route mismatch")
        except _ProcessingTransportError as error:
            return _transport_error_response(error)
        except ProcessingServiceError as error:
            return _service_error_response(error)
        except _ProcessingResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return AuditProcessingHttpResponse(
            status_code=200 if replayed else 201,
            body=body,
            headers={
                **_JSON_HEADERS,
                "Location": f"/v1/audit/processing-jobs/{job_id}",
                "Idempotency-Replayed": "true" if replayed else "false",
            },
        )

    def get_status(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
    ) -> AuditProcessingHttpResponse:
        try:
            if (
                not _valid_principal(principal)
                or not isinstance(job_id, str)
                or _JOB_ID_RE.fullmatch(job_id) is None
            ):
                raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
            job = _job_document(self._service.get_job(principal=principal, job_id=job_id))
            if job.get("job_id") != job_id:
                raise _ProcessingResponseContractError("processing status route mismatch")
            body = _validated_response_body(
                "status",
                {"schema_version": "audit_processing_status.v1", "job": job},
            )
        except _ProcessingTransportError as error:
            return _transport_error_response(error)
        except ProcessingServiceError as error:
            return _service_error_response(error)
        except _ProcessingResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return AuditProcessingHttpResponse(
            status_code=200,
            body=body,
            headers=dict(_JSON_HEADERS),
        )

    def get_job(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
    ) -> AuditProcessingHttpResponse:
        """Compatibility spelling for framework-neutral status reads."""

        return self.get_status(principal=principal, job_id=job_id)


def _raw_json_within_nesting_limit(raw: bytes) -> bool:
    containers: list[int] = []
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            if len(containers) >= _MAX_JSON_DEPTH:
                return False
            containers.append(byte)
        elif byte == 0x5D:
            if not containers or containers.pop() != 0x5B:
                return False
        elif byte == 0x7D:
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


async def _read_request_document(request, *, allow_empty: bool) -> object:
    raw_headers = request.scope.get("headers", ())
    content_lengths = [
        value
        for name, value in raw_headers
        if isinstance(name, bytes)
        and isinstance(value, bytes)
        and name.lower() == b"content-length"
    ]
    if len(content_lengths) > 1:
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
    declared_length: int | None = None
    if content_lengths:
        raw_length = content_lengths[0]
        if not raw_length or not raw_length.isdigit():
            raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
        declared_length = int(raw_length)
        if declared_length > _MAX_HTTP_REQUEST_BYTES:
            raise _ProcessingTransportError("LIMIT_EXCEEDED", status_code=413)

    buffered = bytearray()
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
        if not chunk:
            continue
        if len(buffered) + len(chunk) > _MAX_HTTP_REQUEST_BYTES:
            raise _ProcessingTransportError("LIMIT_EXCEEDED", status_code=413)
        buffered.extend(chunk)
    raw = bytes(buffered)
    if declared_length is not None and len(raw) != declared_length:
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
    if not raw:
        if allow_empty:
            return _EMPTY_HTTP_BODY
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)

    content_types = [
        value.decode("latin-1")
        for name, value in raw_headers
        if isinstance(name, bytes)
        and isinstance(value, bytes)
        and name.lower() == b"content-type"
    ]
    if (
        len(content_types) != 1
        or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
    ):
        raise _ProcessingTransportError("UNSUPPORTED_MEDIA_TYPE", status_code=415)
    if not _raw_json_within_nesting_limit(raw):
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400)
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _ProcessingTransportError("INVALID_REQUEST", status_code=400) from None


def create_processing_fastapi_app(
    adapter: AuditProcessingHttpAdapter,
    *,
    principal_dependency,
    executor_workers: int = 4,
):
    """Create the optional authenticated processing API without eager FastAPI imports."""

    if not isinstance(adapter, AuditProcessingHttpAdapter):
        raise TypeError("adapter must be an AuditProcessingHttpAdapter")
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
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ProcessingWebAdapterUnavailableError(
            "FastAPI is optional; install it in the hosting web application."
        ) from exc

    @asynccontextmanager
    async def lifespan(app):
        executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="audit-processing-web",
        )
        app.state.audit_processing_executor = executor
        app.state.audit_processing_executor_gate = asyncio.Semaphore(executor_workers * 2)
        try:
            yield
        finally:
            app.state.audit_processing_executor = None
            app.state.audit_processing_executor_gate = None
            executor.shutdown(wait=True, cancel_futures=True)

    async def offload(request, call):
        executor = getattr(request.app.state, "audit_processing_executor", None)
        gate = getattr(request.app.state, "audit_processing_executor_gate", None)
        if not isinstance(executor, ThreadPoolExecutor) or not isinstance(
            gate, asyncio.Semaphore
        ):
            raise RuntimeError("processing executor is outside its application lifespan")
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
        if not isinstance(principal, ServicePrincipal):
            raise RuntimeError("principal dependency returned an invalid principal")
        return principal

    def header_items(request):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.scope.get("headers", ())
            if isinstance(name, bytes) and isinstance(value, bytes)
        ]

    def json_response(response: AuditProcessingHttpResponse):
        return JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=dict(response.headers),
        )

    async def authenticated_principal(request):
        try:
            return await resolve_principal(request)
        except HTTPException:
            raise
        except Exception:
            return None

    async def start_endpoint(workbook_id, raw_snapshot_id, request):
        principal = await authenticated_principal(request)
        if principal is None:
            return json_response(_internal_error())
        try:
            body = await _read_request_document(request, allow_empty=True)
        except _ProcessingTransportError as error:
            return json_response(_transport_error_response(error))
        if body is _EMPTY_HTTP_BODY:
            body = None
        elif body is None:
            # JSON ``null`` is not an empty request body and is not the exact empty object.
            return json_response(
                _transport_error_response(
                    _ProcessingTransportError("INVALID_REQUEST", status_code=400)
                )
            )
        response = await offload(
            request,
            lambda: adapter.start(
                principal=principal,
                workbook_id=workbook_id,
                raw_snapshot_id=raw_snapshot_id,
                headers=header_items(request),
                json_body=body,
            ),
        )
        return json_response(response)

    async def scope_selection_endpoint(job_id, request):
        principal = await authenticated_principal(request)
        if principal is None:
            return json_response(_internal_error())
        try:
            body = await _read_request_document(request, allow_empty=False)
        except _ProcessingTransportError as error:
            return json_response(_transport_error_response(error))
        response = await offload(
            request,
            lambda: adapter.select_scope(
                principal=principal,
                job_id=job_id,
                headers=header_items(request),
                json_body=body,
            ),
        )
        return json_response(response)

    async def status_endpoint(job_id, request):
        principal = await authenticated_principal(request)
        if principal is None:
            return json_response(_internal_error())
        response = await offload(
            request,
            lambda: adapter.get_status(principal=principal, job_id=job_id),
        )
        return json_response(response)

    resource_annotations = {
        "workbook_id": str,
        "raw_snapshot_id": str,
        "request": Request,
    }
    start_endpoint.__annotations__ = resource_annotations
    job_annotations = {"job_id": str, "request": Request}
    scope_selection_endpoint.__annotations__ = job_annotations
    status_endpoint.__annotations__ = dict(job_annotations)

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, error):
        del request, error
        return json_response(
            _fixed_error(
                "INVALID_REQUEST",
                "The processing request is invalid.",
                status_code=400,
            )
        )

    start_path = (
        "/v1/audit/workbooks/{workbook_id}/raw-snapshots/{raw_snapshot_id}/processing-jobs"
    )
    app.add_api_route(
        start_path,
        start_endpoint,
        methods=["POST"],
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": False,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "maxProperties": 0,
                        }
                    }
                },
            }
        },
    )
    job_path = "/v1/audit/processing-jobs/{job_id}"
    app.add_api_route(job_path, status_endpoint, methods=["GET"])
    app.add_api_route(
        job_path + "/scope-selection",
        scope_selection_endpoint,
        methods=["POST"],
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": copy.deepcopy(PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA)
                    }
                },
            }
        },
    )
    return app


# Compact aliases keep the module's names predictable for hosts while the class names retain the
# audit domain prefix used by the workbook-edit HTTP boundary.
ProcessingHttpAdapter = AuditProcessingHttpAdapter
ProcessingHttpResponse = AuditProcessingHttpResponse
AuditProcessingWebAdapterUnavailableError = ProcessingWebAdapterUnavailableError
create_audit_processing_fastapi_app = create_processing_fastapi_app
PROCESSING_SUBMISSION_RESPONSE_SCHEMA = PROCESSING_SUBMISSION_SCHEMA
PROCESSING_STATUS_RESPONSE_SCHEMA = PROCESSING_STATUS_SCHEMA
PROCESSING_ERROR_RESPONSE_SCHEMA = PROCESSING_HTTP_ERROR_SCHEMA


__all__ = [
    "AuditProcessingHttpAdapter",
    "AuditProcessingHttpResponse",
    "AuditProcessingWebAdapterUnavailableError",
    "PROCESSING_ERROR_RESPONSE_SCHEMA",
    "PROCESSING_HTTP_ERROR_SCHEMA",
    "PROCESSING_JOB_SCHEMA",
    "PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA",
    "PROCESSING_STATUS_RESPONSE_SCHEMA",
    "PROCESSING_STATUS_SCHEMA",
    "PROCESSING_SUBMISSION_RESPONSE_SCHEMA",
    "PROCESSING_SUBMISSION_SCHEMA",
    "ProcessingHttpAdapter",
    "ProcessingHttpResponse",
    "ProcessingWebAdapterUnavailableError",
    "create_audit_processing_fastapi_app",
    "create_processing_fastapi_app",
]
