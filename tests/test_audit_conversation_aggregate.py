"""Aggregate-bound main-agent routing, provenance, and persistence tests."""
from __future__ import annotations

import copy
import json
import sqlite3
from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

import excel_to_skill.audit.conversation as conversation_module
import excel_to_skill.audit.aggregate_agent as aggregate_agent_module
from excel_to_skill.audit.aggregate_agent import (
    AuditAggregateAgentError,
    _aggregate_observation_witness,
    _evidence_for_source,
    _notices,
    _prepare_audit_aggregate_agent_runtime,
    _source_claim,
    _source_wrapper,
    _validated_aggregate_response,
)
from excel_to_skill.audit.aggregate import aggregate_audit_package
from excel_to_skill.audit.conversation import (
    AuditConversationBundleChangedError,
    AuditConversationError,
    run_audit_conversation_turn,
)
from excel_to_skill.audit.conversation_store import ConversationArtifactStore
from excel_to_skill.audit.scope import AuditScope, load_scope_bundle

from test_audit_aggregate import (
    SelectionClient,
    _FORMULA_SENTINEL,
    _RAW_STANDARD_SENTINEL,
    _package,
    _write_scope,
)


def _prepared_aggregate(tmp_path: Path):
    pkg = _package(tmp_path)
    aggregate = aggregate_audit_package(
        pkg,
        all_committed_sheets=True,
        model="aggregate-model",
        client=SelectionClient(),
        generated_at="2026-07-13T12:00:00Z",
    )
    return pkg, aggregate


def _final(kind: str, *refs: str) -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": False,
            "abstention_code": None,
            "selections": [{"kind": kind, "refs": list(refs)}],
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


class RootSelectionClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.usage_events: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["user"])
        root = payload["observations"][0]["result"]
        refs = [
            item["record_ref"] for item in root["portfolio"]["highlights"]
        ]
        self.usage_events.append({
            "provider": "stub",
            "model": "main-model",
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
        })
        return _final("aggregate_record", *refs)


class QueueClient:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []
        self.usage_events: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if callable(response):
            response = response(json.loads(kwargs["user"]))
        return response


def _run(pkg: Path, aggregate_id: str, root: Path, saver, client, **kwargs):
    return run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate_id,
        model="main-model",
        question=kwargs.pop("question", "계정별 핵심 위험은?"),
        thread_id=kwargs.pop("thread_id", "aggregate-thread"),
        runtime_root=root,
        checkpointer=saver,
        client=client,
        **kwargs,
    )


def _recommit_source(pkg: Path, sheet: str = "Main") -> None:
    scope = AuditScope.for_sheet(sheet)
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    _, facts, standards, brief, _ = loaded
    changed = copy.deepcopy(brief)
    changed["summary"]["text"] += " source bundle 변경"
    _write_scope(pkg, sheet, facts, standards, changed)


