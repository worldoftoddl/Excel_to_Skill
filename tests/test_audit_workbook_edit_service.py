from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from io import BytesIO

import openpyxl
import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_snapshot_publication import (
    AcquiredWorkbook,
    LocalImmutableWorkbookAssetStore,
)
from excel_to_skill.audit.workbook_edit_service import (
    InMemoryWorkbookEditRepository,
    InMemoryWorkbookSessionRepository,
    WorkbookEditService,
    WorkbookEditServiceError,
    WorkbookSessionBinding,
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
CHANGES = [{"cell": "A1", "kind": "set_value", "value": "승인됨"}]


def _binding(**changes) -> WorkbookSessionBinding:
    values = {
        "session_id": "office-session-a",
        "tenant_id": PRINCIPAL.tenant_id,
        "subject_id": PRINCIPAL.subject_id,
        "bundle_id": "bundle-a",
        "snapshot_id": "a" * 64,
        "workbook_sha256": "b" * 64,
        "revision_id": "revision-a",
        "sheet": "매출채권",
        "worksheet_id": "worksheet-a",
        "workbook_instance_id": "workbook-instance-a",
    }
    values.update(changes)
    return WorkbookSessionBinding(**values)


def test_session_binding_requires_a_valid_stable_workbook_instance_id() -> None:
    values = asdict(_binding())
    values.pop("workbook_instance_id")
    with pytest.raises(TypeError):
        WorkbookSessionBinding(**values)

    with pytest.raises(ValueError):
        _binding(workbook_instance_id=None)


@pytest.fixture
def edit_service():
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    edits = InMemoryWorkbookEditRepository()
    service = WorkbookEditService(
        sessions=sessions,
        edits=edits,
        now=lambda: NOW,
    )
    return service, sessions, edits


def _propose(service: WorkbookEditService, *, key: str = "propose-1"):
    return service.propose(
        principal=PRINCIPAL,
        session_id="office-session-a",
        proposal_input={"changes": CHANGES},
        idempotency_key=key,
    )


def _preview(service: WorkbookEditService, workflow_id: str, *, key: str = "preview-1"):
    return service.preview(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_input={
            "office_revision_id": "revision-a",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key=key,
    )


def _approve(service: WorkbookEditService, workflow_id: str, preview: dict, *, key="approve-1"):
    return service.approve(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        preview_sha256=preview["preview_sha256"],
        confirmed=True,
        idempotency_key=key,
    )


def _approved(service: WorkbookEditService, *, prefix: str = ""):
    proposed = _propose(service, key=prefix + "propose")
    workflow_id = proposed.receipt.workflow_id
    previewed = _preview(service, workflow_id, key=prefix + "preview")
    preview = previewed.receipt.details["preview"]
    _approve(service, workflow_id, preview, key=prefix + "approve")
    return workflow_id, preview


def _claim(service: WorkbookEditService, workflow_id: str, *, key="claim-1"):
    return service.claim_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        session_id="office-session-a",
        idempotency_key=key,
    )


def _start(service: WorkbookEditService, workflow_id: str, details: dict, *, key="start-1"):
    return service.mark_apply_started(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key=key,
    )


def _verify(service: WorkbookEditService, workflow_id: str, details: dict, *, key="verify-1"):
    return service.verify_execution(
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
        idempotency_key=key,
    )


def _assert_error(code: str, call) -> WorkbookEditServiceError:
    with pytest.raises(WorkbookEditServiceError) as caught:
        call()
    assert caught.value.code == code
    assert "/" not in caught.value.message
    return caught.value


class _SavedWorkbookReacquirer:
    def __init__(
        self,
        *,
        content: bytes | None = None,
        revision_id: str = "revision-b",
        workbook_instance_id: str = "workbook-instance-a",
    ) -> None:
        self.content = _saved_workbook_bytes() if content is None else content
        self.revision_id = revision_id
        self.workbook_instance_id = workbook_instance_id
        self.calls = 0

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
        assert expected_sheet == "매출채권"
        assert expected_worksheet_id == "worksheet-a"
        assert len(self.content) <= max_bytes
        self.calls += 1
        return AcquiredWorkbook(
            provider_revision_id=self.revision_id,
            predecessor_revision_id=base_revision_id,
            worksheet_id=expected_worksheet_id,
            workbook_instance_id=self.workbook_instance_id,
            content=self.content,
        )


def _saved_workbook_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "매출채권"
    worksheet["A1"] = "승인됨"
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_full_lifecycle_is_exact_fenced_and_session_verified(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    claimed = _claim(service, workflow_id)
    details = claimed.receipt.details

    assert claimed.receipt.state == "claimed"
    assert details["fence"] == 1
    assert details["apply_manifest"]["fencing_token"] == 1
    assert details["apply_manifest"]["challenge_nonce"] == details["challenge"]
    assert details["apply_manifest"]["office_binding"] == {
        "session_id": "office-session-a",
        "revision_id": "revision-a",
        "worksheet_id": "worksheet-a",
        "sheet": "매출채권",
    }

    started = _start(service, workflow_id, details)
    verified = _verify(service, workflow_id, details)

    assert started.receipt.state == "apply_started"
    assert started.receipt.details["execution_deadline"] == "2026-07-14T00:02:00Z"
    assert verified.receipt.state == "session_verified"
    assert verified.receipt.details["verification"]["asset_persisted"] is False
    assert verified.receipt.details["verification"]["new_snapshot_required"] is True
    workflow = service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    assert workflow["state"] == "session_verified"
    assert workflow["approval_consumed"] is True
    assert workflow["execution_deadline"] == "2026-07-14T00:02:00Z"
    assert "challenge" not in workflow


def test_publication_enabled_service_holds_lock_until_source_head_cas(tmp_path) -> None:
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    edits = InMemoryWorkbookEditRepository()
    reacquirer = _SavedWorkbookReacquirer()
    service = WorkbookEditService(
        sessions=sessions,
        edits=edits,
        saved_workbooks=reacquirer,
        workbook_assets=LocalImmutableWorkbookAssetStore(tmp_path / "assets"),
        now=lambda: NOW,
    )
    workflow_id, _ = _approved(service, prefix="publish-a-")
    claim = _claim(service, workflow_id, key="publish-a-claim")
    details = claim.receipt.details
    _start(service, workflow_id, details, key="publish-a-start")
    _verify(service, workflow_id, details, key="publish-a-verify")

    second_workflow, _ = _approved(service, prefix="publish-b-")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_workflow, key="publish-b-claim"),
    )

    manifest = details["apply_manifest"]
    published = service.publish_verified_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        manifest_ref=manifest["manifest_ref"],
        manifest_sha256=manifest["manifest_sha256"],
        idempotency_key="publish-a-snapshot",
    )
    publication = published.receipt.details["publication"]

    assert publication == service.get_snapshot_publication(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
    )
    assert publication["base_snapshot_id"] == "a" * 64
    assert publication["base_revision_id"] == "revision-a"
    assert publication["revision_id"] == "revision-b"
    assert publication["asset_persisted"] is True
    assert publication["prepared_bundle_created"] is False
    assert "asset_ref" not in publication
    assert "workbook_instance_id" not in publication
    assert reacquirer.calls == 1

    replay = service.publish_verified_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        manifest_ref=manifest["manifest_ref"],
        manifest_sha256=manifest["manifest_sha256"],
        idempotency_key="publish-a-snapshot",
    )
    assert replay.replayed is True
    assert replay.receipt.to_dict() == published.receipt.to_dict()
    assert reacquirer.calls == 1

    # The repository head now outranks the old mutable Office-session registration. A host must
    # register a new exact revision before any subsequent edit can proceed.
    _assert_error(
        "STALE_REVISION",
        lambda: _claim(service, second_workflow, key="publish-b-claim-after-cas"),
    )


