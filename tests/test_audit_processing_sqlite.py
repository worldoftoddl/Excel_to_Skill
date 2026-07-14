from __future__ import annotations

import os
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from excel_to_skill.audit.model import json_sha256
from excel_to_skill.audit.processing import (
    PROCESSING_JOB_SCHEMA_VERSION,
    ProcessingConflictError,
    ProcessingFailure,
    ProcessingJob,
    ProcessingLeaseError,
    ProcessingProgress,
    ProcessingRepository,
    ProcessingRepositoryError,
    ProcessingResult,
    ProcessingScopeSelection,
    PublishedBundleRecord,
)
from excel_to_skill.audit.processing_sqlite import SQLiteProcessingRepository
from excel_to_skill.audit.service import ServicePrincipal


NOW = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "auditor-a")
OTHER_PRINCIPAL = ServicePrincipal("tenant-a", "auditor-b")
JOB_ID = "process-" + "a" * 48
WORKBOOK_ID = "workbook-" + "b" * 48
RAW_SNAPSHOT_ID = "c" * 64
WORKBOOK_SHA256 = "d" * 64
PROFILE_SHA256 = "e" * 64
START_DIGEST = "f" * 64
EXECUTION_DIGEST = "1" * 64
SCOPE_ID = "2" * 64
OTHER_SCOPE_ID = "3" * 64
TOKEN_ONE = "A" * 43
TOKEN_TWO = "B" * 43
TOKEN_THREE = "C" * 43
BUNDLE_ID = "bundle-" + "4" * 48
BUNDLE_SNAPSHOT_ID = "5" * 64
PACKAGE_MANIFEST_SHA256 = "6" * 64


def _planning_job(**changes: object) -> ProcessingJob:
    values: dict[str, object] = {
        "schema_version": PROCESSING_JOB_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "workbook_id": WORKBOOK_ID,
        "raw_snapshot_id": RAW_SNAPSHOT_ID,
        "workbook_sha256": WORKBOOK_SHA256,
        "profile_sha256": PROFILE_SHA256,
        "status": "planning",
        "scope_plan_sha256": None,
        "scope_plan": None,
        "selection": None,
        "progress": ProcessingProgress(),
        "result": None,
        "failure": None,
        "created_at": "2026-07-14T15:00:00Z",
        "updated_at": "2026-07-14T15:00:00Z",
    }
    values.update(changes)
    return ProcessingJob(**values)


def _plan() -> dict[str, object]:
    return {
        "schema_version": "audit_scope_plan.v1",
        "workbook": {"scope": {"kind": "workbook"}, "analyzable": True},
        "sheets": [
            {
                "scope": {"kind": "sheet", "sheet": "C", "id": SCOPE_ID},
                "analyzable": True,
            },
            {
                "scope": {
                    "kind": "sheet",
                    "sheet": "D",
                    "id": OTHER_SCOPE_ID,
                },
                "analyzable": True,
            },
        ],
    }


def _selection(plan: dict[str, object] | None = None) -> ProcessingScopeSelection:
    current = plan or _plan()
    return ProcessingScopeSelection(
        mode="selected_sheets",
        scope_plan_sha256=json_sha256(current),
        scope_ids=(SCOPE_ID,),
        sheets=("C",),
    )


def _claim_plan(
    repository: SQLiteProcessingRepository,
    *,
    principal: ServicePrincipal = PRINCIPAL,
    token: str = TOKEN_ONE,
    key: str = "start-one",
    digest: str = START_DIGEST,
    job: ProcessingJob | None = None,
):
    return repository.claim_planning(
        principal=principal,
        idempotency_key=key,
        command_sha256=digest,
        proposed_job=job or _planning_job(),
        claim_token=token,
    )


def _publish_plan(
    repository: SQLiteProcessingRepository,
    claim,
    *,
    principal: ServicePrincipal = PRINCIPAL,
    plan: dict[str, object] | None = None,
):
    current = plan or _plan()
    return repository.publish_plan(
        principal=principal,
        job_id=claim.job.job_id,
        claim_token=claim.claim_token or "",
        claim_fence=claim.claim_fence or 0,
        scope_plan=current,
        scope_plan_sha256=json_sha256(current),
    )


def _awaiting(repository: SQLiteProcessingRepository):
    claim = _claim_plan(repository)
    return _publish_plan(repository, claim)


