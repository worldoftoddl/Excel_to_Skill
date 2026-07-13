from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from excel_to_skill.audit.service import (
    AuditConversationService,
    AuditConversationServiceError,
    BundleSnapshot,
    BundleSnapshotNotFoundError,
    ConversationArtifactRepositoryError,
    ConversationTurnCommand,
    InMemoryConversationArtifactRepository,
    NoopTurnLock,
    ServicePrincipal,
)


class _Bundles:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.calls = []

    def resolve(self, *, principal, bundle_id):
        self.calls.append((principal, bundle_id))
        try:
            return self.snapshots[(principal.scope, bundle_id)]
        except KeyError as exc:
            raise BundleSnapshotNotFoundError(bundle_id) from exc


class _Lock:
    def __init__(self):
        self.calls = []

    @contextmanager
    def hold(self, *, principal, thread_id):
        self.calls.append((principal.scope, thread_id))
        yield


class _PublishFailingArtifacts(InMemoryConversationArtifactRepository):
    def publish(self, **kwargs):
        del kwargs
        raise ConversationArtifactRepositoryError("simulated publish failure")


def _result(thread_id: str) -> dict:
    return {
        "schema_version": "audit_conversation_turn_result.v1",
        "thread_id": thread_id,
        "turn_index": 1,
        "resumed": False,
        "bundle": {
            "scope": {"kind": "workbook"},
            "workbook_sha256": "1" * 64,
            "prepare_version": "test",
            "facts_key": "2" * 64,
            "standards_key": "3" * 64,
            "brief_key": "4" * 64,
            "prepared_at": "2026-07-14T00:00:00Z",
            "standards_corpus_version": "test-corpus",
        },
        "response": {"schema_version": "audit_agent_response.v2"},
        "usage": {
            "requests": [],
            "request_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


@pytest.fixture
def principal():
    return ServicePrincipal(tenant_id="tenant-a", subject_id="user-a")


@pytest.fixture
def snapshot(tmp_path):
    package = tmp_path / "server" / "packages" / "snapshot-a"
    package.mkdir(parents=True)
    return BundleSnapshot(
        bundle_id="bundle-a",
        snapshot_id="a" * 64,
        package_path=package,
        runtime_root=tmp_path / "server" / "runtime" / "snapshot-a",
    )


def _service(*, principal, snapshot, runner, artifacts=None, turn_lock=None, **kwargs):
    bundles = _Bundles({(principal.scope, snapshot.bundle_id): snapshot})
    service = AuditConversationService(
        bundles=bundles,
        artifacts=artifacts or InMemoryConversationArtifactRepository(),
        model="test-model",
        runner=runner,
        turn_lock=turn_lock,
        **kwargs,
    )
    return service, bundles


def test_service_resolves_opaque_bundle_and_delegates_server_paths(
    principal,
    snapshot,
):
    calls = []
    lock = _Lock()
    client = object()
    client_factory = object()
    checkpointer = object()
    retriever = object()
    retriever_factory = object()

    def runner(pkg, **kwargs):
        calls.append((pkg, kwargs))
        return _result(kwargs["thread_id"])

    service, bundles = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
        turn_lock=lock,
        client=client,
        client_factory=client_factory,
        checkpointer=checkpointer,
        standards_retriever=retriever,
        standards_retriever_factory=retriever_factory,
    )
    command = ConversationTurnCommand(
        bundle_id="bundle-a",
        question="핵심 위험은?",
        thread_id="web-thread-1",
        sheet="매출채권",
        standards_research=True,
        procedure_planning=True,
        workbook_inspection=True,
    )

    submission = service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="request-key-1",
    )

    assert submission.replayed is False
    assert bundles.calls == [(principal, "bundle-a")]
    assert len(calls) == 1
    package, arguments = calls[0]
    assert package == snapshot.package_path
    runtime_thread_id = arguments["thread_id"]
    assert runtime_thread_id.startswith("runtime-thread-")
    assert runtime_thread_id != "web-thread-1"
    assert arguments == {
        "model": "test-model",
        "question": "핵심 위험은?",
        "thread_id": runtime_thread_id,
        "sheet": "매출채권",
        "aggregate_id": None,
        "limit": 100,
        "max_steps": 6,
        "client": client,
        "client_factory": client_factory,
        "standards_research": True,
        "procedure_planning": True,
        "workbook_inspection": True,
        "workbook_source_provider": None,
        "standards_retriever": retriever,
        "standards_retriever_factory": retriever_factory,
        "checkpointer": checkpointer,
        "runtime_root": snapshot.runtime_root,
        "eprint": None,
    }
    assert lock.calls == [(principal.scope, runtime_thread_id)]
    document = submission.receipt.to_dict()
    serialized = json.dumps(document, ensure_ascii=False)
    assert document["thread_id"] == "web-thread-1"
    assert document["result"]["thread_id"] == "web-thread-1"
    assert runtime_thread_id not in serialized
    assert str(snapshot.package_path) not in serialized
    assert str(snapshot.runtime_root) not in serialized
    assert set(document) == {
        "schema_version",
        "request_id",
        "bundle_id",
        "snapshot_id",
        "thread_id",
        "result",
    }
    assert service.get_turn(
        principal=principal,
        request_id=submission.receipt.request_id,
    ).to_dict() == document


