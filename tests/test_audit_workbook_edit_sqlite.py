from __future__ import annotations

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from threading import Event

import openpyxl
import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_edit_service import (
    InMemoryWorkbookSessionRepository,
    WorkbookEditConflictError,
    WorkbookEditReceipt,
    WorkbookEditRepositoryError,
    WorkbookEditService,
    WorkbookEditServiceError,
    WorkbookSessionBinding,
)
from excel_to_skill.audit.workbook_edit_sqlite import SQLiteWorkbookEditRepository
from excel_to_skill.audit.workbook_snapshot_publication import (
    AcquiredWorkbook,
    LocalImmutableWorkbookAssetStore,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
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


def _service(
    repository: SQLiteWorkbookEditRepository,
    sessions: InMemoryWorkbookSessionRepository,
    *,
    publication: bool = False,
    assets: Path | None = None,
    reacquirer=None,
) -> WorkbookEditService:
    return WorkbookEditService(
        sessions=sessions,
        edits=repository,
        now=lambda: NOW,
        **(
            {
                "saved_workbooks": reacquirer,
                "workbook_assets": LocalImmutableWorkbookAssetStore(assets),
            }
            if publication
            else {}
        ),
    )


def _propose(service: WorkbookEditService, *, key: str):
    return service.propose(
        principal=PRINCIPAL,
        session_id="office-session-a",
        proposal_input={"changes": CHANGES},
        idempotency_key=key,
    )


def _approved(
    service: WorkbookEditService,
    *,
    prefix: str,
    revision_id: str = "revision-a",
):
    proposed = _propose(service, key=prefix + "propose")
    workflow_id = proposed.receipt.workflow_id
    previewed = service.preview(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_input={
            "office_revision_id": revision_id,
            "worksheet_id": "worksheet-a",
            "before": [BLANK],
        },
        idempotency_key=prefix + "preview",
    )
    preview = previewed.receipt.details["preview"]
    service.approve(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        preview_sha256=preview["preview_sha256"],
        confirmed=True,
        idempotency_key=prefix + "approve",
    )
    return workflow_id


def _claim(service: WorkbookEditService, workflow_id: str, *, key: str):
    return service.claim_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        session_id="office-session-a",
        idempotency_key=key,
    )


def _start(service: WorkbookEditService, workflow_id: str, details: dict, *, key: str):
    return service.mark_apply_started(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        challenge=details["challenge"],
        idempotency_key=key,
    )


def _verify(service: WorkbookEditService, workflow_id: str, details: dict, *, key: str):
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


def _receipt(command_id: str, workflow, *, details=None) -> WorkbookEditReceipt:
    binding = workflow.binding
    return WorkbookEditReceipt(
        command_id=command_id,
        workflow_id=workflow.workflow_id,
        session_id=binding.session_id,
        bundle_id=binding.bundle_id,
        snapshot_id=binding.snapshot_id,
        workbook_sha256=binding.workbook_sha256,
        revision_id=binding.revision_id,
        sheet=binding.sheet,
        state=workflow.state,
        details={} if details is None else details,
    )


class _SavedWorkbookReacquirer:
    def __init__(self) -> None:
        self.content = _saved_workbook_bytes()
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
            provider_revision_id="revision-b",
            predecessor_revision_id=base_revision_id,
            worksheet_id=expected_worksheet_id,
            workbook_instance_id="workbook-instance-a",
            content=self.content,
        )


class _BlockingSavedWorkbookReacquirer(_SavedWorkbookReacquirer):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def reacquire_saved_workbook(self, **kwargs) -> AcquiredWorkbook:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().reacquire_saved_workbook(**kwargs)


