"""Framework-neutral HTTP contract and an optional lazy FastAPI adapter."""
from __future__ import annotations

import asyncio
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Mapping

from ..resources import SCHEMA_DIR
from .service import (
    AuditConversationService,
    AuditConversationServiceError,
    ConversationTurnCommand,
    ServicePrincipal,
)


def _load_turn_request_schema() -> dict[str, object]:
    try:
        value = json.loads(
            (SCHEMA_DIR / "audit_conversation_http_turn_request.schema.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("audit conversation HTTP request schema is unavailable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("audit conversation HTTP request schema must be an object")
    return value


TURN_REQUEST_SCHEMA: dict[str, object] = _load_turn_request_schema()

_REQUEST_FIELDS = frozenset(TURN_REQUEST_SCHEMA["properties"])
_REQUEST_REQUIRED = frozenset(TURN_REQUEST_SCHEMA["required"])
_QUESTION_PATTERN = TURN_REQUEST_SCHEMA["properties"]["question"]["pattern"]
_SHEET_PATTERN = TURN_REQUEST_SCHEMA["properties"]["sheet"]["pattern"]
_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
}


class WebAdapterUnavailableError(RuntimeError):
    """The optional web framework is not installed or could not be initialized."""


@dataclass(frozen=True)
class HttpResponse:
    """Small HTTP-shaped value that can be tested without a web dependency."""

    status_code: int
    body: dict[str, object]
    headers: Mapping[str, str]


def _error_response(error: AuditConversationServiceError) -> HttpResponse:
    return HttpResponse(
        status_code=error.status_code,
        body={
            "schema_version": "audit_conversation_http_error.v1",
            "error": {"code": error.code, "message": error.message},
        },
        headers=dict(_JSON_HEADERS),
    )


def _invalid_request(message: str) -> AuditConversationServiceError:
    return AuditConversationServiceError(
        "INVALID_REQUEST",
        message,
        status_code=400,
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
        raise AuditConversationServiceError(
            "MISSING_IDEMPOTENCY_KEY",
            "Exactly one Idempotency-Key header is required.",
            status_code=400,
        )
    values = [
        value
        for name, value in items
        if isinstance(name, str) and name.lower() == "idempotency-key"
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise AuditConversationServiceError(
            "MISSING_IDEMPOTENCY_KEY",
            "Exactly one Idempotency-Key header is required.",
            status_code=400,
        )
    return values[0]


def _command(document: object) -> ConversationTurnCommand:
    if not isinstance(document, dict):
        raise _invalid_request("The JSON request body must be an object.")
    fields = set(document)
    if not all(isinstance(field, str) for field in fields):
        raise _invalid_request("The JSON request contains an invalid field name.")
    unknown = fields - _REQUEST_FIELDS
    if unknown:
        raise _invalid_request("The JSON request contains unsupported fields.")
    if not _REQUEST_REQUIRED.issubset(fields):
        raise _invalid_request("bundle_id and question are required.")
    try:
        return ConversationTurnCommand(
            bundle_id=document.get("bundle_id"),
            question=document.get("question"),
            thread_id=document.get("thread_id"),
            sheet=document.get("sheet"),
            aggregate_id=document.get("aggregate_id"),
            standards_research=document.get("standards_research", False),
            procedure_planning=document.get("procedure_planning", False),
            workbook_inspection=document.get("workbook_inspection", False),
        )
    except (TypeError, ValueError) as exc:
        raise _invalid_request("The JSON request does not match the turn contract.") from exc


class AuditConversationHttpAdapter:
    """Synchronous web adapter; authentication and tenant resolution stay with the host."""

    def __init__(self, service: AuditConversationService) -> None:
        if not isinstance(service, AuditConversationService):
            raise TypeError("service must be an AuditConversationService")
        self._service = service

    def submit(
        self,
        *,
        principal: ServicePrincipal,
        headers: (
            Mapping[str, object]
            | list[tuple[object, object]]
            | tuple[tuple[object, object], ...]
        ),
        json_body: object,
    ) -> HttpResponse:
        try:
            command = _command(json_body)
            idempotency_key = _idempotency_header(headers)
            submission = self._service.submit_turn(
                principal=principal,
                command=command,
                idempotency_key=idempotency_key,
            )
        except AuditConversationServiceError as error:
            return _error_response(error)
        receipt = submission.receipt.to_dict()
        status_code = 200 if submission.replayed else 201
        return HttpResponse(
            status_code=status_code,
            body={
                "schema_version": "audit_conversation_http_submission.v1",
                "replayed": submission.replayed,
                "receipt": receipt,
            },
            headers={
                **_JSON_HEADERS,
                "Location": f"/v1/audit/conversation-turns/{submission.receipt.request_id}",
                "Idempotency-Replayed": "true" if submission.replayed else "false",
            },
        )

    def fetch(
        self,
        *,
        principal: ServicePrincipal,
        request_id: str,
    ) -> HttpResponse:
        try:
            receipt = self._service.get_turn(
                principal=principal,
                request_id=request_id,
            )
        except AuditConversationServiceError as error:
            return _error_response(error)
        return HttpResponse(
            status_code=200,
            body={
                "schema_version": "audit_conversation_http_receipt.v1",
                "receipt": receipt.to_dict(),
            },
            headers=dict(_JSON_HEADERS),
        )


def create_fastapi_app(
    adapter: AuditConversationHttpAdapter,
    *,
    principal_dependency,
    executor_workers: int = 4,
):
    """Create a FastAPI app without importing FastAPI at module import time.

    ``principal_dependency`` belongs to the hosting application and must return a validated
    :class:`ServicePrincipal`.  This package intentionally does not invent authentication,
    cookies, bearer-token parsing, or tenant lookup.
    """
    if not isinstance(adapter, AuditConversationHttpAdapter):
        raise TypeError("adapter must be an AuditConversationHttpAdapter")
    if not callable(principal_dependency):
        raise TypeError("principal_dependency must be callable")
    if (
        not isinstance(executor_workers, int)
        or isinstance(executor_workers, bool)
        or not 1 <= executor_workers <= 32
    ):
        raise ValueError("executor_workers must be an integer from 1 to 32")
    try:
        principal_parameters = tuple(inspect.signature(principal_dependency).parameters.values())
    except (TypeError, ValueError) as exc:
        raise TypeError("principal_dependency must have an inspectable signature") from exc
    if len(principal_parameters) > 1 or any(
        parameter.kind
        not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in principal_parameters
    ):
        raise TypeError("principal_dependency must accept zero arguments or one request")
    principal_accepts_request = len(principal_parameters) == 1
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from pydantic import (
            BaseModel,
            ConfigDict,
            Field,
            StrictBool,
            StrictStr,
            model_validator,
        )
    except ImportError as exc:
        raise WebAdapterUnavailableError(
            "FastAPI is optional; install it in the hosting web application."
        ) from exc

    class TurnRequestModel(BaseModel):
        model_config = ConfigDict(
            extra="forbid",
            strict=True,
            json_schema_extra={"allOf": TURN_REQUEST_SCHEMA["allOf"]},
        )

        bundle_id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
        question: StrictStr = Field(
            min_length=1,
            max_length=12_000,
            pattern=_QUESTION_PATTERN,
        )
        thread_id: StrictStr | None = Field(
            default=None,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
        )
        sheet: StrictStr | None = Field(
            default=None,
            min_length=1,
            max_length=31,
            pattern=_SHEET_PATTERN,
        )
        aggregate_id: StrictStr | None = Field(
            default=None,
            pattern=r"^[0-9a-f]{64}$",
        )
        standards_research: StrictBool = False
        procedure_planning: StrictBool = False
        workbook_inspection: StrictBool = False

        @model_validator(mode="after")
        def validate_scope(self):
            if self.sheet is not None and self.aggregate_id is not None:
                raise ValueError("sheet and aggregate_id are mutually exclusive")
            return self

    @asynccontextmanager
    async def lifespan(app):
        executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="audit-web",
        )
        app.state.audit_executor = executor
        app.state.audit_executor_gate = asyncio.Semaphore(executor_workers * 2)
        try:
            yield
        finally:
            app.state.audit_executor = None
            app.state.audit_executor_gate = None
            executor.shutdown(wait=True, cancel_futures=True)

    async def offload(request, call):
        executor = getattr(request.app.state, "audit_executor", None)
        gate = getattr(request.app.state, "audit_executor_gate", None)
        if not isinstance(executor, ThreadPoolExecutor) or not isinstance(
            gate, asyncio.Semaphore
        ):
            raise RuntimeError("audit web executor is outside its application lifespan")
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
                # Application shutdown may close the loop after executor shutdown.  The gate is
                # then unreachable, so there is no live request slot to return.
                pass

        future.add_done_callback(release_gate)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(wrapped),
                        timeout=0.05,
                    )
                except TimeoutError:
                    # Some embedded ASGI runners can miss the worker's wake-up signal.
                    # The bounded timer keeps the event loop live and lets us consume the
                    # already-completed concurrent future without an AnyIO worker.
                    if future.done():
                        return future.result()
        except asyncio.CancelledError:
            # A queued call can be cancelled immediately.  A running call retains its gate slot
            # until its completion callback fires, preventing cancelled clients from creating an
            # unbounded executor backlog.
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

    async def submit_endpoint(body, request):
        principal = await resolve_principal(request)
        raw_headers = request.scope.get("headers", ())
        header_items = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in raw_headers
            if isinstance(name, bytes) and isinstance(value, bytes)
        ]
        request_body = body.model_dump(mode="json")
        response = await offload(
            request,
            lambda: adapter.submit(
                principal=principal,
                headers=header_items,
                json_body=request_body,
            ),
        )
        return JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=dict(response.headers),
        )

    submit_endpoint.__annotations__ = {
        "body": TurnRequestModel,
        "request": Request,
    }

    async def fetch_endpoint(request_id, request):
        principal = await resolve_principal(request)
        response = await offload(
            request,
            lambda: adapter.fetch(principal=principal, request_id=request_id)
        )
        return JSONResponse(
            status_code=response.status_code,
            content=response.body,
            headers=dict(response.headers),
        )

    fetch_endpoint.__annotations__ = {"request_id": str, "request": Request}

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request, error):
        del request, error
        return JSONResponse(
            status_code=400,
            content={
                "schema_version": "audit_conversation_http_error.v1",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "The JSON request does not match the turn contract.",
                },
            },
            headers=dict(_JSON_HEADERS),
        )

    app.add_api_route(
        "/v1/audit/conversation-turns",
        submit_endpoint,
        methods=["POST"],
        status_code=201,
    )
    app.add_api_route(
        "/v1/audit/conversation-turns/{request_id}",
        fetch_endpoint,
        methods=["GET"],
    )
    return app


__all__ = [
    "AuditConversationHttpAdapter",
    "HttpResponse",
    "TURN_REQUEST_SCHEMA",
    "WebAdapterUnavailableError",
    "create_fastapi_app",
]