def test_server_owned_workbook_provider_is_injected_but_never_serialized(
    principal,
    snapshot,
):
    provider = object()
    source_snapshot = BundleSnapshot(
        bundle_id=snapshot.bundle_id,
        snapshot_id=snapshot.snapshot_id,
        package_path=snapshot.package_path,
        runtime_root=snapshot.runtime_root,
        workbook_source_provider=provider,
    )
    received = []

    def runner(pkg, **kwargs):
        received.append(kwargs["workbook_source_provider"])
        return _result(kwargs["thread_id"])

    service, _ = _service(
        principal=principal,
        snapshot=source_snapshot,
        runner=runner,
    )
    submitted = service.submit_turn(
        principal=principal,
        command=ConversationTurnCommand(
            bundle_id="bundle-a",
            question="범위를 다시 검사해줘",
            workbook_inspection=True,
        ),
        idempotency_key="inspection-key",
    )

    assert received == [provider]
    assert "workbook_source_provider" not in repr(source_snapshot)
    assert "workbook_source_provider" not in json.dumps(submitted.receipt.to_dict())


def test_runtime_thread_is_deterministic_and_principal_namespaced_without_public_leak(
    principal,
    snapshot,
):
    other_principal = ServicePrincipal(tenant_id="tenant-a", subject_id="user-b")
    bundles = _Bundles(
        {
            (principal.scope, snapshot.bundle_id): snapshot,
            (other_principal.scope, snapshot.bundle_id): snapshot,
        }
    )
    runtime_threads = []

    def runner(pkg, **kwargs):
        runtime_threads.append(kwargs["thread_id"])
        return _result(kwargs["thread_id"])

    service = AuditConversationService(
        bundles=bundles,
        artifacts=InMemoryConversationArtifactRepository(),
        model="test-model",
        runner=runner,
    )
    receipts = []
    for active_principal, key, question in (
        (principal, "principal-a-first", "질문 1"),
        (other_principal, "principal-b", "질문 2"),
        (principal, "principal-a-second", "질문 3"),
    ):
        submission = service.submit_turn(
            principal=active_principal,
            command=ConversationTurnCommand(
                bundle_id="bundle-a",
                question=question,
                thread_id="shared-public-thread",
            ),
            idempotency_key=key,
        )
        receipts.append(submission.receipt.to_dict())

    assert runtime_threads[0] == runtime_threads[2]
    assert runtime_threads[0] != runtime_threads[1]
    assert all(item.startswith("runtime-thread-") for item in runtime_threads)
    for document in receipts:
        serialized = json.dumps(document, ensure_ascii=False)
        assert document["thread_id"] == "shared-public-thread"
        assert document["result"]["thread_id"] == "shared-public-thread"
        assert all(runtime_thread not in serialized for runtime_thread in runtime_threads)


def test_service_replays_same_idempotency_key_without_resolving_or_rerunning(
    principal,
    snapshot,
):
    calls = []

    def runner(pkg, **kwargs):
        calls.append((pkg, kwargs))
        return _result(kwargs["thread_id"])

    service, bundles = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
    )
    command = ConversationTurnCommand(bundle_id="bundle-a", question="질문")

    first = service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="stable-key",
    )
    second = service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="stable-key",
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.receipt.to_dict() == first.receipt.to_dict()
    assert len(calls) == 1
    assert len(bundles.calls) == 1
    assert first.receipt.thread_id.startswith("thread-")


def test_service_atomic_claim_rejects_concurrent_same_key_and_then_replays(
    principal,
    snapshot,
):
    entered = threading.Event()
    second_entry = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = 0

    def runner(pkg, **kwargs):
        nonlocal calls
        with guard:
            calls += 1
            if calls > 1:
                second_entry.set()
        entered.set()
        assert release.wait(timeout=5)
        return _result(kwargs["thread_id"])

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)
    command = ConversationTurnCommand(bundle_id="bundle-a", question="질문")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            service.submit_turn,
            principal=principal,
            command=command,
            idempotency_key="concurrent-key",
        )
        assert entered.wait(timeout=5)
        second_future = executor.submit(
            service.submit_turn,
            principal=principal,
            command=command,
            idempotency_key="concurrent-key",
        )
        assert not second_entry.wait(timeout=0.2)
        with pytest.raises(AuditConversationServiceError) as caught:
            second_future.result(timeout=5)
        assert caught.value.code == "TURN_IN_PROGRESS"
        assert caught.value.status_code == 409
        release.set()
        first = first_future.result(timeout=5)

    replay = service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="concurrent-key",
    )

    assert calls == 1
    assert first.replayed is False
    assert replay.replayed is True
    assert first.receipt.to_dict() == replay.receipt.to_dict()


