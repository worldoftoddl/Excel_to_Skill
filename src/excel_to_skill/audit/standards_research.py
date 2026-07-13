"""Isolated, turn-scoped standards research for ``audit-chat``.

The application owns the MCP boundary.  A child model receives only one standards question and
opaque wrappers around verified search candidates.  It can select candidate references, but the
application resolves and revalidates every selected CID before the main agent can observe it.
This graph has no checkpointer and never writes prepared audit artifacts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Protocol, TypedDict, runtime_checkable

from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import AuditModelError, StandardsDomain, json_sha256
from .standards import StandardHit, StandardsQueryError, StandardsRetrievalFatalError


RESEARCH_VERSION = "0.1.0"
RESEARCH_PROMPT = "audit_standards_research_v1.md"
RESEARCH_WORKER_SCHEMA = "audit_standards_research_worker.schema.json"
RESEARCH_RESULT_SCHEMA = "audit_standards_research.v1"
MAX_QUERY_CHARS = 500
MAX_CANDIDATES = 5
MAX_SELECTED = 3
MAX_CANDIDATE_BYTES = 60_000
MAX_SELECTED_BYTES = 24_000
MAX_RESULT_BYTES = 80_000

_CID_RE = re.compile(r"^(KSA|KIFRS)::([^:]+)::(.+)$")
_RESEARCH_REF_RE = re.compile(r"^research:[0-9a-f]{64}$")
_CANDIDATE_REF_RE = re.compile(r"^candidate:[0-9a-f]{64}$")
_SOURCE_TYPE = {"KSA": "감사기준", "KIFRS": "회계기준"}
_PARA_TYPES = {"정의", "참조", "부록", "요구사항", "적용지침", "본문"}
_LIMITATIONS = (
    "동적 기준서 조회 결과는 현재 turn에만 유효한 미검토 보조 근거입니다.",
    "기준서 시행일 적합성은 구조화 검증되지 않았습니다.",
    "이 결과는 조서의 절차 수행 또는 준수 사실을 입증하지 않습니다.",
)
class StandardsResearchError(RuntimeError):
    """A dynamic standards request failed without weakening the committed bundle."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@runtime_checkable
class VerifiedStandardsResearchRetriever(Protocol):
    """Search plus exact selected-CID verification required by the child graph."""

    @property
    def collection(self) -> str | None:
        ...

    def search(
        self,
        query: str,
        *,
        domain: StandardsDomain | str,
        framework: str | None,
        effective_date: str | None = None,
        standard_nos: list[str] | None = None,
    ) -> list[StandardHit]:
        ...

    def get_verified_paragraph(self, cid: str) -> Mapping[str, object]:
        ...


class StandardsResearchInput(TypedDict):
    request: dict


class StandardsResearchState(TypedDict, total=False):
    request: dict
    candidates: list[dict]
    selection: dict
    result: dict
    route: str


class StandardsResearchOutput(TypedDict):
    result: dict


@dataclass(frozen=True, slots=True)
class StandardsResearchRuntime:
    retriever: VerifiedStandardsResearchRetriever
    client: object
    model: str
    expected_collection: str
    invocation_id: str
    bundle_sha256: str
    scope: dict
    eprint: object | None = None


def _clean_request(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise StandardsResearchError("INVALID_REQUEST", "research request가 객체가 아닙니다.")
    if set(value) != {"query", "domain", "framework", "scope_id", "limit"}:
        raise StandardsResearchError(
            "INVALID_REQUEST", "research request 필드가 유효하지 않습니다."
        )
    query = value.get("query")
    if not isinstance(query, str) or not query.strip():
        raise StandardsResearchError("INVALID_REQUEST", "research query가 비어 있습니다.")
    query = " ".join(query.split())
    if len(query) > MAX_QUERY_CHARS:
        raise StandardsResearchError("LIMIT_EXCEEDED", "research query 상한을 초과했습니다.")
    domain = value.get("domain")
    framework = value.get("framework")
    if {"audit": "KSA", "accounting": "K-IFRS"}.get(domain) != framework:
        raise StandardsResearchError(
            "INVALID_REQUEST", "research domain/framework가 유효하지 않습니다."
        )
    scope_id = value.get("scope_id")
    if scope_id is not None and (
        not isinstance(scope_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", scope_id) is None
    ):
        raise StandardsResearchError("INVALID_REQUEST", "research scope_id가 유효하지 않습니다.")
    limit = value.get("limit")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_CANDIDATES
    ):
        raise StandardsResearchError("LIMIT_EXCEEDED", "research limit은 1~5여야 합니다.")
    return {
        "query": query,
        "domain": domain,
        "framework": framework,
        "scope_id": scope_id,
        "limit": limit,
    }


def _context(runtime) -> StandardsResearchRuntime:
    value = getattr(runtime, "context", None)
    if not isinstance(value, StandardsResearchRuntime):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research runtime context가 없습니다."
        )
    return value


