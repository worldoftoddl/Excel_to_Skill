from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import jsonschema
import pytest

from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_edit_host import (
    InMemoryWorkbookEditHostSessionRepository,
    PersistencePolicy,
    WorkbookEditHostServiceError,
    WorkbookEditHostSessionService,
    workbook_session_binding_sha256,
)
from excel_to_skill.audit.workbook_edit_service import WorkbookSessionBinding
from excel_to_skill.resources import SCHEMA_DIR


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
OTHER_PRINCIPAL = ServicePrincipal("tenant-a", "user-b")


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


class _Resolvers:
    def __init__(self, binding: WorkbookSessionBinding) -> None:
        self.current = binding
        self.workflow = binding

    def current_binding(
        self,
        principal: ServicePrincipal,
        session_id: str,
    ) -> WorkbookSessionBinding:
        del principal
        if session_id != self.current.session_id:
            raise LookupError(session_id)
        return self.current

    def workflow_binding(
        self,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookSessionBinding:
        del principal
        if workflow_id != "workflow-a":
            raise LookupError(workflow_id)
        return self.workflow

    def published_binding(
        self,
        principal: ServicePrincipal,
        workflow_id: str,
    ) -> WorkbookSessionBinding:
        return self.workflow_binding(principal, workflow_id)


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def host_service():
    repository = InMemoryWorkbookEditHostSessionRepository()
    resolvers = _Resolvers(_binding())
    clock = _Clock()
    service = WorkbookEditHostSessionService(
        repository=repository,
        resolve_current_binding=resolvers.current_binding,
        resolve_workflow_binding=resolvers.workflow_binding,
        resolve_published_binding=resolvers.published_binding,
        session_ttl=timedelta(minutes=15),
        now=clock,
    )
    return service, repository, resolvers, clock


def _issue(
    service: WorkbookEditHostSessionService,
    *,
    principal: ServicePrincipal = PRINCIPAL,
    persistence_policy: str = "required",
) -> dict[str, object]:
    return service.issue(
        principal=principal,
        workflow_id="workflow-a",
        session_id="office-session-a",
        persistence_policy=cast(PersistencePolicy, persistence_policy),
    )


def _assert_error(code: str, call) -> WorkbookEditHostServiceError:
    with pytest.raises(WorkbookEditHostServiceError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.message in {
        "The host-session request is invalid.",
        "The workbook host session was not found.",
        "The workbook host binding was not found.",
        "The workbook host binding is not available to this principal.",
        "The request does not match the host workflow.",
        "The request does not match the Office session.",
        "The workbook host binding changed.",
        "The workbook host session expired.",
        "The workbook host session was revoked.",
        "The workbook host-session service is unavailable.",
    }
    return caught.value


def test_issue_returns_strict_public_bootstrap_without_private_fields(host_service) -> None:
    service, _, _, _ = host_service

    bootstrap = _issue(service)

    schema = json.loads(
        (SCHEMA_DIR / "audit_workbook_edit_host_bootstrap.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(bootstrap)
    assert bootstrap["host_session_id"].startswith("edit-host-")
    assert len(bootstrap["host_session_id"]) == len("edit-host-") + 32
    assert bootstrap["workflow_id"] == "workflow-a"
    assert bootstrap["persistence_policy"] == "required"
    assert bootstrap["expires_at"] == "2026-07-14T00:15:00Z"
    assert bootstrap["session_id"] == "office-session-a"
    assert bootstrap["bundle_id"] == "bundle-a"
    assert bootstrap["snapshot_id"] == "a" * 64
    assert bootstrap["workbook_sha256"] == "b" * 64
    assert bootstrap["revision_id"] == "revision-a"
    assert bootstrap["sheet"] == "매출채권"
    assert bootstrap["worksheet_id"] == "worksheet-a"
    serialized = json.dumps(bootstrap, ensure_ascii=False).casefold()
    for private_name in (
        "tenant_id",
        "subject_id",
        "workbook_instance_id",
        "path",
        "secret",
        "token",
        "authorization",
    ):
        assert private_name not in serialized


@pytest.mark.parametrize("policy", ["required", "session_only", "unsupported"])
def test_every_persistence_policy_round_trips(host_service, policy: str) -> None:
    service, _, _, _ = host_service
    bootstrap = _issue(service, persistence_policy=policy)
    assert bootstrap["persistence_policy"] == policy


def test_binding_digest_includes_private_stable_workbook_identity() -> None:
    original = _binding()
    copied_workbook = replace(original, workbook_instance_id="workbook-instance-copy")
    other_principal = replace(original, subject_id="other-user")

    assert workbook_session_binding_sha256(original) != workbook_session_binding_sha256(
        copied_workbook
    )
    assert workbook_session_binding_sha256(original) != workbook_session_binding_sha256(
        other_principal
    )


def test_resolve_and_authorize_revalidate_exact_live_binding(host_service) -> None:
    service, _, _, _ = host_service
    bootstrap = _issue(service)
    host_session_id = bootstrap["host_session_id"]

    assert service.resolve(
        principal=PRINCIPAL,
        host_session_id=host_session_id,
    ) == bootstrap
    assert service.authorize(
        principal=PRINCIPAL,
        host_session_id=host_session_id,
        workflow_id="workflow-a",
        session_id="office-session-a",
    ) == bootstrap


def test_expired_session_fails_closed(host_service) -> None:
    service, _, _, clock = host_service
    bootstrap = _issue(service)
    clock.value = NOW + timedelta(minutes=15)

    error = _assert_error(
        "HOST_SESSION_EXPIRED",
        lambda: service.resolve(
            principal=PRINCIPAL,
            host_session_id=bootstrap["host_session_id"],
        ),
    )
    assert error.status_code == 410


def test_authenticated_host_can_issue_fresh_selector_for_published_workflow(
    host_service,
) -> None:
    service, _, _, clock = host_service
    expired = _issue(service)
    clock.value = NOW + timedelta(minutes=16)
    _assert_error(
        "HOST_SESSION_EXPIRED",
        lambda: service.resolve(
            principal=PRINCIPAL,
            host_session_id=expired["host_session_id"],
        ),
    )

    renewed = service.issue_published(
        principal=PRINCIPAL,
        workflow_id="workflow-a",
        session_id="office-session-a",
    )
    assert renewed["host_session_id"] != expired["host_session_id"]
    assert renewed["persistence_policy"] == "required"
    assert renewed["expires_at"] == "2026-07-14T00:31:00Z"
    assert service.resolve_stored(
        principal=PRINCIPAL,
        host_session_id=renewed["host_session_id"],
    ) == renewed


def test_revoke_is_atomic_and_idempotent_but_revoked_sessions_cannot_resolve(
    host_service,
) -> None:
    service, repository, _, _ = host_service
    bootstrap = _issue(service)
    host_session_id = bootstrap["host_session_id"]

    service.revoke(principal=PRINCIPAL, host_session_id=host_session_id)
    service.revoke(principal=PRINCIPAL, host_session_id=host_session_id)

    stored = repository.get(principal=PRINCIPAL, host_session_id=host_session_id)
    assert stored is not None and stored.revoked is True
    _assert_error(
        "HOST_SESSION_REVOKED",
        lambda: service.resolve(
            principal=PRINCIPAL,
            host_session_id=host_session_id,
        ),
    )


def test_cross_principal_access_is_hidden_as_not_found(host_service) -> None:
    service, _, _, _ = host_service
    bootstrap = _issue(service)

    error = _assert_error(
        "HOST_SESSION_NOT_FOUND",
        lambda: service.authorize(
            principal=OTHER_PRINCIPAL,
            host_session_id=bootstrap["host_session_id"],
            workflow_id="workflow-a",
            session_id="office-session-a",
        ),
    )
    assert error.status_code == 404


def test_authorize_rejects_workflow_and_session_substitution(host_service) -> None:
    service, _, _, _ = host_service
    bootstrap = _issue(service)
    host_session_id = bootstrap["host_session_id"]

    _assert_error(
        "WORKFLOW_MISMATCH",
        lambda: service.authorize(
            principal=PRINCIPAL,
            host_session_id=host_session_id,
            workflow_id="workflow-b",
            session_id="office-session-a",
        ),
    )
    _assert_error(
        "SESSION_MISMATCH",
        lambda: service.authorize(
            principal=PRINCIPAL,
            host_session_id=host_session_id,
            workflow_id="workflow-a",
            session_id="office-session-b",
        ),
    )


@pytest.mark.parametrize(
    "drift",
    [
        {"workbook_instance_id": "workbook-instance-copy"},
        {"revision_id": "revision-new"},
        {"snapshot_id": "c" * 64},
        {"worksheet_id": "worksheet-b"},
    ],
)
def test_current_binding_drift_invalidates_existing_host_session(host_service, drift) -> None:
    service, _, resolvers, _ = host_service
    bootstrap = _issue(service)
    resolvers.current = _binding(**drift)
    resolvers.workflow = resolvers.current

    _assert_error(
        "BINDING_MISMATCH",
        lambda: service.resolve(
            principal=PRINCIPAL,
            host_session_id=bootstrap["host_session_id"],
        ),
    )


def test_workflow_binding_drift_is_detected_separately_from_current_session(host_service) -> None:
    service, _, resolvers, _ = host_service
    bootstrap = _issue(service)
    resolvers.workflow = _binding(revision_id="revision-other")

    _assert_error(
        "BINDING_MISMATCH",
        lambda: service.resolve(
            principal=PRINCIPAL,
            host_session_id=bootstrap["host_session_id"],
        ),
    )


def test_issue_rejects_a_workflow_not_pinned_to_the_current_session(host_service) -> None:
    service, _, resolvers, _ = host_service
    resolvers.workflow = _binding(session_id="office-session-b")

    _assert_error("SESSION_MISMATCH", lambda: _issue(service))


def test_invalid_input_and_resolver_failures_use_fixed_safe_errors(host_service) -> None:
    service, _, _, _ = host_service

    _assert_error(
        "INVALID_REQUEST",
        lambda: _issue(service, persistence_policy="durable"),
    )

    unavailable = WorkbookEditHostSessionService(
        repository=InMemoryWorkbookEditHostSessionRepository(),
        resolve_current_binding=lambda principal, session_id: _binding(),
        resolve_workflow_binding=lambda principal, workflow_id: (_ for _ in ()).throw(
            RuntimeError("sensitive provider detail")
        ),
        now=lambda: NOW,
    )
    error = _assert_error("SERVICE_UNAVAILABLE", lambda: _issue(unavailable))
    assert "sensitive" not in error.message


def test_in_memory_repository_supports_concurrent_unique_issue(host_service) -> None:
    service, _, _, _ = host_service
    with ThreadPoolExecutor(max_workers=8) as executor:
        bootstraps = list(executor.map(lambda _index: _issue(service), range(32)))

    identifiers = {item["host_session_id"] for item in bootstraps}
    assert len(identifiers) == 32