def _artifact_documents(root: Path, kind: str) -> list[dict]:
    result: list[dict] = []
    for path in root.glob("threads/*/objects/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("kind") == kind:
            result.append(document)
    return result


def test_aggregate_root_keeps_same_local_ids_distinct_and_hydrates_exact_sheets(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    client = RootSelectionClient()

    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
    )

    assert result["schema_version"] == "audit_conversation_turn_result.v1"
    assert result["bundle"]["kind"] == "aggregate"
    response = result["response"]
    assert response["schema_version"] == "audit_main_agent_response.v1"
    evidence = response["evidence"]["records"]
    assert len(evidence) == 2
    assert len({item["selection_ref"] for item in evidence}) == 2
    assert {item["source_id"] for item in evidence} == {"statement:fact"}
    assert {item["scope"]["sheet"] for item in evidence} == {"Main", "Other"}
    for item in evidence:
        assert [fact["id"] for fact in item["trace"]["facts"]] == ["fact:risk"]
        assert {
            cell["sheet"] for cell in item["trace"].get("cells", [])
        } <= {item["scope"]["sheet"]}
        assert item["trace"]["cells"]
    model_payload = client.calls[0]["user"]
    assert _FORMULA_SENTINEL not in model_payload
    assert _RAW_STANDARD_SENTINEL not in model_payload
    payload = json.loads(model_payload)
    assert payload["selection_contract"] == {
        "linked_evidence_hydrated_after_final": True,
        "select_linked_refs_only_if_observed_and_independently_needed": True,
        "must_finalize": False,
    }
    assert "Selecting that statement is sufficient for final provenance" in (
        client.calls[0]["system"]
    )
    assert len(client.calls) == 1
    assert result["usage"]["total_tokens"] == 25
    assert response["trust"]["answer_review_status"] == "unreviewed"
    assert response["trust"]["readiness"]["status"] == "partial"
    assert "AGGREGATE_PARTIAL" in {item["code"] for item in response["notices"]}


def test_aggregate_last_model_call_is_final_only(tmp_path: Path) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    aggregate_ref = aggregate.document["portfolio"]["highlights"][0]["record_ref"]
    client = QueueClient([_final("aggregate_record", aggregate_ref)])

    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
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


def test_aggregate_last_model_call_rejects_tool_without_execution(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    root = tmp_path / "runtime"
    client = QueueClient([{
        "action": "tool",
        "tool": {
            "name": "aggregate_search",
            "query": "위험",
            "kind": "statement",
            "item_ref": None,
            "scope_id": None,
            "limit": 1,
        },
        "final": None,
    }])

    with pytest.raises(
        AuditConversationError,
        match="1회 안에",
    ):
        _run(
            pkg,
            aggregate.paths.aggregate_id,
            root,
            InMemorySaver(),
            client,
            max_steps=1,
        )

    assert len(client.calls) == 1
    observations = _artifact_documents(root, "observations")
    assert all(
        not any(
            item.get("tool") == "aggregate_search"
            for item in document["payload"]["value"]
        )
        for document in observations
    )


def test_unobserved_linked_source_ref_retries_with_aggregate_statement_only(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="관련 감사기준은?",
    )
    scope_id = AuditScope.for_sheet("Main").id
    aggregate_record = next(
        record for record in runtime.aggregate_records.values()
        if record["scope"]["id"] == scope_id
        and record.get("source_id") == "statement:standard"
    )
    aggregate_ref = aggregate_record["record_ref"]
    citation_ref = runtime.source_lookup[
        (scope_id, "standard_citation", "citation:1")
    ]

    invalid_final = {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": False,
            "abstention_code": None,
            "selections": [
                {"kind": "aggregate_record", "refs": [aggregate_ref]},
                {"kind": "source_record", "refs": [citation_ref]},
            ],
        },
    }

    def retry_with_statement_only(payload: dict) -> dict:
        validation = next(
            item["result"]["error"]
            for item in payload["observations"]
            if item.get("tool") == "answer_validation"
        )
        assert validation["code"] == "UNGROUNDED_FINAL"
        assert "unobserved source ref" in validation["message"][0]
        assert not any(
            item.get("tool") == "trace" for item in payload["observations"]
        )
        assert payload["selection_contract"]["must_finalize"] is True
        return _final("aggregate_record", aggregate_ref)

    client = QueueClient([invalid_final, retry_with_statement_only])
    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
        question="관련 감사기준은?",
        max_steps=2,
    )

    [evidence] = result["response"]["evidence"]["records"]
    assert evidence["selection_ref"] == aggregate_ref
    assert evidence["selection_kind"] == "aggregate_record"
    assert [
        citation["id"] for citation in evidence["trace"]["standards_citations"]
    ] == ["citation:1"]
    assert evidence["trace_complete"] is True
    [claim] = result["response"]["answer"]["claims"]
    assert claim["aggregate_record_refs"] == [aggregate_ref]
    assert claim["source_record_refs"] == []
    assert len(client.calls) == 2


