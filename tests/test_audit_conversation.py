"""Compiled audit conversation workflow, persistence, and trust-boundary tests."""
from __future__ import annotations

import copy
import json
import sqlite3
import stat
import subprocess
import sys
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import pytest
import jsonschema

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

import excel_to_skill.audit.agent as agent_module
import excel_to_skill.audit.conversation as conversation_module
from excel_to_skill.audit.contract import bundle_keys
from excel_to_skill.audit.conversation import (
    AuditConversationBundleChangedError,
    AuditConversationError,
    run_audit_conversation_turn,
)
from excel_to_skill.audit.llm import load_schema

from test_audit_consume_gate import _write_committed_bundle


class StubClient:
    def __init__(self, responses, *, usage_events=()) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []
        self.usage_events: list[dict] = []
        self._pending_usage_events = deque(usage_events)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if self._pending_usage_events:
            self.usage_events.append(self._pending_usage_events.popleft())
        return response


def _selection(kind: str, *ids: str) -> dict:
    return {"kind": kind, "ids": list(ids)}


def _final(*selections: dict) -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": False,
            "abstention_code": None,
            "selections": list(selections),
        },
    }


def _abstained() -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": True,
            "abstention_code": "insufficient_evidence",
            "selections": [],
        },
    }


def _tool(
    name: str,
    *,
    query: str | None = None,
    kind: str | None = None,
    item_id: str | None = None,
    limit: int = 20,
) -> dict:
    return {
        "action": "tool",
        "tool": {
            "name": name,
            "query": query,
            "kind": kind,
            "item_id": item_id,
            "limit": limit,
        },
        "final": None,
    }


def _run(
    pkg: Path,
    root: Path,
    saver,
    client,
    *,
    thread: str = "thread-a",
    question: str = "핵심 미비점은?",
    max_steps: int = 6,
) -> dict:
    return run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question=question,
        thread_id=thread,
        max_steps=max_steps,
        client=client,
        checkpointer=saver,
        runtime_root=root,
    )