def test_publication_enabled_service_can_pin_a_session_only_execution(tmp_path) -> None:
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        saved_workbooks=_SavedWorkbookReacquirer(),
        workbook_assets=LocalImmutableWorkbookAssetStore(tmp_path / "assets"),
        now=lambda: NOW,
    )
    workflow_id, _ = _approved(service, prefix="session-only-a-")
    claimed = service.claim_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        session_id="office-session-a",
        idempotency_key="session-only-a-claim",
        publication_required=False,
    )
    details = claimed.receipt.details
    _start(service, workflow_id, details, key="session-only-a-start")
    _verify(service, workflow_id, details, key="session-only-a-verify")

    second_workflow, _ = _approved(service, prefix="session-only-b-")
    second = _claim(service, second_workflow, key="session-only-b-claim")
    assert second.receipt.details["fence"] == 2
    manifest = details["apply_manifest"]
    _assert_error(
        "PUBLICATION_NOT_READY",
        lambda: service.publish_verified_snapshot(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            execution_id=details["execution_id"],
            manifest_ref=manifest["manifest_ref"],
            manifest_sha256=manifest["manifest_sha256"],
            idempotency_key="session-only-publish",
        ),
    )


def test_publication_identity_mismatch_keeps_workbook_quarantined(tmp_path) -> None:
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    edits = InMemoryWorkbookEditRepository()
    service = WorkbookEditService(
        sessions=sessions,
        edits=edits,
        saved_workbooks=_SavedWorkbookReacquirer(workbook_instance_id="copied-workbook"),
        workbook_assets=LocalImmutableWorkbookAssetStore(tmp_path / "assets"),
        now=lambda: NOW,
    )
    workflow_id, _ = _approved(service, prefix="mismatch-a-")
    details = _claim(service, workflow_id, key="mismatch-a-claim").receipt.details
    _start(service, workflow_id, details, key="mismatch-a-start")
    _verify(service, workflow_id, details, key="mismatch-a-verify")
    manifest = details["apply_manifest"]

    _assert_error(
        "PUBLICATION_CONFLICT",
        lambda: service.publish_verified_snapshot(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            execution_id=details["execution_id"],
            manifest_ref=manifest["manifest_ref"],
            manifest_sha256=manifest["manifest_sha256"],
            idempotency_key="mismatch-a-publish",
        ),
    )

    second_workflow, _ = _approved(service, prefix="mismatch-b-")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_workflow, key="mismatch-b-claim"),
    )


