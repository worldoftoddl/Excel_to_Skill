from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import BytesIO

import jsonschema
import openpyxl
import pytest
from referencing import Registry, Resource

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_asset_service import (
    InMemoryWorkbookAssetRepository,
    WorkbookAssetService,
)
from excel_to_skill.audit.workbook_asset_web import (
    RAW_WORKBOOK_SNAPSHOT_SCHEMA,
    WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA,
    WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA,
    WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA,
    XLSX_CONTENT_TYPE,
    WorkbookAssetHttpAdapter,
    WorkbookAssetHttpResponse,
    create_workbook_asset_fastapi_app,
)
from excel_to_skill.audit.workbook_snapshot_publication import (
    LocalImmutableWorkbookAssetStore,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
OTHER = ServicePrincipal("tenant-a", "user-b")


def _xlsx(value: str = "원본") -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "감사조서"
    worksheet["A1"] = value
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


@pytest.fixture
def asset_web(tmp_path):
    assets = LocalImmutableWorkbookAssetStore(tmp_path / "assets")
    repository = InMemoryWorkbookAssetRepository(now=lambda: NOW)
    service = WorkbookAssetService(
        repository=repository,
        assets=assets,
        now=lambda: NOW,
    )
    return service, WorkbookAssetHttpAdapter(service), tmp_path / "assets"


def _assert_schema(document: object, schema: dict[str, object]) -> None:
    registry = Registry().with_resource(
        "audit_raw_workbook_snapshot.schema.json",
        Resource.from_contents(RAW_WORKBOOK_SNAPSHOT_SCHEMA),
    )
    jsonschema.Draft202012Validator(schema, registry=registry).validate(document)


def test_response_schemas_are_closed_and_reference_the_exact_snapshot() -> None:
    for schema in (
        RAW_WORKBOOK_SNAPSHOT_SCHEMA,
        WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA,
        WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA,
        WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA,
    ):
        jsonschema.Draft202012Validator.check_schema(schema)

    assert RAW_WORKBOOK_SNAPSHOT_SCHEMA["additionalProperties"] is False
    assert WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA["additionalProperties"] is False
    assert WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA["additionalProperties"] is False
    assert WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA["additionalProperties"] is False


def test_adapter_upload_replays_fetches_and_downloads_exact_private_asset(asset_web) -> None:
    _, adapter, _ = asset_web
    content = _xlsx()

    created = adapter.upload(
        principal=PRINCIPAL,
        content=content,
        idempotency_key="upload-action-a",
    )
    replayed = adapter.upload(
        principal=PRINCIPAL,
        content=content,
        idempotency_key="upload-action-a",
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert created.body["replayed"] is False
    assert replayed.body["replayed"] is True
    assert replayed.body["snapshot"] == created.body["snapshot"]
    snapshot = created.body["snapshot"]
    _assert_schema(snapshot, RAW_WORKBOOK_SNAPSHOT_SCHEMA)
    _assert_schema(created.body, WORKBOOK_ASSET_SUBMISSION_RESPONSE_SCHEMA)
    assert created.headers["Location"].endswith(
        f"/{snapshot['workbook_id']}/raw-snapshots/{snapshot['raw_snapshot_id']}"
    )
    assert created.headers["Idempotency-Replayed"] == "false"
    assert replayed.headers["Idempotency-Replayed"] == "true"

    status = adapter.get_status(
        principal=PRINCIPAL,
        workbook_id=snapshot["workbook_id"],
        raw_snapshot_id=snapshot["raw_snapshot_id"],
    )
    download = adapter.download(
        principal=PRINCIPAL,
        workbook_id=snapshot["workbook_id"],
        raw_snapshot_id=snapshot["raw_snapshot_id"],
    )
    assert status.status_code == 200
    assert status.body["snapshot"] == snapshot
    _assert_schema(status.body, WORKBOOK_ASSET_STATUS_RESPONSE_SCHEMA)
    assert not isinstance(download, WorkbookAssetHttpResponse)
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["Content-Type"] == XLSX_CONTENT_TYPE
    assert download.headers["Content-Length"] == str(len(content))
    assert download.headers["ETag"] == f'"{snapshot["workbook_sha256"]}"'
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    assert download.headers["Content-Disposition"].startswith(
        'attachment; filename="workbook-'
    )

    public = json.dumps(
        {"created": created.body, "status": status.body, "headers": dict(download.headers)},
        ensure_ascii=False,
    ).lower()
    for forbidden in ("asset_ref", "path", "provider", "tenant_id", "subject_id"):
        assert forbidden not in public


def test_adapter_conflict_and_cross_principal_reads_are_fixed_safe_errors(asset_web) -> None:
    _, adapter, _ = asset_web
    created = adapter.upload(
        principal=PRINCIPAL,
        content=_xlsx("첫 파일"),
        idempotency_key="upload-conflict",
    )
    conflict = adapter.upload(
        principal=PRINCIPAL,
        content=_xlsx("다른 파일"),
        idempotency_key="upload-conflict",
    )
    snapshot = created.body["snapshot"]
    hidden_status = adapter.get_status(
        principal=OTHER,
        workbook_id=snapshot["workbook_id"],
        raw_snapshot_id=snapshot["raw_snapshot_id"],
    )
    hidden_download = adapter.download(
        principal=OTHER,
        workbook_id=snapshot["workbook_id"],
        raw_snapshot_id=snapshot["raw_snapshot_id"],
    )

    assert conflict.status_code == 409
    assert conflict.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert hidden_status.status_code == 404
    assert hidden_download.status_code == 404
    assert hidden_status.body == hidden_download.body
    assert hidden_status.body["error"]["code"] == "WORKBOOK_NOT_FOUND"
    _assert_schema(hidden_status.body, WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA)


def test_adapter_maps_invalid_xlsx_to_a_fixed_unprocessable_error(asset_web) -> None:
    _, adapter, _ = asset_web

    response = adapter.upload(
        principal=PRINCIPAL,
        content=b"not-an-xlsx",
        idempotency_key="invalid-workbook",
    )

    assert response.status_code == 422
    assert response.body["error"]["code"] == "INVALID_WORKBOOK"
    assert "zip" not in response.body["error"]["message"].lower()
    _assert_schema(response.body, WORKBOOK_ASSET_ERROR_RESPONSE_SCHEMA)


def test_corrupt_immutable_object_fails_before_download_bytes_are_returned(asset_web) -> None:
    _, adapter, assets_root = asset_web
    created = adapter.upload(
        principal=PRINCIPAL,
        content=_xlsx(),
        idempotency_key="upload-corrupt",
    )
    snapshot = created.body["snapshot"]
    object_path = assets_root / "objects" / f"{snapshot['workbook_sha256']}.xlsx"
    object_path.write_bytes(b"corrupt")

    response = adapter.download(
        principal=PRINCIPAL,
        workbook_id=snapshot["workbook_id"],
        raw_snapshot_id=snapshot["raw_snapshot_id"],
    )

    assert isinstance(response, WorkbookAssetHttpResponse)
    assert response.status_code == 503
    assert response.body["error"]["code"] == "ASSET_INTEGRITY_MISMATCH"
    assert str(object_path) not in json.dumps(response.body)


def _asgi_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/audit/workbooks",
        "raw_path": b"/v1/audit/workbooks",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("test", 443),
    }


def test_fastapi_authentication_rejection_never_reads_upload_body(asset_web) -> None:
    fastapi = pytest.importorskip("fastapi")
    _, adapter, _ = asset_web
    receive_calls = 0

    async def reject_principal(_request):
        raise fastapi.HTTPException(status_code=401, detail="unauthorized")

    app = create_workbook_asset_fastapi_app(
        adapter,
        principal_dependency=reject_principal,
    )

    async def scenario():
        nonlocal receive_calls
        sent = []

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return {"type": "http.request", "body": _xlsx(), "more_body": False}

        async def send(message):
            sent.append(message)

        scope = _asgi_scope(
            [
                (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
                (b"idempotency-key", b"auth-reject"),
            ]
        )
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        return sent

    messages = asyncio.run(scenario())
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert starts[0]["status"] == 401
    assert receive_calls == 0


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        (
            [(b"content-type", XLSX_CONTENT_TYPE.encode("ascii"))],
            400,
            "MISSING_IDEMPOTENCY_KEY",
        ),
        (
            [(b"content-type", b"application/octet-stream"), (b"idempotency-key", b"k")],
            415,
            "UNSUPPORTED_MEDIA_TYPE",
        ),
        (
            [
                (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
                (b"content-encoding", b"gzip"),
                (b"idempotency-key", b"k"),
            ],
            415,
            "UNSUPPORTED_CONTENT_ENCODING",
        ),
        (
            [
                (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
                (b"idempotency-key", b"k"),
                (b"content-length", b"12"),
                (b"content-length", b"12"),
            ],
            400,
            "INVALID_REQUEST",
        ),
        (
            [
                (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
                (b"idempotency-key", b"k"),
                (b"content-length", b"not-a-number"),
            ],
            400,
            "INVALID_REQUEST",
        ),
        (
            [
                (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
                (b"idempotency-key", b"k"),
                (b"content-length", b"1025"),
            ],
            413,
            "SOURCE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_fastapi_header_preflight_rejects_without_reading_body(
    asset_web,
    headers,
    expected_status,
    expected_code,
) -> None:
    _, adapter, _ = asset_web
    receive_calls = 0

    async def principal(_request):
        return PRINCIPAL

    app = create_workbook_asset_fastapi_app(
        adapter,
        principal_dependency=principal,
        max_upload_bytes=1024,
    )

    async def scenario():
        nonlocal receive_calls
        sent = []

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return {"type": "http.request", "body": b"attacker", "more_body": False}

        async def send(message):
            sent.append(message)

        async with app.router.lifespan_context(app):
            await app(_asgi_scope(headers), receive, send)
        return sent

    messages = asyncio.run(scenario())
    starts = [message for message in messages if message["type"] == "http.response.start"]
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert starts[0]["status"] == expected_status
    assert json.loads(bodies[-1]["body"])["error"]["code"] == expected_code
    assert receive_calls == 0


def test_fastapi_chunked_overflow_stops_immediately_and_never_calls_service(asset_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    _, adapter, assets_root = asset_web
    app = create_workbook_asset_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
        max_upload_bytes=10,
    )
    streamed: list[int] = []

    async def chunks():
        for index in range(3):
            streamed.append(index)
            yield b"x" * 6

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/v1/audit/workbooks",
                    headers={
                        "Content-Type": XLSX_CONTENT_TYPE,
                        "Idempotency-Key": "overflow-action",
                    },
                    content=chunks(),
                )

    response = asyncio.run(scenario())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "SOURCE_LIMIT_EXCEEDED"
    assert streamed == [0, 1]
    assert list((assets_root / "objects").iterdir()) == []


def test_fastapi_declared_length_mismatch_reads_once_but_does_not_store(asset_web) -> None:
    _, adapter, _ = asset_web
    receive_calls = 0

    async def principal(_request):
        return PRINCIPAL

    app = create_workbook_asset_fastapi_app(
        adapter,
        principal_dependency=principal,
        max_upload_bytes=1024,
    )

    async def scenario():
        nonlocal receive_calls
        sent = []
        delivered = False

        async def receive():
            nonlocal receive_calls, delivered
            receive_calls += 1
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": b"short", "more_body": False}

        async def send(message):
            sent.append(message)

        headers = [
            (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
            (b"idempotency-key", b"length-mismatch"),
            (b"content-length", b"6"),
        ]
        async with app.router.lifespan_context(app):
            await app(_asgi_scope(headers), receive, send)
        return sent

    messages = asyncio.run(scenario())
    starts = [message for message in messages if message["type"] == "http.response.start"]
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert starts[0]["status"] == 400
    assert json.loads(bodies[-1]["body"])["error"]["code"] == "INVALID_REQUEST"
    assert receive_calls == 1


def test_fastapi_upload_deadline_releases_the_global_admission_slot(asset_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    _, adapter, _ = asset_web
    receive_started = asyncio.Event()

    app = create_workbook_asset_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
        upload_concurrency=1,
        upload_read_timeout_seconds=0.05,
    )

    async def scenario():
        sent = []

        async def stalled_receive():
            receive_started.set()
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        headers = [
            (b"content-type", XLSX_CONTENT_TYPE.encode("ascii")),
            (b"idempotency-key", b"stalled-upload"),
        ]
        async with app.router.lifespan_context(app):
            stalled = asyncio.create_task(
                app(_asgi_scope(headers), stalled_receive, send)
            )
            await asyncio.wait_for(receive_started.wait(), timeout=1)
            await asyncio.wait_for(stalled, timeout=1)
            starts = [
                message for message in sent if message["type"] == "http.response.start"
            ]
            bodies = [
                message for message in sent if message["type"] == "http.response.body"
            ]
            assert starts[0]["status"] == 408
            assert json.loads(bodies[-1]["body"])["error"]["code"] == "UPLOAD_TIMEOUT"

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                normal = await client.post(
                    "/v1/audit/workbooks",
                    headers={
                        "Content-Type": XLSX_CONTENT_TYPE,
                        "Idempotency-Key": "after-timeout",
                    },
                    content=_xlsx(),
                )
            assert normal.status_code == 201

    asyncio.run(scenario())


def test_fastapi_upload_status_download_and_cross_principal_hiding(asset_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    _, adapter, _ = asset_web
    current = {"principal": PRINCIPAL}

    async def principal(_request):
        return current["principal"]

    app = create_workbook_asset_fastapi_app(adapter, principal_dependency=principal)
    content = _xlsx()

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                uploaded = await client.post(
                    "/v1/audit/workbooks",
                    headers={
                        "Content-Type": XLSX_CONTENT_TYPE,
                        "Idempotency-Key": "asgi-upload",
                    },
                    content=content,
                )
                snapshot = uploaded.json()["snapshot"]
                resource = (
                    f"/v1/audit/workbooks/{snapshot['workbook_id']}"
                    f"/raw-snapshots/{snapshot['raw_snapshot_id']}"
                )
                status = await client.get(resource)
                download = await client.get(resource + "/download")
                current["principal"] = OTHER
                hidden_status = await client.get(resource)
                hidden_download = await client.get(resource + "/download")
                return uploaded, status, download, hidden_status, hidden_download

    uploaded, status, download, hidden_status, hidden_download = asyncio.run(scenario())
    assert uploaded.status_code == 201
    assert status.status_code == 200
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-type"] == XLSX_CONTENT_TYPE
    assert download.headers["cache-control"] == "no-store, private"
    assert hidden_status.status_code == hidden_download.status_code == 404
    assert hidden_status.json()["error"]["code"] == "WORKBOOK_NOT_FOUND"
    assert hidden_download.json()["error"]["code"] == "WORKBOOK_NOT_FOUND"