def _claim_execution(
    repository: SQLiteProcessingRepository,
    *,
    token: str = TOKEN_TWO,
    key: str = "execute-one",
    digest: str = EXECUTION_DIGEST,
    selection: ProcessingScopeSelection | None = None,
):
    return repository.claim_execution(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        idempotency_key=key,
        command_sha256=digest,
        selection=selection or _selection(),
        claim_token=token,
    )


def _complete_progress(
    repository: SQLiteProcessingRepository,
    claim,
    *,
    status: str = "preparing",
):
    return repository.checkpoint(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        claim_token=claim.claim_token or "",
        claim_fence=claim.claim_fence or 0,
        status=status,
        progress=ProcessingProgress(1, 1, ()),
    )


def _result_and_bundle() -> tuple[ProcessingResult, PublishedBundleRecord]:
    selection_sha = _selection().selection_sha256
    result = ProcessingResult(
        bundle_id=BUNDLE_ID,
        snapshot_id=BUNDLE_SNAPSHOT_ID,
        package_manifest_sha256=PACKAGE_MANIFEST_SHA256,
        selection_sha256=selection_sha,
        sheet="C",
        aggregate_id=None,
        included_sheets=("C",),
        skipped_empty_sheets=(),
    )
    bundle = PublishedBundleRecord(
        bundle_id=BUNDLE_ID,
        snapshot_id=BUNDLE_SNAPSHOT_ID,
        package_manifest_sha256=PACKAGE_MANIFEST_SHA256,
        file_count=17,
        total_bytes=123_456,
        job_id=JOB_ID,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        workbook_sha256=WORKBOOK_SHA256,
        selection_sha256=selection_sha,
        sheet="C",
        aggregate_id=None,
        published_at="2026-07-14T15:00:00Z",
    )
    return result, bundle


def test_repository_structurally_implements_protocol_and_uses_private_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "processing.sqlite3"
    repository = SQLiteProcessingRepository(database, now=lambda: NOW)

    assert isinstance(repository, ProcessingRepository)
    assert repository.database_path == database
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_lifecycle_restart_and_exact_replays_include_bundle_counts(tmp_path: Path) -> None:
    database = tmp_path / "processing.sqlite3"
    repository = SQLiteProcessingRepository(database, now=lambda: NOW)
    planning = _claim_plan(repository)
    assert planning.state == "claimed" and planning.claim_fence == 1
    assert _claim_plan(repository, token=TOKEN_TWO).state == "pending"

    awaiting = _publish_plan(repository, planning)
    assert awaiting.status == "awaiting_scope"
    assert _claim_plan(repository, token=TOKEN_THREE).state == "current"

    execution = _claim_execution(repository)
    assert execution.state == "claimed" and execution.claim_fence == 2
    assert _claim_execution(repository, token=TOKEN_THREE).state == "pending"
    _complete_progress(repository, execution)
    result, bundle = _result_and_bundle()
    published = repository.publish_bundle(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        claim_token=execution.claim_token or "",
        claim_fence=execution.claim_fence or 0,
        result=result,
        bundle=bundle,
    )
    assert published.status == "published"

    restarted = SQLiteProcessingRepository(database, now=lambda: NOW)
    assert restarted.get_job(principal=PRINCIPAL, job_id=JOB_ID) == published
    assert restarted.get_bundle(principal=PRINCIPAL, bundle_id=BUNDLE_ID) == bundle
    assert restarted.bundle_count(PRINCIPAL) == 1
    assert _claim_plan(restarted, token=TOKEN_THREE).state == "current"
    replay = _claim_execution(restarted, token=TOKEN_THREE)
    assert replay.state == "current" and replay.job == published


def test_start_idempotency_digest_and_immutable_identity_conflicts(tmp_path: Path) -> None:
    repository = SQLiteProcessingRepository(
        tmp_path / "processing.sqlite3", now=lambda: NOW
    )
    _claim_plan(repository)

    with pytest.raises(ProcessingConflictError):
        _claim_plan(repository, token=TOKEN_TWO, digest="0" * 64)
    with pytest.raises(ProcessingConflictError):
        _claim_plan(
            repository,
            token=TOKEN_TWO,
            job=_planning_job(raw_snapshot_id="9" * 64),
        )