def test_saved_workbook_is_validated_before_immutable_asset_write(tmp_path) -> None:
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    assets_root = tmp_path / "assets"
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        saved_workbooks=_SavedWorkbookReacquirer(content=b"not-an-xlsx"),
        workbook_assets=LocalImmutableWorkbookAssetStore(assets_root),
        now=lambda: NOW,
    )
    workflow_id, _ = _approved(service, prefix="invalid-saved-")
    details = _claim(service, workflow_id, key="invalid-saved-claim").receipt.details
    _start(service, workflow_id, details, key="invalid-saved-start")
    _verify(service, workflow_id, details, key="invalid-saved-verify")
    manifest = details["apply_manifest"]

    _assert_error(
        "PUBLICATION_CONFLICT",
        lambda: service.publish_verified_snapshot(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            execution_id=details["execution_id"],
            manifest_ref=manifest["manifest_ref"],
            manifest_sha256=manifest["manifest_sha256"],
            idempotency_key="invalid-saved-publish",
        ),
    )

    assert list((assets_root / "objects").iterdir()) == []


def test_every_completed_mutation_replays_same_receipt(edit_service) -> None:
    service, _, _ = edit_service
    proposed = _propose(service)
    replay = _propose(service)
    assert replay.replayed is True
    assert replay.receipt.to_dict() == proposed.receipt.to_dict()

    workflow_id = proposed.receipt.workflow_id
    previewed = _preview(service, workflow_id)
    assert _preview(service, workflow_id).replayed is True
    preview = previewed.receipt.details["preview"]
    approved = _approve(service, workflow_id, preview)
    assert _approve(service, workflow_id, preview).receipt.to_dict() == approved.receipt.to_dict()
    claimed = _claim(service, workflow_id)
    assert _claim(service, workflow_id).receipt.to_dict() == claimed.receipt.to_dict()
    details = claimed.receipt.details
    started = _start(service, workflow_id, details)
    assert _start(service, workflow_id, details).receipt.to_dict() == started.receipt.to_dict()
    verified = _verify(service, workflow_id, details)
    assert _verify(service, workflow_id, details).receipt.to_dict() == verified.receipt.to_dict()


def test_same_idempotency_key_with_different_command_conflicts(edit_service) -> None:
    service, _, _ = edit_service
    _propose(service, key="shared-key")
    _assert_error(
        "IDEMPOTENCY_CONFLICT",
        lambda: service.propose(
            principal=PRINCIPAL,
            session_id="office-session-a",
            proposal_input={
                "changes": [{"cell": "A1", "kind": "set_value", "value": "다름"}]
            },
            idempotency_key="shared-key",
        ),
    )


def test_pending_same_command_reports_in_progress(edit_service) -> None:
    service, _, edits = edit_service
    entered = threading.Event()
    release = threading.Event()
    original = edits.publish_transition

    def blocking_publish(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(**kwargs)

    edits.publish_transition = blocking_publish
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_propose, service, key="pending-key")
        assert entered.wait(timeout=5)
        error = _assert_error(
            "COMMAND_IN_PROGRESS",
            lambda: _propose(service, key="pending-key"),
        )
        assert error.status_code == 409
        release.set()
        assert first.result(timeout=5).receipt.state == "proposed"