def test_shared_repository_claim_prevents_duplicate_turn_across_service_workers(
    principal,
    snapshot,
):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def runner(pkg, **kwargs):
        calls.append(kwargs["thread_id"])
        entered.set()
        assert release.wait(timeout=5)
        return _result(kwargs["thread_id"])

    artifacts = InMemoryConversationArtifactRepository()
    first_service, _ = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
        artifacts=artifacts,
        turn_lock=NoopTurnLock(),
    )
    second_service, _ = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
        artifacts=artifacts,
        turn_lock=NoopTurnLock(),
    )
    command = ConversationTurnCommand(
        bundle_id="bundle-a",
        question="질문",
        thread_id="shared-thread",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            first_service.submit_turn,
            principal=principal,
            command=command,
            idempotency_key="distributed-key",
        )
        assert entered.wait(timeout=5)
        with pytest.raises(AuditConversationServiceError) as caught:
            second_service.submit_turn(
                principal=principal,
                command=command,
                idempotency_key="distributed-key",
            )
        assert caught.value.code == "TURN_IN_PROGRESS"
        assert caught.value.status_code == 409

        with pytest.raises(AuditConversationServiceError) as conflict:
            second_service.submit_turn(
                principal=principal,
                command=ConversationTurnCommand(
                    bundle_id="bundle-a",
                    question="다른 질문",
                    thread_id="shared-thread",
                ),
                idempotency_key="distributed-key",
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        release.set()
        first = first_future.result(timeout=5)

    replay = second_service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="distributed-key",
    )
    assert len(calls) == 1
    assert replay.replayed is True
    assert replay.receipt.to_dict() == first.receipt.to_dict()


def test_pre_runtime_failure_aborts_claim_and_allows_same_key_retry(
    principal,
    snapshot,
):
    bundles = _Bundles({})
    artifacts = InMemoryConversationArtifactRepository()
    calls = []

    def runner(pkg, **kwargs):
        calls.append(kwargs["thread_id"])
        return _result(kwargs["thread_id"])

    service = AuditConversationService(
        bundles=bundles,
        artifacts=artifacts,
        model="test-model",
        runner=runner,
    )
    command = ConversationTurnCommand(bundle_id="bundle-a", question="질문")

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=command,
            idempotency_key="retryable-key",
        )
    assert caught.value.code == "BUNDLE_NOT_FOUND"

    bundles.snapshots[(principal.scope, snapshot.bundle_id)] = snapshot
    completed = service.submit_turn(
        principal=principal,
        command=command,
        idempotency_key="retryable-key",
    )
    assert completed.replayed is False
    assert len(calls) == 1


def test_publish_failure_keeps_claim_pending_and_prevents_runtime_reexecution(
    principal,
    snapshot,
):
    calls = []

    def runner(pkg, **kwargs):
        calls.append(kwargs["thread_id"])
        return _result(kwargs["thread_id"])

    service, _ = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
        artifacts=_PublishFailingArtifacts(),
    )
    command = ConversationTurnCommand(bundle_id="bundle-a", question="질문")

    with pytest.raises(AuditConversationServiceError) as failed:
        service.submit_turn(
            principal=principal,
            command=command,
            idempotency_key="publish-failure-key",
        )
    assert failed.value.code == "SERVICE_STORAGE_UNAVAILABLE"

    with pytest.raises(AuditConversationServiceError) as pending:
        service.submit_turn(
            principal=principal,
            command=command,
            idempotency_key="publish-failure-key",
        )
    assert pending.value.code == "TURN_IN_PROGRESS"
    assert pending.value.status_code == 409
    assert len(calls) == 1


def test_repository_abort_releases_only_the_matching_pending_owner(principal):
    artifacts = InMemoryConversationArtifactRepository()
    arguments = {
        "principal": principal,
        "idempotency_key": "abortable-key",
        "command_sha256": "a" * 64,
        "request_id": "turn-abortable",
    }
    first = artifacts.claim(**arguments, claim_token="owner-token")
    assert first.state == "claimed"

    with pytest.raises(ConversationArtifactRepositoryError):
        artifacts.abort(**arguments, claim_token="another-owner")
    assert artifacts.claim(**arguments, claim_token="third-owner").state == "pending"

    artifacts.abort(**arguments, claim_token="owner-token")
    reclaimed = artifacts.claim(**arguments, claim_token="replacement-owner")
    assert reclaimed.state == "claimed"
    assert reclaimed.claim_token == "replacement-owner"


