"""standards context 조립 — 주입형 retriever, query 격리, provenance, schema."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from excel_to_skill.audit.context import (
    StandardsContextError,
    build_standards_context,
    validate_standards_context,
)
from excel_to_skill.audit.standards import StandardHit

_SHA = "a" * 64
_FACTS_SHA = "b" * 64
_RETRIEVER = {
    "name": "standards-rag",
    "version": "1.2.0",
    "mcp_server": "accounting-standards",
    "tool": "search",
    "corpus_id": "kr-standards",
    "corpus_version": "2026.1",
    "retrieved_at": "2026-07-11T00:00:00Z",
}


def _query(
    query_id: str,
    query: str,
    *,
    domain: str = "audit",
    framework: str | None = "KSA",
    effective_date: str | None = "2026-01-01",
    standard_nos: list[str] | None = None,
) -> dict:
    result = {
        "id": query_id,
        "query": query,
        "domain": domain,
        "framework": framework,
        "effective_date": effective_date,
        "fact_ids": [f"fact:{query_id[-1]}"],
        "rationale": "조서 사실에 관련된 기준서 문맥이 필요함",
    }
    if standard_nos is not None:
        result["standard_nos"] = standard_nos
    return result


def _facts(*queries: dict) -> dict:
    return {
        "source": {"filename": "audit.xlsx", "sha256": _SHA, "format": "xlsx"},
        "standard_queries": list(queries),
    }


def _hit(**changes) -> StandardHit:
    values = {
        "domain": "audit",
        "framework": "KSA",
        "document_id": "KSA-315",
        "paragraph": "26",
        "title": "중요왜곡표시위험의 식별과 평가",
        "snippet": "감사인은 중요한 거래유형과 공시에 관한 위험을 식별한다.",
        "score": 0.91,
        "edition": "2026",
        "effective_date": "2026-01-01",
        "source_uri": "standards://ksa/315/26",
        "corpus_id": "kr-standards",
        "corpus_version": "2026.1",
        "retriever_version": None,
        "retrieved_at": "2026-07-11T09:00:00+09:00",
    }
    values.update(changes)
    return StandardHit(**values)


class StubRetriever:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def search(
        self, query, *, domain, framework, effective_date=None, standard_nos=None
    ):
        self.calls.append({
            "query": query,
            "domain": domain,
            "framework": framework,
            "effective_date": effective_date,
            "standard_nos": standard_nos,
        })
        response = self.responses[query]
        if isinstance(response, Exception):
            raise response
        return response


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items}


def test_build_context_maps_hit_provenance_and_validates_schema() -> None:
    query = _query("query:audit", "위험평가 요구사항")
    stub = StubRetriever({"위험평가 요구사항": [_hit()]})
    doc = build_standards_context(
        _facts(query),
        stub,
        retriever_descriptor=_RETRIEVER,
        audit_facts_sha256=_FACTS_SHA,
    )

    validate_standards_context(doc)
    assert doc["input"] == {
        "audit_facts_sha256": _FACTS_SHA,
        "workbook_sha256": _SHA,
    }
    assert doc["retriever"] == _RETRIEVER
    result = doc["queries"][0]
    assert result["status"] == "success" and result["error"] is None
    citation = doc["citations"][0]
    assert result["citation_ids"] == [citation["id"]]
    assert result["matches"] == [{
        "citation_id": citation["id"], "rank": 1, "score": 0.91,
        "retrieval_role": "result", "search_text_sha256": None,
    }]
    assert citation["kind"] == "audit_standard"
    assert citation["domain"] == "audit" and citation["query_ids"] == ["query:audit"]
    assert citation["snippet_sha256"] == _hit().snippet_sha256
    assert citation["corpus_id"] == "kr-standards"
    assert citation["corpus_version"] == "2026.1"
    assert citation["retriever_version"] == _RETRIEVER["version"]
    assert citation["retrieved_at"] == "2026-07-11T00:00:00Z"
    assert "score" not in citation


def test_structured_standard_numbers_are_preserved_and_sent_to_retriever() -> None:
    query = _query(
        "query:audit", "복수 기준서 요구사항", standard_nos=["330", "315"]
    )
    stub = StubRetriever({"복수 기준서 요구사항": [_hit()]})

    doc = build_standards_context(
        _facts(query), stub, retriever_descriptor=_RETRIEVER
    )

    assert doc["queries"][0]["standard_nos"] == ["315", "330"]
    assert stub.calls[0]["standard_nos"] == ["315", "330"]


def test_query_success_no_results_and_error_are_isolated() -> None:
    queries = [
        _query("query:c", "성공"),
        _query("query:a", "실패"),
        _query("query:b", "없음"),
    ]
    stub = StubRetriever({
        "성공": [_hit()],
        "실패": RuntimeError("stub unavailable"),
        "없음": [],
    })
    doc = build_standards_context(
        _facts(*queries), stub, retriever_descriptor=_RETRIEVER
    )

    results = _by_id(doc["queries"])
    assert [q["id"] for q in doc["queries"]] == ["query:a", "query:b", "query:c"]
    assert results["query:a"]["status"] == "error"
    assert "RuntimeError" in results["query:a"]["error"]
    assert results["query:a"]["matches"] == []
    assert results["query:b"]["status"] == "no_results"
    assert results["query:b"]["matches"] == []
    assert results["query:c"]["status"] == "success"
    assert [call["query"] for call in stub.calls] == ["실패", "없음", "성공"]
    limitations = {(item["code"], tuple(item["query_ids"])) for item in doc["limitations"]}
    assert ("query_failed", ("query:a",)) in limitations
    assert ("no_results", ("query:b",)) in limitations


def test_duplicate_hits_are_deduped_deterministically_across_queries() -> None:
    first = _query("query:a", "첫 query")
    second = _query("query:b", "둘째 query")
    hit = _hit()
    responses = {"첫 query": [hit, hit], "둘째 query": [hit]}
    doc = build_standards_context(
        _facts(second, first),
        StubRetriever(responses),
        retriever_descriptor=_RETRIEVER,
        audit_facts_sha256=_FACTS_SHA,
    )
    reordered = build_standards_context(
        _facts(first, second),
        StubRetriever(responses),
        retriever_descriptor=_RETRIEVER,
        audit_facts_sha256=_FACTS_SHA,
    )

    assert doc == reordered
    assert len(doc["citations"]) == 1
    citation_id = doc["citations"][0]["id"]
    assert doc["citations"][0]["query_ids"] == ["query:a", "query:b"]
    assert all(result["citation_ids"] == [citation_id] for result in doc["queries"])
    assert all(result["matches"] == [{
        "citation_id": citation_id, "rank": 1, "score": 0.91,
        "retrieval_role": "result", "search_text_sha256": None,
    }] for result in doc["queries"])


def test_cross_query_dedupe_preserves_each_query_rank_and_score() -> None:
    first = _query("query:a", "첫 query")
    second = _query("query:b", "둘째 query")
    shared = _hit(score=0.91)
    other = _hit(
        paragraph="27",
        snippet="감사인은 식별된 위험에 대응하는 절차를 설계한다.",
        score=0.98,
    )
    doc = build_standards_context(
        _facts(first, second),
        StubRetriever({
            "첫 query": [shared],
            "둘째 query": [other, _hit(score=0.37)],
        }),
        retriever_descriptor=_RETRIEVER,
    )

    results = _by_id(doc["queries"])
    shared_id = results["query:a"]["citation_ids"][0]
    other_id = results["query:b"]["citation_ids"][0]
    assert results["query:a"]["matches"] == [{
        "citation_id": shared_id, "rank": 1, "score": 0.91,
        "retrieval_role": "result", "search_text_sha256": None,
    }]
    assert results["query:b"]["matches"] == [
        {"citation_id": other_id, "rank": 1, "score": 0.98,
         "retrieval_role": "result", "search_text_sha256": None},
        {"citation_id": shared_id, "rank": 2, "score": 0.37,
         "retrieval_role": "result", "search_text_sha256": None},
    ]
    shared_citation = next(item for item in doc["citations"] if item["id"] == shared_id)
    assert shared_citation["query_ids"] == ["query:a", "query:b"]
    assert "score" not in shared_citation


def test_same_cid_can_be_definition_then_result_without_citation_conflict() -> None:
    first = _query("query:a", "정의로 조회")
    second = _query("query:b", "일반 결과로 조회")
    excerpt = "중요한 거래유형"
    full = _hit().snippet
    doc = build_standards_context(
        _facts(first, second),
        StubRetriever({
            "정의로 조회": [_hit(
                score=None,
                metadata={
                    "retrieval_role": "definition",
                    "search_text_sha256": hashlib.sha256(
                        excerpt.encode("utf-8")
                    ).hexdigest(),
                },
            )],
            "일반 결과로 조회": [_hit(
                metadata={
                    "retrieval_role": "result",
                    "search_text_sha256": hashlib.sha256(
                        full.encode("utf-8")
                    ).hexdigest(),
                },
            )],
        }),
        retriever_descriptor=_RETRIEVER,
    )

    results = _by_id(doc["queries"])
    assert all(result["status"] == "success" for result in results.values())
    assert len(doc["citations"]) == 1
    assert results["query:a"]["matches"][0]["retrieval_role"] == "definition"
    assert results["query:b"]["matches"][0]["retrieval_role"] == "result"
    assert "search_text_sha256" not in doc["citations"][0].get(
        "provider_metadata", {}
    )


def test_semantically_duplicate_provider_ids_share_one_citation() -> None:
    query = _query("query:a", "중복 passage")
    hits = [_hit(citation_id="provider:a"), _hit(citation_id="provider:b", score=0.5)]
    doc = build_standards_context(
        _facts(query),
        StubRetriever({"중복 passage": hits}),
        retriever_descriptor=_RETRIEVER,
    )
    assert [c["id"] for c in doc["citations"]] == ["provider:a"]
    assert doc["queries"][0]["citation_ids"] == ["provider:a"]
    assert doc["queries"][0]["matches"] == [{
        "citation_id": "provider:a", "rank": 1, "score": 0.91,
        "retrieval_role": "result", "search_text_sha256": None,
    }]


def test_bad_hit_provenance_errors_only_its_query() -> None:
    bad = _query("query:a", "불완전 provenance")
    good = _query("query:b", "정상 provenance")
    doc = build_standards_context(
        _facts(bad, good),
        StubRetriever({
            "불완전 provenance": [_hit(corpus_version=None)],
            "정상 provenance": [_hit()],
        }),
        retriever_descriptor=_RETRIEVER,
    )
    results = _by_id(doc["queries"])
    assert results["query:a"]["status"] == "error"
    assert results["query:a"]["matches"] == []
    assert results["query:b"]["status"] == "success"
    assert {item["code"] for item in doc["limitations"]} >= {
        "query_failed",
        "corpus_version_unknown",
    }
    assert len(doc["citations"]) == 1


def test_same_citation_id_with_different_passages_is_not_silently_merged() -> None:
    first = _query("query:a", "첫 passage")
    conflict = _query("query:b", "충돌 passage")
    doc = build_standards_context(
        _facts(first, conflict),
        StubRetriever({
            "첫 passage": [_hit(citation_id="provider:same")],
            "충돌 passage": [_hit(citation_id="provider:same", snippet="다른 원문")],
        }),
        retriever_descriptor=_RETRIEVER,
    )
    results = _by_id(doc["queries"])
    assert results["query:a"]["status"] == "success"
    assert results["query:b"]["status"] == "error"
    assert results["query:b"]["matches"] == []
    assert "서로 다른 passage" in results["query:b"]["error"]
    assert "ambiguous_passage" in {item["code"] for item in doc["limitations"]}
    assert len(doc["citations"]) == 1


def test_unknown_framework_and_date_are_recorded_as_limitations() -> None:
    query = _query(
        "query:a", "범용 검색", framework=None, effective_date=None
    )
    stub = StubRetriever({"범용 검색": []})
    doc = build_standards_context(
        _facts(query), stub, retriever_descriptor=_RETRIEVER
    )
    assert stub.calls[0]["framework"] is None
    assert {item["code"] for item in doc["limitations"]} == {
        "no_results",
        "framework_unknown",
        "effective_date_unknown",
    }


def test_hit_effective_date_later_than_query_is_isolated_as_error() -> None:
    query = _query("query:a", "과거 적용일", effective_date="2026-01-01")
    doc = build_standards_context(
        _facts(query),
        StubRetriever({"과거 적용일": [_hit(effective_date="2026-02-01")]}),
        retriever_descriptor=_RETRIEVER,
    )

    result = doc["queries"][0]
    assert result["status"] == "error"
    assert result["citation_ids"] == [] and result["matches"] == []
    assert "query 적용일보다 늦습니다" in result["error"]
    assert {item["code"] for item in doc["limitations"]} >= {
        "query_failed", "effective_date_mismatch",
    }


def test_unverified_date_limitation_links_only_undated_matches() -> None:
    query = _query("query:a", "시행일 일부 미확인")
    undated = _hit(effective_date=None, score=0.88)
    dated = _hit(
        paragraph="27",
        snippet="감사인은 식별된 위험에 대응하는 절차를 설계한다.",
        effective_date="2025-01-01",
        score=0.77,
    )
    doc = build_standards_context(
        _facts(query),
        StubRetriever({"시행일 일부 미확인": [undated, dated]}),
        retriever_descriptor=_RETRIEVER,
    )

    result = doc["queries"][0]
    limitation = next(
        item for item in doc["limitations"]
        if item["code"] == "effective_date_unverified"
    )
    assert limitation["citation_ids"] == [result["citation_ids"][0]]


def test_invalid_descriptor_fails_before_retriever_call() -> None:
    stub = StubRetriever({"q": [_hit()]})
    descriptor = {**_RETRIEVER, "retrieved_at": "2026-07-11T00:00:00"}
    with pytest.raises(StandardsContextError, match="timezone"):
        build_standards_context(
            _facts(_query("query:a", "q")),
            stub,
            retriever_descriptor=descriptor,
        )
    assert stub.calls == []


def test_schema_validation_rejects_tampered_document() -> None:
    doc = build_standards_context(
        _facts(_query("query:a", "q")),
        StubRetriever({"q": [_hit()]}),
        retriever_descriptor=_RETRIEVER,
    )
    broken = deepcopy(doc)
    broken["citations"][0]["kind"] = "workbook"
    with pytest.raises(StandardsContextError, match="schema 불일치"):
        validate_standards_context(broken)
