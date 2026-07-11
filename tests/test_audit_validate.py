from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from excel_to_skill.audit.sources import WorkbookSourceResolver
from excel_to_skill.audit.model import json_sha256
from excel_to_skill.audit.validate import (
    AuditValidationError,
    collect_audit_validation_problems,
    collect_package_audit_validation_problems,
    load_audit_schema,
    raise_for_audit_validation,
    validate_audit_bundle,
    validate_audit_package,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    meta = {
        "source": {"sha256": SHA_A},
        "sheets": [{"name": "Main", "dimensions": "A1:B2"}],
    }
    cells = [
        {"sheet": "Main", "cell": "A1", "row": 1, "col": 1,
         "value": "매출 위험", "formula": None},
        {"sheet": "Main", "cell": "B2", "row": 2, "col": 2,
         "value": "미해결", "formula": None, "border": True},
    ]
    (pkg / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (pkg / "data" / "cells.jsonl").write_text(
        "".join(json.dumps(cell, ensure_ascii=False) + "\n" for cell in cells),
        encoding="utf-8",
    )
    return pkg


def _bundle(tmp_path: Path) -> tuple[Path, dict, dict, dict]:
    pkg = _package(tmp_path)
    content_sha = WorkbookSourceResolver(pkg).resolve("Main!A1:B2").content_sha256
    snippet = "감사인은 중요한 거래유형과 공시의 위험을 식별한다."
    snippet_sha = hashlib.sha256(snippet.encode("utf-8")).hexdigest()

    facts = {
        "schema_version": "audit_facts.v1",
        "source": {"filename": "audit.xlsx", "sha256": SHA_A, "format": "xlsx"},
        "generator": {
            "name": "audit-facts", "version": "1", "kind": "llm", "model": "stub",
            "prompt_sha256": SHA_B, "generated_at": "2026-07-11T00:00:00Z",
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "workpaper": {
            "kind": "risk_assessment", "title": "위험평가", "entity": "Example",
            "period_start": "2026-01-01", "period_end": "2026-12-31",
            "audit_phase": "risk_assessment", "document_state": "partially_completed",
            "purpose": "매출 위험 평가", "source_ids": ["source:1"],
        },
        "sources": [{
            "id": "source:1", "kind": "workbook", "sheet": "Main", "range": "A1:B2",
            "role": "mixed", "content_sha256": content_sha,
        }],
        "facts": [
            {
                "id": "fact:risk", "type": "risk", "description": "매출 인식 위험",
                "status": "identified", "normalized_code": None, "value": None,
                "unit": None, "severity": "high", "confidence": 0.9,
                "source_ids": ["source:1"],
            },
            {
                "id": "fact:open", "type": "open_item", "description": "미해결 검토",
                "status": "unresolved", "normalized_code": None, "value": None,
                "unit": None, "severity": "moderate", "confidence": 0.8,
                "source_ids": ["source:1"],
            },
        ],
        "relations": [{
            "id": "relation:1", "type": "relates_to", "from_fact_id": "fact:risk",
            "to_fact_id": "fact:open", "status": "documented", "confidence": 0.8,
            "source_ids": ["source:1"],
        }],
        "standard_queries": [{
            "id": "query:1", "query": "매출 위험 식별 감사 요구사항", "domain": "audit",
            "framework": "KSA", "effective_date": "2026-01-01",
            "fact_ids": ["fact:risk"], "rationale": "위험평가 배경 확인",
        }],
        "limitations": [{
            "id": "facts-limit:1", "code": "missing_context",
            "description": "담당자 설명 없음", "severity": "low",
            "affected_fact_ids": ["fact:risk"], "source_ids": ["source:1"],
        }],
    }

    context = {
        "schema_version": "standards_context.v1",
        "input": {"audit_facts_sha256": json_sha256(facts), "workbook_sha256": SHA_A},
        "retriever": {
            "name": "stub-rag", "version": "1", "mcp_server": "standards",
            "tool": "search", "corpus_id": "kr-standards",
            "corpus_version": "2026.1", "retrieved_at": "2026-07-11T00:01:00Z",
        },
        "queries": [{
            "id": "query:1", "query": "매출 위험 식별 감사 요구사항", "domain": "audit",
            "framework": "KSA", "effective_date": "2026-01-01",
            "fact_ids": ["fact:risk"], "status": "success", "error": None,
            "citation_ids": ["citation:1"],
            "matches": [{
                "citation_id": "citation:1", "rank": 1, "score": 0.9,
                "retrieval_role": "result", "search_text_sha256": None,
            }],
        }],
        "citations": [{
            "id": "citation:1", "kind": "audit_standard", "domain": "audit",
            "query_ids": ["query:1"], "framework": "KSA", "document_id": "KSA-315",
            "paragraph": "26", "title": "위험의 식별", "snippet": snippet,
            "snippet_sha256": snippet_sha,
            "effective_date": "2026-01-01", "edition": "2026",
            "source_uri": "standards://ksa/315/26", "corpus_id": "kr-standards",
            "corpus_version": "2026.1", "retriever_version": "1",
            "retrieved_at": "2026-07-11T00:01:00Z",
        }],
        "limitations": [{
            "id": "context-limit:1", "code": "ambiguous_passage",
            "description": "관련 문단 추가 검토 필요", "severity": "low",
            "query_ids": ["query:1"], "citation_ids": ["citation:1"],
        }],
    }

    brief = {
        "schema_version": "audit_brief.v1",
        "inputs": {
            "audit_facts_sha256": json_sha256(facts),
            "standards_context_sha256": json_sha256(context),
            "workbook_sha256": SHA_A,
        },
        "generator": {
            "name": "audit-brief", "version": "1", "model": "stub",
            "prompt_sha256": "d" * 64, "generated_at": "2026-07-11T00:02:00Z",
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "readiness": {
            "status": "partial", "reasons": ["미해결 항목 존재"],
            "open_item_fact_ids": ["fact:open"],
        },
        "workpaper": {
            "kind": "risk_assessment", "title": "위험평가", "entity": "Example",
            "period_start": "2026-01-01", "period_end": "2026-12-31",
            "audit_phase": "risk_assessment", "document_state": "partially_completed",
            "purpose": "매출 위험 평가", "fact_ids": ["fact:risk", "fact:open"],
        },
        "summary": {
            "text": "매출 위험과 관련 기준 및 미해결 항목이 있다.",
            "statement_ids": ["statement:fact", "statement:standard", "statement:synthesis",
                              "statement:gap"],
        },
        "statements": [
            {
                "id": "statement:fact", "section": "risks_assertions",
                "type": "documented_fact", "text": "조서에 매출 위험이 기록됐다.",
                "status": "documented", "confidence": 0.9,
                "fact_ids": ["fact:risk"], "standard_citation_ids": [],
            },
            {
                "id": "statement:standard", "section": "standards",
                "type": "authoritative_context", "text": "관련 위험 식별 요구사항이 있다.",
                "status": "documented", "confidence": 0.9,
                "fact_ids": [], "standard_citation_ids": ["citation:1"],
            },
            {
                "id": "statement:synthesis", "section": "risks_assertions",
                "type": "synthesis", "text": "조서 위험은 관련 기준의 검토 대상이다.",
                "status": "inferred", "confidence": 0.8,
                "fact_ids": ["fact:risk"], "standard_citation_ids": ["citation:1"],
            },
            {
                "id": "statement:gap", "section": "open_items", "type": "gap",
                "text": "기준 대응 여부가 조서에 명확하지 않다.", "status": "unresolved",
                "confidence": 0.7, "fact_ids": ["fact:open"],
                "standard_citation_ids": ["citation:1"],
            },
        ],
        "limitations": [{
            "id": "brief-limit:1", "description": "사실·기준 맥락 추가 검토 필요",
            "severity": "low", "audit_facts_limitation_ids": ["facts-limit:1"],
            "standards_context_limitation_ids": ["context-limit:1"],
            "affected_statement_ids": ["statement:gap"],
        }],
    }
    return pkg, facts, context, brief


def test_valid_bundle_passes_schema_links_and_strict_helpers(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    assert load_audit_schema("audit_facts")["title"] == "data/audit_facts.json"
    assert load_audit_schema("standards_context.schema.json")["type"] == "object"
    assert collect_audit_validation_problems(pkg, facts, context, brief) == []
    validate_audit_bundle(pkg, facts, context, brief)
    raise_for_audit_validation([])


def test_package_loader_validates_the_three_fixed_artifact_paths(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    for name, doc in (
        ("audit_facts.json", facts),
        ("standards_context.json", context),
        ("audit_brief.json", brief),
    ):
        (pkg / "data" / name).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8"
        )
    assert collect_package_audit_validation_problems(pkg) == []
    validate_audit_package(pkg)


def test_schema_failures_are_returned_with_artifact_and_path(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    del facts["facts"][0]["description"]
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    assert any("audit_facts.schema:/facts/0" in problem for problem in problems)
    assert any("description" in problem for problem in problems)


def test_audit_facts_checks_duplicate_and_all_cross_record_links(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    facts["sources"].append(copy.deepcopy(facts["sources"][0]))
    facts["facts"].append(copy.deepcopy(facts["facts"][0]))
    facts["workpaper"]["source_ids"] = ["source:missing"]
    facts["facts"][0]["source_ids"] = ["source:missing"]
    facts["relations"][0].update({
        "from_fact_id": "fact:missing", "to_fact_id": "fact:other",
        "source_ids": ["source:missing"],
    })
    facts["standard_queries"][0]["fact_ids"] = ["fact:missing"]
    facts["limitations"][0]["affected_fact_ids"] = ["fact:missing"]
    facts["limitations"][0]["source_ids"] = ["source:missing"]

    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "audit_facts.sources[1].id: duplicate" in joined
    assert "audit_facts.facts[2].id: duplicate" in joined
    assert "audit_facts.workpaper.source_ids[0]: unknown source" in joined
    assert "relations[0].from_fact_id: unknown fact" in joined
    assert "relations[0].to_fact_id: unknown fact" in joined
    assert "relations[0].source_ids[0]: unknown source" in joined
    assert "standard_queries[0].fact_ids[0]: unknown fact" in joined
    assert "limitations[0].affected_fact_ids[0]: unknown fact" in joined


def test_workbook_source_must_resolve_and_match_ledger_digest(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    facts["sources"][0]["range"] = "B1"
    facts["sources"][0]["content_sha256"] = "0" * 64
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    assert any("cells.jsonl" in problem for problem in problems)

    _, facts, context, brief = _bundle(tmp_path / "second")
    facts["sources"][0]["content_sha256"] = "0" * 64
    problems = collect_audit_validation_problems(tmp_path / "second" / "pkg", facts, context, brief)
    assert any("content_sha256: digest mismatch" in problem for problem in problems)


def test_standards_query_citation_links_and_snippet_digest(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    context["queries"][0]["fact_ids"] = ["fact:missing"]
    context["queries"][0]["citation_ids"] = ["citation:missing"]
    context["queries"][0]["matches"] = [{
        "citation_id": "citation:missing", "rank": 1, "score": 0.9,
    }]
    context["citations"][0]["query_ids"] = ["query:missing"]
    context["citations"][0]["snippet_sha256"] = "0" * 64
    context["limitations"][0]["query_ids"] = ["query:missing"]
    context["limitations"][0]["citation_ids"] = ["citation:missing"]
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "queries[0].fact_ids[0]: unknown fact" in joined
    assert "queries[0].citation_ids[0]: unknown citation" in joined
    assert "queries[0].matches[0].citation_id: unknown citation" in joined
    assert "citations[0].query_ids[0]: unknown query" in joined
    assert "citations[0].snippet_sha256: digest mismatch" in joined
    assert "limitations[0].query_ids[0]: unknown query" in joined
    assert "limitations[0].citation_ids[0]: unknown citation" in joined


def test_one_standard_citation_can_belong_to_multiple_queries(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    second = copy.deepcopy(facts["standard_queries"][0])
    second.update({"id": "query:2", "query": "두 번째 관련 질의"})
    facts["standard_queries"].append(second)
    second_result = copy.deepcopy(context["queries"][0])
    second_result.update({"id": "query:2", "query": "두 번째 관련 질의"})
    context["queries"].append(second_result)
    context["citations"][0]["query_ids"] = ["query:1", "query:2"]
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief["inputs"]["audit_facts_sha256"] = json_sha256(facts)
    brief["inputs"]["standards_context_sha256"] = json_sha256(context)
    assert collect_audit_validation_problems(pkg, facts, context, brief) == []


def test_structured_standard_number_order_is_not_a_provenance_difference(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    facts["standard_queries"][0]["standard_nos"] = ["330", "315"]
    context["queries"][0]["standard_nos"] = ["315", "330"]
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief["inputs"].update({
        "audit_facts_sha256": json_sha256(facts),
        "standards_context_sha256": json_sha256(context),
    })

    assert collect_audit_validation_problems(pkg, facts, context, brief) == []


def test_query_matches_must_exactly_project_ordered_citation_ids(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    context["queries"][0]["matches"] = [{
        "citation_id": "citation:missing", "rank": 2, "score": 0.4,
    }]

    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "matches[0].citation_id: unknown citation" in joined
    assert "must exactly equal matches[].citation_id in order" in joined


def test_citation_must_match_owner_and_retriever_provenance(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    citation = context["citations"][0]
    citation.update({
        "domain": "accounting",
        "framework": "K-IFRS",
        "corpus_id": "other-corpus",
        "corpus_version": "2099.1",
        "retriever_version": "other-retriever",
        "effective_date": "2026-02-01",
    })

    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "citations[0].domain: differs from owning query 'query:1'" in joined
    assert "citations[0].framework: differs from owning query 'query:1'" in joined
    assert "citations[0].corpus_id: differs from standards_context.retriever" in joined
    assert "citations[0].corpus_version: differs from standards_context.retriever" in joined
    assert "citations[0].retriever_version: differs from standards_context.retriever" in joined
    assert "citations[0].effective_date: later than owning query 'query:1'" in joined


def test_brief_checks_fact_citation_statement_and_limitation_links(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    brief["readiness"]["open_item_fact_ids"] = ["fact:missing"]
    brief["workpaper"]["fact_ids"] = ["fact:missing"]
    brief["summary"]["statement_ids"] = ["statement:missing"]
    brief["statements"][0]["fact_ids"] = ["fact:missing"]
    brief["statements"][1]["standard_citation_ids"] = ["citation:missing"]
    brief["limitations"][0]["audit_facts_limitation_ids"] = ["facts-limit:missing"]
    brief["limitations"][0]["standards_context_limitation_ids"] = [
        "context-limit:missing"
    ]
    brief["limitations"][0]["affected_statement_ids"] = ["statement:missing"]
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "readiness.open_item_fact_ids[0]: unknown fact" in joined
    assert "workpaper.fact_ids[0]: unknown fact" in joined
    assert "summary.statement_ids[0]: unknown statement" in joined
    assert "statements[0].fact_ids[0]: unknown fact" in joined
    assert "statements[1].standard_citation_ids[0]: unknown citation" in joined
    assert "audit_facts limitation" in joined
    assert "standards_context limitation" in joined
    assert "affected_statement_ids[0]: unknown statement" in joined


def test_brief_integrity_rejects_hidden_inputs_wrong_identity_and_ready_blockers(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    second_query = copy.deepcopy(facts["standard_queries"][0])
    second_query.update({"id": "query:error", "query": "실패하는 추가 질의"})
    facts["standard_queries"].append(second_query)
    context["queries"].append({
        "id": "query:error", "query": "실패하는 추가 질의", "domain": "audit",
        "framework": "KSA", "effective_date": "2026-01-01",
        "fact_ids": ["fact:risk"], "status": "error", "error": "unavailable",
        "citation_ids": [],
        "matches": [],
    })
    facts["limitations"][0].update({
        "code": "extraction_incomplete", "severity": "high",
    })
    brief["workpaper"]["title"] = "다른 제목"
    brief["readiness"].update({"status": "ready", "open_item_fact_ids": []})
    brief["limitations"][0].update({
        "severity": "high", "standards_context_limitation_ids": [],
    })
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief["inputs"].update({
        "audit_facts_sha256": json_sha256(facts),
        "standards_context_sha256": json_sha256(context),
    })

    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "workpaper.title: differs from audit_facts.workpaper" in joined
    assert "missing unresolved open_item fact 'fact:open'" in joined
    assert "cannot be 'ready'" in joined
    assert "a standards query failed" in joined
    assert "workbook fact extraction is incomplete" in joined
    assert "missing standards_context input limitation 'context-limit:1'" in joined


def test_ready_rejects_no_results_and_unverified_standards_identity(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    second_query = copy.deepcopy(facts["standard_queries"][0])
    second_query.update({"id": "query:none", "query": "검색 결과 없는 질의"})
    facts["standard_queries"].append(second_query)
    context["queries"].append({
        "id": "query:none",
        "query": "검색 결과 없는 질의",
        "domain": "audit",
        "framework": "KSA",
        "effective_date": "2026-01-01",
        "fact_ids": ["fact:risk"],
        "status": "no_results",
        "error": None,
        "citation_ids": [],
        "matches": [],
    })
    context["limitations"].append({
        "id": "context-limit:identity",
        "code": "effective_date_unknown",
        "description": "적용 기준일을 확정하지 못했다.",
        "severity": "moderate",
        "query_ids": ["query:none"],
        "citation_ids": [],
    })
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief["inputs"].update({
        "audit_facts_sha256": json_sha256(facts),
        "standards_context_sha256": json_sha256(context),
    })
    brief["readiness"]["status"] = "ready"

    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "a standards query returned no results" in joined
    assert "standards framework or effective date remains unverified" in joined


def test_empty_facts_allow_only_an_honest_not_ready_source_free_gap(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    facts.update({
        "facts": [], "relations": [], "standard_queries": [], "limitations": [],
    })
    context.update({"queries": [], "citations": [], "limitations": []})
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief["readiness"] = {
        "status": "not_ready",
        "reasons": ["추출된 workbook fact가 없음"],
        "open_item_fact_ids": [],
    }
    brief["workpaper"].update({"fact_ids": []})
    brief["summary"] = {
        "text": "추출된 사실이 없어 분석 준비가 되지 않았다.",
        "statement_ids": ["statement:empty"],
    }
    brief["statements"] = [{
        "id": "statement:empty", "section": "open_items", "type": "gap",
        "text": "추출된 workbook fact가 없다.", "status": "unknown",
        "confidence": 1.0, "fact_ids": [], "standard_citation_ids": [],
    }]
    brief["limitations"] = []
    brief["inputs"].update({
        "audit_facts_sha256": json_sha256(facts),
        "standards_context_sha256": json_sha256(context),
    })

    assert collect_audit_validation_problems(pkg, facts, context, brief) == []

    brief["readiness"]["status"] = "partial"
    brief["statements"] = []
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    joined = "\n".join(problems)
    assert "must be 'not_ready' when no workbook facts were extracted" in joined
    assert "a gap statement is required when no workbook facts were extracted" in joined


def test_source_free_gap_is_rejected_when_workbook_facts_exist(tmp_path: Path) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    brief["statements"][3].update({
        "status": "unknown", "fact_ids": [], "standard_citation_ids": [],
    })
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    assert any(
        "source-free gap is allowed only when no workbook facts were extracted" in problem
        for problem in problems
    )


def test_global_id_namespace_allows_query_pair_but_rejects_cross_kind_collision(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    # audit_facts query plan + standards_context query result intentionally share one ID.
    assert not any(
        "global id collision 'query:1'" in problem
        for problem in collect_audit_validation_problems(pkg, facts, context, brief)
    )

    old_statement_id = brief["statements"][0]["id"]
    brief["statements"][0]["id"] = "fact:risk"
    brief["summary"]["statement_ids"] = [
        "fact:risk" if item == old_statement_id else item
        for item in brief["summary"]["statement_ids"]
    ]
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    assert any(
        "global id collision 'fact:risk'" in problem
        for problem in problems
    )


@pytest.mark.parametrize(
    ("statement_index", "field", "value"),
    [
        (0, "standard_citation_ids", ["citation:1"]),
        (1, "fact_ids", ["fact:risk"]),
        (2, "standard_citation_ids", []),
        (3, "fact_ids", []),
    ],
)
def test_statement_types_enforce_workbook_standard_source_separation(
    tmp_path: Path, statement_index: int, field: str, value: list[str]
) -> None:
    pkg, facts, context, brief = _bundle(tmp_path)
    brief["statements"][statement_index][field] = value
    problems = collect_audit_validation_problems(pkg, facts, context, brief)
    assert any(
        f"audit_brief.statements[{statement_index}]: source separation violation" in problem
        for problem in problems
    )


def test_raise_helper_preserves_all_collected_problems() -> None:
    problems = ["first", "second"]
    with pytest.raises(AuditValidationError) as exc:
        raise_for_audit_validation(problems)
    assert exc.value.problems == tuple(problems)
    assert "first" in str(exc.value) and "second" in str(exc.value)


def test_package_loader_reports_missing_artifacts_without_crashing(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    problems = collect_package_audit_validation_problems(pkg)
    assert len(problems) == 3
    assert all("file missing" in problem for problem in problems)
