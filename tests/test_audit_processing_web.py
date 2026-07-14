from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass

import jsonschema
import pytest
from referencing import Registry, Resource

from excel_to_skill.audit.model import json_sha256
from excel_to_skill.audit.processing import ProcessingServiceError
from excel_to_skill.audit.processing_web import (
    PROCESSING_HTTP_ERROR_SCHEMA,
    PROCESSING_JOB_SCHEMA,
    PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA,
    PROCESSING_STATUS_SCHEMA,
    PROCESSING_SUBMISSION_SCHEMA,
    AuditProcessingHttpAdapter,
    create_processing_fastapi_app,
)
from excel_to_skill.audit.service import ServicePrincipal


PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
OTHER = ServicePrincipal("tenant-a", "user-b")
WORKBOOK_ID = "workbook-" + "a" * 48
RAW_SNAPSHOT_ID = "b" * 64
WORKBOOK_SHA256 = "c" * 64
JOB_ID = "process-" + "d" * 48
SCOPE_ID = "f" * 64


def _scope_plan() -> dict[str, object]:
    calls = {"facts": 1, "brief": 1, "total_llm": 2}
    return {
        "schema_version": "audit_scope_plan.v1",
        "workbook": {
            "scope": {"kind": "workbook"},
            "sheet_count": 1,
            "cell_count": 12,
            "region_count": 2,
            "analyzable": True,
            "estimated_calls": dict(calls),
        },
        "all_sheets": {
            "sheet_count": 1,
            "total_sheet_count": 1,
            "skipped_empty_sheet_count": 0,
            "cell_count": 12,
            "region_count": 2,
            "selectable": True,
            "selection_limit": 64,
            "estimated_calls": dict(calls),
        },
        "sheets": [
            {
                "scope": {"kind": "sheet", "sheet": "C", "id": SCOPE_ID},
                "dimensions": "A1:C4",
                "cell_count": 12,
                "region_count": 2,
                "analyzable": True,
                "dependency_sheets": [],
                "estimated_calls": dict(calls),
            }
        ],
    }


PLAN_SHA256 = json_sha256(_scope_plan())


def _job_document(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "audit_processing_job.v1",
        "job_id": JOB_ID,
        "workbook_id": WORKBOOK_ID,
        "raw_snapshot_id": RAW_SNAPSHOT_ID,
        "workbook_sha256": WORKBOOK_SHA256,
        "status": "awaiting_scope",
        "scope_plan_sha256": PLAN_SHA256,
        "scope_plan": _scope_plan(),
        "selection": None,
        "progress": {
            "total_scopes": 0,
            "completed_scopes": 0,
            "current_scope_ids": [],
        },
        "result": None,
        "failure": None,
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
    }
    value.update(updates)
    return value


@dataclass
class _Job:
    document: dict[str, object]

    def to_public_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.document)


@dataclass
class _Submission:
    job: object
    replayed: bool


class _FakeProcessingService:
    def __init__(self) -> None:
        self.start_commands: list[dict[str, object]] = []
        self.selection_commands: list[dict[str, object]] = []
        self.status_commands: list[dict[str, object]] = []
        self._start_keys: set[str] = set()
        self._selection_keys: set[str] = set()
        self.worker_threads: list[str] = []
        self.malformed = False
        self.job_updates: dict[str, object] = {}

    def _job(self) -> _Job:
        document = _job_document()
        if self.malformed:
            document["package_path"] = "/private/workspace/package"
        document.update(copy.deepcopy(self.job_updates))
        return _Job(document)

    def start(self, **kwargs: object) -> _Submission:
        self.worker_threads.append(threading.current_thread().name)
        self.start_commands.append(dict(kwargs))
        key = kwargs["idempotency_key"]
        assert isinstance(key, str)
        replayed = key in self._start_keys
        self._start_keys.add(key)
        return _Submission(self._job(), replayed)

    def select_scope(self, **kwargs: object) -> _Submission:
        self.worker_threads.append(threading.current_thread().name)
        self.selection_commands.append(dict(kwargs))
        key = kwargs["idempotency_key"]
        assert isinstance(key, str)
        replayed = key in self._selection_keys
        self._selection_keys.add(key)
        return _Submission(self._job(), replayed)

    def get_job(self, **kwargs: object) -> _Job:
        self.worker_threads.append(threading.current_thread().name)
        self.status_commands.append(dict(kwargs))
        if kwargs.get("principal") == OTHER:
            raise ProcessingServiceError("JOB_NOT_FOUND")
        return self._job()


@pytest.fixture
def processing_web():
    service = _FakeProcessingService()
    return service, AuditProcessingHttpAdapter(service)


def _schema_registry() -> Registry:
    return Registry().with_resource(
        "audit_processing_job.schema.json",
        Resource.from_contents(PROCESSING_JOB_SCHEMA),
    )


