from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill.audit.consume import AuditConsumeError, assertion_procedures
from excel_to_skill.cli import main

from test_audit_consume_gate import _write_committed_bundle
from test_audit_validate import _bundle as _validation_bundle


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
    *,
    status: str = "documented",
) -> dict:
    return {
        "id": relation_id,
        "type": relation_type,
        "from_fact_id": from_fact_id,
        "to_fact_id": to_fact_id,
        "status": status,
        "confidence": 0.9,
        "source_ids": ["source:1"],
    }


def _add_assertion_matrix(_pkg, facts, _context, _brief) -> None:
    facts["facts"].extend([
        _fact(
            "fact:assertion:existence",
            "assertion",
            "기말 매출채권이 실제로 존재한다.",
            status="documented",
            normalized_code="existence",
        ),
        _fact(
            "fact:assertion:completeness",
            "assertion",
            "기록되어야 할 매출채권이 누락 없이 기록되었다.",
            status="documented",
            normalized_code="completeness",
        ),
        _fact(
            "fact:assertion:cutoff",
            "assertion",
            "매출이 적절한 기간에 기록되었다.",
            status="documented",
            normalized_code="cutoff",
        ),
        _fact(
            "fact:assertion:presentation",
            "assertion",
            "매출채권이 적절하게 표시되었다.",
            status="documented",
            normalized_code="presentation",
        ),
        _fact(
            "fact:procedure:confirm",
            "procedure",
            "거래처에 채권 잔액을 조회하고 회신을 대조하였다.",
            status="performed",
        ),
        _fact(
            "fact:procedure:subsequent-cash",
            "procedure",
            "기말 후 입금내역에서 매출채권 원장으로 역추적하였다.",
            status="performed",
        ),
        _fact(
            "fact:procedure:cutoff",
            "procedure",
            "기말 전후 출하자료를 검사할 계획이다.",
            status="planned",
        ),
        _fact(
            "fact:procedure:presentation",
            "procedure",
            "공시 표시를 검사할 계획이다.",
            status="planned",
        ),
        _fact(
            "fact:result:confirm",
            "result",
            "회신 대조 결과 예외가 없었다.",
            status="passed",
        ),
        _fact(
            "fact:finding:missing",
            "finding",
            "누락 가능성이 있는 입금 1건을 발견하였다.",
            status="exception",
        ),
    ])
    facts["relations"].extend([
        _relation(
            "relation:tests:confirm:1",
            "tests",
            "fact:procedure:confirm",
            "fact:assertion:existence",
        ),
        _relation(
            "relation:tests:confirm:2",
            "tests",
            "fact:procedure:confirm",
            "fact:assertion:existence",
            status="inferred",
        ),
        _relation(
            "relation:tests:subsequent-cash",
            "tests",
            "fact:procedure:subsequent-cash",
            "fact:assertion:completeness",
        ),
        _relation(
            "relation:produces:confirm",
            "produces",
            "fact:procedure:confirm",
            "fact:result:confirm",
        ),
        _relation(
            "relation:produces:missing",
            "produces",
            "fact:procedure:subsequent-cash",
            "fact:finding:missing",
        ),
        _relation(
            "relation:tests:cutoff-inferred",
            "tests",
            "fact:procedure:cutoff",
            "fact:assertion:cutoff",
            status="inferred",
        ),
    ])
    # Prove the consumer's ordering does not depend on model-emitted array order.
    facts["facts"].reverse()
    facts["relations"].reverse()


def _add_nested_fanout(pkg, facts, context, brief) -> None:
    _add_assertion_matrix(pkg, facts, context, brief)
    facts["facts"].append(_fact(
        "fact:result:subsequent-review",
        "result",
        "추가 결과 문서에서 후속 입금 검사가 완료되었음을 확인하였다.",
        status="passed",
    ))
    facts["relations"].extend([
        _relation(
            "relation:tests:subsequent-cash:second",
            "tests",
            "fact:procedure:subsequent-cash",
            "fact:assertion:completeness",
            status="inferred",
        ),
        _relation(
            "relation:produces:subsequent-review",
            "produces",
            "fact:procedure:subsequent-cash",
            "fact:result:subsequent-review",
        ),
    ])


