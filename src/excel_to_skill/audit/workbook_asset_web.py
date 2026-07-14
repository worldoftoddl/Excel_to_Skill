"""Strict HTTP boundary for server-owned raw workbook assets.

The upload transport deliberately accepts a raw XLSX body rather than multipart form data.  A
hosting application authenticates the request first; only then does this adapter inspect headers
and consume a bounded request stream.  Client requests never select a filesystem path, object-store
locator, workbook identifier, digest, or download filename.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Mapping

import jsonschema
from referencing import Registry, Resource

from ..resources import SCHEMA_DIR
from .service import ServicePrincipal
from .workbook_asset_service import WorkbookAssetService, WorkbookAssetServiceError


XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_RAW_WORKBOOK_UPLOAD_BYTES = 64 * 1024 * 1024

_MAX_JSON_RESPONSE_BYTES = 64 * 1024
_MAX_JSON_RESPONSE_DEPTH = 12
_MAX_JSON_RESPONSE_NODES = 2_000
_IDEMPOTENCY_KEY_MAX_LENGTH = 128
_DOWNLOAD_FILENAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.xlsx\Z")

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
}
_SCHEMA_FILES = {
    "snapshot": "audit_raw_workbook_snapshot.schema.json",
    "submission": "audit_workbook_asset_http_submission_response.schema.json",
    "status": "audit_workbook_asset_http_status_response.schema.json",
    "error": "audit_workbook_asset_http_error_response.schema.json",
}
_SERVICE_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "SOURCE_LIMIT_EXCEEDED": 413,
    "INVALID_WORKBOOK": 422,
    "IDEMPOTENCY_CONFLICT": 409,
    "COMMAND_IN_PROGRESS": 409,
    "STALE_UPLOAD_CLAIM": 409,
    "WORKBOOK_NOT_FOUND": 404,
    "ASSET_INTEGRITY_MISMATCH": 503,
    "SERVICE_UNAVAILABLE": 503,
}


class WorkbookAssetWebAdapterUnavailableError(RuntimeError):
    """FastAPI is not installed in the hosting application."""


class _WorkbookAssetResponseContractError(RuntimeError):
    """A service result did not match the closed public response contract."""


class _WorkbookAssetResponseLimitError(RuntimeError):
    """A valid JSON response exceeded the public response byte limit."""


class _WorkbookAssetTransportError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WorkbookAssetHttpResponse:
    status_code: int
    body: dict[str, object]
    headers: Mapping[str, str]


@dataclass(frozen=True)
class WorkbookAssetDownloadHttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class _UploadPreflight:
    idempotency_key: str
    declared_length: int | None


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
        raise RuntimeError("audit workbook asset HTTP schemas are unavailable") from exc
    return schemas


_SCHEMAS = _load_schemas()
RAW_WORKBOOK_SNAPSHOT_SCHEMA = _SCHEMAS["snapshot"]
WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA = _SCHEMAS["submission"]
WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA = _SCHEMAS["status"]
WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA = _SCHEMAS["error"]

_SCHEMA_REGISTRY = Registry().with_resource(
    _SCHEMA_FILES["snapshot"],
    Resource.from_contents(RAW_WORKBOOK_SNAPSHOT_SCHEMA),
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
        if visited > _MAX_JSON_RESPONSE_NODES:
            return False
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, dict):
            depth = parent_depth + 1
            if depth > _MAX_JSON_RESPONSE_DEPTH:
                return False
            for key, nested in item.items():
                if not isinstance(key, str):
                    return False
                stack.append((nested, depth))
        elif isinstance(item, (list, tuple)):
            depth = parent_depth + 1
            if depth > _MAX_JSON_RESPONSE_DEPTH:
                return False
            for nested in item:
                stack.append((nested, depth))
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            return False
    return True


def _validated_response_body(name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not _safe_json_shape(value):
        raise _WorkbookAssetResponseContractError("invalid workbook asset response")
    try:
        _VALIDATORS[name].validate(value)
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
        raise _WorkbookAssetResponseContractError(
            "invalid workbook asset response"
        ) from exc
    if len(encoded) > _MAX_JSON_RESPONSE_BYTES:
        raise _WorkbookAssetResponseLimitError("workbook asset response is too large")
    return copy.deepcopy(value)


def _fixed_error(code: str, message: str, *, status_code: int) -> WorkbookAssetHttpResponse:
    body = _validated_response_body(
        "error",
        {
            "schema_version": "audit_workbook_asset_http_error.v1",
            "error": {"code": code, "message": message},
        },
    )
    return WorkbookAssetHttpResponse(
        status_code=status_code,
        body=body,
        headers=dict(_JSON_HEADERS),
    )


def _transport_error_response(error: _WorkbookAssetTransportError) -> WorkbookAssetHttpResponse:
    messages = {
        "INVALID_REQUEST": "The raw workbook upload request is invalid.",
        "MISSING_IDEMPOTENCY_KEY": "Exactly one valid Idempotency-Key is required.",
        "SOURCE_LIMIT_EXCEEDED": "The raw workbook upload exceeds the supported byte limit.",
        "UNSUPPORTED_MEDIA_TYPE": "The upload must be one raw XLSX document.",
        "UNSUPPORTED_CONTENT_ENCODING": "Encoded workbook upload bodies are not supported.",
        "UPLOAD_TIMEOUT": "The raw workbook upload body was not received in time.",
    }
    message = messages.get(error.code)
    if message is None:
        return _internal_error()
    return _fixed_error(error.code, message, status_code=error.status_code)


def _service_error_response(error: WorkbookAssetServiceError) -> WorkbookAssetHttpResponse:
    status_code = _SERVICE_ERROR_STATUS.get(error.code)
    if status_code is None or error.status_code != status_code:
        return _internal_error()
    return _fixed_error(
        error.code,
        "The workbook asset request could not be completed.",
        status_code=status_code,
    )


def _internal_error() -> WorkbookAssetHttpResponse:
    return _fixed_error(
        "INTERNAL_ERROR",
        "The workbook asset service could not complete the request.",
        status_code=500,
    )


def _response_limit_error() -> WorkbookAssetHttpResponse:
    return _fixed_error(
        "LIMIT_EXCEEDED",
        "The workbook asset response exceeds the supported limit.",
        status_code=413,
    )


def _header_values(headers, name: str) -> list[str]:
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


def _idempotency_key(headers) -> str:
    values = _header_values(headers, "Idempotency-Key")
    if len(values) != 1:
        raise _WorkbookAssetTransportError("MISSING_IDEMPOTENCY_KEY", status_code=400)
    value = values[0]
    if (
        not 1 <= len(value) <= _IDEMPOTENCY_KEY_MAX_LENGTH
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise _WorkbookAssetTransportError("MISSING_IDEMPOTENCY_KEY", status_code=400)
    return value


def _upload_preflight(headers, *, maximum: int) -> _UploadPreflight:
    key = _idempotency_key(headers)
    content_types = _header_values(headers, "Content-Type")
    if len(content_types) != 1 or content_types[0].lower() != XLSX_CONTENT_TYPE:
        raise _WorkbookAssetTransportError("UNSUPPORTED_MEDIA_TYPE", status_code=415)
    if _header_values(headers, "Content-Encoding"):
        raise _WorkbookAssetTransportError("UNSUPPORTED_CONTENT_ENCODING", status_code=415)
    lengths = _header_values(headers, "Content-Length")
    if len(lengths) > 1:
        raise _WorkbookAssetTransportError("INVALID_REQUEST", status_code=400)
    declared_length: int | None = None
    if lengths:
        raw_length = lengths[0]
        if not raw_length or not raw_length.isascii() or not raw_length.isdigit():
            raise _WorkbookAssetTransportError("INVALID_REQUEST", status_code=400)
        declared_length = int(raw_length)
        if declared_length < 1:
            raise _WorkbookAssetTransportError("INVALID_REQUEST", status_code=400)
        if declared_length > maximum:
            raise _WorkbookAssetTransportError("SOURCE_LIMIT_EXCEEDED", status_code=413)
    return _UploadPreflight(idempotency_key=key, declared_length=declared_length)


def _snapshot_document(snapshot: object) -> dict[str, object]:
    to_public_dict = getattr(snapshot, "to_public_dict", None)
    if not callable(to_public_dict):
        raise _WorkbookAssetResponseContractError("invalid raw workbook snapshot")
    return _validated_response_body("snapshot", to_public_dict())


class WorkbookAssetHttpAdapter:
    """Framework-neutral strict adapter over one principal-scoped asset service."""

    def __init__(self, service: WorkbookAssetService) -> None:
        if not isinstance(service, WorkbookAssetService):
            raise TypeError("service must be a WorkbookAssetService")
        self._service = service

    def upload(
        self,
        *,
        principal: ServicePrincipal,
        content: bytes,
        idempotency_key: str,
    ) -> WorkbookAssetHttpResponse:
        try:
            submission = self._service.upload(principal, content, idempotency_key)
            replayed = getattr(submission, "replayed", None)
            if not isinstance(replayed, bool):
                raise _WorkbookAssetResponseContractError("invalid upload submission")
            snapshot = _snapshot_document(getattr(submission, "snapshot", None))
            body = _validated_response_body(
                "submission",
                {
                    "schema_version": "audit_workbook_asset_http_submission.v1",
                    "replayed": replayed,
                    "snapshot": snapshot,
                },
            )
            workbook_id = snapshot["workbook_id"]
            raw_snapshot_id = snapshot["raw_snapshot_id"]
        except WorkbookAssetServiceError as error:
            return _service_error_response(error)
        except _WorkbookAssetResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookAssetHttpResponse(
            status_code=200 if replayed else 201,
            body=body,
            headers={
                **_JSON_HEADERS,
                "Location": (
                    f"/v1/audit/workbooks/{workbook_id}/raw-snapshots/{raw_snapshot_id}"
                ),
                "Idempotency-Replayed": "true" if replayed else "false",
            },
        )

    def get_status(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> WorkbookAssetHttpResponse:
        try:
            snapshot = _snapshot_document(
                self._service.get_snapshot(principal, workbook_id, raw_snapshot_id)
            )
            if (
                snapshot.get("workbook_id") != workbook_id
                or snapshot.get("raw_snapshot_id") != raw_snapshot_id
            ):
                raise _WorkbookAssetResponseContractError("snapshot route mismatch")
            body = _validated_response_body(
                "status",
                {
                    "schema_version": "audit_workbook_asset_http_status.v1",
                    "snapshot": snapshot,
                },
            )
        except WorkbookAssetServiceError as error:
            return _service_error_response(error)
        except _WorkbookAssetResponseLimitError:
            return _response_limit_error()
        except Exception:
            return _internal_error()
        return WorkbookAssetHttpResponse(
            status_code=200,
            body=body,
            headers=dict(_JSON_HEADERS),
        )

    def download(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> WorkbookAssetDownloadHttpResponse | WorkbookAssetHttpResponse:
        try:
            download = self._service.download(principal, workbook_id, raw_snapshot_id)
            snapshot = _snapshot_document(getattr(download, "snapshot", None))
            if (
                snapshot.get("workbook_id") != workbook_id
                or snapshot.get("raw_snapshot_id") != raw_snapshot_id
            ):
                raise _WorkbookAssetResponseContractError("download route mismatch")
            content = getattr(download, "content", None)
            filename = getattr(download, "filename", None)
            if (
                not isinstance(content, bytes)
                or len(content) != snapshot.get("size_bytes")
                or not isinstance(filename, str)
                or _DOWNLOAD_FILENAME_RE.fullmatch(filename) is None
                or "\r" in filename
                or "\n" in filename
            ):
                raise _WorkbookAssetResponseContractError("invalid workbook download")
            workbook_sha256 = snapshot.get("workbook_sha256")
            if not isinstance(workbook_sha256, str):
                raise _WorkbookAssetResponseContractError("invalid workbook digest")
        except WorkbookAssetServiceError as error:
            return _service_error_response(error)
        except Exception:
            return _internal_error()
        return WorkbookAssetDownloadHttpResponse(
            status_code=200,
            content=content,
            headers={
                "Content-Type": XLSX_CONTENT_TYPE,
                "Content-Length": str(len(content)),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, private",
                "ETag": f'"{workbook_sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )


async def _read_bounded_upload(
    request,
    *,
    maximum: int,
    declared_length: int | None,
) -> bytes:
    threshold = min(maximum, 8 * 1024 * 1024)
    total = 0
    spool = tempfile.SpooledTemporaryFile(max_size=threshold, mode="w+b")
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                raise _WorkbookAssetTransportError("INVALID_REQUEST", status_code=400)
            if not chunk:
                continue
            if total + len(chunk) > maximum:
                raise _WorkbookAssetTransportError(
                    "SOURCE_LIMIT_EXCEEDED", status_code=413
                )
            spool.write(chunk)
            total += len(chunk)
        if total < 1 or (declared_length is not None and declared_length != total):
            raise _WorkbookAssetTransportError("INVALID_REQUEST", status_code=400)
        spool.seek(0)
        content = spool.read(maximum + 1)
        if not isinstance(content, bytes) or len(content) != total or len(content) > maximum:
            raise _WorkbookAssetTransportError("SOURCE_LIMIT_EXCEEDED", status_code=413)
        return content
    finally:
        spool.close()


def create_workbook_asset_fastapi_app(
    adapter: WorkbookAssetHttpAdapter,
    *,
    principal_dependency,
    executor_workers: int = 4,
    upload_concurrency: int = 2,
    max_upload_bytes: int = MAX_RAW_WORKBOOK_UPLOAD_BYTES,
    upload_read_timeout_seconds: float = 120.0,
):
    """Create a lazy FastAPI app for authenticated raw workbook assets."""

    if not isinstance(adapter, WorkbookAssetHttpAdapter):
        raise TypeError("adapter must be a WorkbookAssetHttpAdapter")
    if not callable(principal_dependency):
        raise TypeError("principal_dependency must be callable")
    if (
        not isinstance(executor_workers, int)
        or isinstance(executor_workers, bool)
        or not 1 <= executor_workers <= 32
    ):
        raise ValueError("executor_workers must be an integer from 1 to 32")
    if (
        not isinstance(upload_concurrency, int)
        or isinstance(upload_concurrency, bool)
        or not 1 <= upload_concurrency <= 16
    ):
        raise ValueError("upload_concurrency must be an integer from 1 to 16")
    if (
        not isinstance(max_upload_bytes, int)
        or isinstance(max_upload_bytes, bool)
        or not 1 <= max_upload_bytes <= MAX_RAW_WORKBOOK_UPLOAD_BYTES
    ):
        raise ValueError("max_upload_bytes must be within the raw workbook byte limit")
    if (
        not isinstance(upload_read_timeout_seconds, (int, float))
        or isinstance(upload_read_timeout_seconds, bool)
        or not 0.01 <= float(upload_read_timeout_seconds) <= 600.0
    ):
        raise ValueError("upload_read_timeout_seconds must be between 0.01 and 600")
    upload_read_timeout = float(upload_read_timeout_seconds)
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
        from fastapi.responses import JSONResponse, Response
    except ImportError as exc:
        raise WorkbookAssetWebAdapterUnavailableError(
            "FastAPI is optional; install it in the hosting web application."
        ) from exc

    @asynccontextmanager
    async def lifespan(app):
        executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="audit-workbook-asset-web",
        )
        app.state.audit_workbook_asset_executor = executor
        app.state.audit_workbook_asset_executor_gate = asyncio.Semaphore(executor_workers * 2)
        app.state.audit_workbook_asset_upload_gate = asyncio.Semaphore(upload_concurrency)
        try:
            yield
        finally:
            app.state.audit_workbook_asset_executor = None
            app.state.audit_workbook_asset_executor_gate = None
            app.state.audit_workbook_asset_upload_gate = None
            executor.shutdown(wait=True, cancel_futures=True)

    async def offload(request, call):
        executor = getattr(request.app.state, "audit_workbook_asset_executor", None)
        gate = getattr(request.app.state, "audit_workbook_asset_executor_gate", None)
        if not isinstance(executor, ThreadPoolExecutor) or not isinstance(
            gate, asyncio.Semaphore
        ):
            raise RuntimeError("workbook asset executor is outside its application lifespan")
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

    def json_response(response: WorkbookAssetHttpResponse):
        return JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=dict(response.headers),
        )

    async def upload_endpoint(request):
        principal = await resolve_principal(request)
        headers = header_items(request)
        try:
            preflight = _upload_preflight(headers, maximum=max_upload_bytes)
        except _WorkbookAssetTransportError as error:
            return json_response(_transport_error_response(error))
        upload_gate = getattr(request.app.state, "audit_workbook_asset_upload_gate", None)
        if not isinstance(upload_gate, asyncio.Semaphore):
            raise RuntimeError("workbook asset upload gate is unavailable")
        await upload_gate.acquire()
        try:
            try:
                async with asyncio.timeout(upload_read_timeout):
                    content = await _read_bounded_upload(
                        request,
                        maximum=max_upload_bytes,
                        declared_length=preflight.declared_length,
                    )
            except TimeoutError:
                return json_response(
                    _transport_error_response(
                        _WorkbookAssetTransportError(
                            "UPLOAD_TIMEOUT", status_code=408
                        )
                    )
                )
            except _WorkbookAssetTransportError as error:
                return json_response(_transport_error_response(error))
            response = await offload(
                request,
                lambda: adapter.upload(
                    principal=principal,
                    content=content,
                    idempotency_key=preflight.idempotency_key,
                ),
            )
            return json_response(response)
        finally:
            upload_gate.release()

    async def status_endpoint(workbook_id, raw_snapshot_id, request):
        principal = await resolve_principal(request)
        response = await offload(
            request,
            lambda: adapter.get_status(
                principal=principal,
                workbook_id=workbook_id,
                raw_snapshot_id=raw_snapshot_id,
            ),
        )
        return json_response(response)

    async def download_endpoint(workbook_id, raw_snapshot_id, request):
        principal = await resolve_principal(request)
        response = await offload(
            request,
            lambda: adapter.download(
                principal=principal,
                workbook_id=workbook_id,
                raw_snapshot_id=raw_snapshot_id,
            ),
        )
        if isinstance(response, WorkbookAssetHttpResponse):
            return json_response(response)
        return Response(
            status_code=response.status_code,
            content=response.content,
            headers=dict(response.headers),
        )

    upload_endpoint.__annotations__ = {"request": Request}
    resource_annotations = {
        "workbook_id": str,
        "raw_snapshot_id": str,
        "request": Request,
    }
    status_endpoint.__annotations__ = resource_annotations
    download_endpoint.__annotations__ = dict(resource_annotations)

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, error):
        del request, error
        return json_response(
            _fixed_error(
                "INVALID_REQUEST",
                "The workbook asset request is invalid.",
                status_code=400,
            )
        )

    base = "/v1/audit/workbooks"
    app.add_api_route(
        base,
        upload_endpoint,
        methods=["POST"],
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    XLSX_CONTENT_TYPE: {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    resource_path = base + "/{workbook_id}/raw-snapshots/{raw_snapshot_id}"
    app.add_api_route(resource_path, status_endpoint, methods=["GET"])
    app.add_api_route(resource_path + "/download", download_endpoint, methods=["GET"])
    return app


__all__ = [
    "MAX_RAW_WORKBOOK_UPLOAD_BYTES",
    "RAW_WORKBOOK_SNAPSHOT_SCHEMA",
    "WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA",
    "WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA",
    "WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA",
    "WorkbookAssetDownloadHttpResponse",
    "WorkbookAssetHttpAdapter",
    "WorkbookAssetHttpResponse",
    "WorkbookAssetWebAdapterUnavailableError",
    "XLSX_CONTENT_TYPE",
    "create_workbook_asset_fastapi_app",
]