def _bind_request(state: StandardsResearchState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    if not context.expected_collection:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research collection binding이 없습니다."
        )
    if not context.invocation_id or re.fullmatch(
        r"[0-9a-f]{64}", context.bundle_sha256
    ) is None:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research invocation/bundle binding이 유효하지 않습니다."
        )
    return {"request": request, "route": "search"}


def _candidate_ref(collection: str, hit: StandardHit) -> str:
    return "candidate:" + json_sha256({
        "collection": collection,
        "cid": hit.document_id,
        "text_sha256": hit.snippet_sha256,
    })


def _candidate_wrapper(
    collection: str,
    hit: StandardHit,
    request: Mapping[str, object],
) -> dict:
    metadata = hit.metadata
    match = _CID_RE.fullmatch(hit.document_id)
    if match is None:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "검색 후보 CID가 유효하지 않습니다."
        )
    prefix, standard_no, para_no = match.groups()
    if hit.corpus_version != collection or metadata.get("source_cid") != hit.document_id:
        raise StandardsResearchError(
            "CORPUS_DRIFT", "검색 후보 collection/CID가 binding과 다릅니다."
        )
    if (
        hit.domain.value != request.get("domain")
        or hit.framework != request.get("framework")
        or {"audit": "KSA", "accounting": "KIFRS"}.get(
            str(request.get("domain"))
        ) != prefix
        or metadata.get("verified_by") != "standards_get_paragraph"
        or metadata.get("paragraph_text_sha256") != hit.snippet_sha256
        or metadata.get("source_type") != _SOURCE_TYPE[prefix]
        or str(metadata.get("standard_no")) != standard_no
        or str(hit.paragraph) != para_no
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "검색 후보의 원문 검증 메타데이터가 다릅니다."
        )
    return {
        "typed_kind": "standards_candidate",
        "candidate_ref": _candidate_ref(collection, hit),
        "cid": hit.document_id,
        "source_type": metadata.get("source_type"),
        "standard_no": standard_no,
        "standard_title": hit.title,
        "para_no": hit.paragraph,
        "para_type": metadata.get("para_type"),
        "section_path": metadata.get("section_path"),
        "text": hit.snippet,
        "text_sha256": hit.snippet_sha256,
        "score": hit.score,
    }


def _fatal_retrieval_code(error: Exception) -> str:
    message = str(error).casefold()
    if "collection" in message:
        return "CORPUS_DRIFT"
    if any(
        token in message
        for token in ("unavailable", "timeout", "연결", "transport")
    ):
        return "UPSTREAM_UNAVAILABLE"
    return "CONTRACT_MISMATCH"


