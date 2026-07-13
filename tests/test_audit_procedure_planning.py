from __future__ import annotations

import copy
import hashlib
import json

import pytest
import jsonschema

from excel_to_skill.audit.llm import load_schema
from excel_to_skill.audit.procedure_planning import (
    PLANNING_RESULT_SCHEMA,
    ProcedurePlanningError,
    ProcedurePlanningRuntime,
    build_procedure_planning_graph,
    procedure_plan_records,
    procedure_plan_summary,
    run_procedure_planning,
    standard_basis_ref,
    validate_procedure_plan,
    validate_procedure_plan_summary,
    workbook_basis_ref,
)


SCOPE = {"kind": "workbook"}
COLLECTION = "standards_20250829_bgem3"


def _workbook(kind: str, source_ref: str, text: str) -> dict:
    record = {
        "typed_kind": "planning_workbook_basis",
        "scope": copy.deepcopy(SCOPE),
        "source_kind": "fact",
        "record_kind": kind,
        "source_ref": source_ref,
        "text": text,
        "status": "documented",
        "confidence": 0.9,
    }
    record["basis_ref"] = workbook_basis_ref(record)
    return record


def _standard(cid: str = "KSA::330::6") -> dict:
    text = "감사인은 평가된 위험에 대응하는 감사절차를 설계하고 수행한다."
    prefix, standard_no, para_no = cid.split("::", 2)
    record = {
        "typed_kind": "planning_standard_basis",
        "scope": copy.deepcopy(SCOPE),
        "origin": "prepared_citation",
        "source_ref": "standard:citation",
        "collection": COLLECTION,
        "cid": cid,
        "domain": "audit" if prefix == "KSA" else "accounting",
        "framework": "KSA" if prefix == "KSA" else "K-IFRS",
        "source_type": "감사기준" if prefix == "KSA" else "회계기준",
        "standard_no": standard_no,
        "standard_title": "평가된 위험에 대한 감사인의 대응",
        "para_no": para_no,
        "para_type": "요구사항",
        "section_path": "감사절차",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "effective_date_verified": False,
        "verified_by": "standards_get_paragraph",
    }
    record["basis_ref"] = standard_basis_ref(record)
    return record


def _request(*, standards: bool = True, count: int = 3) -> dict:
    account = _workbook("account", "fact:account", "매출채권")
    risk = _workbook("risk", "fact:risk", "가공 매출채권 계상 위험")
    assertion = _workbook("assertion", "fact:assertion", "실재성")
    documented = _workbook("procedure", "fact:procedure", "기존 명세서 대사")
    return {
        "objective": "이 위험과 주장에 대응할 수 있는 여러 test를 추천해줘.",
        "target": {
            "account_ref": account["basis_ref"],
            "risk_ref": risk["basis_ref"],
            "assertion_ref": assertion["basis_ref"],
        },
        "workbook_basis": [account, risk, assertion, documented],
        "standards_basis": [_standard()] if standards else [],
        "existing_procedure_refs": [documented["basis_ref"]],
        "candidate_count": count,
    }


def _candidate(
    rank: int,
    role: str,
    request: dict,
    *,
    title: str | None = None,
) -> dict:
    target = request["target"]
    standard_ref = request["standards_basis"][0]["basis_ref"]
    return {
        "candidate_key": f"T{rank}",
        "rank": rank,
        "portfolio_role": role,
        "title": title or f"감사 test 후보 {rank}",
        "objective": "대상 위험과 주장에 대응하는 감사증거를 확보한다.",
        "approach": "test_of_details",
        "evidence_methods": ["inspection"],
        "steps": ["대상 자료를 확보하고 원천 증빙과 대조한다."],
        "applicability": {
            "assessment": "conditional",
            "conditions_for_use": ["원천 증빙에 접근할 수 있어야 한다."],
            "disqualifiers": ["자료의 완전성을 확인할 수 없는 경우 적용이 제한된다."],
        },
        "evidence_to_obtain": ["독립적으로 확인 가능한 원천 증빙"],
        "strengths": ["기록된 금액을 원천 증빙과 직접 대조할 수 있다."],
        "limitations": ["제공 자료의 완전성에 영향을 받을 수 있다."],
        "prerequisites": ["모집단과 자료 접근 가능성을 확인한다."],
        "open_questions": ["모집단의 구성과 데이터 품질은 어떠한가?"],
        "relationship_to_documented": "new_option",
        "documented_procedure_basis_refs": [],
        "standard_support": "principle_based",
        "workbook_basis_refs": [
            target["account_ref"], target["risk_ref"], target["assertion_ref"]
        ],
        "standards_basis_refs": [standard_ref],
        "quantitative_design": {
            "sample_size": "TBD",
            "amount_threshold": "TBD",
            "selection_interval": "TBD",
        },
    }