def _saved_workbook_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "매출채권"
    worksheet["A1"] = "승인됨"
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_service_lifecycle_and_receipt_replay_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "workbook-edits.sqlite3"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    service = _service(repository, sessions)

    workflow_id = _approved(service, prefix="lifecycle-")
    claimed = _claim(service, workflow_id, key="lifecycle-claim")
    details = claimed.receipt.details
    _start(service, workflow_id, details, key="lifecycle-start")
    verified = _verify(service, workflow_id, details, key="lifecycle-verify")
    assert verified.receipt.state == "session_verified"

    restarted_repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    stored = restarted_repository.get_workflow(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
    )
    assert stored is not None
    assert stored.state == "session_verified"
    assert stored.verification == verified.receipt.details["verification"]
    restarted_service = _service(restarted_repository, sessions)
    replay = _propose(restarted_service, key="lifecycle-propose")
    assert replay.replayed is True
    assert replay.receipt.workflow_id == workflow_id
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_two_repository_instances_serialize_claim_and_fence_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.sqlite3"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    first_repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    second_repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    first_service = _service(first_repository, sessions)
    second_service = _service(second_repository, sessions)
    first_workflow = _approved(first_service, prefix="race-a-")
    second_workflow = _approved(first_service, prefix="race-b-")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_claim, first_service, first_workflow, key="race-a-claim"),
            pool.submit(_claim, second_service, second_workflow, key="race-b-claim"),
        ]
    successes = []
    conflicts = []
    for future in futures:
        try:
            successes.append(future.result())
        except WorkbookEditServiceError as error:
            conflicts.append(error)
    assert len(successes) == 1
    assert [error.code for error in conflicts] == ["ACTIVE_EXECUTION_CONFLICT"]
    winner = successes[0]
    assert winner.receipt.details["fence"] == 1

    winner_details = winner.receipt.details
    first_service.abort_claim(
        principal=PRINCIPAL,
        workflow_id=winner.receipt.workflow_id,
        execution_id=winner_details["execution_id"],
        fence=winner_details["fence"],
        challenge=winner_details["challenge"],
        idempotency_key="race-winner-abort",
    )

    restarted = _service(SQLiteWorkbookEditRepository(database, now=lambda: NOW), sessions)
    third_workflow = _approved(restarted, prefix="race-c-")
    third = _claim(restarted, third_workflow, key="race-c-claim")
    assert third.receipt.details["fence"] == 2


def test_snapshot_publication_cas_releases_lease_and_persists_new_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "publication.sqlite3"
    assets = tmp_path / "assets"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    reacquirer = _SavedWorkbookReacquirer()
    repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    service = _service(
        repository,
        sessions,
        publication=True,
        assets=assets,
        reacquirer=reacquirer,
    )
    workflow_id = _approved(service, prefix="publication-")
    claimed = _claim(service, workflow_id, key="publication-claim")
    details = claimed.receipt.details
    _start(service, workflow_id, details, key="publication-start")
    _verify(service, workflow_id, details, key="publication-verify")
    manifest = details["apply_manifest"]

    restarted_repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    restarted_service = _service(
        restarted_repository,
        sessions,
        publication=True,
        assets=assets,
        reacquirer=reacquirer,
    )
    published = restarted_service.publish_verified_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        manifest_ref=manifest["manifest_ref"],
        manifest_sha256=manifest["manifest_sha256"],
        idempotency_key="publication-snapshot",
    )
    publication = published.receipt.details["publication"]
    assert publication["base_snapshot_id"] == _binding().snapshot_id
    assert publication["revision_id"] == "revision-b"
    assert reacquirer.calls == 1

    with pytest.raises(WorkbookEditConflictError):
        restarted_repository.assert_snapshot_head(binding=_binding())
    current_binding = _binding(
        snapshot_id=publication["snapshot_id"],
        workbook_sha256=publication["workbook_sha256"],
        revision_id=publication["revision_id"],
    )
    restarted_repository.assert_snapshot_head(binding=current_binding)

    replay_service = _service(
        SQLiteWorkbookEditRepository(database, now=lambda: NOW),
        sessions,
        publication=True,
        assets=assets,
        reacquirer=reacquirer,
    )
    replay = replay_service.publish_verified_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        manifest_ref=manifest["manifest_ref"],
        manifest_sha256=manifest["manifest_sha256"],
        idempotency_key="publication-snapshot",
    )
    assert replay.replayed is True
    assert replay.receipt.to_dict() == published.receipt.to_dict()
    assert reacquirer.calls == 1

    sessions.register(binding=current_binding)
    current_service = _service(
        SQLiteWorkbookEditRepository(database, now=lambda: NOW), sessions
    )
    next_workflow = _approved(
        current_service,
        prefix="after-publication-",
        revision_id=publication["revision_id"],
    )
    next_claim = _claim(current_service, next_workflow, key="after-publication-claim")
    assert next_claim.receipt.details["fence"] == 2


