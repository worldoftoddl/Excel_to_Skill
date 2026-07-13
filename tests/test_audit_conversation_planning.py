"""Workbook-bound procedure-planning integration with the persistent graph."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

from excel_to_skill.audit.conversation import (
    render_audit_conversation_markdown,
    run_audit_conversation_turn,
)
from excel_to_skill.audit import procedure_planning as planning_module

from test_audit_consume_gate import _write_committed_bundle
from test_audit_conversation_research import ResearchRetriever


ACCOUNT_ID = "fact:account:receivables"
RISK_ID = "fact:risk"
ASSERTION_ID = "fact:assertion:existence"
PROCEDURE_ID = "fact:procedure:reconcile"
TESTS_ID = "relation:tests:reconcile"
ADDRESSES_ID = "relation:addresses:reconcile"
ASSERTS_OVER_ID = "relation:asserts-over:receivables"
CITATION_ID = "citation:1"
TARGET_STATEMENT_ID = "statement:synthesis"


def _fact(
    fact_id: str,
    fact_type: str,
    description: str,
    *,
    status: str,
    normalized_code: str | None = None,
) -> dict:
    return {
        "id": fact_id,
        "type": fact_type,
        "description": description,
        "status": status,
        "normalized_code": normalized_code,
        "value": None,
        "unit": None,
        "severity": None,
        "confidence": 0.9,
        "source_ids": ["source:1"],
    }


def _relation(
    relation_id: str,
    relation_type: str,
    from_fact_id: str,
    to_fact_id: str,
) -> dict:
    return {
        "id": relation_id,
        "type": relation_type,
        "from_fact_id": from_fact_id,
        "to_fact_id": to_fact_id,
        "status": "documented",
        "confidence": 0.9,
        "source_ids": ["source:1"],
    }


def _configure_planning_bundle(_pkg, facts, context, brief) -> None:
    facts["facts"][0].update({
        "description": "가공 매출채권이 계상될 위험",
        "status": "identified",
        "severity": "high",
    })
    facts["facts"].extend([
        _fact(
            ACCOUNT_ID,
            "account",
            "매출채권",
            status="documented",
        ),
        _fact(
            ASSERTION_ID,
            "assertion",
            "기말 매출채권이 실제로 존재한다.",
            status="documented",
            normalized_code="existence",
        ),
        _fact(
            PROCEDURE_ID,
            "procedure",
            "매출채권 명세서와 총계정원장을 대사하였다.",
            status="performed",
        ),
    ])
    facts["relations"].extend([
        _relation(TESTS_ID, "tests", PROCEDURE_ID, ASSERTION_ID),
        _relation(ADDRESSES_ID, "addresses", PROCEDURE_ID, RISK_ID),
        _relation(ASSERTS_OVER_ID, "asserts_over", ASSERTION_ID, ACCOUNT_ID),
    ])

    standard_text = "감사인은 평가된 위험에 대응하는 감사절차를 설계하고 수행한다."
    citation = context["citations"][0]
    citation.update({
        "document_id": "KSA::330::6",
        "paragraph": "6",
        "title": "평가된 위험에 대한 감사인의 대응",
        "snippet": standard_text,
        "snippet_sha256": hashlib.sha256(
            standard_text.encode("utf-8")
        ).hexdigest(),
        "source_uri": "standards://KSA::330::6",
        "provider_metadata": {
            "source_cid": "KSA::330::6",
            "source_type": "감사기준",
            "standard_no": "330",
            "para_type": "요구사항",
            "section_path": "평가된 위험에 대한 대응 > 감사절차",
            "verified_by": "standards_get_paragraph",
        },
    })

    statement = next(
        item for item in brief["statements"]
        if item["id"] == TARGET_STATEMENT_ID
    )
    statement.update({
        "text": "매출채권 위험과 실재성 주장에 문서화된 절차가 연결되어 있다.",
        "fact_ids": [ACCOUNT_ID, RISK_ID, ASSERTION_ID, PROCEDURE_ID],
        "relation_ids": [TESTS_ID, ADDRESSES_ID, ASSERTS_OVER_ID],
        "standard_citation_ids": [CITATION_ID],
    })


def _trace_request() -> dict:
    return {
        "action": "tool",
        "tool": {
            "name": "trace",
            "query": None,
            "kind": None,
            "item_id": TARGET_STATEMENT_ID,
            "limit": 20,
        },
        "final": None,
    }


def _planning_request(*, query: str = "대응 가능한 감사 test 후보를 추천해줘") -> dict:
    return {
        "action": "tool",
        "tool": {
            "name": "procedure_planning",
            "query": query,
            "kind": None,
            "item_id": None,
            "limit": 3,
            "fact_ids": [ACCOUNT_ID, RISK_ID, ASSERTION_ID, PROCEDURE_ID],
            "relation_ids": [TESTS_ID, ADDRESSES_ID, ASSERTS_OVER_ID],
            "standard_citation_ids": [CITATION_ID],
            "research_refs": [],
        },
        "final": None,
    }


def _final(
    *,
    plan_ref: str | None = None,
    selections: list[dict] | None = None,
) -> dict:
    final = {
        "abstained": False,
        "abstention_code": None,
        "selections": list(selections or []),
    }
    if plan_ref is not None:
        final["plan_refs"] = [plan_ref]
    return {"action": "final", "tool": None, "final": final}


def _abstained_final() -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": True,
            "abstention_code": "insufficient_evidence",
            "selections": [],
        },
    }


def _candidate(
    rank: int,
    role: str,
    payload: dict,
    *,
    title: str,
    method: str,
    relationship: str,
) -> dict:
    target = payload["target"]
    existing = payload["existing_procedure_refs"]
    return {
        "candidate_key": f"T{rank}",
        "rank": rank,
        "portfolio_role": role,
        "title": title,
        "objective": "가공 매출채권 위험에 대응하는 감사증거를 확보한다.",
        "approach": "test_of_details",
        "evidence_methods": [method],
        "steps": ["대상 항목을 선정하고 관련 원천 증거와 대조한다."],
        "applicability": {
            "assessment": "conditional",
            "conditions_for_use": ["관련 원천 자료에 접근할 수 있어야 한다."],
            "disqualifiers": ["모집단의 완전성을 확인할 수 없으면 적용이 제한된다."],
        },
        "evidence_to_obtain": ["독립적으로 검증 가능한 원천 증거"],
        "strengths": ["위험과 주장에 직접 대응하는 증거를 얻을 수 있다."],
        "limitations": ["자료의 품질과 입수 가능성에 영향을 받는다."],
        "prerequisites": ["모집단과 자료 접근 가능성을 확인한다."],
        "open_questions": ["모집단의 구성과 데이터 품질은 어떠한가?"],
        "relationship_to_documented": relationship,
        "documented_procedure_basis_refs": list(existing),
        "standard_support": "principle_based",
        "workbook_basis_refs": [
            target["account_ref"],
            target["risk_ref"],
            target["assertion_ref"],
        ],
        "standards_basis_refs": [payload["standards_basis"][0]["basis_ref"]],
        "quantitative_design": {
            "sample_size": "TBD",
            "amount_threshold": "TBD",
            "selection_interval": "TBD",
        },
    }


def _worker_output(payload: dict) -> dict:
    return {
        "abstained": False,
        "abstention_code": None,
        "assumptions": ["추천은 현재 조서와 검증된 기준서 문단에 한정한다."],
        "open_questions": ["중요성과 통제 의존 전략은 어떻게 결정되었는가?"],
        "candidates": [
            _candidate(
                1,
                "primary",
                payload,
                title="거래처 외부조회",
                method="external_confirmation",
                relationship="complements_documented",
            ),
            _candidate(
                2,
                "alternative",
                payload,
                title="기말 후 입금 증빙 대조",
                method="inspection",
                relationship="alternative_to_documented",
            ),
            _candidate(
                3,
                "complementary",
                payload,
                title="명세서와 원장 재대사",
                method="reperformance",
                relationship="overlaps_documented",
            ),
        ],
        "recommended_combinations": [{
            "combination_key": "C1",
            "candidate_keys": ["T1", "T3"],
            "rationale": "독립적 외부 증거와 재수행 증거를 함께 사용한다.",
            "tradeoffs": ["외부 회신과 추가 수행 시간이 필요하다."],
        }],
    }


def _usage_event(index: int) -> dict:
    return {
        "provider": "stub",
        "model": "stub-model",
        "input_tokens": 10 + index,
        "output_tokens": 3,
        "total_tokens": 13 + index,
    }


class SuccessfulPlanningClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.usage_events: list[dict] = []
        self.main_calls = 0
        self.child_calls = 0
        self.plan_ref: str | None = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.usage_events.append(_usage_event(len(self.calls)))
        payload = json.loads(kwargs["user"])
        if "objective" in payload and "target" in payload:
            self.child_calls += 1
            return _worker_output(payload)
        self.main_calls += 1
        assert payload["remaining_turns"] == payload["remaining_model_calls"]
        if self.main_calls == 1:
            assert payload["capabilities"]["procedure_planning"]["enabled"] is True
            return _trace_request()
        if self.main_calls == 2:
            trace = next(
                item for item in payload["observations"]
                if item.get("tool") == "trace"
            )
            assert {item["id"] for item in trace["result"]["facts"]} == {
                ACCOUNT_ID, RISK_ID, ASSERTION_ID, PROCEDURE_ID
            }
            assert {item["id"] for item in trace["result"]["relations"]} == {
                TESTS_ID, ADDRESSES_ID, ASSERTS_OVER_ID
            }
            assert trace["result"]["standards_citations"][0]["id"] == CITATION_ID
            return _planning_request()
        plan = next(
            item["result"] for item in payload["observations"]
            if item.get("tool") == "procedure_planning"
            and item.get("result", {}).get("schema_version")
            == "audit_procedure_plan.v1"
        )
        assert payload["remaining_model_calls"] == 2
        self.plan_ref = plan["plan_ref"]
        return _final(plan_ref=self.plan_ref)


def _prepared_bytes(pkg: Path) -> dict[str, bytes]:
    paths = (
        pkg / "meta.json",
        pkg / "data/audit_facts.json",
        pkg / "data/standards_context.json",
        pkg / "data/audit_brief.json",
    )
    return {str(path.relative_to(pkg)): path.read_bytes() for path in paths}


def test_workbook_planning_materializes_portfolio_and_keeps_answer_separate(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )
    prepared_before = _prepared_bytes(pkg)
    client = SuccessfulPlanningClient()

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="가공 매출채권 위험에 어떤 test를 해야 하나?",
        thread_id="planning-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        procedure_planning=True,
    )

    plan = result["response"]["procedure_plan"]
    assert plan["plan_ref"] == client.plan_ref
    assert plan["status"] == "completed"
    assert plan["proposal_status"] == "proposed"
    assert plan["review_status"] == "unreviewed"
    assert plan["execution_evidence_status"] == "not_evidenced"
    assert 3 <= len(plan["candidates"]) <= 5
    assert [item["portfolio_role"] for item in plan["candidates"]] == [
        "primary", "alternative", "complementary"
    ]
    assert all(item["applicability"]["conditions_for_use"] for item in plan["candidates"])
    assert all(item["evidence_to_obtain"] for item in plan["candidates"])
    assert all(item["strengths"] and item["limitations"] for item in plan["candidates"])
    combination = plan["recommended_combinations"][0]
    assert combination["candidate_keys"] == ["T1", "T3"]
    assert len(combination["proposed_test_refs"]) == 2
    assert all(ref.startswith("proposed-test:") for ref in combination["proposed_test_refs"])

    # A proposed plan is supplemental; it cannot become a performed workpaper claim.
    answer = result["response"]["answer"]
    assert answer["abstained"] is True
    assert answer["claims"] == []
    assert "거래처 외부조회" not in json.dumps(answer, ensure_ascii=False)
    assert client.main_calls == 3
    assert client.child_calls == 1
    assert result["usage"]["request_count"] == 4
    assert len(result["usage"]["requests"]) == 4
    assert result["usage"]["total_tokens"] == sum(
        event["total_tokens"] for event in client.usage_events
    )
    rendered = render_audit_conversation_markdown(result)
    for expected in (
        "수행 단계",
        "적용 배제/제약 조건",
        "후속 확인사항",
        "KSA::330::6",
        "조합 trade-off",
    ):
        assert expected in rendered
    assert _prepared_bytes(pkg) == prepared_before


def test_disabled_planning_returns_fixed_error_without_child_call(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )

    class DisabledClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.child_calls = 0

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if "objective" in payload:
                self.child_calls += 1
                raise AssertionError("disabled planning must not call the child worker")
            if len(self.calls) == 1:
                assert payload["capabilities"]["procedure_planning"]["enabled"] is False
                return _planning_request()
            observation = next(
                item for item in payload["observations"]
                if item.get("tool") == "procedure_planning"
            )
            assert observation["result"] == {
                "error": {
                    "code": "PLANNING_DISABLED",
                    "message": "감사 test 후보 계획을 완료하지 못했습니다.",
                }
            }
            return _abstained_final()

    client = DisabledClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="test를 추천해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        procedure_planning=False,
    )

    assert client.child_calls == 0
    assert len(client.calls) == 2
    assert result["response"]["answer"]["abstained"] is True
    assert "procedure_plan" not in result["response"]


def test_second_planning_request_is_bounded_without_second_child_call(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )

    class BoundedClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.main_calls = 0
            self.child_calls = 0
            self.plan_ref: str | None = None

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if "objective" in payload:
                self.child_calls += 1
                return _worker_output(payload)
            self.main_calls += 1
            if self.main_calls == 1:
                return _trace_request()
            if self.main_calls == 2:
                return _planning_request(query="첫 번째 후보 묶음")
            if self.main_calls == 3:
                first = next(
                    item["result"] for item in payload["observations"]
                    if item.get("tool") == "procedure_planning"
                )
                self.plan_ref = first["plan_ref"]
                return _planning_request(query="두 번째 후보 묶음")
            planning = [
                item for item in payload["observations"]
                if item.get("tool") == "procedure_planning"
            ]
            assert len(planning) == 2
            assert planning[1]["result"] == {
                "error": {
                    "code": "PLANNING_LIMIT_EXCEEDED",
                    "message": "감사 test 후보 계획을 완료하지 못했습니다.",
                }
            }
            assert self.plan_ref is not None
            return _final(plan_ref=self.plan_ref)

    client = BoundedClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="서로 다른 후보 묶음을 두 번 만들어줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        max_steps=8,
        procedure_planning=True,
    )

    assert client.child_calls == 1
    assert result["response"]["procedure_plan"]["plan_ref"] == client.plan_ref


def test_successful_plan_must_be_selected_before_finalization(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )

    class RetryClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.main_calls = 0
            self.plan_ref: str | None = None
            self.saw_validation = False

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if "objective" in payload:
                return _worker_output(payload)
            self.main_calls += 1
            if self.main_calls == 1:
                return _trace_request()
            if self.main_calls == 2:
                return _planning_request()
            plan = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "procedure_planning"
            )
            self.plan_ref = plan["plan_ref"]
            if self.main_calls == 3:
                return _final(selections=[{
                    "kind": "statement",
                    "ids": ["statement:fact"],
                }])
            validation = next(
                item for item in payload["observations"]
                if item.get("tool") == "answer_validation"
            )
            assert validation["result"]["error"]["code"] == "UNGROUNDED_PLANNING_FINAL"
            self.saw_validation = True
            return _final(
                plan_ref=self.plan_ref,
                selections=[{
                    "kind": "statement",
                    "ids": ["statement:fact"],
                }],
            )

    client = RetryClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="후보를 만들고 결과를 보여줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        max_steps=8,
        procedure_planning=True,
    )

    assert client.saw_validation is True
    assert client.main_calls == 4
    assert result["response"]["procedure_plan"]["plan_ref"] == client.plan_ref


def test_plan_content_and_refs_are_not_reexposed_in_next_turn_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )
    saver = InMemorySaver()
    root = tmp_path / "runtime"
    first_client = SuccessfulPlanningClient()
    first = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="가공 매출채권 위험의 test 후보는?",
        thread_id="planning-focus-thread",
        client=first_client,
        checkpointer=saver,
        runtime_root=root,
        procedure_planning=True,
    )
    first_plan_ref = first["response"]["procedure_plan"]["plan_ref"]

    class FollowupClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            serialized = json.dumps(payload, ensure_ascii=False)
            assert first_plan_ref not in serialized
            assert "procedure-plan:" not in serialized
            assert "proposed-test:" not in serialized
            assert "거래처 외부조회" not in serialized
            focus = next(
                item for item in payload["observations"]
                if item.get("tool") == "conversation_focus"
            )
            assert focus["result"]["records"] == []
            return _abstained_final()

    followup = FollowupClient()
    monkeypatch.setattr(
        planning_module,
        "load_prompt",
        lambda _name: ("updated planning prompt", "f" * 64),
    )
    second = run_audit_conversation_turn(
        pkg,
        model="stub-model-v2",
        question="그 제안은 이미 수행된 절차인가?",
        thread_id="planning-focus-thread",
        client=followup,
        checkpointer=saver,
        runtime_root=root,
        procedure_planning=False,
    )

    assert second["turn_index"] == 2
    assert second["resumed"] is True
    assert len(followup.calls) == 1
    assert "procedure_plan" not in second["response"]


def test_research_required_does_not_consume_the_planning_opportunity(
    tmp_path: Path,
) -> None:
    def configure_without_prepared_standard(pkg, facts, context, brief) -> None:
        _configure_planning_bundle(pkg, facts, context, brief)
        statement = next(
            item for item in brief["statements"]
            if item["id"] == TARGET_STATEMENT_ID
        )
        statement["type"] = "documented_fact"
        statement["status"] = "documented"
        statement["standard_citation_ids"] = []

    pkg, _, standards, _ = _write_committed_bundle(
        tmp_path,
        configure=configure_without_prepared_standard,
    )
    retriever = ResearchRetriever(standards["retriever"]["corpus_version"])

    class ResearchThenPlanningClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.main_calls = 0
            self.research_child_calls = 0
            self.planning_child_calls = 0
            self.plan_ref: str | None = None

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if "objective" in payload and "target" in payload:
                self.planning_child_calls += 1
                return _worker_output(payload)
            if "candidates" in payload and "limits" in payload:
                self.research_child_calls += 1
                return {
                    "abstained": False,
                    "selected_candidate_refs": [
                        payload["candidates"][0]["candidate_ref"]
                    ],
                }
            self.main_calls += 1
            if self.main_calls == 1:
                return _trace_request()
            if self.main_calls == 2:
                request = _planning_request(query="기준서 근거를 확보한 뒤 후보 추천")
                request["tool"]["standard_citation_ids"] = []
                return request
            if self.main_calls == 3:
                planning_error = next(
                    item["result"] for item in payload["observations"]
                    if item.get("tool") == "procedure_planning"
                )
                assert planning_error["error"]["code"] == "RESEARCH_REQUIRED"
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "가공 매출채권 위험과 실재성 주장 대응 감사절차",
                        "kind": "audit_standard",
                        "item_id": None,
                        "limit": 3,
                    },
                    "final": None,
                }
            if self.main_calls == 4:
                research = next(
                    item["result"] for item in payload["observations"]
                    if item.get("tool") == "standards_research"
                )
                request = _planning_request(query="기준서 근거를 반영한 후보 추천")
                request["tool"]["standard_citation_ids"] = []
                request["tool"]["research_refs"] = [
                    research["records"][0]["research_ref"]
                ]
                return request
            plan = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "procedure_planning"
                and item.get("result", {}).get("schema_version")
                == "audit_procedure_plan.v1"
            )
            self.plan_ref = plan["plan_ref"]
            return _final(plan_ref=self.plan_ref)

    client = ResearchThenPlanningClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="가공 매출채권 위험에 대응할 test 후보를 추천해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        max_steps=9,
        standards_research=True,
        standards_retriever=retriever,
        procedure_planning=True,
    )

    assert result["response"]["procedure_plan"]["plan_ref"] == client.plan_ref
    assert client.research_child_calls == 1
    assert client.planning_child_calls == 1


def test_plan_text_and_refs_stay_out_of_sqlite_checkpoints(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_configure_planning_bundle
    )
    root = tmp_path / "runtime"
    prepared_before = _prepared_bytes(pkg)
    question = "PLANNING-QUESTION-LEAK-SENTINEL 후보를 추천해줘"

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question=question,
        thread_id="planning-sqlite-thread",
        client=SuccessfulPlanningClient(),
        runtime_root=root,
        procedure_planning=True,
    )

    plan = result["response"]["procedure_plan"]
    checkpoint_bytes = b"".join(
        path.read_bytes() for path in root.glob("checkpoints.sqlite3*")
    )
    forbidden = [
        question,
        "거래처 외부조회",
        "대상 항목을 선정하고 관련 원천 증거와 대조한다.",
        "KSA::330::6",
        plan["plan_ref"],
        plan["candidates"][0]["proposed_test_ref"],
    ]
    assert all(item.encode("utf-8") not in checkpoint_bytes for item in forbidden)
    with sqlite3.connect(root / "checkpoints.sqlite3") as connection:
        for table in ("checkpoints", "writes"):
            child_rows = connection.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE checkpoint_ns LIKE 'execute_plan:%'"
            ).fetchone()
            assert child_rows == (0,)
    private_objects = b"".join(
        path.read_bytes() for path in root.glob("threads/*/objects/*.json")
    )
    assert all(item.encode("utf-8") in private_objects for item in forbidden)
    assert _prepared_bytes(pkg) == prepared_before