def _worker(request: dict) -> dict:
    return {
        "abstained": False,
        "abstention_code": None,
        "assumptions": ["추천 후보는 제공된 제한된 조서 문맥에 기반한다."],
        "open_questions": ["중요성과 통제 의존 전략을 별도로 결정해야 하는가?"],
        "candidates": [
            _candidate(1, "primary", request, title="외부 증거 직접 확보"),
            _candidate(2, "alternative", request, title="후속 거래 증빙 대조"),
            _candidate(3, "complementary", request, title="보조부와 원장 재대사"),
        ],
        "recommended_combinations": [
            {
                "combination_key": "C1",
                "candidate_keys": ["T1", "T3"],
                "rationale": "독립적 증거와 내부 기록 대사를 함께 사용해 증거를 보강한다.",
                "tradeoffs": ["자료 확보 가능성과 추가 수행 시간이 필요하다."],
            },
            {
                "combination_key": "C2",
                "candidate_keys": ["T2", "T3"],
                "rationale": "직접 증거 확보가 어려운 경우 서로 다른 내부 증거를 결합한다.",
                "tradeoffs": ["독립성은 외부 증거 조합보다 제한될 수 있다."],
            },
        ],
    }


class PlanningClient:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.output)


def _runtime(client: object) -> ProcedurePlanningRuntime:
    return ProcedurePlanningRuntime(
        client=client,
        model="stub-model",
        invocation_id="invocation-1",
        bundle_sha256="a" * 64,
        scope=copy.deepcopy(SCOPE),
    )


def test_planner_materializes_multiple_candidates_and_combinations_with_fixed_trust() -> None:
    request = _request()
    client = PlanningClient(_worker(request))

    result = run_procedure_planning(request, runtime=_runtime(client))

    assert result["schema_version"] == PLANNING_RESULT_SCHEMA
    assert result["status"] == "completed"
    assert result["plan_ref"].startswith("procedure-plan:")
    assert result["proposal_status"] == "proposed"
    assert result["review_status"] == "unreviewed"
    assert result["execution_evidence_status"] == "not_evidenced"
    assert result["turn_scoped"] is True
    assert result["outside_prepared_bundle"] is True
    assert [item["portfolio_role"] for item in result["candidates"]] == [
        "primary", "alternative", "complementary"
    ]
    assert all(item["proposal_status"] == "proposed" for item in result["candidates"])
    assert len(result["recommended_combinations"]) == 2
    assert all(
        len(item["proposed_test_refs"]) >= 2
        for item in result["recommended_combinations"]
    )
    assert set(procedure_plan_records(result)) == {
        item["proposed_test_ref"] for item in result["candidates"]
    }
    payload = json.loads(client.calls[0]["user"])
    assert set(payload) == {
        "objective", "target", "workbook_basis", "standards_basis",
        "existing_procedure_refs", "limits"
    }
    assert "allOf" not in client.calls[0]["schema"]


