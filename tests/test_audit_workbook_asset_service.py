from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import openpyxl
import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_asset_service import (
    InMemoryWorkbookAssetRepository,
    RAW_SNAPSHOT_SCHEMA_VERSION,
    RawWorkbookSnapshot,
    WorkbookAssetService,
    WorkbookAssetServiceError,
    uploaded_raw_snapshot_id,
)
from excel_to_skill.audit.workbook_snapshot_publication import (
    LocalImmutableWorkbookAssetStore,
)


PRINCIPAL = ServicePrincipal("tenant-a", "auditor-a")
OTHER_PRINCIPAL = ServicePrincipal("tenant-a", "auditor-b")


def _xlsx(value: str = "value") -> bytes:
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = value
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _service(tmp_path: Path) -> WorkbookAssetService:
    return WorkbookAssetService(
        InMemoryWorkbookAssetRepository(),
        LocalImmutableWorkbookAssetStore(tmp_path / "assets"),
    )


def test_upload_publishes_exact_public_snapshot_and_replays(tmp_path: Path) -> None:
    service = _service(tmp_path)
    content = _xlsx()

    first = service.upload(PRINCIPAL, content, "upload-one")
    replay = service.upload(PRINCIPAL, content, "upload-one")

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.snapshot == first.snapshot
    public = first.snapshot.to_public_dict()
    assert public == {
        "schema_version": RAW_SNAPSHOT_SCHEMA_VERSION,
        "workbook_id": first.snapshot.workbook_id,
        "raw_snapshot_id": first.snapshot.raw_snapshot_id,
        "workbook_sha256": first.snapshot.workbook_sha256,
        "size_bytes": len(content),
        "status": "stored",
        "origin_kind": "upload",
        "prepared_bundle_created": False,
        "created_at": first.snapshot.created_at,
    }
    assert "asset_ref" not in public
    assert "path" not in public


def test_same_idempotency_key_with_different_content_conflicts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.upload(PRINCIPAL, _xlsx("first"), "same-key")

    with pytest.raises(WorkbookAssetServiceError) as captured:
        service.upload(PRINCIPAL, _xlsx("second"), "same-key")

    assert (captured.value.code, captured.value.status_code) == (
        "IDEMPOTENCY_CONFLICT",
        409,
    )


def test_invalid_xlsx_is_rejected_before_an_asset_is_stored(tmp_path: Path) -> None:
    assets = LocalImmutableWorkbookAssetStore(tmp_path / "assets")
    service = WorkbookAssetService(InMemoryWorkbookAssetRepository(), assets)

    with pytest.raises(WorkbookAssetServiceError) as captured:
        service.upload(PRINCIPAL, b"not-an-xlsx", "invalid-upload")

    assert (captured.value.code, captured.value.status_code) == ("INVALID_WORKBOOK", 422)
    assert list((tmp_path / "assets" / "objects").iterdir()) == []


def test_exact_status_download_and_bound_provider_are_digest_bound(tmp_path: Path) -> None:
    service = _service(tmp_path)
    content = _xlsx()
    uploaded = service.upload(PRINCIPAL, content, "download-one").snapshot

    status = service.get_snapshot(
        PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id
    )
    download = service.download(
        PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id
    )
    provider = service.bind_source_provider(
        PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id
    )

    assert status == uploaded
    assert download.snapshot == uploaded
    assert download.content == content
    assert download.filename.endswith(".xlsx")
    assert download.filename.isascii()
    assert provider.read_bound_source(
        expected_sha256=uploaded.workbook_sha256,
        max_bytes=64 * 1024 * 1024,
    ) == content


def test_exact_snapshot_isolated_by_principal(tmp_path: Path) -> None:
    service = _service(tmp_path)
    uploaded = service.upload(PRINCIPAL, _xlsx(), "private-upload").snapshot

    with pytest.raises(WorkbookAssetServiceError) as captured:
        service.get_snapshot(
            OTHER_PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id
        )

    assert (captured.value.code, captured.value.status_code) == (
        "WORKBOOK_NOT_FOUND",
        404,
    )


def test_download_fails_closed_when_immutable_object_is_corrupt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    uploaded = service.upload(PRINCIPAL, _xlsx(), "corrupt-upload").snapshot
    target = tmp_path / "assets" / "objects" / f"{uploaded.workbook_sha256}.xlsx"
    original = target.read_bytes()
    target.write_bytes(b"x" * len(original))
    target.chmod(0o600)

    with pytest.raises(WorkbookAssetServiceError) as captured:
        service.download(PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id)

    assert (captured.value.code, captured.value.status_code) == (
        "ASSET_INTEGRITY_MISMATCH",
        503,
    )


def test_completed_replay_restores_a_missing_exact_object_but_rejects_corruption(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    content = _xlsx()
    uploaded = service.upload(PRINCIPAL, content, "replay-integrity").snapshot
    target = tmp_path / "assets" / "objects" / f"{uploaded.workbook_sha256}.xlsx"

    target.unlink()
    replay = service.upload(PRINCIPAL, content, "replay-integrity")
    assert replay.replayed is True
    assert service.download(
        PRINCIPAL, uploaded.workbook_id, uploaded.raw_snapshot_id
    ).content == content

    target.write_bytes(b"x" * len(content))
    target.chmod(0o600)
    with pytest.raises(WorkbookAssetServiceError) as captured:
        service.upload(PRINCIPAL, content, "replay-integrity")
    assert (captured.value.code, captured.value.status_code) == (
        "ASSET_INTEGRITY_MISMATCH",
        503,
    )


def test_slow_validation_and_store_complete_before_short_upload_claim(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 7, 14, tzinfo=timezone.utc)]
    delegate = LocalImmutableWorkbookAssetStore(tmp_path / "assets")

    class AdvancingStore:
        def put_if_absent(self, data: bytes):
            clock[0] += timedelta(seconds=2)
            return delegate.put_if_absent(data)

        def read_verified(self, asset):
            return delegate.read_verified(asset)

    service = WorkbookAssetService(
        InMemoryWorkbookAssetRepository(
            command_claim_ttl_seconds=1,
            now=lambda: clock[0],
        ),
        AdvancingStore(),
        now=lambda: clock[0],
    )

    uploaded = service.upload(PRINCIPAL, _xlsx(), "slow-before-claim")

    assert uploaded.replayed is False


def test_raw_snapshot_recomputes_its_canonical_upload_identity() -> None:
    workbook_id = "workbook-" + "a" * 48
    workbook_sha256 = "b" * 64
    expected = uploaded_raw_snapshot_id(
        workbook_id=workbook_id,
        workbook_sha256=workbook_sha256,
        size_bytes=12,
    )
    snapshot = RawWorkbookSnapshot(
        schema_version=RAW_SNAPSHOT_SCHEMA_VERSION,
        workbook_id=workbook_id,
        raw_snapshot_id=expected,
        workbook_sha256=workbook_sha256,
        size_bytes=12,
        status="stored",
        origin_kind="upload",
        prepared_bundle_created=False,
        created_at="2026-07-14T00:00:00Z",
    )
    assert snapshot.raw_snapshot_id == expected

    with pytest.raises(ValueError, match="identity"):
        RawWorkbookSnapshot(
            schema_version=RAW_SNAPSHOT_SCHEMA_VERSION,
            workbook_id=workbook_id,
            raw_snapshot_id="c" * 64,
            workbook_sha256=workbook_sha256,
            size_bytes=12,
            status="stored",
            origin_kind="upload",
            prepared_bundle_created=False,
            created_at="2026-07-14T00:00:00Z",
        )