def test_service_rejects_idempotency_key_reuse_for_different_command(
    principal,
    snapshot,
):
    def runner(pkg, **kwargs):
        return _result(kwargs["thread_id"])

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)
    service.submit_turn(
        principal=principal,
        command=ConversationTurnCommand(bundle_id="bundle-a", question="질문 1"),
        idempotency_key="same-key",
    )

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(bundle_id="bundle-a", question="질문 2"),
            idempotency_key="same-key",
        )

    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert caught.value.status_code == 409


def test_service_binds_explicit_thread_to_exact_snapshot_and_scope(
    principal,
    snapshot,
):
    def runner(pkg, **kwargs):
        return _result(kwargs["thread_id"])

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)
    service.submit_turn(
        principal=principal,
        command=ConversationTurnCommand(
            bundle_id="bundle-a",
            question="질문 1",
            thread_id="thread-a",
            sheet="A",
        ),
        idempotency_key="key-1",
    )

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(
                bundle_id="bundle-a",
                question="질문 2",
                thread_id="thread-a",
                sheet="B",
            ),
            idempotency_key="key-2",
        )

    assert caught.value.code == "THREAD_BUNDLE_CONFLICT"
    assert caught.value.status_code == 409


def test_service_scopes_receipts_to_injected_principal(principal, snapshot):
    def runner(pkg, **kwargs):
        return _result(kwargs["thread_id"])

    artifacts = InMemoryConversationArtifactRepository()
    service, _ = _service(
        principal=principal,
        snapshot=snapshot,
        runner=runner,
        artifacts=artifacts,
    )
    submitted = service.submit_turn(
        principal=principal,
        command=ConversationTurnCommand(bundle_id="bundle-a", question="질문"),
        idempotency_key="key",
    )

    with pytest.raises(AuditConversationServiceError) as caught:
        service.get_turn(
            principal=ServicePrincipal(tenant_id="tenant-a", subject_id="user-b"),
            request_id=submitted.receipt.request_id,
        )

    assert caught.value.code == "TURN_NOT_FOUND"


@pytest.mark.parametrize("field", ["path", "package_path", "runtime_root", "source_path"])
def test_service_blocks_path_fields_in_runner_result(principal, snapshot, field):
    def runner(pkg, **kwargs):
        result = _result(kwargs["thread_id"])
        result["response"][field] = "/private/server/path"
        return result

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(bundle_id="bundle-a", question="질문"),
            idempotency_key=f"key-{field}",
        )

    assert caught.value.code == "PATH_DISCLOSURE_BLOCKED"


def test_service_blocks_nested_runtime_thread_identifier_from_public_result(
    principal,
    snapshot,
):
    internal_ids = []

    def runner(pkg, **kwargs):
        internal_ids.append(kwargs["thread_id"])
        result = _result(kwargs["thread_id"])
        result["response"]["debug"] = {"thread": kwargs["thread_id"]}
        return result

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(
                bundle_id="bundle-a",
                question="질문",
                thread_id="public-thread",
            ),
            idempotency_key="internal-leak-key",
        )

    assert caught.value.code == "INTERNAL_IDENTIFIER_DISCLOSURE_BLOCKED"
    assert caught.value.status_code == 500
    assert len(internal_ids) == 1
    assert internal_ids[0] not in caught.value.message


def test_service_rejects_inconsistent_usage_metadata(principal, snapshot):
    def runner(pkg, **kwargs):
        result = _result(kwargs["thread_id"])
        result["usage"]["request_count"] = 1
        return result

    service, _ = _service(principal=principal, snapshot=snapshot, runner=runner)

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(bundle_id="bundle-a", question="질문"),
            idempotency_key="bad-usage-key",
        )

    assert caught.value.code == "INVALID_TURN_RESULT"


def test_bundle_snapshot_requires_absolute_server_paths(tmp_path):
    with pytest.raises(ValueError, match="absolute server-owned"):
        BundleSnapshot(
            bundle_id="bundle-a",
            snapshot_id="a" * 64,
            package_path=tmp_path.relative_to(tmp_path),
            runtime_root=tmp_path / "runtime",
        )


def test_service_reports_missing_bundle_without_exposing_repository_detail(
    principal,
    snapshot,
):
    bundles = _Bundles({})
    service = AuditConversationService(
        bundles=bundles,
        artifacts=InMemoryConversationArtifactRepository(),
        model="test-model",
        runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
    )

    with pytest.raises(AuditConversationServiceError) as caught:
        service.submit_turn(
            principal=principal,
            command=ConversationTurnCommand(bundle_id="missing", question="질문"),
            idempotency_key="key",
        )

    assert caught.value.code == "BUNDLE_NOT_FOUND"
    assert caught.value.status_code == 404
    assert "missing" not in caught.value.message