def test_approval_requires_exact_preview_and_explicit_confirmation(edit_service) -> None:
    service, _, _ = edit_service
    proposed = _propose(service)
    workflow_id = proposed.receipt.workflow_id
    preview = _preview(service, workflow_id).receipt.details["preview"]

    _assert_error(
        "PREVIEW_MISMATCH",
        lambda: service.approve(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            preview_id=preview["preview_ref"],
            preview_sha256="f" * 64,
            confirmed=True,
            idempotency_key="bad-digest",
        ),
    )
    _assert_error(
        "APPROVAL_CONFIRMATION_REQUIRED",
        lambda: service.approve(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            preview_id=preview["preview_ref"],
            preview_sha256=preview["preview_sha256"],
            confirmed=False,
            idempotency_key="not-confirmed",
        ),
    )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "previewed"


def test_preview_checks_registered_revision_and_worksheet(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id = _propose(service).receipt.workflow_id
    for field, value, code in (
        ("office_revision_id", "revision-b", "STALE_REVISION"),
        ("worksheet_id", "worksheet-b", "WORKSHEET_MISMATCH"),
    ):
        body = {
            "office_revision_id": "revision-a",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        }
        body[field] = value
        _assert_error(
            code,
            lambda body=body, field=field: service.preview(
                principal=PRINCIPAL,
                workflow_id=workflow_id,
                preview_input=body,
                idempotency_key="bad-" + field,
            ),
        )


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ({"revision_id": "revision-b"}, "STALE_REVISION"),
        ({"workbook_sha256": "c" * 64}, "WORKBOOK_MISMATCH"),
        ({"snapshot_id": "d" * 64}, "BUNDLE_MISMATCH"),
        ({"worksheet_id": "worksheet-b"}, "WORKSHEET_MISMATCH"),
    ],
)
def test_resume_fails_closed_when_registered_session_drifts(
    edit_service, replacement, code
) -> None:
    service, sessions, _ = edit_service
    workflow_id = _propose(service).receipt.workflow_id
    sessions.register(binding=_binding(**replacement))
    _assert_error(
        code,
        lambda: _preview(service, workflow_id),
    )


def test_principal_scope_cannot_resolve_another_users_session(edit_service) -> None:
    service, _, _ = edit_service
    _assert_error(
        "SESSION_NOT_FOUND",
        lambda: service.propose(
            principal=OTHER,
            session_id="office-session-a",
            proposal_input={"changes": CHANGES},
            idempotency_key="other-user",
        ),
    )


def test_one_active_execution_per_session_and_monotonic_fence(edit_service) -> None:
    service, _, _ = edit_service
    first_id, _ = _approved(service, prefix="first-")
    second_id, _ = _approved(service, prefix="second-")
    first = _claim(service, first_id, key="first-claim")

    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="second-claim-blocked"),
    )
    details = first.receipt.details
    service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=first_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="abort-first",
    )
    second = _claim(service, second_id, key="second-claim")
    assert second.receipt.details["fence"] == 2


def test_aborted_claim_consumes_approval_and_cannot_be_reclaimed(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    claimed = _claim(service, workflow_id)
    details = claimed.receipt.details
    aborted = service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="abort",
    )
    assert aborted.receipt.state == "aborted_before_apply"
    _assert_error(
        "APPROVAL_REPLAY",
        lambda: _claim(service, workflow_id, key="reclaim"),
    )


def test_abort_and_execution_retry_are_forbidden_after_apply_started(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    _start(service, workflow_id, details)

    _assert_error(
        "RETRY_FORBIDDEN",
        lambda: service.abort_claim(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            execution_id=details["execution_id"],
            fence=details["fence"],
            challenge=details["challenge"],
            idempotency_key="late-abort",
        ),
    )
    _assert_error(
        "RETRY_FORBIDDEN",
        lambda: _start(service, workflow_id, details, key="second-start"),
    )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "apply_started"


def test_wrong_fence_or_challenge_never_starts_apply(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    for fence, challenge in (
        (details["fence"] + 1, details["challenge"]),
        (details["fence"], "wrong-challenge"),
    ):
        _assert_error(
            "EXECUTION_CLAIM_MISMATCH",
            lambda fence=fence, challenge=challenge: service.mark_apply_started(
                principal=PRINCIPAL,
                workflow_id=workflow_id,
                execution_id=details["execution_id"],
                fence=fence,
                challenge=challenge,
                idempotency_key=f"wrong-{fence}-{challenge}",
            ),
        )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "claimed"


def test_approval_expiry_blocks_write_start_but_claim_can_be_aborted() -> None:
    clock = [NOW]
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        approval_ttl=timedelta(seconds=5),
        now=lambda: clock[0],
    )
    workflow_id, _ = _approved(service)
    claimed = _claim(service, workflow_id)
    details = claimed.receipt.details
    clock[0] += timedelta(seconds=6)
    _assert_error(
        "APPROVAL_EXPIRED",
        lambda: _start(service, workflow_id, details),
    )
    assert service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="expired-abort",
    ).receipt.state == "aborted_before_apply"


