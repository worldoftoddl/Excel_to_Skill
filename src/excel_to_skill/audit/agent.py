"""Grounded briefing agent over a committed audit package.

The model chooses among bounded read-only audit consumers and emits an ID-only answer plan.
Workbook cells and standards passage metadata are never trusted from model output: they are
hydrated deterministically with :func:`trace` after every cited ID has been validated.
"""
from __future__ import annotations

import copy
import json
import re
import html
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from .consume import (
    AuditConsumeError,
    _assertion_procedures_loaded,
    _audit_get_loaded,
    _audit_search_loaded,
    _brief_loaded,
    load_validated_audit_bundle,
    _trace_loaded,
)
from .contract import bundle_keys
from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .scope import (
    AuditScope,
    AuditScopeError,
    WORKBOOK_SCOPE,
    read_scope_commit,
    resolve_scope,
    sheet_model_context,
    scope_bundle_keys,
    validate_scope_commit,
)
from .sources import WorkbookSourceResolver


AGENT_VERSION = "0.3.0"  # scope-bound extractive selections and bounded hydration
AGENT_PROMPT = "audit_agent_v1.md"
AGENT_TURN_SCHEMA = "audit_agent_turn.schema.json"
AGENT_RESPONSE_SCHEMA = "audit_agent_response.schema.json"
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
DEFAULT_MAX_STEPS = 6
MAX_STEPS = 12
MAX_MODEL_CONTEXT_BYTES = 600_000
MAX_SELECTED_RECORDS = 60
MAX_EVIDENCE_RECORDS = 120
MAX_EVIDENCE_CELLS = 1000

_SECTION_TITLES = {
    "overview": "한눈에 보기",
    "scope": "조서 범위",
    "risks_assertions": "위험과 경영진 주장",
    "procedures": "감사절차",
    "results": "수행 결과",
    "conclusions": "결론",
    "open_items": "미해결·추가 검토 사항",
    "standards": "관련 기준서 문맥",
    "answer": "답변",
    "identity_scope": "조서 범위",
    "controls": "통제",
    "findings": "발견사항",
    "signoffs": "작성·검토",
}
_BASIS_LABELS = {
    "documented_fact": "조서 문서화 사실",
    "authoritative_context": "기준서 문맥",
    "synthesis": "조서·기준서 종합",
    "gap": "문서화 공백",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ABSTENTION_REASONS = {
    "insufficient_evidence": "관찰된 근거만으로 질문에 답할 수 없습니다.",
    "ambiguous_question": "질문의 범위가 모호하여 근거를 안전하게 선택할 수 없습니다.",
    "retrieval_incomplete": "근거 조회가 불완전하여 답변을 보류합니다.",
}


class AuditAgentError(RuntimeError):
    """The briefing agent could not produce a grounded response."""


@dataclass(frozen=True)
class _AuditAgentRuntime:
    """Validated, immutable inputs shared by every step of one agent turn."""

    path: Path
    facts: dict
    context: dict
    brief_doc: dict
    scope: AuditScope
    bundle: dict
    briefing: dict
    mappings: dict
    prompt: str
    prompt_sha: str
    schema: dict
    provider_schema: dict
    analysis_scope: dict | None
    resolver: WorkbookSourceResolver
    known: dict[str, set[str]]
    model: str
    question: str | None
    limit: int
    max_steps: int


@dataclass
class _AuditAgentTurnState:
    """Mutable control state for the bounded model/tool loop."""

    observations: list[dict]
    used_tools: list[str]
    observed: dict[str, set[str]]
    discovery_complete: bool
    seen_tool_requests: set[str]


def _provider_turn_schema(
    strict_schema: dict,
    *,
    include_research: bool = False,
    include_planning: bool = False,
    include_inspection: bool = False,
) -> dict:
    """Flatten nullable unions for Anthropic while retaining strict local validation."""
    schema = copy.deepcopy(strict_schema)
    schema.pop("allOf", None)
    definitions = schema["definitions"]
    identifier = definitions["identifier"]
    tool = definitions["toolRequest"]
    tool.pop("allOf", None)
    if not include_research:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "standards_research"
        ]
        tool["properties"]["kind"]["enum"] = [
            value for value in tool["properties"]["kind"]["enum"]
            if value not in {"audit_standard", "accounting_standard"}
        ]
        definitions["finalResponse"]["properties"].pop("research_refs", None)
    if not include_planning:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "procedure_planning"
        ]
        for name in (
            "fact_ids", "relation_ids", "standard_citation_ids", "research_refs",
        ):
            tool["properties"].pop(name, None)
        definitions["finalResponse"]["properties"].pop("plan_refs", None)
    if not include_inspection:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "workbook_inspection"
        ]
        for name in ("operation", "sheet", "range", "parameters"):
            tool["properties"].pop(name, None)
        definitions.pop("inspectionParameters", None)
        definitions["finalResponse"]["properties"].pop("inspection_refs", None)
    else:
        definitions["inspectionParameters"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "source": {"type": ["string", "null"]},
                "limit": {"type": ["integer", "null"]},
                "direction": {"type": ["string", "null"]},
                "header": {"type": ["boolean", "null"]},
                "columns": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "column": {"type": ["string", "null"]},
            },
        }
    definitions["toolRequest"]["properties"]["item_id"] = {
        "type": ["string", "null"],
        "pattern": identifier["pattern"],
    }

    for property_name, definition_name in (
        ("tool", "toolRequest"),
        ("final", "finalResponse"),
    ):
        nullable = copy.deepcopy(definitions[definition_name])
        nullable["type"] = ["object", "null"]
        schema["properties"][property_name] = nullable
    return schema


def _validated_response(document: dict) -> dict:
    try:
        jsonschema.validate(document, load_schema(AGENT_RESPONSE_SCHEMA))
    except (jsonschema.ValidationError, AuditLLMError) as e:
        raise AuditAgentError(f"audit agent 응답 계약 검증 실패: {e}") from e
    evidence = document["evidence"]
    coverage = document["coverage"]
    fact_ids = {item["fact_id"] for item in evidence["facts"]}
    relation_ids = {item["relation_id"] for item in evidence["relations"]}
    citation_ids = {item["citation_id"] for item in evidence["standards"]}
    problems: list[str] = []
    trust = document["trust"]
    expected_source_unreviewed = (
        trust["source_facts_review_status"] != "approved"
        or trust["source_brief_review_status"] != "approved"
    )
    if trust["source_unreviewed"] != expected_source_unreviewed:
        problems.append(
            "trust.source_unreviewed must reflect facts OR brief not approved"
        )
    for field, actual in (
        ("fact_count", len(fact_ids)),
        ("relation_count", len(relation_ids)),
        ("standards_citation_count", len(citation_ids)),
    ):
        if coverage[field] != actual:
            problems.append(f"coverage.{field}={coverage[field]} but evidence has {actual}")
    for index, claim in enumerate(document["answer"]["claims"]):
        for field, known in (
            ("fact_ids", fact_ids),
            ("relation_ids", relation_ids),
            ("standard_citation_ids", citation_ids),
        ):
            missing = [item_id for item_id in claim[field] if item_id not in known]
            if missing:
                problems.append(f"answer.claims[{index}].{field}: missing evidence {missing}")
    if not document["answer"]["abstained"]:
        traced_complete = all(
            item.get("trace_complete") is True
            for group in ("facts", "relations", "standards")
            for item in evidence[group]
        )
        if coverage["evidence_complete"] != traced_complete:
            problems.append(
                "coverage.evidence_complete does not match hydrated trace completeness"
            )
    expected_complete = (
        coverage["discovery_complete"] and coverage["evidence_complete"]
    )
    if coverage["complete"] != expected_complete:
        problems.append("coverage.complete must equal discovery_complete AND evidence_complete")
    if problems:
        raise AuditAgentError(
            "audit agent 응답 의미 검증 실패: " + "; ".join(problems[:10])
        )
    return document