def test_callback_failure_rolls_back_workflow_active_lock_fence_and_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atomic.sqlite3"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    repository = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    service = _service(repository, sessions)
    workflow_id = _approved(service, prefix="atomic-")
    before = repository.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    assert before is not None and before.state == "approved"

    command = repository.claim_command(
        principal=PRINCIPAL,
        idempotency_key="atomic-direct-claim",
        command_sha256="c" * 64,
        command_id="atomic-command",
        claim_token="atomic-token",
    )
    assert command.state == "claimed"

    def fail_receipt(_workflow):
        raise RuntimeError("copy callback failed")

    with pytest.raises(RuntimeError, match="copy callback failed"):
        repository.claim_execution(
            principal=PRINCIPAL,
            idempotency_key="atomic-direct-claim",
            command_sha256="c" * 64,
            command_id="atomic-command",
            claim_token="atomic-token",
            workflow_id=workflow_id,
            expected_version=before.version,
            execution_id="atomic-execution",
            challenge="atomic-challenge",
            publication_required=False,
            manifest_factory=lambda fence: {"fence": fence},
            receipt_factory=fail_receipt,
        )

    restarted = SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    after = restarted.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    assert after == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM active_workbooks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM workbook_fences").fetchone()[0] == 0
        command_row = connection.execute(
            "SELECT state, claim_token FROM commands WHERE idempotency_key=?",
            ("atomic-direct-claim",),
        ).fetchone()
    assert command_row == ("pending", "atomic-token")

    restarted.abort_command(
        principal=PRINCIPAL,
        idempotency_key="atomic-direct-claim",
        command_sha256="c" * 64,
        command_id="atomic-command",
        claim_token="atomic-token",
    )
    replacement = restarted.claim_command(
        principal=PRINCIPAL,
        idempotency_key="atomic-replacement",
        command_sha256="d" * 64,
        command_id="replacement-command",
        claim_token="replacement-token",
    )
    assert replacement.state == "claimed"
    receipt = restarted.claim_execution(
        principal=PRINCIPAL,
        idempotency_key="atomic-replacement",
        command_sha256="d" * 64,
        command_id="replacement-command",
        claim_token="replacement-token",
        workflow_id=workflow_id,
        expected_version=before.version,
        execution_id="replacement-execution",
        challenge="replacement-challenge",
        publication_required=False,
        manifest_factory=lambda fence: {"fence": fence},
        receipt_factory=lambda workflow: _receipt(
            "replacement-command",
            workflow,
            details={"fence": workflow.fence},
        ),
    )
    assert receipt.details["fence"] == 1


def test_database_rejects_symlinks_and_enforces_private_file_mode(tmp_path: Path) -> None:
    database = tmp_path / "private.sqlite3"
    SQLiteWorkbookEditRepository(database, now=lambda: NOW)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(database)
    except OSError:
        pytest.skip("symbolic links are not available")
    with pytest.raises(WorkbookEditRepositoryError):
        SQLiteWorkbookEditRepository(link, now=lambda: NOW)


def test_expired_command_claim_is_fenced_and_reclaimable(tmp_path: Path) -> None:
    clock = [NOW]
    repository = SQLiteWorkbookEditRepository(
        tmp_path / "claim-lease.sqlite3",
        command_claim_ttl_seconds=1,
        now=lambda: clock[0],
    )
    first = repository.claim_command(
        principal=PRINCIPAL,
        idempotency_key="lease-key",
        command_sha256="a" * 64,
        command_id="lease-command",
        claim_token="first-token",
    )
    pending = repository.claim_command(
        principal=PRINCIPAL,
        idempotency_key="lease-key",
        command_sha256="a" * 64,
        command_id="lease-command",
        claim_token="second-token",
    )
    assert first.state == "claimed"
    assert pending.state == "pending"

    clock[0] += timedelta(seconds=2)
    reclaimed = repository.claim_command(
        principal=PRINCIPAL,
        idempotency_key="lease-key",
        command_sha256="a" * 64,
        command_id="lease-command",
        claim_token="replacement-token",
    )
    assert reclaimed.state == "claimed"
    with pytest.raises(WorkbookEditRepositoryError):
        repository.abort_command(
            principal=PRINCIPAL,
            idempotency_key="lease-key",
            command_sha256="a" * 64,
            command_id="lease-command",
            claim_token="first-token",
        )
    repository.abort_command(
        principal=PRINCIPAL,
        idempotency_key="lease-key",
        command_sha256="a" * 64,
        command_id="lease-command",
        claim_token="replacement-token",
    )