@pytest.mark.parametrize(
    ("outcome", "actual_after", "expected_state"),
    [
        ("indeterminate", None, "indeterminate"),
        ("applied", [BLANK], "verification_failed"),
    ],
)
def test_verification_terminal_states_release_active_claim(
    edit_service, outcome, actual_after, expected_state
) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    _start(service, workflow_id, details)
    completed = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": outcome,
            "observed_before": [BLANK],
            "actual_after": actual_after,
            "recalculation": "recalculate",
        },
        idempotency_key="terminal",
    )
    assert completed.receipt.state == expected_state


def test_provider_exception_text_is_never_exposed() -> None:
    class BrokenSessions:
        def resolve(self, **kwargs):
            del kwargs
            raise RuntimeError("/private/client.xlsx token=super-secret")

    service = WorkbookEditService(
        sessions=BrokenSessions(),
        edits=InMemoryWorkbookEditRepository(),
    )
    error = _assert_error(
        "SERVICE_UNAVAILABLE",
        lambda: service.propose(
            principal=PRINCIPAL,
            session_id="office-session-a",
            proposal_input={"changes": CHANGES},
            idempotency_key="failure",
        ),
    )
    assert "private" not in error.message
    assert "secret" not in error.message


def test_completed_replays_do_not_require_a_live_session_or_current_revision() -> None:
    def fresh():
        sessions = InMemoryWorkbookSessionRepository([_binding()])
        service = WorkbookEditService(
            sessions=sessions,
            edits=InMemoryWorkbookEditRepository(),
            now=lambda: NOW,
        )
        return service, sessions

    service, sessions = fresh()
    proposed = _propose(service)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert _propose(service).receipt.to_dict() == proposed.receipt.to_dict()

    service, sessions = fresh()
    workflow_id = _propose(service).receipt.workflow_id
    previewed = _preview(service, workflow_id)
    sessions.register(binding=_binding(revision_id="revision-drift"))
    assert _preview(service, workflow_id).receipt.to_dict() == previewed.receipt.to_dict()

    service, sessions = fresh()
    workflow_id = _propose(service).receipt.workflow_id
    preview = _preview(service, workflow_id).receipt.details["preview"]
    approved = _approve(service, workflow_id, preview)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert _approve(service, workflow_id, preview).receipt.to_dict() == approved.receipt.to_dict()

    service, sessions = fresh()
    workflow_id = _propose(service).receipt.workflow_id
    preview = _preview(service, workflow_id).receipt.details["preview"]
    rejected = service.reject(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        idempotency_key="reject-replay",
    )
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert service.reject(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        idempotency_key="reject-replay",
    ).receipt.to_dict() == rejected.receipt.to_dict()

    service, sessions = fresh()
    workflow_id, _ = _approved(service)
    claimed = _claim(service, workflow_id)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert _claim(service, workflow_id).receipt.to_dict() == claimed.receipt.to_dict()

    service, sessions = fresh()
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    started = _start(service, workflow_id, details)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert _start(service, workflow_id, details).receipt.to_dict() == started.receipt.to_dict()

    service, sessions = fresh()
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    aborted = service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="abort-replay",
    )
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="abort-replay",
    ).receipt.to_dict() == aborted.receipt.to_dict()

    service, sessions = fresh()
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    _start(service, workflow_id, details)
    verified = _verify(service, workflow_id, details)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")
    assert _verify(service, workflow_id, details).receipt.to_dict() == verified.receipt.to_dict()


def test_stale_precondition_finishes_claim_before_write_and_releases_quarantine(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service, prefix="stale-")
    details = _claim(service, workflow_id, key="stale-claim").receipt.details
    changed = {
        **AFTER,
        "authored": {"kind": "value", "value": "다른 사용자 변경"},
        "calculated_value": "다른 사용자 변경",
    }
    stale = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "stale_precondition",
            "observed_before": [changed],
            "actual_after": None,
            "recalculation": "none",
        },
        idempotency_key="stale-witness",
    )
    assert stale.receipt.state == "stale_precondition"
    assert stale.receipt.details["verification"]["application_status"] == "not_applied"

    second_id, _ = _approved(service, prefix="after-stale-")
    second = _claim(service, second_id, key="after-stale-claim")
    assert second.receipt.details["fence"] == 2