def _search_candidates(state: StandardsResearchState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    current_collection = context.retriever.collection
    if current_collection is not None and current_collection != context.expected_collection:
        raise StandardsResearchError(
            "CORPUS_DRIFT", "research collection이 committed binding과 다릅니다."
        )
    try:
        hits = context.retriever.search(
            request["query"],
            domain=request["domain"],
            framework=request["framework"],
            effective_date=None,
        )
    except StandardsQueryError as e:
        code = (
            "LIMIT_EXCEEDED"
            if e.limitation_code == "retrieval_capped"
            else "UPSTREAM_UNAVAILABLE"
        )
        raise StandardsResearchError(code, "기준서 검색을 완료하지 못했습니다.") from e
    except StandardsRetrievalFatalError as e:
        raise StandardsResearchError(
            _fatal_retrieval_code(e), "기준서 검색 계약을 검증하지 못했습니다."
        ) from e
    except Exception as e:  # noqa: BLE001 - injected provider boundary
        raise StandardsResearchError(
            "UPSTREAM_UNAVAILABLE", "기준서 검색을 사용할 수 없습니다."
        ) from e
    if context.retriever.collection != context.expected_collection:
        raise StandardsResearchError(
            "CORPUS_DRIFT", "research collection이 committed binding과 다릅니다."
        )
    if not isinstance(hits, list) or any(not isinstance(hit, StandardHit) for hit in hits):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research hit 계약이 유효하지 않습니다."
        )
    definition_hits = [
        hit for hit in hits if hit.metadata.get("retrieval_role") == "definition"
    ]
    requirement_hits = [
        hit for hit in hits if hit.metadata.get("retrieval_role") != "definition"
    ]
    definition_slots = min(len(definition_hits), request["limit"])
    requirement_slots = request["limit"] - definition_slots
    selected_hits = (
        requirement_hits[:requirement_slots]
        + definition_hits[:definition_slots]
    )
    candidates: list[dict] = []
    seen: set[str] = set()
    for hit in selected_hits:
        wrapper = _candidate_wrapper(context.expected_collection, hit, request)
        if wrapper["candidate_ref"] in seen:
            continue
        seen.add(wrapper["candidate_ref"])
        candidates.append(wrapper)
    if len(json.dumps(candidates, ensure_ascii=False).encode("utf-8")) > MAX_CANDIDATE_BYTES:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "research 후보 원문 예산을 초과했습니다."
        )
    return {
        "candidates": candidates,
        "route": "select" if candidates else "empty",
    }


def _after_search(state: StandardsResearchState) -> str:
    return (
        "select_candidates"
        if state.get("route") == "select"
        else "materialize_empty"
    )


def _provider_worker_schema(strict_schema: dict) -> dict:
    """Remove provider-hostile conditionals while retaining strict local validation."""
    schema = copy.deepcopy(strict_schema)
    schema.pop("allOf", None)
    return schema


def _select_candidates(state: StandardsResearchState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    candidates = state.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research 후보가 없습니다."
        )
    prompt, prompt_sha = load_prompt(RESEARCH_PROMPT)
    schema = load_schema(RESEARCH_WORKER_SCHEMA)
    payload = {
        "request": {
            "query": request["query"],
            "domain": request["domain"],
            "framework": request["framework"],
        },
        "candidates": candidates,
        "limits": {"max_selected": MAX_SELECTED},
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CANDIDATE_BYTES + 8_000:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "research worker 입력 예산을 초과했습니다."
        )
    try:
        selection = call_json(
            context.client,
            system=prompt,
            user=encoded,
            schema=_provider_worker_schema(schema),
            validation_schema=schema,
            label="audit standards research worker",
            retries=0,
            eprint=context.eprint or (lambda *args: None),
        )
    except (AuditLLMError, AuditModelError) as e:
        raise StandardsResearchError(
            "UPSTREAM_UNAVAILABLE", "research worker를 완료하지 못했습니다."
        ) from e
    known = {item["candidate_ref"] for item in candidates}
    selected = selection.get("selected_candidate_refs")
    if (
        not isinstance(selected, list)
        or len(selected) > MAX_SELECTED
        or len(selected) != len(set(selected))
        or any(ref not in known for ref in selected)
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "worker가 관찰하지 않은 후보를 선택했습니다."
        )
    if selection.get("abstained") is bool(selected):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "worker abstention과 선택 결과가 다릅니다."
        )
    return {
        "selection": {
            "selected_candidate_refs": list(selected),
            "prompt_sha256": prompt_sha,
            "model": context.model,
        },
        "route": "verify" if selected else "empty",
    }


def _after_selection(state: StandardsResearchState) -> str:
    return (
        "verify_selected"
        if state.get("route") == "verify"
        else "materialize_empty"
    )


