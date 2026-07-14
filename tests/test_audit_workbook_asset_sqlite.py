from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_asset_service import (
    RAW_SNAPSHOT_SCHEMA_VERSION,
    RawWorkbookSnapshot,
    StoredRawWorkbookSnapshot,
    WorkbookAssetClaimError,
    WorkbookAssetIdempotencyConflictError,
    uploaded_raw_snapshot_id,
    WorkbookAssetRepositoryError,
)
from excel_to_skill.audit.workbook_asset_sqlite import SQLiteWorkbookAssetRepository
from excel_to_skill.audit.workbook_snapshot_publication import StoredWorkbookAsset


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "auditor-a")
OTHER_PRINCIPAL = ServicePrincipal("tenant-a", "auditor-b")
WORKBOOK_ID = "workbook-" + "a" * 48
WORKBOOK_SHA256 = "c" * 64
RAW_SNAPSHOT_ID = uploaded_raw_snapshot_id(
    workbook_id=WORKBOOK_ID,
    workbook_sha256=WORKBOOK_SHA256,
    size_bytes=12,
)
COMMAND_SHA256 = "d" * 64
COMMAND_ID = "upload-command-" + "e" * 64
TOKEN_ONE = "f" * 43
TOKEN_TWO = "g" * 43


def _stored() -> StoredRawWorkbookSnapshot:
    snapshot = RawWorkbookSnapshot(
        schema_version=RAW_SNAPSHOT_SCHEMA_VERSION,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
        workbook_sha256=WORKBOOK_SHA256,
        size_bytes=12,
        status="stored",
        origin_kind="upload",
        prepared_bundle_created=False,
        created_at="2026-07-14T12:00:00Z",
    )
    return StoredRawWorkbookSnapshot(
        snapshot=snapshot,
        asset=StoredWorkbookAsset(
            asset_ref="workbook-asset:" + WORKBOOK_SHA256,
            workbook_sha256=WORKBOOK_SHA256,
            size_bytes=12,
        ),
    )


def _claim(
    repository: SQLiteWorkbookAssetRepository,
    *,
    token: str = TOKEN_ONE,
    key: str = "upload-one",
    principal: ServicePrincipal = PRINCIPAL,
):
    return repository.claim_upload(
        principal=principal,
        idempotency_key=key,
        command_sha256=COMMAND_SHA256,
        command_id=COMMAND_ID,
        workbook_id=WORKBOOK_ID,
        claim_token=token,
    )


def _publish(
    repository: SQLiteWorkbookAssetRepository,
    *,
    token: str,
    fence: int,
    key: str = "upload-one",
):
    return repository.publish_upload(
        principal=PRINCIPAL,
        idempotency_key=key,
        command_sha256=COMMAND_SHA256,
        command_id=COMMAND_ID,
        claim_token=token,
        claim_fence=fence,
        stored=_stored(),
    )


def test_atomic_publish_head_and_completed_replay_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "assets.sqlite3"
    repository = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)
    claim = _claim(repository)
    assert claim.state == "claimed"
    receipt = _publish(
        repository,
        token=claim.claim_token or "",
        fence=claim.claim_fence or 0,
    )

    restarted = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)
    replay = _claim(restarted, token=TOKEN_TWO)

    assert replay.state == "completed"
    assert replay.receipt == receipt
    assert restarted.get_snapshot(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
    ) == _stored()
    assert restarted.get_head_snapshot(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
    ) == _stored()
    assert restarted.get_snapshot(
        principal=OTHER_PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
    ) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_active_claim_is_pending_and_expired_claim_reclaims_with_new_fence(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    repository = SQLiteWorkbookAssetRepository(
        tmp_path / "assets.sqlite3",
        command_claim_ttl_seconds=1,
        now=lambda: clock[0],
    )
    first = _claim(repository)
    assert _claim(repository, token=TOKEN_TWO).state == "pending"

    clock[0] += timedelta(seconds=2)
    reclaimed = _claim(repository, token=TOKEN_TWO)
    assert (first.claim_fence, reclaimed.claim_fence) == (1, 2)

    with pytest.raises(WorkbookAssetClaimError):
        _publish(
            repository,
            token=first.claim_token or "",
            fence=first.claim_fence or 0,
        )
    _publish(
        repository,
        token=reclaimed.claim_token or "",
        fence=reclaimed.claim_fence or 0,
    )


def test_same_key_different_command_digest_conflicts(tmp_path: Path) -> None:
    repository = SQLiteWorkbookAssetRepository(
        tmp_path / "assets.sqlite3", now=lambda: NOW
    )
    _claim(repository)

    with pytest.raises(WorkbookAssetIdempotencyConflictError):
        repository.claim_upload(
            principal=PRINCIPAL,
            idempotency_key="upload-one",
            command_sha256="0" * 64,
            command_id=COMMAND_ID,
            workbook_id=WORKBOOK_ID,
            claim_token=TOKEN_TWO,
        )


def test_concurrent_same_key_has_one_owner_and_one_pending(tmp_path: Path) -> None:
    database = tmp_path / "assets.sqlite3"
    first = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)
    second = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda pair: _claim(pair[0], token=pair[1]),
                ((first, TOKEN_ONE), (second, TOKEN_TWO)),
            )
        )

    assert sorted(claim.state for claim in claims) == ["claimed", "pending"]


def test_mid_transaction_fault_rolls_back_workbook_snapshot_head_and_receipt(
    tmp_path: Path,
) -> None:
    class FailingRepository(SQLiteWorkbookAssetRepository):
        def _before_publish_commit(self, connection, stored) -> None:  # noqa: ANN001
            raise RuntimeError("injected publication fault")

    database = tmp_path / "assets.sqlite3"
    failing = FailingRepository(database, now=lambda: NOW)
    claim = _claim(failing)
    with pytest.raises(RuntimeError, match="injected publication fault"):
        _publish(
            failing,
            token=claim.claim_token or "",
            fence=claim.claim_fence or 0,
        )

    restarted = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)
    assert restarted.get_snapshot(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
        raw_snapshot_id=RAW_SNAPSHOT_ID,
    ) is None
    assert restarted.get_head_snapshot(
        principal=PRINCIPAL,
        workbook_id=WORKBOOK_ID,
    ) is None
    assert _claim(restarted, token=TOKEN_TWO).state == "pending"


def test_corrupt_private_asset_reference_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "assets.sqlite3"
    repository = SQLiteWorkbookAssetRepository(database, now=lambda: NOW)
    claim = _claim(repository)
    _publish(
        repository,
        token=claim.claim_token or "",
        fence=claim.claim_fence or 0,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE raw_snapshots SET asset_ref=?
            WHERE tenant_id=? AND subject_id=? AND workbook_id=? AND raw_snapshot_id=?
            """,
            (
                "workbook-asset:" + "0" * 64,
                PRINCIPAL.tenant_id,
                PRINCIPAL.subject_id,
                WORKBOOK_ID,
                RAW_SNAPSHOT_ID,
            ),
        )
        connection.commit()

    with pytest.raises(WorkbookAssetRepositoryError):
        repository.get_snapshot(
            principal=PRINCIPAL,
            workbook_id=WORKBOOK_ID,
            raw_snapshot_id=RAW_SNAPSHOT_ID,
        )