def test_indeterminate_result_keeps_live_workbook_quarantined(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service, prefix="indeterminate-")
    details = _claim(service, workflow_id, key="indeterminate-claim").receipt.details
    _start(service, workflow_id, details, key="indeterminate-start")
    result = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "indeterminate",
            "observed_before": [BLANK],
            "actual_after": None,
            "recalculation": "recalculate",
        },
        idempotency_key="indeterminate-witness",
    )
    assert result.receipt.state == "indeterminate"

    second_id, _ = _approved(service, prefix="quarantined-")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="quarantined-claim"),
    )


def test_live_workbook_claim_is_shared_across_subjects_and_sessions() -> None:
    other_binding = _binding(
        session_id="office-session-b",
        subject_id=OTHER.subject_id,
    )
    sessions = InMemoryWorkbookSessionRepository([_binding(), other_binding])
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        now=lambda: NOW,
    )

    first_id, _ = _approved(service, prefix="owner-")
    _claim(service, first_id, key="owner-claim")

    proposed = service.propose(
        principal=OTHER,
        session_id="office-session-b",
        proposal_input={"changes": CHANGES},
        idempotency_key="other-propose",
    )
    other_id = proposed.receipt.workflow_id
    preview = service.preview(
        principal=OTHER,
        workflow_id=other_id,
        preview_input={
            "office_revision_id": "revision-a",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key="other-preview",
    ).receipt.details["preview"]
    service.approve(
        principal=OTHER,
        workflow_id=other_id,
        preview_id=preview["preview_ref"],
        preview_sha256=preview["preview_sha256"],
        confirmed=True,
        idempotency_key="other-approve",
    )
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: service.claim_execution(
            principal=OTHER,
            workflow_id=other_id,
            session_id="office-session-b",
            idempotency_key="other-claim",
        ),
    )


def test_get_workflow_exposes_review_artifacts_but_not_witness_or_challenge(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    document = service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    artifacts = document["artifacts"]

    assert artifacts["proposal"]["schema_version"] == "audit_workbook_edit_proposal.v1"
    assert artifacts["preview"]["schema_version"] == "audit_workbook_edit_preview.v1"
    assert artifacts["approval"]["schema_version"] == "audit_workbook_edit_approval.v1"
    assert artifacts["manifest"] == {
        "manifest_ref": details["apply_manifest"]["manifest_ref"],
        "manifest_sha256": details["apply_manifest"]["manifest_sha256"],
        "execution_id": details["execution_id"],
        "fencing_token": details["fence"],
        "redacted": True,
    }
    serialized = str(document)
    assert details["challenge"] not in serialized
    assert "challenge_nonce" not in serialized
    assert "witness" not in document


def test_contract_payload_limit_has_fixed_413_error(edit_service) -> None:
    service, _, _ = edit_service
    changes = [
        {"cell": f"A{index}", "kind": "set_value", "value": "가" * 10_000}
        for index in range(1, 101)
    ]
    error = _assert_error(
        "LIMIT_EXCEEDED",
        lambda: service.propose(
            principal=PRINCIPAL,
            session_id="office-session-a",
            proposal_input={"changes": changes},
            idempotency_key="oversize",
        ),
    )
    assert error.status_code == 413


def test_get_and_exact_prewrite_abort_survive_session_unregister(edit_service) -> None:
    service, sessions, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")

    stored = service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    assert stored["state"] == "claimed"
    aborted = service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key="orphan-abort",
    )
    assert aborted.receipt.state == "aborted_before_apply"


def test_stale_witness_survives_session_drift_before_write(edit_service) -> None:
    service, sessions, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    sessions.register(binding=_binding(revision_id="revision-b"))
    changed = {
        **AFTER,
        "authored": {"kind": "value", "value": "drift"},
        "calculated_value": "drift",
    }

    stale = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "stale_precondition",
            "observed_before": [changed],
            "actual_after": None,
            "recalculation": "none",
        },
        idempotency_key="drift-stale",
    )
    assert stale.receipt.state == "stale_precondition"


def test_postwrite_applied_witness_survives_session_unregister(edit_service) -> None:
    service, sessions, _ = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    _start(service, workflow_id, details)
    sessions.unregister(principal=PRINCIPAL, session_id="office-session-a")

    verified = _verify(service, workflow_id, details)
    assert verified.receipt.state == "session_verified"


