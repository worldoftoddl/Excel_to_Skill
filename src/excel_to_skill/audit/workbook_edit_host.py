"""Authenticated host-session authority for the Office workbook-edit executor.

This module deliberately does not implement an identity provider, parse bearer tokens, or reach
into workbook-edit repositories.  A product host supplies an already authenticated
``ServicePrincipal`` plus two narrow resolvers: one for the currently registered Office session
and one for the workflow's pinned session binding.  The resulting host-session ID is an opaque,
expiring selector used together with that authenticated principal; it is not a bearer credential.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Protocol, cast

from .service import ServicePrincipal
from .workbook_edit_service import WorkbookEditServiceError, WorkbookSessionBinding


PersistencePolicy = Literal["required", "session_only", "unsupported"]

_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HOST_SESSION_ID_RE = re.compile(r"\Aedit-host-[0-9a-f]{32}\Z")
_PERSISTENCE_POLICIES = frozenset({"required", "session_only", "unsupported"})
_MAX_COLLISION_RETRIES = 4


class WorkbookEditHostServiceError(RuntimeError):
    """A fixed, host-safe failure that may cross an HTTP adapter boundary."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class WorkbookEditHostRepositoryError(RuntimeError):
    """The private host-session repository could not safely complete an operation."""


class WorkbookEditHostSessionConflictError(WorkbookEditHostRepositoryError):
    """A randomly generated host-session ID already exists."""