def test_aggregate_checkpoint_ids_cannot_override_observation_replay(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="계정별 핵심 위험은?",
    )
    turn_state = aggregate_agent_module._new_audit_aggregate_agent_turn_state(runtime)
    root = tmp_path / "runtime"
    store = ConversationArtifactStore(root)
    invocation_id = "aggregate-checkpoint-replay"
    observations_ref = store.write(
        "aggregate-thread",
        kind="observations",
        schema_version=conversation_module.OBSERVATIONS_SCHEMA,
        payload={
            "invocation_id": invocation_id,
            "value": turn_state.observations,
        },
    )
    context = conversation_module.AuditConversationRuntime(
        thread_id="aggregate-thread",
        package_path=pkg,
        sheet=None,
        store=store,
        agent=runtime,
        blocked_response=None,
        aggregate_id=aggregate.paths.aggregate_id,
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
    source = next(iter(runtime.source_records.values()))
    assert source.source_ref not in turn_state.observed[source.kind]
    tampered = copy.deepcopy(state)
    tampered["observed_ids"][source.kind].append(source.source_ref)
    with pytest.raises(AuditConversationError, match="observed_ids"):
        conversation_module._turn_state(context, tampered)


def test_truncated_discovery_is_bound_to_persisted_observations_witness(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    root = tmp_path / "runtime"

    def select_search_match(payload: dict) -> dict:
        result = payload["observations"][-1]["result"]
        assert result["truncated"] is True
        return _final("aggregate_record", result["matches"][0]["record_ref"])

    client = QueueClient([
        {
            "action": "tool",
            "tool": {
                "name": "aggregate_search",
                "query": "위험",
                "kind": "statement",
                "item_ref": None,
                "scope_id": None,
                "limit": 1,
            },
            "final": None,
        },
        select_search_match,
    ])
    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        InMemorySaver(),
        client,
        limit=100,
    )

    response = result["response"]
    assert response["coverage"]["discovery_complete"] is False
    [turn_document] = _artifact_documents(root, "turn_record")
    turn = turn_document["payload"]
    assert set(turn) == {
        "turn_index",
        "invocation_id",
        "question_ref",
        "response_ref",
        "usage_ref",
        "observations_ref",
        "execution",
    }
    assert turn["execution"]["turns"] == 2
    assert turn["execution"]["tools_used"] == [
        "aggregate_brief", "aggregate_search",
    ]
    assert turn["execution"]["limit"] == 100
    assert turn["execution"]["model"] == "main-model"
    serialized_turn = json.dumps(turn, ensure_ascii=False)
    assert '"result"' not in serialized_turn
    assert "조서에 매출 위험이 기록됐다." not in serialized_turn

    store = ConversationArtifactStore(root)
    observations_payload = store.load(
        "aggregate-thread",
        turn["observations_ref"],
        expected_kind="observations",
        expected_schema_version=conversation_module.OBSERVATIONS_SCHEMA,
    )
    observations = observations_payload["value"]
    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="계정별 핵심 위험은?",
        limit=1,
    )
    discovery_complete, observed, tools_used, turns = _aggregate_observation_witness(
        runtime,
        observations,
        expected_limit=response["generator"]["limit"],
    )
    assert discovery_complete is False

    omitted_observations = [
        item for item in observations
        if item.get("tool") != "aggregate_search"
    ]
    omitted_complete, _, omitted_tools, omitted_turns = (
        _aggregate_observation_witness(
            runtime,
            omitted_observations,
            expected_limit=response["generator"]["limit"],
        )
    )
    assert omitted_complete is True
    assert {
        "turns": turn["execution"]["turns"],
        "tools_used": turn["execution"]["tools_used"],
    } != {
        "turns": omitted_turns,
        "tools_used": omitted_tools,
    }
    omitted_ref = store.write(
        "aggregate-thread",
        kind="observations",
        schema_version=conversation_module.OBSERVATIONS_SCHEMA,
        payload={
            "invocation_id": turn["invocation_id"],
            "value": omitted_observations,
        },
    )
    context = conversation_module.AuditConversationRuntime(
        thread_id="aggregate-thread",
        package_path=pkg,
        sheet=None,
        store=store,
        agent=runtime,
        blocked_response=None,
        aggregate_id=aggregate.paths.aggregate_id,
    )
    with pytest.raises(AuditConversationError, match="execution witness"):
        conversation_module._response_from_ref(
            context,
            turn["response_ref"],
            invocation_id=turn["invocation_id"],
            observations_ref=omitted_ref,
            expected_execution=turn["execution"],
        )
    cross_limit_execution = copy.deepcopy(turn["execution"])
    cross_limit_execution["limit"] = 1
    with pytest.raises(AuditConversationError, match="bootstrap witness"):
        conversation_module._response_from_ref(
            context,
            turn["response_ref"],
            invocation_id=turn["invocation_id"],
            observations_ref=turn["observations_ref"],
            expected_execution=cross_limit_execution,
        )
    forged_provenance = copy.deepcopy(response)
    forged_provenance["generator"]["model"] = "forged-model"
    forged_provenance["generator"]["prompt_sha256"] = "0" * 64
    forged_response_ref = store.write(
        "aggregate-thread",
        kind="response",
        schema_version=conversation_module.RESPONSE_SCHEMA,
        payload={
            "invocation_id": turn["invocation_id"],
            "value": forged_provenance,
        },
    )
    with pytest.raises(AuditConversationError, match="generator"):
        conversation_module._response_from_ref(
            context,
            forged_response_ref,
            invocation_id=turn["invocation_id"],
            observations_ref=turn["observations_ref"],
            expected_execution=turn["execution"],
        )

    forged_observations = copy.deepcopy(observations)
    search = next(
        item for item in forged_observations
        if item.get("tool") == "aggregate_search"
    )
    search["result"]["truncated"] = False
    with pytest.raises(AuditAggregateAgentError, match="deterministic replay"):
        _aggregate_observation_witness(
            runtime,
            forged_observations,
            expected_limit=response["generator"]["limit"],
        )
    with pytest.raises(AuditAggregateAgentError, match="bootstrap witness"):
        _aggregate_observation_witness(
            runtime,
            [],
            expected_limit=response["generator"]["limit"],
        )

    forged = copy.deepcopy(response)
    forged["coverage"]["discovery_complete"] = True
    forged["coverage"]["complete"] = forged["coverage"]["evidence_complete"]
    forged["notices"] = _notices(
        runtime,
        complete=forged["coverage"]["complete"],
    )
    with pytest.raises(AuditAggregateAgentError, match="observations witness"):
        _validated_aggregate_response(
            runtime,
            forged,
            expected_discovery_complete=discovery_complete,
            expected_observed=observed,
            expected_generator=turn["execution"],
        )

    unobserved_source = next(
        record for record in runtime.source_records.values()
        if record.source_ref not in observed[record.kind]
    )
    forged_authorization = copy.deepcopy(response)
    forged_authorization["answer"]["claims"] = [
        _source_claim(runtime, unobserved_source)
    ]
    forged_authorization["evidence"]["records"] = [
        _evidence_for_source(
            runtime,
            unobserved_source,
            limit=response["generator"]["limit"],
        )
    ]
    source_evidence = forged_authorization["evidence"]["records"][0]
    forged_authorization["coverage"].update({
        "complete": False,
        "evidence_complete": source_evidence["trace_complete"],
        "record_count": 1,
        "scope_count": 1,
    })
    forged_authorization["notices"] = _notices(runtime, complete=False)
    with pytest.raises(AuditAggregateAgentError, match="authorization witness"):
        _validated_aggregate_response(
            runtime,
            forged_authorization,
            expected_discovery_complete=discovery_complete,
            expected_observed=observed,
            expected_generator=turn["execution"],
        )


