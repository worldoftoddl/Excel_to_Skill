from __future__ import annotations

import asyncio
import builtins
import jsonschema
import threading

import pytest

from excel_to_skill.audit.service import (
    AuditConversationService,
    BundleSnapshot,
    BundleSnapshotNotFoundError,
    InMemoryConversationArtifactRepository,
    ServicePrincipal,
)
from excel_to_skill.audit.web import (
    AuditConversationHttpAdapter,
    TURN_REQUEST_SCHEMA,
    WebAdapterUnavailableError,
    create_fastapi_app,
)


def test_public_turn_request_schema_matches_whitespace_and_scope_exclusivity() -> None:
    jsonschema.Draft202012Validator.check_schema(TURN_REQUEST_SCHEMA)
    jsonschema.validate(
        {"bundle_id": "bundle-a", "question": "질문", "sheet": "C"},
        TURN_REQUEST_SCHEMA,
    )
    invalid = [
        {"bundle_id": "bundle-a", "question": "   "},
        {"bundle_id": "bundle-a", "question": " 질문"},
        {"bundle_id": "bundle-a", "question": "질문 "},
        {"bundle_id": "bundle-a", "question": "질문\n후속"},
        {"bundle_id": "bundle-a", "question": "질문", "sheet": "A/B"},
        {"bundle_id": "bundle-a", "question": "질문", "sheet": " C"},
        {
            "bundle_id": "bundle-a",
            "question": "질문",
            "sheet": "C",
            "aggregate_id": "a" * 64,
        },
    ]
    for document in invalid:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, TURN_REQUEST_SCHEMA)


class _Bundles:
    def __init__(self, principal, snapshot):
        self.principal = principal
        self.snapshot = snapshot

    def resolve(self, *, principal, bundle_id):
        if principal != self.principal or bundle_id != self.snapshot.bundle_id:
            raise BundleSnapshotNotFoundError(bundle_id)
        return self.snapshot