@dataclass(frozen=True)
class WorkbookEditHostSession:
    """Private exact launch binding retained by the authenticated product host."""

    host_session_id: str
    tenant_id: str
    subject_id: str
    workflow_id: str
    session_id: str
    binding: WorkbookSessionBinding
    binding_sha256: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool
    persistence_policy: PersistencePolicy

    def __post_init__(self) -> None:
        _host_session_id(self.host_session_id)
        _opaque(self.tenant_id, field="tenant_id")
        _opaque(self.subject_id, field="subject_id")
        _opaque(self.workflow_id, field="workflow_id")
        _opaque(self.session_id, field="session_id")
        if not isinstance(self.binding, WorkbookSessionBinding):
            raise ValueError("binding must be a WorkbookSessionBinding")
        if self.binding.principal_scope != self.principal_scope:
            raise ValueError("binding principal does not match the host session")
        if self.binding.session_id != self.session_id:
            raise ValueError("binding session does not match the host session")
        if self.binding_sha256 != workbook_session_binding_sha256(self.binding):
            raise ValueError("binding_sha256 does not match the private binding")
        _aware_datetime(self.issued_at, field="issued_at")
        _aware_datetime(self.expires_at, field="expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if not isinstance(self.revoked, bool):
            raise ValueError("revoked must be a boolean")
        _persistence_policy(self.persistence_policy)

    @property
    def principal_scope(self) -> tuple[str, str]:
        return (self.tenant_id, self.subject_id)


class WorkbookEditHostSessionRepository(Protocol):
    """Private storage boundary; durable implementations must preserve atomic create/revoke."""

    def create(self, *, record: WorkbookEditHostSession) -> None: ...

    def get(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession | None: ...

    def revoke(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession | None: ...


class InMemoryWorkbookEditHostSessionRepository:
    """Thread-safe reference repository for tests and a single-process product host."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, WorkbookEditHostSession] = {}

    def create(self, *, record: WorkbookEditHostSession) -> None:
        if not isinstance(record, WorkbookEditHostSession):
            raise TypeError("record must be a WorkbookEditHostSession")
        with self._lock:
            if record.host_session_id in self._records:
                raise WorkbookEditHostSessionConflictError("host session already exists")
            self._records[record.host_session_id] = record

    def get(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession | None:
        if not isinstance(principal, ServicePrincipal):
            raise TypeError("principal must be a ServicePrincipal")
        clean_id = _host_session_id(host_session_id)
        with self._lock:
            found = self._records.get(clean_id)
            if found is None or found.principal_scope != principal.scope:
                return None
            return found

    def revoke(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession | None:
        if not isinstance(principal, ServicePrincipal):
            raise TypeError("principal must be a ServicePrincipal")
        clean_id = _host_session_id(host_session_id)
        with self._lock:
            found = self._records.get(clean_id)
            if found is None or found.principal_scope != principal.scope:
                return None
            if found.revoked:
                return found
            revoked = replace(found, revoked=True)
            self._records[clean_id] = revoked
            return revoked


CurrentBindingResolver = Callable[[ServicePrincipal, str], WorkbookSessionBinding]
WorkflowBindingResolver = Callable[[ServicePrincipal, str], WorkbookSessionBinding]
PublishedBindingResolver = Callable[[ServicePrincipal, str], WorkbookSessionBinding]


class WorkbookEditHostSessionService:
    """Issue and enforce exact, principal-scoped Office host-session bindings."""

    def __init__(
        self,
        *,
        repository: WorkbookEditHostSessionRepository,
        resolve_current_binding: CurrentBindingResolver,
        resolve_workflow_binding: WorkflowBindingResolver,
        resolve_published_binding: PublishedBindingResolver | None = None,
        session_ttl: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(session_ttl, timedelta) or not (
            timedelta(seconds=1) <= session_ttl <= timedelta(hours=24)
        ):
            raise ValueError("session_ttl must be between one second and 24 hours")
        if not callable(resolve_current_binding):
            raise TypeError("resolve_current_binding must be callable")
        if not callable(resolve_workflow_binding):
            raise TypeError("resolve_workflow_binding must be callable")
        if resolve_published_binding is not None and not callable(resolve_published_binding):
            raise TypeError("resolve_published_binding must be callable")
        self._repository = repository
        self._resolve_current_binding = resolve_current_binding
        self._resolve_workflow_binding = resolve_workflow_binding
        self._resolve_published_binding = resolve_published_binding
        self._session_ttl = session_ttl
        self._now = now or (lambda: datetime.now(timezone.utc))

    def issue(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        session_id: str,
        persistence_policy: PersistencePolicy,
    ) -> dict[str, object]:
        clean_principal = _principal(principal)
        clean_workflow_id = _public_opaque(workflow_id)
        clean_session_id = _public_opaque(session_id)
        try:
            clean_policy = _persistence_policy(persistence_policy)
        except (TypeError, ValueError):
            raise _public_error("INVALID_REQUEST") from None
        binding = self._resolve_exact_binding(
            principal=clean_principal,
            workflow_id=clean_workflow_id,
            session_id=clean_session_id,
        )
        return self._issue_binding(
            principal=clean_principal,
            workflow_id=clean_workflow_id,
            session_id=clean_session_id,
            binding=binding,
            persistence_policy=clean_policy,
        )

    def issue_published(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        session_id: str,
    ) -> dict[str, object]:
        """Issue a fresh selector for one already published immutable workflow.

        This is an authenticated host operation, not a public Add-in endpoint. The injected
        resolver must return a binding only when the workflow has a committed publication.
        """

        clean_principal = _principal(principal)
        clean_workflow_id = _public_opaque(workflow_id)
        clean_session_id = _public_opaque(session_id)
        if self._resolve_published_binding is None:
            raise _public_error("SERVICE_UNAVAILABLE")
        try:
            binding = self._resolve_published_binding(
                clean_principal,
                clean_workflow_id,
            )
        except LookupError:
            raise _public_error("HOST_BINDING_NOT_FOUND") from None
        except WorkbookEditServiceError as error:
            if error.code in {"WORKFLOW_NOT_FOUND", "PUBLICATION_NOT_FOUND"}:
                raise _public_error("HOST_BINDING_NOT_FOUND") from None
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if (
            not isinstance(binding, WorkbookSessionBinding)
            or binding.principal_scope != clean_principal.scope
            or binding.session_id != clean_session_id
        ):
            raise _public_error("BINDING_MISMATCH")
        return self._issue_binding(
            principal=clean_principal,
            workflow_id=clean_workflow_id,
            session_id=clean_session_id,
            binding=binding,
            persistence_policy="required",
        )

    def _issue_binding(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        session_id: str,
        binding: WorkbookSessionBinding,
        persistence_policy: PersistencePolicy,
    ) -> dict[str, object]:
        issued_at = self._clock()
        expires_at = issued_at + self._session_ttl
        digest = workbook_session_binding_sha256(binding)
        for _attempt in range(_MAX_COLLISION_RETRIES):
            record = WorkbookEditHostSession(
                host_session_id=f"edit-host-{secrets.token_hex(16)}",
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
                workflow_id=workflow_id,
                session_id=session_id,
                binding=binding,
                binding_sha256=digest,
                issued_at=issued_at,
                expires_at=expires_at,
                revoked=False,
                persistence_policy=persistence_policy,
            )
            try:
                self._repository.create(record=record)
            except WorkbookEditHostSessionConflictError:
                continue
            except Exception:
                raise _public_error("SERVICE_UNAVAILABLE") from None
            return _public_bootstrap(record)
        raise _public_error("SERVICE_UNAVAILABLE")

    def resolve(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> dict[str, object]:
        record = self._valid_record(
            principal=_principal(principal),
            host_session_id=_public_host_session_id(host_session_id),
        )
        return _public_bootstrap(record)

    def resolve_stored(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> dict[str, object]:
        """Authorize an immutable completed-workflow read after the live binding moved.

        Expiry, revocation, and principal scope still apply. Callers must separately bind the
        requested stored workflow/execution to this exact host-session record.
        """

        record = self._record(
            principal=_principal(principal),
            host_session_id=_public_host_session_id(host_session_id),
        )
        return _public_bootstrap(record)

    def authorize(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
        workflow_id: str,
        session_id: str,
    ) -> dict[str, object]:
        record = self._valid_record(
            principal=_principal(principal),
            host_session_id=_public_host_session_id(host_session_id),
        )
        if record.workflow_id != _public_opaque(workflow_id):
            raise _public_error("WORKFLOW_MISMATCH")
        if record.session_id != _public_opaque(session_id):
            raise _public_error("SESSION_MISMATCH")
        return _public_bootstrap(record)

    def revoke(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> None:
        clean_principal = _principal(principal)
        clean_id = _public_host_session_id(host_session_id)
        try:
            revoked = self._repository.revoke(
                principal=clean_principal,
                host_session_id=clean_id,
            )
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if revoked is None:
            raise _public_error("HOST_SESSION_NOT_FOUND")

    def _valid_record(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession:
        record = self._record(
            principal=principal,
            host_session_id=host_session_id,
        )
        current = self._resolve_exact_binding(
            principal=principal,
            workflow_id=record.workflow_id,
            session_id=record.session_id,
        )
        if (
            current != record.binding
            or workbook_session_binding_sha256(current) != record.binding_sha256
        ):
            raise _public_error("BINDING_MISMATCH")
        return record

    def _record(
        self,
        *,
        principal: ServicePrincipal,
        host_session_id: str,
    ) -> WorkbookEditHostSession:
        try:
            record = self._repository.get(
                principal=principal,
                host_session_id=host_session_id,
            )
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if record is None:
            raise _public_error("HOST_SESSION_NOT_FOUND")
        if not isinstance(record, WorkbookEditHostSession):
            raise _public_error("SERVICE_UNAVAILABLE")
        if record.revoked:
            raise _public_error("HOST_SESSION_REVOKED")
        if self._clock() >= record.expires_at:
            raise _public_error("HOST_SESSION_EXPIRED")
        return record

    def _resolve_exact_binding(
        self,
        *,
        principal: ServicePrincipal,
        workflow_id: str,
        session_id: str,
    ) -> WorkbookSessionBinding:
        try:
            current = self._resolve_current_binding(principal, session_id)
            workflow = self._resolve_workflow_binding(principal, workflow_id)
        except LookupError:
            raise _public_error("HOST_BINDING_NOT_FOUND") from None
        except WorkbookEditServiceError as error:
            if error.code in {"SESSION_NOT_FOUND", "WORKFLOW_NOT_FOUND"}:
                raise _public_error("HOST_BINDING_NOT_FOUND") from None
            if error.code in {
                "BUNDLE_MISMATCH",
                "WORKBOOK_MISMATCH",
                "STALE_REVISION",
                "WORKSHEET_MISMATCH",
            }:
                raise _public_error("BINDING_MISMATCH") from None
            raise _public_error("SERVICE_UNAVAILABLE") from None
        except WorkbookEditHostServiceError:
            raise
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if not isinstance(current, WorkbookSessionBinding) or not isinstance(
            workflow, WorkbookSessionBinding
        ):
            raise _public_error("SERVICE_UNAVAILABLE")
        if current.principal_scope != principal.scope or workflow.principal_scope != principal.scope:
            raise _public_error("PRINCIPAL_MISMATCH")
        if current.session_id != session_id or workflow.session_id != session_id:
            raise _public_error("SESSION_MISMATCH")
        if current != workflow:
            raise _public_error("BINDING_MISMATCH")
        return current

    def _clock(self) -> datetime:
        try:
            value = self._now()
        except Exception:
            raise _public_error("SERVICE_UNAVAILABLE") from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise _public_error("SERVICE_UNAVAILABLE")
        return value.astimezone(timezone.utc)


def workbook_session_binding_sha256(binding: WorkbookSessionBinding) -> str:
    """Hash every private binding field, including stable physical workbook identity."""

    if not isinstance(binding, WorkbookSessionBinding):
        raise TypeError("binding must be a WorkbookSessionBinding")
    document = {
        "schema_version": "audit_workbook_session_binding.v1",
        "session_id": binding.session_id,
        "tenant_id": binding.tenant_id,
        "subject_id": binding.subject_id,
        "bundle_id": binding.bundle_id,
        "snapshot_id": binding.snapshot_id,
        "workbook_sha256": binding.workbook_sha256,
        "revision_id": binding.revision_id,
        "sheet": binding.sheet,
        "worksheet_id": binding.worksheet_id,
        "workbook_instance_id": binding.workbook_instance_id,
    }
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _public_bootstrap(record: WorkbookEditHostSession) -> dict[str, object]:
    binding = record.binding
    return {
        "schema_version": "audit_workbook_edit_host_bootstrap.v1",
        "host_session_id": record.host_session_id,
        "workflow_id": record.workflow_id,
        "session_id": binding.session_id,
        "bundle_id": binding.bundle_id,
        "snapshot_id": binding.snapshot_id,
        "workbook_sha256": binding.workbook_sha256,
        "revision_id": binding.revision_id,
        "sheet": binding.sheet,
        "worksheet_id": binding.worksheet_id,
        "binding_sha256": record.binding_sha256,
        "expires_at": _iso_utc(record.expires_at),
        "persistence_policy": record.persistence_policy,
    }


_ERRORS: dict[str, tuple[str, int]] = {
    "INVALID_REQUEST": ("The host-session request is invalid.", 400),
    "HOST_SESSION_NOT_FOUND": ("The workbook host session was not found.", 404),
    "HOST_BINDING_NOT_FOUND": ("The workbook host binding was not found.", 404),
    "PRINCIPAL_MISMATCH": ("The workbook host binding is not available to this principal.", 403),
    "WORKFLOW_MISMATCH": ("The request does not match the host workflow.", 409),
    "SESSION_MISMATCH": ("The request does not match the Office session.", 409),
    "BINDING_MISMATCH": ("The workbook host binding changed.", 409),
    "HOST_SESSION_EXPIRED": ("The workbook host session expired.", 410),
    "HOST_SESSION_REVOKED": ("The workbook host session was revoked.", 410),
    "SERVICE_UNAVAILABLE": ("The workbook host-session service is unavailable.", 503),
}


def _public_error(code: str) -> WorkbookEditHostServiceError:
    message, status_code = _ERRORS[code]
    return WorkbookEditHostServiceError(code, message, status_code=status_code)


def _principal(value: object) -> ServicePrincipal:
    if not isinstance(value, ServicePrincipal):
        raise _public_error("PRINCIPAL_MISMATCH")
    return value


def _opaque(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an opaque identifier")
    return value


def _public_opaque(value: object) -> str:
    try:
        return _opaque(value, field="identifier")
    except (TypeError, ValueError):
        raise _public_error("INVALID_REQUEST") from None


def _host_session_id(value: object) -> str:
    if not isinstance(value, str) or _HOST_SESSION_ID_RE.fullmatch(value) is None:
        raise ValueError("host_session_id must contain 128 bits of lowercase hexadecimal entropy")
    return value


def _public_host_session_id(value: object) -> str:
    try:
        return _host_session_id(value)
    except (TypeError, ValueError):
        raise _public_error("INVALID_REQUEST") from None


def _persistence_policy(value: object) -> PersistencePolicy:
    if not isinstance(value, str) or value not in _PERSISTENCE_POLICIES:
        raise ValueError("persistence_policy is invalid")
    return cast(PersistencePolicy, value)


def _aware_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CurrentBindingResolver",
    "InMemoryWorkbookEditHostSessionRepository",
    "PersistencePolicy",
    "WorkbookEditHostRepositoryError",
    "WorkbookEditHostServiceError",
    "WorkbookEditHostSession",
    "WorkbookEditHostSessionConflictError",
    "WorkbookEditHostSessionRepository",
    "WorkbookEditHostSessionService",
    "WorkflowBindingResolver",
    "workbook_session_binding_sha256",
]
