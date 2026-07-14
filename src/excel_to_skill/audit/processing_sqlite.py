"""Durable SQLite implementation of the audit processing repository contract.

Jobs and published bundle records are principal scoped.  Every operation uses
``BEGIN IMMEDIATE`` so idempotency binding, monotonic lease fencing, state transitions, and final
bundle publication share one serialization order across processes.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from .processing import (
    PROCESSING_JOB_SCHEMA_VERSION,
    PROCESSING_SCOPE_SELECTION_SCHEMA_VERSION,
    ProcessingConflictError,
    ProcessingFailure,
    ProcessingJob,
    ProcessingLeaseClaim,
    ProcessingLeaseError,
    ProcessingProgress,
    ProcessingRepositoryError,
    ProcessingResult,
    ProcessingScopeSelection,
    PublishedBundleRecord,
)
from .service import ServicePrincipal


_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"\Aprocess-[0-9a-f]{48}\Z")
_BUNDLE_ID_RE = re.compile(r"\Abundle-[0-9a-f]{48}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")
_STATUSES = frozenset(
    {"planning", "awaiting_scope", "preparing", "aggregating", "published", "failed"}
)
_JOB_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "workbook_id",
        "raw_snapshot_id",
        "workbook_sha256",
        "profile_sha256",
        "status",
        "scope_plan_sha256",
        "scope_plan",
        "selection",
        "progress",
        "result",
        "failure",
        "created_at",
        "updated_at",
    }
)
_SELECTION_FIELDS = frozenset(
    {"schema_version", "mode", "scope_plan_sha256", "scopes"}
)
_SCOPE_FIELDS = frozenset({"scope_id", "sheet"})
_PROGRESS_FIELDS = frozenset(
    {"total_scopes", "completed_scopes", "current_scope_ids"}
)
_RESULT_FIELDS = frozenset(
    {
        "bundle_id",
        "snapshot_id",
        "package_manifest_sha256",
        "selection_sha256",
        "sheet",
        "aggregate_id",
        "included_sheets",
        "skipped_empty_sheets",
    }
)
_FAILURE_FIELDS = frozenset({"code", "stage", "retryable"})
_BUNDLE_FIELDS = frozenset(
    {
        "bundle_id",
        "snapshot_id",
        "package_manifest_sha256",
        "file_count",
        "total_bytes",
        "job_id",
        "workbook_id",
        "raw_snapshot_id",
        "workbook_sha256",
        "selection_sha256",
        "sheet",
        "aggregate_id",
        "published_at",
    }
)


class SQLiteProcessingRepository:
    """Private restart-safe processing and published-bundle catalog."""

    def __init__(
        self,
        database: Path | str,
        *,
        timeout_seconds: float = 30.0,
        lease_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        candidate = Path(database).expanduser()
        if not candidate.is_absolute():
            raise ProcessingRepositoryError("processing database path must be absolute")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0.1 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        if (
            not isinstance(lease_ttl_seconds, int)
            or isinstance(lease_ttl_seconds, bool)
            or not 1 <= lease_ttl_seconds <= 900
        ):
            raise ValueError("lease_ttl_seconds must be between 1 and 900")
        self._database = candidate
        self._timeout = float(timeout_seconds)
        self._lease_ttl = lease_ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._prepare_database_file()
        try:
            with self._transaction() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, _SCHEMA_VERSION):
                    raise ProcessingRepositoryError(
                        "processing database schema version is unsupported"
                    )
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except ProcessingRepositoryError:
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError(
                "processing database initialization failed"
            ) from None

    @property
    def database_path(self) -> Path:
        return self._database

    @property
    def lease_ttl_seconds(self) -> int:
        """Expose the fixed lease window so the runner can choose a safe heartbeat cadence."""

        return self._lease_ttl

    def claim_planning(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        proposed_job: ProcessingJob,
        claim_token: str,
    ) -> ProcessingLeaseClaim:
        tenant_id, subject_id = _principal_scope(principal)
        _idempotency_key(idempotency_key)
        _sha256(command_sha256, field="command_sha256")
        _token(claim_token)
        if not isinstance(proposed_job, ProcessingJob) or proposed_job.status != "planning":
            raise TypeError("proposed_job must be a planning ProcessingJob")
        if (
            proposed_job.scope_plan is not None
            or proposed_job.scope_plan_sha256 is not None
            or proposed_job.selection is not None
            or proposed_job.result is not None
            or proposed_job.failure is not None
            or proposed_job.progress != ProcessingProgress()
        ):
            raise ProcessingConflictError("proposed planning job is not initial")
        proposed_json = _encode_job(proposed_job)
        try:
            with self._transaction() as connection:
                # BEGIN IMMEDIATE may wait for another writer.  Start the lease only after this
                # transaction owns the write lock so a successful claim cannot already be stale.
                now_epoch, _ = self._clock_values()
                expires_epoch = now_epoch + self._lease_ttl * 1000
                row = connection.execute(
                    """
                    SELECT job_id, command_sha256 FROM start_commands
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                    """,
                    (tenant_id, subject_id, idempotency_key),
                ).fetchone()
                if row is None:
                    if connection.execute(
                        """
                        SELECT 1 FROM processing_jobs
                        WHERE tenant_id=? AND subject_id=? AND job_id=?
                        """,
                        (tenant_id, subject_id, proposed_job.job_id),
                    ).fetchone() is not None:
                        raise ProcessingConflictError("processing job identity collision")
                    connection.execute(
                        """
                        INSERT INTO processing_jobs(
                            tenant_id, subject_id, job_id, status, job_json,
                            lease_token, lease_fence, lease_expires_at,
                            execution_idempotency_key, execution_command_sha256,
                            selection_sha256
                        ) VALUES (?, ?, ?, 'planning', ?, ?, 1, ?, NULL, NULL, NULL)
                        """,
                        (
                            tenant_id,
                            subject_id,
                            proposed_job.job_id,
                            proposed_json,
                            claim_token,
                            expires_epoch,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO start_commands(
                            tenant_id, subject_id, idempotency_key,
                            command_sha256, job_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            tenant_id,
                            subject_id,
                            idempotency_key,
                            command_sha256,
                            proposed_job.job_id,
                        ),
                    )
                    return ProcessingLeaseClaim(
                        "claimed",
                        proposed_job,
                        claim_token=claim_token,
                        claim_fence=1,
                    )
                if row["command_sha256"] != command_sha256:
                    raise ProcessingConflictError("start idempotency conflict")
                if row["job_id"] != proposed_job.job_id:
                    raise ProcessingConflictError("start job identity conflict")
                job_row = self._load_job_row(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=proposed_job.job_id,
                )
                if job_row is None:
                    raise ProcessingRepositoryError("start command job is missing")
                job = _job_from_row(job_row)
                _assert_same_start_identity(job, proposed_job)
                if job.status != "planning":
                    return ProcessingLeaseClaim("current", job)
                if not isinstance(job_row["lease_expires_at"], int):
                    raise ProcessingRepositoryError("planning lease is invalid")
                if job_row["lease_expires_at"] > now_epoch:
                    return ProcessingLeaseClaim("pending", job)
                fence = _next_fence(job_row["lease_fence"])
                changed = connection.execute(
                    """
                    UPDATE processing_jobs
                    SET lease_token=?, lease_fence=?, lease_expires_at=?
                    WHERE tenant_id=? AND subject_id=? AND job_id=?
                      AND status='planning' AND lease_fence=? AND lease_expires_at<=?
                    """,
                    (
                        claim_token,
                        fence,
                        expires_epoch,
                        tenant_id,
                        subject_id,
                        job.job_id,
                        job_row["lease_fence"],
                        now_epoch,
                    ),
                ).rowcount
                if changed != 1:
                    raise ProcessingLeaseError("planning lease changed")
                return ProcessingLeaseClaim(
                    "claimed", job, claim_token=claim_token, claim_fence=fence
                )
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.IntegrityError:
            raise ProcessingConflictError("processing start identity conflict") from None
        except sqlite3.Error:
            raise ProcessingRepositoryError("planning claim failed") from None

    def publish_plan(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        scope_plan: Mapping[str, object],
        scope_plan_sha256: str,
    ) -> ProcessingJob:
        tenant_id, subject_id = _principal_scope(principal)
        _job_identifier(job_id)
        _token(claim_token)
        _fence(claim_fence)
        _sha256(scope_plan_sha256, field="scope_plan_sha256")
        plan = _json_object_copy(scope_plan, label="scope plan")
        from .model import json_sha256

        if json_sha256(plan) != scope_plan_sha256:
            raise ProcessingConflictError("scope plan digest mismatch")
        try:
            with self._transaction() as connection:
                row = self._require_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    allowed_statuses={"planning"},
                )
                current = _job_from_row(row)
                updated = replace(
                    current,
                    status="awaiting_scope",
                    scope_plan_sha256=scope_plan_sha256,
                    scope_plan=plan,
                    progress=ProcessingProgress(),
                    updated_at=self._now_text(),
                )
                self._update_job_and_clear_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    expected=row,
                    updated=updated,
                )
                return _copy_job(updated)
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("scope plan publication failed") from None

    def claim_execution(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        idempotency_key: str,
        command_sha256: str,
        selection: ProcessingScopeSelection,
        claim_token: str,
    ) -> ProcessingLeaseClaim:
        tenant_id, subject_id = _principal_scope(principal)
        _job_identifier(job_id)
        _idempotency_key(idempotency_key)
        _sha256(command_sha256, field="command_sha256")
        _token(claim_token)
        if not isinstance(selection, ProcessingScopeSelection):
            raise TypeError("selection must be a ProcessingScopeSelection")
        try:
            with self._transaction() as connection:
                now_epoch, now_text = self._clock_values()
                expires_epoch = now_epoch + self._lease_ttl * 1000
                row = self._load_job_row(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                )
                if row is None:
                    raise ProcessingConflictError("processing job does not exist")
                job = _job_from_row(row)
                if job.scope_plan_sha256 != selection.scope_plan_sha256:
                    raise ProcessingConflictError("selection is not bound to the current plan")
                selected_digest = selection.selection_sha256
                stored_binding = (
                    row["execution_idempotency_key"],
                    row["execution_command_sha256"],
                    row["selection_sha256"],
                )
                requested_binding = (idempotency_key, command_sha256, selected_digest)
                if job.status == "awaiting_scope":
                    if stored_binding != (None, None, None):
                        raise ProcessingRepositoryError("awaiting job selection binding is invalid")
                    initial_total = 1 if selection.mode == "workbook" else len(selection.scope_ids)
                    updated = replace(
                        job,
                        status="preparing",
                        selection=selection,
                        progress=ProcessingProgress(initial_total, 0, selection.scope_ids),
                        updated_at=now_text,
                    )
                    fence = _next_fence(row["lease_fence"])
                    changed = connection.execute(
                        """
                        UPDATE processing_jobs
                        SET status='preparing', job_json=?, lease_token=?, lease_fence=?,
                            lease_expires_at=?, execution_idempotency_key=?,
                            execution_command_sha256=?, selection_sha256=?
                        WHERE tenant_id=? AND subject_id=? AND job_id=?
                          AND status='awaiting_scope' AND lease_token IS NULL
                          AND lease_expires_at IS NULL AND lease_fence=?
                        """,
                        (
                            _encode_job(updated),
                            claim_token,
                            fence,
                            expires_epoch,
                            idempotency_key,
                            command_sha256,
                            selected_digest,
                            tenant_id,
                            subject_id,
                            job_id,
                            row["lease_fence"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ProcessingLeaseError("scope selection changed")
                    return ProcessingLeaseClaim(
                        "claimed", updated, claim_token=claim_token, claim_fence=fence
                    )
                if stored_binding != requested_binding or job.selection != selection:
                    raise ProcessingConflictError("scope selection conflict")
                if job.status in {"published", "failed"}:
                    return ProcessingLeaseClaim("current", job)
                if job.status not in {"preparing", "aggregating"}:
                    raise ProcessingConflictError("scope selection is not currently executable")
                if not isinstance(row["lease_expires_at"], int):
                    raise ProcessingRepositoryError("execution lease is invalid")
                if row["lease_expires_at"] > now_epoch:
                    return ProcessingLeaseClaim("pending", job)
                fence = _next_fence(row["lease_fence"])
                changed = connection.execute(
                    """
                    UPDATE processing_jobs
                    SET lease_token=?, lease_fence=?, lease_expires_at=?
                    WHERE tenant_id=? AND subject_id=? AND job_id=?
                      AND status=? AND lease_fence=? AND lease_expires_at<=?
                    """,
                    (
                        claim_token,
                        fence,
                        expires_epoch,
                        tenant_id,
                        subject_id,
                        job_id,
                        job.status,
                        row["lease_fence"],
                        now_epoch,
                    ),
                ).rowcount
                if changed != 1:
                    raise ProcessingLeaseError("execution lease changed")
                return ProcessingLeaseClaim(
                    "claimed", job, claim_token=claim_token, claim_fence=fence
                )
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("execution claim failed") from None

    def checkpoint(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        status: Literal["planning", "preparing", "aggregating"],
        progress: ProcessingProgress,
    ) -> ProcessingJob:
        tenant_id, subject_id = _principal_scope(principal)
        if status not in {"planning", "preparing", "aggregating"}:
            raise ValueError("checkpoint status is invalid")
        if not isinstance(progress, ProcessingProgress):
            raise TypeError("progress must be ProcessingProgress")
        try:
            with self._transaction() as connection:
                row = self._require_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    allowed_statuses={"planning", "preparing", "aggregating"},
                )
                current = _job_from_row(row)
                allowed = {
                    "planning": {"planning"},
                    "preparing": {"preparing", "aggregating"},
                    "aggregating": {"aggregating"},
                }
                if status not in allowed[current.status]:
                    raise ProcessingConflictError("processing checkpoint transition is invalid")
                if status == "planning" and progress != ProcessingProgress():
                    raise ProcessingConflictError("planning progress must remain empty")
                if status == "aggregating" and (
                    progress.completed_scopes != progress.total_scopes
                    or progress.current_scope_ids
                ):
                    raise ProcessingConflictError("aggregation requires complete scope progress")
                _, now_text = self._clock_values()
                updated = replace(
                    current,
                    status=status,
                    progress=progress,
                    updated_at=now_text,
                )
                expires_epoch = self._clock_epoch() + self._lease_ttl * 1000
                changed = connection.execute(
                    """
                    UPDATE processing_jobs
                    SET status=?, job_json=?, lease_expires_at=?
                    WHERE tenant_id=? AND subject_id=? AND job_id=?
                      AND status=? AND lease_token=? AND lease_fence=?
                      AND lease_expires_at>?
                    """,
                    (
                        status,
                        _encode_job(updated),
                        expires_epoch,
                        tenant_id,
                        subject_id,
                        job_id,
                        current.status,
                        claim_token,
                        claim_fence,
                        self._clock_epoch(),
                    ),
                ).rowcount
                if changed != 1:
                    raise ProcessingLeaseError("processing checkpoint lease changed")
                return _copy_job(updated)
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("processing checkpoint failed") from None

    def fail(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        failure: ProcessingFailure,
    ) -> ProcessingJob:
        tenant_id, subject_id = _principal_scope(principal)
        if not isinstance(failure, ProcessingFailure):
            raise TypeError("failure must be a ProcessingFailure")
        try:
            with self._transaction() as connection:
                row = self._require_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    allowed_statuses={"planning", "preparing", "aggregating"},
                )
                current = _job_from_row(row)
                valid_stages = {
                    "planning": {"planning"},
                    "preparing": {"preparing", "publishing"},
                    "aggregating": {"aggregating", "publishing"},
                }
                if failure.stage not in valid_stages[current.status]:
                    raise ProcessingConflictError("failure stage does not match job state")
                updated = replace(
                    current,
                    status="failed",
                    result=None,
                    failure=failure,
                    updated_at=self._now_text(),
                )
                self._update_job_and_clear_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    expected=row,
                    updated=updated,
                )
                return _copy_job(updated)
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("processing failure publication failed") from None

    def publish_bundle(
        self,
        *,
        principal: ServicePrincipal,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        result: ProcessingResult,
        bundle: PublishedBundleRecord,
    ) -> ProcessingJob:
        tenant_id, subject_id = _principal_scope(principal)
        if not isinstance(result, ProcessingResult) or not isinstance(
            bundle, PublishedBundleRecord
        ):
            raise TypeError("result and bundle record are required")
        try:
            with self._transaction() as connection:
                row = self._require_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    allowed_statuses={"preparing", "aggregating"},
                )
                current = _job_from_row(row)
                _assert_bundle_identity(current, result, bundle)
                if (
                    current.progress.completed_scopes != current.progress.total_scopes
                    or current.progress.current_scope_ids
                ):
                    raise ProcessingConflictError("processing scopes are incomplete")
                connection.execute(
                    """
                    INSERT INTO published_bundles(
                        tenant_id, subject_id, bundle_id, snapshot_id,
                        bundle_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        subject_id,
                        bundle.bundle_id,
                        bundle.snapshot_id,
                        _encode_bundle(bundle),
                    ),
                )
                updated = replace(
                    current,
                    status="published",
                    result=result,
                    failure=None,
                    updated_at=bundle.published_at,
                )
                self._before_bundle_commit(connection, updated, bundle)
                self._update_job_and_clear_lease(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    expected=row,
                    updated=updated,
                )
                return _copy_job(updated)
        except (ProcessingConflictError, ProcessingLeaseError, ProcessingRepositoryError):
            raise
        except sqlite3.IntegrityError:
            raise ProcessingConflictError("published bundle identity collision") from None
        except sqlite3.Error:
            raise ProcessingRepositoryError("bundle publication failed") from None

    def get_job(
        self, *, principal: ServicePrincipal, job_id: str
    ) -> ProcessingJob | None:
        tenant_id, subject_id = _principal_scope(principal)
        _job_identifier(job_id)
        try:
            with self._transaction() as connection:
                row = self._load_job_row(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    job_id=job_id,
                )
                return None if row is None else _job_from_row(row)
        except ProcessingRepositoryError:
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("processing job lookup failed") from None

    def get_bundle(
        self, *, principal: ServicePrincipal, bundle_id: str
    ) -> PublishedBundleRecord | None:
        tenant_id, subject_id = _principal_scope(principal)
        _bundle_identifier(bundle_id)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT snapshot_id, bundle_json FROM published_bundles
                    WHERE tenant_id=? AND subject_id=? AND bundle_id=?
                    """,
                    (tenant_id, subject_id, bundle_id),
                ).fetchone()
                if row is None:
                    return None
                bundle = _decode_bundle(row["bundle_json"])
                if bundle.bundle_id != bundle_id or bundle.snapshot_id != row["snapshot_id"]:
                    raise ProcessingRepositoryError("stored bundle index is invalid")
                return bundle
        except ProcessingRepositoryError:
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("published bundle lookup failed") from None

    def bundle_count(self, principal: ServicePrincipal) -> int:
        """Return a principal-scoped diagnostic count without exposing bundle records."""

        tenant_id, subject_id = _principal_scope(principal)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS bundle_count FROM published_bundles
                    WHERE tenant_id=? AND subject_id=?
                    """,
                    (tenant_id, subject_id),
                ).fetchone()
                if row is None or not isinstance(row["bundle_count"], int):
                    raise ProcessingRepositoryError("published bundle count is invalid")
                return int(row["bundle_count"])
        except ProcessingRepositoryError:
            raise
        except sqlite3.Error:
            raise ProcessingRepositoryError("published bundle count failed") from None

    def _load_job_row(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        job_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT status, job_json, lease_token, lease_fence, lease_expires_at,
                   execution_idempotency_key, execution_command_sha256,
                   selection_sha256
            FROM processing_jobs
            WHERE tenant_id=? AND subject_id=? AND job_id=?
            """,
            (tenant_id, subject_id, job_id),
        ).fetchone()

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        job_id: str,
        claim_token: str,
        claim_fence: int,
        allowed_statuses: set[str],
    ) -> sqlite3.Row:
        _job_identifier(job_id)
        _token(claim_token)
        _fence(claim_fence)
        row = self._load_job_row(
            connection,
            tenant_id=tenant_id,
            subject_id=subject_id,
            job_id=job_id,
        )
        if (
            row is None
            or row["status"] not in allowed_statuses
            or row["lease_token"] != claim_token
            or row["lease_fence"] != claim_fence
            or not isinstance(row["lease_expires_at"], int)
            or row["lease_expires_at"] <= self._clock_epoch()
        ):
            raise ProcessingLeaseError("processing lease mismatch")
        return row

    def _update_job_and_clear_lease(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        expected: sqlite3.Row,
        updated: ProcessingJob,
    ) -> None:
        changed = connection.execute(
            """
            UPDATE processing_jobs
            SET status=?, job_json=?, lease_token=NULL, lease_expires_at=NULL
            WHERE tenant_id=? AND subject_id=? AND job_id=?
              AND status=? AND lease_token=? AND lease_fence=?
              AND lease_expires_at>?
            """,
            (
                updated.status,
                _encode_job(updated),
                tenant_id,
                subject_id,
                updated.job_id,
                expected["status"],
                expected["lease_token"],
                expected["lease_fence"],
                self._clock_epoch(),
            ),
        ).rowcount
        if changed != 1:
            raise ProcessingLeaseError("processing lease changed")

    def _before_bundle_commit(
        self,
        connection: sqlite3.Connection,
        job: ProcessingJob,
        bundle: PublishedBundleRecord,
    ) -> None:
        """Test seam proving bundle insert and job publication roll back together."""

    def _clock_values(self) -> tuple[int, str]:
        try:
            value = self._now()
        except Exception:
            raise ProcessingRepositoryError("processing repository clock failed") from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProcessingRepositoryError("processing repository clock is invalid")
        utc = value.astimezone(timezone.utc)
        # Millisecond precision prevents a one-second lease from becoming unrenewable merely
        # because two heartbeats straddle an integer-second boundary.
        return int(utc.timestamp() * 1000), _format_time(utc)

    def _clock_epoch(self) -> int:
        return self._clock_values()[0]

    def _now_text(self) -> str:
        return self._clock_values()[1]

    def _prepare_database_file(self) -> None:
        parent = self._database.parent
        _assert_no_symlink_components(parent)
        try:
            metadata = parent.stat(follow_symlinks=False)
        except OSError:
            raise ProcessingRepositoryError("processing database parent is unavailable") from None
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
            raise ProcessingRepositoryError("processing database parent is invalid")
        if self._database.is_symlink():
            raise ProcessingRepositoryError("processing database cannot be a symbolic link")
        if self._database.exists():
            try:
                metadata = self._database.stat(follow_symlinks=False)
            except OSError:
                raise ProcessingRepositoryError("processing database is unavailable") from None
            if not stat.S_ISREG(metadata.st_mode):
                raise ProcessingRepositoryError("processing database must be a regular file")
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._database, flags, 0o600)
            except FileExistsError:
                if self._database.is_symlink():
                    raise ProcessingRepositoryError(
                        "processing database cannot be a symbolic link"
                    ) from None
            except OSError:
                raise ProcessingRepositoryError("processing database creation failed") from None
            else:
                os.close(descriptor)
        _private_file(self._database)

    def _connect(self) -> sqlite3.Connection:
        _assert_no_symlink_components(self._database)
        if self._database.is_symlink():
            raise ProcessingRepositoryError("processing database cannot be a symbolic link")
        _private_file(self._database)
        try:
            connection = sqlite3.connect(
                self._database,
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=MEMORY")
            return connection
        except sqlite3.Error:
            raise ProcessingRepositoryError("processing database connection failed") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            # Check/chmod SQLite's transient files while rollback is still possible.  A fallible
            # post-commit check would make callers observe an error after durable publication.
            self._secure_sidecars()
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _secure_sidecars(self) -> None:
        _private_file(self._database)
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = Path(str(self._database) + suffix)
            if candidate.is_symlink():
                raise ProcessingRepositoryError("processing database sidecar is invalid")
            if candidate.exists():
                _private_file(candidate)


def _job_document(job: ProcessingJob) -> dict[str, object]:
    if not isinstance(job, ProcessingJob):
        raise TypeError("job must be a ProcessingJob")
    return {
        "schema_version": job.schema_version,
        "job_id": job.job_id,
        "workbook_id": job.workbook_id,
        "raw_snapshot_id": job.raw_snapshot_id,
        "workbook_sha256": job.workbook_sha256,
        "profile_sha256": job.profile_sha256,
        "status": job.status,
        "scope_plan_sha256": job.scope_plan_sha256,
        "scope_plan": None if job.scope_plan is None else _json_object_copy(job.scope_plan, label="scope plan"),
        "selection": None if job.selection is None else job.selection.identity(),
        "progress": job.progress.to_public_dict(),
        "result": None
        if job.result is None
        else {
            "bundle_id": job.result.bundle_id,
            "snapshot_id": job.result.snapshot_id,
            "package_manifest_sha256": job.result.package_manifest_sha256,
            "selection_sha256": job.result.selection_sha256,
            "sheet": job.result.sheet,
            "aggregate_id": job.result.aggregate_id,
            "included_sheets": list(job.result.included_sheets),
            "skipped_empty_sheets": list(job.result.skipped_empty_sheets),
        },
        "failure": None if job.failure is None else job.failure.to_public_dict(),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _encode_job(job: ProcessingJob) -> str:
    try:
        return _canonical_json(_job_document(job))
    except ProcessingRepositoryError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise ProcessingRepositoryError("processing job is not canonical JSON") from None


def _decode_job(value: object) -> ProcessingJob:
    document = _decode_document(value, fields=_JOB_FIELDS, label="processing job")
    try:
        selection = _decode_selection(document["selection"])
        progress = _decode_progress(document["progress"])
        result = _decode_result(document["result"])
        failure = _decode_failure(document["failure"])
        scope_plan = document["scope_plan"]
        if scope_plan is not None and not isinstance(scope_plan, dict):
            raise ValueError("scope plan is invalid")
        return ProcessingJob(
            schema_version=document["schema_version"],
            job_id=document["job_id"],
            workbook_id=document["workbook_id"],
            raw_snapshot_id=document["raw_snapshot_id"],
            workbook_sha256=document["workbook_sha256"],
            profile_sha256=document["profile_sha256"],
            status=document["status"],
            scope_plan_sha256=document["scope_plan_sha256"],
            scope_plan=scope_plan,
            selection=selection,
            progress=progress,
            result=result,
            failure=failure,
            created_at=document["created_at"],
            updated_at=document["updated_at"],
        )
    except (TypeError, ValueError, KeyError):
        raise ProcessingRepositoryError("stored processing job is invalid") from None


def _decode_selection(value: object) -> ProcessingScopeSelection | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _SELECTION_FIELDS:
        raise ValueError("selection is invalid")
    if value.get("schema_version") != PROCESSING_SCOPE_SELECTION_SCHEMA_VERSION:
        raise ValueError("selection schema is invalid")
    scopes = value.get("scopes")
    if not isinstance(scopes, list):
        raise ValueError("selection scopes are invalid")
    ids: list[str] = []
    sheets: list[str] = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
            raise ValueError("selection scope is invalid")
        ids.append(scope["scope_id"])
        sheets.append(scope["sheet"])
    return ProcessingScopeSelection(
        mode=value["mode"],
        scope_plan_sha256=value["scope_plan_sha256"],
        scope_ids=tuple(ids),
        sheets=tuple(sheets),
    )


def _decode_progress(value: object) -> ProcessingProgress:
    if not isinstance(value, dict) or set(value) != _PROGRESS_FIELDS:
        raise ValueError("progress is invalid")
    scope_ids = value.get("current_scope_ids")
    if not isinstance(scope_ids, list):
        raise ValueError("progress scope IDs are invalid")
    return ProcessingProgress(
        total_scopes=value["total_scopes"],
        completed_scopes=value["completed_scopes"],
        current_scope_ids=tuple(scope_ids),
    )


def _decode_result(value: object) -> ProcessingResult | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _RESULT_FIELDS:
        raise ValueError("processing result is invalid")
    included = value.get("included_sheets")
    skipped = value.get("skipped_empty_sheets")
    if not isinstance(included, list) or not isinstance(skipped, list):
        raise ValueError("processing result coverage is invalid")
    return ProcessingResult(
        bundle_id=value["bundle_id"],
        snapshot_id=value["snapshot_id"],
        package_manifest_sha256=value["package_manifest_sha256"],
        selection_sha256=value["selection_sha256"],
        sheet=value["sheet"],
        aggregate_id=value["aggregate_id"],
        included_sheets=tuple(included),
        skipped_empty_sheets=tuple(skipped),
    )


def _decode_failure(value: object) -> ProcessingFailure | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _FAILURE_FIELDS:
        raise ValueError("processing failure is invalid")
    return ProcessingFailure(
        code=value["code"], stage=value["stage"], retryable=value["retryable"]
    )


def _bundle_document(bundle: PublishedBundleRecord) -> dict[str, object]:
    if not isinstance(bundle, PublishedBundleRecord):
        raise TypeError("bundle must be a PublishedBundleRecord")
    return {
        "bundle_id": bundle.bundle_id,
        "snapshot_id": bundle.snapshot_id,
        "package_manifest_sha256": bundle.package_manifest_sha256,
        "file_count": bundle.file_count,
        "total_bytes": bundle.total_bytes,
        "job_id": bundle.job_id,
        "workbook_id": bundle.workbook_id,
        "raw_snapshot_id": bundle.raw_snapshot_id,
        "workbook_sha256": bundle.workbook_sha256,
        "selection_sha256": bundle.selection_sha256,
        "sheet": bundle.sheet,
        "aggregate_id": bundle.aggregate_id,
        "published_at": bundle.published_at,
    }


def _encode_bundle(bundle: PublishedBundleRecord) -> str:
    try:
        return _canonical_json(_bundle_document(bundle))
    except (TypeError, ValueError, UnicodeError):
        raise ProcessingRepositoryError("published bundle is not canonical JSON") from None


def _decode_bundle(value: object) -> PublishedBundleRecord:
    document = _decode_document(value, fields=_BUNDLE_FIELDS, label="published bundle")
    try:
        if document["sheet"] is not None and not isinstance(document["sheet"], str):
            raise ValueError("bundle sheet is invalid")
        return PublishedBundleRecord(**document)
    except (TypeError, ValueError, KeyError):
        raise ProcessingRepositoryError("stored published bundle is invalid") from None


def _decode_document(
    value: object, *, fields: frozenset[str], label: str
) -> dict[str, object]:
    if not isinstance(value, str):
        raise ProcessingRepositoryError(f"stored {label} is invalid")
    try:
        document = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("invalid JSON number")
            ),
        )
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ProcessingRepositoryError(f"stored {label} is invalid") from None
    if not isinstance(document, dict) or set(document) != fields:
        raise ProcessingRepositoryError(f"stored {label} fields are invalid")
    if _canonical_json(document) != value:
        raise ProcessingRepositoryError(f"stored {label} is not canonical")
    return document


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object_copy(value: Mapping[str, object], *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        encoded = _canonical_json(dict(value))
        copied = json.loads(encoded, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ProcessingRepositoryError(f"{label} is not canonical JSON") from None
    if not isinstance(copied, dict):
        raise ProcessingRepositoryError(f"{label} must be an object")
    return copied


def _copy_job(job: ProcessingJob) -> ProcessingJob:
    return _decode_job(_encode_job(job))


def _job_from_row(row: sqlite3.Row) -> ProcessingJob:
    job = _decode_job(row["job_json"])
    if job.status != row["status"]:
        raise ProcessingRepositoryError("stored processing job index is invalid")
    selected = None if job.selection is None else job.selection.selection_sha256
    if selected != row["selection_sha256"]:
        raise ProcessingRepositoryError("stored processing selection index is invalid")
    if job.status in {"awaiting_scope", "published", "failed"}:
        if row["lease_token"] is not None or row["lease_expires_at"] is not None:
            raise ProcessingRepositoryError("terminal or waiting job has an active lease")
    elif job.status in {"planning", "preparing", "aggregating"}:
        if row["lease_token"] is None or not isinstance(row["lease_expires_at"], int):
            raise ProcessingRepositoryError("active processing job has no lease")
    if not isinstance(row["lease_fence"], int) or row["lease_fence"] < 1:
        raise ProcessingRepositoryError("stored processing fence is invalid")
    return job


def _assert_same_start_identity(current: ProcessingJob, proposed: ProcessingJob) -> None:
    if (
        current.job_id != proposed.job_id
        or current.workbook_id != proposed.workbook_id
        or current.raw_snapshot_id != proposed.raw_snapshot_id
        or current.workbook_sha256 != proposed.workbook_sha256
        or current.profile_sha256 != proposed.profile_sha256
    ):
        raise ProcessingConflictError("processing start identity conflict")


def _assert_bundle_identity(
    job: ProcessingJob,
    result: ProcessingResult,
    bundle: PublishedBundleRecord,
) -> None:
    selection = job.selection
    if selection is None:
        raise ProcessingConflictError("published job has no selection")
    if (
        bundle.job_id != job.job_id
        or bundle.workbook_id != job.workbook_id
        or bundle.raw_snapshot_id != job.raw_snapshot_id
        or bundle.workbook_sha256 != job.workbook_sha256
        or bundle.selection_sha256 != selection.selection_sha256
        or result.selection_sha256 != selection.selection_sha256
        or bundle.bundle_id != result.bundle_id
        or bundle.snapshot_id != result.snapshot_id
        or bundle.package_manifest_sha256 != result.package_manifest_sha256
        or bundle.sheet != result.sheet
        or bundle.aggregate_id != result.aggregate_id
    ):
        raise ProcessingConflictError("published bundle does not match processing job")


def _principal_scope(principal: ServicePrincipal) -> tuple[str, str]:
    if not isinstance(principal, ServicePrincipal):
        raise TypeError("principal must be a ServicePrincipal")
    return principal.scope


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


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 identifier")
    return value


def _job_identifier(value: object) -> str:
    if not isinstance(value, str) or _JOB_ID_RE.fullmatch(value) is None:
        raise ValueError("job_id is invalid")
    return value


def _bundle_identifier(value: object) -> str:
    if not isinstance(value, str) or _BUNDLE_ID_RE.fullmatch(value) is None:
        raise ValueError("bundle_id is invalid")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError("claim_token is invalid")
    return value


def _fence(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value < 2**63 - 1
    ):
        raise ValueError("claim_fence is invalid")
    return value


def _next_fence(value: object) -> int:
    current = _fence(value)
    if current >= 2**63 - 2:
        raise ProcessingRepositoryError("processing lease fence exhausted")
    return current + 1


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise ProcessingRepositoryError("processing database path is unavailable") from None
        except OSError:
            raise ProcessingRepositoryError("processing database path is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ProcessingRepositoryError("processing database path contains a symlink")


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600, follow_symlinks=False)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise ProcessingRepositoryError(
            "processing database permissions are unavailable"
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProcessingRepositoryError("processing database is not private")


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS processing_jobs(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'planning', 'awaiting_scope', 'preparing',
            'aggregating', 'published', 'failed'
        )),
        job_json TEXT NOT NULL,
        lease_token TEXT,
        lease_fence INTEGER NOT NULL CHECK(lease_fence >= 1),
        lease_expires_at INTEGER,
        execution_idempotency_key TEXT,
        execution_command_sha256 TEXT,
        selection_sha256 TEXT,
        PRIMARY KEY(tenant_id, subject_id, job_id),
        CHECK(
            (lease_token IS NULL AND lease_expires_at IS NULL)
            OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
        CHECK(
            (execution_idempotency_key IS NULL AND execution_command_sha256 IS NULL
             AND selection_sha256 IS NULL)
            OR (execution_idempotency_key IS NOT NULL AND execution_command_sha256 IS NOT NULL
                AND selection_sha256 IS NOT NULL)
        )
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS start_commands(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        command_sha256 TEXT NOT NULL,
        job_id TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, idempotency_key),
        FOREIGN KEY(tenant_id, subject_id, job_id)
          REFERENCES processing_jobs(tenant_id, subject_id, job_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS published_bundles(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        bundle_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        bundle_json TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, bundle_id),
        UNIQUE(tenant_id, subject_id, snapshot_id)
    ) WITHOUT ROWID
    """,
)


__all__ = ["SQLiteProcessingRepository"]