def _ephemeral_record(
    context: StandardsResearchRuntime,
    request: dict,
    candidate: Mapping[str, object],
    paragraph: Mapping[str, object],
) -> dict:
    cid = str(candidate["cid"])
    text = paragraph.get("text")
    if (
        paragraph.get("cid") != cid
        or paragraph.get("is_context") is not False
        or not isinstance(text, str)
        or not text.strip()
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "직조회 paragraph identity가 다릅니다."
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if text != candidate.get("text") or digest != candidate.get("text_sha256"):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "search/get 원문이 다시 일치하지 않습니다."
        )
    for paragraph_field, candidate_field in (
        ("source_type", "source_type"),
        ("standard_no", "standard_no"),
        ("standard_title", "standard_title"),
        ("para_no", "para_no"),
        ("para_type", "para_type"),
        ("section_path", "section_path"),
    ):
        if str(paragraph.get(paragraph_field)) != str(candidate.get(candidate_field)):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "search/get 문단 메타데이터가 다릅니다."
            )
    identity = {
        "invocation_id": context.invocation_id,
        "bundle_sha256": context.bundle_sha256,
        "scope": context.scope,
        "collection": context.expected_collection,
        "cid": cid,
        "text_sha256": digest,
    }
    return {
        "typed_kind": "ephemeral_standard",
        "research_ref": "research:" + json_sha256(identity),
        "status": "ephemeral",
        "review_status": "unreviewed",
        "turn_scoped": True,
        "outside_prepared_bundle": True,
        "effective_date_verified": False,
        "scope": copy.deepcopy(context.scope),
        "collection": context.expected_collection,
        "cid": cid,
        "domain": request["domain"],
        "framework": request["framework"],
        "source_type": paragraph.get("source_type"),
        "standard_no": str(paragraph.get("standard_no")),
        "standard_title": paragraph.get("standard_title"),
        "para_no": str(paragraph.get("para_no")),
        "para_type": paragraph.get("para_type"),
        "section_path": paragraph.get("section_path"),
        "text": text,
        "text_sha256": digest,
        "source_uri": f"auditpaper://{context.expected_collection}/{cid}",
        "verified_by": "standards_get_paragraph",
    }


def _result_document(
    context: StandardsResearchRuntime,
    *,
    request: dict,
    status: str,
    records: list[dict],
    selection: Mapping[str, object] | None,
) -> dict:
    _, default_prompt_sha = load_prompt(RESEARCH_PROMPT)
    document = {
        "schema_version": RESEARCH_RESULT_SCHEMA,
        "status": status,
        "evidence_status": "ephemeral",
        "review_status": "unreviewed",
        "turn_scoped": True,
        "outside_prepared_bundle": True,
        "effective_date_verified": False,
        "scope": copy.deepcopy(context.scope),
        "collection": context.expected_collection,
        "request": {
            "domain": request["domain"],
            "framework": request["framework"],
            "scope_id": request["scope_id"],
            "limit": request["limit"],
        },
        "worker": {
            "name": "excel_to_skill.audit.standards_research",
            "version": RESEARCH_VERSION,
            "model": (
                selection.get("model")
                if isinstance(selection, Mapping)
                else context.model
            ),
            "prompt_sha256": (
                selection.get("prompt_sha256")
                if isinstance(selection, Mapping)
                else default_prompt_sha
            ),
            "selected_candidate_refs": (
                list(selection.get("selected_candidate_refs", []))
                if isinstance(selection, Mapping)
                else []
            ),
        },
        "records": copy.deepcopy(records),
        "limitations": list(_LIMITATIONS),
    }
    validate_research_result(document)
    if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "research 결과 예산을 초과했습니다."
        )
    return document


def _verify_selected(state: StandardsResearchState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    candidates = state.get("candidates")
    selection = state.get("selection")
    if not isinstance(candidates, list) or not isinstance(selection, Mapping):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research selection state가 없습니다."
        )
    by_ref = {item.get("candidate_ref"): item for item in candidates}
    selected = selection.get("selected_candidate_refs")
    if not isinstance(selected, list):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research selection refs가 없습니다."
        )
    records: list[dict] = []
    for ref in selected:
        candidate = by_ref.get(ref)
        if not isinstance(candidate, Mapping):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "worker candidate ref가 유효하지 않습니다."
            )
        if context.retriever.collection != context.expected_collection:
            raise StandardsResearchError(
                "CORPUS_DRIFT", "선택 CID 직조회 전에 collection이 변경되었습니다."
            )
        try:
            paragraph = context.retriever.get_verified_paragraph(str(candidate["cid"]))
        except StandardsRetrievalFatalError as e:
            raise StandardsResearchError(
                _fatal_retrieval_code(e), "선택 CID 원문 재검증에 실패했습니다."
            ) from e
        except Exception as e:  # noqa: BLE001 - injected retriever boundary
            raise StandardsResearchError(
                "UPSTREAM_UNAVAILABLE", "선택 CID 원문을 조회할 수 없습니다."
            ) from e
        if context.retriever.collection != context.expected_collection:
            raise StandardsResearchError(
                "CORPUS_DRIFT", "선택 CID 직조회 중 collection이 변경되었습니다."
            )
        if not isinstance(paragraph, Mapping):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "직조회 paragraph 계약이 유효하지 않습니다."
            )
        records.append(_ephemeral_record(context, request, candidate, paragraph))
    if len(json.dumps(records, ensure_ascii=False).encode("utf-8")) > MAX_SELECTED_BYTES:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "선택 기준서 원문 예산을 초과했습니다."
        )
    return {
        "result": _result_document(
            context,
            request=request,
            status="completed",
            records=records,
            selection=selection,
        ),
        "route": "completed",
    }