def test_persisted_aggregate_response_rejects_forged_claim_and_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        RootSelectionClient(),
    )
    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="계정별 핵심 위험은?",
    )
    response = result["response"]
    forged_claim = copy.deepcopy(response)
    forged_claim["answer"]["claims"][0]["text"] = "조서에 없는 조작된 결론"
    with pytest.raises(AuditAggregateAgentError, match="claim materialization"):
        _validated_aggregate_response(runtime, forged_claim)

    forged_cell = copy.deepcopy(response)
    cells = forged_cell["evidence"]["records"][0]["trace"]["cells"]
    assert cells
    cells[0]["value"] = "조작된 셀 값"
    with pytest.raises(AuditAggregateAgentError, match="evidence hydration"):
        _validated_aggregate_response(runtime, forged_cell)

    forged_notices = copy.deepcopy(response)
    forged_notices["notices"] = []
    with pytest.raises(AuditAggregateAgentError, match="notices"):
        _validated_aggregate_response(runtime, forged_notices)

    monkeypatch.setattr(aggregate_agent_module, "MAX_EVIDENCE_CELLS", 0)
    with pytest.raises(AuditAggregateAgentError, match="cell evidence"):
        _validated_aggregate_response(runtime, response)


def test_aggregate_get_exposes_qualified_source_ref_before_source_selection(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    aggregate_ref = aggregate.document["accounts"][0]["highlights"][0]["record_ref"]

    def select_linked(payload: dict) -> dict:
        result = payload["observations"][-1]["result"]
        source = next(
            item for item in result["linked_source_records"]
            if item["kind"] == "statement"
        )
        return _final("source_record", source["source_ref"])

    client = QueueClient([
        {
            "action": "tool",
            "tool": {
                "name": "aggregate_get",
                "query": None,
                "kind": None,
                "item_ref": aggregate_ref,
                "scope_id": None,
                "limit": 20,
            },
            "final": None,
        },
        select_linked,
    ])
    saver = InMemorySaver()
    root = tmp_path / "runtime"

    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        client,
    )

    item = result["response"]["evidence"]["records"][0]
    assert item["selection_kind"] == "source_record"
    assert item["selection_ref"].startswith("source:")
    assert item["source_id"] == "statement:fact"
    assert item["scope"]["sheet"] == "Main"

    resumed_client = QueueClient([_abstained()])
    _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        resumed_client,
        question="그 source를 기억해줘",
    )
    focus = json.loads(resumed_client.calls[0]["user"])["observations"][-1]
    assert focus["tool"] == "conversation_focus"
    assert focus["result"]["records"] == [{
        "typed_kind": "source_record",
        "source_ref": item["selection_ref"],
        "scope": item["scope"],
        "kind": item["source_kind"],
        "source_id": item["source_id"],
        "item": item["source_record"],
    }]


