"""Isolated, turn-scoped proposed audit-test planning.

The planner is deliberately outside the prepared audit bundle.  Application code supplies
validated opaque wrappers for workbook and standards material; the child model may copy only
those wrapper refs while authoring a bounded, non-exhaustive set of possible tests.  Code then
adds the fixed ``proposed``/``unreviewed``/``not_evidenced`` trust boundary and content-addressed
refs.  Nothing in this module creates a workbook fact, a performed procedure, or an audit
conclusion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, TypedDict

import jsonschema

from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import AuditModelError, json_sha256


PLANNING_VERSION = "0.1.0"
PLANNING_PROMPT = "audit_procedure_planning_v1.md"
PLANNING_WORKER_SCHEMA = "audit_procedure_planning_worker.schema.json"
PLANNING_RESULT_SCHEMA = "audit_procedure_plan.v1"

MIN_CANDIDATES = 3
MAX_CANDIDATES = 5
MAX_COMBINATIONS = 3
MAX_WORKBOOK_BASIS = 24
MAX_STANDARDS_BASIS = 8
MAX_EXISTING_PROCEDURES = 8
MAX_REQUEST_BYTES = 150_000
MAX_WORKER_BYTES = 100_000
MAX_RESULT_BYTES = 180_000

_WORKBOOK_REF_RE = re.compile(r"^workbook-basis:[0-9a-f]{64}$")
_STANDARD_REF_RE = re.compile(r"^standard-basis:[0-9a-f]{64}$")
_PLAN_REF_RE = re.compile(r"^procedure-plan:[0-9a-f]{64}$")
_TEST_REF_RE = re.compile(r"^proposed-test:[0-9a-f]{64}$")
_COMBINATION_REF_RE = re.compile(r"^test-combination:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CID_RE = re.compile(r"^(KSA|KIFRS)::([^:]+)::(.+)$")
_RESEARCH_REF_RE = re.compile(r"^research:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")
_WORKBOOK_KINDS = {
    "account", "risk", "assertion", "procedure", "gap", "relation", "other",
}
_WORKBOOK_SOURCE_KINDS = {"fact", "relation", "statement", "source_record"}
_STANDARD_ORIGINS = {"prepared_citation", "ephemeral_research"}
_PARA_TYPES = {"정의", "참조", "부록", "요구사항", "적용지침", "본문"}
_SOURCE_TYPE = {"KSA": "감사기준", "KIFRS": "회계기준"}
_DOMAIN_FRAMEWORK = {"KSA": ("audit", "KSA"), "KIFRS": ("accounting", "K-IFRS")}
_ABSTENTION_CODES = {"insufficient_basis", "ambiguous_target", "planning_not_supported"}
_LIMITATIONS = (
    "추천 test 후보는 가능한 선택지의 비완전 목록이며 감사계획 전체를 대신하지 않습니다.",
    "모든 후보는 proposed·unreviewed이며 조서에 수행되었다는 증거가 없습니다.",
    "표본 수, 금액 기준, 선정 간격과 구체적 범위는 TBD이며 감사인의 별도 판단이 필요합니다.",
    "기준서 근거는 원칙이나 고려사항을 설명하며 개별 추천 test가 반드시 요구된다는 뜻이 아닙니다.",
)

# Free-form prose must not smuggle a quantitative sample or amount design around the fixed TBD
# fields.  Ordinary standard numbers and dates remain allowed; only an extent/amount expression
# is rejected.
_QUANTITATIVE_DESIGN_PATTERNS = (
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),
    re.compile(r"(?:표본(?:\s*(?:수|크기))?|샘플)\s*(?::|=|은|는|으로)?\s*\d+", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:건|개|항목)\s*(?:을|를|의)?\s*(?:표본|샘플|선정|추출)"),
    re.compile(r"(?:₩|\$|KRW\s*)\s*\d", re.I),
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(?:원|천원|만원|백만원|억원)\b"),
    re.compile(r"(?:금액\s*(?:기준|임계치|한도)|threshold)\s*(?::|=|은|는)?\s*\d", re.I),
    re.compile(r"(?:매|간격\s*)\s*\d+\s*(?:번째|건|개|항목)"),
)


class ProcedurePlanningError(RuntimeError):
    """A proposed-test planning request failed without creating audit authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProcedurePlanningInput(TypedDict):
    request: dict


class ProcedurePlanningState(TypedDict, total=False):
    request: dict
    worker_output: dict
    route: str
    result: dict


class ProcedurePlanningOutput(TypedDict):
    result: dict


@dataclass(frozen=True, slots=True)
class ProcedurePlanningRuntime:
    """Dependencies and exact committed-scope binding kept outside graph state."""

    client: object
    model: str
    invocation_id: str
    bundle_sha256: str
    scope: dict
    eprint: object | None = None


def _canonical_without_ref(value: Mapping[str, object], ref_field: str) -> dict:
    return {key: copy.deepcopy(item) for key, item in value.items() if key != ref_field}


def workbook_basis_ref(value: Mapping[str, object]) -> str:
    """Return the opaque ref for one canonical workbook-basis wrapper."""
    return "workbook-basis:" + json_sha256(_canonical_without_ref(value, "basis_ref"))


