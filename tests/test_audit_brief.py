from __future__ import annotations

import copy
from collections import deque

import pytest

from excel_to_skill.audit.brief import build_audit_brief
from excel_to_skill.audit.llm import AuditLLMError
from excel_to_skill.audit.model import json_sha256


_SHA = "a" * 64


def _facts() -> dict:
    return {
        "schema_version": "audit_facts.v1",
        "source": {"filename": "audit.xlsx", "sha256": _SHA, "format": "xlsx"},
        "generator": {
            "name": "test",
            "version": "1",
            "kind": "llm",
            "model": "stub",
            "prompt_sha256": "b" * 64,
            "generated_at": "2026-07-11T00:00:00Z",
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "workpaper": {
            "kind": "risk_assessment",
            "title": "위험평가",
            "entity": None,
            "period_start": None,
            "period_end": None,
            "audit_phase": "risk_assessment",
            "document_state": "partially_completed",
            "purpose": "위험을 식별한다.",
            "source_ids": ["source:title"],
        },
        "sources": [{
            "id": "source:title",
            "kind": "workbook",
            "sheet": "Main",
            "range": "A1",
            "role": "label",
            "content_sha256": "c" * 64,
        }],
        "facts": [{
            "id": "fact:purpose",
            "type": "workpaper_attribute",
            "description": "위험평가 조서이다.",
            "status": "documented",
            "normalized_code": "workpaper_purpose",
            "value": None,
            "unit": None,
            "severity": None,
            "confidence": 0.9,
            "source_ids": ["source:title"],
        }],
        "relations": [],
        "standard_queries": [],
        "limitations": [],
    }


def _context(facts: dict) -> dict:
    return {
        "schema_version": "standards_context.v1",
        "input": {
            "audit_facts_sha256": json_sha256(facts),
            "workbook_sha256": _SHA,
        },
        "retriever": {
            "name": "not-configured",
            "version": None,
            "mcp_server": "not-configured",
            "tool": "not-configured",
            "corpus_id": "not-configured",
            "corpus_version": "not-configured",
            "retrieved_at": "2026-07-11T00:00:00Z",
        },
        "queries": [],
        "citations": [],
        "limitations": [],
    }


def _authored() -> dict:
    return {
        "readiness": {
            "status": "partial",
            "reasons": ["기준서 문맥 미연결"],
            "open_item_fact_ids": [],
        },
        "workpaper": {
            "kind": "risk_assessment",
            "title": "위험평가",
            "entity": None,
            "period_start": None,
            "period_end": None,
            "audit_phase": "risk_assessment",
            "document_state": "partially_completed",
            "purpose": "위험을 식별한다.",
            "fact_ids": ["fact:purpose"],
        },
        "summary": {
            "text": "위험평가 목적이 문서화되어 있다.",
            "statement_ids": ["brief:purpose"],
        },
        "statements": [{
            "id": "brief:purpose",
            "section": "identity_scope",
            "type": "documented_fact",
            "text": "이 문서는 위험평가 조서이다.",
            "status": "documented",
            "confidence": 0.9,
            "fact_ids": ["fact:purpose"],
            "standard_citation_ids": [],
        }],
        "limitations": [],
    }


class StubClient:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.popleft()


def test_build_brief_keeps_inputs_separate_and_adds_provenance() -> None:
    facts = _facts()
    context = _context(facts)
    client = StubClient([_authored()])
    brief = build_audit_brief(
        facts,
        context,
        client=client,
        model="stub-model",
        generated_at="2026-07-11T00:00:00Z",
    )

    assert brief["schema_version"] == "audit_brief.v1"
    assert brief["review"]["status"] == "draft"
    assert brief["inputs"] == {
        "audit_facts_sha256": json_sha256(facts),
        "standards_context_sha256": json_sha256(context),
        "workbook_sha256": _SHA,
    }
    assert brief["statements"][0]["fact_ids"] == ["fact:purpose"]
    assert brief["statements"][0]["standard_citation_ids"] == []
    assert "standards_context" in client.calls[0]["user"]


def test_brief_schema_rejects_synthesis_without_both_source_types() -> None:
    facts = _facts()
    context = _context(facts)
    invalid = _authored()
    invalid["statements"][0].update({
        "type": "synthesis",
        "status": "inferred",
        "standard_citation_ids": [],
    })
    client = StubClient([invalid, invalid])
    with pytest.raises(AuditLLMError, match="응답 검증 실패"):
        build_audit_brief(facts, context, client=client, model="stub")
    assert len(client.calls) == 2


def test_build_brief_deterministically_surfaces_identity_blockers_and_inputs() -> None:
    facts = _facts()
    facts["facts"].append({
        "id": "fact:open",
        "type": "open_item",
        "description": "담당자 답변이 남아 있다.",
        "status": "unresolved",
        "normalized_code": None,
        "value": None,
        "unit": None,
        "severity": "moderate",
        "confidence": 0.8,
        "source_ids": ["source:title"],
    })
    facts["standard_queries"] = [{
        "id": "query:failed",
        "query": "위험평가 문서화 요구사항",
        "domain": "audit",
        "framework": "KSA",
        "effective_date": None,
        "fact_ids": ["fact:purpose"],
        "rationale": "기준 맥락 확인",
    }]
    facts["limitations"] = [{
        "id": "facts-limit:extract",
        "code": "extraction_incomplete",
        "description": "한 영역에서 사실을 추출하지 못했다.",
        "severity": "moderate",
        "affected_fact_ids": [],
        "source_ids": ["source:title"],
    }]
    context = _context(facts)
    context["queries"] = [{
        "id": "query:failed",
        "query": "위험평가 문서화 요구사항",
        "domain": "audit",
        "framework": "KSA",
        "effective_date": None,
        "fact_ids": ["fact:purpose"],
        "status": "error",
        "error": "retriever unavailable",
        "citation_ids": [],
        "matches": [],
    }]
    context["limitations"] = [{
        "id": "context-limit:failed",
        "code": "query_failed",
        "description": "기준서 조회가 실패했다.",
        "severity": "high",
        "query_ids": ["query:failed"],
        "citation_ids": [],
    }]
    authored = _authored()
    authored["readiness"] = {
        "status": "ready", "reasons": ["모델 판단"], "open_item_fact_ids": [],
    }
    authored["workpaper"].update({
        "kind": "other", "title": "잘못된 제목", "purpose": None,
    })
    authored["limitations"] = [{
        "id": "brief-limit:model",
        "description": "조회 제한",
        "severity": "low",
        "audit_facts_limitation_ids": [],
        "standards_context_limitation_ids": ["context-limit:failed"],
        "affected_statement_ids": [],
    }]

    brief = build_audit_brief(
        facts,
        context,
        client=StubClient([authored]),
        model="stub",
        generated_at="2026-07-11T00:00:00Z",
    )

    for field in (
        "kind", "title", "entity", "period_start", "period_end", "audit_phase",
        "document_state", "purpose",
    ):
        assert brief["workpaper"][field] == facts["workpaper"][field]
    assert brief["readiness"]["status"] == "partial"
    assert brief["readiness"]["open_item_fact_ids"] == ["fact:open"]
    assert "One or more standards queries failed." in brief["readiness"]["reasons"]
    assert "Workbook fact extraction is incomplete." in brief["readiness"]["reasons"]
    assert brief["limitations"][0]["severity"] == "high"
    assert {
        item
        for limitation in brief["limitations"]
        for item in limitation["audit_facts_limitation_ids"]
    } == {"facts-limit:extract"}
    assert {
        item
        for limitation in brief["limitations"]
        for item in limitation["standards_context_limitation_ids"]
    } == {"context-limit:failed"}


def test_empty_workbook_facts_become_not_ready_source_free_gap() -> None:
    facts = _facts()
    facts["facts"] = []
    facts["limitations"] = [{
        "id": "facts-limit:empty",
        "code": "extraction_incomplete",
        "description": "추출된 사실이 없다.",
        "severity": "high",
        "affected_fact_ids": [],
        "source_ids": ["source:title"],
    }]
    context = _context(facts)
    authored = copy.deepcopy(_authored())
    authored["readiness"]["status"] = "ready"

    brief = build_audit_brief(
        facts,
        context,
        client=StubClient([authored]),
        model="stub",
        generated_at="2026-07-11T00:00:00Z",
    )

    assert brief["readiness"]["status"] == "not_ready"
    assert brief["workpaper"]["fact_ids"] == []
    assert brief["summary"]["statement_ids"] == ["statement:empty_workbook"]
    assert brief["statements"] == [{
        "id": "statement:empty_workbook",
        "section": "open_items",
        "type": "gap",
        "text": (
            "No workbook facts were extracted; substantive audit analysis is not "
            "ready from the extracted workbook content."
        ),
        "status": "unknown",
        "confidence": 1.0,
        "fact_ids": [],
        "standard_citation_ids": [],
    }]
    assert brief["limitations"][0]["audit_facts_limitation_ids"] == [
        "facts-limit:empty"
    ]


def test_no_results_and_unverified_effective_date_downgrade_ready() -> None:
    facts = _facts()
    facts["standard_queries"] = [{
        "id": "query:none",
        "query": "위험평가 관련 감사기준",
        "domain": "audit",
        "framework": "KSA",
        "effective_date": "2026-12-31",
        "fact_ids": ["fact:purpose"],
        "rationale": "관련 기준 확인",
    }]
    context = _context(facts)
    context["queries"] = [{
        "id": "query:none",
        "query": "위험평가 관련 감사기준",
        "domain": "audit",
        "framework": "KSA",
        "effective_date": "2026-12-31",
        "fact_ids": ["fact:purpose"],
        "status": "no_results",
        "error": None,
        "citation_ids": [],
        "matches": [],
    }]
    context["limitations"] = [{
        "id": "context-limit:date",
        "code": "effective_date_unverified",
        "description": "서버가 구조화 시행일을 제공하지 않는다.",
        "severity": "moderate",
        "query_ids": ["query:none"],
        "citation_ids": [],
    }]
    authored = _authored()
    authored["readiness"] = {
        "status": "ready",
        "reasons": ["모델 판단"],
        "open_item_fact_ids": [],
    }

    brief = build_audit_brief(
        facts,
        context,
        client=StubClient([authored]),
        model="stub",
        generated_at="2026-07-11T00:00:00Z",
    )

    assert brief["readiness"]["status"] == "partial"
    assert "One or more standards queries returned no results." in (
        brief["readiness"]["reasons"]
    )
    assert "Standards framework or effective date remains unverified." in (
        brief["readiness"]["reasons"]
    )


def test_model_source_free_gap_is_removed_when_workbook_facts_exist() -> None:
    facts = _facts()
    context = _context(facts)
    authored = _authored()
    authored["statements"].append({
        "id": "statement:unsupported-gap",
        "section": "open_items",
        "type": "gap",
        "text": "UNSUPPORTED GAP TEXT",
        "status": "unknown",
        "confidence": 0.5,
        "fact_ids": [],
        "standard_citation_ids": [],
    })
    authored["summary"] = {
        "text": "UNSUPPORTED GAP TEXT",
        "statement_ids": ["brief:purpose", "statement:unsupported-gap"],
    }

    brief = build_audit_brief(
        facts,
        context,
        client=StubClient([authored]),
        model="stub",
        generated_at="2026-07-11T00:00:00Z",
    )

    assert {item["id"] for item in brief["statements"]} == {"brief:purpose"}
    assert brief["summary"]["statement_ids"] == ["brief:purpose"]
    assert "UNSUPPORTED GAP TEXT" not in brief["summary"]["text"]
