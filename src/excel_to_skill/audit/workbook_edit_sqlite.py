"""Durable SQLite repository for the approved workbook-edit state machine.

The reference in-memory repository is intentionally single-process.  This implementation keeps
the same compare-and-swap, idempotency, workbook-wide lease, monotonic fence, and raw snapshot-head
semantics in one private SQLite database.  Every public operation owns a ``BEGIN IMMEDIATE``
transaction so separate application workers observe one serialization order.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .service import ServicePrincipal
from .workbook_edit import MAX_SAFE_INTEGER
from .workbook_edit_service import (
    WorkbookEditCommandClaim,
    WorkbookEditConflictError,
    WorkbookEditIdempotencyClaimError,
    WorkbookEditIdempotencyConflictError,
    WorkbookEditPublicationClaimError,
    WorkbookEditReceipt,
    WorkbookEditRepositoryError,
    WorkbookEditWorkflow,
    WorkbookSessionBinding,
)


_SCHEMA_VERSION = 1
_WORKFLOW_SCHEMA = "audit_workbook_edit_repository_workflow.v1"
_WORKFLOW_STATES = frozenset(
    {
        "proposed",
        "previewed",
        "approved",
        "rejected",
        "claimed",
        "apply_started",
        "session_verified",
        "verification_failed",
        "indeterminate",
        "stale_precondition",
        "aborted_before_apply",
    }
)
_WORKFLOW_FIELDS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "binding",
        "state",
        "version",
        "proposal",
        "preview",
        "approval",
        "approval_consumed",
        "execution_id",
        "fence",
        "challenge",
        "execution_deadline",
        "manifest",
        "witness",
        "verification",
        "publication",
        "publication_required",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "session_id",
        "tenant_id",
        "subject_id",
        "bundle_id",
        "snapshot_id",
        "workbook_sha256",
        "revision_id",
        "sheet",
        "worksheet_id",
        "workbook_instance_id",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "command_id",
        "workflow_id",
        "session_id",
        "bundle_id",
        "snapshot_id",
        "workbook_sha256",
        "revision_id",
        "sheet",
        "state",
        "details",
    }
)


class SQLiteWorkbookEditRepository:
    """Private durable repository shared safely by multiple local workers."""

    def __init__(
        self,
        database: Path | str,
        *,
        timeout_seconds: float = 30.0,
        command_claim_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        candidate = Path(database).expanduser()
        if not candidate.is_absolute():
            raise WorkbookEditRepositoryError("workbook edit database path must be absolute")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0.1 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        if (
            not isinstance(command_claim_ttl_seconds, int)
            or isinstance(command_claim_ttl_seconds, bool)
            or not 1 <= command_claim_ttl_seconds <= 900
        ):
            raise ValueError("command_claim_ttl_seconds must be between 1 and 900")
        self._database = candidate
        self._timeout = float(timeout_seconds)
        self._command_claim_ttl_seconds = command_claim_ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._prepare_database_file()
        try:
            with self._transaction() as connection:
                for statement in _DDL:
                    connection.execute(statement)
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, _SCHEMA_VERSION):
                    raise WorkbookEditRepositoryError(
                        "workbook edit database schema version is unsupported"
                    )
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except WorkbookEditRepositoryError:
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError(
                "workbook edit database initialization failed"
            ) from None

    @property
    def database_path(self) -> Path:
        return self._database

    def _clock_epoch(self) -> int:
        try:
            value = self._now()
        except Exception:
            raise WorkbookEditRepositoryError("workbook edit repository clock failed") from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WorkbookEditRepositoryError("workbook edit repository clock is invalid")
        return int(value.astimezone(timezone.utc).timestamp())

    def claim_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> WorkbookEditCommandClaim:
        tenant_id, subject_id = _principal_scope(principal)
        now_epoch = self._clock_epoch()
        expires_epoch = now_epoch + self._command_claim_ttl_seconds
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT command_sha256, command_id, state, claim_token, receipt_json,
                           claim_expires_at
                    FROM commands
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                    """,
                    (tenant_id, subject_id, idempotency_key),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO commands(
                            tenant_id, subject_id, idempotency_key, command_sha256,
                            command_id, state, claim_token, receipt_json, claim_expires_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, ?)
                        """,
                        (
                            tenant_id,
                            subject_id,
                            idempotency_key,
                            command_sha256,
                            command_id,
                            claim_token,
                            expires_epoch,
                        ),
                    )
                    return WorkbookEditCommandClaim("claimed", claim_token=claim_token)
                if row["command_sha256"] != command_sha256:
                    raise WorkbookEditIdempotencyConflictError("idempotency conflict")
                if row["state"] == "completed":
                    if row["receipt_json"] is None:
                        raise WorkbookEditRepositoryError("completed command has no receipt")
                    return WorkbookEditCommandClaim(
                        "completed",
                        receipt=_decode_receipt(row["receipt_json"]),
                    )
                if row["state"] != "pending":
                    raise WorkbookEditRepositoryError("command state is invalid")
                if not isinstance(row["claim_expires_at"], int):
                    raise WorkbookEditRepositoryError("command claim lease is invalid")
                if row["claim_expires_at"] <= now_epoch:
                    changed = connection.execute(
                        """
                        UPDATE commands
                        SET command_id=?, claim_token=?, claim_expires_at=?
                        WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                          AND command_sha256=? AND state='pending'
                          AND claim_expires_at<=?
                        """,
                        (
                            command_id,
                            claim_token,
                            expires_epoch,
                            tenant_id,
                            subject_id,
                            idempotency_key,
                            command_sha256,
                            now_epoch,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise WorkbookEditIdempotencyClaimError(
                            "command claim lease changed"
                        )
                    return WorkbookEditCommandClaim("claimed", claim_token=claim_token)
                return WorkbookEditCommandClaim("pending")
        except (
            WorkbookEditRepositoryError,
            WorkbookEditIdempotencyConflictError,
        ):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("workbook edit command claim failed") from None

    def get_workflow(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookEditWorkflow | None:
        tenant_id, subject_id = _principal_scope(principal)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT version, workflow_json
                    FROM workflows
                    WHERE tenant_id=? AND subject_id=? AND workflow_id=?
                    """,
                    (tenant_id, subject_id, workflow_id),
                ).fetchone()
                if row is None:
                    return None
                workflow = _decode_workflow(row["workflow_json"])
                _assert_stored_workflow(
                    workflow,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                    version=row["version"],
                )
                return workflow
        except WorkbookEditRepositoryError:
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("workbook edit workflow read failed") from None

    def assert_snapshot_head(self, *, binding: WorkbookSessionBinding) -> None:
        if not isinstance(binding, WorkbookSessionBinding):
            raise TypeError("binding must be a WorkbookSessionBinding")
        try:
            with self._transaction() as connection:
                self._assert_snapshot_head(connection, binding)
        except WorkbookEditConflictError:
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("snapshot head read failed") from None

    def publish_transition(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        workflow: WorkbookEditWorkflow,
        expected_version: int | None,
        receipt: WorkbookEditReceipt,
        release_active: bool = False,
        require_active: bool = False,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        copied_workflow = _copy_workflow(workflow)
        copied_receipt = _copy_receipt(receipt)
        _assert_workflow_principal(copied_workflow, tenant_id, subject_id)
        _assert_receipt_for_workflow(copied_receipt, copied_workflow)
        workflow_json = _encode_workflow(copied_workflow)
        receipt_json = _encode_receipt(copied_receipt)
        try:
            with self._transaction() as connection:
                self._require_command_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                )
                current = self._load_workflow(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=copied_workflow.workflow_id,
                )
                if expected_version is None:
                    if current is not None:
                        raise WorkbookEditConflictError("workflow already exists")
                elif current is None or current.version != expected_version:
                    raise WorkbookEditConflictError("workflow version changed")
                elif current.binding != copied_workflow.binding:
                    raise WorkbookEditConflictError("workflow binding changed")
                active = self._active_row(connection, copied_workflow.binding)
                if release_active or require_active:
                    if not _active_matches(
                        active,
                        subject_id=subject_id,
                        workflow_id=copied_workflow.workflow_id,
                        execution_id=copied_workflow.execution_id,
                        fence=copied_workflow.fence,
                    ):
                        raise WorkbookEditConflictError("execution claim changed")
                if expected_version is None:
                    connection.execute(
                        """
                        INSERT INTO workflows(
                            tenant_id, subject_id, workflow_id, version, workflow_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            tenant_id,
                            subject_id,
                            copied_workflow.workflow_id,
                            copied_workflow.version,
                            workflow_json,
                        ),
                    )
                else:
                    changed = connection.execute(
                        """
                        UPDATE workflows SET version=?, workflow_json=?
                        WHERE tenant_id=? AND subject_id=? AND workflow_id=? AND version=?
                        """,
                        (
                            copied_workflow.version,
                            workflow_json,
                            tenant_id,
                            subject_id,
                            copied_workflow.workflow_id,
                            expected_version,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise WorkbookEditConflictError("workflow version changed")
                if release_active:
                    self._delete_active(
                        connection,
                        binding=copied_workflow.binding,
                        subject_id=subject_id,
                        workflow_id=copied_workflow.workflow_id,
                        execution_id=copied_workflow.execution_id,
                        fence=copied_workflow.fence,
                    )
                self._complete_command(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                    receipt_json=receipt_json,
                )
        except (
            WorkbookEditConflictError,
            WorkbookEditIdempotencyClaimError,
            WorkbookEditRepositoryError,
        ):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("workbook edit transition failed") from None

    def claim_execution(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        workflow_id: str,
        expected_version: int,
        execution_id: str,
        challenge: str,
        publication_required: bool,
        manifest_factory: Callable[[int], Mapping[str, object]],
        receipt_factory: Callable[[WorkbookEditWorkflow], WorkbookEditReceipt],
    ) -> WorkbookEditReceipt:
        tenant_id, subject_id = _principal_scope(principal)
        if not isinstance(publication_required, bool):
            raise TypeError("publication_required must be a boolean")
        try:
            with self._transaction() as connection:
                self._require_command_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                )
                current = self._load_workflow(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                )
                if current is None or current.version != expected_version:
                    raise WorkbookEditConflictError("workflow version changed")
                self._assert_snapshot_head(connection, current.binding)
                if self._active_row(connection, current.binding) is not None:
                    raise WorkbookEditConflictError("session already has an active execution")
                fence_row = connection.execute(
                    """
                    SELECT fence FROM workbook_fences
                    WHERE tenant_id=? AND workbook_instance_id=?
                    """,
                    (tenant_id, current.binding.workbook_instance_id),
                ).fetchone()
                fence = (0 if fence_row is None else int(fence_row["fence"])) + 1
                if fence > MAX_SAFE_INTEGER:
                    raise WorkbookEditRepositoryError("execution fence exhausted")
                manifest = copy.deepcopy(dict(manifest_factory(fence)))
                claimed = replace(
                    current,
                    state="claimed",
                    version=current.version + 1,
                    approval_consumed=True,
                    execution_id=execution_id,
                    fence=fence,
                    challenge=challenge,
                    manifest=manifest,
                    publication_required=publication_required,
                )
                copied_claimed = _copy_workflow(claimed)
                receipt = _copy_receipt(receipt_factory(copied_claimed))
                _assert_receipt_for_workflow(receipt, copied_claimed)
                workflow_json = _encode_workflow(copied_claimed)
                receipt_json = _encode_receipt(receipt)
                changed = connection.execute(
                    """
                    UPDATE workflows SET version=?, workflow_json=?
                    WHERE tenant_id=? AND subject_id=? AND workflow_id=? AND version=?
                    """,
                    (
                        copied_claimed.version,
                        workflow_json,
                        tenant_id,
                        subject_id,
                        workflow_id,
                        expected_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookEditConflictError("workflow version changed")
                connection.execute(
                    """
                    INSERT INTO active_workbooks(
                        tenant_id, workbook_instance_id, subject_id,
                        workflow_id, execution_id, fence
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        current.binding.workbook_instance_id,
                        subject_id,
                        workflow_id,
                        execution_id,
                        fence,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO workbook_fences(tenant_id, workbook_instance_id, fence)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tenant_id, workbook_instance_id)
                    DO UPDATE SET fence=excluded.fence
                    """,
                    (tenant_id, current.binding.workbook_instance_id, fence),
                )
                self._complete_command(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                    receipt_json=receipt_json,
                )
                return _decode_receipt(receipt_json)
        except (
            WorkbookEditConflictError,
            WorkbookEditIdempotencyClaimError,
            WorkbookEditRepositoryError,
        ):
            raise
        except sqlite3.IntegrityError:
            raise WorkbookEditConflictError("session already has an active execution") from None
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("workbook execution claim failed") from None

    def claim_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        publication_claim_token: str,
        claim_expires_at: str,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        expires_epoch = _iso_epoch(claim_expires_at)
        now_epoch = self._clock_epoch()
        if expires_epoch <= now_epoch:
            raise WorkbookEditConflictError("publication claim already expired")
        try:
            with self._transaction() as connection:
                workflow = self._load_workflow(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                )
                if (
                    workflow is None
                    or workflow.state != "session_verified"
                    or workflow.execution_id != execution_id
                    or workflow.fence != fence
                    or workflow.publication is not None
                ):
                    raise WorkbookEditConflictError("publication basis changed")
                active = self._active_row(connection, workflow.binding)
                if not _active_matches(
                    active,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                    execution_id=execution_id,
                    fence=fence,
                ):
                    raise WorkbookEditConflictError("publication lease changed")
                existing_token = active["publication_claim_token"]
                existing_expiry = active["publication_claim_expires_at"]
                if existing_token == publication_claim_token:
                    return
                if (
                    existing_token is not None
                    and isinstance(existing_expiry, int)
                    and existing_expiry > now_epoch
                ):
                    raise WorkbookEditPublicationClaimError(
                        "publication already in progress"
                    )
                changed = connection.execute(
                    """
                    UPDATE active_workbooks
                    SET publication_claim_token=?, publication_claim_expires_at=?
                    WHERE tenant_id=? AND workbook_instance_id=?
                      AND subject_id=? AND workflow_id=? AND execution_id=? AND fence=?
                      AND (
                        publication_claim_token IS NULL
                        OR publication_claim_expires_at<=?
                      )
                    """,
                    (
                        publication_claim_token,
                        expires_epoch,
                        tenant_id,
                        workflow.binding.workbook_instance_id,
                        subject_id,
                        workflow_id,
                        execution_id,
                        fence,
                        now_epoch,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookEditPublicationClaimError(
                        "publication already in progress"
                    )
        except (WorkbookEditConflictError, WorkbookEditRepositoryError):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("publication claim failed") from None

    def release_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        publication_claim_token: str,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        try:
            with self._transaction() as connection:
                workflow = self._load_workflow(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                )
                if workflow is None:
                    raise WorkbookEditConflictError("publication basis changed")
                changed = connection.execute(
                    """
                    UPDATE active_workbooks
                    SET publication_claim_token=NULL, publication_claim_expires_at=NULL
                    WHERE tenant_id=? AND workbook_instance_id=?
                      AND subject_id=? AND workflow_id=? AND execution_id=? AND fence=?
                      AND publication_claim_token=?
                    """,
                    (
                        tenant_id,
                        workflow.binding.workbook_instance_id,
                        subject_id,
                        workflow_id,
                        execution_id,
                        fence,
                        publication_claim_token,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookEditConflictError("publication claim changed")
        except (WorkbookEditConflictError, WorkbookEditRepositoryError):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("publication claim release failed") from None

    def publish_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        workflow_id: str,
        expected_version: int,
        execution_id: str,
        fence: int,
        publication: Mapping[str, object],
        asset_ref: str,
        publication_claim_token: str,
        receipt_factory: Callable[[WorkbookEditWorkflow], WorkbookEditReceipt],
    ) -> WorkbookEditReceipt:
        tenant_id, subject_id = _principal_scope(principal)
        copied_publication = _copy_mapping(publication, field="publication")
        if not isinstance(asset_ref, str) or not asset_ref or len(asset_ref) > 4096:
            raise WorkbookEditRepositoryError("snapshot asset reference is invalid")
        new_snapshot_id = _publication_text(copied_publication, "snapshot_id")
        new_workbook_sha256 = _publication_text(copied_publication, "workbook_sha256")
        new_revision_id = _publication_text(copied_publication, "revision_id")
        try:
            with self._transaction() as connection:
                self._require_command_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                )
                current = self._load_workflow(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                )
                if current is None or current.version != expected_version:
                    raise WorkbookEditConflictError("workflow version changed")
                if (
                    current.state != "session_verified"
                    or current.execution_id != execution_id
                    or current.fence != fence
                    or current.publication is not None
                ):
                    raise WorkbookEditConflictError("publication basis changed")
                active = self._active_row(connection, current.binding)
                if not _active_matches(
                    active,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                    execution_id=execution_id,
                    fence=fence,
                ):
                    raise WorkbookEditConflictError("publication lease changed")
                if (
                    active["publication_claim_token"] != publication_claim_token
                    or not isinstance(active["publication_claim_expires_at"], int)
                    or active["publication_claim_expires_at"] <= self._clock_epoch()
                ):
                    raise WorkbookEditConflictError("publication claim changed")
                self._assert_snapshot_head(connection, current.binding)
                published = replace(
                    current,
                    version=current.version + 1,
                    publication=copied_publication,
                )
                copied_published = _copy_workflow(published)
                receipt = _copy_receipt(receipt_factory(copied_published))
                _assert_receipt_for_workflow(receipt, copied_published)
                workflow_json = _encode_workflow(copied_published)
                receipt_json = _encode_receipt(receipt)
                changed = connection.execute(
                    """
                    UPDATE workflows SET version=?, workflow_json=?
                    WHERE tenant_id=? AND subject_id=? AND workflow_id=? AND version=?
                    """,
                    (
                        copied_published.version,
                        workflow_json,
                        tenant_id,
                        subject_id,
                        workflow_id,
                        expected_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookEditConflictError("workflow version changed")
                connection.execute(
                    """
                    INSERT INTO snapshot_heads(
                        tenant_id, workbook_instance_id, bundle_id, snapshot_id,
                        workbook_sha256, revision_id, asset_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, workbook_instance_id) DO UPDATE SET
                        bundle_id=excluded.bundle_id,
                        snapshot_id=excluded.snapshot_id,
                        workbook_sha256=excluded.workbook_sha256,
                        revision_id=excluded.revision_id,
                        asset_ref=excluded.asset_ref
                    """,
                    (
                        tenant_id,
                        current.binding.workbook_instance_id,
                        current.binding.bundle_id,
                        new_snapshot_id,
                        new_workbook_sha256,
                        new_revision_id,
                        asset_ref,
                    ),
                )
                self._delete_active(
                    connection,
                    binding=current.binding,
                    subject_id=subject_id,
                    workflow_id=workflow_id,
                    execution_id=execution_id,
                    fence=fence,
                )
                self._complete_command(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                    receipt_json=receipt_json,
                )
                return _decode_receipt(receipt_json)
        except (
            WorkbookEditConflictError,
            WorkbookEditIdempotencyClaimError,
            WorkbookEditRepositoryError,
        ):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("snapshot publication failed") from None

    def abort_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        try:
            with self._transaction() as connection:
                self._require_command_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                )
                deleted = connection.execute(
                    """
                    DELETE FROM commands
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                      AND command_sha256=? AND command_id=?
                      AND state='pending' AND claim_token=?
                    """,
                    (
                        tenant_id,
                        subject_id,
                        idempotency_key,
                        command_sha256,
                        command_id,
                        claim_token,
                    ),
                ).rowcount
                if deleted != 1:
                    raise WorkbookEditIdempotencyClaimError("command claim mismatch")
        except (
            WorkbookEditIdempotencyClaimError,
            WorkbookEditRepositoryError,
        ):
            raise
        except sqlite3.Error:
            raise WorkbookEditRepositoryError("workbook command abort failed") from None

    def _prepare_database_file(self) -> None:
        parent = self._database.parent
        _assert_no_symlink_components(parent)
        try:
            metadata = parent.stat(follow_symlinks=False)
        except OSError:
            raise WorkbookEditRepositoryError("workbook edit database parent is unavailable") from None
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
            raise WorkbookEditRepositoryError("workbook edit database parent is invalid")
        if self._database.is_symlink():
            raise WorkbookEditRepositoryError("workbook edit database cannot be a symbolic link")
        if self._database.exists():
            try:
                metadata = self._database.stat(follow_symlinks=False)
            except OSError:
                raise WorkbookEditRepositoryError("workbook edit database is unavailable") from None
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkbookEditRepositoryError("workbook edit database must be a regular file")
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._database, flags, 0o600)
            except FileExistsError:
                if self._database.is_symlink():
                    raise WorkbookEditRepositoryError(
                        "workbook edit database cannot be a symbolic link"
                    ) from None
            except OSError:
                raise WorkbookEditRepositoryError("workbook edit database creation failed") from None
            else:
                os.close(descriptor)
        _private_file(self._database)

    def _connect(self) -> sqlite3.Connection:
        _assert_no_symlink_components(self._database)
        if self._database.is_symlink():
            raise WorkbookEditRepositoryError("workbook edit database cannot be a symbolic link")
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
            raise WorkbookEditRepositoryError("workbook edit database connection failed") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_sidecars()

    def _secure_sidecars(self) -> None:
        _private_file(self._database)
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = Path(str(self._database) + suffix)
            if candidate.is_symlink():
                raise WorkbookEditRepositoryError("workbook edit database sidecar is invalid")
            if candidate.exists():
                _private_file(candidate)

    def _load_workflow(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        workflow_id: str,
    ) -> WorkbookEditWorkflow | None:
        row = connection.execute(
            """
            SELECT version, workflow_json FROM workflows
            WHERE tenant_id=? AND subject_id=? AND workflow_id=?
            """,
            (tenant_id, subject_id, workflow_id),
        ).fetchone()
        if row is None:
            return None
        workflow = _decode_workflow(row["workflow_json"])
        _assert_stored_workflow(
            workflow,
            tenant_id=tenant_id,
            subject_id=subject_id,
            workflow_id=workflow_id,
            version=row["version"],
        )
        return workflow

    def _require_command_claim(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT command_sha256, command_id, state, claim_token, claim_expires_at
            FROM commands
            WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
            """,
            (tenant_id, subject_id, idempotency_key),
        ).fetchone()
        if (
            row is None
            or row["state"] != "pending"
            or row["command_sha256"] != command_sha256
            or row["command_id"] != command_id
            or row["claim_token"] != claim_token
            or not isinstance(row["claim_expires_at"], int)
            or row["claim_expires_at"] <= self._clock_epoch()
        ):
            raise WorkbookEditIdempotencyClaimError("command claim mismatch")

    @staticmethod
    def _complete_command(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        receipt_json: str,
    ) -> None:
        changed = connection.execute(
            """
            UPDATE commands
            SET state='completed', claim_token=NULL, receipt_json=?, claim_expires_at=NULL
            WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
              AND command_sha256=? AND command_id=?
              AND state='pending' AND claim_token=?
            """,
            (
                receipt_json,
                tenant_id,
                subject_id,
                idempotency_key,
                command_sha256,
                command_id,
                claim_token,
            ),
        ).rowcount
        if changed != 1:
            raise WorkbookEditIdempotencyClaimError("command claim mismatch")

    @staticmethod
    def _active_row(
        connection: sqlite3.Connection,
        binding: WorkbookSessionBinding,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT subject_id, workflow_id, execution_id, fence,
                   publication_claim_token, publication_claim_expires_at
            FROM active_workbooks
            WHERE tenant_id=? AND workbook_instance_id=?
            """,
            (binding.tenant_id, binding.workbook_instance_id),
        ).fetchone()

    @staticmethod
    def _delete_active(
        connection: sqlite3.Connection,
        *,
        binding: WorkbookSessionBinding,
        subject_id: str,
        workflow_id: str,
        execution_id: str | None,
        fence: int | None,
    ) -> None:
        deleted = connection.execute(
            """
            DELETE FROM active_workbooks
            WHERE tenant_id=? AND workbook_instance_id=? AND subject_id=?
              AND workflow_id=? AND execution_id=? AND fence=?
            """,
            (
                binding.tenant_id,
                binding.workbook_instance_id,
                subject_id,
                workflow_id,
                execution_id,
                fence,
            ),
        ).rowcount
        if deleted != 1:
            raise WorkbookEditConflictError("execution claim changed")

    @staticmethod
    def _assert_snapshot_head(
        connection: sqlite3.Connection,
        binding: WorkbookSessionBinding,
    ) -> None:
        row = connection.execute(
            """
            SELECT bundle_id, snapshot_id, workbook_sha256, revision_id
            FROM snapshot_heads
            WHERE tenant_id=? AND workbook_instance_id=?
            """,
            (binding.tenant_id, binding.workbook_instance_id),
        ).fetchone()
        if row is None:
            return
        if (
            row["bundle_id"] != binding.bundle_id
            or row["snapshot_id"] != binding.snapshot_id
            or row["workbook_sha256"] != binding.workbook_sha256
            or row["revision_id"] != binding.revision_id
        ):
            raise WorkbookEditConflictError("source snapshot head changed")


def _principal_scope(principal: ServicePrincipal) -> tuple[str, str]:
    if not isinstance(principal, ServicePrincipal):
        raise TypeError("principal must be a ServicePrincipal")
    return principal.scope


def _iso_epoch(value: object) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WorkbookEditRepositoryError("publication claim deadline is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise WorkbookEditRepositoryError("publication claim deadline is invalid") from None
    if parsed.tzinfo is None:
        raise WorkbookEditRepositoryError("publication claim deadline is invalid")
    return int(parsed.astimezone(timezone.utc).timestamp())


def _active_matches(
    row: sqlite3.Row | None,
    *,
    subject_id: str,
    workflow_id: str,
    execution_id: str | None,
    fence: int | None,
) -> bool:
    return bool(
        row is not None
        and row["subject_id"] == subject_id
        and row["workflow_id"] == workflow_id
        and row["execution_id"] == execution_id
        and row["fence"] == fence
    )


def _copy_mapping(value: Mapping[str, object], *, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkbookEditRepositoryError(f"{field} must be a mapping")
    try:
        serialized = _canonical_json(dict(value))
        copied = _decode_canonical_json(serialized)
    except (TypeError, ValueError, UnicodeError):
        raise WorkbookEditRepositoryError(f"{field} is not canonical JSON") from None
    if not isinstance(copied, dict):
        raise WorkbookEditRepositoryError(f"{field} must be an object")
    return copied


def _workflow_document(workflow: WorkbookEditWorkflow) -> dict[str, object]:
    if not isinstance(workflow, WorkbookEditWorkflow):
        raise TypeError("workflow must be a WorkbookEditWorkflow")
    return {
        "schema_version": _WORKFLOW_SCHEMA,
        "workflow_id": workflow.workflow_id,
        "binding": {
            "session_id": workflow.binding.session_id,
            "tenant_id": workflow.binding.tenant_id,
            "subject_id": workflow.binding.subject_id,
            "bundle_id": workflow.binding.bundle_id,
            "snapshot_id": workflow.binding.snapshot_id,
            "workbook_sha256": workflow.binding.workbook_sha256,
            "revision_id": workflow.binding.revision_id,
            "sheet": workflow.binding.sheet,
            "worksheet_id": workflow.binding.worksheet_id,
            "workbook_instance_id": workflow.binding.workbook_instance_id,
        },
        "state": workflow.state,
        "version": workflow.version,
        "proposal": copy.deepcopy(dict(workflow.proposal)),
        "preview": None if workflow.preview is None else copy.deepcopy(dict(workflow.preview)),
        "approval": None if workflow.approval is None else copy.deepcopy(dict(workflow.approval)),
        "approval_consumed": workflow.approval_consumed,
        "execution_id": workflow.execution_id,
        "fence": workflow.fence,
        "challenge": workflow.challenge,
        "execution_deadline": workflow.execution_deadline,
        "manifest": None if workflow.manifest is None else copy.deepcopy(dict(workflow.manifest)),
        "witness": None if workflow.witness is None else copy.deepcopy(dict(workflow.witness)),
        "verification": (
            None if workflow.verification is None else copy.deepcopy(dict(workflow.verification))
        ),
        "publication": (
            None if workflow.publication is None else copy.deepcopy(dict(workflow.publication))
        ),
        "publication_required": workflow.publication_required,
    }


def _encode_workflow(workflow: WorkbookEditWorkflow) -> str:
    try:
        return _canonical_json(_workflow_document(workflow))
    except (TypeError, ValueError, UnicodeError):
        raise WorkbookEditRepositoryError("workflow is not canonical JSON") from None


def _decode_workflow(value: object) -> WorkbookEditWorkflow:
    document = _decode_document(value, fields=_WORKFLOW_FIELDS, label="workflow")
    if document.get("schema_version") != _WORKFLOW_SCHEMA:
        raise WorkbookEditRepositoryError("workflow schema is invalid")
    raw_binding = document.get("binding")
    if not isinstance(raw_binding, dict) or set(raw_binding) != _BINDING_FIELDS:
        raise WorkbookEditRepositoryError("workflow binding is invalid")
    try:
        binding = WorkbookSessionBinding(**raw_binding)
    except (TypeError, ValueError):
        raise WorkbookEditRepositoryError("workflow binding is invalid") from None
    state = document.get("state")
    version = document.get("version")
    if state not in _WORKFLOW_STATES:
        raise WorkbookEditRepositoryError("workflow state is invalid")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise WorkbookEditRepositoryError("workflow version is invalid")
    proposal = _required_object(document.get("proposal"), field="proposal")
    optional_objects = {
        field: _optional_object(document.get(field), field=field)
        for field in ("preview", "approval", "manifest", "witness", "verification", "publication")
    }
    approval_consumed = document.get("approval_consumed")
    if not isinstance(approval_consumed, bool):
        raise WorkbookEditRepositoryError("workflow approval flag is invalid")
    publication_required = document.get("publication_required")
    if publication_required is not None and not isinstance(publication_required, bool):
        raise WorkbookEditRepositoryError("workflow publication policy is invalid")
    execution_id = _optional_string(document.get("execution_id"), field="execution_id")
    challenge = _optional_string(document.get("challenge"), field="challenge")
    execution_deadline = _optional_string(
        document.get("execution_deadline"), field="execution_deadline"
    )
    fence = document.get("fence")
    if fence is not None and (
        not isinstance(fence, int)
        or isinstance(fence, bool)
        or not 1 <= fence <= MAX_SAFE_INTEGER
    ):
        raise WorkbookEditRepositoryError("workflow fence is invalid")
    workflow_id = document.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise WorkbookEditRepositoryError("workflow id is invalid")
    return WorkbookEditWorkflow(
        workflow_id=workflow_id,
        binding=binding,
        state=state,
        version=version,
        proposal=proposal,
        preview=optional_objects["preview"],
        approval=optional_objects["approval"],
        approval_consumed=approval_consumed,
        execution_id=execution_id,
        fence=fence,
        challenge=challenge,
        execution_deadline=execution_deadline,
        manifest=optional_objects["manifest"],
        witness=optional_objects["witness"],
        verification=optional_objects["verification"],
        publication=optional_objects["publication"],
        publication_required=publication_required,
    )


def _copy_workflow(workflow: WorkbookEditWorkflow) -> WorkbookEditWorkflow:
    return _decode_workflow(_encode_workflow(workflow))


def _encode_receipt(receipt: WorkbookEditReceipt) -> str:
    if not isinstance(receipt, WorkbookEditReceipt):
        raise TypeError("receipt must be a WorkbookEditReceipt")
    try:
        return _canonical_json(receipt.to_dict())
    except (TypeError, ValueError, UnicodeError):
        raise WorkbookEditRepositoryError("receipt is not canonical JSON") from None


def _decode_receipt(value: object) -> WorkbookEditReceipt:
    document = _decode_document(value, fields=_RECEIPT_FIELDS, label="receipt")
    if document.get("schema_version") != "audit_workbook_edit_receipt.v1":
        raise WorkbookEditRepositoryError("receipt schema is invalid")
    details = _required_object(document.get("details"), field="receipt details")
    try:
        return WorkbookEditReceipt(
            command_id=document["command_id"],
            workflow_id=document["workflow_id"],
            session_id=document["session_id"],
            bundle_id=document["bundle_id"],
            snapshot_id=document["snapshot_id"],
            workbook_sha256=document["workbook_sha256"],
            revision_id=document["revision_id"],
            sheet=document["sheet"],
            state=document["state"],
            details=details,
        )
    except (TypeError, ValueError, KeyError):
        raise WorkbookEditRepositoryError("receipt is invalid") from None


def _copy_receipt(receipt: WorkbookEditReceipt) -> WorkbookEditReceipt:
    return _decode_receipt(_encode_receipt(receipt))


def _decode_document(value: object, *, fields: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise WorkbookEditRepositoryError(f"stored {label} is invalid")
    try:
        document = _decode_canonical_json(value)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WorkbookEditRepositoryError(f"stored {label} is invalid") from None
    if not isinstance(document, dict) or set(document) != fields:
        raise WorkbookEditRepositoryError(f"stored {label} fields are invalid")
    if _canonical_json(document) != value:
        raise WorkbookEditRepositoryError(f"stored {label} is not canonical")
    return document


def _decode_canonical_json(value: str) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON number")),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _required_object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkbookEditRepositoryError(f"{field} is invalid")
    return copy.deepcopy(value)


def _optional_object(value: object, *, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    return _required_object(value, field=field)


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkbookEditRepositoryError(f"{field} is invalid")
    return value


def _assert_workflow_principal(
    workflow: WorkbookEditWorkflow,
    tenant_id: str,
    subject_id: str,
) -> None:
    if workflow.binding.principal_scope != (tenant_id, subject_id):
        raise WorkbookEditConflictError("workflow principal changed")


def _assert_stored_workflow(
    workflow: WorkbookEditWorkflow,
    *,
    tenant_id: str,
    subject_id: str,
    workflow_id: str,
    version: object,
) -> None:
    _assert_workflow_principal(workflow, tenant_id, subject_id)
    if workflow.workflow_id != workflow_id or workflow.version != version:
        raise WorkbookEditRepositoryError("stored workflow index is invalid")


def _assert_receipt_for_workflow(
    receipt: WorkbookEditReceipt,
    workflow: WorkbookEditWorkflow,
) -> None:
    binding = workflow.binding
    if (
        receipt.workflow_id != workflow.workflow_id
        or receipt.session_id != binding.session_id
        or receipt.bundle_id != binding.bundle_id
        or receipt.snapshot_id != binding.snapshot_id
        or receipt.workbook_sha256 != binding.workbook_sha256
        or receipt.revision_id != binding.revision_id
        or receipt.sheet != binding.sheet
        or receipt.state != workflow.state
    ):
        raise WorkbookEditRepositoryError("receipt does not match workflow")


def _publication_text(publication: Mapping[str, object], field: str) -> str:
    value = publication.get(field)
    if not isinstance(value, str) or not value:
        raise WorkbookEditRepositoryError("snapshot publication is invalid")
    return value


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise WorkbookEditRepositoryError("workbook edit database path is unavailable") from None
        except OSError:
            raise WorkbookEditRepositoryError("workbook edit database path is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkbookEditRepositoryError("workbook edit database path contains a symlink")


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600, follow_symlinks=False)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise WorkbookEditRepositoryError("workbook edit database permissions are unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise WorkbookEditRepositoryError("workbook edit database is not private")


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS workflows(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1),
        workflow_json TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, workflow_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS commands(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        command_sha256 TEXT NOT NULL,
        command_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending', 'completed')),
        claim_token TEXT,
        receipt_json TEXT,
        claim_expires_at INTEGER,
        PRIMARY KEY(tenant_id, subject_id, idempotency_key),
        CHECK(
            (state='pending' AND claim_token IS NOT NULL AND receipt_json IS NULL
             AND claim_expires_at IS NOT NULL)
            OR (state='completed' AND claim_token IS NULL AND receipt_json IS NOT NULL
                AND claim_expires_at IS NULL)
        )
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS active_workbooks(
        tenant_id TEXT NOT NULL,
        workbook_instance_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        fence INTEGER NOT NULL CHECK(fence >= 1),
        publication_claim_token TEXT,
        publication_claim_expires_at INTEGER,
        CHECK(
            (publication_claim_token IS NULL AND publication_claim_expires_at IS NULL)
            OR (publication_claim_token IS NOT NULL AND publication_claim_expires_at IS NOT NULL)
        ),
        PRIMARY KEY(tenant_id, workbook_instance_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS workbook_fences(
        tenant_id TEXT NOT NULL,
        workbook_instance_id TEXT NOT NULL,
        fence INTEGER NOT NULL CHECK(fence >= 1),
        PRIMARY KEY(tenant_id, workbook_instance_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_heads(
        tenant_id TEXT NOT NULL,
        workbook_instance_id TEXT NOT NULL,
        bundle_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        workbook_sha256 TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        asset_ref TEXT,
        PRIMARY KEY(tenant_id, workbook_instance_id)
    ) WITHOUT ROWID
    """,
)


__all__ = ["SQLiteWorkbookEditRepository"]
