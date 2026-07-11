"""Workbook-derived 기준서 조회 계획을 `standards_context.v1`으로 실행한다.

이 모듈은 네트워크나 MCP를 직접 알지 못한다. 호출자가 주입한
`StandardsRetriever`만 사용하며, 한 query의 조회·응답 오류는 그 query의 `error`
결과와 limitation으로 격리한다. 최종 문서는 언제나 저장소의 JSON schema로 검증한
뒤 반환한다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache

from jsonschema import Draft7Validator

from ..resources import SCHEMA_DIR
from .model import (
    AuditModelError,
    SourceKind,
    StandardsDomain,
    canonical_json,
    json_sha256,
    require_non_empty,
    validate_iso_date,
)
from .standards import (
    StandardHit,
    StandardsQueryError,
    StandardsRetrievalFatalError,
    StandardsRetriever,
)

_SCHEMA_PATH = SCHEMA_DIR / "standards_context.schema.json"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STANDARD_NO_RE = re.compile(
    r"^(?:[A-Z]{2,8}-?\d+(?:-\d+)?|\d+(?:-\d+)?)$"
)
_QUERY_MATCH_METADATA_FIELDS = {"retrieval_role", "search_text_sha256"}


class StandardsContextError(ValueError):
    """standards context 입력·조립·스키마 계약 오류."""


class _CitationProblem(StandardsContextError):
    """query 단위로 격리할 수 있는 citation 변환 오류."""

    def __init__(self, message: str, *, limitation_code: str = "ambiguous_passage") -> None:
        super().__init__(message)
        self.limitation_code = limitation_code


@lru_cache(maxsize=1)
def _schema() -> dict:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return schema


def validate_standards_context(doc: Mapping[str, object]) -> None:
    """문서를 `standards_context.schema.json`으로 검증한다. 실패하면 예외를 낸다."""
    errors = sorted(
        Draft7Validator(_schema()).iter_errors(doc),
        key=lambda e: (list(e.absolute_path), e.message),
    )
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(part) for part in first.absolute_path) or "$"
    raise StandardsContextError(f"standards_context schema 불일치({path}): {first.message}")


def _identifier(value: object, *, field: str) -> str:
    text = require_non_empty(value, field=field)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise StandardsContextError(f"{field}가 유효한 identifier가 아닙니다: {text!r}")
    return text


def _sha256(value: object, *, field: str) -> str:
    text = require_non_empty(value, field=field)
    if not _SHA256_RE.fullmatch(text):
        raise StandardsContextError(f"{field}는 소문자 SHA-256 hex여야 합니다.")
    return text


def _optional_text(value: object, *, field: str) -> str | None:
    return None if value is None else require_non_empty(value, field=field)


def _timestamp_z(value: object, *, field: str) -> str:
    """timezone 포함 ISO 시각을 schema의 초 단위 UTC `...Z`로 정규화한다."""
    text = require_non_empty(value, field=field)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as e:
        raise StandardsContextError(f"{field}가 유효한 ISO 8601 시각이 아닙니다.") from e
    if "T" not in text or parsed.tzinfo is None:
        raise StandardsContextError(f"{field}에는 시각과 timezone이 필요합니다.")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retriever_view(descriptor: Mapping[str, object]) -> dict:
    if not isinstance(descriptor, Mapping):
        raise StandardsContextError("retriever_descriptor는 객체여야 합니다.")
    allowed = {
        "name", "version", "mcp_server", "tool", "corpus_id", "corpus_version",
        "config_sha256", "retrieved_at",
    }
    required = allowed - {"config_sha256"}
    extra = sorted(str(key) for key in set(descriptor) - allowed)
    if extra:
        raise StandardsContextError(f"retriever_descriptor 계약 밖 필드: {extra}")
    missing = sorted(required - set(descriptor))
    if missing:
        raise StandardsContextError(f"retriever_descriptor 필수 필드 누락: {missing}")
    view = {
        "name": require_non_empty(descriptor["name"], field="retriever.name"),
        "version": _optional_text(descriptor["version"], field="retriever.version"),
        "mcp_server": require_non_empty(
            descriptor["mcp_server"], field="retriever.mcp_server"
        ),
        "tool": require_non_empty(descriptor["tool"], field="retriever.tool"),
        "corpus_id": require_non_empty(
            descriptor["corpus_id"], field="retriever.corpus_id"
        ),
        "corpus_version": require_non_empty(
            descriptor["corpus_version"], field="retriever.corpus_version"
        ),
        "retrieved_at": _timestamp_z(
            descriptor["retrieved_at"], field="retriever.retrieved_at"
        ),
    }
    if "config_sha256" in descriptor:
        view["config_sha256"] = _sha256(
            descriptor["config_sha256"], field="retriever.config_sha256"
        )
    return view


def _query_views(audit_facts: Mapping[str, object]) -> list[dict]:
    raw_queries = audit_facts.get("standard_queries")
    if not isinstance(raw_queries, list):
        raise StandardsContextError("audit_facts.standard_queries는 배열이어야 합니다.")
    queries: list[dict] = []
    seen: set[str] = set()
    required = {"id", "query", "domain", "framework", "effective_date", "fact_ids"}
    for index, raw in enumerate(raw_queries):
        if not isinstance(raw, Mapping):
            raise StandardsContextError(f"standard_queries[{index}]는 객체여야 합니다.")
        missing = sorted(required - set(raw))
        if missing:
            raise StandardsContextError(
                f"standard_queries[{index}] 필수 필드 누락: {missing}"
            )
        query_id = _identifier(raw["id"], field=f"standard_queries[{index}].id")
        if query_id in seen:
            raise StandardsContextError(f"중복 standard query id: {query_id!r}")
        seen.add(query_id)
        try:
            domain = StandardsDomain(raw["domain"])
            effective_date = validate_iso_date(
                raw["effective_date"], field=f"standard_queries[{index}].effective_date"
            )
        except AuditModelError as e:
            raise StandardsContextError(str(e)) from e
        framework = _optional_text(
            raw["framework"], field=f"standard_queries[{index}].framework"
        )
        raw_fact_ids = raw["fact_ids"]
        if not isinstance(raw_fact_ids, list) or not raw_fact_ids:
            raise StandardsContextError(
                f"standard_queries[{index}].fact_ids는 비어 있지 않은 배열이어야 합니다."
            )
        fact_ids = [
            _identifier(fact_id, field=f"standard_queries[{index}].fact_ids")
            for fact_id in raw_fact_ids
        ]
        if len(fact_ids) != len(set(fact_ids)):
            raise StandardsContextError(
                f"standard_queries[{index}].fact_ids에 중복이 있습니다."
            )
        query_view = {
            "id": query_id,
            "query": require_non_empty(
                raw["query"], field=f"standard_queries[{index}].query"
            ),
            "domain": domain.value,
            "framework": framework,
            "effective_date": effective_date,
            "fact_ids": sorted(fact_ids),
        }
        if "standard_nos" in raw:
            raw_standard_nos = raw["standard_nos"]
            if (
                not isinstance(raw_standard_nos, list)
                or not 1 <= len(raw_standard_nos) <= 20
                or any(
                    not isinstance(value, str)
                    or not _STANDARD_NO_RE.fullmatch(value)
                    for value in raw_standard_nos
                )
            ):
                raise StandardsContextError(
                    f"standard_queries[{index}].standard_nos는 유효한 기준서 번호 "
                    "1~20건이어야 합니다."
                )
            standard_nos = sorted(value.upper() for value in raw_standard_nos)
            if len(standard_nos) != len(set(standard_nos)):
                raise StandardsContextError(
                    f"standard_queries[{index}].standard_nos에 중복이 있습니다."
                )
            query_view["standard_nos"] = standard_nos
        queries.append(query_view)
    return sorted(queries, key=lambda query: query["id"])


def _citation_from_hit(hit: StandardHit, query: dict, retriever: dict) -> dict:
    if not isinstance(hit, StandardHit):
        raise _CitationProblem(
            f"retriever가 StandardHit이 아닌 값을 반환했습니다: {type(hit).__name__}"
        )
    if hit.domain.value != query["domain"]:
        raise _CitationProblem(
            f"query domain({query['domain']})과 hit domain({hit.domain.value})이 다릅니다."
        )
    if query["framework"] is not None and hit.framework != query["framework"]:
        raise _CitationProblem(
            f"query framework({query['framework']})와 hit framework({hit.framework})가 다릅니다."
        )
    if (
        query["effective_date"] is not None
        and hit.effective_date is not None
        and hit.effective_date > query["effective_date"]
    ):
        raise _CitationProblem(
            "citation effective_date가 query 적용일보다 늦습니다: "
            f"{hit.effective_date} > {query['effective_date']}",
            limitation_code="effective_date_mismatch",
        )
    citation_id = _identifier(hit.citation_id, field="StandardHit.citation_id")
    paragraph = require_non_empty(hit.paragraph, field="StandardHit.paragraph")
    corpus_id = _optional_text(hit.corpus_id, field="StandardHit.corpus_id")
    corpus_version = _optional_text(hit.corpus_version, field="StandardHit.corpus_version")
    if corpus_id is None or corpus_version is None:
        raise _CitationProblem(
            "StandardHit에 corpus_id와 corpus_version이 모두 필요합니다.",
            limitation_code="corpus_version_unknown",
        )
    if (
        corpus_id != retriever["corpus_id"]
        or corpus_version != retriever["corpus_version"]
    ):
        raise _CitationProblem(
            "StandardHit corpus provenance가 retriever descriptor와 일치하지 않습니다.",
            limitation_code="corpus_version_unknown",
        )
    retrieved_at = _timestamp_z(
        hit.retrieved_at or retriever["retrieved_at"], field="StandardHit.retrieved_at"
    )
    citation = {
        "id": citation_id,
        "kind": (
            SourceKind.AUDIT_STANDARD.value
            if hit.domain is StandardsDomain.AUDIT
            else SourceKind.ACCOUNTING_STANDARD.value
        ),
        "domain": hit.domain.value,
        "query_ids": [query["id"]],
        "framework": hit.framework,
        "document_id": hit.document_id,
        "paragraph": paragraph,
        "title": hit.title,
        "snippet": hit.snippet,
        "snippet_sha256": hit.snippet_sha256,
        "effective_date": hit.effective_date,
        "edition": hit.edition,
        "source_uri": hit.source_uri,
        "corpus_id": corpus_id,
        "corpus_version": corpus_version,
        "retriever_version": hit.retriever_version or retriever["version"],
        "retrieved_at": retrieved_at,
    }
    passage_metadata = {
        key: value
        for key, value in hit.metadata.items()
        if key not in _QUERY_MATCH_METADATA_FIELDS
    }
    if passage_metadata:
        citation["provider_metadata"] = json.loads(canonical_json(passage_metadata))
    return citation


def _query_match_metadata(hit: StandardHit) -> tuple[str, str | None]:
    """질의별 검색 역할·발췌 hash를 전역 citation 정체성과 분리한다."""
    role = hit.metadata.get("retrieval_role", "result")
    if role not in {"result", "definition"}:
        raise _CitationProblem(f"지원하지 않는 retrieval_role: {role!r}")
    search_text_sha = hit.metadata.get("search_text_sha256")
    if search_text_sha is not None:
        try:
            search_text_sha = _sha256(
                search_text_sha, field="StandardHit.metadata.search_text_sha256"
            )
        except StandardsContextError as e:
            raise _CitationProblem(str(e)) from e
    return role, search_text_sha


def _citation_identity(citation: dict) -> str:
    """query ownership and observation time을 제외한 동일 passage 판정 키."""
    return canonical_json({
        key: value
        for key, value in citation.items()
        if key not in {"id", "query_ids", "retrieved_at"}
    })


def _error_text(error: Exception) -> str:
    detail = str(error).strip() or "상세 메시지 없음"
    return f"{type(error).__name__}: {detail}"[:2000]


def _limitation(
    *,
    code: str,
    description: str,
    severity: str,
    query_ids: list[str],
    citation_ids: list[str],
) -> dict:
    query_ids = sorted(set(query_ids))
    citation_ids = sorted(set(citation_ids))
    identity = {"code": code, "query_ids": query_ids, "citation_ids": citation_ids}
    return {
        "id": f"limitation:{json_sha256(identity)[:20]}",
        "code": code,
        "description": description,
        "severity": severity,
        "query_ids": query_ids,
        "citation_ids": citation_ids,
    }


def build_standards_context(
    audit_facts: Mapping[str, object],
    retriever: StandardsRetriever,
    *,
    retriever_descriptor: Mapping[str, object],
    audit_facts_sha256: str | None = None,
) -> dict:
    """`audit_facts.standard_queries`를 실행해 schema-valid context를 만든다.

    query는 ID 순으로 실행하고 citations/limitations도 ID 순으로 반환한다. 조회 예외,
    잘못된 hit, citation 충돌은 해당 query만 `error`로 남기며 후속 query는 계속한다.
    `audit_facts_sha256`을 생략하면 공통 canonical JSON 규칙으로 계산한다.
    """
    if not isinstance(audit_facts, Mapping):
        raise StandardsContextError("audit_facts는 객체여야 합니다.")
    retriever_view = _retriever_view(retriever_descriptor)
    source = audit_facts.get("source")
    if not isinstance(source, Mapping):
        raise StandardsContextError("audit_facts.source는 객체여야 합니다.")
    workbook_sha = _sha256(source.get("sha256"), field="audit_facts.source.sha256")
    if audit_facts_sha256 is None:
        try:
            facts_sha = json_sha256(dict(audit_facts))
        except AuditModelError as e:
            raise StandardsContextError(str(e)) from e
    else:
        facts_sha = _sha256(audit_facts_sha256, field="audit_facts_sha256")
    queries = _query_views(audit_facts)

    query_results: list[dict] = []
    citations: dict[str, dict] = {}
    passage_ids: dict[str, str] = {}
    limitations: dict[str, dict] = {}

    def add_limitation(**kwargs) -> None:
        item = _limitation(**kwargs)
        limitations.setdefault(item["id"], item)

    for query in queries:
        citation_ids: list[str] = []
        matches: list[dict] = []
        try:
            # framework=None은 audit_facts 계약상 허용된다. Protocol의 구체 구현도
            # corpus 전체 검색 의미로 이를 받아야 한다.
            raw_hits = retriever.search(
                query["query"],
                domain=query["domain"],
                framework=query["framework"],  # type: ignore[arg-type]
                effective_date=query["effective_date"],
                standard_nos=query.get("standard_nos"),
            )
            if not isinstance(raw_hits, list):
                raise _CitationProblem("retriever.search 결과는 StandardHit 배열이어야 합니다.")
            local: list[tuple[dict, int, float | None, str, str | None]] = [
                (
                    _citation_from_hit(hit, query, retriever_view),
                    rank,
                    hit.score,
                    *_query_match_metadata(hit),
                )
                for rank, hit in enumerate(raw_hits, start=1)
            ]

            # query가 중간에 실패해도 일부 citation만 전역에 남지 않도록 임시 사본에서
            # 충돌·중복을 전부 확인한 뒤 한 번에 반영한다.
            staged_citations = dict(citations)
            staged_passages = dict(passage_ids)
            matched_citation_ids: set[str] = set()
            for citation, rank, score, retrieval_role, search_text_sha in local:
                citation_id = citation["id"]
                identity = _citation_identity(citation)
                previous = staged_citations.get(citation_id)
                if previous is not None and _citation_identity(previous) != identity:
                    raise _CitationProblem(
                        f"같은 citation id가 서로 다른 passage를 가리킵니다: {citation_id}"
                    )
                canonical_id = staged_passages.get(identity)
                if canonical_id is None:
                    canonical_id = citation_id
                    staged_passages[identity] = canonical_id
                    staged_citations.setdefault(canonical_id, citation)
                else:
                    existing = dict(staged_citations[canonical_id])
                    existing["query_ids"] = sorted(set(
                        existing.get("query_ids", []) + citation.get("query_ids", [])
                    ))
                    staged_citations[canonical_id] = existing
                if canonical_id not in matched_citation_ids:
                    matched_citation_ids.add(canonical_id)
                    citation_ids.append(canonical_id)
                    matches.append({
                        "citation_id": canonical_id,
                        "rank": rank,
                        "score": score,
                        "retrieval_role": retrieval_role,
                        "search_text_sha256": search_text_sha,
                    })
            citations, passage_ids = staged_citations, staged_passages
            status = "success" if citation_ids else "no_results"
            error_text = None
            unverified_date_ids = [
                citation_id
                for citation_id in citation_ids
                if citations[citation_id].get("effective_date") is None
            ]
            if unverified_date_ids:
                add_limitation(
                    code="effective_date_unverified",
                    description=(
                        "조회 citation에 구조화 시행일이 없어 target 적용일에 유효한 "
                        "판본인지 별도 확인이 필요합니다."
                    ),
                    severity="moderate",
                    query_ids=[query["id"]],
                    citation_ids=unverified_date_ids,
                )
            if status == "no_results":
                add_limitation(
                    code="no_results",
                    description="관련 기준서 passage를 찾지 못했습니다.",
                    severity="moderate",
                    query_ids=[query["id"]],
                    citation_ids=[],
                )
        except StandardsRetrievalFatalError:
            # 인증 실패, malformed envelope, collection drift, search/get 원문 불일치처럼
            # 출처 계약 자체가 깨진 경우 partial brief를 게시하면 안 된다.
            raise
        except Exception as e:  # query 단위 실패 격리 — 다음 query는 계속 실행
            status = "error"
            citation_ids = []
            matches = []
            error_text = _error_text(e)
            add_limitation(
                code="query_failed",
                description=f"기준서 query 실행 실패: {error_text}",
                severity="high",
                query_ids=[query["id"]],
                citation_ids=[],
            )
            if isinstance(e, _CitationProblem):
                add_limitation(
                    code=e.limitation_code,
                    description=str(e),
                    severity="high",
                    query_ids=[query["id"]],
                    citation_ids=[],
                )
            query_limitation_code = (
                e.limitation_code if isinstance(e, StandardsQueryError) else None
            )
            if query_limitation_code is not None:
                add_limitation(
                    code=query_limitation_code,
                    description=str(e),
                    severity="high",
                    query_ids=[query["id"]],
                    citation_ids=[],
                )

        if query["framework"] is None:
            add_limitation(
                code="framework_unknown",
                description="조회할 기준서 framework가 조서에서 식별되지 않았습니다.",
                severity="moderate",
                query_ids=[query["id"]],
                citation_ids=citation_ids,
            )
        if query["effective_date"] is None:
            add_limitation(
                code="effective_date_unknown",
                description="적용 기준일이 식별되지 않아 기준서 적용시점을 확정할 수 없습니다.",
                severity="moderate",
                query_ids=[query["id"]],
                citation_ids=citation_ids,
            )
        query_results.append({
            **query,
            "status": status,
            "error": error_text,
            "citation_ids": citation_ids,
            "matches": matches,
        })

    doc = {
        "schema_version": "standards_context.v1",
        "input": {
            "audit_facts_sha256": facts_sha,
            "workbook_sha256": workbook_sha,
        },
        "retriever": retriever_view,
        "queries": query_results,
        "citations": [citations[key] for key in sorted(citations)],
        "limitations": [limitations[key] for key in sorted(limitations)],
    }
    validate_standards_context(doc)
    return doc