def test_same_aggregate_thread_resumes_with_typed_qualified_focus(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    first = _run(pkg, aggregate.paths.aggregate_id, root, saver, RootSelectionClient())
    resumed_client = RootSelectionClient()

    second = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        resumed_client,
        question="그 두 계정을 다시 보여줘",
    )

    assert first["turn_index"] == 1
    assert second["turn_index"] == 2 and second["resumed"] is True
    payload = json.loads(resumed_client.calls[0]["user"])
    focus = payload["observations"][-1]
    assert focus["tool"] == "conversation_focus"
    records = focus["result"]["records"]
    assert len(records) == 2
    assert all(item["typed_kind"] == "aggregate_record" for item in records)
    assert all(item["record_ref"].startswith("record:") for item in records)

    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="그 두 계정을 다시 보여줘",
    )
    injected = copy.deepcopy(payload["observations"])
    expected_focus = copy.deepcopy(injected[-1])
    prior_refs = {
        item.get("record_ref") or item.get("source_ref")
        for item in expected_focus["result"]["records"]
    }
    unexposed = next(
        record for record in runtime.source_records.values()
        if record.source_ref not in prior_refs
    )
    injected[-1]["result"]["records"].append(_source_wrapper(unexposed))
    with pytest.raises(AuditAggregateAgentError, match="prior turn"):
        _aggregate_observation_witness(
            runtime,
            injected,
            expected_limit=second["response"]["generator"]["limit"],
            expected_focus=expected_focus,
        )