def _bundle_identity(
    path: Path,
    facts: dict,
    context: dict,
    brief_doc: dict,
    scope: AuditScope = WORKBOOK_SCOPE,
) -> dict:
    if scope.kind == "sheet":
        try:
            prepared = read_scope_commit(path, scope)
            assert prepared is not None
            validate_scope_commit(
                path, scope, prepared, facts, context, brief_doc
            )
        except (AuditScopeError, OSError, json.JSONDecodeError) as e:
            raise AuditAgentError(
                f"sheet bundle identity 재조회 중 package가 변경되었습니다: {e}"
            ) from e
        calculated = scope_bundle_keys(scope, facts, context, brief_doc)
    else:
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise AuditAgentError(f"검증된 bundle meta 재조회 실패: {e}") from e
        if not isinstance(meta, dict) or not isinstance(
            meta.get("audit_preparation"), dict
        ):
            raise AuditAgentError(
                "bundle identity 재조회 중 package meta 형식이 변경되었습니다. "
                "다시 실행하십시오."
            )
        prepared = meta["audit_preparation"]
        calculated = bundle_keys(facts, context, brief_doc)
    advertised = tuple(
        prepared.get(field) for field in ("facts_key", "standards_key", "brief_key")
    )
    if advertised != calculated:
        raise AuditAgentError(
            "bundle identity 재조회 중 package가 변경되었습니다. 다시 실행하십시오."
        )
    retriever = context.get("retriever", {})
    return {
        "scope": scope.identity(),
        "workbook_sha256": facts.get("source", {}).get("sha256"),
        "prepare_version": prepared.get("version"),
        "facts_key": prepared.get("facts_key"),
        "standards_key": prepared.get("standards_key"),
        "brief_key": prepared.get("brief_key"),
        "prepared_at": prepared.get("prepared_at"),
        "standards_corpus_version": retriever.get("corpus_version"),
    }


def _assert_bundle_unchanged(
    path: Path,
    facts: dict,
    context: dict,
    brief_doc: dict,
    expected: dict,
    scope: AuditScope = WORKBOOK_SCOPE,
) -> None:
    current = _bundle_identity(path, facts, context, brief_doc, scope)
    if current != expected:
        raise AuditAgentError("audit agent 실행 중 package bundle identity가 변경되었습니다.")


def _bounded_int(value: object, *, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise AuditAgentError(f"{name}은 정수여야 합니다: {value!r}") from e
    if number < 1 or number > maximum:
        raise AuditAgentError(f"{name}은 1~{maximum} 범위여야 합니다: {number}")
    return number


def _clean_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditAgentError(f"{field}가 비어 있습니다.")
    cleaned = _CONTROL_CHARS.sub("", value)
    return " ".join(cleaned.split())


def _markdown_text(value: object) -> str:
    text = _CONTROL_CHARS.sub("", str(value))
    text = " ".join(text.split())
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*\[\]])", r"\\\1", text)


def _question(value: str | None) -> str | None:
    if value is None:
        return None
    text = _clean_text(value, field="question")
    if len(text) > 2000:
        raise AuditAgentError("question은 2,000자 이하여야 합니다.")
    return text