@pytest.mark.parametrize(
    "response_schema_name",
    ["audit_agent_response.schema.json", "audit_main_agent_response.schema.json"],
)
def test_public_response_schemas_strictly_validate_procedure_plan_supplement(
    response_schema_name: str,
) -> None:
    request = _request()
    plan = run_procedure_planning(
        request, runtime=_runtime(PlanningClient(_worker(request)))
    )
    response_schema = load_schema(response_schema_name)
    assert response_schema["properties"]["procedure_plan"] == {
        "$ref": "#/$defs/procedurePlan"
    }
    plan_schema = {
        "$schema": response_schema["$schema"],
        "$ref": "#/$defs/procedurePlan",
        "$defs": response_schema["$defs"],
    }

    jsonschema.Draft7Validator.check_schema(plan_schema)
    jsonschema.validate(plan, plan_schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"status": "completed"}, plan_schema)

    forged = copy.deepcopy(plan)
    forged["unexpected"] = {"performed": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(forged, plan_schema)


def test_planner_accepts_five_distinct_candidates_with_required_role_mix() -> None:
    request = _request(count=5)
    output = _worker(request)
    output["candidates"].extend([
        _candidate(4, "alternative", request, title="다른 원천자료 추적"),
        _candidate(5, "complementary", request, title="독립 재계산 보강"),
    ])

    result = run_procedure_planning(
        request, runtime=_runtime(PlanningClient(output))
    )

    assert len(result["candidates"]) == 5
    assert sum(item["portfolio_role"] == "primary" for item in result["candidates"]) == 1


def test_planner_without_standards_returns_no_plan_before_child_call() -> None:
    request = _request(standards=False)
    client = PlanningClient({"must": "not be called"})

    result = run_procedure_planning(request, runtime=_runtime(client))

    assert result["status"] == "no_plan"
    assert result["worker"]["called"] is False
    assert result["worker"]["abstention_code"] == "insufficient_basis"
    assert result["candidates"] == []
    assert result["recommended_combinations"] == []
    assert client.calls == []


def test_worker_may_abstain_without_materializing_candidates() -> None:
    request = _request()
    client = PlanningClient({
        "abstained": True,
        "abstention_code": "planning_not_supported",
        "assumptions": [],
        "open_questions": ["추가 조서 문맥이 필요한가?"],
        "candidates": [],
        "recommended_combinations": [],
    })

    result = run_procedure_planning(request, runtime=_runtime(client))

    assert result["status"] == "no_plan"
    assert result["worker"]["called"] is True
    assert result["worker"]["abstained"] is True
    assert result["open_questions"] == ["추가 조서 문맥이 필요한가?"]


def test_planning_objective_is_bounded_normalized_data() -> None:
    request = _request(standards=False)
    request["objective"] = "  여러   가능한 test를\n추천해줘.  "
    result = run_procedure_planning(
        request, runtime=_runtime(PlanningClient({"must": "not be called"}))
    )
    assert result["request"]["objective"] == "여러 가능한 test를 추천해줘."

    request["objective"] = "가" * 501
    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(
            request, runtime=_runtime(PlanningClient({"must": "not be called"}))
        )
    assert error.value.code == "LIMIT_EXCEEDED"


def test_planner_rejects_candidate_role_mix_without_all_three_roles() -> None:
    request = _request()
    output = _worker(request)
    output["candidates"][2]["portfolio_role"] = "alternative"

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    assert error.value.code == "CONTRACT_MISMATCH"


def test_planner_requires_an_applicability_condition_for_every_candidate() -> None:
    request = _request()
    output = _worker(request)
    output["candidates"][0]["applicability"]["conditions_for_use"] = []

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    assert error.value.code == "CONTRACT_MISMATCH"


@pytest.mark.parametrize("boundary", ["long_text", "four_text_items"])
def test_planner_rejects_worker_output_beyond_compact_text_boundaries(
    boundary: str,
) -> None:
    request = _request()
    output = _worker(request)
    if boundary == "long_text":
        output["candidates"][0]["objective"] = "가" * 401
    else:
        output["candidates"][0]["evidence_to_obtain"] = [
            f"감사증거 {index}" for index in range(1, 5)
        ]

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    assert error.value.code == "CONTRACT_MISMATCH"


def test_planner_rejects_unobserved_basis_ref() -> None:
    request = _request()
    output = _worker(request)
    output["candidates"][0]["workbook_basis_refs"].append(
        "workbook-basis:" + "f" * 64
    )

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    assert error.value.code == "CONTRACT_MISMATCH"


@pytest.mark.parametrize(
    "text",
    ["표본 수 25건을 선정한다.", "금액 기준 10,000,000원을 적용한다.", "모집단의 20%를 검사한다."],
)
def test_planner_rejects_quantitative_sample_or_amount_design(text: str) -> None:
    request = _request()
    output = _worker(request)
    output["candidates"][0]["steps"] = [text]

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    assert error.value.code == "CONTRACT_MISMATCH"


def test_planner_rejects_combination_with_only_one_candidate() -> None:
    request = _request()
    output = _worker(request)
    output["recommended_combinations"][0]["candidate_keys"] = ["T1"]

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(PlanningClient(output)))

    # JSON Schema rejects this before semantic materialization.  It is a model-output contract
    # violation, not provider unavailability.
    assert error.value.code == "CONTRACT_MISMATCH"


