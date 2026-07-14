"""Authoritative service boundary for approved Office.js workbook edits.

This state machine is deliberately independent from audit-chat conversations, graph
checkpoints, and prepared artifacts.  It resolves a server-registered Office session, creates
content-addressed edit documents, consumes one exact approval once, and fences one executor at
a time for that session.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Mapping, Protocol, Sequence

from .service import ServicePrincipal
from .workbook_snapshot_publication import (
    ImmutableWorkbookAssetStore,
    SavedWorkbookReacquirer,
    SnapshotPublicationBasis,
    WorkbookSnapshotPublicationError,
    build_snapshot_publication,
    reacquire_saved_workbook,
    store_acquired_workbook,
    validate_saved_workbook_manifest,
)
from .workbook_edit import (
    MAX_SAFE_INTEGER,
    WorkbookEditError,
    create_apply_manifest,
    create_edit_approval,
    create_edit_preview,
    create_edit_proposal,
    create_execution_witness,
    verify_execution_witness,
)


_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MANIFEST_REF_RE = re.compile(r"\Aedit-manifest:[0-9a-f]{64}\Z")
_MAX_IDEMPOTENCY_KEY_LENGTH = 200
_VERIFICATION_STATES = frozenset(
    {"session_verified", "verification_failed", "indeterminate", "stale_precondition"}
)
_TERMINAL_STATES = frozenset(
    {*_VERIFICATION_STATES, "rejected", "aborted_before_apply"}
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "path",
        "package_path",
        "runtime_root",
        "source_path",
        "file_path",
        "provider",
        "api_key",
        "authorization",
        "secret",
        "token",
    }
)


class WorkbookEditServiceError(RuntimeError):
    """A stable error that is safe to return through a public adapter."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class WorkbookSessionNotFoundError(LookupError):
    """No exact server-registered session is visible to this principal."""


class WorkbookEditRepositoryError(RuntimeError):
    """The private edit repository could not safely complete a transition."""


class WorkbookEditConflictError(WorkbookEditRepositoryError):
    """A compare-and-swap or active-execution invariant failed."""


class WorkbookEditPublicationClaimError(WorkbookEditConflictError):
    """Another claimant currently owns the bounded publication attempt."""


class WorkbookEditIdempotencyConflictError(WorkbookEditRepositoryError):
    """An idempotency key belongs to another command digest."""


class WorkbookEditIdempotencyClaimError(WorkbookEditRepositoryError):
    """A command claim is missing or belongs to another claimant."""


def _opaque(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an opaque identifier")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 identifier")
    return value


def _sheet(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 31
        or any(character in value for character in "[]:*?/\\")
    ):
        raise ValueError("sheet must be an exact Excel sheet name")
    return value


@dataclass(frozen=True)
class WorkbookSessionBinding:
    """Exact host-registered workbook and worksheet identity for one Office session."""

    session_id: str
    tenant_id: str
    subject_id: str
    bundle_id: str
    snapshot_id: str
    workbook_sha256: str
    revision_id: str
    sheet: str
    worksheet_id: str
    workbook_instance_id: str

    def __post_init__(self) -> None:
        _opaque(self.session_id, field="session_id")
        _opaque(self.tenant_id, field="tenant_id")
        _opaque(self.subject_id, field="subject_id")
        _opaque(self.bundle_id, field="bundle_id")
        _sha256(self.snapshot_id, field="snapshot_id")
        _sha256(self.workbook_sha256, field="workbook_sha256")
        _opaque(self.revision_id, field="revision_id")
        _sheet(self.sheet)
        _opaque(self.worksheet_id, field="worksheet_id")
        _opaque(self.workbook_instance_id, field="workbook_instance_id")

    @property
    def principal_scope(self) -> tuple[str, str]:
        return (self.tenant_id, self.subject_id)


class WorkbookSessionRepository(Protocol):
    def resolve(
        self,
        *,
        principal: ServicePrincipal,
        session_id: str,
    ) -> WorkbookSessionBinding:
        """Resolve one exact server-owned binding or raise WorkbookSessionNotFoundError."""


class InMemoryWorkbookSessionRepository:
    """Thread-safe session registry for tests and single-process hosts."""

    def __init__(self, bindings: Sequence[WorkbookSessionBinding] = ()) -> None:
        self._lock = threading.RLock()
        self._bindings: dict[
            tuple[tuple[str, str], str], WorkbookSessionBinding
        ] = {}
        for binding in bindings:
            self.register(binding=binding)

    def register(self, *, binding: WorkbookSessionBinding) -> None:
        if not isinstance(binding, WorkbookSessionBinding):
            raise TypeError("binding must be a WorkbookSessionBinding")
        key = (binding.principal_scope, binding.session_id)
        with self._lock:
            self._bindings[key] = binding

    def unregister(
        self,
        *,
        principal: ServicePrincipal,
        session_id: str,
    ) -> None:
        with self._lock:
            self._bindings.pop((principal.scope, session_id), None)

    def resolve(
        self,
        *,
        principal: ServicePrincipal,
        session_id: str,
    ) -> WorkbookSessionBinding:
        with self._lock:
            found = self._bindings.get((principal.scope, session_id))
        if found is None:
            raise WorkbookSessionNotFoundError(session_id)
        return found


WorkflowState = Literal[
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
]


@dataclass(frozen=True)
class WorkbookEditWorkflow:
    """Private authoritative state. Raw edit documents never enter graph checkpoints."""

    workflow_id: str
    binding: WorkbookSessionBinding
    state: WorkflowState
    version: int
    proposal: Mapping[str, object]
    preview: Mapping[str, object] | None = None
    approval: Mapping[str, object] | None = None
    approval_consumed: bool = False
    execution_id: str | None = None
    fence: int | None = None
    challenge: str | None = None
    execution_deadline: str | None = None
    manifest: Mapping[str, object] | None = None
    witness: Mapping[str, object] | None = None
    verification: Mapping[str, object] | None = None
    publication: Mapping[str, object] | None = None
    publication_required: bool | None = None


@dataclass(frozen=True)
class WorkbookEditReceipt:
    command_id: str
    workflow_id: str
    session_id: str
    bundle_id: str
    snapshot_id: str
    workbook_sha256: str
    revision_id: str
    sheet: str
    state: WorkflowState
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        _opaque(self.command_id, field="command_id")
        _opaque(self.workflow_id, field="workflow_id")
        _opaque(self.session_id, field="session_id")
        _opaque(self.bundle_id, field="bundle_id")
        _sha256(self.snapshot_id, field="snapshot_id")
        _sha256(self.workbook_sha256, field="workbook_sha256")
        _opaque(self.revision_id, field="revision_id")
        _sheet(self.sheet)
        if self.state not in {
            "proposed", "previewed", "approved", "rejected", "claimed",
            "apply_started", "session_verified", "verification_failed", "indeterminate",
            "stale_precondition", "aborted_before_apply",
        }:
            raise ValueError("state is invalid")
        if not isinstance(self.details, Mapping):
            raise ValueError("details must be a mapping")
        _assert_public_value(self.details)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "audit_workbook_edit_receipt.v1",
            "command_id": self.command_id,
            "workflow_id": self.workflow_id,
            "session_id": self.session_id,
            "bundle_id": self.bundle_id,
            "snapshot_id": self.snapshot_id,
            "workbook_sha256": self.workbook_sha256,
            "revision_id": self.revision_id,
            "sheet": self.sheet,
            "state": self.state,
            "details": copy.deepcopy(dict(self.details)),
        }