def standard_basis_ref(value: Mapping[str, object]) -> str:
    """Return the opaque ref for one canonical standards-basis wrapper."""
    return "standard-basis:" + json_sha256(_canonical_without_ref(value, "basis_ref"))


def _scope(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("INVALID_REQUEST", "planning scope가 객체가 아닙니다.")
    scope = dict(value)
    if scope.get("kind") == "workbook" and set(scope) == {"kind"}:
        return {"kind": "workbook"}
    if scope.get("kind") == "sheet" and set(scope) == {"kind", "sheet", "id"}:
        sheet = scope.get("sheet")
        if (
            isinstance(sheet, str)
            and bool(sheet)
            and scope.get("id") == hashlib.sha256(sheet.encode("utf-8")).hexdigest()
        ):
            return copy.deepcopy(scope)
    raise ProcedurePlanningError("INVALID_REQUEST", "planning scope identity가 유효하지 않습니다.")


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProcedurePlanningError("INVALID_REQUEST", f"{field}가 유효하지 않습니다.")
    return value


def _workbook_basis(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("INVALID_REQUEST", "workbook basis가 객체가 아닙니다.")
    required = {
        "typed_kind", "basis_ref", "scope", "source_kind", "record_kind",
        "source_ref", "text", "status", "confidence",
    }
    if set(value) != required:
        raise ProcedurePlanningError("INVALID_REQUEST", "workbook basis 필드가 유효하지 않습니다.")
    source_ref = value.get("source_ref")
    status = value.get("status")
    confidence = value.get("confidence")
    record = {
        "typed_kind": value.get("typed_kind"),
        "basis_ref": value.get("basis_ref"),
        "scope": _scope(value.get("scope")),
        "source_kind": value.get("source_kind"),
        "record_kind": value.get("record_kind"),
        "source_ref": source_ref,
        "text": _text(value.get("text"), field="workbook basis text", maximum=8_000),
        "status": status,
        "confidence": confidence,
    }
    if (
        record["typed_kind"] != "planning_workbook_basis"
        or not isinstance(record["basis_ref"], str)
        or _WORKBOOK_REF_RE.fullmatch(record["basis_ref"]) is None
        or record["source_kind"] not in _WORKBOOK_SOURCE_KINDS
        or record["record_kind"] not in _WORKBOOK_KINDS
        or not isinstance(source_ref, str)
        or not source_ref
        or len(source_ref) > 256
        or not (status is None or isinstance(status, str) and bool(status))
        or not (
            confidence is None
            or isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
        )
        or record["basis_ref"] != workbook_basis_ref(record)
    ):
        raise ProcedurePlanningError("INVALID_REQUEST", "workbook basis 계약이 유효하지 않습니다.")
    return record


def _standard_basis(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("INVALID_REQUEST", "standard basis가 객체가 아닙니다.")
    required = {
        "typed_kind", "basis_ref", "scope", "origin", "source_ref", "collection",
        "cid", "domain", "framework", "source_type", "standard_no",
        "standard_title", "para_no", "para_type", "section_path", "text",
        "text_sha256", "effective_date_verified", "verified_by",
    }
    if set(value) != required:
        raise ProcedurePlanningError("INVALID_REQUEST", "standard basis 필드가 유효하지 않습니다.")
    cid = value.get("cid")
    match = _CID_RE.fullmatch(cid) if isinstance(cid, str) else None
    prefix, standard_no, para_no = match.groups() if match is not None else (None,) * 3
    expected_domain, expected_framework = _DOMAIN_FRAMEWORK.get(prefix, (None, None))
    source_ref = value.get("source_ref")
    origin = value.get("origin")
    text = _text(value.get("text"), field="standard basis text", maximum=40_000)
    record = {
        "typed_kind": value.get("typed_kind"),
        "basis_ref": value.get("basis_ref"),
        "scope": _scope(value.get("scope")),
        "origin": origin,
        "source_ref": source_ref,
        "collection": value.get("collection"),
        "cid": cid,
        "domain": value.get("domain"),
        "framework": value.get("framework"),
        "source_type": value.get("source_type"),
        "standard_no": str(value.get("standard_no")),
        "standard_title": value.get("standard_title"),
        "para_no": str(value.get("para_no")),
        "para_type": value.get("para_type"),
        "section_path": value.get("section_path"),
        "text": text,
        "text_sha256": value.get("text_sha256"),
        "effective_date_verified": value.get("effective_date_verified"),
        "verified_by": value.get("verified_by"),
    }
    source_ref_valid = (
        isinstance(source_ref, str)
        and (
            _RESEARCH_REF_RE.fullmatch(source_ref) is not None
            if origin == "ephemeral_research"
            else _IDENTIFIER_RE.fullmatch(source_ref) is not None
        )
    )
    if (
        record["typed_kind"] != "planning_standard_basis"
        or not isinstance(record["basis_ref"], str)
        or _STANDARD_REF_RE.fullmatch(record["basis_ref"]) is None
        or origin not in _STANDARD_ORIGINS
        or not source_ref_valid
        or not isinstance(record["collection"], str)
        or not record["collection"]
        or match is None
        or record["domain"] != expected_domain
        or record["framework"] != expected_framework
        or record["source_type"] != _SOURCE_TYPE.get(prefix)
        or record["standard_no"] != standard_no
        or record["para_no"] != para_no
        or not isinstance(record["standard_title"], str)
        or not record["standard_title"]
        or record["para_type"] not in _PARA_TYPES
        or not (
            record["section_path"] is None or isinstance(record["section_path"], str)
        )
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != record["text_sha256"]
        or not isinstance(record["effective_date_verified"], bool)
        or record["verified_by"] != "standards_get_paragraph"
        or record["basis_ref"] != standard_basis_ref(record)
    ):
        raise ProcedurePlanningError("INVALID_REQUEST", "standard basis 계약이 유효하지 않습니다.")
    return record


def _clean_request(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("INVALID_REQUEST", "planning request가 객체가 아닙니다.")
    if set(value) != {
        "objective", "target", "workbook_basis", "standards_basis",
        "existing_procedure_refs", "candidate_count",
    }:
        raise ProcedurePlanningError("INVALID_REQUEST", "planning request 필드가 유효하지 않습니다.")
    objective_value = value.get("objective")
    if not isinstance(objective_value, str) or not objective_value.strip():
        raise ProcedurePlanningError("INVALID_REQUEST", "planning objective가 비어 있습니다.")
    objective = " ".join(objective_value.split())
    if len(objective) > 500:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "planning objective는 500자 이하여야 합니다.")
    workbook_values = value.get("workbook_basis")
    standards_values = value.get("standards_basis")
    existing = value.get("existing_procedure_refs")
    if (
        not isinstance(workbook_values, list)
        or not 2 <= len(workbook_values) <= MAX_WORKBOOK_BASIS
        or not isinstance(standards_values, list)
        or len(standards_values) > MAX_STANDARDS_BASIS
        or not isinstance(existing, list)
        or len(existing) > MAX_EXISTING_PROCEDURES
    ):
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "planning basis 수가 상한을 벗어났습니다.")
    workbook = [_workbook_basis(item) for item in workbook_values]
    standards = [_standard_basis(item) for item in standards_values]
    workbook_by_ref = {item["basis_ref"]: item for item in workbook}
    standard_refs = [item["basis_ref"] for item in standards]
    if len(workbook_by_ref) != len(workbook) or len(set(standard_refs)) != len(standards):
        raise ProcedurePlanningError("INVALID_REQUEST", "planning basis ref가 중복되었습니다.")
    scopes = [item["scope"] for item in workbook + standards]
    if not scopes or any(scope != scopes[0] for scope in scopes[1:]):
        raise ProcedurePlanningError("INVALID_REQUEST", "planning basis scope가 서로 다릅니다.")
    collections = {item["collection"] for item in standards}
    if len(collections) > 1:
        raise ProcedurePlanningError("INVALID_REQUEST", "planning standards collection이 다릅니다.")
    target = value.get("target")
    if not isinstance(target, Mapping) or set(target) != {
        "account_ref", "risk_ref", "assertion_ref"
    }:
        raise ProcedurePlanningError("INVALID_REQUEST", "planning target이 유효하지 않습니다.")
    target_clean = {
        "account_ref": target.get("account_ref"),
        "risk_ref": target.get("risk_ref"),
        "assertion_ref": target.get("assertion_ref"),
    }
    expected_kinds = {
        "account_ref": "account", "risk_ref": "risk", "assertion_ref": "assertion"
    }
    for field, kind in expected_kinds.items():
        ref = target_clean[field]
        if field == "account_ref" and ref is None:
            continue
        if (
            not isinstance(ref, str)
            or ref not in workbook_by_ref
            or workbook_by_ref[ref]["record_kind"] != kind
        ):
            raise ProcedurePlanningError("INVALID_REQUEST", f"planning {field}가 관찰 근거와 다릅니다.")
    if not isinstance(existing, list) or len(existing) != len(set(existing)):
        raise ProcedurePlanningError("INVALID_REQUEST", "existing procedure ref가 중복되었습니다.")
    for ref in existing:
        if (
            not isinstance(ref, str)
            or ref not in workbook_by_ref
            or workbook_by_ref[ref]["record_kind"] != "procedure"
        ):
            raise ProcedurePlanningError("INVALID_REQUEST", "existing procedure ref가 유효하지 않습니다.")
    candidate_count = value.get("candidate_count")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or not MIN_CANDIDATES <= candidate_count <= MAX_CANDIDATES
    ):
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "candidate_count는 3~5여야 합니다.")
    result = {
        "objective": objective,
        "target": target_clean,
        "workbook_basis": workbook,
        "standards_basis": standards,
        "existing_procedure_refs": list(existing),
        "candidate_count": candidate_count,
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "planning request byte 상한을 초과했습니다.")
    return result


