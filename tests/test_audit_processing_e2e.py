from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from excel_to_skill.audit.processing import ProcessingService
from excel_to_skill.audit.processing_sqlite import SQLiteProcessingRepository
from excel_to_skill.audit.processing_store import LocalPreparedBundleStore
from excel_to_skill.audit.processing_web import (
    AuditProcessingHttpAdapter,
    create_processing_fastapi_app,
)
from excel_to_skill.audit.service import (
    AuditConversationService,
    InMemoryConversationArtifactRepository,
    ServicePrincipal,
)
from excel_to_skill.audit.web import AuditConversationHttpAdapter, create_fastapi_app
from excel_to_skill.audit.workbook_asset_service import WorkbookAssetService
from excel_to_skill.audit.workbook_asset_sqlite import SQLiteWorkbookAssetRepository
from excel_to_skill.audit.workbook_asset_web import (
    XLSX_CONTENT_TYPE,
    WorkbookAssetHttpAdapter,
    create_workbook_asset_fastapi_app,
)
from excel_to_skill.audit.workbook_snapshot_publication import (
    LocalImmutableWorkbookAssetStore,
)

from test_audit_conversation_aggregate import RootSelectionClient
from test_audit_prepare import _DESCRIPTOR
from test_audit_processing import _Factories, _xlsx


PRINCIPAL = ServicePrincipal("tenant-e2e", "auditor-e2e")


def test_http_upload_processing_publish_and_bundle_bound_chat_e2e(
    tmp_path: Path,
) -> None:
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from langgraph.checkpoint.memory import InMemorySaver

    root = tmp_path / "server"
    root.mkdir(mode=0o700)
    assets = WorkbookAssetService(
        SQLiteWorkbookAssetRepository(root / "raw.sqlite3"),
        LocalImmutableWorkbookAssetStore(root / "raw-assets"),
    )
    factories = _Factories()
    processing = ProcessingService(
        repository=SQLiteProcessingRepository(root / "processing.sqlite3"),
        workbook_assets=assets,
        bundle_store=LocalPreparedBundleStore(root / "prepared-bundles"),
        workspace_root=root / "processing-workspace",
        model="stub-model",
        client_factory=factories.client,
        standards_retriever_factory=factories.retriever,
        retriever_descriptor=_DESCRIPTOR,
        aggregate_client_factory=factories.aggregate,
        max_prepare_workers=2,
    )
    main_client = RootSelectionClient()
    conversation = AuditConversationService(
        bundles=processing,
        artifacts=InMemoryConversationArtifactRepository(),
        model="main-model",
        client=main_client,
        checkpointer=InMemorySaver(),
    )

    upload_app = create_workbook_asset_fastapi_app(
        WorkbookAssetHttpAdapter(assets),
        principal_dependency=lambda: PRINCIPAL,
    )
    processing_app = create_processing_fastapi_app(
        AuditProcessingHttpAdapter(processing),
        principal_dependency=lambda: PRINCIPAL,
        executor_workers=2,
    )
    conversation_app = create_fastapi_app(
        AuditConversationHttpAdapter(conversation),
        principal_dependency=lambda: PRINCIPAL,
    )

    async def scenario():
        async with upload_app.router.lifespan_context(upload_app):
            transport = httpx.ASGITransport(app=upload_app)
            async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
                uploaded = await client.post(
                    "/v1/audit/workbooks",
                    headers={
                        "Content-Type": XLSX_CONTENT_TYPE,
                        "Idempotency-Key": "e2e-upload",
                    },
                    content=_xlsx(),
                )
        assert uploaded.status_code == 201
        raw = uploaded.json()["snapshot"]

        async with processing_app.router.lifespan_context(processing_app):
            transport = httpx.ASGITransport(app=processing_app)
            async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
                start_path = (
                    f"/v1/audit/workbooks/{raw['workbook_id']}/raw-snapshots/"
                    f"{raw['raw_snapshot_id']}/processing-jobs"
                )
                planned = await client.post(
                    start_path,
                    headers={"Idempotency-Key": "e2e-plan"},
                )
                assert planned.status_code == 201
                assert factories.prepare_clients == []
                job = planned.json()["job"]
                published = await client.post(
                    f"/v1/audit/processing-jobs/{job['job_id']}/scope-selection",
                    headers={"Idempotency-Key": "e2e-scope"},
                    json={
                        "mode": "all_sheets",
                        "scope_plan_sha256": job["scope_plan_sha256"],
                        "scope_ids": [],
                    },
                )
                assert published.status_code == 201
                final_job = published.json()["job"]
                fetched = await client.get(
                    f"/v1/audit/processing-jobs/{job['job_id']}"
                )
        assert fetched.status_code == 200
        assert fetched.json()["job"] == final_job
        assert final_job["status"] == "published"
        result = final_job["result"]
        assert result["chat_binding"]["aggregate_id"]

        async with conversation_app.router.lifespan_context(conversation_app):
            transport = httpx.ASGITransport(app=conversation_app)
            async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
                # Scope is deliberately omitted: the server injects the published aggregate root.
                answered = await client.post(
                    "/v1/audit/conversation-turns",
                    headers={"Idempotency-Key": "e2e-chat"},
                    json={
                        "bundle_id": result["bundle_id"],
                        "question": "계정별 핵심 사항은?",
                    },
                )
        return raw, final_job, answered

    raw, final_job, answered = asyncio.run(scenario())
    assert answered.status_code == 201
    receipt = answered.json()["receipt"]
    assert receipt["bundle_id"] == final_job["result"]["bundle_id"]
    assert receipt["snapshot_id"] == final_job["result"]["snapshot_id"]
    evidence = receipt["result"]["response"]["evidence"]["records"]
    assert {item["scope"]["sheet"] for item in evidence} == {"Alpha", "Beta"}
    assert raw["workbook_sha256"] == final_job["workbook_sha256"]
    assert len(main_client.calls) == 1