@dataclass(frozen=True)
class WorkbookEditSubmission:
    receipt: WorkbookEditReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkbookEditCommandClaim:
    state: Literal["claimed", "pending", "completed"]
    claim_token: str | None = None
    receipt: WorkbookEditReceipt | None = None


class WorkbookEditRepository(Protocol):
    """Private persistence boundary; implementations must provide atomic CAS semantics."""

    def claim_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> WorkbookEditCommandClaim: ...

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
    ) -> None: ...

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
    ) -> WorkbookEditReceipt: ...

    def claim_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        publication_claim_token: str,
        claim_expires_at: str,
    ) -> None: ...

    def release_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        publication_claim_token: str,
    ) -> None: ...

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
    ) -> WorkbookEditReceipt: ...

    def abort_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> None: ...

    def get_workflow(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookEditWorkflow | None: ...

    def assert_snapshot_head(self, *, binding: WorkbookSessionBinding) -> None:
        """Raise WorkbookEditConflictError when the registered raw source head moved."""


@dataclass
class _IdempotencyEntry:
    command_sha256: str
    command_id: str
    state: Literal["pending", "completed"]
    claim_token: str | None
    receipt: WorkbookEditReceipt | None


@dataclass(frozen=True)
class _WorkbookSnapshotHead:
    bundle_id: str
    snapshot_id: str
    workbook_sha256: str
    revision_id: str
    asset_ref: str | None


class InMemoryWorkbookEditRepository:
    """Thread-safe reference repository with CAS, one-active-session, and fencing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workflows: dict[tuple[tuple[str, str], str], WorkbookEditWorkflow] = {}
        self._idempotency: dict[tuple[tuple[str, str], str], _IdempotencyEntry] = {}
        self._active: dict[tuple[str, ...], tuple[str, str, int]] = {}
        self._fences: dict[tuple[str, ...], int] = {}
        self._snapshot_heads: dict[tuple[str, ...], _WorkbookSnapshotHead] = {}
        self._publication_claims: dict[
            tuple[str, ...], tuple[str, str, int, str, str]
        ] = {}

    @staticmethod
    def _copy_workflow(value: WorkbookEditWorkflow) -> WorkbookEditWorkflow:
        return copy.deepcopy(value)

    @staticmethod
    def _copy_receipt(value: WorkbookEditReceipt) -> WorkbookEditReceipt:
        return copy.deepcopy(value)

    def claim_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> WorkbookEditCommandClaim:
        with self._lock:
            key = (principal.scope, idempotency_key)
            found = self._idempotency.get(key)
            if found is None:
                self._idempotency[key] = _IdempotencyEntry(
                    command_sha256, command_id, "pending", claim_token, None
                )
                return WorkbookEditCommandClaim("claimed", claim_token=claim_token)
            if found.command_sha256 != command_sha256:
                raise WorkbookEditIdempotencyConflictError("idempotency conflict")
            if found.state == "completed":
                if found.receipt is None:
                    raise WorkbookEditRepositoryError("completed command has no receipt")
                return WorkbookEditCommandClaim(
                    "completed", receipt=self._copy_receipt(found.receipt)
                )
            return WorkbookEditCommandClaim("pending")

    def get_workflow(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookEditWorkflow | None:
        with self._lock:
            found = self._workflows.get((principal.scope, workflow_id))
            return None if found is None else self._copy_workflow(found)

    def assert_snapshot_head(self, *, binding: WorkbookSessionBinding) -> None:
        with self._lock:
            head = self._snapshot_heads.get(_live_workbook_key(binding))
        if head is None:
            return
        if (
            head.bundle_id != binding.bundle_id
            or head.snapshot_id != binding.snapshot_id
            or head.workbook_sha256 != binding.workbook_sha256
            or head.revision_id != binding.revision_id
        ):
            raise WorkbookEditConflictError("source snapshot head changed")

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
        with self._lock:
            idem_key = (principal.scope, idempotency_key)
            entry = self._idempotency.get(idem_key)
            if (
                entry is None
                or entry.state != "pending"
                or entry.command_sha256 != command_sha256
                or entry.command_id != command_id
                or entry.claim_token != claim_token
            ):
                raise WorkbookEditIdempotencyClaimError("command claim mismatch")
            workflow_key = (principal.scope, workflow.workflow_id)
            current = self._workflows.get(workflow_key)
            if expected_version is None:
                if current is not None:
                    raise WorkbookEditConflictError("workflow already exists")
            elif current is None or current.version != expected_version:
                raise WorkbookEditConflictError("workflow version changed")
            active_key = _live_workbook_key(workflow.binding)
            active = self._active.get(active_key)
            if release_active or require_active:
                if active != (workflow.workflow_id, workflow.execution_id, workflow.fence):
                    raise WorkbookEditConflictError("execution claim changed")
            copied_workflow = self._copy_workflow(workflow)
            copied_receipt = self._copy_receipt(receipt)
            completed_entry = _IdempotencyEntry(
                command_sha256, command_id, "completed", None, copied_receipt
            )
            if release_active:
                del self._active[active_key]
            self._workflows[workflow_key] = copied_workflow
            self._idempotency[idem_key] = completed_entry

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
        if not isinstance(publication_required, bool):
            raise TypeError("publication_required must be a boolean")
        with self._lock:
            idem = self._idempotency.get((principal.scope, idempotency_key))
            if (
                idem is None
                or idem.state != "pending"
                or idem.command_sha256 != command_sha256
                or idem.command_id != command_id
                or idem.claim_token != claim_token
            ):
                raise WorkbookEditIdempotencyClaimError("command claim mismatch")
            workflow_key = (principal.scope, workflow_id)
            current = self._workflows.get(workflow_key)
            if current is None or current.version != expected_version:
                raise WorkbookEditConflictError("workflow version changed")
            self.assert_snapshot_head(binding=current.binding)
            active_key = _live_workbook_key(current.binding)
            if active_key in self._active:
                raise WorkbookEditConflictError("session already has an active execution")
            fence = self._fences.get(active_key, 0) + 1
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
            receipt = receipt_factory(claimed)
            copied_claimed = self._copy_workflow(claimed)
            copied_receipt = self._copy_receipt(receipt)
            return_receipt = self._copy_receipt(copied_receipt)
            completed_entry = _IdempotencyEntry(
                command_sha256,
                command_id,
                "completed",
                None,
                copied_receipt,
            )
            self._workflows[workflow_key] = copied_claimed
            self._active[active_key] = (workflow_id, execution_id, fence)
            self._fences[active_key] = fence
            self._idempotency[(principal.scope, idempotency_key)] = completed_entry
            return return_receipt

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
        """Atomically advance the raw-workbook head and release the publication lease.

        Immutable asset storage happens before this transaction.  A failed CAS can therefore
        leave only an unreferenced content-addressed object; it can never expose a losing head.
        """

        with self._lock:
            idem_key = (principal.scope, idempotency_key)
            idem = self._idempotency.get(idem_key)
            if (
                idem is None
                or idem.state != "pending"
                or idem.command_sha256 != command_sha256
                or idem.command_id != command_id
                or idem.claim_token != claim_token
            ):
                raise WorkbookEditIdempotencyClaimError("command claim mismatch")
            workflow_key = (principal.scope, workflow_id)
            current = self._workflows.get(workflow_key)
            if current is None or current.version != expected_version:
                raise WorkbookEditConflictError("workflow version changed")
            if (
                current.state != "session_verified"
                or current.execution_id != execution_id
                or current.fence != fence
                or current.publication is not None
            ):
                raise WorkbookEditConflictError("publication basis changed")
            active_key = _live_workbook_key(current.binding)
            if self._active.get(active_key) != (workflow_id, execution_id, fence):
                raise WorkbookEditConflictError("publication lease changed")
            publication_claim = self._publication_claims.get(active_key)
            if publication_claim is None or publication_claim[:4] != (
                workflow_id,
                execution_id,
                fence,
                publication_claim_token,
            ):
                raise WorkbookEditConflictError("publication claim changed")

            base = current.binding
            head = self._snapshot_heads.get(active_key)
            if head is None:
                head = _WorkbookSnapshotHead(
                    bundle_id=base.bundle_id,
                    snapshot_id=base.snapshot_id,
                    workbook_sha256=base.workbook_sha256,
                    revision_id=base.revision_id,
                    asset_ref=None,
                )
            if (
                head.bundle_id != base.bundle_id
                or head.snapshot_id != base.snapshot_id
                or head.workbook_sha256 != base.workbook_sha256
                or head.revision_id != base.revision_id
            ):
                raise WorkbookEditConflictError("source snapshot head changed")
            try:
                copied_publication = copy.deepcopy(dict(publication))
                new_head = _WorkbookSnapshotHead(
                    bundle_id=base.bundle_id,
                    snapshot_id=str(copied_publication["snapshot_id"]),
                    workbook_sha256=str(copied_publication["workbook_sha256"]),
                    revision_id=str(copied_publication["revision_id"]),
                    asset_ref=asset_ref,
                )
                published = replace(
                    current,
                    version=current.version + 1,
                    publication=copied_publication,
                )
                receipt = receipt_factory(published)
                copied_workflow = self._copy_workflow(published)
                copied_receipt = self._copy_receipt(receipt)
            except Exception:
                raise

            self._workflows[workflow_key] = copied_workflow
            self._snapshot_heads[active_key] = new_head
            del self._publication_claims[active_key]
            del self._active[active_key]
            self._idempotency[idem_key] = _IdempotencyEntry(
                command_sha256,
                command_id,
                "completed",
                None,
                copied_receipt,
            )
            return self._copy_receipt(copied_receipt)

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
        _parse_iso(claim_expires_at)
        with self._lock:
            workflow = self._workflows.get((principal.scope, workflow_id))
            if (
                workflow is None
                or workflow.state != "session_verified"
                or workflow.execution_id != execution_id
                or workflow.fence != fence
                or workflow.publication is not None
            ):
                raise WorkbookEditConflictError("publication basis changed")
            active_key = _live_workbook_key(workflow.binding)
            if self._active.get(active_key) != (workflow_id, execution_id, fence):
                raise WorkbookEditConflictError("publication lease changed")
            current = self._publication_claims.get(active_key)
            if current is not None:
                if current[:4] == (
                    workflow_id,
                    execution_id,
                    fence,
                    publication_claim_token,
                ):
                    return
                raise WorkbookEditPublicationClaimError("publication already in progress")
            self._publication_claims[active_key] = (
                workflow_id,
                execution_id,
                fence,
                publication_claim_token,
                claim_expires_at,
            )

    def release_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        publication_claim_token: str,
    ) -> None:
        with self._lock:
            workflow = self._workflows.get((principal.scope, workflow_id))
            if workflow is None:
                raise WorkbookEditConflictError("publication basis changed")
            active_key = _live_workbook_key(workflow.binding)
            claim = self._publication_claims.get(active_key)
            if claim is None or claim[:4] != (
                workflow_id,
                execution_id,
                fence,
                publication_claim_token,
            ):
                raise WorkbookEditConflictError("publication claim changed")
            del self._publication_claims[active_key]

    def abort_command(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> None:
        with self._lock:
            key = (principal.scope, idempotency_key)
            found = self._idempotency.get(key)
            if (
                found is None
                or found.state != "pending"
                or found.command_sha256 != command_sha256
                or found.command_id != command_id
                or found.claim_token != claim_token
            ):
                raise WorkbookEditIdempotencyClaimError("command claim mismatch")
            del self._idempotency[key]


class WorkbookEditService:
    """Authoritative edit workflow independent of conversation/graph persistence."""

    def __init__(
        self,
        *,
        sessions: WorkbookSessionRepository,
        edits: WorkbookEditRepository,
        approval_ttl: timedelta = timedelta(minutes=5),
        execution_ttl: timedelta = timedelta(minutes=2),
        publication_claim_ttl: timedelta = timedelta(minutes=5),
        saved_workbooks: SavedWorkbookReacquirer | None = None,
        workbook_assets: ImmutableWorkbookAssetStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(approval_ttl, timedelta) or not (
            timedelta(seconds=1) <= approval_ttl <= timedelta(hours=1)
        ):
            raise ValueError("approval_ttl must be between one second and one hour")
        if not isinstance(execution_ttl, timedelta) or not (
            timedelta(seconds=1) <= execution_ttl <= timedelta(minutes=10)
        ):
            raise ValueError("execution_ttl must be between one second and ten minutes")
        if not isinstance(publication_claim_ttl, timedelta) or not (
            timedelta(seconds=1) <= publication_claim_ttl <= timedelta(minutes=15)
        ):
            raise ValueError(
                "publication_claim_ttl must be between one second and fifteen minutes"
            )
        if (saved_workbooks is None) != (workbook_assets is None):
            raise ValueError(
                "saved_workbooks and workbook_assets must be configured together"
            )
        self._sessions = sessions
        self._edits = edits
        self._approval_ttl = approval_ttl
        self._execution_ttl = execution_ttl
        self._publication_claim_ttl = publication_claim_ttl
        self._saved_workbooks = saved_workbooks
        self._workbook_assets = workbook_assets
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def snapshot_publication_enabled(self) -> bool:
        return self._saved_workbooks is not None and self._workbook_assets is not None

    def _clock(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WorkbookEditServiceError(
                "SERVICE_UNAVAILABLE",
                "The workbook edit service clock is unavailable.",
                status_code=503,
            )
        return value.astimezone(timezone.utc)

    def _binding(
        self,
        *,
        principal: ServicePrincipal,
        session_id: str,
    ) -> WorkbookSessionBinding:
        _require_principal(principal)
        try:
            clean_session_id = _opaque(session_id, field="session_id")
            binding = self._sessions.resolve(
                principal=principal,
                session_id=clean_session_id,
            )
        except (TypeError, ValueError):
            raise _public_error("INVALID_REQUEST") from None
        except WorkbookSessionNotFoundError:
            raise _public_error("SESSION_NOT_FOUND") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if not isinstance(binding, WorkbookSessionBinding):
            raise _public_error("SERVICE_UNAVAILABLE")
        if binding.principal_scope != principal.scope or binding.session_id != clean_session_id:
            raise _public_error("PRINCIPAL_MISMATCH")
        return binding

    def _current(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookEditWorkflow:
        workflow = self._stored(
            principal=principal,
            workflow_id=workflow_id,
        )
        self._assert_binding_current(principal=principal, workflow=workflow)
        return workflow

    def _stored(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookEditWorkflow:
        """Load principal-scoped history without requiring a still-live Office session."""

        _require_principal(principal)
        try:
            clean_workflow_id = _opaque(workflow_id, field="workflow_id")
            workflow = self._edits.get_workflow(
                principal=principal,
                workflow_id=clean_workflow_id,
            )
        except (TypeError, ValueError):
            raise _public_error("INVALID_REQUEST") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if workflow is None:
            raise _public_error("WORKFLOW_NOT_FOUND")
        if not isinstance(workflow, WorkbookEditWorkflow):
            raise _public_error("SERVICE_UNAVAILABLE")
        return workflow

    def _assert_binding_current(
        self,
        *,
        principal: ServicePrincipal,
        workflow: WorkbookEditWorkflow,
    ) -> None:
        current = self._binding(
            principal=principal,
            session_id=workflow.binding.session_id,
        )
        expected = workflow.binding
        if current.principal_scope != expected.principal_scope:
            raise _public_error("PRINCIPAL_MISMATCH")
        if current.bundle_id != expected.bundle_id or current.snapshot_id != expected.snapshot_id:
            raise _public_error("BUNDLE_MISMATCH")
        if current.workbook_sha256 != expected.workbook_sha256:
            raise _public_error("WORKBOOK_MISMATCH")
        if current.workbook_instance_id != expected.workbook_instance_id:
            raise _public_error("WORKBOOK_MISMATCH")
        if current.revision_id != expected.revision_id:
            raise _public_error("STALE_REVISION")
        if current.sheet != expected.sheet or current.worksheet_id != expected.worksheet_id:
            raise _public_error("WORKSHEET_MISMATCH")
        try:
            self._edits.assert_snapshot_head(binding=expected)
        except WorkbookEditConflictError:
            raise _public_error("STALE_REVISION") from None
        except WorkbookEditRepositoryError:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def _begin(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command: Mapping[str, object],
    ) -> tuple[
        str,
        str,
        str,
        str,
        WorkbookEditSubmission | None,
    ]:
        _require_principal(principal)
        key = _idempotency_key(idempotency_key)
        digest = _canonical_digest(command)
        command_id = _derived_id("edit-command", principal, key)
        claim_token = secrets.token_hex(24)
        try:
            claim = self._edits.claim_command(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
        except WorkbookEditIdempotencyConflictError:
            raise _public_error("IDEMPOTENCY_CONFLICT") from None
        except WorkbookEditRepositoryError:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if claim.state == "completed":
            if claim.receipt is None:
                raise _public_error("SERVICE_UNAVAILABLE")
            return key, digest, command_id, "", WorkbookEditSubmission(
                receipt=claim.receipt,
                replayed=True,
            )
        if claim.state == "pending":
            raise _public_error("COMMAND_IN_PROGRESS")
        if claim.state != "claimed" or claim.claim_token != claim_token:
            raise _public_error("SERVICE_UNAVAILABLE")
        return key, digest, command_id, claim_token, None

    def _abort_pending(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
    ) -> None:
        try:
            self._edits.abort_command(
                principal=principal,
                idempotency_key=idempotency_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim_token,
            )
        except Exception:
            # The original fixed error is safer than leaking repository state.  A surviving
            # pending claim intentionally makes a later retry report COMMAND_IN_PROGRESS.
            pass

    def _release_publication_claim(
        self,
        *,
        principal: ServicePrincipal,
        claim: tuple[str, str, int, str] | None,
    ) -> None:
        if claim is None:
            return
        workflow_id, execution_id, fence, publication_claim_token = claim
        try:
            self._edits.release_snapshot_publication(
                principal=principal,
                workflow_id=workflow_id,
                execution_id=execution_id,
                fence=fence,
                publication_claim_token=publication_claim_token,
            )
        except Exception:
            # A surviving claim remains a fail-closed publication quarantine. Durable
            # repositories may reclaim only after their bounded claim lease expires.
            pass

    def _publish(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        workflow: WorkbookEditWorkflow,
        expected_version: int | None,
        details: Mapping[str, object],
        release_active: bool = False,
        require_current_binding: bool = True,
        require_active: bool = False,
    ) -> WorkbookEditSubmission:
        if require_current_binding:
            self._assert_binding_current(principal=principal, workflow=workflow)
        receipt = _receipt(command_id=command_id, workflow=workflow, details=details)
        try:
            self._edits.publish_transition(
                principal=principal,
                idempotency_key=idempotency_key,
                command_sha256=command_sha256,
                command_id=command_id,
                claim_token=claim_token,
                workflow=workflow,
                expected_version=expected_version,
                receipt=receipt,
                release_active=release_active,
                require_active=require_active,
            )
        except WorkbookEditConflictError:
            raise _public_error("EDIT_CONFLICT") from None
        except WorkbookEditRepositoryError:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        return WorkbookEditSubmission(receipt=receipt, replayed=False)

    def propose(
        self,
        *,
        principal: ServicePrincipal,
        session_id: str,
        proposal_input: Mapping[str, object],
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        _require_principal(principal)
        try:
            clean_session_id = _opaque(session_id, field="session_id")
        except (TypeError, ValueError):
            raise _public_error("INVALID_REQUEST") from None
        document = _strict_mapping(proposal_input, fields={"changes"})
        command = {
            "operation": "propose",
            "session_id": clean_session_id,
            "proposal_input": document,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal,
            idempotency_key=idempotency_key,
            command=command,
        )
        if replay is not None:
            return replay
        try:
            binding = self._binding(
                principal=principal,
                session_id=clean_session_id,
            )
            proposal = create_edit_proposal(
                bundle_id=binding.bundle_id,
                snapshot_id=binding.snapshot_id,
                workbook_sha256=binding.workbook_sha256,
                sheet=binding.sheet,
                changes=document["changes"],
            )
            workflow_id = _derived_id("edit-workflow", principal, key)
            workflow = WorkbookEditWorkflow(
                workflow_id=workflow_id,
                binding=binding,
                state="proposed",
                version=1,
                proposal=proposal,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=workflow,
                expected_version=None,
                details={
                    "proposal_ref": proposal["proposal_ref"],
                    "proposal_sha256": proposal["proposal_sha256"],
                },
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise
        except WorkbookEditError as error:
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _contract_error(error) from None
        except Exception:
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def preview(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        preview_input: Mapping[str, object],
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        document = _strict_mapping(
            preview_input,
            fields={"office_revision_id", "worksheet_id", "before"},
        )
        command = {
            "operation": "preview",
            "workflow_id": clean_workflow_id,
            "preview_input": document,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal,
            idempotency_key=idempotency_key,
            command=command,
        )
        if replay is not None:
            return replay
        try:
            workflow = self._current(
                principal=principal,
                workflow_id=clean_workflow_id,
            )
            if workflow.state != "proposed":
                raise _public_error("INVALID_STATE")
            if document["office_revision_id"] != workflow.binding.revision_id:
                raise _public_error("STALE_REVISION")
            if document["worksheet_id"] != workflow.binding.worksheet_id:
                raise _public_error("WORKSHEET_MISMATCH")
            preview = create_edit_preview(
                workflow.proposal,
                office_session_id=workflow.binding.session_id,
                office_revision_id=workflow.binding.revision_id,
                worksheet_id=workflow.binding.worksheet_id,
                before=document["before"],
            )
            updated = replace(
                workflow,
                state="previewed",
                version=workflow.version + 1,
                preview=preview,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=updated,
                expected_version=workflow.version,
                details={"preview": preview},
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise
        except WorkbookEditError as error:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _contract_error(error) from None
        except Exception:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def approve(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        preview_id: str,
        preview_sha256: str,
        confirmed: bool,
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_preview_id = _public_opaque(preview_id)
        clean_preview_sha256 = _public_sha256(preview_sha256)
        if not isinstance(confirmed, bool):
            raise _public_error("INVALID_REQUEST")
        command = {
            "operation": "approve",
            "workflow_id": clean_workflow_id,
            "preview_id": clean_preview_id,
            "preview_sha256": clean_preview_sha256,
            "confirmed": confirmed,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal, idempotency_key=idempotency_key, command=command
        )
        if replay is not None:
            return replay
        try:
            workflow = self._current(principal=principal, workflow_id=clean_workflow_id)
            if workflow.state != "previewed" or workflow.preview is None:
                raise _public_error("INVALID_STATE")
            if confirmed is not True:
                raise _public_error("APPROVAL_CONFIRMATION_REQUIRED")
            if (
                clean_preview_id != workflow.preview["preview_ref"]
                or clean_preview_sha256 != workflow.preview["preview_sha256"]
            ):
                raise _public_error("PREVIEW_MISMATCH")
            expires_at = _iso_utc(self._clock() + self._approval_ttl)
            approval = create_edit_approval(
                workflow.preview,
                approver_id=principal.subject_id,
                expires_at=expires_at,
            )
            updated = replace(
                workflow,
                state="approved",
                version=workflow.version + 1,
                approval=approval,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=updated,
                expected_version=workflow.version,
                details={
                    "approval_ref": approval["approval_ref"],
                    "approval_sha256": approval["approval_sha256"],
                    "expires_at": approval["expires_at"],
                },
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise
        except WorkbookEditError as error:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _contract_error(error) from None
        except Exception:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def reject(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        preview_id: str,
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_preview_id = _public_opaque(preview_id)
        command = {
            "operation": "reject",
            "workflow_id": clean_workflow_id,
            "preview_id": clean_preview_id,
        }
        return self._simple_transition(
            principal=principal,
            workflow_id=clean_workflow_id,
            idempotency_key=idempotency_key,
            command=command,
            expected_state="previewed",
            new_state="rejected",
            validator=lambda current: _require_preview(current, clean_preview_id),
            details={"preview_ref": clean_preview_id, "reason": "human_rejected"},
        )

    def claim_execution(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        session_id: str,
        idempotency_key: str,
        publication_required: bool | None = None,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_session_id = _public_opaque(session_id)
        if publication_required is None:
            require_publication = self.snapshot_publication_enabled
        elif isinstance(publication_required, bool):
            require_publication = publication_required
        else:
            raise _public_error("INVALID_REQUEST")
        if require_publication and not self.snapshot_publication_enabled:
            raise _public_error("PUBLICATION_UNAVAILABLE")
        command = {
            "operation": "claim_execution",
            "workflow_id": clean_workflow_id,
            "session_id": clean_session_id,
            "publication_required": require_publication,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal, idempotency_key=idempotency_key, command=command
        )
        if replay is not None:
            return replay
        try:
            workflow = self._current(principal=principal, workflow_id=clean_workflow_id)
            if clean_session_id != workflow.binding.session_id:
                raise _public_error("SESSION_MISMATCH")
            if workflow.state != "approved" or workflow.preview is None or workflow.approval is None:
                if workflow.state == "claimed" or workflow.approval_consumed:
                    raise _public_error("APPROVAL_REPLAY")
                if workflow.state == "apply_started" or workflow.state in _TERMINAL_STATES:
                    raise _public_error("RETRY_FORBIDDEN")
                raise _public_error("INVALID_STATE")
            if workflow.approval_consumed:
                raise _public_error("APPROVAL_REPLAY")
            if self._clock() >= _parse_iso(workflow.approval["expires_at"]):
                raise _public_error("APPROVAL_EXPIRED")
            execution_id = _derived_id("edit-execution", principal, key)
            challenge = secrets.token_hex(24)

            def manifest_factory(fence: int) -> Mapping[str, object]:
                return create_apply_manifest(
                    workflow.preview,
                    workflow.approval,
                    execution_id=execution_id,
                    fencing_token=fence,
                    challenge_nonce=challenge,
                )

            def receipt_factory(claimed: WorkbookEditWorkflow) -> WorkbookEditReceipt:
                assert claimed.manifest is not None
                return _receipt(
                    command_id=command_id,
                    workflow=claimed,
                    details={
                        "execution_id": claimed.execution_id,
                        "fence": claimed.fence,
                        "challenge": claimed.challenge,
                        "apply_manifest": claimed.manifest,
                    },
                )

            self._assert_binding_current(principal=principal, workflow=workflow)
            receipt = self._edits.claim_execution(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow_id=workflow.workflow_id,
                expected_version=workflow.version,
                execution_id=execution_id,
                challenge=challenge,
                publication_required=require_publication,
                manifest_factory=manifest_factory,
                receipt_factory=receipt_factory,
            )
            return WorkbookEditSubmission(receipt=receipt, replayed=False)
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise
        except WorkbookEditConflictError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("ACTIVE_EXECUTION_CONFLICT") from None
        except WorkbookEditError as error:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _contract_error(error) from None
        except Exception:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def mark_apply_started(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        challenge: str,
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_execution_id = _public_opaque(execution_id)
        clean_fence = _public_fence(fence)
        clean_challenge = _public_opaque(challenge)
        command = _execution_command(
            "mark_apply_started",
            clean_workflow_id,
            clean_execution_id,
            clean_fence,
            clean_challenge,
        )
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal,
            idempotency_key=idempotency_key,
            command=command,
        )
        if replay is not None:
            return replay
        try:
            workflow = self._current(principal=principal, workflow_id=clean_workflow_id)
            if workflow.state != "claimed":
                if workflow.state == "apply_started" or workflow.state in _TERMINAL_STATES:
                    raise _public_error("RETRY_FORBIDDEN")
                raise _public_error("INVALID_STATE")
            self._validate_execution(
                workflow,
                execution_id=clean_execution_id,
                fence=clean_fence,
                challenge=clean_challenge,
                require_unexpired=False,
            )
            started_at = self._clock()
            if workflow.approval is None or started_at >= _parse_iso(
                workflow.approval["expires_at"]
            ):
                raise _public_error("APPROVAL_EXPIRED")
            execution_deadline = _iso_utc(started_at + self._execution_ttl)
            updated = replace(
                workflow,
                state="apply_started",
                version=workflow.version + 1,
                execution_deadline=execution_deadline,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=updated,
                expected_version=workflow.version,
                details={
                    "execution_id": clean_execution_id,
                    "fence": clean_fence,
                    "manifest_ref": (
                        None if workflow.manifest is None else workflow.manifest["manifest_ref"]
                    ),
                    "write_started": True,
                    "execution_deadline": execution_deadline,
                },
                require_active=True,
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise
        except Exception:
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def abort_claim(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        challenge: str,
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_execution_id = _public_opaque(execution_id)
        clean_fence = _public_fence(fence)
        clean_challenge = _public_opaque(challenge)
        command = _execution_command(
            "abort_claim",
            clean_workflow_id,
            clean_execution_id,
            clean_fence,
            clean_challenge,
        )
        return self._simple_transition(
            principal=principal,
            workflow_id=clean_workflow_id,
            idempotency_key=idempotency_key,
            command=command,
            expected_state="claimed",
            new_state="aborted_before_apply",
            validator=lambda current: self._validate_execution(
                current,
                execution_id=clean_execution_id,
                fence=clean_fence,
                challenge=clean_challenge,
                require_unexpired=False,
            ),
            details={
                "execution_id": clean_execution_id,
                "fence": clean_fence,
                "reason": "claim_aborted",
            },
            release_active=True,
            require_current_binding=False,
        )

    def verify_execution(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        fence: int,
        challenge: str,
        witness: Mapping[str, object],
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        clean_workflow_id = _public_opaque(workflow_id)
        clean_execution_id = _public_opaque(execution_id)
        clean_fence = _public_fence(fence)
        clean_challenge = _public_opaque(challenge)
        witness_input = _strict_mapping(
            witness,
            fields={"outcome", "observed_before", "actual_after", "recalculation"},
        )
        command = {
            **_execution_command(
                "verify_execution",
                clean_workflow_id,
                clean_execution_id,
                clean_fence,
                clean_challenge,
            ),
            "witness": witness_input,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal, idempotency_key=idempotency_key, command=command
        )
        if replay is not None:
            return replay
        try:
            # Once an exact fenced execution exists, registry churn must not prevent recording
            # a stale, applied, failed, or indeterminate witness.  The manifest binding and
            # execution challenge remain the authority for this terminal write report.
            workflow = self._stored(principal=principal, workflow_id=clean_workflow_id)
            outcome = witness_input["outcome"]
            if outcome == "stale_precondition":
                if workflow.state != "claimed":
                    if workflow.state == "apply_started" or workflow.state in _TERMINAL_STATES:
                        raise _public_error("RETRY_FORBIDDEN")
                    raise _public_error("INVALID_STATE")
            elif workflow.state != "apply_started":
                if workflow.state in _TERMINAL_STATES:
                    raise _public_error("RETRY_FORBIDDEN")
                raise _public_error("INVALID_STATE")
            if outcome == "applied":
                if (
                    workflow.execution_deadline is None
                    or self._clock() >= _parse_iso(workflow.execution_deadline)
                ):
                    raise _public_error("EXECUTION_LEASE_EXPIRED")
            self._validate_execution(
                workflow,
                execution_id=clean_execution_id,
                fence=clean_fence,
                challenge=clean_challenge,
                require_unexpired=False,
            )
            if workflow.manifest is None:
                raise _public_error("SERVICE_UNAVAILABLE")
            canonical_witness = create_execution_witness(
                workflow.manifest,
                executor_id=principal.subject_id,
                outcome=witness_input["outcome"],
                observed_before=witness_input["observed_before"],
                actual_after=witness_input["actual_after"],
                recalculation=witness_input["recalculation"],
            )
            verification = verify_execution_witness(workflow.manifest, canonical_witness)
            verified_state: WorkflowState = (
                verification["status"]
                if verification["status"] in _VERIFICATION_STATES
                else "verification_failed"
            )
            updated = replace(
                workflow,
                state=verified_state,
                version=workflow.version + 1,
                witness=canonical_witness,
                verification=verification,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=updated,
                expected_version=workflow.version,
                details={"verification": verification},
                # In a publication-enabled host, a verified write still owns the workbook-level
                # publication lease. Only the raw source-head CAS releases it. The legacy/local
                # session-only service keeps its previous terminal behavior.
                release_active=(
                    verified_state == "stale_precondition"
                    or (
                        verified_state == "session_verified"
                        and workflow.publication_required is not True
                    )
                ),
                require_current_binding=False,
                require_active=True,
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise
        except WorkbookEditError as error:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _contract_error(error) from None
        except Exception:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def _validate_execution(
        self,
        workflow: WorkbookEditWorkflow,
        *,
        execution_id: str,
        fence: int,
        challenge: str,
        require_unexpired: bool,
    ) -> None:
        if (
            not isinstance(fence, int)
            or isinstance(fence, bool)
            or fence < 1
            or workflow.execution_id != execution_id
            or workflow.fence != fence
            or workflow.challenge != challenge
        ):
            raise _public_error("EXECUTION_CLAIM_MISMATCH")
        if require_unexpired:
            if workflow.approval is None or self._clock() >= _parse_iso(
                workflow.approval["expires_at"]
            ):
                raise _public_error("APPROVAL_EXPIRED")

    def _simple_transition(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        idempotency_key: str,
        command: Mapping[str, object],
        expected_state: WorkflowState,
        new_state: WorkflowState,
        validator: Callable[[WorkbookEditWorkflow], None],
        details: Mapping[str, object] | Callable[[WorkbookEditWorkflow], Mapping[str, object]],
        release_active: bool = False,
        require_current_binding: bool = True,
    ) -> WorkbookEditSubmission:
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal, idempotency_key=idempotency_key, command=command
        )
        if replay is not None:
            return replay
        try:
            workflow = (
                self._current(principal=principal, workflow_id=workflow_id)
                if require_current_binding
                else self._stored(principal=principal, workflow_id=workflow_id)
            )
            if workflow.state != expected_state:
                if workflow.state == "apply_started" or workflow.state in _TERMINAL_STATES:
                    raise _public_error("RETRY_FORBIDDEN")
                raise _public_error("INVALID_STATE")
            validator(workflow)
            updated = replace(
                workflow,
                state=new_state,
                version=workflow.version + 1,
            )
            return self._publish(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow=updated,
                expected_version=workflow.version,
                details=details(workflow) if callable(details) else details,
                release_active=release_active,
                require_current_binding=require_current_binding,
            )
        except WorkbookEditServiceError:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise
        except Exception:
            self._abort_pending(
                principal=principal, idempotency_key=key, command_sha256=digest,
                command_id=command_id, claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def get_workflow(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> dict[str, object]:
        workflow = self._stored(principal=principal, workflow_id=workflow_id)
        refs: dict[str, object] = {
            "proposal_ref": workflow.proposal.get("proposal_ref"),
            "preview_ref": None if workflow.preview is None else workflow.preview.get("preview_ref"),
            "approval_ref": None if workflow.approval is None else workflow.approval.get("approval_ref"),
            "manifest_ref": None if workflow.manifest is None else workflow.manifest.get("manifest_ref"),
            "verification_ref": None if workflow.verification is None else workflow.verification.get("verification_ref"),
        }
        manifest_summary = None
        if workflow.manifest is not None:
            manifest_summary = {
                "manifest_ref": workflow.manifest.get("manifest_ref"),
                "manifest_sha256": workflow.manifest.get("manifest_sha256"),
                "execution_id": workflow.manifest.get("execution_id"),
                "fencing_token": workflow.manifest.get("fencing_token"),
                "redacted": True,
            }
        return {
            "schema_version": "audit_workbook_edit_workflow.v1",
            "workflow_id": workflow.workflow_id,
            **_binding_identity(workflow.binding),
            "state": workflow.state,
            "version": workflow.version,
            "approval_consumed": workflow.approval_consumed,
            "execution_id": workflow.execution_id,
            "fence": workflow.fence,
            "execution_deadline": workflow.execution_deadline,
            "refs": refs,
            "artifacts": {
                "proposal": copy.deepcopy(workflow.proposal),
                "preview": copy.deepcopy(workflow.preview),
                "approval": copy.deepcopy(workflow.approval),
                "manifest": manifest_summary,
                "verification": copy.deepcopy(workflow.verification),
            },
        }

    def publish_verified_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
        manifest_ref: str,
        manifest_sha256: str,
        idempotency_key: str,
    ) -> WorkbookEditSubmission:
        """Reacquire, persist, and CAS-publish one verified saved workbook revision.

        The client supplies only exact content-addressed execution selectors. Workbook bytes,
        provider revision, physical workbook identity, asset location, and the base source head
        all come from server-owned state.
        """

        clean_workflow_id = _public_opaque(workflow_id)
        clean_execution_id = _public_opaque(execution_id)
        clean_manifest_ref = _public_manifest_ref(manifest_ref)
        clean_manifest_sha256 = _public_sha256(manifest_sha256)
        command = {
            "operation": "publish_verified_snapshot",
            "workflow_id": clean_workflow_id,
            "execution_id": clean_execution_id,
            "manifest_ref": clean_manifest_ref,
            "manifest_sha256": clean_manifest_sha256,
        }
        key, digest, command_id, claim_token, replay = self._begin(
            principal=principal,
            idempotency_key=idempotency_key,
            command=command,
        )
        if replay is not None:
            return replay
        active_publication_claim: tuple[str, str, int, str] | None = None
        try:
            if not self.snapshot_publication_enabled:
                raise _public_error("PUBLICATION_UNAVAILABLE")
            workflow = self._stored(
                principal=principal,
                workflow_id=clean_workflow_id,
            )
            if (
                workflow.state != "session_verified"
                or workflow.publication is not None
                or workflow.publication_required is not True
            ):
                raise _public_error("PUBLICATION_NOT_READY")
            if (
                workflow.execution_id != clean_execution_id
                or workflow.manifest is None
                or workflow.verification is None
                or workflow.manifest.get("manifest_ref") != clean_manifest_ref
                or workflow.manifest.get("manifest_sha256") != clean_manifest_sha256
                or workflow.verification.get("status") != "session_verified"
                or workflow.verification.get("new_snapshot_required") is not True
                or workflow.verification.get("asset_persisted") is not False
            ):
                raise _public_error("PUBLICATION_BASIS_MISMATCH")
            if workflow.fence is None:
                raise _public_error("SERVICE_UNAVAILABLE")
            claim_expires_at = _iso_utc(self._clock() + self._publication_claim_ttl)
            self._edits.claim_snapshot_publication(
                principal=principal,
                workflow_id=workflow.workflow_id,
                execution_id=clean_execution_id,
                fence=workflow.fence,
                publication_claim_token=claim_token,
                claim_expires_at=claim_expires_at,
            )
            active_publication_claim = (
                workflow.workflow_id,
                clean_execution_id,
                workflow.fence,
                claim_token,
            )
            basis = SnapshotPublicationBasis(
                bundle_id=workflow.binding.bundle_id,
                execution_id=clean_execution_id,
                manifest_ref=clean_manifest_ref,
                manifest_sha256=clean_manifest_sha256,
                base_snapshot_id=workflow.binding.snapshot_id,
                base_workbook_sha256=workflow.binding.workbook_sha256,
                base_revision_id=workflow.binding.revision_id,
                sheet=workflow.binding.sheet,
                worksheet_id=workflow.binding.worksheet_id,
                workbook_instance_id=workflow.binding.workbook_instance_id,
            )
            assert self._saved_workbooks is not None
            assert self._workbook_assets is not None
            acquired = reacquire_saved_workbook(
                basis=basis,
                reacquirer=self._saved_workbooks,
            )
            validate_saved_workbook_manifest(acquired, workflow.manifest)
            stored = store_acquired_workbook(
                acquired=acquired,
                assets=self._workbook_assets,
            )
            publication = build_snapshot_publication(
                basis=basis,
                acquired=acquired,
                stored=stored,
            )

            def receipt_factory(published: WorkbookEditWorkflow) -> WorkbookEditReceipt:
                return _receipt(
                    command_id=command_id,
                    workflow=published,
                    details={"publication": publication},
                )

            receipt = self._edits.publish_snapshot(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
                workflow_id=workflow.workflow_id,
                expected_version=workflow.version,
                execution_id=clean_execution_id,
                fence=workflow.fence,
                publication=publication,
                asset_ref=stored.asset_ref,
                publication_claim_token=claim_token,
                receipt_factory=receipt_factory,
            )
            active_publication_claim = None
            return WorkbookEditSubmission(receipt=receipt, replayed=False)
        except WorkbookEditServiceError:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise
        except WorkbookSnapshotPublicationError as error:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _publication_error(error) from None
        except WorkbookEditPublicationClaimError:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("COMMAND_IN_PROGRESS") from None
        except WorkbookEditConflictError:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("PUBLICATION_CONFLICT") from None
        except WorkbookEditRepositoryError:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            self._release_publication_claim(
                principal=principal,
                claim=active_publication_claim,
            )
            self._abort_pending(
                principal=principal,
                idempotency_key=key,
                command_sha256=digest,
                command_id=command_id,
                claim_token=claim_token,
            )
            raise _public_error("SERVICE_UNAVAILABLE") from None

    def get_snapshot_publication(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        workflow = self._stored(
            principal=principal,
            workflow_id=_public_opaque(workflow_id),
        )
        clean_execution_id = _public_opaque(execution_id)
        if workflow.execution_id != clean_execution_id:
            raise _public_error("PUBLICATION_NOT_FOUND")
        if workflow.publication is None:
            if workflow.state == "session_verified":
                raise _public_error("PUBLICATION_NOT_READY")
            raise _public_error("PUBLICATION_NOT_FOUND")
        return copy.deepcopy(dict(workflow.publication))

    def resolve_current_binding(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookSessionBinding:
        """Return an exact private binding for an authenticated host-session issuer.

        This method is intentionally not exposed by the public workbook-edit HTTP adapter.  The
        host bootstrap service hashes the private binding, retains workbook_instance_id only on
        the server, and returns a separately bounded public document.
        """

        workflow = self._current(principal=principal, workflow_id=workflow_id)
        return copy.deepcopy(workflow.binding)

    def resolve_published_binding(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookSessionBinding:
        """Return the immutable base binding only after its snapshot publication committed."""

        workflow = self._stored(principal=principal, workflow_id=workflow_id)
        if workflow.publication is None:
            raise _public_error("PUBLICATION_NOT_FOUND")
        return copy.deepcopy(workflow.binding)


_ERRORS: dict[str, tuple[str, int]] = {
    "INVALID_REQUEST": ("The request does not match the workbook edit contract.", 400),
    "INVALID_IDEMPOTENCY_KEY": ("Idempotency-Key must be a non-empty visible string.", 400),
    "SESSION_NOT_FOUND": ("The workbook session was not found.", 404),
    "WORKFLOW_NOT_FOUND": ("The workbook edit workflow was not found.", 404),
    "PRINCIPAL_MISMATCH": ("The workbook session is not available to this principal.", 403),
    "SESSION_MISMATCH": ("The execution session does not match the approved workflow.", 409),
    "BUNDLE_MISMATCH": ("The workbook bundle changed after the workflow was created.", 409),
    "WORKBOOK_MISMATCH": ("The open workbook does not match the approved workflow.", 409),
    "STALE_REVISION": ("The workbook revision changed after the workflow was created.", 409),
    "WORKSHEET_MISMATCH": ("The worksheet does not match the approved workflow.", 409),
    "INVALID_STATE": ("The workbook edit workflow is not in the required state.", 409),
    "EDIT_CONFLICT": ("The workbook edit workflow changed concurrently.", 409),
    "ACTIVE_EXECUTION_CONFLICT": ("The workbook session already has an active execution.", 409),
    "IDEMPOTENCY_CONFLICT": ("The idempotency key belongs to another command.", 409),
    "COMMAND_IN_PROGRESS": ("The same command is already in progress.", 409),
    "APPROVAL_CONFIRMATION_REQUIRED": ("Explicit approval confirmation is required.", 400),
    "PREVIEW_MISMATCH": ("Approval is not bound to the exact current preview.", 409),
    "APPROVAL_REPLAY": ("The approval has already been consumed.", 409),
    "APPROVAL_EXPIRED": ("The approval expired before execution started.", 409),
    "EXECUTION_CLAIM_MISMATCH": ("The execution fence or challenge is invalid.", 409),
    "EXECUTION_LEASE_EXPIRED": ("The execution lease expired before verification.", 409),
    "RETRY_FORBIDDEN": ("The execution cannot be aborted or retried after writing started.", 409),
    "FORMULA_INJECTION_BLOCKED": ("The literal value could be interpreted as a formula.", 400),
    "UNSAFE_FORMULA": ("The formula is outside the allowed edit policy.", 400),
    "DUPLICATE_CELL": ("A proposal cannot edit the same cell more than once.", 400),
    "UNSAFE_TARGET": ("The target cell is outside the supported safe edit scope.", 409),
    "NO_OP_EDIT": ("The edit would not change the current Office state.", 409),
    "LIMIT_EXCEEDED": ("The workbook edit payload exceeds the supported limit.", 413),
    "PUBLICATION_UNAVAILABLE": ("Saved workbook publication is not configured.", 503),
    "PUBLICATION_NOT_READY": ("The verified workbook publication is not ready.", 409),
    "PUBLICATION_NOT_FOUND": ("The workbook snapshot publication was not found.", 404),
    "PUBLICATION_BASIS_MISMATCH": ("The publication request does not match the verified execution.", 409),
    "PUBLICATION_CONFLICT": ("The workbook source snapshot head changed.", 409),
    "EDIT_CONTRACT_MISMATCH": ("The workbook edit artifact failed validation.", 500),
    "SERVICE_UNAVAILABLE": ("The workbook edit service could not safely complete the request.", 503),
}


def _public_error(code: str) -> WorkbookEditServiceError:
    message, status_code = _ERRORS[code]
    return WorkbookEditServiceError(code, message, status_code=status_code)


def _contract_error(error: WorkbookEditError) -> WorkbookEditServiceError:
    if error.code in {
        "FORMULA_INJECTION_BLOCKED",
        "UNSAFE_FORMULA",
        "DUPLICATE_CELL",
        "UNSAFE_TARGET",
        "NO_OP_EDIT",
        "LIMIT_EXCEEDED",
    }:
        return _public_error(error.code)
    if error.code == "INVALID_INPUT":
        return _public_error("INVALID_REQUEST")
    return _public_error("EDIT_CONTRACT_MISMATCH")


def _publication_error(
    error: WorkbookSnapshotPublicationError,
) -> WorkbookEditServiceError:
    if error.code == "SOURCE_LIMIT_EXCEEDED":
        return _public_error("LIMIT_EXCEEDED")
    if error.code in {
        "WORKBOOK_INSTANCE_MISMATCH",
        "WORKSHEET_MISMATCH",
        "REVISION_NOT_ADVANCED",
        "REVISION_CHAIN_MISMATCH",
        "WORKBOOK_NOT_CHANGED",
        "SNAPSHOT_NOT_ADVANCED",
        "INVALID_ACQUIRED_WORKBOOK",
        "INVALID_STORED_ASSET",
        "ASSET_INTEGRITY_MISMATCH",
        "SAVED_WORKBOOK_MISMATCH",
    }:
        return _public_error("PUBLICATION_CONFLICT")
    return _public_error("SERVICE_UNAVAILABLE")


def _require_principal(value: object) -> ServicePrincipal:
    if not isinstance(value, ServicePrincipal):
        raise _public_error("PRINCIPAL_MISMATCH")
    return value


def _public_opaque(value: object) -> str:
    try:
        return _opaque(value, field="identifier")
    except (TypeError, ValueError):
        raise _public_error("INVALID_REQUEST") from None


def _public_sha256(value: object) -> str:
    try:
        return _sha256(value, field="sha256")
    except (TypeError, ValueError):
        raise _public_error("INVALID_REQUEST") from None


def _public_manifest_ref(value: object) -> str:
    if not isinstance(value, str) or _MANIFEST_REF_RE.fullmatch(value) is None:
        raise _public_error("INVALID_REQUEST")
    return value


def _public_fence(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_SAFE_INTEGER
    ):
        raise _public_error("INVALID_REQUEST")
    return value


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise _public_error("INVALID_IDEMPOTENCY_KEY")
    return value


def _canonical_digest(value: Mapping[str, object]) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise _public_error("INVALID_REQUEST") from None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _strict_mapping(
    value: object,
    *,
    fields: set[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _public_error("INVALID_REQUEST")
    try:
        if set(value) != fields:
            raise _public_error("INVALID_REQUEST")
        document = copy.deepcopy(dict(value))
    except WorkbookEditServiceError:
        raise
    except Exception:
        raise _public_error("INVALID_REQUEST") from None
    _canonical_digest(document)
    return document


def _assert_public_value(value: object) -> None:
    def visit(item: object) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise ValueError("public document contains a non-finite number")
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                    raise ValueError("public document contains a forbidden field")
                visit(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
            return
        raise ValueError("public document is not JSON-compatible")

    visit(value)
    _canonical_digest(dict(value) if isinstance(value, Mapping) else {"value": value})


def _derived_id(prefix: str, principal: ServicePrincipal, key: str) -> str:
    digest = hashlib.sha256(
        (
            prefix
            + "\0"
            + principal.tenant_id
            + "\0"
            + principal.subject_id
            + "\0"
            + key
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _binding_identity(binding: WorkbookSessionBinding) -> dict[str, object]:
    return {
        "session_id": binding.session_id,
        "bundle_id": binding.bundle_id,
        "snapshot_id": binding.snapshot_id,
        "workbook_sha256": binding.workbook_sha256,
        "revision_id": binding.revision_id,
        "sheet": binding.sheet,
        "worksheet_id": binding.worksheet_id,
    }


def _live_workbook_key(binding: WorkbookSessionBinding) -> tuple[str, str]:
    """Stable physical workbook identity; revision is a precondition, never a lock key."""

    return (binding.tenant_id, binding.workbook_instance_id)


def _receipt(
    *,
    command_id: str,
    workflow: WorkbookEditWorkflow,
    details: Mapping[str, object],
) -> WorkbookEditReceipt:
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
        details=copy.deepcopy(dict(details)),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise _public_error("EDIT_CONTRACT_MISMATCH")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise _public_error("EDIT_CONTRACT_MISMATCH") from None
    if parsed.tzinfo is None:
        raise _public_error("EDIT_CONTRACT_MISMATCH")
    return parsed.astimezone(timezone.utc)


def _require_preview(workflow: WorkbookEditWorkflow, preview_id: str) -> None:
    if workflow.preview is None or workflow.preview.get("preview_ref") != preview_id:
        raise _public_error("PREVIEW_MISMATCH")


def _execution_command(
    operation: str,
    workflow_id: str,
    execution_id: object,
    fence: object,
    challenge: object,
) -> dict[str, object]:
    return {
        "operation": operation,
        "workflow_id": workflow_id,
        "execution_id": execution_id,
        "fence": fence,
        "challenge": challenge,
    }


__all__ = [
    "InMemoryWorkbookEditRepository",
    "InMemoryWorkbookSessionRepository",
    "WorkbookEditCommandClaim",
    "WorkbookEditConflictError",
    "WorkbookEditIdempotencyClaimError",
    "WorkbookEditIdempotencyConflictError",
    "WorkbookEditReceipt",
    "WorkbookEditRepository",
    "WorkbookEditRepositoryError",
    "WorkbookEditService",
    "WorkbookEditServiceError",
    "WorkbookEditSubmission",
    "WorkbookEditWorkflow",
    "WorkbookSessionBinding",
    "WorkbookSessionNotFoundError",
    "WorkbookSessionRepository",
]