def _known_ids(facts: dict, context: dict, brief_doc: dict) -> dict[str, set[str]]:
    return {
        "fact": {
            item["id"] for item in facts.get("facts", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
        "relation": {
            item["id"] for item in facts.get("relations", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
        "standard_citation": {
            item["id"] for item in context.get("citations", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
        "statement": {
            item["id"] for item in brief_doc.get("statements", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        },
    }


def _empty_observed() -> dict[str, set[str]]:
    return {kind: set() for kind in (
        "fact", "relation", "standard_citation", "statement"
    )}


def _add_record_id(
    observed: dict[str, set[str]],
    known: dict[str, set[str]],
    kind: str,
    item: object,
) -> None:
    if not isinstance(item, dict):
        return
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id in known.get(kind, set()):
        observed[kind].add(item_id)


def _observed_from_result(
    tool: str,
    result: object,
    known: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Collect only typed record IDs, never ID-looking strings inside free text/cells."""
    observed = _empty_observed()
    if not isinstance(result, dict) or "error" in result:
        return observed
    if tool == "brief":
        for item in result.get("statements", []):
            _add_record_id(observed, known, "statement", item)
    elif tool == "assertion_procedures":
        for pair in result.get("pairs", []):
            if not isinstance(pair, dict):
                continue
            _add_record_id(observed, known, "fact", pair.get("assertion"))
            _add_record_id(observed, known, "fact", pair.get("procedure"))
            for item in pair.get("produced_facts", []):
                _add_record_id(observed, known, "fact", item)
            for field in ("test_relations", "produces_relations"):
                for item in pair.get(field, []):
                    _add_record_id(observed, known, "relation", item)
        for field in ("unpaired_assertions", "unpaired_procedures"):
            for item in result.get(field, []):
                _add_record_id(observed, known, "fact", item)
    elif tool == "audit_search":
        for match in result.get("matches", []):
            if not isinstance(match, dict):
                continue
            kind = match.get("kind")
            if kind in {"fact", "statement"}:
                _add_record_id(observed, known, kind, match.get("item"))
    elif tool == "audit_get":
        kind = result.get("kind")
        mapped = {
            "fact": "fact",
            "relation": "relation",
            "standard_citation": "standard_citation",
            "statement": "statement",
        }.get(kind)
        if mapped:
            _add_record_id(observed, known, mapped, result.get("item"))
    elif tool == "trace":
        kind = result.get("kind")
        mapped = {
            "fact": "fact",
            "relation": "relation",
            "standard_citation": "standard_citation",
            "statement": "statement",
        }.get(kind)
        if mapped:
            _add_record_id(observed, known, mapped, result.get("item"))
        for item in result.get("facts", []):
            _add_record_id(observed, known, "fact", item)
        for item in result.get("relations", []):
            _add_record_id(observed, known, "relation", item)
        for item in result.get("standards_citations", []):
            _add_record_id(observed, known, "standard_citation", item)
    return observed


def _merge_observed(
    target: dict[str, set[str]], source: dict[str, set[str]]
) -> None:
    for kind, values in source.items():
        target[kind].update(values)


def _serialize_model_payload(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_MODEL_CONTEXT_BYTES:
        return encoded
    raise AuditAgentError(
        "audit agent 입력이 600KB 모델 예산을 초과했습니다. --limit을 낮추거나 "
        "질문 범위를 좁혀 주세요."
    )


def _tool_error(message: str) -> dict:
    return {"error": {"code": "INVALID_TOOL_REQUEST", "message": message}}


def _execute_tool(
    pkg: Path,
    facts: dict,
    context: dict,
    brief_doc: dict,
    resolver: WorkbookSourceResolver,
    request: dict,
    *,
    observed: dict[str, set[str]],
    maximum_limit: int,
) -> dict:
    name = request.get("name")
    query = request.get("query")
    kind = request.get("kind")
    item_id = request.get("item_id")
    requested_limit = request.get("limit")
    if not isinstance(requested_limit, int):
        raise AuditAgentError("tool.limit은 정수여야 합니다.")
    limit = min(requested_limit, maximum_limit)

    if name == "audit_search":
        if not isinstance(query, str) or not query.strip():
            raise AuditAgentError("audit_search에는 query가 필요합니다.")
        if item_id is not None:
            raise AuditAgentError("audit_search에는 item_id를 사용할 수 없습니다.")
        return _audit_search_loaded(
            facts, brief_doc, query=query, kind=kind, limit=limit
        )
    if name == "audit_get":
        if not isinstance(item_id, str) or not item_id.strip():
            raise AuditAgentError("audit_get에는 item_id가 필요합니다.")
        if query is not None or kind is not None:
            raise AuditAgentError("audit_get에는 query/kind를 사용할 수 없습니다.")
        if not any(item_id in values for values in observed.values()):
            raise AuditAgentError(
                "audit_get item_id는 앞선 typed 결과에서 관찰된 ID여야 합니다."
            )
        return _audit_get_loaded(
            facts, context, brief_doc, item_id=item_id
        )
    if name == "assertion_procedures":
        if item_id is not None or kind is not None:
            raise AuditAgentError(
                "assertion_procedures에는 item_id/kind를 사용할 수 없습니다."
            )
        return _assertion_procedures_loaded(
            facts, brief_doc, query=query, limit=limit
        )
    if name == "trace":
        if not isinstance(item_id, str) or not item_id.strip():
            raise AuditAgentError("trace에는 item_id가 필요합니다.")
        if query is not None or kind is not None:
            raise AuditAgentError("trace에는 query/kind를 사용할 수 없습니다.")
        if not any(item_id in values for values in observed.values()):
            raise AuditAgentError(
                "trace item_id는 앞선 typed 결과에서 관찰된 ID여야 합니다."
            )
        return _trace_loaded(
            pkg,
            facts,
            context,
            brief_doc,
            item_id=item_id,
            limit=limit,
            resolver=resolver,
        )
    raise AuditAgentError(f"지원하지 않는 audit agent tool입니다: {name!r}")


def _validate_plan(
    plan: dict,
    *,
    known: dict[str, set[str]],
    observed: dict[str, set[str]],
) -> list[str]:
    problems: list[str] = []
    selections = plan.get("selections", [])
    abstained = plan.get("abstained")
    abstention_code = plan.get("abstention_code")
    if abstained:
        if abstention_code not in _ABSTENTION_REASONS:
            problems.append("abstained=true이면 유효한 abstention_code가 필요합니다.")
        if selections:
            problems.append("abstained=true이면 selections는 비어 있어야 합니다.")
    else:
        if abstention_code is not None:
            problems.append("abstained=false이면 abstention_code는 null이어야 합니다.")
        if not selections:
            problems.append("abstained=false이면 selection이 1건 이상 필요합니다.")

    for index, selection in enumerate(selections):
        kind = selection.get("kind")
        ids = selection.get("ids", [])
        if kind not in known:
            problems.append(f"selections[{index}].kind가 유효하지 않습니다: {kind!r}")
            continue
        unknown = [item_id for item_id in ids if item_id not in known[kind]]
        unseen = [
            item_id for item_id in ids
            if item_id in known[kind] and item_id not in observed[kind]
        ]
        if unknown:
            problems.append(f"selections[{index}]: unknown {kind} IDs {unknown[:5]}")
        if unseen:
            problems.append(f"selections[{index}]: unobserved {kind} IDs {unseen[:5]}")
    selected_count = len({
        (selection.get("kind"), item_id)
        for selection in selections
        for item_id in selection.get("ids", [])
    })
    if selected_count > MAX_SELECTED_RECORDS:
        problems.append(
            f"selection 고유 record 수가 상한 {MAX_SELECTED_RECORDS}을 초과합니다: "
            f"{selected_count}"
        )
    return problems


def _sanitize_plan(plan: dict) -> dict:
    result = {
        "abstained": plan.get("abstained"),
        "abstention_code": plan.get("abstention_code"),
        "selections": [
            {"kind": item.get("kind"), "ids": list(item.get("ids", []))}
            for item in plan.get("selections", [])
        ],
    }
    return result


def _fact_section(fact_type: object) -> str:
    return {
        "workpaper_attribute": "scope",
        "account": "scope",
        "risk": "risks_assertions",
        "assertion": "risks_assertions",
        "control": "controls",
        "procedure": "procedures",
        "result": "results",
        "finding": "findings",
        "conclusion": "conclusions",
        "open_item": "open_items",
        "signoff": "signoffs",
    }.get(str(fact_type), "overview")


def _relation_text(relation: dict, fact_map: dict[str, dict]) -> str:
    source = fact_map[relation["from_fact_id"]]
    target = fact_map[relation["to_fact_id"]]
    source_text = _clean_text(source.get("description"), field="relation.from_fact")
    target_text = _clean_text(target.get("description"), field="relation.to_fact")
    relation_type = relation.get("type")
    status = relation.get("status")
    status_label = {
        "documented": "문서화된",
        "inferred": "추론된",
        "unknown": "상태 미확정",
    }.get(status, str(status))
    return f"{source_text} → {target_text} ({status_label} {relation_type} 관계)"


def _materialize_plan(
    plan: dict,
    *,
    facts_doc: dict,
    context_doc: dict,
    brief_doc: dict,
    question: str | None,
) -> dict:
    title_source = brief_doc.get("workpaper", {}).get("title") or "감사조서"
    title = _clean_text(title_source, field="workpaper.title")
    title += " 질의 답변" if question else " 브리핑"
    if plan.get("abstained"):
        return {
            "title": title,
            "abstained": True,
            "abstention_reason": _ABSTENTION_REASONS[plan["abstention_code"]],
            "claims": [],
            "suggested_questions": [],
        }

    fact_map = {item["id"]: item for item in facts_doc.get("facts", [])}
    relation_map = {item["id"]: item for item in facts_doc.get("relations", [])}
    citation_map = {item["id"]: item for item in context_doc.get("citations", [])}
    statement_map = {item["id"]: item for item in brief_doc.get("statements", [])}
    claims: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for selection in plan.get("selections", []):
        kind = selection["kind"]
        for item_id in selection["ids"]:
            key = (kind, item_id)
            if key in seen:
                continue
            seen.add(key)
            if kind == "statement":
                item = statement_map[item_id]
                claims.append({
                    "section": item.get("section", "overview"),
                    "text": _clean_text(item.get("text"), field=f"statement {item_id}"),
                    "basis": item.get("type"),
                    "status": item.get("status"),
                    "confidence": item.get("confidence"),
                    "fact_ids": list(item.get("fact_ids", [])),
                    "relation_ids": list(item.get("relation_ids", [])),
                    "standard_citation_ids": list(item.get("standard_citation_ids", [])),
                    "statement_ids": [item_id],
                })
            elif kind == "fact":
                item = fact_map[item_id]
                claims.append({
                    "section": _fact_section(item.get("type")),
                    "text": _clean_text(item.get("description"), field=f"fact {item_id}"),
                    "basis": "documented_fact",
                    "status": item.get("status"),
                    "confidence": item.get("confidence"),
                    "fact_ids": [item_id],
                    "relation_ids": [],
                    "standard_citation_ids": [],
                    "statement_ids": [],
                })
            elif kind == "relation":
                item = relation_map[item_id]
                claims.append({
                    "section": "results" if item.get("type") == "produces" else "procedures",
                    "text": _relation_text(item, fact_map),
                    "basis": "documented_fact",
                    "status": item.get("status"),
                    "confidence": item.get("confidence"),
                    "fact_ids": [item["from_fact_id"], item["to_fact_id"]],
                    "relation_ids": [item_id],
                    "standard_citation_ids": [],
                    "statement_ids": [],
                })
            else:  # standard_citation
                item = citation_map[item_id]
                text = _clean_text(
                    item.get("snippet") or item.get("title") or item_id,
                    field=f"standard citation {item_id}",
                )
                if len(text) > 2000:
                    text = text[:2000] + "…"
                claims.append({
                    "section": "standards",
                    "text": text,
                    "basis": "authoritative_context",
                    "status": "documented",
                    "confidence": 1.0,
                    "fact_ids": [],
                    "relation_ids": [],
                    "standard_citation_ids": [item_id],
                    "statement_ids": [],
                })
    if not claims:
        raise AuditAgentError("선택된 근거 record에서 렌더링할 claim이 없습니다.")
    return {
        "title": title,
        "abstained": False,
        "abstention_reason": None,
        "claims": claims,
        "suggested_questions": [],
    }


def _evidence_budget_problems(final: dict) -> list[str]:
    record_ids = {
        (kind, item_id)
        for claim in final.get("claims", [])
        for kind, field in (
            ("fact", "fact_ids"),
            ("relation", "relation_ids"),
            ("standard_citation", "standard_citation_ids"),
        )
        for item_id in claim.get(field, [])
    }
    if len(record_ids) > MAX_EVIDENCE_RECORDS:
        return [
            f"최종 evidence record 수가 상한 {MAX_EVIDENCE_RECORDS}을 초과합니다: "
            f"{len(record_ids)}"
        ]
    return []


def _trace_complete(result: dict) -> bool:
    pairs = (
        ("returned_facts", "total_facts"),
        ("returned_relations", "total_relations"),
        ("returned_sources", "total_sources"),
        ("returned_relation_direct_sources", "total_relation_direct_sources"),
        ("returned_relation_direct_cells", "total_relation_direct_cells"),
        ("returned_endpoint_sources", "total_endpoint_sources"),
        ("returned_endpoint_cells", "total_endpoint_cells"),
        ("returned_standards_citations", "total_standards_citations"),
        ("returned_cells", "total_cells"),
    )
    return all(result.get(returned) == result.get(total) for returned, total in pairs)


def _discovery_result_complete(tool: str, result: object) -> bool:
    if not isinstance(result, dict) or "error" in result:
        return False

    def has_truncation(value: object) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if (key == "truncated" or key.endswith("_truncated")) and item is True:
                    return True
                if has_truncation(item):
                    return True
        elif isinstance(value, list):
            return any(has_truncation(item) for item in value)
        return False

    if has_truncation(result):
        return False
    if tool == "trace":
        return _trace_complete(result)
    return True


def _hydrate_evidence(
    pkg: Path,
    facts_doc: dict,
    context_doc: dict,
    brief_doc: dict,
    final: dict,
    *,
    limit: int,
    resolver: WorkbookSourceResolver,
) -> tuple[dict, bool]:
    fact_ids = list(dict.fromkeys(
        fact_id
        for claim in final.get("claims", [])
        for fact_id in claim.get("fact_ids", [])
    ))
    citation_ids = list(dict.fromkeys(
        citation_id
        for claim in final.get("claims", [])
        for citation_id in claim.get("standard_citation_ids", [])
    ))
    relation_ids = list(dict.fromkeys(
        relation_id
        for claim in final.get("claims", [])
        for relation_id in claim.get("relation_ids", [])
    ))
    facts: list[dict] = []
    relations: list[dict] = []
    standards: list[dict] = []
    complete = True
    remaining_cells = MAX_EVIDENCE_CELLS

    def bounded_cells(traced: dict) -> tuple[list[dict], bool]:
        nonlocal remaining_cells
        cells = list(traced.get("cells", []))
        kept = cells[:remaining_cells]
        within_budget = len(kept) == len(cells)
        remaining_cells -= len(kept)
        return kept, within_budget

    for fact_id in fact_ids:
        traced = _trace_loaded(
            pkg, facts_doc, context_doc, brief_doc,
            item_id=fact_id, limit=limit, resolver=resolver,
        )
        cells, cells_complete = bounded_cells(traced)
        item_complete = _trace_complete(traced) and cells_complete
        complete = complete and item_complete
        facts.append({
            "fact_id": fact_id,
            "fact": traced["item"],
            "sources": traced.get("sources", []),
            "cells": cells,
            "trace_complete": item_complete,
        })
    for citation_id in citation_ids:
        traced = _trace_loaded(
            pkg, facts_doc, context_doc, brief_doc,
            item_id=citation_id, limit=limit, resolver=resolver,
        )
        item_complete = _trace_complete(traced)
        complete = complete and item_complete
        citations = traced.get("standards_citations", [])
        standards.append({
            "citation_id": citation_id,
            "citation": citations[0] if citations else traced["item"],
            "trace_complete": item_complete,
        })
    for relation_id in relation_ids:
        traced = _trace_loaded(
            pkg, facts_doc, context_doc, brief_doc,
            item_id=relation_id, limit=limit, resolver=resolver,
        )
        cells, cells_complete = bounded_cells(traced)
        kept_cell_keys = {
            (cell.get("sheet"), cell.get("cell")) for cell in cells
        }
        item_complete = _trace_complete(traced) and cells_complete
        complete = complete and item_complete
        relations.append({
            "relation_id": relation_id,
            "relation": traced["item"],
            "facts": traced.get("facts", []),
            "sources": traced.get("sources", []),
            "cells": cells,
            "relation_direct_sources": traced.get("relation_direct_sources", []),
            "relation_direct_cells": [
                cell for cell in traced.get("relation_direct_cells", [])
                if (cell.get("sheet"), cell.get("cell"))
                in kept_cell_keys
            ],
            "endpoint_sources": traced.get("endpoint_sources", []),
            "endpoint_cells": [
                cell for cell in traced.get("endpoint_cells", [])
                if (cell.get("sheet"), cell.get("cell"))
                in kept_cell_keys
            ],
            "trace_complete": item_complete,
        })
    return {
        "facts": facts,
        "relations": relations,
        "standards": standards,
    }, complete


def _notices(
    briefing: dict,
    *,
    trace_complete: bool | None,
    discovery_complete: bool | None = None,
) -> list[dict]:
    notices: list[dict] = []
    facts_review_status = briefing.get("facts_review_status")
    brief_review_status = briefing.get("review_status")
    if "draft" in {facts_review_status, brief_review_status}:
        notices.append({
            "code": "UNREVIEWED_DRAFT",
            "severity": "high",
            "text": "이 답변은 draft 상태인 감사 facts 또는 brief를 사용했습니다.",
        })
    if facts_review_status == "rejected":
        note = briefing.get("facts_review_note")
        notices.append({
            "code": "SOURCE_FACTS_REJECTED",
            "severity": "high",
            "text": "입력 audit facts가 rejected 상태입니다."
            + (f" 반려 사유: {note}" if isinstance(note, str) and note.strip() else ""),
        })
    if brief_review_status == "rejected":
        note = briefing.get("review_note")
        notices.append({
            "code": "SOURCE_BRIEF_REJECTED",
            "severity": "high",
            "text": "입력 audit brief가 rejected 상태입니다."
            + (f" 반려 사유: {note}" if isinstance(note, str) and note.strip() else ""),
        })
    readiness = briefing.get("readiness", {})
    status = readiness.get("status")
    if status in {"partial", "not_ready"}:
        notices.append({
            "code": f"READINESS_{str(status).upper()}",
            "severity": "high" if status == "not_ready" else "moderate",
            "text": f"감사 brief 준비 상태가 {status}입니다.",
        })
    if trace_complete is False:
        notices.append({
            "code": "TRACE_TRUNCATED",
            "severity": "moderate",
            "text": "일부 근거 trace가 반환 상한에 걸려 완전하지 않습니다.",
        })
    if discovery_complete is False:
        notices.append({
            "code": "DISCOVERY_INCOMPLETE",
            "severity": "moderate",
            "text": "일부 brief·검색·관계 조회가 잘렸거나 실패해 전체 범위를 대표하지 않습니다.",
        })
    return notices


def _blocked_response(
    *,
    path: Path,
    model: str,
    prompt_sha: str,
    briefing: dict,
    bundle: dict,
    reason: str,
    question: str | None,
) -> dict:
    final = {
        "title": "감사조서 브리핑 보류",
        "abstained": True,
        "abstention_reason": reason,
        "claims": [],
        "suggested_questions": [],
    }
    return _validated_response({
        "schema_version": "audit_agent_response.v2",
        "mode": "answer" if question else "briefing",
        "question": question,
        "package": path.name,
        "bundle": bundle,
        "generator": {
            "name": "excel_to_skill.audit.agent",
            "version": AGENT_VERSION,
            "model": model,
            "prompt_sha256": prompt_sha,
            "turns": 0,
            "tools_used": ["brief", "assertion_procedures"],
        },
        "trust": {
            "source_facts_review_status": briefing.get("facts_review_status"),
            "source_facts_reviewed_at": briefing.get("facts_reviewed_at"),
            "source_facts_review_note": briefing.get("facts_review_note"),
            "source_facts_review_note_truncated": briefing.get(
                "facts_review_note_truncated", False
            ),
            "source_brief_review_status": briefing.get("review_status"),
            "source_brief_reviewed_at": briefing.get("reviewed_at"),
            "source_brief_review_note": briefing.get("review_note"),
            "source_brief_review_note_truncated": briefing.get(
                "review_note_truncated", False
            ),
            "source_unreviewed": briefing.get("unreviewed"),
            "answer_review_status": "unreviewed",
            "readiness": briefing.get("readiness"),
        },
        "answer": final,
        "evidence": {"facts": [], "relations": [], "standards": []},
        "coverage": {
            "complete": False,
            "discovery_complete": False,
            "evidence_complete": False,
            "fact_count": 0,
            "relation_count": 0,
            "standards_citation_count": 0,
        },
        "notices": _notices(briefing, trace_complete=None),
        "package_limitations": briefing.get("limitations", []),
    })


def _prepare_audit_agent_runtime(
    pkg: Path | str,
    *,
    model: str,
    question: str | None = None,
    sheet: str | None = None,
    limit: int = DEFAULT_LIMIT,
    max_steps: int = DEFAULT_MAX_STEPS,
    prompt_name: str = AGENT_PROMPT,
) -> tuple[_AuditAgentRuntime | None, dict | None]:
    """Validate and bind a turn before any provider client is constructed.

    Exactly one tuple member is returned. Rejected or not-ready packages yield a
    validated blocked response; runnable packages yield the immutable runtime used
    by the step primitives below.
    """
    lim = _bounded_int(limit, name="limit", default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
    steps_limit = _bounded_int(
        max_steps, name="max_steps", default=DEFAULT_MAX_STEPS, maximum=MAX_STEPS
    )
    user_question = _question(question)
    loaded = load_validated_audit_bundle(pkg, sheet=sheet)
    assert loaded is not None
    path, facts, context, brief_doc = loaded
    try:
        scope = resolve_scope(path, sheet=sheet)
    except AuditScopeError as e:
        raise AuditAgentError(f"audit agent scope 검증 실패: {e}") from e
    bundle = _bundle_identity(path, facts, context, brief_doc, scope)
    briefing = _brief_loaded(facts, context, brief_doc, limit=lim)
    mappings = _assertion_procedures_loaded(facts, brief_doc, limit=lim)
    prompt, prompt_sha = load_prompt(prompt_name)
    if (
        briefing.get("review_status") == "rejected"
        or briefing.get("facts_review_status") == "rejected"
    ):
        rejected = [
            name for name, status in (
                ("facts", briefing.get("facts_review_status")),
                ("brief", briefing.get("review_status")),
            )
            if status == "rejected"
        ]
        rejected_notes = list(dict.fromkeys(
            note.strip()
            for note in (
                briefing.get("facts_review_note"),
                briefing.get("review_note"),
            )
            if isinstance(note, str) and note.strip()
        ))
        return None, _blocked_response(
            path=path,
            model=model,
            prompt_sha=prompt_sha,
            briefing=briefing,
            bundle=bundle,
            reason=(
                "입력 audit " + "/".join(rejected)
                + "가 rejected 상태이므로 실질 브리핑을 생성하지 않습니다."
                + (
                    " 반려 사유: " + "; ".join(rejected_notes)
                    if rejected_notes else ""
                )
            ),
            question=user_question,
        )
    if briefing.get("readiness", {}).get("status") == "not_ready":
        reasons = briefing.get("readiness", {}).get("reasons", [])
        detail = "; ".join(str(reason) for reason in reasons) or "준비된 감사 사실이 없습니다."
        return None, _blocked_response(
            path=path,
            model=model,
            prompt_sha=prompt_sha,
            briefing=briefing,
            bundle=bundle,
            reason=f"감사 brief가 not_ready 상태입니다: {detail}",
            question=user_question,
        )
    try:
        analysis_scope = (
            sheet_model_context(path, scope)
            if scope.kind == "sheet"
            else None
        )
    except AuditScopeError as e:
        raise AuditAgentError(f"audit agent scope context 구성 실패: {e}") from e
    try:
        resolver = WorkbookSourceResolver(path)
    except Exception as e:  # workbook ledger may have changed after the committed-bundle gate
        raise AuditAgentError(f"workbook source ledger 재조회 실패: {e}") from e
    schema = load_schema(AGENT_TURN_SCHEMA)
    return _AuditAgentRuntime(
        path=path,
        facts=facts,
        context=context,
        brief_doc=brief_doc,
        scope=scope,
        bundle=bundle,
        briefing=briefing,
        mappings=mappings,
        prompt=prompt,
        prompt_sha=prompt_sha,
        schema=schema,
        provider_schema=_provider_turn_schema(schema),
        analysis_scope=analysis_scope,
        resolver=resolver,
        known=_known_ids(facts, context, brief_doc),
        model=model,
        question=user_question,
        limit=lim,
        max_steps=steps_limit,
    ), None


def _new_audit_agent_turn_state(
    runtime: _AuditAgentRuntime,
) -> _AuditAgentTurnState:
    """Create the exact bootstrap state used by the legacy one-shot loop."""
    observations: list[dict] = [
        {
            "tool": "brief",
            "input": {"limit": runtime.limit},
            "result": runtime.briefing,
        },
        {
            "tool": "assertion_procedures",
            "input": {"query": None, "limit": runtime.limit},
            "result": runtime.mappings,
        },
    ]
    observed = _empty_observed()
    _merge_observed(
        observed,
        _observed_from_result("brief", runtime.briefing, runtime.known),
    )
    _merge_observed(
        observed,
        _observed_from_result(
            "assertion_procedures", runtime.mappings, runtime.known
        ),
    )
    return _AuditAgentTurnState(
        observations=observations,
        used_tools=["brief", "assertion_procedures"],
        observed=observed,
        discovery_complete=all((
            _discovery_result_complete("brief", runtime.briefing),
            _discovery_result_complete(
                "assertion_procedures", runtime.mappings
            ),
        )),
        seen_tool_requests=set(),
    )


def _audit_agent_observation_witness(
    runtime: _AuditAgentRuntime,
    observations: object,
    *,
    expected_focus: dict | None = None,
) -> tuple[bool, dict[str, set[str]], list[str]]:
    """Replay private observations so checkpoint state never grants record authority."""
    if (
        not isinstance(observations, list)
        or len(observations) < 2
        or any(not isinstance(item, dict) for item in observations)
    ):
        raise AuditAgentError("audit conversation observations witness가 유효하지 않습니다.")
    bootstrap = (
        {
            "tool": "brief",
            "input": {"limit": runtime.limit},
            "result": runtime.briefing,
        },
        {
            "tool": "assertion_procedures",
            "input": {"query": None, "limit": runtime.limit},
            "result": runtime.mappings,
        },
    )
    if observations[0] != bootstrap[0] or observations[1] != bootstrap[1]:
        raise AuditAgentError(
            "audit conversation bootstrap witness가 runtime과 다릅니다."
        )
    observed = _empty_observed()
    for observation in bootstrap:
        _merge_observed(
            observed,
            _observed_from_result(
                observation["tool"], observation["result"], runtime.known
            ),
        )
    discovery_complete = all((
        _discovery_result_complete("brief", runtime.briefing),
        _discovery_result_complete("assertion_procedures", runtime.mappings),
    ))
    used_tools = ["brief", "assertion_procedures"]
    seen_tool_requests: set[str] = set()
    saw_focus = False
    for index, observation in enumerate(observations[2:], 2):
        tool = observation.get("tool")
        if tool == "conversation_focus":
            if (
                saw_focus
                or index != 2
                or expected_focus is None
                or observation != expected_focus
            ):
                raise AuditAgentError(
                    "audit conversation focus witness가 prior turn 기록과 다릅니다."
                )
            saw_focus = True
            result = observation.get("result")
            if not isinstance(result, dict) or not isinstance(
                result.get("records"), list
            ):
                raise AuditAgentError(
                    "audit conversation focus payload가 유효하지 않습니다."
                )
            for record in result["records"]:
                _merge_observed(
                    observed,
                    _observed_from_result("audit_get", record, runtime.known),
                )
            continue
        if tool == "answer_validation":
            if (
                set(observation) != {"tool", "result"}
                or not isinstance(observation.get("result"), dict)
                or set(observation["result"]) != {"error"}
            ):
                raise AuditAgentError(
                    "audit answer validation witness가 유효하지 않습니다."
                )
            continue
        if tool == "agent_protocol":
            if observation not in ({
                "tool": "agent_protocol",
                "result": _tool_error(
                    "action=tool에는 tool 객체와 final=null이 필요합니다."
                ),
            }, {
                "tool": "agent_protocol",
                "result": _tool_error(
                    "action=final에는 tool=null과 final 객체가 필요합니다."
                ),
            }):
                raise AuditAgentError(
                    "audit agent protocol witness가 유효하지 않습니다."
                )
            continue
        request = observation.get("input")
        if not isinstance(tool, str) or not isinstance(request, dict):
            raise AuditAgentError(
                "audit tool observation witness가 유효하지 않습니다."
            )
        try:
            jsonschema.validate(
                request,
                {
                    "$schema": runtime.schema.get(
                        "$schema",
                        "http://json-schema.org/draft-07/schema#",
                    ),
                    "$ref": "#/definitions/toolRequest",
                    "definitions": runtime.schema["definitions"],
                },
            )
        except (jsonschema.ValidationError, KeyError) as e:
            raise AuditAgentError(
                "audit tool request witness schema가 유효하지 않습니다."
            ) from e
        request_key = json.dumps(request, ensure_ascii=False, sort_keys=True)
        if request_key in seen_tool_requests:
            expected_result = _tool_error(
                "동일한 tool request를 반복할 수 없습니다."
            )
        else:
            seen_tool_requests.add(request_key)
            try:
                expected_result = _execute_tool(
                    runtime.path,
                    runtime.facts,
                    runtime.context,
                    runtime.brief_doc,
                    runtime.resolver,
                    request,
                    observed=observed,
                    maximum_limit=runtime.limit,
                )
            except (AuditAgentError, AuditConsumeError) as e:
                expected_result = _tool_error(str(e))
        expected_observation = {
            "tool": str(request.get("name")),
            "input": request,
            "result": expected_result,
        }
        if observation != expected_observation:
            raise AuditAgentError(
                "audit tool result witness가 deterministic replay와 다릅니다."
            )
        used_tools.append(tool)
        _merge_observed(
            observed,
            _observed_from_result(tool, expected_result, runtime.known),
        )
        discovery_complete = (
            discovery_complete
            and _discovery_result_complete(tool, expected_result)
        )
    if (expected_focus is not None) != saw_focus:
        raise AuditAgentError(
            "audit conversation focus witness 유무가 prior turn 기록과 다릅니다."
        )
    return discovery_complete, observed, used_tools


def _request_audit_agent_model_turn(
    runtime: _AuditAgentRuntime,
    state: _AuditAgentTurnState,
    *,
    client,
    step: int,
    child_model_calls: int = 0,
    capabilities: dict | None = None,
    eprint=None,
) -> dict:
    """Request and locally validate one provider action."""
    remaining_model_calls = max(
        0,
        runtime.max_steps - step - child_model_calls,
    )
    payload = {
        "request": {
            "mode": "answer" if runtime.question else "briefing",
            "question": runtime.question,
        },
        "trust": {
            "review_status": runtime.briefing.get("review_status"),
            "unreviewed": runtime.briefing.get("unreviewed"),
            "readiness": runtime.briefing.get("readiness"),
        },
        "turn": step,
        "remaining_turns": remaining_model_calls,
        "remaining_model_calls": remaining_model_calls,
        "observations": state.observations,
    }
    if capabilities is not None:
        payload["capabilities"] = copy.deepcopy(capabilities)
    if runtime.analysis_scope is not None:
        payload["analysis_scope"] = runtime.analysis_scope
    try:
        research_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("standards_research", {}).get("enabled") is True
        )
        planning_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("procedure_planning", {}).get("enabled") is True
        )
        inspection_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("workbook_inspection", {}).get("enabled") is True
        )
        return call_json(
            client,
            system=runtime.prompt,
            user=_serialize_model_payload(payload),
            schema=(
                _provider_turn_schema(
                    runtime.schema,
                    include_research=research_enabled,
                    include_planning=planning_enabled,
                    include_inspection=inspection_enabled,
                )
                if research_enabled or planning_enabled or inspection_enabled
                else runtime.provider_schema
            ),
            validation_schema=runtime.schema,
            label="audit briefing agent",
            retries=0,
            eprint=eprint or (lambda *args: None),
        )
    except AuditLLMError as e:
        raise AuditAgentError(str(e)) from e


def _apply_audit_agent_tool_turn(
    runtime: _AuditAgentRuntime,
    state: _AuditAgentTurnState,
    turn: dict,
) -> None:
    """Validate and apply one requested read-only tool action to turn state."""
    if not isinstance(turn.get("tool"), dict) or turn.get("final") is not None:
        state.observations.append({
            "tool": "agent_protocol",
            "result": _tool_error(
                "action=tool에는 tool 객체와 final=null이 필요합니다."
            ),
        })
        return
    request = turn["tool"]
    request_key = json.dumps(request, ensure_ascii=False, sort_keys=True)
    if request_key in state.seen_tool_requests:
        result = _tool_error("동일한 tool request를 반복할 수 없습니다.")
    else:
        state.seen_tool_requests.add(request_key)
        try:
            result = _execute_tool(
                runtime.path,
                runtime.facts,
                runtime.context,
                runtime.brief_doc,
                runtime.resolver,
                request,
                observed=state.observed,
                maximum_limit=runtime.limit,
            )
        except (AuditAgentError, AuditConsumeError) as e:
            result = _tool_error(str(e))
    tool_name = str(request.get("name"))
    state.observations.append({
        "tool": tool_name,
        "input": request,
        "result": result,
    })
    state.used_tools.append(tool_name)
    _merge_observed(
        state.observed,
        _observed_from_result(tool_name, result, runtime.known),
    )
    state.discovery_complete = (
        state.discovery_complete
        and _discovery_result_complete(tool_name, result)
    )


def _finalize_audit_agent_turn(
    runtime: _AuditAgentRuntime,
    state: _AuditAgentTurnState,
    turn: dict,
    *,
    step: int,
) -> dict | None:
    """Validate a final plan and return a hydrated response, or record retry feedback."""
    if turn.get("action") != "final" or turn.get("tool") is not None or not isinstance(
        turn.get("final"), dict
    ):
        state.observations.append({
            "tool": "agent_protocol",
            "result": _tool_error(
                "action=final에는 tool=null과 final 객체가 필요합니다."
            ),
        })
        return None

    plan = _sanitize_plan(turn["final"])
    problems = _validate_plan(
        plan,
        known=runtime.known,
        observed=state.observed,
    )
    if problems:
        state.observations.append({
            "tool": "answer_validation",
            "result": {
                "error": {
                    "code": "UNGROUNDED_FINAL",
                    "message": problems[:10],
                }
            },
        })
        return None

    final = _materialize_plan(
        plan,
        facts_doc=runtime.facts,
        context_doc=runtime.context,
        brief_doc=runtime.brief_doc,
        question=runtime.question,
    )
    budget_problems = _evidence_budget_problems(final)
    if budget_problems:
        state.observations.append({
            "tool": "answer_validation",
            "result": {
                "error": {
                    "code": "EVIDENCE_BUDGET_EXCEEDED",
                    "message": budget_problems,
                }
            },
        })
        return None

    _assert_bundle_unchanged(
        runtime.path,
        runtime.facts,
        runtime.context,
        runtime.brief_doc,
        runtime.bundle,
        runtime.scope,
    )
    try:
        evidence, trace_complete = _hydrate_evidence(
            runtime.path,
            runtime.facts,
            runtime.context,
            runtime.brief_doc,
            final,
            limit=runtime.limit,
            resolver=runtime.resolver,
        )
    except AuditConsumeError as e:
        raise AuditAgentError(f"최종 근거 trace 실패: {e}") from e
    _assert_bundle_unchanged(
        runtime.path,
        runtime.facts,
        runtime.context,
        runtime.brief_doc,
        runtime.bundle,
        runtime.scope,
    )
    briefing = runtime.briefing
    return _validated_response({
        "schema_version": "audit_agent_response.v2",
        "mode": "answer" if runtime.question else "briefing",
        "question": runtime.question,
        "package": runtime.path.name,
        "bundle": runtime.bundle,
        "generator": {
            "name": "excel_to_skill.audit.agent",
            "version": AGENT_VERSION,
            "model": runtime.model,
            "prompt_sha256": runtime.prompt_sha,
            "turns": step,
            "tools_used": state.used_tools,
        },
        "trust": {
            "source_facts_review_status": briefing.get("facts_review_status"),
            "source_facts_reviewed_at": briefing.get("facts_reviewed_at"),
            "source_facts_review_note": briefing.get("facts_review_note"),
            "source_facts_review_note_truncated": briefing.get(
                "facts_review_note_truncated", False
            ),
            "source_brief_review_status": briefing.get("review_status"),
            "source_brief_reviewed_at": briefing.get("reviewed_at"),
            "source_brief_review_note": briefing.get("review_note"),
            "source_brief_review_note_truncated": briefing.get(
                "review_note_truncated", False
            ),
            "source_unreviewed": briefing.get("unreviewed"),
            "answer_review_status": "unreviewed",
            "readiness": briefing.get("readiness"),
        },
        "answer": final,
        "evidence": evidence,
        "coverage": {
            "complete": trace_complete and state.discovery_complete,
            "discovery_complete": state.discovery_complete,
            "evidence_complete": trace_complete,
            "fact_count": len(evidence["facts"]),
            "relation_count": len(evidence["relations"]),
            "standards_citation_count": len(evidence["standards"]),
        },
        "notices": _notices(
            briefing,
            trace_complete=trace_complete,
            discovery_complete=state.discovery_complete,
        ),
        "package_limitations": briefing.get("limitations", []),
    })


def run_audit_agent(
    pkg: Path | str,
    *,
    model: str,
    client=None,
    client_factory=None,
    question: str | None = None,
    sheet: str | None = None,
    limit: int = DEFAULT_LIMIT,
    max_steps: int = DEFAULT_MAX_STEPS,
    eprint=None,
) -> dict:
    """Run a bounded tool-using briefing turn over a committed audit package."""
    eprint = eprint or (lambda *args: None)
    runtime, blocked_response = _prepare_audit_agent_runtime(
        pkg,
        model=model,
        question=question,
        sheet=sheet,
        limit=limit,
        max_steps=max_steps,
    )
    if blocked_response is not None:
        return blocked_response
    assert runtime is not None
    if client is None:
        if client_factory is None:
            raise AuditAgentError("audit agent 모델 client 또는 client_factory가 필요합니다.")
        try:
            client = client_factory()
        except Exception as e:  # noqa: BLE001 - provider factory boundary
            raise AuditAgentError(f"audit agent 모델 client 생성 실패: {e}") from e
    state = _new_audit_agent_turn_state(runtime)
    for step in range(1, runtime.max_steps + 1):
        turn = _request_audit_agent_model_turn(
            runtime,
            state,
            client=client,
            step=step,
            eprint=eprint,
        )
        if turn.get("action") == "tool":
            _apply_audit_agent_tool_turn(runtime, state, turn)
            continue
        response = _finalize_audit_agent_turn(
            runtime,
            state,
            turn,
            step=step,
        )
        if response is not None:
            return response

    raise AuditAgentError(
        f"{runtime.max_steps}회 안에 근거가 검증된 최종 답변을 만들지 못했습니다."
    )


def _fact_evidence_map(response: dict) -> dict[str, dict]:
    return {
        item["fact_id"]: item
        for item in response.get("evidence", {}).get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("fact_id"), str)
    }


def _citation_evidence_map(response: dict) -> dict[str, dict]:
    return {
        item["citation_id"]: item
        for item in response.get("evidence", {}).get("standards", [])
        if isinstance(item, dict) and isinstance(item.get("citation_id"), str)
    }


def _relation_evidence_map(response: dict) -> dict[str, dict]:
    return {
        item["relation_id"]: item
        for item in response.get("evidence", {}).get("relations", [])
        if isinstance(item, dict) and isinstance(item.get("relation_id"), str)
    }


def render_audit_agent_markdown(response: dict) -> str:
    """Render the validated response; provenance metadata comes only from hydrated evidence."""
    answer = response["answer"]
    lines = [f"# {_markdown_text(answer['title'])}", ""]
    scope = response.get("bundle", {}).get("scope", {"kind": "workbook"})
    if scope.get("kind") == "sheet":
        lines.append(f"> 분석 범위: 시트 {_markdown_text(scope.get('sheet'))}")
    else:
        lines.append("> 분석 범위: 전체 workbook")
    trust = response.get("trust", {})
    readiness = trust.get("readiness", {})
    lines.append(
        "> 입력 facts 검토: "
        f"{trust.get('source_facts_review_status')} · "
        "입력 brief 검토: "
        f"{trust.get('source_brief_review_status')} · "
        f"이 답변 검토: {trust.get('answer_review_status')} · "
        f"준비 상태: {readiness.get('status')}"
    )
    for notice in response.get("notices", []):
        lines.append(
            f"> 주의({_markdown_text(notice['code'])}): "
            f"{_markdown_text(notice['text'])}"
        )
    if response.get("question"):
        lines.extend(["", f"질문: {_markdown_text(response['question'])}"])
    readiness_reasons = readiness.get("reasons", [])
    package_limitations = response.get("package_limitations", [])
    if readiness_reasons or package_limitations:
        lines.extend(["", "## 준비 상태와 한계", ""])
        for reason in readiness_reasons[:20]:
            text = _markdown_text(" ".join(str(reason).split())[:500])
            lines.append(f"- 준비 사유: {text}")
        for limitation in package_limitations[:20]:
            if not isinstance(limitation, dict):
                continue
            text = _markdown_text(
                " ".join(str(limitation.get("description", "")).split())[:500]
            )
            if text:
                lines.append(
                    f"- 제한({limitation.get('severity', 'unknown')}): {text}"
                )
        omitted = max(0, len(readiness_reasons) - 20) + max(
            0, len(package_limitations) - 20
        )
        if omitted:
            lines.append(f"- 추가 제한 {omitted}건은 JSON 출력에서 확인하십시오.")
    if answer.get("abstained"):
        lines.extend([
            "", f"답변 보류: {_markdown_text(answer.get('abstention_reason'))}"
        ])
    else:
        fact_map = _fact_evidence_map(response)
        relation_map = _relation_evidence_map(response)
        citation_map = _citation_evidence_map(response)
        sections: dict[str, list[dict]] = {}
        for claim in answer.get("claims", []):
            sections.setdefault(claim["section"], []).append(claim)
        for section, claims in sections.items():
            lines.extend(["", f"## {_SECTION_TITLES.get(section, section)}", ""])
            for claim in claims:
                lines.append(f"- {_markdown_text(claim['text'])}")
                lines.append(
                    "  - 성격: "
                    f"{_BASIS_LABELS.get(claim['basis'], claim['basis'])} · "
                    f"상태: {claim['status']}"
                    + (
                        f" · 신뢰도: {claim['confidence']:.2f}"
                        if isinstance(claim.get("confidence"), (int, float)) else ""
                    )
                )
                if claim.get("fact_ids"):
                    lines.append("  - 조서 근거: " + ", ".join(claim["fact_ids"]))
                    cell_refs: list[str] = []
                    for fact_id in claim["fact_ids"]:
                        for cell in fact_map.get(fact_id, {}).get("cells", []):
                            ref = f"{cell.get('sheet')}!{cell.get('cell')}"
                            if ref not in cell_refs:
                                cell_refs.append(ref)
                    if cell_refs:
                        lines.append(
                            "  - 원문 셀: "
                            + ", ".join(_markdown_text(ref) for ref in cell_refs)
                        )
                if claim.get("relation_ids"):
                    lines.append("  - 관계 근거: " + ", ".join(claim["relation_ids"]))
                    relation_cells: list[str] = []
                    endpoint_cells: list[str] = []
                    for relation_id in claim["relation_ids"]:
                        relation_evidence = relation_map.get(relation_id, {})
                        for cell in relation_evidence.get("relation_direct_cells", []):
                            ref = f"{cell.get('sheet')}!{cell.get('cell')}"
                            if ref not in relation_cells:
                                relation_cells.append(ref)
                        for cell in relation_evidence.get("endpoint_cells", []):
                            ref = f"{cell.get('sheet')}!{cell.get('cell')}"
                            if ref not in endpoint_cells:
                                endpoint_cells.append(ref)
                    if relation_cells:
                        lines.append(
                            "  - 관계 직접 근거 셀: "
                            + ", ".join(_markdown_text(ref) for ref in relation_cells)
                        )
                    if endpoint_cells:
                        lines.append(
                            "  - 관계 양 끝점 원문 셀: "
                            + ", ".join(_markdown_text(ref) for ref in endpoint_cells)
                        )
                if claim.get("standard_citation_ids"):
                    lines.append(
                        "  - 기준서 근거: " + ", ".join(claim["standard_citation_ids"])
                    )
                    locations: list[str] = []
                    for citation_id in claim["standard_citation_ids"]:
                        citation = citation_map.get(citation_id, {}).get("citation", {})
                        provider = citation.get("provider_metadata", {})
                        location = citation.get("cid")
                        if not location and isinstance(provider, dict):
                            location = provider.get("source_cid")
                        if not location and citation.get("document_id"):
                            location = str(citation["document_id"])
                            if citation.get("paragraph"):
                                location += f"::{citation['paragraph']}"
                        if not location:
                            location = citation.get("source_uri")
                        if isinstance(location, str):
                            corpus = citation.get("corpus_version")
                            if isinstance(corpus, str) and corpus:
                                location = f"{location} ({corpus})"
                            locations.append(location)
                    if locations:
                        lines.append(
                            "  - 검증 기준 위치: "
                            + ", ".join(_markdown_text(item) for item in locations)
                        )
                    for citation_id in claim["standard_citation_ids"]:
                        citation = citation_map.get(citation_id, {}).get("citation", {})
                        snippet = citation.get("snippet")
                        if not isinstance(snippet, str) or not snippet.strip():
                            continue
                        excerpt = " ".join(snippet.split())
                        if len(excerpt) > 400:
                            excerpt = excerpt[:400].rstrip() + "…"
                        lines.append(
                            "  - 기준서 원문 발췌("
                            + _markdown_text(citation_id)
                            + "): "
                            + _markdown_text(excerpt)
                        )
                if claim.get("statement_ids"):
                    lines.append("  - brief 문장: " + ", ".join(claim["statement_ids"]))
    if answer.get("suggested_questions"):
        lines.extend(["", "## 다음에 확인할 질문", ""])
        lines.extend(
            f"- {_markdown_text(question)}"
            for question in answer["suggested_questions"]
        )
    lines.extend([
        "",
        f"근거 추적 완전성: {'완전' if response['coverage']['complete'] else '일부 제한'}",
    ])
    return "\n".join(lines) + "\n"


__all__ = [
    "AGENT_VERSION",
    "AuditAgentError",
    "_audit_agent_observation_witness",
    "render_audit_agent_markdown",
    "run_audit_agent",
]