def _artifact_documents(root: Path, *, kind: str | None = None) -> list[tuple[Path, dict]]:
    result: list[tuple[Path, dict]] = []
    for path in root.glob("threads/*/objects/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if kind is None or document.get("kind") == kind:
            result.append((path, document))
    return result


def _recommit_changed_brief(pkg: Path) -> None:
    facts_path = pkg / "data/audit_facts.json"
    context_path = pkg / "data/standards_context.json"
    brief_path = pkg / "data/audit_brief.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    brief["summary"]["text"] += " 새 commit"
    brief_path.write_text(
        json.dumps(brief, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    keys = bundle_keys(facts, context, brief)
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for field, value in zip(
        ("facts_key", "standards_key", "brief_key"), keys, strict=True
    ):
        meta["audit_preparation"][field] = value
    meta["audit_preparation"]["prepared_at"] = "2026-07-13T00:00:00Z"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_checkpoint_control_state_must_match_private_observation_replay(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    runtime, blocked = agent_module._prepare_audit_agent_runtime(
        pkg,
        model="stub-model",
        question="핵심 미비점은?",
    )
    assert runtime is not None and blocked is None
    turn_state = agent_module._new_audit_agent_turn_state(runtime)
    root = tmp_path / "runtime"
    store = conversation_module.ConversationArtifactStore(root)
    invocation_id = "checkpoint-replay"
    observations_ref = store.write(
        "thread-a",
        kind="observations",
        schema_version=conversation_module.OBSERVATIONS_SCHEMA,
        payload={
            "invocation_id": invocation_id,
            "value": turn_state.observations,
        },
    )
    context = conversation_module.AuditConversationRuntime(
        thread_id="thread-a",
        package_path=pkg,
        sheet=None,
        store=store,
        agent=runtime,
        blocked_response=None,
    )
    state = {
        "invocation_id": invocation_id,
        "history_ref": None,
        "observations_ref": observations_ref,
        "observed_ids": {
            kind: sorted(values) for kind, values in turn_state.observed.items()
        },
        "used_tools": list(turn_state.used_tools),
        "discovery_complete": turn_state.discovery_complete,
    }

    restored = conversation_module._turn_state(context, state)
    assert restored.observed == turn_state.observed
    assert restored.seen_tool_requests == set()

    tampered_observed = copy.deepcopy(state)
    citation_id = next(iter(runtime.known["standard_citation"]))
    tampered_observed["observed_ids"]["standard_citation"] = [citation_id]
    with pytest.raises(AuditConversationError, match="observed_ids"):
        conversation_module._turn_state(context, tampered_observed)

    tampered_tools = copy.deepcopy(state)
    tampered_tools["used_tools"].append("trace")
    with pytest.raises(AuditConversationError, match="used_tools"):
        conversation_module._turn_state(context, tampered_tools)

    tampered_discovery = copy.deepcopy(state)
    tampered_discovery["discovery_complete"] = not turn_state.discovery_complete
    with pytest.raises(AuditConversationError, match="discovery_complete"):
        conversation_module._turn_state(context, tampered_discovery)


def test_conversation_result_schema_is_packaged_and_strict() -> None:
    schema = load_schema("audit_conversation_turn_result.schema.json")
    assert schema["title"] == "audit_conversation_turn_result.v1"
    assert schema["additionalProperties"] is False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "schema_version": "audit_conversation_turn_result.v1",
                "unexpected": True,
            },
            schema,
        )


def test_graph_routes_tool_then_final_and_returns_unreviewed_grounded_result(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    saver = InMemorySaver()
    client = StubClient([
        _tool("trace", item_id="statement:gap"),
        _final(_selection("statement", "statement:gap")),
    ], usage_events=[
        {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
        {"input_tokens": 55, "output_tokens": 15, "total_tokens": 70},
    ])

    result = _run(pkg, tmp_path / "runtime", saver, client)

    assert result["schema_version"] == "audit_conversation_turn_result.v1"
    assert result["turn_index"] == 1 and result["resumed"] is False
    assert result["response"]["trust"]["answer_review_status"] == "unreviewed"
    assert result["response"]["answer"]["claims"][0]["statement_ids"] == [
        "statement:gap"
    ]
    assert result["response"]["coverage"]["evidence_complete"] is True
    assert len(client.calls) == 2
    first_payload = json.loads(client.calls[0]["user"])
    assert first_payload["selection_contract"] == {
        "linked_evidence_hydrated_after_final": True,
        "select_linked_ids_only_if_observed_and_independently_needed": True,
        "must_finalize": False,
    }
    assert "Selecting an observed brief statement is sufficient for provenance" in (
        client.calls[0]["system"]
    )
    assert any(
        observation.get("tool") == "trace"
        for observation in json.loads(client.calls[1]["user"])["observations"]
    )
    assert result["usage"]["request_count"] == 2
    assert result["usage"]["input_tokens"] == 95
    assert result["usage"]["output_tokens"] == 25
    assert result["usage"]["total_tokens"] == 120
    assert [item["event_id"] for item in result["usage"]["requests"]] == [
        "request:1",
        "request:2",
    ]


def test_last_model_call_is_explicitly_reserved_for_a_grounded_final(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    client = StubClient([_final(_selection("statement", "statement:fact"))])

    result = _run(
        pkg,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
        max_steps=1,
    )

    payload = json.loads(client.calls[0]["user"])
    assert payload["remaining_model_calls"] == 0
    assert payload["selection_contract"]["must_finalize"] is True
    provider_schema = client.calls[0]["schema"]
    assert provider_schema["properties"]["action"]["enum"] == ["final"]
    assert provider_schema["properties"]["tool"] == {"type": "null"}
    assert provider_schema["properties"]["final"]["type"] == "object"
    assert result["response"]["answer"]["abstained"] is False


def test_last_model_call_rejects_a_tool_without_executing_it(tmp_path: Path) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    client = StubClient([_tool("audit_search", query="위험", kind="risk")])
    runtime_root = tmp_path / "runtime"

    with pytest.raises(AuditConversationError, match="1회 안에"):
        _run(
            pkg,
            runtime_root,
            InMemorySaver(),
            client,
            max_steps=1,
        )

    assert len(client.calls) == 1
    observations = _artifact_documents(runtime_root, kind="observations")
    assert all(
        not any(item.get("tool") == "audit_search" for item in document["payload"]["value"])
        for _, document in observations
    )


def test_unobserved_linked_id_feedback_can_retry_with_statement_only(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    client = StubClient([
        _final(
            _selection("statement", "statement:synthesis"),
            _selection("standard_citation", "citation:1"),
        ),
        _final(_selection("statement", "statement:synthesis")),
    ])

    result = _run(
        pkg,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
        max_steps=2,
    )

    retry_payload = json.loads(client.calls[1]["user"])
    validation = next(
        item["result"]["error"]
        for item in retry_payload["observations"]
        if item.get("tool") == "answer_validation"
    )
    assert validation["code"] == "UNGROUNDED_FINAL"
    assert "unobserved standard_citation" in validation["message"][0]
    assert not any(
        item.get("tool") == "trace"
        for item in retry_payload["observations"]
    )
    assert [item["fact_id"] for item in result["response"]["evidence"]["facts"]] == [
        "fact:risk"
    ]
    assert [
        item["citation_id"]
        for item in result["response"]["evidence"]["standards"]
    ] == ["citation:1"]
    assert result["response"]["coverage"]["evidence_complete"] is True


def test_conversation_serializes_each_thread_with_common_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    locked: list[Path] = []

    @contextmanager
    def fake_lock(path):
        locked.append(Path(path))
        yield

    monkeypatch.setattr(conversation_module.cache, "package_lock", fake_lock)
    _run(
        pkg,
        tmp_path / "runtime",
        InMemorySaver(),
        StubClient([_abstained()]),
        thread="sensitive-thread-name",
    )

    assert len(locked) == 2
    assert locked[0].parent.name == "threads"
    assert locked[0].name != "sensitive-thread-name"
    assert len(locked[0].name) == 64
    assert locked[1] == pkg


def test_same_thread_resumes_with_typed_focus_while_other_thread_is_isolated(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    first = _run(
        pkg,
        root,
        saver,
        StubClient([_final(_selection("statement", "statement:fact"))]),
        question="첫 질문",
    )
    resumed_client = StubClient([
        _tool("audit_get", item_id="fact:risk"),
        _final(_selection("fact", "fact:risk")),
    ])

    second = _run(
        pkg,
        root,
        saver,
        resumed_client,
        question="그 위험을 다시 설명해줘",
    )
    isolated = _run(
        pkg,
        root,
        saver,
        StubClient([_abstained()]),
        thread="thread-b",
        question="별도 대화",
    )

    assert first["turn_index"] == 1 and first["resumed"] is False
    assert second["turn_index"] == 2 and second["resumed"] is True
    assert isolated["turn_index"] == 1 and isolated["resumed"] is False
    focus = next(
        item
        for item in json.loads(resumed_client.calls[0]["user"])["observations"]
        if item.get("tool") == "conversation_focus"
    )
    assert focus["result"]["prior_turns"][0]["question"] == "첫 질문"
    assert {
        (record["kind"], record["item"]["id"])
        for record in focus["result"]["records"]
    } >= {("statement", "statement:fact"), ("fact", "fact:risk")}
    assert "INVALID_TOOL_REQUEST" not in resumed_client.calls[1]["user"]


def test_resuming_thread_after_valid_bundle_change_fails_before_model(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    _run(pkg, root, saver, StubClient([_abstained()]))
    _recommit_changed_brief(pkg)
    called = False

    def factory():
        nonlocal called
        called = True
        return StubClient([_abstained()])

    with pytest.raises(AuditConversationBundleChangedError, match="다른 audit bundle"):
        run_audit_conversation_turn(
            pkg,
            model="stub-model",
            question="변경 후 질문",
            thread_id="thread-a",
            client_factory=factory,
            checkpointer=saver,
            runtime_root=root,
        )
    assert called is False


def test_bundle_change_after_finalization_fails_before_turn_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    original_write = conversation_module.ConversationArtifactStore.write
    changed = False

    def write_then_change_bundle(self, *args, **kwargs):
        nonlocal changed
        ref = original_write(self, *args, **kwargs)
        if kwargs.get("kind") == "response" and not changed:
            changed = True
            _recommit_changed_brief(pkg)
        return ref

    monkeypatch.setattr(
        conversation_module.ConversationArtifactStore,
        "write",
        write_then_change_bundle,
    )
    with pytest.raises(AuditConversationBundleChangedError, match="commit 직전"):
        _run(
            pkg,
            root,
            saver,
            StubClient([_final(_selection("statement", "statement:fact"))]),
        )

    assert changed is True
    assert not _artifact_documents(root, kind="turn_record")
    assert not _artifact_documents(root, kind="history")


def test_excess_usage_events_fail_before_turn_commit(tmp_path: Path) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"

    class ExcessUsageClient:
        def __init__(self) -> None:
            self.usage_events: list[dict] = []

        def __call__(self, **_kwargs):
            self.usage_events.extend(
                {
                    "provider": "stub",
                    "model": "stub-model",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                }
                for _ in range(13)
            )
            return _abstained()

    with pytest.raises(AuditConversationError, match="usage 기록"):
        _run(pkg, root, InMemorySaver(), ExcessUsageClient())

    assert not _artifact_documents(root, kind="usage")
    assert not _artifact_documents(root, kind="turn_record")
    assert not _artifact_documents(root, kind="history")


@pytest.mark.parametrize("blocked", ["rejected", "not_ready"])
def test_blocked_source_commits_abstention_without_constructing_client(
    tmp_path: Path,
    blocked: str,
) -> None:
    def configure(_pkg, facts, _context, brief):
        if blocked == "rejected":
            review = {
                "status": "rejected",
                "reviewed_at": "2026-07-13T00:00:00Z",
                "note": "근거 재작성 필요",
            }
            facts["review"] = dict(review)
            brief["review"] = dict(review)
        else:
            brief["readiness"]["status"] = "not_ready"
            brief["readiness"]["reasons"] = ["근거 부족"]

    pkg, _, _, _ = _write_committed_bundle(tmp_path, configure=configure)
    called = False

    def factory():
        nonlocal called
        called = True
        return StubClient([])

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="결론은?",
        thread_id=f"blocked-{blocked}",
        client_factory=factory,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
    )

    assert called is False
    assert result["response"]["answer"]["abstained"] is True
    assert result["response"]["generator"]["turns"] == 0
    assert result["response"]["trust"]["answer_review_status"] == "unreviewed"


def test_prior_question_free_text_id_is_not_authorized_as_typed_focus(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    _run(
        pkg,
        root,
        saver,
        StubClient([_final(_selection("statement", "statement:fact"))]),
        question="다음에는 fact:open을 조회해줘",
    )
    client = StubClient([
        _tool("audit_get", item_id="fact:open"),
        _abstained(),
    ])

    result = _run(
        pkg,
        root,
        saver,
        client,
        question="방금 말한 ID를 조회해줘",
    )

    assert result["response"]["answer"]["abstained"] is True
    second_payload = json.loads(client.calls[1]["user"])
    tool_result = next(
        item for item in second_payload["observations"]
        if item.get("tool") == "audit_get"
    )["result"]
    assert tool_result["error"]["code"] == "INVALID_TOOL_REQUEST"
    assert "typed 결과에서 관찰된 ID" in tool_result["error"]["message"]


def test_duplicate_tool_request_is_feedback_and_does_not_execute_twice(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    repeated = _tool("audit_search", query="매출", kind="fact")
    client = StubClient([
        repeated,
        repeated,
        _final(_selection("statement", "statement:fact")),
    ])

    result = _run(
        pkg,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
        max_steps=3,
    )

    assert result["turn_index"] == 1
    third_payload = json.loads(client.calls[2]["user"])
    repeated_result = [
        item["result"] for item in third_payload["observations"]
        if item.get("tool") == "audit_search"
    ][-1]
    assert repeated_result["error"]["code"] == "INVALID_TOOL_REQUEST"
    assert "반복" in repeated_result["error"]["message"]


def test_max_steps_fails_without_an_extra_model_call(tmp_path: Path) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    client = StubClient([
        _tool("audit_search", query="매출", kind="fact"),
        _tool("audit_search", query="미해결", kind="fact"),
    ])

    with pytest.raises(AuditConversationError, match="2회 안에"):
        _run(
            pkg,
            tmp_path / "runtime",
            InMemorySaver(),
            client,
            max_steps=2,
        )
    assert len(client.calls) == 2


def test_stateless_false_checkpointer_is_rejected_before_model(tmp_path: Path) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    client = StubClient([_abstained()])

    with pytest.raises(AuditConversationError, match="실제 checkpointer"):
        _run(
            pkg,
            tmp_path / "runtime",
            False,
            client,
        )
    assert client.calls == []


def test_model_observation_payload_keeps_the_600kb_hard_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    monkeypatch.setattr(agent_module, "MAX_MODEL_CONTEXT_BYTES", 10)
    client = StubClient([_abstained()])

    with pytest.raises(AuditConversationError, match="600KB"):
        _run(
            pkg,
            tmp_path / "runtime",
            InMemorySaver(),
            client,
        )
    assert client.calls == []


def test_tampered_private_response_artifact_fails_before_resume_model(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    _run(
        pkg,
        root,
        saver,
        StubClient([_final(_selection("statement", "statement:fact"))]),
    )
    [(response_path, document)] = _artifact_documents(root, kind="response")
    document["payload"]["value"]["answer"]["title"] = "변조된 답변"
    response_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    called = False

    def factory():
        nonlocal called
        called = True
        return StubClient([_abstained()])

    with pytest.raises(AuditConversationError, match="digest"):
        run_audit_conversation_turn(
            pkg,
            model="stub-model",
            question="재개 질문",
            thread_id="thread-a",
            client_factory=factory,
            checkpointer=saver,
            runtime_root=root,
        )
    assert called is False


def test_graph_result_refs_must_match_committed_turn_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    original = conversation_module._invoke_graph

    def tampered_result(**kwargs):
        result = original(**kwargs)
        return {**result, "turn_index": result["turn_index"] + 1}

    monkeypatch.setattr(conversation_module, "_invoke_graph", tampered_result)
    with pytest.raises(AuditConversationError, match="history 길이"):
        _run(
            pkg,
            tmp_path / "runtime",
            InMemorySaver(),
            StubClient([_abstained()]),
        )


def test_failed_node_persists_only_fixed_error_code_not_provider_text(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    sentinel = "PROVIDER-ERROR-RAW-SECRET-XYZ"
    question_sentinel = "QUESTION-FAILURE-LEAK-SENTINEL"

    class ExplodingClient:
        def __call__(self, **_kwargs):
            raise RuntimeError(sentinel)

    with pytest.raises(
        AuditConversationError,
        match="모델 호출 또는 구조화 응답 검증",
    ):
        run_audit_conversation_turn(
            pkg,
            model="stub-model",
            question=question_sentinel,
            thread_id="failed-node",
            client=ExplodingClient(),
            runtime_root=root,
        )

    checkpoint_files = [
        path
        for path in (
            root / "checkpoints.sqlite3",
            root / "checkpoints.sqlite3-wal",
            root / "checkpoints.sqlite3-shm",
        )
        if path.is_file()
    ]
    assert checkpoint_files
    for forbidden in (sentinel, question_sentinel):
        assert all(
            forbidden.encode("utf-8") not in path.read_bytes()
            for path in checkpoint_files
        )
    with sqlite3.connect(root / "checkpoints.sqlite3") as connection:
        errors = connection.execute(
            "SELECT value FROM writes WHERE channel = '__error__'"
        ).fetchall()
    assert errors
    assert all(sentinel.encode("utf-8") not in row[0] for row in errors)
    assert any(b"AUDIT_CHAT_NODE_FAILED:decide" in row[0] for row in errors)


def test_default_sqlite_saver_resumes_after_reopen_without_sensitive_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    secret = "sk-ant-DB-LEAK-SENTINEL"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    first_question = "QUESTION-LEAK-SENTINEL 첫 질문"
    thread_id = "CLIENT-NAME-SENSITIVE-THREAD"
    first = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question=first_question,
        thread_id=thread_id,
        client=StubClient([_final(_selection("statement", "statement:fact"))]),
        runtime_root=root,
    )
    answer_text = first["response"]["answer"]["claims"][0]["text"]

    second = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="SECOND-QUESTION-LEAK-SENTINEL",
        thread_id=thread_id,
        client=StubClient([_abstained()]),
        runtime_root=root,
    )

    assert first["thread_id"] == thread_id
    assert second["thread_id"] == thread_id
    assert second["turn_index"] == 2 and second["resumed"] is True
    database = root / "checkpoints.sqlite3"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    checkpoint_files = [
        path
        for path in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
        )
        if path.is_file()
    ]
    for forbidden in (
        first_question,
        "SECOND-QUESTION-LEAK-SENTINEL",
        answer_text,
        "매출 위험",
        secret,
        thread_id,
    ):
        assert all(
            forbidden.encode("utf-8") not in path.read_bytes()
            for path in checkpoint_files
        )
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        serialized_types = {
            row[0]
            for row in connection.execute(
                "SELECT type FROM checkpoints UNION SELECT type FROM writes"
            )
        }
    assert {"checkpoints", "writes"} <= tables
    assert "pickle" not in serialized_types


def test_core_audit_import_does_not_import_optional_graph_runtime() -> None:
    script = (
        "import sys; import excel_to_skill.audit; "
        "assert not any(n == 'langgraph' or n.startswith('langgraph.') "
        "for n in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
