from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO

import openpyxl
import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_edit_host import (
    InMemoryWorkbookEditHostSessionRepository,
    WorkbookEditHostSessionService,
)
from excel_to_skill.audit.workbook_edit_service import (
    InMemoryWorkbookEditRepository,
    InMemoryWorkbookSessionRepository,
    WorkbookEditService,
    WorkbookSessionBinding,
)
from excel_to_skill.audit.workbook_edit_web import (
    AuditWorkbookEditHttpAdapter,
    create_workbook_edit_fastapi_app,
)
from excel_to_skill.audit.workbook_snapshot_publication import (
    AcquiredWorkbook,
    LocalImmutableWorkbookAssetStore,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
OTHER = ServicePrincipal("tenant-a", "user-b")
BLANK = {
    "cell": "A1",
    "authored": {"kind": "blank"},
    "calculated_value": None,
    "calculated_type": "empty",
    "number_format": "General",
    "target_constraints": {
        "merged": False,
        "spill": "none",
        "protected": False,
        "table_member": False,
    },
}
AFTER = {
    **BLANK,
    "authored": {"kind": "value", "value": "승인됨"},
    "calculated_value": "승인됨",
    "calculated_type": "string",
}


class _Reacquirer:
    def reacquire_saved_workbook(
        self,
        *,
        expected_workbook_instance_id: str,
        base_revision_id: str,
        expected_sheet: str,
        expected_worksheet_id: str,
        max_bytes: int,
    ) -> AcquiredWorkbook:
        assert expected_workbook_instance_id == "workbook-instance-a"
        assert base_revision_id == "revision-a"
        assert expected_sheet == "C"
        assert expected_worksheet_id == "worksheet-a"
        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = "C"
        worksheet["A1"] = "승인됨"
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        return AcquiredWorkbook(
            provider_revision_id="revision-b",
            predecessor_revision_id=base_revision_id,
            worksheet_id=expected_worksheet_id,
            workbook_instance_id="workbook-instance-a",
            content=buffer.getvalue(),
        )


def _system(tmp_path, *, publication: bool = True, policy: str | None = None):
    binding = WorkbookSessionBinding(
        session_id="office-session-a",
        tenant_id=PRINCIPAL.tenant_id,
        subject_id=PRINCIPAL.subject_id,
        bundle_id="bundle-a",
        snapshot_id="1" * 64,
        workbook_sha256="2" * 64,
        revision_id="revision-a",
        sheet="C",
        worksheet_id="worksheet-a",
        workbook_instance_id="workbook-instance-a",
    )
    sessions = InMemoryWorkbookSessionRepository([binding])
    edits = InMemoryWorkbookEditRepository()
    service = WorkbookEditService(
        sessions=sessions,
        edits=edits,
        **(
            {
                "saved_workbooks": _Reacquirer(),
                "workbook_assets": LocalImmutableWorkbookAssetStore(tmp_path / "assets"),
            }
            if publication
            else {}
        ),
        now=lambda: NOW,
    )
    proposed = service.propose(
        principal=PRINCIPAL,
        session_id="office-session-a",
        proposal_input={
            "changes": [{"cell": "A1", "kind": "set_value", "value": "승인됨"}]
        },
        idempotency_key="propose",
    )
    workflow_id = proposed.receipt.workflow_id
    host = WorkbookEditHostSessionService(
        repository=InMemoryWorkbookEditHostSessionRepository(),
        resolve_current_binding=lambda principal, session_id: sessions.resolve(
            principal=principal,
            session_id=session_id,
        ),
        resolve_workflow_binding=lambda principal, selected_workflow: (
            service.resolve_current_binding(
                principal=principal,
                workflow_id=selected_workflow,
            )
        ),
        resolve_published_binding=lambda principal, selected_workflow: (
            service.resolve_published_binding(
                principal=principal,
                workflow_id=selected_workflow,
            )
        ),
        now=lambda: NOW,
    )
    bootstrap = host.issue(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        session_id="office-session-a",
        persistence_policy=policy or ("required" if publication else "session_only"),
    )
    adapter = AuditWorkbookEditHttpAdapter(service, host_sessions=host)
    headers = {"X-Audit-Workbook-Host-Session": bootstrap["host_session_id"]}
    return service, adapter, workflow_id, bootstrap, headers


def _verified(service: WorkbookEditService, workflow_id: str):
    previewed = service.preview(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_input={
            "office_revision_id": "revision-a",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key="preview",
    )
    preview = previewed.receipt.details["preview"]
    service.approve(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        preview_sha256=preview["preview_sha256"],
        confirmed=True,
        idempotency_key="approve",
    )
    claimed = service.claim_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        session_id="office-session-a",
        idempotency_key="claim",
    )
    details = claimed.receipt.details
    service.mark_apply_started(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="start",
    )
    service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "applied",
            "observed_before": [BLANK],
            "actual_after": [AFTER],
            "recalculation": "recalculate",
        },
        idempotency_key="verify",
    )
    return details