def test_expired_planning_lease_reclaims_with_fence_and_rejects_stale_owner(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    repository = SQLiteProcessingRepository(
        tmp_path / "processing.sqlite3",
        lease_ttl_seconds=1,
        now=lambda: clock[0],
    )
    first = _claim_plan(repository)
    clock[0] += timedelta(seconds=2)
    second = _claim_plan(repository, token=TOKEN_TWO)
    assert (first.claim_fence, second.claim_fence) == (1, 2)

    with pytest.raises(ProcessingLeaseError):
        _publish_plan(repository, first)
    assert _publish_plan(repository, second).status == "awaiting_scope"


def test_claim_ttl_starts_after_begin_immediate_lock_is_acquired(tmp_path: Path) -> None:
    database = tmp_path / "processing.sqlite3"
    repository = SQLiteProcessingRepository(database, lease_ttl_seconds=1)
    blocker = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_claim_plan, repository)
            time.sleep(1.2)
            blocker.commit()
            claim = future.result(timeout=5)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()

    assert claim.state == "claimed"
    refreshed = repository.checkpoint(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        claim_token=claim.claim_token or "",
        claim_fence=claim.claim_fence or 0,
        status="planning",
        progress=ProcessingProgress(),
    )
    assert refreshed.status == "planning"


def test_execution_binding_conflicts_and_expired_fence_reclaim(tmp_path: Path) -> None:
    clock = [NOW]
    repository = SQLiteProcessingRepository(
        tmp_path / "processing.sqlite3",
        lease_ttl_seconds=1,
        now=lambda: clock[0],
    )
    _awaiting(repository)
    first = _claim_execution(repository)
    with pytest.raises(ProcessingConflictError):
        _claim_execution(repository, token=TOKEN_THREE, key="another-key")
    with pytest.raises(ProcessingConflictError):
        _claim_execution(repository, token=TOKEN_THREE, digest="7" * 64)
    with pytest.raises(ProcessingConflictError):
        _claim_execution(
            repository,
            token=TOKEN_THREE,
            selection=ProcessingScopeSelection(
                "selected_sheets",
                json_sha256(_plan()),
                (OTHER_SCOPE_ID,),
                ("D",),
            ),
        )

    clock[0] += timedelta(seconds=2)
    reclaimed = _claim_execution(repository, token=TOKEN_THREE)
    assert (first.claim_fence, reclaimed.claim_fence) == (2, 3)
    with pytest.raises(ProcessingLeaseError):
        _complete_progress(repository, first)
    with pytest.raises(ProcessingLeaseError):
        repository.fail(
            principal=PRINCIPAL,
            job_id=JOB_ID,
            claim_token=first.claim_token or "",
            claim_fence=first.claim_fence or 0,
            failure=ProcessingFailure("PREPARATION_FAILED", "preparing", False),
        )
    assert _complete_progress(repository, reclaimed).progress.completed_scopes == 1


def test_checkpoint_renews_lease_and_enforces_forward_state_transitions(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    repository = SQLiteProcessingRepository(
        tmp_path / "processing.sqlite3",
        lease_ttl_seconds=2,
        now=lambda: clock[0],
    )
    planning = _claim_plan(repository)
    repository.checkpoint(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        claim_token=planning.claim_token or "",
        claim_fence=planning.claim_fence or 0,
        status="planning",
        progress=ProcessingProgress(),
    )
    with pytest.raises(ProcessingConflictError):
        repository.checkpoint(
            principal=PRINCIPAL,
            job_id=JOB_ID,
            claim_token=planning.claim_token or "",
            claim_fence=planning.claim_fence or 0,
            status="preparing",
            progress=ProcessingProgress(),
        )
    awaiting = _publish_plan(repository, planning)
    assert awaiting.status == "awaiting_scope"
    execution = _claim_execution(repository)
    clock[0] += timedelta(seconds=1)
    aggregated = _complete_progress(repository, execution, status="aggregating")
    assert aggregated.status == "aggregating"
    clock[0] += timedelta(seconds=1)
    assert _claim_execution(repository, token=TOKEN_THREE).state == "pending"
    with pytest.raises(ProcessingConflictError):
        repository.checkpoint(
            principal=PRINCIPAL,
            job_id=JOB_ID,
            claim_token=execution.claim_token or "",
            claim_fence=execution.claim_fence or 0,
            status="preparing",
            progress=ProcessingProgress(1, 1, ()),
        )


def test_fail_is_terminal_principal_scoped_and_never_publishes_bundle(
    tmp_path: Path,
) -> None:
    repository = SQLiteProcessingRepository(
        tmp_path / "processing.sqlite3", now=lambda: NOW
    )
    _awaiting(repository)
    execution = _claim_execution(repository)
    failed = repository.fail(
        principal=PRINCIPAL,
        job_id=JOB_ID,
        claim_token=execution.claim_token or "",
        claim_fence=execution.claim_fence or 0,
        failure=ProcessingFailure("PREPARATION_FAILED", "preparing", False),
    )

    assert failed.status == "failed"
    assert repository.bundle_count(PRINCIPAL) == 0
    assert repository.get_bundle(principal=PRINCIPAL, bundle_id=BUNDLE_ID) is None
    assert repository.get_job(principal=OTHER_PRINCIPAL, job_id=JOB_ID) is None
    assert repository.get_bundle(
        principal=OTHER_PRINCIPAL, bundle_id=BUNDLE_ID
    ) is None
    assert repository.bundle_count(OTHER_PRINCIPAL) == 0
    assert _claim_execution(repository, token=TOKEN_THREE).state == "current"


def test_concurrent_start_claim_has_one_owner_and_one_pending(tmp_path: Path) -> None:
    database = tmp_path / "processing.sqlite3"
    first = SQLiteProcessingRepository(database, now=lambda: NOW)
    second = SQLiteProcessingRepository(database, now=lambda: NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda pair: _claim_plan(pair[0], token=pair[1]),
                ((first, TOKEN_ONE), (second, TOKEN_TWO)),
            )
        )

    assert sorted(claim.state for claim in claims) == ["claimed", "pending"]


