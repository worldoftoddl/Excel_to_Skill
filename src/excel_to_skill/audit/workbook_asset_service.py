"""Durable, principal-scoped ingestion of immutable raw workbook snapshots.

This boundary is deliberately separate from prepared audit bundles and Office edit snapshot
heads.  A caller supplies only authenticated principal context, workbook bytes, and an
idempotency key.  The service validates the XLSX before storing it, verifies the immutable-store
readback, and then asks the repository to publish the workbook, exact raw snapshot, current raw
head, and completed command receipt atomically.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Callable, Literal, Protocol, runtime_checkable

import openpyxl

from .service import ServicePrincipal
from .workbook_snapshot_publication import (
    ImmutableWorkbookAssetStore,
    MAX_PUBLISHED_WORKBOOK_BYTES,
    StoredWorkbookAsset,
    WorkbookSnapshotPublicationError,
)
from .workbook_source import BoundWorkbookSourceProvider, WorkbookSourceError
from .xlsx_safety import XlsxSafetyError, validate_xlsx_archive


RAW_SNAPSHOT_SCHEMA_VERSION = "audit_raw_workbook_snapshot.v1"
MAX_WORKBOOK_ASSET_BYTES = MAX_PUBLISHED_WORKBOOK_BYTES

_WORKBOOK_ID_RE = re.compile(r"\Aworkbook-[0-9a-f]{48}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")
_CREATED_AT_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_ERRORS: dict[str, tuple[str, int]] = {
    "INVALID_REQUEST": ("The workbook upload request is invalid.", 400),
    "SOURCE_LIMIT_EXCEEDED": ("The workbook exceeds the upload safety limit.", 413),
    "INVALID_WORKBOOK": ("The uploaded content is not a safe readable XLSX workbook.", 422),
    "IDEMPOTENCY_CONFLICT": ("The idempotency key was already used for different content.", 409),
    "COMMAND_IN_PROGRESS": ("The workbook upload command is already in progress.", 409),
    "STALE_UPLOAD_CLAIM": ("The workbook upload claim is no longer current.", 409),
    "WORKBOOK_NOT_FOUND": ("The exact workbook snapshot was not found.", 404),
    "ASSET_INTEGRITY_MISMATCH": ("The stored workbook failed integrity verification.", 503),
    "SERVICE_UNAVAILABLE": ("The workbook asset service is unavailable.", 503),
}


class WorkbookAssetServiceError(RuntimeError):
    """A fixed, locator-free error safe to expose through an HTTP adapter."""

    def __init__(self, code: str, message: str | None = None, status_code: int | None = None):
        safe_code = code if code in _ERRORS else "SERVICE_UNAVAILABLE"
        safe_message, safe_status = _ERRORS[safe_code]
        self.code = safe_code
        self.message = safe_message
        self.status_code = safe_status
        super().__init__(self.message)


class WorkbookAssetRepositoryError(RuntimeError):
    """The private raw-workbook catalog could not safely complete an operation."""


class WorkbookAssetIdempotencyConflictError(WorkbookAssetRepositoryError):
    """An idempotency key is already bound to another command digest."""


class WorkbookAssetClaimError(WorkbookAssetRepositoryError):
    """A pending upload claim is missing, expired, or owned by another worker."""


def _service_error(code: str) -> WorkbookAssetServiceError:
    return WorkbookAssetServiceError(code)


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 identifier")
    return value


def _workbook_id(value: object) -> str:
    if not isinstance(value, str) or _WORKBOOK_ID_RE.fullmatch(value) is None:
        raise ValueError("workbook_id must be an opaque workbook identifier")
    return value


def _created_at(value: object) -> str:
    if not isinstance(value, str) or _CREATED_AT_RE.fullmatch(value) is None:
        raise ValueError("created_at must be canonical UTC seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("created_at must be canonical UTC seconds") from None
    if _format_time(parsed) != value:
        raise ValueError("created_at must be canonical UTC seconds")
    return value


@dataclass(frozen=True, slots=True)
class RawWorkbookSnapshot:
    """The complete public representation of one immutable uploaded workbook snapshot."""

    schema_version: str
    workbook_id: str
    raw_snapshot_id: str
    workbook_sha256: str
    size_bytes: int
    status: str
    origin_kind: str
    prepared_bundle_created: bool
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != RAW_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("raw snapshot schema is invalid")
        _workbook_id(self.workbook_id)
        _sha256(self.raw_snapshot_id, field_name="raw_snapshot_id")
        _sha256(self.workbook_sha256, field_name="workbook_sha256")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or not 1 <= self.size_bytes <= MAX_WORKBOOK_ASSET_BYTES
        ):
            raise ValueError("size_bytes is invalid")
        if self.status != "stored" or self.origin_kind != "upload":
            raise ValueError("raw snapshot state is invalid")
        if self.prepared_bundle_created is not False:
            raise ValueError("uploaded raw snapshot cannot claim a prepared bundle")
        if self.raw_snapshot_id != uploaded_raw_snapshot_id(
            workbook_id=self.workbook_id,
            workbook_sha256=self.workbook_sha256,
            size_bytes=self.size_bytes,
        ):
            raise ValueError("raw snapshot identity is invalid")
        _created_at(self.created_at)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workbook_id": self.workbook_id,
            "raw_snapshot_id": self.raw_snapshot_id,
            "workbook_sha256": self.workbook_sha256,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "origin_kind": self.origin_kind,
            "prepared_bundle_created": self.prepared_bundle_created,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class WorkbookAssetSubmission:
    replayed: bool
    snapshot: RawWorkbookSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.replayed, bool) or not isinstance(
            self.snapshot, RawWorkbookSnapshot
        ):
            raise TypeError("workbook asset submission is invalid")


@dataclass(frozen=True, slots=True)
class WorkbookAssetDownload:
    snapshot: RawWorkbookSnapshot
    content: bytes = field(repr=False)
    filename: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, RawWorkbookSnapshot):
            raise TypeError("snapshot must be a RawWorkbookSnapshot")
        if (
            not isinstance(self.content, bytes)
            or len(self.content) != self.snapshot.size_bytes
            or hashlib.sha256(self.content).hexdigest() != self.snapshot.workbook_sha256
        ):
            raise ValueError("download content does not match its snapshot")
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or len(self.filename) > 128
            or not self.filename.isascii()
            or any(character in self.filename for character in '\r\n/\\"')
            or not self.filename.endswith(".xlsx")
        ):
            raise ValueError("download filename is invalid")


@dataclass(frozen=True, slots=True)
class StoredRawWorkbookSnapshot:
    """Repository record joining the public snapshot to its private immutable-store pointer."""

    snapshot: RawWorkbookSnapshot
    asset: StoredWorkbookAsset = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, RawWorkbookSnapshot) or not isinstance(
            self.asset, StoredWorkbookAsset
        ):
            raise TypeError("stored raw workbook snapshot is invalid")
        if (
            self.asset.workbook_sha256 != self.snapshot.workbook_sha256
            or self.asset.size_bytes != self.snapshot.size_bytes
        ):
            raise ValueError("stored asset does not match raw snapshot")


@dataclass(frozen=True, slots=True)
class WorkbookAssetCommandClaim:
    state: Literal["claimed", "pending", "completed"]
    claim_token: str | None = field(default=None, repr=False)
    claim_fence: int | None = None
    receipt: RawWorkbookSnapshot | None = None

    def __post_init__(self) -> None:
        if self.state == "claimed":
            if (
                not isinstance(self.claim_token, str)
                or _TOKEN_RE.fullmatch(self.claim_token) is None
                or not isinstance(self.claim_fence, int)
                or isinstance(self.claim_fence, bool)
                or self.claim_fence < 1
                or self.receipt is not None
            ):
                raise ValueError("claimed upload command is invalid")
            return
        if self.state == "pending":
            if (
                self.claim_token is not None
                or self.claim_fence is not None
                or self.receipt is not None
            ):
                raise ValueError("pending upload command is invalid")
            return
        if self.state == "completed":
            if (
                self.claim_token is not None
                or self.claim_fence is not None
                or not isinstance(self.receipt, RawWorkbookSnapshot)
            ):
                raise ValueError("completed upload command is invalid")
            return
        raise ValueError("upload command state is invalid")


@runtime_checkable
class WorkbookAssetRepository(Protocol):
    def claim_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        workbook_id: str,
        claim_token: str,
    ) -> WorkbookAssetCommandClaim: ...

    def publish_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
        stored: StoredRawWorkbookSnapshot,
    ) -> RawWorkbookSnapshot: ...

    def abort_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> None: ...

    def get_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> StoredRawWorkbookSnapshot | None: ...


@dataclass(slots=True)
class _MemoryCommand:
    command_sha256: str
    command_id: str
    workbook_id: str
    state: Literal["pending", "completed"]
    claim_token: str | None
    claim_fence: int
    claim_expires_at: int | None
    receipt: RawWorkbookSnapshot | None


class InMemoryWorkbookAssetRepository:
    """Thread-safe reference catalog for tests and single-process development hosts."""

    def __init__(
        self,
        *,
        command_claim_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(command_claim_ttl_seconds, int)
            or isinstance(command_claim_ttl_seconds, bool)
            or not 1 <= command_claim_ttl_seconds <= 900
        ):
            raise ValueError("command_claim_ttl_seconds must be between 1 and 900")
        self._ttl = command_claim_ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._commands: dict[tuple[str, str, str], _MemoryCommand] = {}
        self._workbooks: set[tuple[str, str, str]] = set()
        self._snapshots: dict[
            tuple[str, str, str, str], StoredRawWorkbookSnapshot
        ] = {}
        self._heads: dict[tuple[str, str, str], str] = {}

    def _epoch(self) -> int:
        return int(_clock(self._now).timestamp())

    def claim_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        workbook_id: str,
        claim_token: str,
    ) -> WorkbookAssetCommandClaim:
        tenant_id, subject_id = _principal_scope(principal)
        key = (tenant_id, subject_id, idempotency_key)
        now_epoch = self._epoch()
        with self._lock:
            command = self._commands.get(key)
            if command is None:
                self._commands[key] = _MemoryCommand(
                    command_sha256=command_sha256,
                    command_id=command_id,
                    workbook_id=workbook_id,
                    state="pending",
                    claim_token=claim_token,
                    claim_fence=1,
                    claim_expires_at=now_epoch + self._ttl,
                    receipt=None,
                )
                return WorkbookAssetCommandClaim(
                    "claimed", claim_token=claim_token, claim_fence=1
                )
            if command.command_sha256 != command_sha256:
                raise WorkbookAssetIdempotencyConflictError("idempotency conflict")
            if command.command_id != command_id or command.workbook_id != workbook_id:
                raise WorkbookAssetRepositoryError("upload command identity changed")
            if command.state == "completed":
                if command.receipt is None:
                    raise WorkbookAssetRepositoryError("completed upload has no receipt")
                return WorkbookAssetCommandClaim("completed", receipt=command.receipt)
            if command.claim_expires_at is None:
                raise WorkbookAssetRepositoryError("upload claim lease is invalid")
            if command.claim_expires_at > now_epoch:
                return WorkbookAssetCommandClaim("pending")
            command.claim_token = claim_token
            command.claim_fence += 1
            command.claim_expires_at = now_epoch + self._ttl
            return WorkbookAssetCommandClaim(
                "claimed", claim_token=claim_token, claim_fence=command.claim_fence
            )

    def publish_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
        stored: StoredRawWorkbookSnapshot,
    ) -> RawWorkbookSnapshot:
        tenant_id, subject_id = _principal_scope(principal)
        key = (tenant_id, subject_id, idempotency_key)
        with self._lock:
            command = self._require_claim(
                key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim_token,
                claim_fence=claim_fence,
            )
            snapshot = stored.snapshot
            if command.workbook_id != snapshot.workbook_id:
                raise WorkbookAssetClaimError("upload workbook identity changed")
            workbook_key = (tenant_id, subject_id, snapshot.workbook_id)
            snapshot_key = (*workbook_key, snapshot.raw_snapshot_id)
            if workbook_key in self._workbooks or snapshot_key in self._snapshots:
                raise WorkbookAssetRepositoryError("raw workbook identity already exists")
            self._workbooks.add(workbook_key)
            self._snapshots[snapshot_key] = stored
            self._heads[workbook_key] = snapshot.raw_snapshot_id
            command.state = "completed"
            command.claim_token = None
            command.claim_expires_at = None
            command.receipt = snapshot
            return snapshot

    def abort_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        key = (tenant_id, subject_id, idempotency_key)
        with self._lock:
            self._require_claim(
                key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim_token,
                claim_fence=claim_fence,
            )
            del self._commands[key]

    def get_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> StoredRawWorkbookSnapshot | None:
        tenant_id, subject_id = _principal_scope(principal)
        with self._lock:
            return self._snapshots.get(
                (tenant_id, subject_id, workbook_id, raw_snapshot_id)
            )

    def _require_claim(
        self,
        key: tuple[str, str, str],
        *,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> _MemoryCommand:
        command = self._commands.get(key)
        if (
            command is None
            or command.state != "pending"
            or command.command_sha256 != command_sha256
            or command.command_id != command_id
            or command.claim_token != claim_token
            or command.claim_fence != claim_fence
            or command.claim_expires_at is None
            or command.claim_expires_at <= self._epoch()
        ):
            raise WorkbookAssetClaimError("upload claim mismatch")
        return command


class WorkbookAssetService:
    """Application service for upload, exact status/download, and bound source access."""

    def __init__(
        self,
        repository: WorkbookAssetRepository,
        assets: ImmutableWorkbookAssetStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, WorkbookAssetRepository):
            raise TypeError("repository must implement WorkbookAssetRepository")
        if not isinstance(assets, ImmutableWorkbookAssetStore):
            raise TypeError("assets must implement ImmutableWorkbookAssetStore")
        self._repository = repository
        self._assets = assets
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upload(
        self,
        principal: ServicePrincipal,
        content: bytes,
        idempotency_key: str,
    ) -> WorkbookAssetSubmission:
        try:
            _principal_scope(principal)
            clean_key = _idempotency_key(idempotency_key)
            data = _upload_bytes(content)
        except (TypeError, ValueError):
            raise _service_error("INVALID_REQUEST") from None
        workbook_sha256 = hashlib.sha256(data).hexdigest()
        workbook_id = _derived_id("workbook", principal, clean_key, length=48)
        command_id = _derived_id("upload-command", principal, clean_key, length=64)
        raw_snapshot_id = uploaded_raw_snapshot_id(
            workbook_id=workbook_id,
            workbook_sha256=workbook_sha256,
            size_bytes=len(data),
        )
        command_sha256 = _command_sha256(
            workbook_id=workbook_id,
            raw_snapshot_id=raw_snapshot_id,
            workbook_sha256=workbook_sha256,
            size_bytes=len(data),
        )
        # XLSX parsing and immutable object publication can be comparatively slow.  Complete both
        # before taking the short repository claim so a valid upload cannot repeatedly outlive its
        # lease.  A later conflict can leave only a content-addressed orphan; it cannot expose a
        # workbook, snapshot, or head without the commit-last repository transaction.
        try:
            _validate_workbook(data)
            stored_asset = self._store_and_readback(data)
            snapshot = RawWorkbookSnapshot(
                schema_version=RAW_SNAPSHOT_SCHEMA_VERSION,
                workbook_id=workbook_id,
                raw_snapshot_id=raw_snapshot_id,
                workbook_sha256=workbook_sha256,
                size_bytes=len(data),
                status="stored",
                origin_kind="upload",
                prepared_bundle_created=False,
                created_at=_format_time(_clock(self._now)),
            )
        except WorkbookAssetServiceError:
            raise
        except Exception:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        claim_token = secrets.token_urlsafe(32)
        claim: WorkbookAssetCommandClaim
        try:
            claim = self._repository.claim_upload(
                principal=principal,
                idempotency_key=clean_key,
                command_sha256=command_sha256,
                command_id=command_id,
                workbook_id=workbook_id,
                claim_token=claim_token,
            )
        except WorkbookAssetIdempotencyConflictError:
            raise _service_error("IDEMPOTENCY_CONFLICT") from None
        except WorkbookAssetRepositoryError:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if not isinstance(claim, WorkbookAssetCommandClaim):
            raise _service_error("SERVICE_UNAVAILABLE")
        if claim.state == "completed":
            if claim.receipt is None:
                raise _service_error("SERVICE_UNAVAILABLE")
            if (
                claim.receipt.workbook_id != snapshot.workbook_id
                or claim.receipt.raw_snapshot_id != snapshot.raw_snapshot_id
                or claim.receipt.workbook_sha256 != snapshot.workbook_sha256
                or claim.receipt.size_bytes != snapshot.size_bytes
            ):
                raise _service_error("SERVICE_UNAVAILABLE")
            self._verify_completed_replay(
                principal=principal,
                receipt=claim.receipt,
                expected_asset=stored_asset,
                expected_content=data,
            )
            return WorkbookAssetSubmission(replayed=True, snapshot=claim.receipt)
        if claim.state == "pending":
            raise _service_error("COMMAND_IN_PROGRESS")
        if claim.claim_token is None or claim.claim_fence is None:
            raise _service_error("SERVICE_UNAVAILABLE")
        try:
            receipt = self._repository.publish_upload(
                principal=principal,
                idempotency_key=clean_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                stored=StoredRawWorkbookSnapshot(snapshot=snapshot, asset=stored_asset),
            )
        except WorkbookAssetServiceError:
            self._abort_quietly(
                principal=principal,
                idempotency_key=clean_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
            )
            raise
        except WorkbookAssetClaimError:
            raise _service_error("STALE_UPLOAD_CLAIM") from None
        except WorkbookAssetRepositoryError:
            self._abort_quietly(
                principal=principal,
                idempotency_key=clean_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
            )
            raise _service_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            self._abort_quietly(
                principal=principal,
                idempotency_key=clean_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
            )
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if receipt != snapshot:
            raise _service_error("SERVICE_UNAVAILABLE")
        return WorkbookAssetSubmission(replayed=False, snapshot=receipt)

    def get_snapshot(
        self,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> RawWorkbookSnapshot:
        return self._resolve(principal, workbook_id, raw_snapshot_id).snapshot

    def download(
        self,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> WorkbookAssetDownload:
        stored = self._resolve(principal, workbook_id, raw_snapshot_id)
        data = self._read_asset(stored)
        filename = f"{stored.snapshot.workbook_id}-{stored.snapshot.raw_snapshot_id[:12]}.xlsx"
        return WorkbookAssetDownload(snapshot=stored.snapshot, content=data, filename=filename)

    def bind_source_provider(
        self,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> BoundWorkbookSourceProvider:
        stored = self._resolve(principal, workbook_id, raw_snapshot_id)

        def reader(asset_id: str, max_bytes: int) -> bytes:
            if asset_id != stored.snapshot.raw_snapshot_id:
                raise WorkbookSourceError(
                    "SOURCE_UNAVAILABLE", "원본 workbook asset을 읽을 수 없습니다."
                )
            if max_bytes < stored.snapshot.size_bytes:
                raise WorkbookSourceError(
                    "SOURCE_LIMIT_EXCEEDED",
                    "원본 workbook asset byte 상한을 초과했습니다.",
                )
            try:
                current = self._resolve(principal, workbook_id, raw_snapshot_id)
                return self._read_asset(current)
            except WorkbookAssetServiceError:
                raise WorkbookSourceError(
                    "SOURCE_UNAVAILABLE", "원본 workbook asset을 읽을 수 없습니다."
                ) from None

        return BoundWorkbookSourceProvider(
            asset_id=stored.snapshot.raw_snapshot_id,
            reader=reader,
        )

    def _resolve(
        self,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> StoredRawWorkbookSnapshot:
        try:
            _principal_scope(principal)
            clean_workbook_id = _workbook_id(workbook_id)
            clean_snapshot_id = _sha256(raw_snapshot_id, field_name="raw_snapshot_id")
        except (TypeError, ValueError):
            raise _service_error("INVALID_REQUEST") from None
        try:
            stored = self._repository.get_snapshot(
                principal=principal,
                workbook_id=clean_workbook_id,
                raw_snapshot_id=clean_snapshot_id,
            )
        except WorkbookAssetRepositoryError:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if stored is None:
            raise _service_error("WORKBOOK_NOT_FOUND")
        if not isinstance(stored, StoredRawWorkbookSnapshot):
            raise _service_error("SERVICE_UNAVAILABLE")
        return stored

    def _store_and_readback(self, data: bytes) -> StoredWorkbookAsset:
        try:
            stored = self._assets.put_if_absent(data)
            if not isinstance(stored, StoredWorkbookAsset):
                raise _service_error("SERVICE_UNAVAILABLE")
            reread = self._assets.read_verified(stored)
        except WorkbookAssetServiceError:
            raise
        except WorkbookSnapshotPublicationError as error:
            if error.code == "SOURCE_LIMIT_EXCEEDED":
                raise _service_error("SOURCE_LIMIT_EXCEEDED") from None
            if error.code == "ASSET_INTEGRITY_MISMATCH":
                raise _service_error("ASSET_INTEGRITY_MISMATCH") from None
            raise _service_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if (
            not isinstance(reread, bytes)
            or reread != data
            or stored.workbook_sha256 != hashlib.sha256(data).hexdigest()
            or stored.size_bytes != len(data)
        ):
            raise _service_error("ASSET_INTEGRITY_MISMATCH")
        return stored

    def _read_asset(self, stored: StoredRawWorkbookSnapshot) -> bytes:
        try:
            data = self._assets.read_verified(stored.asset)
        except WorkbookSnapshotPublicationError as error:
            code = (
                "ASSET_INTEGRITY_MISMATCH"
                if error.code == "ASSET_INTEGRITY_MISMATCH"
                else "SERVICE_UNAVAILABLE"
            )
            raise _service_error(code) from None
        except Exception:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if (
            not isinstance(data, bytes)
            or len(data) != stored.snapshot.size_bytes
            or hashlib.sha256(data).hexdigest() != stored.snapshot.workbook_sha256
        ):
            raise _service_error("ASSET_INTEGRITY_MISMATCH")
        return data

    def _verify_completed_replay(
        self,
        *,
        principal: ServicePrincipal,
        receipt: RawWorkbookSnapshot,
        expected_asset: StoredWorkbookAsset,
        expected_content: bytes,
    ) -> None:
        """Recheck the catalog row and immutable object before reporting stored replay."""

        try:
            stored = self._repository.get_snapshot(
                principal=principal,
                workbook_id=receipt.workbook_id,
                raw_snapshot_id=receipt.raw_snapshot_id,
            )
        except WorkbookAssetRepositoryError:
            raise _service_error("SERVICE_UNAVAILABLE") from None
        if (
            not isinstance(stored, StoredRawWorkbookSnapshot)
            or stored.snapshot != receipt
            or stored.asset != expected_asset
        ):
            raise _service_error("SERVICE_UNAVAILABLE")
        if self._read_asset(stored) != expected_content:
            raise _service_error("ASSET_INTEGRITY_MISMATCH")

    def _abort_quietly(self, **kwargs: object) -> None:
        try:
            self._repository.abort_upload(**kwargs)  # type: ignore[arg-type]
        except Exception:
            pass


def _validate_workbook(data: bytes) -> None:
    try:
        validate_xlsx_archive(data)
        workbook = openpyxl.load_workbook(
            BytesIO(data),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        try:
            if not workbook.sheetnames:
                raise ValueError("workbook contains no worksheets")
        finally:
            workbook.close()
    except XlsxSafetyError as error:
        code = "SOURCE_LIMIT_EXCEEDED" if error.code == "LIMIT_EXCEEDED" else "INVALID_WORKBOOK"
        raise _service_error(code) from None
    except WorkbookAssetServiceError:
        raise
    except Exception:
        raise _service_error("INVALID_WORKBOOK") from None


def _upload_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ValueError("content must be non-empty bytes")
    if len(value) > MAX_WORKBOOK_ASSET_BYTES:
        raise WorkbookAssetServiceError("SOURCE_LIMIT_EXCEEDED")
    return value


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("idempotency_key is invalid")
    return value


def _principal_scope(principal: ServicePrincipal) -> tuple[str, str]:
    if not isinstance(principal, ServicePrincipal):
        raise TypeError("principal must be a ServicePrincipal")
    return principal.scope


def _clock(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise WorkbookAssetRepositoryError("workbook asset clock failed") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkbookAssetRepositoryError("workbook asset clock is invalid")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _derived_id(
    prefix: str,
    principal: ServicePrincipal,
    idempotency_key: str,
    *,
    length: int,
) -> str:
    tenant_id, subject_id = _principal_scope(principal)
    identity = _canonical_json(
        {
            "schema_version": "audit_workbook_upload_derived_id.v1",
            "kind": prefix,
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "idempotency_key": idempotency_key,
        }
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"{prefix}-{digest[:length]}"


def uploaded_raw_snapshot_id(
    *, workbook_id: str, workbook_sha256: str, size_bytes: int
) -> str:
    """Return the canonical identity for one initial uploaded raw snapshot."""

    _workbook_id(workbook_id)
    _sha256(workbook_sha256, field_name="workbook_sha256")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not 1 <= size_bytes <= MAX_WORKBOOK_ASSET_BYTES
    ):
        raise ValueError("size_bytes is invalid")
    identity = {
        "schema_version": "audit_raw_workbook_snapshot_identity.v1",
        "workbook_id": workbook_id,
        "workbook_sha256": workbook_sha256,
        "size_bytes": size_bytes,
        "origin_kind": "upload",
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _command_sha256(
    *,
    workbook_id: str,
    raw_snapshot_id: str,
    workbook_sha256: str,
    size_bytes: int,
) -> str:
    command = {
        "schema_version": "audit_raw_workbook_upload_command.v1",
        "workbook_id": workbook_id,
        "raw_snapshot_id": raw_snapshot_id,
        "workbook_sha256": workbook_sha256,
        "size_bytes": size_bytes,
    }
    return hashlib.sha256(_canonical_json(command).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "InMemoryWorkbookAssetRepository",
    "MAX_WORKBOOK_ASSET_BYTES",
    "RAW_SNAPSHOT_SCHEMA_VERSION",
    "RawWorkbookSnapshot",
    "StoredRawWorkbookSnapshot",
    "WorkbookAssetClaimError",
    "WorkbookAssetCommandClaim",
    "WorkbookAssetDownload",
    "WorkbookAssetIdempotencyConflictError",
    "WorkbookAssetRepository",
    "WorkbookAssetRepositoryError",
    "WorkbookAssetService",
    "WorkbookAssetServiceError",
    "WorkbookAssetSubmission",
    "uploaded_raw_snapshot_id",
]