def _materialize_empty(state: StandardsResearchState, runtime) -> dict:
    context = _context(runtime)
    request = _clean_request(state.get("request"))
    selection = state.get("selection")
    return {
        "result": _result_document(
            context,
            request=request,
            status="no_results",
            records=[],
            selection=selection if isinstance(selection, Mapping) else None,
        ),
        "route": "completed",
    }


def validate_research_result(value: object) -> dict:
    """Validate the self-contained private observation contract."""
    if not isinstance(value, Mapping):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research 결과가 객체가 아닙니다."
        )
    required = {
        "schema_version", "status", "evidence_status", "review_status",
        "turn_scoped", "outside_prepared_bundle", "effective_date_verified",
        "scope", "collection", "request", "worker", "records", "limitations",
    }
    if set(value) != required or value.get("schema_version") != RESEARCH_RESULT_SCHEMA:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research 결과 필드가 유효하지 않습니다."
        )
    if value.get("status") not in {"completed", "no_results"}:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research 결과 상태가 유효하지 않습니다."
        )
    if (
        value.get("evidence_status") != "ephemeral"
        or value.get("review_status") != "unreviewed"
        or value.get("turn_scoped") is not True
        or value.get("outside_prepared_bundle") is not True
        or value.get("effective_date_verified") is not False
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research trust 경계가 유효하지 않습니다."
        )
    collection = value.get("collection")
    records = value.get("records")
    if not isinstance(collection, str) or not collection:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research collection이 없습니다."
        )
    if not isinstance(records, list) or len(records) > MAX_SELECTED:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research records가 유효하지 않습니다."
        )
    if (value.get("status") == "completed") != bool(records):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research status와 record 수가 다릅니다."
        )
    scope = value.get("scope")
    if not isinstance(scope, Mapping):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research scope가 객체가 아닙니다."
        )
    if scope.get("kind") == "workbook":
        scope_valid = set(scope) == {"kind"}
    elif scope.get("kind") == "sheet":
        sheet = scope.get("sheet")
        scope_valid = (
            set(scope) == {"kind", "sheet", "id"}
            and isinstance(sheet, str)
            and bool(sheet)
            and scope.get("id")
            == hashlib.sha256(sheet.encode("utf-8")).hexdigest()
        )
    else:
        scope_valid = False
    if not scope_valid:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research scope identity가 유효하지 않습니다."
        )
    request = value.get("request")
    if not isinstance(request, Mapping) or set(request) != {
        "domain", "framework", "scope_id", "limit"
    }:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research request witness가 유효하지 않습니다."
        )
    if (
        {"audit": "KSA", "accounting": "K-IFRS"}.get(request.get("domain"))
        != request.get("framework")
        or (
            request.get("scope_id") is not None
            and (
                not isinstance(request.get("scope_id"), str)
                or re.fullmatch(r"[0-9a-f]{64}", request["scope_id"]) is None
            )
        )
        or not isinstance(request.get("limit"), int)
        or isinstance(request.get("limit"), bool)
        or not 1 <= request["limit"] <= MAX_CANDIDATES
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research request witness 값이 유효하지 않습니다."
        )
    if value.get("limitations") != list(_LIMITATIONS):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research limitation 경계가 유효하지 않습니다."
        )
    refs: set[str] = set()
    cids: set[str] = set()
    record_fields = {
        "typed_kind", "research_ref", "status", "review_status", "turn_scoped",
        "outside_prepared_bundle", "effective_date_verified", "scope", "collection",
        "cid", "domain", "framework", "source_type", "standard_no", "standard_title",
        "para_no", "para_type", "section_path", "text", "text_sha256", "source_uri",
        "verified_by",
    }
    for record in records:
        if not isinstance(record, Mapping):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "research record가 객체가 아닙니다."
            )
        ref = record.get("research_ref")
        text = record.get("text")
        cid = record.get("cid")
        match = _CID_RE.fullmatch(cid) if isinstance(cid, str) else None
        prefix, standard_no, para_no = match.groups() if match is not None else (None,) * 3
        expected_domain = "audit" if prefix == "KSA" else "accounting"
        expected_framework = "KSA" if prefix == "KSA" else "K-IFRS"
        if (
            set(record) != record_fields
            or not isinstance(ref, str)
            or _RESEARCH_REF_RE.fullmatch(ref) is None
            or ref in refs
            or match is None
            or cid in cids
            or record.get("typed_kind") != "ephemeral_standard"
            or record.get("collection") != collection
            or record.get("scope") != scope
            or record.get("domain") != expected_domain
            or record.get("framework") != expected_framework
            or record.get("domain") != request.get("domain")
            or record.get("framework") != request.get("framework")
            or record.get("source_type") != _SOURCE_TYPE.get(prefix)
            or record.get("standard_no") != standard_no
            or record.get("para_no") != para_no
            or not isinstance(record.get("standard_title"), str)
            or not record.get("standard_title")
            or record.get("para_type") not in _PARA_TYPES
            or not (
                record.get("section_path") is None
                or isinstance(record.get("section_path"), str)
            )
            or record.get("status") != "ephemeral"
            or record.get("review_status") != "unreviewed"
            or record.get("turn_scoped") is not True
            or record.get("outside_prepared_bundle") is not True
            or record.get("effective_date_verified") is not False
            or record.get("verified_by") != "standards_get_paragraph"
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 40_000
            or hashlib.sha256(text.encode("utf-8")).hexdigest()
            != record.get("text_sha256")
            or record.get("source_uri") != f"auditpaper://{collection}/{cid}"
        ):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "research record trust 계약이 다릅니다."
            )
        refs.add(ref)
        cids.add(cid)
    worker = value.get("worker")
    if not isinstance(worker, Mapping) or set(worker) != {
        "name", "version", "model", "prompt_sha256", "selected_candidate_refs"
    }:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research worker witness가 없습니다."
        )
    selected = worker.get("selected_candidate_refs")
    if (
        worker.get("name") != "excel_to_skill.audit.standards_research"
        or worker.get("version") != RESEARCH_VERSION
        or not isinstance(worker.get("model"), str)
        or not worker.get("model")
        or not isinstance(worker.get("prompt_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", worker["prompt_sha256"]) is None
        or not isinstance(selected, list)
        or len(selected) > MAX_SELECTED
        or len(selected) != len(set(selected))
        or any(
            not isinstance(ref, str) or _CANDIDATE_REF_RE.fullmatch(ref) is None
            for ref in selected
        )
        or len(selected) != len(records)
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research worker witness가 유효하지 않습니다."
        )
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "research 결과 예산을 초과했습니다."
        )
    return copy.deepcopy(dict(value))