def test_bundle_fault_rolls_back_record_and_published_job_state(tmp_path: Path) -> None:
    class FailingRepository(SQLiteProcessingRepository):
        def _before_bundle_commit(self, connection, job, bundle) -> None:  # noqa: ANN001
            raise RuntimeError("injected bundle publication fault")

    database = tmp_path / "processing.sqlite3"
    repository = FailingRepository(database, now=lambda: NOW)
    _awaiting(repository)
    execution = _claim_execution(repository)
    _complete_progress(repository, execution)
    result, bundle = _result_and_bundle()

    with pytest.raises(RuntimeError, match="injected bundle publication fault"):
        repository.publish_bundle(
            principal=PRINCIPAL,
            job_id=JOB_ID,
            claim_token=execution.claim_token or "",
            claim_fence=execution.claim_fence or 0,
            result=result,
            bundle=bundle,
        )

    restarted = SQLiteProcessingRepository(database, now=lambda: NOW)
    job = restarted.get_job(principal=PRINCIPAL, job_id=JOB_ID)
    assert job is not None and job.status == "preparing" and job.result is None
    assert restarted.get_bundle(principal=PRINCIPAL, bundle_id=BUNDLE_ID) is None
    assert restarted.bundle_count(PRINCIPAL) == 0


def test_sidecar_security_failure_rolls_back_before_commit(tmp_path: Path) -> None:
    class FaultySidecarRepository(SQLiteProcessingRepository):
        fail_sidecars = False

        def _secure_sidecars(self) -> None:
            if self.fail_sidecars:
                raise ProcessingRepositoryError("injected sidecar failure")
            super()._secure_sidecars()

    repository = FaultySidecarRepository(tmp_path / "processing.sqlite3", now=lambda: NOW)
    repository.fail_sidecars = True
    with pytest.raises(ProcessingRepositoryError, match="injected sidecar failure"):
        _claim_plan(repository)
    repository.fail_sidecars = False
    assert repository.get_job(principal=PRINCIPAL, job_id=JOB_ID) is None


def test_strict_json_decoding_rejects_tampered_job_and_bundle_rows(tmp_path: Path) -> None:
    database = tmp_path / "processing.sqlite3"
    repository = SQLiteProcessingRepository(database, now=lambda: NOW)
    _awaiting(repository)
    with sqlite3.connect(database) as connection:
        raw = connection.execute("SELECT job_json FROM processing_jobs").fetchone()[0]
        connection.execute(
            "UPDATE processing_jobs SET job_json=?",
            (raw[:-1] + ',"unknown":true}',),
        )
        connection.commit()
    with pytest.raises(ProcessingRepositoryError):
        repository.get_job(principal=PRINCIPAL, job_id=JOB_ID)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_relative_and_symlink_database_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProcessingRepositoryError):
        SQLiteProcessingRepository(Path("relative.sqlite3"))
    real = tmp_path / "real.sqlite3"
    real.touch()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(real)
    with pytest.raises(ProcessingRepositoryError):
        SQLiteProcessingRepository(linked)