def test_snapshot_hash_and_revision_change_cannot_bypass_quarantine_or_reset_fence(
    edit_service,
) -> None:
    service, sessions, edits = edit_service
    workflow_id, _ = _approved(service, prefix="quarantine-a-")
    details = _claim(service, workflow_id, key="quarantine-a-claim").receipt.details
    _start(service, workflow_id, details, key="quarantine-a-start")
    sessions.register(
        binding=_binding(
            snapshot_id="c" * 64,
            workbook_sha256="d" * 64,
            revision_id="revision-b",
        )
    )
    result = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "indeterminate",
            "observed_before": [BLANK],
            "actual_after": None,
            "recalculation": "recalculate",
        },
        idempotency_key="quarantine-a-witness",
    )
    assert result.receipt.state == "indeterminate"

    proposed = service.propose(
        principal=PRINCIPAL,
        session_id="office-session-a",
        proposal_input={"changes": CHANGES},
        idempotency_key="quarantine-b-propose",
    )
    second_id = proposed.receipt.workflow_id
    preview = service.preview(
        principal=PRINCIPAL,
        workflow_id=second_id,
        preview_input={
            "office_revision_id": "revision-b",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key="quarantine-b-preview",
    ).receipt.details["preview"]
    _approve(service, second_id, preview, key="quarantine-b-approve")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="quarantine-b-claim"),
    )
    assert list(edits._fences.values()) == [1]


def test_fence_remains_monotonic_across_snapshot_hash_and_revision_change_after_abort(
    edit_service,
) -> None:
    service, sessions, _ = edit_service
    first_id, _ = _approved(service, prefix="fence-a-")
    first = _claim(service, first_id, key="fence-a-claim").receipt.details
    service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=first_id,
        execution_id=first["execution_id"],
        fence=first["fence"],
        challenge=first["challenge"],
        idempotency_key="fence-a-abort",
    )
    sessions.register(
        binding=_binding(
            snapshot_id="c" * 64,
            workbook_sha256="d" * 64,
            revision_id="revision-b",
        )
    )

    second_id = service.propose(
        principal=PRINCIPAL,
        session_id="office-session-a",
        proposal_input={"changes": CHANGES},
        idempotency_key="fence-b-propose",
    ).receipt.workflow_id
    preview = service.preview(
        principal=PRINCIPAL,
        workflow_id=second_id,
        preview_input={
            "office_revision_id": "revision-b",
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key="fence-b-preview",
    ).receipt.details["preview"]
    _approve(service, second_id, preview, key="fence-b-approve")
    second = _claim(service, second_id, key="fence-b-claim")
    assert second.receipt.details["fence"] == 2


def test_live_lock_and_fence_cover_all_sheets_in_one_workbook_instance() -> None:
    second_binding = _binding(
        session_id="office-session-b",
        sheet="재고자산",
        worksheet_id="worksheet-b",
    )
    sessions = InMemoryWorkbookSessionRepository([_binding(), second_binding])
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        now=lambda: NOW,
    )
    first_id, _ = _approved(service, prefix="sheet-a-")
    first = _claim(service, first_id, key="sheet-a-claim").receipt.details

    second_id = service.propose(
        principal=PRINCIPAL,
        session_id="office-session-b",
        proposal_input={"changes": CHANGES},
        idempotency_key="sheet-b-propose",
    ).receipt.workflow_id
    preview = service.preview(
        principal=PRINCIPAL,
        workflow_id=second_id,
        preview_input={
            "office_revision_id": "revision-a",
            "worksheet_id": "worksheet-b",
            "before": [BLANK],
        },
        idempotency_key="sheet-b-preview",
    ).receipt.details["preview"]
    _approve(service, second_id, preview, key="sheet-b-approve")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: service.claim_execution(
            principal=PRINCIPAL,
            workflow_id=second_id,
            session_id="office-session-b",
            idempotency_key="sheet-b-blocked",
        ),
    )

    service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=first_id,
        execution_id=first["execution_id"],
        fence=first["fence"],
        challenge=first["challenge"],
        idempotency_key="sheet-a-abort",
    )
    second = service.claim_execution(
        principal=PRINCIPAL,
        workflow_id=second_id,
        session_id="office-session-b",
        idempotency_key="sheet-b-claim",
    )
    assert second.receipt.details["fence"] == 2