def research_records(value: object) -> dict[str, dict]:
    document = validate_research_result(value)
    return {record["research_ref"]: record for record in document["records"]}


def build_standards_research_graph():
    """Compile the isolated research graph without a checkpointer."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise StandardsResearchError(
            "UPSTREAM_UNAVAILABLE",
            "standards research에는 graph extra가 필요합니다.",
        ) from e
    builder = StateGraph(
        StandardsResearchState,
        context_schema=StandardsResearchRuntime,
        input_schema=StandardsResearchInput,
        output_schema=StandardsResearchOutput,
    )
    builder.add_node("bind_request", _bind_request)
    builder.add_node("research_search", _search_candidates)
    builder.add_node("research_select", _select_candidates)
    builder.add_node("research_verify", _verify_selected)
    builder.add_node("materialize_empty", _materialize_empty)
    builder.add_edge(START, "bind_request")
    builder.add_edge("bind_request", "research_search")
    builder.add_conditional_edges(
        "research_search",
        _after_search,
        {
            "select_candidates": "research_select",
            "materialize_empty": "materialize_empty",
        },
    )
    builder.add_conditional_edges(
        "research_select",
        _after_selection,
        {
            "verify_selected": "research_verify",
            "materialize_empty": "materialize_empty",
        },
    )
    builder.add_edge("research_verify", END)
    builder.add_edge("materialize_empty", END)
    # ``None`` inherits an enclosing graph's checkpointer.  This worker handles raw
    # standards candidates and verified paragraph text, so inheritance would copy that
    # material into the parent audit-chat SQLite checkpoint before the outer node can
    # reduce it to a private-store reference.
    return builder.compile(
        checkpointer=False,
        name="audit_standards_research",
    )


def run_standards_research(
    request: Mapping[str, object],
    *,
    runtime: StandardsResearchRuntime,
) -> dict:
    """Run one isolated research request and return its bounded final observation."""
    graph = build_standards_research_graph()
    try:
        result = graph.invoke(
            {"request": copy.deepcopy(dict(request))},
            context=runtime,
        )
    except StandardsResearchError:
        raise
    except Exception as e:  # noqa: BLE001 - nested graph boundary
        cause = e.__cause__
        if isinstance(cause, StandardsResearchError):
            raise cause
        raise StandardsResearchError(
            "UPSTREAM_UNAVAILABLE", "research graph가 완료되지 않았습니다."
        ) from e
    if not isinstance(result, Mapping):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research graph 결과가 객체가 아닙니다."
        )
    return validate_research_result(result.get("result"))


def research_summary(
    observations: list[dict],
    *,
    selected_refs: list[str],
) -> dict | None:
    """Build a response supplement from current-turn typed research observations."""
    results: list[dict] = []
    records: dict[str, dict] = {}
    for observation in observations:
        if observation.get("tool") != "standards_research":
            continue
        result = observation.get("result")
        if (
            isinstance(result, Mapping)
            and result.get("schema_version") == RESEARCH_RESULT_SCHEMA
        ):
            validated = validate_research_result(result)
            results.append(validated)
            records.update(research_records(validated))
    if not results:
        if selected_refs:
            raise StandardsResearchError(
                "CONTRACT_MISMATCH", "선택된 research ref의 observation이 없습니다."
            )
        return None
    if len(results) != 1:
        raise StandardsResearchError(
            "LIMIT_EXCEEDED", "한 turn의 research는 1회만 허용됩니다."
        )
    if (
        len(selected_refs) != len(set(selected_refs))
        or any(ref not in records for ref in selected_refs)
    ):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "관찰되지 않은 research ref가 선택되었습니다."
        )
    result = results[0]
    return {
        "status": result["status"],
        "evidence_status": "ephemeral",
        "review_status": "unreviewed",
        "turn_scoped": True,
        "outside_prepared_bundle": True,
        "effective_date_verified": False,
        "scope": copy.deepcopy(result["scope"]),
        "collection": result["collection"],
        "selected_refs": list(selected_refs),
        "citations": [copy.deepcopy(records[ref]) for ref in selected_refs],
        "limitations": copy.deepcopy(result["limitations"]),
    }


def validate_research_summary(
    value: object,
    *,
    observations: list[dict],
) -> dict:
    """Exact-compare a response supplement with its typed observation witness."""
    if not isinstance(value, Mapping):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research summary가 객체가 아닙니다."
        )
    selected = value.get("selected_refs")
    if not isinstance(selected, list) or any(not isinstance(ref, str) for ref in selected):
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research summary refs가 유효하지 않습니다."
        )
    expected = research_summary(observations, selected_refs=selected)
    if expected is None or dict(value) != expected:
        raise StandardsResearchError(
            "CONTRACT_MISMATCH", "research summary가 observation과 다릅니다."
        )
    return copy.deepcopy(dict(value))


__all__ = [
    "MAX_CANDIDATES",
    "MAX_SELECTED",
    "RESEARCH_RESULT_SCHEMA",
    "RESEARCH_VERSION",
    "StandardsResearchError",
    "StandardsResearchRuntime",
    "VerifiedStandardsResearchRetriever",
    "build_standards_research_graph",
    "research_records",
    "research_summary",
    "run_standards_research",
    "validate_research_result",
    "validate_research_summary",
]
