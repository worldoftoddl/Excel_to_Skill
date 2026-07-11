from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from excel_to_skill.audit.extract import (
    AuditExtractionError,
    _materialize_region,
    extract_audit_facts,
)
from excel_to_skill.audit.regions import build_regions
from excel_to_skill.audit.sources import WorkbookSourceResolver


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    meta = {
        "source": {
            "filename": "audit.xlsx",
            "sha256": "a" * 64,
            "format": "xlsx",
        },
        "sheets": [{"name": "Main", "dimensions": "A1:B10"}],
    }
    (pkg / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    cells = [
        {
            "sheet": "Main",
            "cell": "A1",
            "row": 1,
            "col": 1,
            "value": "매출 기간귀속 위험",
            "formula": None,
            "bold": True,
        },
        {
            "sheet": "Main",
            "cell": "A10",
            "row": 10,
            "col": 1,
            "value": "기말 전후 매출 표본을 검사함",
            "formula": None,
            "bold": False,
        },
    ]
    (pkg / "data" / "cells.jsonl").write_text(
        "".join(json.dumps(cell, ensure_ascii=False) + "\n" for cell in cells),
        encoding="utf-8",
    )
    return pkg


def _fact(local_id: str, fact_type: str, description: str, status: str, ref: str) -> dict:
    return {
        "local_id": local_id,
        "type": fact_type,
        "description": description,
        "status": status,
        "normalized_code": None,
        "value": None,
        "unit": None,
        "severity": "high" if fact_type == "risk" else "none",
        "confidence": 0.9,
        "sources": [
            {
                "ref": ref,
                "role": "narrative" if fact_type == "risk" else "procedure",
            }
        ],
    }


class StubExtractionClient:
    def __init__(
        self,
        *,
        fenced_first_response: bool = False,
        outside_first_region: bool = False,
        cite_context_in_second_region: bool = False,
        cite_context_label_only: bool = False,
        cite_context_with_current_region: bool = False,
        bad_query_reference: bool = False,
        bad_relation_endpoint: bool = False,
    ) -> None:
        self.fenced_first_response = fenced_first_response
        self.outside_first_region = outside_first_region
        self.cite_context_in_second_region = cite_context_in_second_region
        self.cite_context_label_only = cite_context_label_only
        self.cite_context_with_current_region = cite_context_with_current_region
        self.bad_query_reference = bad_query_reference
        self.bad_relation_endpoint = bad_relation_endpoint
        self.calls: list[dict] = []

    def __call__(self, *, system: str, user: str, schema: dict) -> dict | str:
        payload = json.loads(user)
        self.calls.append({"system": system, "payload": payload, "schema": schema})
        if "region_id" in schema["properties"]:
            region_id = payload["region_id"]
            is_first = region_id == "region:00001"
            if is_first and self.outside_first_region:
                ref = "Main!A10"
            elif not is_first and (
                self.cite_context_in_second_region or self.cite_context_label_only
            ):
                ref = "Main!A1"
            else:
                ref = "Main!A1" if is_first else "Main!A10"
            result = {
                "region_id": region_id,
                "facts": [
                    _fact(
                        "risk" if is_first else "procedure",
                        "risk" if is_first else "procedure",
                        "매출 기간귀속 위험" if is_first else "기말 전후 매출 표본 검사",
                        "identified" if is_first else "performed",
                        ref,
                    )
                ],
                "limitations": [],
            }
            if not is_first and self.cite_context_with_current_region:
                result["facts"][0]["sources"].append({
                    "ref": "Main!A1",
                    "role": "label",
                })
            if not is_first and self.cite_context_label_only:
                result["facts"][0]["sources"][0]["role"] = "label"
            if is_first and self.fenced_first_response:
                return "```json\n" + json.dumps(result, ensure_ascii=False) + "\n```"
            return result

        facts = payload["facts"]
        sources = payload["sources"]
        risk_id, procedure_id = facts[0]["id"], facts[1]["id"]
        query_fact_id = "fact:missing" if self.bad_query_reference else risk_id
        return {
            "workpaper": {
                "kind": "risk_assessment",
                "title": "매출 위험 평가",
                "entity": None,
                "period_start": None,
                "period_end": None,
                "audit_phase": "risk_assessment",
                "document_state": "partially_completed",
                "purpose": "매출 기간귀속 위험과 대응 절차 문서화",
                "source_ids": [sources[0]["id"]],
            },
            "relations": [
                {
                    "id": "relation:1",
                    "type": "tests" if self.bad_relation_endpoint else "addresses",
                    "from_fact_id": procedure_id,
                    "to_fact_id": risk_id,
                    "status": "documented",
                    "confidence": 0.9,
                    "source_ids": [source["id"] for source in sources],
                }
            ],
            "standard_queries": [
                {
                    "id": "query:1",
                    "query": "매출 기간귀속 위험에 대응하는 감사절차 요구사항",
                    "domain": "audit",
                    "framework": "KSA",
                    "effective_date": None,
                    "fact_ids": [query_fact_id],
                    "rationale": "조서에 식별된 위험과 수행 절차의 기준상 맥락 확인",
                }
            ],
        }


def test_extract_calls_every_region_then_consolidates_and_binds_sources(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(fenced_first_response=True)

    document = extract_audit_facts(
        pkg,
        client=client,
        model="stub-model",
        generated_at="2026-07-11T00:00:00Z",
    )

    assert len(client.calls) == 3
    assert [call["payload"].get("region_id") for call in client.calls[:2]] == [
        "region:00001",
        "region:00002",
    ]
    assert "facts" in client.calls[2]["payload"]
    assert len(document["facts"]) == 2
    assert len(document["relations"]) == 1
    assert document["standard_queries"][0]["domain"] == "audit"
    assert document["generator"] == {
        "name": "excel_to_skill.audit.extract",
        "version": "0.2.0",
        "kind": "hybrid",
        "model": "stub-model",
        "prompt_sha256": document["generator"]["prompt_sha256"],
        "generated_at": "2026-07-11T00:00:00Z",
    }
    assert len(document["generator"]["prompt_sha256"]) == 64

    resolver = WorkbookSourceResolver(pkg)
    source_by_range = {source["range"]: source for source in document["sources"]}
    assert source_by_range["A1"]["content_sha256"] == resolver.resolve(
        "Main!A1"
    ).content_sha256
    assert source_by_range["A10"]["content_sha256"] == resolver.resolve(
        "Main!A10"
    ).content_sha256
    source_ids = {source["id"] for source in document["sources"]}
    assert all(set(fact["source_ids"]) <= source_ids for fact in document["facts"])

    written = json.loads(
        (pkg / "data" / "audit_facts.json").read_text(encoding="utf-8")
    )
    assert written == document
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "audit_facts.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(document, schema)


def test_extract_rejects_real_ledger_address_outside_observed_region(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(outside_first_region=True)

    with pytest.raises(AuditExtractionError, match="관찰 범위 밖"):
        extract_audit_facts(pkg, client=client)
    assert len(client.calls) == 1
    assert not (pkg / "data" / "audit_facts.json").exists()


def test_extract_rejects_context_label_as_the_only_fact_source(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(cite_context_label_only=True)

    with pytest.raises(AuditExtractionError, match="현재 region의 1차 근거"):
        extract_audit_facts(
            pkg,
            client=client,
            max_cells=1,
            row_gap=20,
        )

    assert not (pkg / "data" / "audit_facts.json").exists()


def test_extract_exposes_header_context_but_rejects_it_as_a_fact_source(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(cite_context_in_second_region=True)

    with pytest.raises(AuditExtractionError, match="role='label'"):
        extract_audit_facts(
            pkg,
            client=client,
            max_cells=1,
            row_gap=20,
        )

    assert len(client.calls) == 2
    context = client.calls[1]["payload"]["read_only_context"]
    assert context["source_eligible"] is False
    assert [cell["cell"] for cell in context["cells"]] == ["A1"]
    assert not (pkg / "data" / "audit_facts.json").exists()


def test_extract_preserves_used_header_context_as_label_provenance(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(cite_context_with_current_region=True)

    document = extract_audit_facts(
        pkg,
        client=client,
        max_cells=1,
        row_gap=20,
    )

    procedure = next(fact for fact in document["facts"] if fact["type"] == "procedure")
    sources = {
        source["id"]: source for source in document["sources"]
    }
    cited = [sources[source_id] for source_id in procedure["source_ids"]]
    assert {(source["range"], source["role"]) for source in cited} == {
        ("A10", "procedure"),
        ("A1", "label"),
    }


def test_extract_rejects_invalid_relation_endpoint_before_writing(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(bad_relation_endpoint=True)

    with pytest.raises(AuditExtractionError, match="requires fact type 'assertion'"):
        extract_audit_facts(pkg, client=client)

    assert not (pkg / "data" / "audit_facts.json").exists()


def test_extract_rejects_consolidation_cross_reference_to_unknown_fact(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    client = StubExtractionClient(bad_query_reference=True)

    with pytest.raises(AuditExtractionError, match="없는 ID"):
        extract_audit_facts(pkg, client=client)
    assert len(client.calls) == 3
    assert not (pkg / "data" / "audit_facts.json").exists()


def test_extract_has_no_implicit_network_client(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    with pytest.raises(AuditExtractionError, match="client를 주입"):
        extract_audit_facts(pkg, client=None)


def test_empty_region_response_becomes_explicit_coverage_limitation(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    region = build_regions(pkg)[0]
    sources: dict[str, dict] = {}
    facts, limitations, summary = _materialize_region(
        region,
        {"region_id": region.region_id, "facts": [], "limitations": []},
        WorkbookSourceResolver(pkg),
        sources,
    )
    assert facts == []
    assert limitations[0]["code"] == "extraction_incomplete"
    assert limitations[0]["source_ids"]
    assert summary["limitation_ids"] == [limitations[0]["id"]]
