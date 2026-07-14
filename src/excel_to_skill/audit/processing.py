"""Durable orchestration from one immutable raw workbook to a chat-ready bundle.

The processing boundary deliberately owns only orchestration.  Deterministic conversion,
scope planning, audit preparation, aggregation, and conversation remain implemented by their
existing modules.  A raw snapshot is first converted and fully verified without constructing a
model or standards retriever.  Only an exact, plan-digest-bound scope selection may start semantic
work.  Prepared files remain private staging until every selected scope and the optional aggregate
pass their existing commit gates and an immutable package copy is published.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from openpyxl.utils import range_boundaries

from ..emit_html import DEFAULT_MAX_ROWS
from ..meta import _converter_version
from ..verify import VerifyResult, verify_package
from .aggregate import AGGREGATE_VERSION, aggregate_audit_package, load_audit_aggregate
from .brief import BRIEF_VERSION
from .consume import load_validated_audit_bundle
from .contract import PREPARE_VERSION
from .extract import EXTRACTOR_VERSION
from .model import canonical_json, json_sha256
from .prepare import CONTEXT_VERSION, prepare_package
from .scope import AuditScope, audit_scopes_plan, read_scope_commit
from .service import (
    BundleScopeBinding,
    BundleSnapshot,
    BundleSnapshotNotFoundError,
    ServicePrincipal,
)
from .workbook_asset_service import (
    MAX_WORKBOOK_ASSET_BYTES,
    RawWorkbookSnapshot,
    WorkbookAssetService,
    WorkbookAssetServiceError,
)
from .workbook_source import WorkbookSourceError, read_verified_workbook_source
from .xlsx_safety import validate_xlsx_archive


PROCESSING_JOB_SCHEMA_VERSION = "audit_processing_job.v1"
PROCESSING_SCOPE_SELECTION_SCHEMA_VERSION = "audit_processing_scope_selection.v1"
PROCESSING_RESULT_SCHEMA_VERSION = "audit_processing_result.v1"
PROCESSING_PROFILE_VERSION = "audit_processing_profile.v1"

MAX_SELECTED_SCOPES = 64
MAX_PLAN_SHEETS = 512
_JOB_ID_RE = re.compile(r"\Aprocess-[0-9a-f]{48}\Z")
_BUNDLE_ID_RE = re.compile(r"\Abundle-[0-9a-f]{48}\Z")
_WORKBOOK_ID_RE = re.compile(r"\Aworkbook-[0-9a-f]{48}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")
_TIME_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_DIMENSIONS_RE = re.compile(
    r"\A[A-Z]{1,3}[1-9][0-9]{0,6}(?::[A-Z]{1,3}[1-9][0-9]{0,6})?\Z"
)
_IDEMPOTENCY_MAX = 128

ProcessingStatus = Literal[
    "planning",
    "awaiting_scope",
    "preparing",
    "aggregating",
    "published",
    "failed",
]
SelectionMode = Literal["workbook", "all_sheets", "selected_sheets"]

_ERRORS: dict[str, tuple[str, int]] = {
    "INVALID_REQUEST": ("The processing request is invalid.", 400),
    "WORKBOOK_NOT_FOUND": ("The exact raw workbook snapshot was not found.", 404),
    "JOB_NOT_FOUND": ("The processing job was not found.", 404),
    "IDEMPOTENCY_CONFLICT": ("The idempotency key is bound to another command.", 409),
    "JOB_IN_PROGRESS": ("The processing job is already being worked.", 409),
    "SCOPE_NOT_READY": ("The scope plan is not ready for selection.", 409),
    "SCOPE_CONFLICT": ("The processing job is bound to another scope selection.", 409),
    "PLAN_CHANGED": ("The submitted scope plan is no longer current.", 409),
    "INVALID_SCOPE_SELECTION": ("The selected workbook scopes are invalid.", 422),
    "PROCESSING_PROFILE_CHANGED": ("The server processing profile changed.", 409),
    "DETERMINISTIC_PROCESSING_FAILED": ("Workbook conversion or verification failed.", 422),
    "PREPARATION_FAILED": ("One or more selected scopes could not be prepared.", 422),
    "AGGREGATION_FAILED": ("The selected scope aggregate could not be prepared.", 422),
    "PUBLICATION_FAILED": ("The prepared bundle could not be published.", 503),
    "BUNDLE_UNAVAILABLE": ("The published bundle is unavailable.", 503),
    "SERVICE_UNAVAILABLE": ("The processing service is unavailable.", 503),
}


class ProcessingServiceError(RuntimeError):
    """A fixed, path-free error that may cross the HTTP boundary."""

    def __init__(self, code: str):
        safe_code = code if code in _ERRORS else "SERVICE_UNAVAILABLE"
        message, status_code = _ERRORS[safe_code]
        self.code = safe_code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ProcessingRepositoryError(RuntimeError):
    """The durable processing catalog could not complete a repository operation."""


class ProcessingConflictError(ProcessingRepositoryError):
    """An idempotency key or immutable job selection has a conflicting payload."""


class ProcessingLeaseError(ProcessingRepositoryError):
    """A worker no longer owns the exact monotonic processing lease."""


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 identifier")
    return value


def _canonical_time(value: object) -> str:
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        raise ValueError("timestamp must use canonical UTC seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("timestamp must use canonical UTC seconds") from None
    if _format_time(parsed) != value:
        raise ValueError("timestamp must use canonical UTC seconds")
    return value


def _job_id(value: object) -> str:
    if not isinstance(value, str) or _JOB_ID_RE.fullmatch(value) is None:
        raise ValueError("job_id is invalid")
    return value


def _bundle_id(value: object) -> str:
    if not isinstance(value, str) or _BUNDLE_ID_RE.fullmatch(value) is None:
        raise ValueError("bundle_id is invalid")
    return value


def _workbook_id(value: object) -> str:
    if not isinstance(value, str) or _WORKBOOK_ID_RE.fullmatch(value) is None:
        raise ValueError("workbook_id is invalid")
    return value


def _fixed_failure_code(value: object) -> str:
    if not isinstance(value, str) or value not in {
        "DETERMINISTIC_PROCESSING_FAILED",
        "PREPARATION_FAILED",
        "AGGREGATION_FAILED",
        "PUBLICATION_FAILED",
        "PROCESSING_PROFILE_CHANGED",
        "SERVICE_UNAVAILABLE",
    }:
        raise ValueError("processing failure code is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProcessingFailure:
    code: str
    stage: Literal["planning", "preparing", "aggregating", "publishing"]
    retryable: bool

    def __post_init__(self) -> None:
        _fixed_failure_code(self.code)
        if self.stage not in {"planning", "preparing", "aggregating", "publishing"}:
            raise ValueError("processing failure stage is invalid")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean")

    def to_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "stage": self.stage, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class ProcessingProgress:
    total_scopes: int = 0
    completed_scopes: int = 0
    current_scope_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.total_scopes, int)
            or isinstance(self.total_scopes, bool)
            or not 0 <= self.total_scopes <= MAX_SELECTED_SCOPES
            or not isinstance(self.completed_scopes, int)
            or isinstance(self.completed_scopes, bool)
            or not 0 <= self.completed_scopes <= self.total_scopes
            or len(self.current_scope_ids) > MAX_SELECTED_SCOPES
        ):
            raise ValueError("processing progress is invalid")
        for scope_id in self.current_scope_ids:
            _sha256(scope_id, field_name="current_scope_id")
        if len(set(self.current_scope_ids)) != len(self.current_scope_ids):
            raise ValueError("current scope IDs must be unique")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "total_scopes": self.total_scopes,
            "completed_scopes": self.completed_scopes,
            "current_scope_ids": list(self.current_scope_ids),
        }


@dataclass(frozen=True, slots=True)
class ProcessingScopeSelection:
    mode: SelectionMode
    scope_plan_sha256: str
    scope_ids: tuple[str, ...]
    sheets: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in {"workbook", "all_sheets", "selected_sheets"}:
            raise ValueError("scope selection mode is invalid")
        _sha256(self.scope_plan_sha256, field_name="scope_plan_sha256")
        if len(self.scope_ids) != len(self.sheets):
            raise ValueError("scope IDs and sheets must have equal length")
        if len(set(self.scope_ids)) != len(self.scope_ids) or len(set(self.sheets)) != len(
            self.sheets
        ):
            raise ValueError("scope selection entries must be unique")
        for scope_id in self.scope_ids:
            _sha256(scope_id, field_name="scope_id")
        for sheet in self.sheets:
            if (
                not isinstance(sheet, str)
                or not sheet
                or len(sheet) > 31
                or any(character in sheet for character in "[]:*?/\\")
            ):
                raise ValueError("selected sheet is invalid")
        if self.mode == "workbook":
            if self.scope_ids or self.sheets:
                raise ValueError("workbook selection cannot include sheet scopes")
        elif not 1 <= len(self.scope_ids) <= MAX_SELECTED_SCOPES:
            raise ValueError("sheet selection count is invalid")

    @property
    def selection_sha256(self) -> str:
        return json_sha256(self.identity())

    def identity(self) -> dict[str, object]:
        return {
            "schema_version": PROCESSING_SCOPE_SELECTION_SCHEMA_VERSION,
            "mode": self.mode,
            "scope_plan_sha256": self.scope_plan_sha256,
            "scopes": [
                {"scope_id": scope_id, "sheet": sheet}
                for scope_id, sheet in zip(self.scope_ids, self.sheets, strict=True)
            ],
        }

    def to_public_dict(self) -> dict[str, object]:
        return self.identity()


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    bundle_id: str
    snapshot_id: str
    package_manifest_sha256: str
    selection_sha256: str
    sheet: str | None
    aggregate_id: str | None
    included_sheets: tuple[str, ...]
    skipped_empty_sheets: tuple[str, ...]

    def __post_init__(self) -> None:
        _bundle_id(self.bundle_id)
        _sha256(self.snapshot_id, field_name="snapshot_id")
        _sha256(self.package_manifest_sha256, field_name="package_manifest_sha256")
        _sha256(self.selection_sha256, field_name="selection_sha256")
        if self.sheet is not None and self.aggregate_id is not None:
            raise ValueError("sheet and aggregate_id are mutually exclusive")
        if self.aggregate_id is not None:
            _sha256(self.aggregate_id, field_name="aggregate_id")
        if self.sheet is not None and self.included_sheets != (self.sheet,):
            raise ValueError("sheet chat binding must match included_sheets")
        for values in (self.included_sheets, self.skipped_empty_sheets):
            if len(set(values)) != len(values) or any(
                not isinstance(item, str) or not item for item in values
            ):
                raise ValueError("processing coverage is invalid")
        if set(self.included_sheets) & set(self.skipped_empty_sheets):
            raise ValueError("processing coverage sets overlap")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROCESSING_RESULT_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "snapshot_id": self.snapshot_id,
            "selection_sha256": self.selection_sha256,
            "chat_binding": {"sheet": self.sheet, "aggregate_id": self.aggregate_id},
            "coverage": {
                "included_sheets": list(self.included_sheets),
                "skipped_empty_sheets": list(self.skipped_empty_sheets),
            },
        }


@dataclass(frozen=True, slots=True)
class ProcessingJob:
    schema_version: str
    job_id: str
    workbook_id: str
    raw_snapshot_id: str
    workbook_sha256: str
    profile_sha256: str
    status: ProcessingStatus
    scope_plan_sha256: str | None
    scope_plan: Mapping[str, object] | None
    selection: ProcessingScopeSelection | None
    progress: ProcessingProgress
    result: ProcessingResult | None
    failure: ProcessingFailure | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != PROCESSING_JOB_SCHEMA_VERSION:
            raise ValueError("processing job schema is invalid")
        _job_id(self.job_id)
        _workbook_id(self.workbook_id)
        _sha256(self.raw_snapshot_id, field_name="raw_snapshot_id")
        _sha256(self.workbook_sha256, field_name="workbook_sha256")
        _sha256(self.profile_sha256, field_name="profile_sha256")
        if self.status not in {
            "planning",
            "awaiting_scope",
            "preparing",
            "aggregating",
            "published",
            "failed",
        }:
            raise ValueError("processing status is invalid")
        _canonical_time(self.created_at)
        _canonical_time(self.updated_at)
        if self.scope_plan_sha256 is None:
            if self.scope_plan is not None or self.status not in {"planning", "failed"}:
                raise ValueError("processing plan state is invalid")
        else:
            _sha256(self.scope_plan_sha256, field_name="scope_plan_sha256")
            if not isinstance(self.scope_plan, Mapping):
                raise ValueError("scope plan is missing")
            if json_sha256(dict(self.scope_plan)) != self.scope_plan_sha256:
                raise ValueError("scope plan digest is invalid")
        if self.selection is not None and self.scope_plan_sha256 != self.selection.scope_plan_sha256:
            raise ValueError("selection is not bound to the job plan")
        if self.status in {"preparing", "aggregating", "published"} and self.selection is None:
            raise ValueError("processing selection is missing")
        if self.status == "published":
            if self.result is None or self.failure is not None:
                raise ValueError("published processing result is invalid")
        elif self.result is not None:
            raise ValueError("only published jobs may expose a result")
        if self.status == "failed":
            if self.failure is None:
                raise ValueError("failed processing job needs a fixed failure")
        elif self.failure is not None:
            raise ValueError("non-failed job cannot expose a failure")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "workbook_id": self.workbook_id,
            "raw_snapshot_id": self.raw_snapshot_id,
            "workbook_sha256": self.workbook_sha256,
            "status": self.status,
            "scope_plan_sha256": self.scope_plan_sha256,
            "scope_plan": dict(self.scope_plan) if self.scope_plan is not None else None,
            "selection": self.selection.to_public_dict() if self.selection else None,
            "progress": self.progress.to_public_dict(),
            "result": self.result.to_public_dict() if self.result else None,
            "failure": self.failure.to_public_dict() if self.failure else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ProcessingSubmission:
    job: ProcessingJob
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProcessingLeaseClaim:
    state: Literal["claimed", "pending", "current"]
    job: ProcessingJob
    claim_token: str | None = field(default=None, repr=False)
    claim_fence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job, ProcessingJob):
            raise TypeError("processing claim job is invalid")
        if self.state == "claimed":
            if (
                not isinstance(self.claim_token, str)
                or _TOKEN_RE.fullmatch(self.claim_token) is None
                or not isinstance(self.claim_fence, int)
                or isinstance(self.claim_fence, bool)
                or self.claim_fence < 1
            ):
                raise ValueError("owned processing claim is invalid")
        elif self.state in {"pending", "current"}:
            if self.claim_token is not None or self.claim_fence is not None:
                raise ValueError("unowned processing claim is invalid")
        else:
            raise ValueError("processing claim state is invalid")


@dataclass(frozen=True, slots=True)
class PublishedBundleRecord:
    bundle_id: str
    snapshot_id: str
    package_manifest_sha256: str
    file_count: int
    total_bytes: int
    job_id: str
    workbook_id: str
    raw_snapshot_id: str
    workbook_sha256: str
    selection_sha256: str
    sheet: str | None
    aggregate_id: str | None
    published_at: str

    def __post_init__(self) -> None:
        _bundle_id(self.bundle_id)
        _sha256(self.snapshot_id, field_name="snapshot_id")
        _sha256(self.package_manifest_sha256, field_name="package_manifest_sha256")
        if (
            not isinstance(self.file_count, int)
            or isinstance(self.file_count, bool)
            or self.file_count < 1
            or not isinstance(self.total_bytes, int)
            or isinstance(self.total_bytes, bool)
            or self.total_bytes < 1
        ):
            raise ValueError("published bundle package counts are invalid")
        _job_id(self.job_id)
        _sha256(self.raw_snapshot_id, field_name="raw_snapshot_id")
        _sha256(self.workbook_sha256, field_name="workbook_sha256")
        _sha256(self.selection_sha256, field_name="selection_sha256")
        if self.sheet is not None and self.aggregate_id is not None:
            raise ValueError("published bundle binding is invalid")
        if self.aggregate_id is not None:
            _sha256(self.aggregate_id, field_name="aggregate_id")
        _canonical_time(self.published_at)


@runtime_checkable
class ProcessingRepository(Protocol):
    def claim_planning(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        proposed_job: ProcessingJob,
        claim_token: str,
    ) -> ProcessingLeaseClaim: ...

    def publish_plan(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        scope_plan: Mapping[str, object],
        scope_plan_sha256: str,
    ) -> ProcessingJob: ...

    def claim_execution(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        idempotency_key: str,
        command_sha256: str,
        selection: ProcessingScopeSelection,
        claim_token: str,
    ) -> ProcessingLeaseClaim: ...

    def checkpoint(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        status: Literal["planning", "preparing", "aggregating"],
        progress: ProcessingProgress,
    ) -> ProcessingJob: ...

    def fail(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        failure: ProcessingFailure,
    ) -> ProcessingJob: ...

    def publish_bundle(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        result: ProcessingResult,
        bundle: PublishedBundleRecord,
    ) -> ProcessingJob: ...

    def get_job(
        self, *, principal: ServicePrincipal, job_id: str
    ) -> ProcessingJob | None: ...

    def get_bundle(
        self, *, principal: ServicePrincipal, bundle_id: str
    ) -> PublishedBundleRecord | None: ...


@runtime_checkable
class PreparedBundleStore(Protocol):
    @property
    def deployment_id(self) -> str: ...

    def publish(self, package: Path, identity: Mapping[str, object]): ...

    def resolve(self, stored): ...


class _LeaseKeeper:
    """Serialize checkpoints and renew one exact repository lease in the background."""

    def __init__(
        self,
        *,
        repository: ProcessingRepository,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        status: Literal["planning", "preparing", "aggregating"],
        progress: ProcessingProgress,
        interval_seconds: float,
    ) -> None:
        self._repository = repository
        self._principal = principal
        self._job_id = job_id
        self._claim_token = claim_token
        self._claim_fence = claim_fence
        self._status = status
        self._progress = progress
        self._interval = interval_seconds
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._failure_code: str | None = None
        self._finalized = False
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"audit-processing-lease-{claim_fence}",
            daemon=True,
        )

    def __enter__(self) -> _LeaseKeeper:
        # Refresh immediately; this catches a claim lost before the runner starts useful work.
        self.checkpoint(status=self._status, progress=self._progress)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=max(1.0, self._interval * 2))

    def ensure_owned(self) -> None:
        with self._lock:
            self._raise_failure_locked()
            if self._finalized:
                raise ProcessingServiceError("JOB_IN_PROGRESS")

    def wait(self, seconds: float) -> None:
        self._stop.wait(seconds)
        self.ensure_owned()

    def checkpoint(
        self,
        *,
        status: Literal["planning", "preparing", "aggregating"],
        progress: ProcessingProgress,
    ) -> ProcessingJob:
        with self._lock:
            self._raise_failure_locked()
            if self._finalized:
                raise ProcessingServiceError("JOB_IN_PROGRESS")
            try:
                job = self._repository.checkpoint(
                    principal=self._principal,
                    job_id=self._job_id,
                    claim_token=self._claim_token,
                    claim_fence=self._claim_fence,
                    status=status,
                    progress=progress,
                )
            except ProcessingLeaseError:
                self._failure_code = "JOB_IN_PROGRESS"
                self._stop.set()
                raise ProcessingServiceError("JOB_IN_PROGRESS") from None
            except ProcessingRepositoryError:
                self._failure_code = "SERVICE_UNAVAILABLE"
                self._stop.set()
                raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
            self._status = status
            self._progress = progress
            return job

    def finalize(self, call: Callable[[], object]) -> object:
        """Run a lease-consuming terminal transition without a racing heartbeat."""

        with self._lock:
            self._raise_failure_locked()
            if self._finalized:
                raise ProcessingServiceError("JOB_IN_PROGRESS")
            try:
                result = call()
            except ProcessingLeaseError:
                self._failure_code = "JOB_IN_PROGRESS"
                self._stop.set()
                raise ProcessingServiceError("JOB_IN_PROGRESS") from None
            except ProcessingRepositoryError:
                self._failure_code = "SERVICE_UNAVAILABLE"
                self._stop.set()
                raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
            self._finalized = True
            self._stop.set()
            return result

    def _raise_failure_locked(self) -> None:
        if self._failure_code is not None:
            raise ProcessingServiceError(self._failure_code)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                if self._stop.is_set() or self._finalized:
                    return
                try:
                    self._repository.checkpoint(
                        principal=self._principal,
                        job_id=self._job_id,
                        claim_token=self._claim_token,
                        claim_fence=self._claim_fence,
                        status=self._status,
                        progress=self._progress,
                    )
                except ProcessingLeaseError:
                    self._failure_code = "JOB_IN_PROGRESS"
                    self._stop.set()
                    return
                except Exception:
                    self._failure_code = "SERVICE_UNAVAILABLE"
                    self._stop.set()
                    return


def _canonical_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _IDEMPOTENCY_MAX
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("idempotency_key is invalid")
    return value


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProcessingRepositoryError("processing clock is invalid")
    return value.astimezone(timezone.utc)


def _derived_id(prefix: str, principal: ServicePrincipal, key: str, *, length: int) -> str:
    identity = {
        "schema_version": "audit_processing_derived_id.v1",
        "prefix": prefix,
        "tenant_id": principal.tenant_id,
        "subject_id": principal.subject_id,
        "key": key,
    }
    return f"{prefix}-{_canonical_digest(identity)[:length]}"


def _principal_directory(principal: ServicePrincipal) -> str:
    return _canonical_digest(
        {
            "schema_version": "audit_processing_principal_directory.v1",
            "tenant_id": principal.tenant_id,
            "subject_id": principal.subject_id,
        }
    )


def _public_scope_plan(raw: object) -> dict[str, object]:
    """Return a bounded path-free projection of ``audit_scopes_plan``."""

    if not isinstance(raw, Mapping) or raw.get("schema_version") != "audit_scope_plan.v1":
        raise ValueError("scope plan is invalid")
    workbook = raw.get("workbook")
    all_sheets = raw.get("all_sheets")
    sheets = raw.get("sheets")
    if not isinstance(workbook, Mapping) or not isinstance(all_sheets, Mapping) or not isinstance(
        sheets, list
    ):
        raise ValueError("scope plan is invalid")
    if len(sheets) > MAX_PLAN_SHEETS:
        raise ValueError("scope plan exceeds the supported sheet count")

    def calls(value: object) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("estimated calls are invalid")
        result: dict[str, int] = {}
        for key in ("facts", "brief", "total_llm"):
            item = value.get(key)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise ValueError("estimated calls are invalid")
            result[key] = item
        return result

    workbook_scope = workbook.get("scope")
    if workbook_scope != {"kind": "workbook"}:
        raise ValueError("workbook scope plan is invalid")
    public_sheets: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for item in sheets:
        if not isinstance(item, Mapping):
            raise ValueError("sheet scope plan is invalid")
        scope = item.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("sheet scope plan is invalid")
        scope_id = _sha256(scope.get("id"), field_name="scope_id")
        sheet = scope.get("sheet")
        if (
            scope.get("kind") != "sheet"
            or not isinstance(sheet, str)
            or not sheet
            or scope_id in seen_ids
            or sheet in seen_names
        ):
            raise ValueError("sheet scope plan is invalid")
        seen_ids.add(scope_id)
        seen_names.add(sheet)
        dependencies = item.get("dependency_sheets")
        if not isinstance(dependencies, list) or any(
            not isinstance(value, str) or value not in seen_names | {
                candidate.get("scope", {}).get("sheet")
                for candidate in sheets
                if isinstance(candidate, Mapping)
                and isinstance(candidate.get("scope"), Mapping)
            }
            for value in dependencies
        ):
            raise ValueError("sheet dependency plan is invalid")
        dimensions = item.get("dimensions")
        if dimensions is not None:
            if (
                not isinstance(dimensions, str)
                or _DIMENSIONS_RE.fullmatch(dimensions) is None
            ):
                raise ValueError("sheet dimensions are invalid")
            try:
                min_col, min_row, max_col, max_row = range_boundaries(dimensions)
            except ValueError:
                raise ValueError("sheet dimensions are invalid") from None
            if not (
                1 <= min_col <= max_col <= 16_384
                and 1 <= min_row <= max_row <= 1_048_576
            ):
                raise ValueError("sheet dimensions are invalid")
        numeric: dict[str, int] = {}
        for key in ("cell_count", "region_count"):
            value = item.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("sheet plan count is invalid")
            numeric[key] = value
        analyzable = item.get("analyzable")
        if not isinstance(analyzable, bool):
            raise ValueError("sheet analyzable flag is invalid")
        public_sheets.append(
            {
                "scope": {"kind": "sheet", "sheet": sheet, "id": scope_id},
                "dimensions": dimensions,
                **numeric,
                "analyzable": analyzable,
                "dependency_sheets": list(dependencies),
                "estimated_calls": calls(item.get("estimated_calls")),
            }
        )

    workbook_counts: dict[str, object] = {}
    for key in ("sheet_count", "cell_count", "region_count"):
        value = workbook.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("workbook plan count is invalid")
        workbook_counts[key] = value
    if workbook_counts["sheet_count"] != len(public_sheets):
        raise ValueError("workbook plan sheet count is invalid")
    workbook_analyzable = workbook.get("analyzable")
    if not isinstance(workbook_analyzable, bool):
        raise ValueError("workbook analyzable flag is invalid")

    all_counts: dict[str, int] = {}
    for key in (
        "sheet_count",
        "total_sheet_count",
        "skipped_empty_sheet_count",
        "cell_count",
        "region_count",
    ):
        value = all_sheets.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("all-sheets plan count is invalid")
        all_counts[key] = value
    if (
        all_counts["total_sheet_count"] != len(public_sheets)
        or all_counts["sheet_count"] != sum(bool(item["analyzable"]) for item in public_sheets)
        or all_counts["skipped_empty_sheet_count"]
        != len(public_sheets) - all_counts["sheet_count"]
    ):
        raise ValueError("all-sheets plan count is inconsistent")
    return {
        "schema_version": "audit_scope_plan.v1",
        "workbook": {
            "scope": {"kind": "workbook"},
            **workbook_counts,
            "analyzable": workbook_analyzable,
            "estimated_calls": calls(workbook.get("estimated_calls")),
        },
        "all_sheets": {
            **all_counts,
            "selectable": 1 <= all_counts["sheet_count"] <= MAX_SELECTED_SCOPES,
            "selection_limit": MAX_SELECTED_SCOPES,
            "estimated_calls": calls(all_sheets.get("estimated_calls")),
        },
        "sheets": public_sheets,
    }


def _selection_from_request(
    job: ProcessingJob,
    *,
    mode: object,
    scope_plan_sha256: object,
    scope_ids: object,
) -> ProcessingScopeSelection:
    if mode not in {"workbook", "all_sheets", "selected_sheets"}:
        raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
    try:
        plan_sha = _sha256(scope_plan_sha256, field_name="scope_plan_sha256")
    except ValueError:
        raise ProcessingServiceError("INVALID_SCOPE_SELECTION") from None
    if job.scope_plan_sha256 != plan_sha or job.scope_plan is None:
        raise ProcessingServiceError("PLAN_CHANGED")
    if not isinstance(scope_ids, (list, tuple)) or any(
        not isinstance(value, str) for value in scope_ids
    ):
        raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
    requested = tuple(scope_ids)
    if len(set(requested)) != len(requested):
        raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
    sheet_entries = job.scope_plan.get("sheets")
    if not isinstance(sheet_entries, list):
        raise ProcessingServiceError("SERVICE_UNAVAILABLE")
    by_id = {
        item["scope"]["id"]: item
        for item in sheet_entries
        if isinstance(item, Mapping) and isinstance(item.get("scope"), Mapping)
    }
    workbook = job.scope_plan.get("workbook")
    if mode == "workbook":
        if requested or not isinstance(workbook, Mapping) or workbook.get("analyzable") is not True:
            raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
        return ProcessingScopeSelection("workbook", plan_sha, (), ())
    if mode == "all_sheets":
        if requested:
            raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
        selected_entries = [item for item in sheet_entries if item.get("analyzable") is True]
    else:
        if not 1 <= len(requested) <= MAX_SELECTED_SCOPES:
            raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
        try:
            selected_entries = [by_id[scope_id] for scope_id in requested]
        except KeyError:
            raise ProcessingServiceError("INVALID_SCOPE_SELECTION") from None
        if any(item.get("analyzable") is not True for item in selected_entries):
            raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
        # Canonicalize user order into workbook order so one selection has one identity.
        requested_set = set(requested)
        selected_entries = [
            item for item in sheet_entries if item["scope"]["id"] in requested_set
        ]
    if not 1 <= len(selected_entries) <= MAX_SELECTED_SCOPES:
        raise ProcessingServiceError("INVALID_SCOPE_SELECTION")
    ids = tuple(item["scope"]["id"] for item in selected_entries)
    sheets = tuple(item["scope"]["sheet"] for item in selected_entries)
    return ProcessingScopeSelection(mode, plan_sha, ids, sheets)


def _verify_complete(result: object) -> None:
    if not isinstance(result, VerifyResult) or not result.ok:
        raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
    reproducibility = [check for check in result.checks if check.name == "V3"]
    if len(reproducibility) != 1 or not reproducibility[0].ok or reproducibility[0].skipped:
        raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")


def _assert_no_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ProcessingServiceError("SERVICE_UNAVAILABLE")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ProcessingServiceError("SERVICE_UNAVAILABLE")


def _private_directory(path: Path) -> None:
    _assert_no_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ProcessingServiceError("SERVICE_UNAVAILABLE")
    try:
        path.chmod(0o700, follow_symlinks=False)
        if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o077:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE")
    except (OSError, NotImplementedError):
        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
    try:
        os.fsync(descriptor)
    except OSError:
        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
    finally:
        os.close(descriptor)


def _materialize_source(path: Path, content: bytes, expected_sha256: str) -> None:
    if len(content) > MAX_WORKBOOK_ASSET_BYTES or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ProcessingServiceError("SERVICE_UNAVAILABLE")
        try:
            existing = path.read_bytes()
        except OSError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if existing != content or hashlib.sha256(existing).hexdigest() != expected_sha256:
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
        return
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".source.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        path.chmod(0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except ProcessingServiceError:
        raise
    except OSError:
        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_job_lock(path: Path, lease: _LeaseKeeper):
    """Fence one local staging directory across processes while the DB lease stays live."""

    _private_directory(path.parent)
    if path.is_symlink():
        raise ProcessingServiceError("SERVICE_UNAVAILABLE")
    descriptor = -1
    locked = False
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - the reference processing host is POSIX
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        while True:
            lease.ensure_owned()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
                lease.wait(0.1)
        lease.ensure_owned()
        yield
    except ProcessingServiceError:
        raise
    except OSError:
        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
    finally:
        if descriptor >= 0:
            if locked:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            os.close(descriptor)


class ProcessingService:
    """Synchronous, restart-safe processing runner and bundle resolver.

    A hosting API may offload these methods to a bounded executor or queue.  The SQLite lease is
    the authority for retries; this class never relies on an in-process task registry.
    """

    def __init__(
        self,
        *,
        repository: ProcessingRepository,
        workbook_assets: WorkbookAssetService,
        bundle_store: PreparedBundleStore,
        workspace_root: Path | str,
        model: str,
        client_factory: Callable[[], object],
        standards_retriever_factory: Callable[[], object],
        retriever_descriptor: Mapping[str, object],
        aggregate_client_factory: Callable[[], object] | None = None,
        max_prepare_workers: int = 4,
        lease_heartbeat_seconds: float | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        full_names: bool = False,
        converter: Callable[..., Path] | None = None,
        verifier: Callable[[Path, Path | None], VerifyResult] = verify_package,
        scope_planner: Callable[[Path], dict] = audit_scopes_plan,
        preparer: Callable[..., object] = prepare_package,
        aggregator: Callable[..., object] = aggregate_audit_package,
        now: Callable[[], datetime] | None = None,
        eprint=None,
    ) -> None:
        if not isinstance(repository, ProcessingRepository):
            raise TypeError("repository must implement ProcessingRepository")
        if not isinstance(workbook_assets, WorkbookAssetService):
            raise TypeError("workbook_assets must be a WorkbookAssetService")
        if not isinstance(bundle_store, PreparedBundleStore):
            raise TypeError("bundle_store must implement PreparedBundleStore")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        if not callable(client_factory) or not callable(standards_retriever_factory):
            raise TypeError("processing factories must be callable")
        if aggregate_client_factory is not None and not callable(aggregate_client_factory):
            raise TypeError("aggregate_client_factory must be callable")
        if (
            not isinstance(max_prepare_workers, int)
            or isinstance(max_prepare_workers, bool)
            or not 1 <= max_prepare_workers <= 16
        ):
            raise ValueError("max_prepare_workers must be between 1 and 16")
        lease_ttl = getattr(repository, "lease_ttl_seconds", 300)
        if (
            not isinstance(lease_ttl, int)
            or isinstance(lease_ttl, bool)
            or lease_ttl < 1
        ):
            raise ValueError("repository lease_ttl_seconds is invalid")
        if lease_heartbeat_seconds is None:
            heartbeat = max(0.1, min(30.0, lease_ttl / 4))
        else:
            heartbeat = lease_heartbeat_seconds
        if (
            not isinstance(heartbeat, (int, float))
            or isinstance(heartbeat, bool)
            or not 0.05 <= float(heartbeat) < lease_ttl
        ):
            raise ValueError("lease heartbeat must be positive and shorter than the lease TTL")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
            raise ValueError("max_rows must be positive")
        if not isinstance(full_names, bool):
            raise ValueError("full_names must be boolean")
        root = Path(workspace_root).expanduser()
        if not root.is_absolute():
            raise ValueError("workspace_root must be an absolute server-owned path")
        _private_directory(root)
        self._repository = repository
        self._assets = workbook_assets
        self._bundle_store = bundle_store
        self._workspace_root = root.resolve(strict=True)
        self._model = model.strip()
        self._client_factory = client_factory
        self._aggregate_client_factory = aggregate_client_factory or client_factory
        self._retriever_factory = standards_retriever_factory
        try:
            normalized_descriptor = json.loads(
                canonical_json(dict(retriever_descriptor))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("retriever_descriptor must be a JSON object") from None
        if not isinstance(normalized_descriptor, dict):
            raise ValueError("retriever_descriptor must be a JSON object")
        self._retriever_descriptor = normalized_descriptor
        descriptor_identity = dict(normalized_descriptor)
        descriptor_identity.pop("retrieved_at", None)
        self._max_prepare_workers = max_prepare_workers
        self._lease_heartbeat_seconds = float(heartbeat)
        self._max_rows = max_rows
        self._full_names = full_names
        self._converter = converter or self._default_converter
        self._verifier = verifier
        self._scope_planner = scope_planner
        self._preparer = preparer
        self._aggregator = aggregator
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._eprint = eprint or (lambda *args: None)
        self._profile_sha256 = _canonical_digest(
            {
                "schema_version": PROCESSING_PROFILE_VERSION,
                "converter_version": _converter_version(),
                "prepare_version": PREPARE_VERSION,
                "extractor_version": EXTRACTOR_VERSION,
                "context_version": CONTEXT_VERSION,
                "brief_version": BRIEF_VERSION,
                "aggregate_version": AGGREGATE_VERSION,
                "max_rows": max_rows,
                "full_names": full_names,
                "model": self._model,
                "retriever_descriptor": descriptor_identity,
                "workspace_deployment_id": hashlib.sha256(
                    str(self._workspace_root).encode("utf-8")
                ).hexdigest(),
                "bundle_store_deployment_id": _sha256(
                    self._bundle_store.deployment_id,
                    field_name="bundle_store_deployment_id",
                ),
            }
        )

    @staticmethod
    def _default_converter(source: Path, output_root: Path, **kwargs) -> Path:
        from ..cli import _convert_one

        return _convert_one(source, output_root, **kwargs)

    def start(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
        idempotency_key: str,
    ) -> ProcessingSubmission:
        try:
            key = _idempotency_key(idempotency_key)
            clean_workbook_id = _workbook_id(workbook_id)
            clean_raw_snapshot_id = _sha256(
                raw_snapshot_id, field_name="raw_snapshot_id"
            )
            if not isinstance(principal, ServicePrincipal):
                raise ValueError
        except (TypeError, ValueError):
            raise ProcessingServiceError("INVALID_REQUEST") from None
        job_id = _derived_id("process", principal, key, length=48)
        try:
            existing = self._repository.get_job(principal=principal, job_id=job_id)
        except ProcessingRepositoryError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        snapshot: RawWorkbookSnapshot | None = None
        if existing is not None:
            if (
                existing.workbook_id != clean_workbook_id
                or existing.raw_snapshot_id != clean_raw_snapshot_id
            ):
                raise ProcessingServiceError("IDEMPOTENCY_CONFLICT")
            self._assert_profile(existing)
            identity_workbook_id = existing.workbook_id
            identity_raw_snapshot_id = existing.raw_snapshot_id
            identity_workbook_sha256 = existing.workbook_sha256
        else:
            snapshot = self._exact_raw_snapshot(
                principal, clean_workbook_id, clean_raw_snapshot_id
            )
            identity_workbook_id = snapshot.workbook_id
            identity_raw_snapshot_id = snapshot.raw_snapshot_id
            identity_workbook_sha256 = snapshot.workbook_sha256
        now = _format_time(_clock(self._now))
        proposed = ProcessingJob(
            PROCESSING_JOB_SCHEMA_VERSION,
            job_id,
            identity_workbook_id,
            identity_raw_snapshot_id,
            identity_workbook_sha256,
            self._profile_sha256,
            "planning",
            None,
            None,
            None,
            ProcessingProgress(),
            None,
            None,
            now,
            now,
        )
        command_sha256 = _canonical_digest(
            {
                "schema_version": "audit_processing_start_command.v1",
                "job_id": job_id,
                "workbook_id": identity_workbook_id,
                "raw_snapshot_id": identity_raw_snapshot_id,
                "workbook_sha256": identity_workbook_sha256,
                "profile_sha256": self._profile_sha256,
            }
        )
        claim_token = secrets.token_urlsafe(32)
        try:
            claim = self._repository.claim_planning(
                principal=principal,
                idempotency_key=key,
                command_sha256=command_sha256,
                proposed_job=proposed,
                claim_token=claim_token,
            )
        except ProcessingConflictError:
            raise ProcessingServiceError("IDEMPOTENCY_CONFLICT") from None
        except ProcessingRepositoryError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if claim.state == "current":
            self._assert_profile(claim.job)
            return ProcessingSubmission(claim.job, replayed=True)
        if claim.state == "pending":
            raise ProcessingServiceError("JOB_IN_PROGRESS")
        assert claim.claim_token is not None and claim.claim_fence is not None
        try:
            if snapshot is None:
                try:
                    snapshot = self._exact_raw_snapshot(
                        principal, identity_workbook_id, identity_raw_snapshot_id
                    )
                except ProcessingServiceError as error:
                    if error.code == "WORKBOOK_NOT_FOUND":
                        raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
                    raise
                if snapshot.workbook_sha256 != identity_workbook_sha256:
                    raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
            lease = self._lease_keeper(principal=principal, claim=claim)
            with lease, _exclusive_job_lock(self._job_lock_path(principal, job_id), lease):
                package, plan = self._run_planning(
                    principal=principal,
                    snapshot=snapshot,
                    job_id=job_id,
                    lease=lease,
                )
                del package
                plan_sha = json_sha256(plan)
                published = lease.finalize(
                    lambda: self._repository.publish_plan(
                        principal=principal,
                        job_id=job_id,
                        claim_token=claim.claim_token or "",
                        claim_fence=claim.claim_fence or 0,
                        scope_plan=plan,
                        scope_plan_sha256=plan_sha,
                    )
                )
                if not isinstance(published, ProcessingJob):
                    raise ProcessingServiceError("SERVICE_UNAVAILABLE")
                job = published
            return ProcessingSubmission(job, replayed=False)
        except ProcessingServiceError as error:
            self._fail_quietly(
                principal=principal,
                job_id=job_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                failure=ProcessingFailure(
                    (
                        "SERVICE_UNAVAILABLE"
                        if error.code == "SERVICE_UNAVAILABLE"
                        else "DETERMINISTIC_PROCESSING_FAILED"
                    ),
                    "planning",
                    False,
                ),
            )
            raise
        except Exception:
            self._fail_quietly(
                principal=principal,
                job_id=job_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                failure=ProcessingFailure(
                    "DETERMINISTIC_PROCESSING_FAILED", "planning", False
                ),
            )
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED") from None

    def select_scope(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        mode: object,
        scope_plan_sha256: object,
        scope_ids: object,
        idempotency_key: str,
    ) -> ProcessingSubmission:
        try:
            clean_job_id = _job_id(job_id)
            key = _idempotency_key(idempotency_key)
            current = self._repository.get_job(principal=principal, job_id=clean_job_id)
        except (TypeError, ValueError):
            raise ProcessingServiceError("INVALID_REQUEST") from None
        except ProcessingRepositoryError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if current is None:
            raise ProcessingServiceError("JOB_NOT_FOUND")
        self._assert_profile(current)
        if current.status == "planning":
            raise ProcessingServiceError("SCOPE_NOT_READY")
        if current.status == "failed" and current.scope_plan is None:
            # A planning failure never established a selectable command identity.  Execution
            # failures do retain their exact selection/idempotency binding and must flow through
            # the repository below so a different payload cannot masquerade as a replay.
            raise ProcessingServiceError("SCOPE_NOT_READY")
        selection = _selection_from_request(
            current,
            mode=mode,
            scope_plan_sha256=scope_plan_sha256,
            scope_ids=scope_ids,
        )
        command_sha256 = _canonical_digest(
            {
                "schema_version": "audit_processing_selection_command.v1",
                "job_id": clean_job_id,
                "selection": selection.identity(),
                "profile_sha256": self._profile_sha256,
            }
        )
        claim_token = secrets.token_urlsafe(32)
        try:
            claim = self._repository.claim_execution(
                principal=principal,
                job_id=clean_job_id,
                idempotency_key=key,
                command_sha256=command_sha256,
                selection=selection,
                claim_token=claim_token,
            )
        except ProcessingConflictError:
            raise ProcessingServiceError("SCOPE_CONFLICT") from None
        except ProcessingRepositoryError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if claim.state == "current":
            return ProcessingSubmission(claim.job, replayed=True)
        if claim.state == "pending":
            raise ProcessingServiceError("JOB_IN_PROGRESS")
        assert claim.claim_token is not None and claim.claim_fence is not None
        stage_state: dict[str, Literal["preparing", "aggregating", "publishing"]] = {
            "value": "aggregating" if claim.job.status == "aggregating" else "preparing"
        }
        try:
            lease = self._lease_keeper(principal=principal, claim=claim)
            with lease, _exclusive_job_lock(
                self._job_lock_path(principal, claim.job.job_id), lease
            ):
                job = self._run_execution(
                    principal=principal,
                    job=claim.job,
                    selection=selection,
                    claim_token=claim.claim_token,
                    claim_fence=claim.claim_fence,
                    lease=lease,
                    stage_changed=lambda value: stage_state.__setitem__("value", value),
                )
            return ProcessingSubmission(job, replayed=False)
        except ProcessingServiceError as error:
            stage = stage_state["value"]
            if error.code == "AGGREGATION_FAILED":
                stage = "aggregating"
            elif error.code == "PUBLICATION_FAILED":
                stage = "publishing"
            failure_code = {
                "preparing": "PREPARATION_FAILED",
                "aggregating": "AGGREGATION_FAILED",
                "publishing": "PUBLICATION_FAILED",
            }[stage]
            if error.code in {
                "PREPARATION_FAILED",
                "AGGREGATION_FAILED",
                "PUBLICATION_FAILED",
                "SERVICE_UNAVAILABLE",
            }:
                failure_code = error.code
            self._fail_quietly(
                principal=principal,
                job_id=claim.job.job_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                failure=ProcessingFailure(
                    failure_code,
                    stage,
                    False,
                ),
            )
            raise
        except Exception:
            stage = stage_state["value"]
            failure_code = {
                "preparing": "PREPARATION_FAILED",
                "aggregating": "AGGREGATION_FAILED",
                "publishing": "PUBLICATION_FAILED",
            }[stage]
            self._fail_quietly(
                principal=principal,
                job_id=claim.job.job_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                failure=ProcessingFailure(failure_code, stage, False),
            )
            raise ProcessingServiceError(failure_code) from None

    def get_job(self, *, principal: ServicePrincipal, job_id: str) -> ProcessingJob:
        try:
            clean_job_id = _job_id(job_id)
            job = self._repository.get_job(principal=principal, job_id=clean_job_id)
        except (TypeError, ValueError):
            raise ProcessingServiceError("INVALID_REQUEST") from None
        except ProcessingRepositoryError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        if job is None:
            raise ProcessingServiceError("JOB_NOT_FOUND")
        return job

    def resolve(self, *, principal: ServicePrincipal, bundle_id: str) -> BundleSnapshot:
        """Implement ``BundleSnapshotRepository`` over only committed bundle rows."""

        try:
            clean_bundle_id = _bundle_id(bundle_id)
            record = self._repository.get_bundle(
                principal=principal, bundle_id=clean_bundle_id
            )
            if record is None:
                raise BundleSnapshotNotFoundError(bundle_id)
            from .processing_store import StoredPreparedBundle

            stored = StoredPreparedBundle(
                snapshot_id=record.snapshot_id,
                package_manifest_sha256=record.package_manifest_sha256,
                file_count=record.file_count,
                total_bytes=record.total_bytes,
            )
            package = Path(self._bundle_store.resolve(stored))
            if record.sheet is not None:
                load_validated_audit_bundle(package, sheet=record.sheet)
            elif record.aggregate_id is not None:
                load_audit_aggregate(package, record.aggregate_id)
            else:
                load_validated_audit_bundle(package)
            try:
                provider = self._assets.bind_source_provider(
                    principal, record.workbook_id, record.raw_snapshot_id
                )
            except WorkbookAssetServiceError:
                # The committed package remains chat-authoritative.  Raw bytes are an optional
                # inspection capability and may expire under a separate retention policy.
                provider = None
            runtime = (
                self._workspace_root
                / "runtime"
                / _principal_directory(principal)
                / record.bundle_id
            )
            _private_directory(runtime)
            commit_lock_root = runtime / "snapshot-locks"
            _private_directory(commit_lock_root)
            _private_directory(commit_lock_root / ".package_locks")
            return BundleSnapshot(
                bundle_id=record.bundle_id,
                snapshot_id=record.snapshot_id,
                package_path=package,
                runtime_root=runtime,
                workbook_source_provider=provider,
                scope_binding=BundleScopeBinding(
                    sheet=record.sheet,
                    aggregate_id=record.aggregate_id,
                ),
                commit_lock_root=commit_lock_root,
            )
        except BundleSnapshotNotFoundError:
            raise
        except Exception:
            # Authorization misses and corrupted/unavailable private snapshots are both hidden
            # from the public bundle namespace.  Conversation still fails closed before a model.
            raise BundleSnapshotNotFoundError(bundle_id) from None

    def _run_planning(
        self,
        *,
        principal: ServicePrincipal,
        snapshot: RawWorkbookSnapshot,
        job_id: str,
        lease: _LeaseKeeper,
    ) -> tuple[Path, dict[str, object]]:
        try:
            provider = self._assets.bind_source_provider(
                principal, snapshot.workbook_id, snapshot.raw_snapshot_id
            )
            content = read_verified_workbook_source(
                provider,
                expected_sha256=snapshot.workbook_sha256,
                max_bytes=snapshot.size_bytes,
            )
        except WorkbookAssetServiceError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        except WorkbookSourceError as error:
            if error.code == "SOURCE_UNAVAILABLE":
                raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED") from None
        if len(content) != snapshot.size_bytes:
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
        validate_xlsx_archive(content)
        job_root = self._job_root(principal, job_id)
        source = job_root / "source.xlsx"
        _materialize_source(source, content, snapshot.workbook_sha256)
        lease.checkpoint(
            status="planning",
            progress=ProcessingProgress(),
        )
        converted = job_root / "converted"
        _private_directory(converted)
        package = self._converter(
            source,
            converted,
            force=False,
            cv=_converter_version(),
            max_rows=self._max_rows,
            full_names=self._full_names,
        )
        lease.checkpoint(
            status="planning",
            progress=ProcessingProgress(),
        )
        _verify_complete(self._verifier(package, source))
        meta = json.loads((package / "meta.json").read_text(encoding="utf-8"))
        if meta.get("source", {}).get("sha256") != snapshot.workbook_sha256:
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
        plan = _public_scope_plan(self._scope_planner(package))
        return package, plan

    def _run_execution(
        self,
        *,
        principal: ServicePrincipal,
        job: ProcessingJob,
        selection: ProcessingScopeSelection,
        claim_token: str,
        claim_fence: int,
        lease: _LeaseKeeper,
        stage_changed: Callable[
            [Literal["preparing", "aggregating", "publishing"]], None
        ],
    ) -> ProcessingJob:
        package, source = self._existing_package(principal, job)
        if selection.mode == "workbook":
            scopes: tuple[AuditScope, ...] = (AuditScope.workbook(),)
        else:
            scopes = tuple(AuditScope.for_sheet(sheet) for sheet in selection.sheets)
        completed: list[str] = []
        resume_aggregating = job.status == "aggregating"
        if resume_aggregating:
            if selection.mode == "workbook" or len(scopes) < 2:
                raise ProcessingServiceError("AGGREGATION_FAILED")
            for scope in scopes:
                load_validated_audit_bundle(package, sheet=scope.sheet)
                completed.append(scope.id)
        else:
            progress = ProcessingProgress(
                len(scopes),
                0,
                tuple(scope.id for scope in scopes if scope.kind == "sheet"),
            )
            lease.checkpoint(
                status="preparing",
                progress=progress,
            )

        def prepare_one(scope: AuditScope) -> AuditScope:
            client = self._client_factory()
            retriever = self._retriever_factory()
            self._preparer(
                package,
                client=client,
                retriever=retriever,
                retriever_descriptor=self._retriever_descriptor,
                model=self._model,
                scope=scope,
                force=False,
                eprint=self._eprint,
            )
            load_validated_audit_bundle(
                package, sheet=scope.sheet if scope.kind == "sheet" else None
            )
            return scope

        if not resume_aggregating:
            worker_count = min(self._max_prepare_workers, len(scopes))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="audit-prepare",
            ) as executor:
                futures: dict[Future[AuditScope], AuditScope] = {
                    executor.submit(prepare_one, scope): scope for scope in scopes
                }
                try:
                    for future in as_completed(futures):
                        scope = future.result()
                        completed.append(scope.id)
                        remaining = tuple(
                            candidate.id
                            for candidate in scopes
                            if candidate.kind == "sheet"
                            and candidate.id not in completed
                        )
                        lease.checkpoint(
                            status="preparing",
                            progress=ProcessingProgress(
                                len(scopes), len(completed), remaining
                            ),
                        )
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        if len(completed) != len(scopes):
            raise ProcessingServiceError("PREPARATION_FAILED")

        aggregate_id: str | None = None
        sheet_binding: str | None = None
        gate_binding: dict[str, object]
        if selection.mode == "workbook":
            _, facts, context, brief = load_validated_audit_bundle(package)  # type: ignore[misc]
            gate_binding = {
                "kind": "workbook",
                "facts_sha256": json_sha256(facts),
                "standards_sha256": json_sha256(context),
                "brief_sha256": json_sha256(brief),
            }
        elif len(selection.sheets) == 1:
            sheet_binding = selection.sheets[0]
            scope = AuditScope.for_sheet(sheet_binding)
            commit = read_scope_commit(package, scope)
            if commit is None:
                raise ProcessingServiceError("PREPARATION_FAILED")
            load_validated_audit_bundle(package, sheet=sheet_binding)
            gate_binding = {"kind": "sheet", "commit": commit}
        else:
            lease.checkpoint(
                status="aggregating",
                progress=ProcessingProgress(len(scopes), len(scopes), ()),
            )
            stage_changed("aggregating")
            try:
                aggregate = self._aggregator(
                    package,
                    sheets=list(selection.sheets),
                    all_committed_sheets=False,
                    model=self._model,
                    client=self._aggregate_client_factory(),
                    force=False,
                    eprint=self._eprint,
                )
                aggregate_id = aggregate.paths.aggregate_id
                _, _, aggregate_commit = load_audit_aggregate(package, aggregate_id)
                gate_binding = {
                    "kind": "aggregate",
                    "aggregate_id": aggregate_id,
                    "commit": aggregate_commit,
                }
            except Exception:
                raise ProcessingServiceError("AGGREGATION_FAILED") from None

        try:
            _verify_complete(self._verifier(package, source))
        except ProcessingServiceError:
            raise ProcessingServiceError(
                "AGGREGATION_FAILED" if aggregate_id is not None else "PREPARATION_FAILED"
            ) from None
        plan_sheets = job.scope_plan.get("sheets") if job.scope_plan else None
        if not isinstance(plan_sheets, list):
            raise ProcessingServiceError("PUBLICATION_FAILED")
        empty = tuple(
            item["scope"]["sheet"]
            for item in plan_sheets
            if item.get("analyzable") is False
        )
        included = selection.sheets
        if selection.mode == "workbook":
            included = tuple(item["scope"]["sheet"] for item in plan_sheets)
        publication_identity = {
            "schema_version": "audit_processing_bundle_identity.v1",
            "job_id": job.job_id,
            "workbook_id": job.workbook_id,
            "raw_snapshot_id": job.raw_snapshot_id,
            "workbook_sha256": job.workbook_sha256,
            "profile_sha256": job.profile_sha256,
            "scope_plan_sha256": job.scope_plan_sha256,
            "selection_sha256": selection.selection_sha256,
            "gate": gate_binding,
        }
        stage_changed("publishing")
        try:
            stored = self._bundle_store.publish(package, publication_identity)
            snapshot_id = _sha256(
                getattr(stored, "snapshot_id", None), field_name="snapshot_id"
            )
            manifest_sha = _sha256(
                getattr(stored, "package_manifest_sha256", None),
                field_name="package_manifest_sha256",
            )
            file_count = getattr(stored, "file_count", None)
            total_bytes = getattr(stored, "total_bytes", None)
            if (
                not isinstance(file_count, int)
                or isinstance(file_count, bool)
                or file_count < 1
                or not isinstance(total_bytes, int)
                or isinstance(total_bytes, bool)
                or total_bytes < 1
            ):
                raise ValueError("prepared bundle store counts are invalid")
        except Exception:
            raise ProcessingServiceError("PUBLICATION_FAILED") from None
        bundle_id = _derived_id(
            "bundle",
            principal,
            f"{job.job_id}:{selection.selection_sha256}",
            length=48,
        )
        published_at = _format_time(_clock(self._now))
        result = ProcessingResult(
            bundle_id=bundle_id,
            snapshot_id=snapshot_id,
            package_manifest_sha256=manifest_sha,
            selection_sha256=selection.selection_sha256,
            sheet=sheet_binding,
            aggregate_id=aggregate_id,
            included_sheets=included,
            skipped_empty_sheets=empty if selection.mode == "all_sheets" else (),
        )
        record = PublishedBundleRecord(
            bundle_id=bundle_id,
            snapshot_id=snapshot_id,
            package_manifest_sha256=manifest_sha,
            file_count=file_count,
            total_bytes=total_bytes,
            job_id=job.job_id,
            workbook_id=job.workbook_id,
            raw_snapshot_id=job.raw_snapshot_id,
            workbook_sha256=job.workbook_sha256,
            selection_sha256=selection.selection_sha256,
            sheet=sheet_binding,
            aggregate_id=aggregate_id,
            published_at=published_at,
        )
        try:
            published = lease.finalize(
                lambda: self._repository.publish_bundle(
                    principal=principal,
                    job_id=job.job_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    result=result,
                    bundle=record,
                )
            )
        except ProcessingServiceError as error:
            if error.code == "JOB_IN_PROGRESS":
                raise
            raise ProcessingServiceError("PUBLICATION_FAILED") from None
        if not isinstance(published, ProcessingJob):
            raise ProcessingServiceError("PUBLICATION_FAILED")
        return published

    def _existing_package(
        self, principal: ServicePrincipal, job: ProcessingJob
    ) -> tuple[Path, Path]:
        self._assert_profile(job)
        root = self._job_root(principal, job.job_id)
        source = root / "source.xlsx"
        package = root / "converted" / f"source_{job.workbook_sha256[:12]}"
        if not source.is_file() or not package.is_dir():
            raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
        try:
            if hashlib.sha256(source.read_bytes()).hexdigest() != job.workbook_sha256:
                raise ProcessingServiceError("DETERMINISTIC_PROCESSING_FAILED")
        except OSError:
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        _verify_complete(self._verifier(package, source))
        return package, source

    def _exact_raw_snapshot(
        self,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> RawWorkbookSnapshot:
        try:
            snapshot = self._assets.get_snapshot(
                principal, workbook_id, raw_snapshot_id
            )
        except WorkbookAssetServiceError as error:
            code = {
                "INVALID_REQUEST": "INVALID_REQUEST",
                "WORKBOOK_NOT_FOUND": "WORKBOOK_NOT_FOUND",
            }.get(error.code, "SERVICE_UNAVAILABLE")
            raise ProcessingServiceError(code) from None
        if (
            snapshot.workbook_id != workbook_id
            or snapshot.raw_snapshot_id != raw_snapshot_id
        ):
            raise ProcessingServiceError("SERVICE_UNAVAILABLE")
        return snapshot

    def _lease_keeper(
        self,
        *,
        principal: ServicePrincipal,
        claim: ProcessingLeaseClaim,
    ) -> _LeaseKeeper:
        if claim.claim_token is None or claim.claim_fence is None:
            raise ProcessingServiceError("JOB_IN_PROGRESS")
        if claim.job.status not in {"planning", "preparing", "aggregating"}:
            raise ProcessingServiceError("JOB_IN_PROGRESS")
        return _LeaseKeeper(
            repository=self._repository,
            principal=principal,
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            status=claim.job.status,
            progress=claim.job.progress,
            interval_seconds=self._lease_heartbeat_seconds,
        )

    def _job_lock_path(self, principal: ServicePrincipal, job_id: str) -> Path:
        parent = self._workspace_root / "locks" / _principal_directory(principal)
        _private_directory(parent)
        path = parent / f"{_job_id(job_id)}.lock"
        try:
            path.parent.resolve(strict=True).relative_to(self._workspace_root)
        except (OSError, ValueError):
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        return path

    def _job_root(self, principal: ServicePrincipal, job_id: str) -> Path:
        root = (
            self._workspace_root
            / "jobs"
            / _principal_directory(principal)
            / _job_id(job_id)
        )
        _private_directory(root)
        try:
            resolved = root.resolve(strict=True)
            resolved.relative_to(self._workspace_root)
        except (OSError, ValueError):
            raise ProcessingServiceError("SERVICE_UNAVAILABLE") from None
        return resolved

    def _assert_profile(self, job: ProcessingJob) -> None:
        if job.profile_sha256 != self._profile_sha256:
            raise ProcessingServiceError("PROCESSING_PROFILE_CHANGED")

    def _fail_quietly(self, **kwargs: object) -> None:
        try:
            self._repository.fail(**kwargs)  # type: ignore[arg-type]
        except Exception:
            pass


__all__ = [
    "MAX_PLAN_SHEETS",
    "MAX_SELECTED_SCOPES",
    "PROCESSING_JOB_SCHEMA_VERSION",
    "PROCESSING_RESULT_SCHEMA_VERSION",
    "PROCESSING_SCOPE_SELECTION_SCHEMA_VERSION",
    "PreparedBundleStore",
    "ProcessingConflictError",
    "ProcessingFailure",
    "ProcessingJob",
    "ProcessingLeaseClaim",
    "ProcessingLeaseError",
    "ProcessingProgress",
    "ProcessingRepository",
    "ProcessingRepositoryError",
    "ProcessingResult",
    "ProcessingScopeSelection",
    "ProcessingService",
    "ProcessingServiceError",
    "ProcessingSubmission",
    "PublishedBundleRecord",
]