def test_host_bootstrap_is_exact_private_and_required_on_every_bound_read(tmp_path) -> None:
    _, adapter, workflow_id, bootstrap, headers = _system(tmp_path)

    response = adapter.get_host_bootstrap(
        principal=PRINCIPAL,
        host_session_id=bootstrap["host_session_id"],
        headers=headers,
    )
    assert response.status_code == 200
    assert response.body == bootstrap
    serialized = str(response.body).lower()
    for forbidden in ("tenant", "subject", "workbook_instance", "path", "token", "provider"):
        assert forbidden not in serialized

    missing = adapter.get_workflow(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        headers={},
    )
    wrong_principal = adapter.get_host_bootstrap(
        principal=OTHER,
        host_session_id=bootstrap["host_session_id"],
        headers=headers,
    )
    assert missing.status_code == 400
    assert missing.body["error"]["code"] == "INVALID_REQUEST"
    assert wrong_principal.status_code == 404
    assert wrong_principal.body["error"]["code"] == "HOST_SESSION_NOT_FOUND"


@pytest.mark.parametrize("policy", ["session_only", "unsupported"])
def test_publication_capable_server_allows_nonpublication_host_policies(
    tmp_path,
    policy,
) -> None:
    _, adapter, _, bootstrap, headers = _system(
        tmp_path,
        publication=True,
        policy=policy,
    )
    response = adapter.get_host_bootstrap(
        principal=PRINCIPAL,
        host_session_id=bootstrap["host_session_id"],
        headers=headers,
    )
    assert response.status_code == 200
    assert response.body["persistence_policy"] == policy


def test_server_reacquires_and_cas_publishes_then_all_recovery_reads_survive_head_advance(
    tmp_path,
) -> None:
    service, adapter, workflow_id, bootstrap, host_headers = _system(tmp_path)
    details = _verified(service, workflow_id)
    manifest = details["apply_manifest"]
    headers = {**host_headers, "Idempotency-Key": "publish"}

    posted = adapter.publish_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        headers=headers,
        json_body={
            "manifest_ref": manifest["manifest_ref"],
            "manifest_sha256": manifest["manifest_sha256"],
        },
    )
    fetched = adapter.get_snapshot_publication(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        headers=host_headers,
    )
    replayed = adapter.publish_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        headers=headers,
        json_body={
            "manifest_ref": manifest["manifest_ref"],
            "manifest_sha256": manifest["manifest_sha256"],
        },
    )
    refreshed_bootstrap = adapter.get_host_bootstrap(
        principal=PRINCIPAL,
        host_session_id=bootstrap["host_session_id"],
        headers=host_headers,
    )

    assert posted.status_code == fetched.status_code == replayed.status_code == 200
    assert posted.body == fetched.body == replayed.body
    assert refreshed_bootstrap.status_code == 200
    assert refreshed_bootstrap.body == bootstrap
    assert posted.body["asset_persisted"] is True
    assert posted.body["prepared_bundle_created"] is False
    assert posted.body["base_snapshot_id"] == "1" * 64
    assert posted.body["base_revision_id"] == "revision-a"
    assert "asset_ref" not in posted.body


def test_fastapi_authenticates_before_buffering_invalid_json_and_serves_bootstrap(tmp_path) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    _, adapter, _, bootstrap, headers = _system(tmp_path)
    principal_calls: list[int] = []

    def principal_dependency():
        principal_calls.append(1)
        return PRINCIPAL

    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=principal_dependency,
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                bound = await client.get(
                    "/v1/audit/workbook-edit-host-sessions/"
                    f"{bootstrap['host_session_id']}/bootstrap",
                    headers=headers,
                )
                assert bound.status_code == 200
                invalid = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={"Idempotency-Key": "invalid", "Content-Type": "application/json"},
                    content=b'{"session_id":',
                )
                assert invalid.status_code == 400

    asyncio.run(scenario())
    assert len(principal_calls) == 2


def test_fastapi_authentication_rejection_never_reads_mutation_body(tmp_path) -> None:
    fastapi = pytest.importorskip("fastapi")
    _, adapter, _, _, _ = _system(tmp_path)
    receive_calls = 0

    async def reject_principal(_request):
        raise fastapi.HTTPException(status_code=401, detail="unauthorized")

    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=reject_principal,
    )

    async def scenario():
        nonlocal receive_calls
        sent = []

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return {
                "type": "http.request",
                "body": b'{"attacker":"body"}',
                "more_body": False,
            }

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/audit/workbook-edit-workflows",
            "raw_path": b"/v1/audit/workbook-edit-workflows",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"19"),
            ],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        return sent

    messages = asyncio.run(scenario())
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert starts[0]["status"] == 401
    assert receive_calls == 0