@pytest.fixture
def web_runtime(tmp_path):
    principal = ServicePrincipal(tenant_id="tenant-a", subject_id="user-a")
    package = tmp_path / "packages" / "snapshot"
    package.mkdir(parents=True)
    snapshot = BundleSnapshot(
        bundle_id="bundle-a",
        snapshot_id="a" * 64,
        package_path=package,
        runtime_root=tmp_path / "runtime" / "snapshot",
    )
    calls = []

    def runner(pkg, **kwargs):
        calls.append((pkg, kwargs))
        return {
            "schema_version": "audit_conversation_turn_result.v1",
            "thread_id": kwargs["thread_id"],
            "turn_index": 1,
            "resumed": False,
            "bundle": {
                "scope": {"kind": "workbook"},
                "workbook_sha256": "1" * 64,
                "prepare_version": "test",
                "facts_key": "2" * 64,
                "standards_key": "3" * 64,
                "brief_key": "4" * 64,
                "prepared_at": "2026-07-14T00:00:00Z",
                "standards_corpus_version": "test-corpus",
            },
            "response": {"schema_version": "audit_agent_response.v2"},
            "usage": {
                "requests": [],
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }

    service = AuditConversationService(
        bundles=_Bundles(principal, snapshot),
        artifacts=InMemoryConversationArtifactRepository(),
        model="test-model",
        runner=runner,
    )
    return principal, snapshot, calls, AuditConversationHttpAdapter(service)


def test_http_adapter_submits_fetches_and_stably_replays(web_runtime):
    principal, snapshot, calls, adapter = web_runtime
    request = {
        "bundle_id": "bundle-a",
        "question": "핵심 위험은?",
        "standards_research": True,
        "workbook_inspection": True,
    }

    created = adapter.submit(
        principal=principal,
        headers={"Idempotency-Key": "web-key"},
        json_body=request,
    )
    replay = adapter.submit(
        principal=principal,
        headers={"idempotency-key": "web-key"},
        json_body=request,
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.headers["Cache-Control"] == "no-store, private"
    assert replay.headers["Cache-Control"] == "no-store, private"
    assert created.body["replayed"] is False
    assert replay.body["replayed"] is True
    assert replay.body["receipt"] == created.body["receipt"]
    assert len(calls) == 1
    receipt = created.body["receipt"]
    assert set(receipt) == {
        "schema_version",
        "request_id",
        "bundle_id",
        "snapshot_id",
        "thread_id",
        "result",
    }
    assert "path" not in str(receipt).lower()
    assert str(snapshot.package_path) not in str(receipt)

    fetched = adapter.fetch(
        principal=principal,
        request_id=receipt["request_id"],
    )
    assert fetched.status_code == 200
    assert fetched.headers["Cache-Control"] == "no-store, private"
    assert fetched.body["receipt"] == receipt


@pytest.mark.parametrize(
    "body",
    [
        {"bundle_id": "bundle-a", "question": "질문", "package_path": "/tmp/a"},
        {"bundle_id": "/tmp/package", "question": "질문"},
        {"bundle_id": "bundle-a", "question": "질문", "runtime_root": "/tmp/r"},
        {"bundle_id": "bundle-a", "question": "질문", "model": "other"},
        {"bundle_id": "bundle-a", "question": "질문", "standards_research": 1},
        {"bundle_id": "bundle-a", "question": "질문", "workbook_inspection": 1},
        {"bundle_id": "bundle-a", "question": " 질문"},
        {"bundle_id": "bundle-a", "question": "질문", "sheet": "A/B"},
    ],
)
def test_http_adapter_rejects_paths_unknown_fields_and_non_strict_types(
    web_runtime,
    body,
):
    principal, _, calls, adapter = web_runtime

    response = adapter.submit(
        principal=principal,
        headers={"Idempotency-Key": "web-key"},
        json_body=body,
    )

    assert response.status_code == 400
    assert response.body["error"]["code"] == "INVALID_REQUEST"
    assert calls == []


def test_http_adapter_requires_exactly_one_idempotency_header(web_runtime):
    principal, _, calls, adapter = web_runtime
    body = {"bundle_id": "bundle-a", "question": "질문"}

    missing = adapter.submit(principal=principal, headers={}, json_body=body)
    duplicate = adapter.submit(
        principal=principal,
        headers={"Idempotency-Key": "one", "idempotency-key": "two"},
        json_body=body,
    )

    assert missing.status_code == 400
    assert duplicate.status_code == 400
    assert missing.body["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
    assert duplicate.body["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
    assert calls == []


def test_http_adapter_returns_stable_idempotency_conflict(web_runtime):
    principal, _, calls, adapter = web_runtime
    first = adapter.submit(
        principal=principal,
        headers={"Idempotency-Key": "web-key"},
        json_body={"bundle_id": "bundle-a", "question": "질문 1"},
    )
    conflict = adapter.submit(
        principal=principal,
        headers={"Idempotency-Key": "web-key"},
        json_body={"bundle_id": "bundle-a", "question": "질문 2"},
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.body == {
        "schema_version": "audit_conversation_http_error.v1",
        "error": {
            "code": "IDEMPOTENCY_CONFLICT",
            "message": "Idempotency-Key was already used for a different request.",
        },
    }
    assert len(calls) == 1


def test_fastapi_import_is_lazy_and_reports_optional_dependency(
    web_runtime,
    monkeypatch,
):
    _, _, _, adapter = web_runtime
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "fastapi" or name.startswith("fastapi."):
            raise ImportError("blocked for test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(WebAdapterUnavailableError, match="FastAPI is optional"):
        create_fastapi_app(
            adapter,
            principal_dependency=lambda: ServicePrincipal("tenant-a", "user-a"),
        )


def test_fastapi_app_runs_the_strict_post_and_get_contract(web_runtime):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")

    principal, _, calls, adapter = web_runtime
    app = create_fastapi_app(adapter, principal_dependency=lambda: principal)
    openapi_model = app.openapi()["components"]["schemas"]["TurnRequestModel"]
    assert openapi_model["allOf"] == TURN_REQUEST_SCHEMA["allOf"]
    assert (
        openapi_model["properties"]["question"]["pattern"]
        == TURN_REQUEST_SCHEMA["properties"]["question"]["pattern"]
    )
    assert (
        openapi_model["properties"]["sheet"]["anyOf"][0]["pattern"]
        == TURN_REQUEST_SCHEMA["properties"]["sheet"]["pattern"]
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            executor = app.state.audit_executor
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                created = await client.post(
                    "/v1/audit/conversation-turns",
                    headers={"Idempotency-Key": "fastapi-key"},
                    json={
                        "bundle_id": "bundle-a",
                        "question": "선택 범위를 분석해줘",
                        "workbook_inspection": True,
                    },
                )

                assert created.status_code == 201
                assert created.headers["cache-control"] == "no-store, private"
                receipt = created.json()["receipt"]
                assert len(calls) == 1
                fetched = await client.get(
                    f"/v1/audit/conversation-turns/{receipt['request_id']}"
                )
                assert fetched.status_code == 200
                assert fetched.headers["cache-control"] == "no-store, private"
                assert fetched.json()["receipt"] == receipt
        assert app.state.audit_executor is None
        assert app.state.audit_executor_gate is None
        assert executor._shutdown is True

    asyncio.run(scenario())


def test_fastapi_app_preserves_duplicate_idempotency_headers_and_normalizes_validation(
    web_runtime,
):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    principal, _, calls, adapter = web_runtime
    app = create_fastapi_app(adapter, principal_dependency=lambda: principal)

    expected_error = {
        "schema_version": "audit_conversation_http_error.v1",
        "error": {
            "code": "INVALID_REQUEST",
            "message": "The JSON request does not match the turn contract.",
        },
    }

    async def scenario():
        async with app.router.lifespan_context(app):
            executor = app.state.audit_executor
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                duplicate = await client.post(
                    "/v1/audit/conversation-turns",
                    headers=[
                        ("Idempotency-Key", "one"),
                        ("Idempotency-Key", "two"),
                    ],
                    json={"bundle_id": "bundle-a", "question": "질문"},
                )
                assert duplicate.status_code == 400
                assert duplicate.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"

                unknown = await client.post(
                    "/v1/audit/conversation-turns",
                    headers={"Idempotency-Key": "unknown-field"},
                    json={
                        "bundle_id": "bundle-a",
                        "question": "질문",
                        "package_path": "/tmp/private",
                    },
                )
                assert unknown.status_code == 400
                assert unknown.json() == expected_error
                assert unknown.headers["cache-control"] == "no-store, private"

                malformed = await client.post(
                    "/v1/audit/conversation-turns",
                    headers={
                        "Idempotency-Key": "malformed",
                        "Content-Type": "application/json",
                    },
                    content=b'{"bundle_id":',
                )
                assert malformed.status_code == 400
                assert malformed.json() == expected_error
                assert calls == []
        assert app.state.audit_executor is None
        assert executor._shutdown is True

    asyncio.run(scenario())


def test_fastapi_cancelled_request_retains_worker_slot_until_call_finishes(web_runtime):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    principal, _, calls, adapter = web_runtime
    started = threading.Event()
    release = threading.Event()

    def slow_principal():
        started.set()
        release.wait(timeout=5)
        return principal

    app = create_fastapi_app(
        adapter,
        principal_dependency=slow_principal,
        executor_workers=1,
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                request = asyncio.create_task(
                    client.post(
                        "/v1/audit/conversation-turns",
                        headers={"Idempotency-Key": "cancelled"},
                        json={"bundle_id": "bundle-a", "question": "질문"},
                    )
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()

                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                await asyncio.sleep(0)

                gate = app.state.audit_executor_gate
                assert gate._value == 1
                assert calls == []

                release.set()
                for _ in range(100):
                    if gate._value == 2:
                        break
                    await asyncio.sleep(0.01)
                assert gate._value == 2

    try:
        asyncio.run(scenario())
    finally:
        release.set()