def test_different_publication_keys_share_one_durable_execution_claim(tmp_path: Path) -> None:
    database = tmp_path / "publication-race.sqlite3"
    assets = tmp_path / "assets-race"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    reacquirer = _BlockingSavedWorkbookReacquirer()
    first_service = _service(
        SQLiteWorkbookEditRepository(database, now=lambda: NOW),
        sessions,
        publication=True,
        assets=assets,
        reacquirer=reacquirer,
    )
    workflow_id = _approved(first_service, prefix="publication-race-")
    claimed = _claim(first_service, workflow_id, key="publication-race-claim")
    details = claimed.receipt.details
    _start(first_service, workflow_id, details, key="publication-race-start")
    _verify(first_service, workflow_id, details, key="publication-race-verify")
    manifest = details["apply_manifest"]
    second_service = _service(
        SQLiteWorkbookEditRepository(database, now=lambda: NOW),
        sessions,
        publication=True,
        assets=assets,
        reacquirer=reacquirer,
    )

    def publish(service, key):
        return service.publish_verified_snapshot(
            principal=PRINCIPAL,
            workflow_id=workflow_id,
            execution_id=details["execution_id"],
            manifest_ref=manifest["manifest_ref"],
            manifest_sha256=manifest["manifest_sha256"],
            idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(publish, first_service, "publication-race-first")
        assert reacquirer.entered.wait(timeout=5)
        with pytest.raises(WorkbookEditServiceError) as caught:
            publish(second_service, "publication-race-second")
        assert caught.value.code == "COMMAND_IN_PROGRESS"
        reacquirer.release.set()
        first_result = first_future.result(timeout=5)

    assert first_result.receipt.details["publication"]["asset_persisted"] is True
    assert reacquirer.calls == 1


def test_expired_publication_claim_is_recoverable_without_replaying_manifest(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    database = tmp_path / "publication-claim-recovery.sqlite3"
    sessions = InMemoryWorkbookSessionRepository([_binding()])
    reacquirer = _SavedWorkbookReacquirer()
    repository = SQLiteWorkbookEditRepository(database, now=lambda: clock[0])
    service = WorkbookEditService(
        sessions=sessions,
        edits=repository,
        saved_workbooks=reacquirer,
        workbook_assets=LocalImmutableWorkbookAssetStore(tmp_path / "assets-recovery"),
        now=lambda: clock[0],
    )
    workflow_id = _approved(service, prefix="publication-recovery-")
    claimed = _claim(service, workflow_id, key="publication-recovery-claim")
    details = claimed.receipt.details
    _start(service, workflow_id, details, key="publication-recovery-start")
    _verify(service, workflow_id, details, key="publication-recovery-verify")
    repository.claim_snapshot_publication(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        fence=details["fence"],
        publication_claim_token="abandoned-publication-token",
        claim_expires_at=(NOW + timedelta(seconds=1))
        .isoformat()
        .replace("+00:00", "Z"),
    )

    clock[0] += timedelta(seconds=2)
    manifest = details["apply_manifest"]
    published = service.publish_verified_snapshot(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=details["execution_id"],
        manifest_ref=manifest["manifest_ref"],
        manifest_sha256=manifest["manifest_sha256"],
        idempotency_key="publication-recovery-publish",
    )
    assert published.receipt.details["publication"]["asset_persisted"] is True
    assert reacquirer.calls == 1