def _context(runtime) -> ProcedurePlanningRuntime:
    value = getattr(runtime, "context", None)
    if not isinstance(value, ProcedurePlanningRuntime):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning runtime context가 없습니다.")
    return value


def _bind_request(state: ProcedurePlanningState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    if (
        not isinstance(context.invocation_id, str)
        or not context.invocation_id
        or not isinstance(context.model, str)
        or not context.model
        or _SHA256_RE.fullmatch(context.bundle_sha256) is None
        or _scope(context.scope) != request["workbook_basis"][0]["scope"]
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning runtime binding이 유효하지 않습니다.")
    return {
        "request": request,
        "route": "plan" if request["standards_basis"] else "no_plan",
    }


def _after_bind(state: ProcedurePlanningState) -> str:
    return "plan_candidates" if state.get("route") == "plan" else "materialize_no_plan"


def _provider_worker_schema(strict_schema: dict) -> dict:
    schema = copy.deepcopy(strict_schema)
    schema.pop("allOf", None)
    return schema


def _all_strings(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            result.extend(_all_strings(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_all_strings(item))
    return result


def _validate_worker_output(value: object, request: Mapping[str, object]) -> dict:
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning worker 결과가 객체가 아닙니다.")
    output = copy.deepcopy(dict(value))
    try:
        jsonschema.validate(output, load_schema(PLANNING_WORKER_SCHEMA))
    except (jsonschema.ValidationError, AuditLLMError) as e:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning worker schema가 유효하지 않습니다.") from e
    abstained = output.get("abstained")
    candidates = output.get("candidates")
    combinations = output.get("recommended_combinations")
    if not isinstance(candidates, list) or not isinstance(combinations, list):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning worker 후보가 배열이 아닙니다.")
    if abstained is True:
        if (
            output.get("abstention_code") not in _ABSTENTION_CODES
            or candidates
            or combinations
        ):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning abstention 계약이 다릅니다.")
        return output
    if abstained is not False or output.get("abstention_code") is not None:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning completion 상태가 유효하지 않습니다.")
    expected_count = request.get("candidate_count")
    if len(candidates) != expected_count or not MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning 후보 수가 요청과 다릅니다.")
    keys: list[str] = []
    titles: set[str] = set()
    roles: list[str] = []
    workbook_refs = {item["basis_ref"] for item in request["workbook_basis"]}
    standards_refs = {item["basis_ref"] for item in request["standards_basis"]}
    required_workbook = {
        request["target"]["risk_ref"], request["target"]["assertion_ref"]
    }
    if request["target"]["account_ref"] is not None:
        required_workbook.add(request["target"]["account_ref"])
    existing = set(request["existing_procedure_refs"])
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, Mapping):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning 후보가 객체가 아닙니다.")
        key = candidate.get("candidate_key")
        title = candidate.get("title")
        role = candidate.get("portfolio_role")
        selected_workbook = candidate.get("workbook_basis_refs")
        selected_standards = candidate.get("standards_basis_refs")
        selected_existing = candidate.get("documented_procedure_basis_refs")
        if (
            key != f"T{index}"
            or candidate.get("rank") != index
            or not isinstance(title, str)
            or title.casefold().strip() in titles
            or not isinstance(selected_workbook, list)
            or not required_workbook.issubset(selected_workbook)
            or len(selected_workbook) != len(set(selected_workbook))
            or any(ref not in workbook_refs for ref in selected_workbook)
            or not isinstance(selected_standards, list)
            or len(selected_standards) != len(set(selected_standards))
            or any(ref not in standards_refs for ref in selected_standards)
            or not isinstance(selected_existing, list)
            or len(selected_existing) != len(set(selected_existing))
            or any(ref not in existing for ref in selected_existing)
        ):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning 후보 basis/identity가 유효하지 않습니다.")
        support = candidate.get("standard_support")
        if (support == "none") != (not selected_standards):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "standard support와 refs가 다릅니다.")
        relationship = candidate.get("relationship_to_documented")
        if relationship in {
            "alternative_to_documented", "complements_documented", "overlaps_documented"
        } and not selected_existing:
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "documented procedure 관계 근거가 없습니다.")
        quantitative = candidate.get("quantitative_design")
        if quantitative != {
            "sample_size": "TBD", "amount_threshold": "TBD", "selection_interval": "TBD"
        }:
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "정량 planning 값은 TBD여야 합니다.")
        keys.append(key)
        titles.add(title.casefold().strip())
        roles.append(role)
    if (
        roles.count("primary") != 1
        or roles.count("alternative") < 1
        or roles.count("complementary") < 1
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "planning 후보 역할 구성이 유효하지 않습니다.")
    if not 1 <= len(combinations) <= MAX_COMBINATIONS:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "추천 조합은 1~3개여야 합니다.")
    seen_sets: set[tuple[str, ...]] = set()
    for index, combination in enumerate(combinations, start=1):
        if not isinstance(combination, Mapping):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "추천 조합이 객체가 아닙니다.")
        selected = combination.get("candidate_keys")
        canonical = tuple(sorted(selected)) if isinstance(selected, list) else ()
        if (
            combination.get("combination_key") != f"C{index}"
            or not isinstance(selected, list)
            or not 2 <= len(selected) <= len(candidates)
            or len(selected) != len(set(selected))
            or any(key not in keys for key in selected)
            or canonical in seen_sets
        ):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "추천 조합 refs가 유효하지 않습니다.")
        seen_sets.add(canonical)
    for text in _all_strings(output):
        if any(pattern.search(text) for pattern in _QUANTITATIVE_DESIGN_PATTERNS):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "정량 표본/금액 설계는 작성할 수 없습니다.")
    if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > MAX_WORKER_BYTES:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "planning worker 결과 상한을 초과했습니다.")
    return output