def test_resume_uses_each_historical_turn_limit_for_exact_trace_validation(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    first = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        RootSelectionClient(),
        limit=100,
    )
    second = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        RootSelectionClient(),
        question="더 짧게 다시 보여줘",
        limit=1,
    )

    assert first["response"]["generator"]["limit"] == 100
    assert second["turn_index"] == 2 and second["resumed"] is True
    assert second["response"]["generator"]["limit"] == 1


def test_trace_tool_limit_is_honored_and_historical_trace_replays_on_resume(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"

    def trace_root(payload: dict) -> dict:
        ref = payload["observations"][0]["result"]["portfolio"]["highlights"][0][
            "record_ref"
        ]
        return {
            "action": "tool",
            "tool": {
                "name": "trace",
                "query": None,
                "kind": None,
                "item_ref": ref,
                "scope_id": None,
                "limit": 1,
            },
            "final": None,
        }

    def trace_full(payload: dict) -> dict:
        trace = payload["observations"][-1]["result"]
        assert trace["trace_complete"] is False
        assert len(trace["trace"]["cells"]) == 1
        ref = payload["observations"][0]["result"]["portfolio"]["highlights"][0][
            "record_ref"
        ]
        return {
            "action": "tool",
            "tool": {
                "name": "trace",
                "query": None,
                "kind": None,
                "item_ref": ref,
                "scope_id": None,
                "limit": 100,
            },
            "final": None,
        }

    def select_traced(payload: dict) -> dict:
        trace = payload["observations"][-1]["result"]
        assert trace["trace_complete"] is True
        assert len(trace["trace"]["cells"]) == 2
        ref = payload["observations"][0]["result"]["portfolio"]["highlights"][0][
            "record_ref"
        ]
        return _final("aggregate_record", ref)

    first = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        QueueClient([trace_root, trace_full, select_traced]),
        limit=100,
    )
    second = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        saver,
        QueueClient([_abstained()]),
        question="trace 근거를 짧게 요약해줘",
        limit=1,
    )

    assert first["response"]["generator"]["limit"] == 100
    assert second["turn_index"] == 2 and second["resumed"] is True


def test_aggregate_default_sqlite_restart_keeps_content_out_of_checkpoints(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    root = tmp_path / "runtime"
    first_question = "SQLite 비밀 계정 질문"
    second_question = "그 계정 근거를 다시 보여줘"
    first = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        None,
        RootSelectionClient(),
        question=first_question,
        thread_id="friendly-aggregate-thread",
    )
    second = _run(
        pkg,
        aggregate.paths.aggregate_id,
        root,
        None,
        RootSelectionClient(),
        question=second_question,
        thread_id="friendly-aggregate-thread",
    )

    assert first["turn_index"] == 1
    assert second["turn_index"] == 2 and second["resumed"] is True
    database_bytes = b"".join(
        path.read_bytes()
        for path in root.glob("checkpoints.sqlite3*")
        if path.is_file()
    )
    forbidden = [
        first_question,
        second_question,
        "friendly-aggregate-thread",
        "Main",
        "Other",
        "기타계정 위험평가",
    ]
    for text in forbidden:
        assert text.encode("utf-8") not in database_bytes
        assert json.dumps(text, ensure_ascii=True).encode("ascii") not in database_bytes