def _assert_schema(document: object, schema: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(schema, registry=_schema_registry()).validate(document)


def test_processing_schemas_are_closed_and_scope_request_is_exact() -> None:
    for schema in (
        PROCESSING_JOB_SCHEMA,
        PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA,
        PROCESSING_SUBMISSION_SCHEMA,
        PROCESSING_STATUS_SCHEMA,
        PROCESSING_HTTP_ERROR_SCHEMA,
    ):
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False

    valid = {
        "mode": "selected_sheets",
        "scope_plan_sha256": PLAN_SHA256,
        "scope_ids": [SCOPE_ID],
    }
    _assert_schema(valid, PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        _assert_schema({**valid, "sheet": "C"}, PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        _assert_schema({**valid, "scope_ids": []}, PROCESSING_SCOPE_SELECTION_REQUEST_SCHEMA)


def test_adapter_accepts_strict_duck_service_and_requires_all_callables() -> None:
    class DuckService(_FakeProcessingService):
        pass

    AuditProcessingHttpAdapter(DuckService())
    with pytest.raises(TypeError, match="start, select_scope, and get_job"):
        AuditProcessingHttpAdapter(object())


def test_adapter_starts_replays_selects_and_reads_exact_job(processing_web) -> None:
    service, adapter = processing_web
    created = adapter.start(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        headers={"Idempotency-Key": "processing-start-key"},
        json_body={},
    )
    replay = adapter.start(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        headers={"Idempotency-Key": "processing-start-key"},
        json_body=None,
    )
    selection_body = {
        "mode": "selected_sheets",
        "scope_plan_sha256": PLAN_SHA256,
        "scope_ids": [SCOPE_ID],
    }
    selected = adapter.select_scope(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        headers={"Idempotency-Key": "processing-scope-key"},
        json_body=selection_body,
    )
    selected_replay = adapter.select_scope(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        headers={"Idempotency-Key": "processing-scope-key"},
        json_body=selection_body,
    )
    status = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    assert created.status_code == selected.status_code == 201
    assert replay.status_code == selected_replay.status_code == status.status_code == 200
    assert created.body["replayed"] is False
    assert replay.body["replayed"] is True
    assert selected.body["replayed"] is False
    assert selected_replay.body["replayed"] is True
    assert created.headers["Location"] == f"/v1/audit/processing-jobs/{JOB_ID}"
    assert selected.headers["Idempotency-Replayed"] == "false"
    assert selected_replay.headers["Idempotency-Replayed"] == "true"
    _assert_schema(created.body, PROCESSING_SUBMISSION_SCHEMA)
    _assert_schema(status.body, PROCESSING_STATUS_SCHEMA)
    assert service.selection_commands[-1]["scope_ids"] == [SCOPE_ID]
    assert service.selection_commands[-1]["principal"] == PRINCIPAL


@pytest.mark.parametrize("body", [{"unexpected": True}, [], "", 0, False])
def test_adapter_start_accepts_only_empty_body_or_empty_object(processing_web, body) -> None:
    service, adapter = processing_web
    response = adapter.start(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        headers={"Idempotency-Key": "invalid-start-body"},
        json_body=body,
    )

    assert response.status_code == 400
    assert response.body["error"]["code"] == "INVALID_REQUEST"
    assert service.start_commands == []
    _assert_schema(response.body, PROCESSING_HTTP_ERROR_SCHEMA)


def test_adapter_rejects_duplicate_key_and_scope_extensions_before_service(processing_web) -> None:
    service, adapter = processing_web
    duplicate = adapter.start(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        headers=[("Idempotency-Key", "one"), ("idempotency-key", "two")],
        json_body={},
    )
    extended = adapter.select_scope(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        headers={"Idempotency-Key": "extended-selection"},
        json_body={
            "mode": "all_sheets",
            "scope_plan_sha256": PLAN_SHA256,
            "scope_ids": [],
            "package_path": "/private/package",
        },
    )

    assert duplicate.status_code == extended.status_code == 400
    assert duplicate.body["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
    assert extended.body["error"]["code"] == "INVALID_REQUEST"
    assert service.start_commands == []
    assert service.selection_commands == []
    assert "/private/package" not in json.dumps(extended.body)


def test_adapter_hides_cross_principal_and_malformed_service_results(processing_web) -> None:
    service, adapter = processing_web
    hidden = adapter.get_status(principal=OTHER, job_id=JOB_ID)
    service.malformed = True
    malformed = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    assert hidden.status_code == 404
    assert hidden.body["error"]["code"] == "JOB_NOT_FOUND"
    assert malformed.status_code == 500
    assert malformed.body["error"]["code"] == "INTERNAL_ERROR"
    serialized = json.dumps(malformed.body).lower()
    assert "package_path" not in serialized
    assert "/private" not in serialized


def test_adapter_rejects_impossible_state_digest_and_dimension_smuggling(
    processing_web,
) -> None:
    service, adapter = processing_web

    service.job_updates = {"status": "published"}
    impossible = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    service.job_updates = {"scope_plan_sha256": "0" * 64}
    wrong_digest = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    path_plan = _scope_plan()
    path_plan["sheets"][0]["dimensions"] = "/home/shin/private.xlsx"
    service.job_updates = {
        "scope_plan": path_plan,
        "scope_plan_sha256": json_sha256(path_plan),
    }
    path_smuggling = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    for response in (impossible, wrong_digest, path_smuggling):
        assert response.status_code == 500
        assert response.body["error"]["code"] == "INTERNAL_ERROR"
    serialized = json.dumps(path_smuggling.body).lower()
    assert "/home" not in serialized
    assert "private.xlsx" not in serialized

    with pytest.raises(jsonschema.ValidationError):
        _assert_schema(_job_document(status="published"), PROCESSING_JOB_SCHEMA)


def test_adapter_rejects_service_route_mismatch(processing_web) -> None:
    service, adapter = processing_web
    original = service._job

    def mismatch():
        job = original()
        job.document["job_id"] = "process-" + "1" * 48
        return job

    service._job = mismatch
    response = adapter.get_status(principal=PRINCIPAL, job_id=JOB_ID)

    assert response.status_code == 500
    assert response.body["error"]["code"] == "INTERNAL_ERROR"


def test_fastapi_asgi_start_scope_selection_status_and_strict_transport(processing_web) -> None:
    httpx = pytest.importorskip("httpx")
    service, adapter = processing_web
    app = create_processing_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
        executor_workers=1,
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://test",
            ) as client:
                start_path = (
                    f"/v1/audit/workbooks/{WORKBOOK_ID}/raw-snapshots/"
                    f"{RAW_SNAPSHOT_ID}/processing-jobs"
                )
                created = await client.post(
                    start_path,
                    headers={"Idempotency-Key": "asgi-start"},
                )
                replay = await client.post(
                    start_path,
                    headers={"Idempotency-Key": "asgi-start"},
                    json={},
                )
                invalid_start = await client.post(
                    start_path,
                    headers={"Idempotency-Key": "asgi-invalid-start"},
                    json={"mode": "workbook"},
                )
                null_start = await client.post(
                    start_path,
                    headers={
                        "Idempotency-Key": "asgi-null-start",
                        "Content-Type": "application/json",
                    },
                    content=b"null",
                )
                duplicate_key = await client.post(
                    start_path,
                    headers=[
                        ("Idempotency-Key", "one"),
                        ("Idempotency-Key", "two"),
                    ],
                    json={},
                )
                selection = await client.post(
                    f"/v1/audit/processing-jobs/{JOB_ID}/scope-selection",
                    headers={"Idempotency-Key": "asgi-selection"},
                    json={
                        "mode": "selected_sheets",
                        "scope_plan_sha256": PLAN_SHA256,
                        "scope_ids": [SCOPE_ID],
                    },
                )
                duplicate_member = await client.post(
                    f"/v1/audit/processing-jobs/{JOB_ID}/scope-selection",
                    headers={
                        "Idempotency-Key": "asgi-duplicate-member",
                        "Content-Type": "application/json",
                    },
                    content=(
                        '{"mode":"workbook","mode":"all_sheets",'
                        f'"scope_plan_sha256":"{PLAN_SHA256}","scope_ids":[]}}'
                    ),
                )
                status = await client.get(f"/v1/audit/processing-jobs/{JOB_ID}")

        assert created.status_code == selection.status_code == 201
        assert replay.status_code == status.status_code == 200
        assert created.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert (
            invalid_start.status_code
            == null_start.status_code
            == duplicate_key.status_code
            == 400
        )
        assert invalid_start.json()["error"]["code"] == "INVALID_REQUEST"
        assert null_start.json()["error"]["code"] == "INVALID_REQUEST"
        assert duplicate_key.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
        assert duplicate_member.status_code == 400
        assert duplicate_member.json()["error"]["code"] == "INVALID_REQUEST"
        assert status.json()["job"]["job_id"] == JOB_ID

    import asyncio

    asyncio.run(scenario())
    assert service.worker_threads
    assert all(name.startswith("audit-processing-web") for name in service.worker_threads)


def test_fastapi_authenticates_before_reading_invalid_json(processing_web) -> None:
    fastapi = pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = processing_web

    async def reject(_request):
        raise fastapi.HTTPException(status_code=401, detail="unauthorized")

    app = create_processing_fastapi_app(adapter, principal_dependency=reject)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
                response = await client.post(
                    (
                        f"/v1/audit/workbooks/{WORKBOOK_ID}/raw-snapshots/"
                        f"{RAW_SNAPSHOT_ID}/processing-jobs"
                    ),
                    headers={
                        "Idempotency-Key": "unauthorized",
                        "Content-Type": "application/json",
                    },
                    content=b"not-json",
                )
        assert response.status_code == 401

    import asyncio

    asyncio.run(scenario())
    assert service.start_commands == []