@pytest.mark.parametrize(
    "output",
    [
        "{not-valid-json",
        {"unexpected": "schema-violating model output"},
    ],
)
def test_planner_maps_invalid_model_json_or_schema_to_contract_mismatch(
    output: object,
) -> None:
    request = _request()

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(
            request, runtime=_runtime(PlanningClient(output))
        )

    assert error.value.code == "CONTRACT_MISMATCH"


def test_planner_maps_provider_availability_failure_to_upstream_unavailable() -> None:
    request = _request()

    class UnavailableClient:
        def __call__(self, **kwargs):
            raise TimeoutError("provider detail must not escape")

    with pytest.raises(ProcedurePlanningError) as error:
        run_procedure_planning(request, runtime=_runtime(UnavailableClient()))

    assert error.value.code == "UPSTREAM_UNAVAILABLE"
    assert "provider detail" not in str(error.value)


def test_validate_plan_rejects_status_promotion_or_unobserved_combination_ref() -> None:
    request = _request()
    result = run_procedure_planning(
        request, runtime=_runtime(PlanningClient(_worker(request)))
    )
    promoted = copy.deepcopy(result)
    promoted["candidates"][0]["proposal_status"] = "documented"
    with pytest.raises(ProcedurePlanningError):
        validate_procedure_plan(promoted)

    forged = copy.deepcopy(result)
    forged["recommended_combinations"][0]["proposed_test_refs"][0] = (
        "proposed-test:" + "f" * 64
    )
    with pytest.raises(ProcedurePlanningError):
        validate_procedure_plan(forged)

    changed_content = copy.deepcopy(result)
    changed_content["candidates"][0]["title"] = "ref를 유지한 변조 제목"
    with pytest.raises(ProcedurePlanningError):
        validate_procedure_plan(changed_content)

    changed_worker = copy.deepcopy(result)
    changed_worker["worker"]["prompt_sha256"] = "f" * 64
    with pytest.raises(ProcedurePlanningError):
        validate_procedure_plan(changed_worker)


def test_compiled_planner_explicitly_disables_checkpoint_inheritance() -> None:
    graph = build_procedure_planning_graph()
    assert graph.checkpointer is False


def test_plan_summary_selects_exactly_one_current_observation_plan() -> None:
    request = _request()
    result = run_procedure_planning(
        request, runtime=_runtime(PlanningClient(_worker(request)))
    )
    observations = [{
        "tool": "procedure_planning",
        "input": {"objective": request["objective"]},
        "result": copy.deepcopy(result),
    }]

    summary = procedure_plan_summary(
        observations, selected_refs=[result["plan_ref"]]
    )

    assert summary == result
    assert validate_procedure_plan_summary(summary, observations=observations) == result
    assert procedure_plan_summary(observations, selected_refs=[]) is None


def test_plan_summary_rejects_unobserved_or_multiple_plan_refs() -> None:
    request = _request()
    first = run_procedure_planning(
        request, runtime=_runtime(PlanningClient(_worker(request)))
    )
    observation = {"tool": "procedure_planning", "input": {}, "result": first}

    with pytest.raises(ProcedurePlanningError):
        procedure_plan_summary(
            [observation], selected_refs=["procedure-plan:" + "f" * 64]
        )
    with pytest.raises(ProcedurePlanningError) as error:
        procedure_plan_summary(
            [observation, copy.deepcopy(observation)], selected_refs=[first["plan_ref"]]
        )
    assert error.value.code == "LIMIT_EXCEEDED"