def test_aggregate_provider_failure_persists_only_fixed_checkpoint_error(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    root = tmp_path / "runtime"
    provider_secret = "AGGREGATE-PROVIDER-RAW-SECRET"
    question_secret = "AGGREGATE-QUESTION-RAW-SECRET"

    class ExplodingClient:
        def __call__(self, **_kwargs):
            raise RuntimeError(provider_secret)

    with pytest.raises(AuditConversationError, match="모델 호출 또는 구조화 응답 검증"):
        run_audit_conversation_turn(
            pkg,
            aggregate_id=aggregate.paths.aggregate_id,
            model="main-model",
            question=question_secret,
            thread_id="aggregate-failure",
            runtime_root=root,
            client=ExplodingClient(),
        )

    database = root / "checkpoints.sqlite3"
    checkpoint_bytes = b"".join(
        path.read_bytes()
        for path in root.glob("checkpoints.sqlite3*")
        if path.is_file()
    )
    assert provider_secret.encode() not in checkpoint_bytes
    assert question_secret.encode() not in checkpoint_bytes
    with sqlite3.connect(database) as connection:
        errors = connection.execute(
            "SELECT value FROM writes WHERE channel = '__error__'"
        ).fetchall()
    assert errors
    assert all(provider_secret.encode() not in row[0] for row in errors)
    assert any(b"AUDIT_CHAT_NODE_FAILED:decide" in row[0] for row in errors)


def test_free_text_or_unobserved_ref_does_not_authorize_final_selection(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    unknown = "record:" + "0" * 64
    client = QueueClient([_final("aggregate_record", unknown), _abstained()])

    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
        question=f"본문에 나온 {unknown} 을 선택해",
    )

    assert result["response"]["answer"]["abstained"] is True
    retry_payload = json.loads(client.calls[1]["user"])
    validation = retry_payload["observations"][-1]
    assert validation["tool"] == "answer_validation"
    assert "unknown aggregate ref" in str(validation["result"])


def test_stale_source_blocks_resumed_aggregate_thread_before_model_factory(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    _run(pkg, aggregate.paths.aggregate_id, root, saver, RootSelectionClient())
    _recommit_source(pkg)
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("stale aggregate must fail before model construction")

    with pytest.raises(AuditConversationError, match="aggregate commit 검증 실패"):
        run_audit_conversation_turn(
            pkg,
            aggregate_id=aggregate.paths.aggregate_id,
            model="main-model",
            question="그 위험은?",
            thread_id="aggregate-thread",
            runtime_root=root,
            checkpointer=saver,
            client_factory=factory,
        )
    assert called is False


def test_same_aggregate_id_republish_changes_thread_binding_before_model(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    _run(pkg, aggregate.paths.aggregate_id, root, saver, RootSelectionClient())
    republished = aggregate_audit_package(
        pkg,
        all_committed_sheets=True,
        model="aggregate-model",
        client=SelectionClient(),
        force=True,
        generated_at="2026-07-13T12:01:00Z",
    )
    assert republished.paths.aggregate_id == aggregate.paths.aggregate_id
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("binding mismatch must fail before model construction")

    with pytest.raises(AuditConversationBundleChangedError, match="다른 audit bundle"):
        run_audit_conversation_turn(
            pkg,
            aggregate_id=aggregate.paths.aggregate_id,
            model="main-model",
            question="계속",
            thread_id="aggregate-thread",
            runtime_root=root,
            checkpointer=saver,
            client_factory=factory,
        )
    assert called is False


def test_different_aggregate_cannot_reuse_existing_thread(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    other = aggregate_audit_package(
        pkg,
        sheets=["Main"],
        model="aggregate-model",
        client=SelectionClient(),
        generated_at="2026-07-13T12:02:00Z",
    )
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    _run(pkg, aggregate.paths.aggregate_id, root, saver, RootSelectionClient())
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("cross-aggregate resume must fail before model")

    with pytest.raises(AuditConversationBundleChangedError, match="다른 audit bundle"):
        run_audit_conversation_turn(
            pkg,
            aggregate_id=other.paths.aggregate_id,
            model="main-model",
            question="다른 aggregate",
            thread_id="aggregate-thread",
            runtime_root=root,
            checkpointer=saver,
            client_factory=factory,
        )
    assert called is False


def test_aggregate_thread_cannot_rebind_to_source_sheet_mode(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    _run(pkg, aggregate.paths.aggregate_id, root, saver, RootSelectionClient())
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("cross-mode resume must fail before model")

    with pytest.raises(AuditConversationBundleChangedError, match="다른 audit bundle"):
        run_audit_conversation_turn(
            pkg,
            sheet="Main",
            model="main-model",
            question="시트 모드로 전환",
            thread_id="aggregate-thread",
            runtime_root=root,
            checkpointer=saver,
            client_factory=factory,
        )
    assert called is False


def test_subset_aggregate_preserves_incomplete_workbook_coverage(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    aggregate = aggregate_audit_package(
        pkg,
        sheets=["Main"],
        model="aggregate-model",
        client=SelectionClient(),
        generated_at="2026-07-13T12:03:00Z",
    )
    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        RootSelectionClient(),
    )
    response = result["response"]

    assert response["coverage"]["complete_over_committed_sheets"] is False
    assert response["coverage"]["complete"] is False
    assert "SELECTION_SUBSET" in {item["code"] for item in response["notices"]}

    runtime = _prepare_audit_aggregate_agent_runtime(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="계정별 핵심 위험은?",
    )
    forged = copy.deepcopy(response)
    forged["coverage"]["discovery_complete"] = True
    forged["coverage"]["complete"] = forged["coverage"]["evidence_complete"]
    with pytest.raises(AuditAggregateAgentError, match="discovery complete"):
        _validated_aggregate_response(runtime, forged)


def test_limitation_record_is_scope_verified_without_fabricated_cells(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)

    class LimitationSelectionClient:
        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            selections = []
            portfolio_highlights = []
            portfolio_attention = []
            for scope in payload["scopes"]:
                highlight = scope["highlight_candidates"][0]["record_ref"]
                limitation = next(
                    item["record_ref"] for item in scope["attention_candidates"]
                    if item["kind"] == "limitation"
                )
                selections.append({
                    "scope_id": scope["scope_id"],
                    "highlight_record_refs": [highlight],
                    "attention_record_refs": [limitation],
                })
                portfolio_highlights.append(highlight)
                portfolio_attention.append(limitation)
            return {
                "scope_selections": selections,
                "portfolio_highlight_record_refs": portfolio_highlights,
                "portfolio_attention_record_refs": portfolio_attention,
            }

    aggregate = aggregate_audit_package(
        pkg,
        all_committed_sheets=True,
        model="aggregate-model",
        client=LimitationSelectionClient(),
        generated_at="2026-07-13T12:04:00Z",
    )
    limitation = next(
        record
        for account in aggregate.document["accounts"]
        for record in account["attention_items"]
        if record["kind"] == "limitation"
    )
    client = QueueClient([_final("aggregate_record", limitation["record_ref"])])
    result = _run(
        pkg,
        aggregate.paths.aggregate_id,
        tmp_path / "runtime",
        InMemorySaver(),
        client,
    )
    evidence = result["response"]["evidence"]["records"][0]

    assert evidence["source_kind"] == "brief_limitation"
    assert evidence["trace"]["record_only"] is True
    assert "cells" not in evidence["trace"]


def test_source_change_after_finalization_fails_before_history_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    root = tmp_path / "runtime"
    original_write = conversation_module.ConversationArtifactStore.write
    changed = False

    def write_then_change(self, *args, **kwargs):
        nonlocal changed
        ref = original_write(self, *args, **kwargs)
        if kwargs.get("kind") == "response" and not changed:
            changed = True
            _recommit_source(pkg)
        return ref

    monkeypatch.setattr(
        conversation_module.ConversationArtifactStore,
        "write",
        write_then_change,
    )
    with pytest.raises(AuditConversationBundleChangedError, match="commit 직전"):
        _run(
            pkg,
            aggregate.paths.aggregate_id,
            root,
            InMemorySaver(),
            RootSelectionClient(),
        )
    assert changed is True
    assert not _artifact_documents(root, "turn_record")
    assert not _artifact_documents(root, "history")


def test_sheet_and_aggregate_modes_are_mutually_exclusive_before_model(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("must not construct model")

    with pytest.raises(AuditConversationError, match="함께 사용할 수 없습니다"):
        run_audit_conversation_turn(
            pkg,
            sheet="Main",
            aggregate_id=aggregate.paths.aggregate_id,
            model="main-model",
            question="질문",
            client_factory=factory,
        )
    assert called is False