def test_publish_copy_failure_is_atomic_and_cannot_enable_two_writers(edit_service) -> None:
    service, _, edits = edit_service
    first_id, _ = _approved(service, prefix="atomic-first-")
    first = _claim(service, first_id, key="atomic-first-claim").receipt.details
    second_id, _ = _approved(service, prefix="atomic-second-")

    original_copy = edits._copy_workflow
    calls = 0

    def fail_during_publish(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated copy failure /private/path token=secret")
        return original_copy(value)

    edits._copy_workflow = fail_during_publish
    error = _assert_error(
        "SERVICE_UNAVAILABLE",
        lambda: service.abort_claim(
            principal=PRINCIPAL,
            workflow_id=first_id,
            execution_id=first["execution_id"],
            fence=first["fence"],
            challenge=first["challenge"],
            idempotency_key="atomic-abort-fails",
        ),
    )
    assert "private" not in error.message
    edits._copy_workflow = original_copy

    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="atomic-second-blocked"),
    )
    assert _start(service, first_id, first, key="atomic-first-start").receipt.state == "apply_started"


def test_claim_copy_failure_leaves_approval_active_map_and_fence_untouched(edit_service) -> None:
    service, _, edits = edit_service
    workflow_id, _ = _approved(service)
    original_copy = edits._copy_workflow
    calls = 0

    def fail_claim_copy(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("copy failure")
        return original_copy(value)

    edits._copy_workflow = fail_claim_copy
    _assert_error(
        "SERVICE_UNAVAILABLE",
        lambda: _claim(service, workflow_id, key="failed-claim"),
    )
    edits._copy_workflow = original_copy
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "approved"
    assert edits._active == {}
    assert edits._fences == {}
    assert _claim(service, workflow_id, key="failed-claim").receipt.details["fence"] == 1


def test_mark_start_requires_repository_active_fence_defense_in_depth(edit_service) -> None:
    service, _, edits = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    edits._active.clear()

    _assert_error(
        "EDIT_CONFLICT",
        lambda: _start(service, workflow_id, details),
    )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "claimed"


def test_verification_publish_requires_repository_active_fence(edit_service) -> None:
    service, _, edits = edit_service
    workflow_id, _ = _approved(service)
    details = _claim(service, workflow_id).receipt.details
    _start(service, workflow_id, details)
    edits._active.clear()

    _assert_error(
        "EDIT_CONFLICT",
        lambda: _verify(service, workflow_id, details),
    )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)[
        "state"
    ] == "apply_started"


def test_execution_lease_expiry_quarantines_applied_but_allows_indeterminate_report() -> None:
    clock = [NOW]
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    service = WorkbookEditService(
        sessions=sessions,
        edits=InMemoryWorkbookEditRepository(),
        execution_ttl=timedelta(minutes=2),
        now=lambda: clock[0],
    )
    workflow_id, _ = _approved(service, prefix="lease-")
    details = _claim(service, workflow_id, key="lease-claim").receipt.details
    started = _start(service, workflow_id, details, key="lease-start")
    assert started.receipt.details["execution_deadline"] == "2026-07-14T00:02:00Z"
    clock[0] += timedelta(minutes=2, seconds=1)

    _assert_error(
        "EXECUTION_LEASE_EXPIRED",
        lambda: _verify(service, workflow_id, details, key="late-applied"),
    )
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)["state"] == "apply_started"

    indeterminate = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "indeterminate",
            "observed_before": [BLANK],
            "actual_after": None,
            "recalculation": "recalculate",
        },
        idempotency_key="late-indeterminate",
    )
    assert indeterminate.receipt.state == "indeterminate"
    assert service.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)[
        "execution_deadline"
    ] == "2026-07-14T00:02:00Z"

    second_id, _ = _approved(service, prefix="lease-second-")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="lease-second-claim"),
    )


def test_verification_failed_keeps_workbook_quarantined(edit_service) -> None:
    service, _, _ = edit_service
    workflow_id, _ = _approved(service, prefix="failed-verification-")
    details = _claim(service, workflow_id, key="failed-verification-claim").receipt.details
    _start(service, workflow_id, details, key="failed-verification-start")
    failed = service.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        witness={
            "outcome": "applied",
            "observed_before": [BLANK],
            "actual_after": [BLANK],
            "recalculation": "recalculate",
        },
        idempotency_key="failed-verification-witness",
    )
    assert failed.receipt.state == "verification_failed"
    second_id, _ = _approved(service, prefix="after-failed-")
    _assert_error(
        "ACTIVE_EXECUTION_CONFLICT",
        lambda: _claim(service, second_id, key="after-failed-claim"),
    )