def _plan_candidates(state: ProcedurePlanningState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    prompt, _ = load_prompt(PLANNING_PROMPT)
    schema = load_schema(PLANNING_WORKER_SCHEMA)
    payload = {
        "objective": request["objective"],
        "target": copy.deepcopy(request["target"]),
        "workbook_basis": copy.deepcopy(request["workbook_basis"]),
        "standards_basis": copy.deepcopy(request["standards_basis"]),
        "existing_procedure_refs": list(request["existing_procedure_refs"]),
        "limits": {
            "candidate_count": request["candidate_count"],
            "min_candidates": MIN_CANDIDATES,
            "max_candidates": MAX_CANDIDATES,
            "max_combinations": MAX_COMBINATIONS,
            "max_steps_per_candidate": 6,
            "quantitative_design": "TBD_only",
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES + 8_000:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "planning worker 입력 상한을 초과했습니다.")
    try:
        worker = call_json(
            context.client,
            system=prompt,
            user=encoded,
            schema=_provider_worker_schema(schema),
            validation_schema=schema,
            label="audit procedure planning worker",
            retries=0,
            eprint=context.eprint or (lambda *args: None),
        )
    except AuditLLMError as e:
        # ``call_json`` preserves provider/transport failures as the cause of its
        # AuditLLMError.  Invalid model JSON/schema output is exhausted inside that
        # boundary and has no cause.  Keep availability failures distinct from a
        # model response that violated the planning contract.
        code = "UPSTREAM_UNAVAILABLE" if e.__cause__ is not None else "CONTRACT_MISMATCH"
        raise ProcedurePlanningError(code, "planning worker를 완료하지 못했습니다.") from e
    except AuditModelError as e:
        raise ProcedurePlanningError(
            "CONTRACT_MISMATCH", "planning worker 계약을 검증하지 못했습니다."
        ) from e
    worker = _validate_worker_output(worker, request)
    return {
        "worker_output": worker,
        "route": "no_plan" if worker["abstained"] else "completed",
    }


def _after_worker(state: ProcedurePlanningState) -> str:
    return "materialize_no_plan" if state.get("route") == "no_plan" else "materialize_plan"


def _request_witness(request: Mapping[str, object]) -> dict:
    return {
        "objective": request["objective"],
        "target": copy.deepcopy(request["target"]),
        "candidate_count": request["candidate_count"],
        "existing_procedure_refs": list(request["existing_procedure_refs"]),
        "workbook_basis_refs": [item["basis_ref"] for item in request["workbook_basis"]],
        "standards_basis_refs": [item["basis_ref"] for item in request["standards_basis"]],
    }


def _binding(
    context: ProcedurePlanningRuntime,
    worker: dict | None,
    worker_witness: dict,
) -> dict:
    return {
        "invocation_sha256": hashlib.sha256(
            context.invocation_id.encode("utf-8")
        ).hexdigest(),
        "bundle_sha256": context.bundle_sha256,
        "worker_output_sha256": json_sha256(worker) if worker is not None else None,
        "worker_witness_sha256": json_sha256(worker_witness),
    }


def _plan_ref(binding: dict, scope: dict, request_witness: dict) -> str:
    return "procedure-plan:" + json_sha256({
        "binding": binding,
        "scope": scope,
        "request": request_witness,
    })


def _worker_witness(
    context: ProcedurePlanningRuntime,
    *,
    called: bool,
    abstained: bool,
    abstention_code: str | None,
) -> dict:
    _, prompt_sha = load_prompt(PLANNING_PROMPT)
    return {
        "name": "excel_to_skill.audit.procedure_planning",
        "version": PLANNING_VERSION,
        "model": context.model,
        "prompt_sha256": prompt_sha,
        "called": called,
        "abstained": abstained,
        "abstention_code": abstention_code,
    }


def _base_document(
    context: ProcedurePlanningRuntime,
    request: dict,
    worker: dict | None,
    *,
    status: str,
    called: bool,
    abstention_code: str | None,
) -> dict:
    worker_witness = _worker_witness(
        context,
        called=called,
        abstained=status == "no_plan",
        abstention_code=abstention_code,
    )
    binding = _binding(context, worker, worker_witness)
    request_witness = _request_witness(request)
    return {
        "schema_version": PLANNING_RESULT_SCHEMA,
        "status": status,
        "plan_ref": _plan_ref(binding, context.scope, request_witness),
        "binding": binding,
        "proposal_status": "proposed",
        "review_status": "unreviewed",
        "execution_evidence_status": "not_evidenced",
        "candidate_set_status": "non_exhaustive",
        "turn_scoped": True,
        "outside_prepared_bundle": True,
        "scope": copy.deepcopy(context.scope),
        "request": request_witness,
        "worker": worker_witness,
        "basis_catalog": {
            "workbook": copy.deepcopy(request["workbook_basis"]),
            "standards": copy.deepcopy(request["standards_basis"]),
        },
        "candidates": [],
        "recommended_combinations": [],
        "assumptions": copy.deepcopy(worker.get("assumptions", []) if worker else []),
        "open_questions": copy.deepcopy(worker.get("open_questions", []) if worker else []),
        "limitations": list(_LIMITATIONS),
    }


def _materialize_plan(state: ProcedurePlanningState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    worker = _validate_worker_output(state.get("worker_output"), request)
    if worker["abstained"]:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "completed route에 abstention이 있습니다.")
    document = _base_document(
        context, request, worker, status="completed", called=True, abstention_code=None
    )
    plan_ref = document["plan_ref"]
    key_to_ref: dict[str, str] = {}
    for candidate in worker["candidates"]:
        candidate_copy = copy.deepcopy(candidate)
        candidate_ref = "proposed-test:" + json_sha256({
            "plan_ref": plan_ref,
            "candidate": candidate_copy,
        })
        key_to_ref[candidate_copy["candidate_key"]] = candidate_ref
        document["candidates"].append({
            "typed_kind": "proposed_test",
            "proposed_test_ref": candidate_ref,
            "proposal_status": "proposed",
            "review_status": "unreviewed",
            "execution_evidence_status": "not_evidenced",
            **candidate_copy,
        })
    for combination in worker["recommended_combinations"]:
        combination_copy = copy.deepcopy(combination)
        refs = [key_to_ref[key] for key in combination_copy["candidate_keys"]]
        document["recommended_combinations"].append({
            "combination_ref": "test-combination:" + json_sha256({
                "plan_ref": plan_ref,
                "combination": combination_copy,
                "proposed_test_refs": refs,
            }),
            **combination_copy,
            "proposed_test_refs": refs,
        })
    validate_procedure_plan(document)
    return {"result": document, "route": "completed"}


def _materialize_no_plan(state: ProcedurePlanningState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    worker_value = state.get("worker_output")
    if worker_value is None:
        worker = None
        called = False
        code = "insufficient_basis"
    else:
        worker = _validate_worker_output(worker_value, request)
        if worker["abstained"] is not True:
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "no_plan route에 완료 후보가 있습니다.")
        called = True
        code = worker["abstention_code"]
    document = _base_document(
        context, request, worker, status="no_plan", called=called, abstention_code=code
    )
    validate_procedure_plan(document)
    return {"result": document, "route": "completed"}


def validate_procedure_plan(value: object) -> dict:
    """Validate a self-contained private proposed-test result."""
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan 결과가 객체가 아닙니다.")
    required = {
        "schema_version", "status", "plan_ref", "binding", "proposal_status", "review_status",
        "execution_evidence_status", "candidate_set_status", "turn_scoped",
        "outside_prepared_bundle", "scope", "request", "worker", "basis_catalog",
        "candidates", "recommended_combinations", "assumptions", "open_questions",
        "limitations",
    }
    document = copy.deepcopy(dict(value))
    if (
        set(document) != required
        or document.get("schema_version") != PLANNING_RESULT_SCHEMA
        or document.get("status") not in {"completed", "no_plan"}
        or not isinstance(document.get("plan_ref"), str)
        or _PLAN_REF_RE.fullmatch(document["plan_ref"]) is None
        or document.get("proposal_status") != "proposed"
        or document.get("review_status") != "unreviewed"
        or document.get("execution_evidence_status") != "not_evidenced"
        or document.get("candidate_set_status") != "non_exhaustive"
        or document.get("turn_scoped") is not True
        or document.get("outside_prepared_bundle") is not True
        or document.get("limitations") != list(_LIMITATIONS)
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan trust 경계가 유효하지 않습니다.")
    scope = _scope(document.get("scope"))
    binding = document.get("binding")
    catalog = document.get("basis_catalog")
    request_witness = document.get("request")
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {
            "invocation_sha256", "bundle_sha256", "worker_output_sha256",
            "worker_witness_sha256",
        }
        or _SHA256_RE.fullmatch(str(binding.get("invocation_sha256"))) is None
        or _SHA256_RE.fullmatch(str(binding.get("bundle_sha256"))) is None
        or not (
            binding.get("worker_output_sha256") is None
            or _SHA256_RE.fullmatch(str(binding.get("worker_output_sha256")))
            is not None
        )
        or _SHA256_RE.fullmatch(str(binding.get("worker_witness_sha256"))) is None
        or not isinstance(catalog, Mapping)
        or set(catalog) != {"workbook", "standards"}
        or not isinstance(request_witness, Mapping)
        or set(request_witness) != {
            "objective", "target", "candidate_count", "existing_procedure_refs",
            "workbook_basis_refs", "standards_basis_refs",
        }
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan basis witness가 유효하지 않습니다.")
    reconstructed = _clean_request({
        "target": request_witness.get("target"),
        "objective": request_witness.get("objective"),
        "workbook_basis": catalog.get("workbook"),
        "standards_basis": catalog.get("standards"),
        "existing_procedure_refs": request_witness.get("existing_procedure_refs"),
        "candidate_count": request_witness.get("candidate_count"),
    })
    if (
        scope != reconstructed["workbook_basis"][0]["scope"]
        or request_witness != _request_witness(reconstructed)
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan request witness가 basis와 다릅니다.")
    worker = document.get("worker")
    if not isinstance(worker, Mapping) or set(worker) != {
        "name", "version", "model", "prompt_sha256", "called", "abstained",
        "abstention_code",
    }:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan worker witness가 없습니다.")
    if (
        worker.get("name") != "excel_to_skill.audit.procedure_planning"
        or worker.get("version") != PLANNING_VERSION
        or not isinstance(worker.get("model"), str)
        or not worker.get("model")
        or not isinstance(worker.get("prompt_sha256"), str)
        or _SHA256_RE.fullmatch(worker["prompt_sha256"]) is None
        or not isinstance(worker.get("called"), bool)
        or not isinstance(worker.get("abstained"), bool)
        or not (
            worker.get("abstention_code") is None
            or worker.get("abstention_code") in _ABSTENTION_CODES
        )
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan worker 값이 유효하지 않습니다.")
    candidates = document.get("candidates")
    combinations = document.get("recommended_combinations")
    if not isinstance(candidates, list) or not isinstance(combinations, list):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan 후보/조합이 배열이 아닙니다.")
    reconstructed_worker: dict | None
    if document["status"] == "no_plan":
        if (
            candidates
            or combinations
            or worker.get("abstained") is not True
            or worker.get("abstention_code") not in _ABSTENTION_CODES
            or (worker.get("called") is False and reconstructed["standards_basis"])
        ):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "no_plan 상태가 worker/basis와 다릅니다.")
        if worker.get("called") is True:
            reconstructed_worker = {
                "abstained": True,
                "abstention_code": worker.get("abstention_code"),
                "assumptions": document.get("assumptions"),
                "open_questions": document.get("open_questions"),
                "candidates": [],
                "recommended_combinations": [],
            }
            _validate_worker_output(reconstructed_worker, reconstructed)
        else:
            reconstructed_worker = None
            if document.get("assumptions") != [] or document.get("open_questions") != []:
                raise ProcedurePlanningError("CONTRACT_MISMATCH", "pre-child no_plan에 worker 내용이 있습니다.")
    else:
        if (
            worker.get("called") is not True
            or worker.get("abstained") is not False
            or worker.get("abstention_code") is not None
        ):
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "completed plan worker 상태가 다릅니다.")
        stripped_candidates: list[dict] = []
        refs_by_key: dict[str, str] = {}
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise ProcedurePlanningError("CONTRACT_MISMATCH", "materialized candidate가 객체가 아닙니다.")
            candidate_copy = dict(candidate)
            ref = candidate_copy.pop("proposed_test_ref", None)
            if (
                candidate_copy.pop("typed_kind", None) != "proposed_test"
                or candidate_copy.pop("proposal_status", None) != "proposed"
                or candidate_copy.pop("review_status", None) != "unreviewed"
                or candidate_copy.pop("execution_evidence_status", None) != "not_evidenced"
                or not isinstance(ref, str)
                or _TEST_REF_RE.fullmatch(ref) is None
                or ref in refs_by_key.values()
                or ref != "proposed-test:" + json_sha256({
                    "plan_ref": document["plan_ref"],
                    "candidate": candidate_copy,
                })
            ):
                raise ProcedurePlanningError("CONTRACT_MISMATCH", "materialized candidate trust 경계가 다릅니다.")
            refs_by_key[str(candidate_copy.get("candidate_key"))] = ref
            stripped_candidates.append(candidate_copy)
        stripped_combinations: list[dict] = []
        seen_combination_refs: set[str] = set()
        for combination in combinations:
            if not isinstance(combination, Mapping):
                raise ProcedurePlanningError("CONTRACT_MISMATCH", "materialized combination이 객체가 아닙니다.")
            combination_copy = dict(combination)
            ref = combination_copy.pop("combination_ref", None)
            proposed_refs = combination_copy.pop("proposed_test_refs", None)
            keys = combination_copy.get("candidate_keys")
            if (
                not isinstance(ref, str)
                or _COMBINATION_REF_RE.fullmatch(ref) is None
                or ref in seen_combination_refs
                or not isinstance(keys, list)
                or proposed_refs != [refs_by_key.get(key) for key in keys]
                or ref != "test-combination:" + json_sha256({
                    "plan_ref": document["plan_ref"],
                    "combination": combination_copy,
                    "proposed_test_refs": proposed_refs,
                })
            ):
                raise ProcedurePlanningError("CONTRACT_MISMATCH", "materialized combination refs가 다릅니다.")
            seen_combination_refs.add(ref)
            stripped_combinations.append(combination_copy)
        reconstructed_worker = {
            "abstained": False,
            "abstention_code": None,
            "assumptions": document.get("assumptions"),
            "open_questions": document.get("open_questions"),
            "candidates": stripped_candidates,
            "recommended_combinations": stripped_combinations,
        }
        _validate_worker_output(reconstructed_worker, reconstructed)
    if (
        binding.get("worker_output_sha256")
        != (json_sha256(reconstructed_worker) if reconstructed_worker is not None else None)
        or binding.get("worker_witness_sha256") != json_sha256(dict(worker))
        or document["plan_ref"] != _plan_ref(
            dict(binding), scope, dict(request_witness)
        )
    ):
        raise ProcedurePlanningError(
            "CONTRACT_MISMATCH", "procedure plan binding witness가 본문과 다릅니다."
        )
    if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "procedure plan 결과 상한을 초과했습니다.")
    return document


def procedure_plan_records(value: object) -> dict[str, dict]:
    document = validate_procedure_plan(value)
    return {item["proposed_test_ref"]: item for item in document["candidates"]}


def procedure_plan_summary(
    observations: list[dict],
    *,
    selected_refs: list[str],
) -> dict | None:
    """Return the one current-turn plan selected by its exact typed observation ref."""
    if (
        not isinstance(selected_refs, list)
        or len(selected_refs) > 1
        or len(selected_refs) != len(set(selected_refs))
        or any(
            not isinstance(ref, str) or _PLAN_REF_RE.fullmatch(ref) is None
            for ref in selected_refs
        )
    ):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "selected procedure plan refs가 유효하지 않습니다.")
    plans: list[dict] = []
    for observation in observations:
        if not isinstance(observation, Mapping) or observation.get("tool") != "procedure_planning":
            continue
        result = observation.get("result")
        if isinstance(result, Mapping) and result.get("schema_version") == PLANNING_RESULT_SCHEMA:
            plans.append(validate_procedure_plan(result))
    if len(plans) > 1:
        raise ProcedurePlanningError("LIMIT_EXCEEDED", "한 turn의 procedure plan은 1회만 허용됩니다.")
    if not plans:
        if selected_refs:
            raise ProcedurePlanningError("CONTRACT_MISMATCH", "선택된 procedure plan observation이 없습니다.")
        return None
    if not selected_refs:
        return None
    plan = plans[0]
    if selected_refs != [plan["plan_ref"]]:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "관찰되지 않은 procedure plan ref가 선택되었습니다.")
    return copy.deepcopy(plan)