def test_explicit_assertion_procedure_pairs_include_only_linked_outcomes(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_add_assertion_matrix
    )

    result = assertion_procedures(pkg)

    assert result["review_status"] == "draft" and result["unreviewed"] is True
    assert result["total_pairs"] == 3
    assert result["documented_pairs"] == 2
    assert result["inferred_pairs"] == 1
    assert result["unknown_pairs"] == 0
    assert [pair["assertion"]["id"] for pair in result["pairs"]] == [
        "fact:assertion:completeness",
        "fact:assertion:cutoff",
        "fact:assertion:existence",
    ]
    assert result["pairs"][0]["mapping_status"] == "documented"
    assert result["pairs"][1]["mapping_status"] == "inferred"
    existence = result["pairs"][2]
    assert existence["procedure"]["id"] == "fact:procedure:confirm"
    assert [relation["id"] for relation in existence["test_relations"]] == [
        "relation:tests:confirm:1",
        "relation:tests:confirm:2",
    ]
    assert [fact["id"] for fact in existence["produced_facts"]] == [
        "fact:result:confirm"
    ]
    assert [relation["id"] for relation in existence["produces_relations"]] == [
        "relation:produces:confirm"
    ]
    assert existence["returned_test_relations"] == 2
    assert existence["total_test_relations"] == 2
    assert existence["test_relations_truncated"] is False
    assert existence["returned_produced_facts"] == 1
    assert existence["total_produced_facts"] == 1
    assert existence["produced_facts_truncated"] is False
    assert existence["returned_produces_relations"] == 1
    assert existence["total_produces_relations"] == 1
    assert existence["produces_relations_truncated"] is False
    assert existence["returned_trace_ids"] == 6
    assert existence["total_trace_ids"] == 6
    assert existence["trace_ids_truncated"] is False
    assert existence["truncated"] is False
    assert existence["trace_ids"] == [
        "fact:assertion:existence",
        "fact:procedure:confirm",
        "relation:tests:confirm:1",
        "relation:tests:confirm:2",
        "fact:result:confirm",
        "relation:produces:confirm",
    ]
    assert [fact["id"] for fact in result["unpaired_assertions"]] == [
        "fact:assertion:presentation"
    ]
    assert [fact["id"] for fact in result["unpaired_procedures"]] == [
        "fact:procedure:presentation"
    ]


def test_query_limit_and_cli_preserve_explicit_mapping_contract(
    tmp_path: Path,
    capsys,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_add_assertion_matrix
    )

    filtered = assertion_procedures(pkg, query="누락")
    assert filtered["query"] == "누락"
    assert filtered["total_pairs"] == 1
    assert filtered["pairs"][0]["assertion"]["normalized_code"] == "completeness"
    assert filtered["pairs"][0]["produced_facts"][0]["type"] == "finding"
    assert filtered["unpaired_assertions"] == []
    assert filtered["unpaired_procedures"] == []

    limited = assertion_procedures(pkg, limit=1)
    assert limited["returned_pairs"] == 1 and limited["total_pairs"] == 3
    assert limited["truncated"] is True

    assert main([
        "assertion-procedures", str(pkg), "--query", "누락", "--limit", "1"
    ]) == 0
    cli_doc = json.loads(capsys.readouterr().out)
    assert cli_doc == assertion_procedures(pkg, query="누락", limit=1)


def test_limit_bounds_nested_pair_and_trace_lists_without_changing_semantics(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(
        tmp_path, configure=_add_nested_fanout
    )

    result = assertion_procedures(pkg, query="누락", limit=1)

    assert result["returned_pairs"] == 1
    assert result["total_pairs"] == 1
    assert result["documented_pairs"] == 1
    assert result["inferred_pairs"] == 0
    assert result["unknown_pairs"] == 0
    assert result["returned_unpaired_assertions"] == 0
    assert result["total_unpaired_assertions"] == 0
    assert result["returned_unpaired_procedures"] == 0
    assert result["total_unpaired_procedures"] == 0
    pair = result["pairs"][0]
    assert pair["mapping_status"] == "documented"
    assert len(pair["test_relations"]) == 1
    assert pair["returned_test_relations"] == 1
    assert pair["total_test_relations"] == 2
    assert pair["test_relations_truncated"] is True
    assert len(pair["produced_facts"]) == 1
    assert pair["returned_produced_facts"] == 1
    assert pair["total_produced_facts"] == 2
    assert pair["produced_facts_truncated"] is True
    assert len(pair["produces_relations"]) == 1
    assert pair["returned_produces_relations"] == 1
    assert pair["total_produces_relations"] == 2
    assert pair["produces_relations_truncated"] is True
    assert len(pair["trace_ids"]) == 1
    assert pair["returned_trace_ids"] == 1
    assert pair["total_trace_ids"] == 8
    assert pair["trace_ids_truncated"] is True
    assert pair["truncated"] is True
    assert len(result["trace_ids"]) == 1
    assert result["returned_trace_ids"] == 1
    assert result["total_trace_ids"] == 8
    assert result["trace_ids_truncated"] is True
    # No top-level collection was cut; the aggregate flag reflects nested truncation.
    assert result["truncated"] is True

    # Query matching happens against the complete pair before its visible lists are capped.
    hidden_match = assertion_procedures(pkg, query="추가 결과", limit=1)
    assert hidden_match["total_pairs"] == 1
    assert hidden_match["pairs"][0]["assertion"]["normalized_code"] == "completeness"


def test_assertion_procedures_is_commit_gated_and_rejects_blank_query(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief_doc = _validation_bundle(tmp_path)
    for name, doc in (
        ("audit_facts.json", facts),
        ("standards_context.json", context),
        ("audit_brief.json", brief_doc),
    ):
        (pkg / "data" / name).write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(AuditConsumeError, match="완료 표식"):
        assertion_procedures(pkg)

    committed, _, _, _ = _write_committed_bundle(tmp_path / "committed")
    with pytest.raises(AuditConsumeError, match="query가 비어"):
        assertion_procedures(committed, query="  ")