def validate_procedure_plan_summary(
    value: object,
    *,
    observations: list[dict],
) -> dict:
    """Exact-compare a response supplement with its private typed plan observation."""
    if not isinstance(value, Mapping):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan summary가 객체가 아닙니다.")
    plan_ref = value.get("plan_ref")
    if not isinstance(plan_ref, str):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan summary ref가 없습니다.")
    expected = procedure_plan_summary(observations, selected_refs=[plan_ref])
    if expected is None or dict(value) != expected:
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure plan summary가 observation과 다릅니다.")
    return copy.deepcopy(dict(value))


def build_procedure_planning_graph():
    """Compile the raw planning worker without inheriting a parent checkpointer."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise ProcedurePlanningError(
            "UPSTREAM_UNAVAILABLE", "procedure planning에는 graph extra가 필요합니다."
        ) from e
    builder = StateGraph(
        ProcedurePlanningState,
        context_schema=ProcedurePlanningRuntime,
        input_schema=ProcedurePlanningInput,
        output_schema=ProcedurePlanningOutput,
    )
    builder.add_node("bind_request", _bind_request)
    builder.add_node("plan_candidates", _plan_candidates)
    builder.add_node("materialize_plan", _materialize_plan)
    builder.add_node("materialize_no_plan", _materialize_no_plan)
    builder.add_edge(START, "bind_request")
    builder.add_conditional_edges(
        "bind_request",
        _after_bind,
        {
            "plan_candidates": "plan_candidates",
            "materialize_no_plan": "materialize_no_plan",
        },
    )
    builder.add_conditional_edges(
        "plan_candidates",
        _after_worker,
        {
            "materialize_plan": "materialize_plan",
            "materialize_no_plan": "materialize_no_plan",
        },
    )
    builder.add_edge("materialize_plan", END)
    builder.add_edge("materialize_no_plan", END)
    return builder.compile(checkpointer=False, name="audit_procedure_planning")


def run_procedure_planning(
    request: Mapping[str, object],
    *,
    runtime: ProcedurePlanningRuntime,
) -> dict:
    """Run one isolated planning request and return the bounded proposed-test result."""
    graph = build_procedure_planning_graph()
    try:
        result = graph.invoke({"request": copy.deepcopy(dict(request))}, context=runtime)
    except ProcedurePlanningError:
        raise
    except Exception as e:  # noqa: BLE001 - nested graph boundary
        cause = e.__cause__
        if isinstance(cause, ProcedurePlanningError):
            raise cause
        raise ProcedurePlanningError(
            "UPSTREAM_UNAVAILABLE", "procedure planning graph가 완료되지 않았습니다."
        ) from e
    if not isinstance(result, Mapping):
        raise ProcedurePlanningError("CONTRACT_MISMATCH", "procedure planning graph 결과가 객체가 아닙니다.")
    return validate_procedure_plan(result.get("result"))


__all__ = [
    "MAX_CANDIDATES",
    "MIN_CANDIDATES",
    "PLANNING_RESULT_SCHEMA",
    "PLANNING_VERSION",
    "ProcedurePlanningError",
    "ProcedurePlanningRuntime",
    "build_procedure_planning_graph",
    "procedure_plan_records",
    "procedure_plan_summary",
    "run_procedure_planning",
    "standard_basis_ref",
    "validate_procedure_plan",
    "validate_procedure_plan_summary",
    "workbook_basis_ref",
]
