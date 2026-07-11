"""감사 공통 모델과 기준서 조회 Protocol — 네트워크 없는 단위 계약."""
from __future__ import annotations

import math

import pytest

from excel_to_skill.audit import (
    AuditModelError,
    SourceKind,
    StandardHit,
    StandardsDomain,
    StandardsRetriever,
    canonical_json,
    json_sha256,
)
from excel_to_skill.audit.model import validate_iso_date, validate_iso_datetime


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
        "query_id": "standard-query:1",
        "corpus_id": "kr-standards",
        "corpus_version": "ksa-2026.1",
        "retriever_version": "stub-1",
        "retrieved_at": "2026-07-11T03:00:00+09:00",
        "metadata": {"rank": 1, "tags": ["risk", "assertion"]},
    }
    values.update(changes)
    return StandardHit(**values)


def test_common_enums_are_stable_strings() -> None:
    assert StandardsDomain.AUDIT == "audit"
    assert StandardsDomain.ACCOUNTING == "accounting"
    assert SourceKind.WORKBOOK == "workbook"
    assert SourceKind.AUDIT_STANDARD == "audit_standard"


def test_canonical_json_and_digest_are_order_independent() -> None:
    left = {"한글": [1, True], "a": {"y": 2, "x": None}}
    right = {"a": {"x": None, "y": 2}, "한글": [1, True]}
    assert canonical_json(left) == canonical_json(right)
    assert "한글" in canonical_json(left)
    assert json_sha256(left) == json_sha256(right)
    assert len(json_sha256(left)) == 64


@pytest.mark.parametrize("value", [math.nan, math.inf, {1: "not-a-string-key"}, {"x": object()}])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(AuditModelError):
        canonical_json(value)


def test_date_and_datetime_validation() -> None:
    assert validate_iso_date("2026-07-11", field="date") == "2026-07-11"
    assert validate_iso_datetime("2026-07-11T00:00:00Z", field="time").endswith("Z")
    with pytest.raises(AuditModelError):
        validate_iso_date("2026-02-30", field="date")
    with pytest.raises(AuditModelError):
        validate_iso_datetime("2026-07-11T00:00:00", field="time")


def test_standard_hit_normalizes_provenance_and_round_trips() -> None:
    metadata = {"rank": 1, "nested": {"seen": True}}
    hit = _hit(domain=StandardsDomain.AUDIT, score=1, metadata=metadata)
    metadata["rank"] = 99
    metadata["nested"]["seen"] = False

    assert hit.domain is StandardsDomain.AUDIT
    assert hit.score == 1.0
    assert hit.metadata == {"nested": {"seen": True}, "rank": 1}
    assert hit.citation_id.startswith("standard:")
    assert len(hit.snippet_sha256) == 64

    doc = hit.to_dict()
    assert doc["domain"] == "audit"
    assert StandardHit.from_dict(doc) == hit


def test_standard_hit_default_ids_are_deterministic_and_content_bound() -> None:
    first = _hit()
    again = _hit()
    changed = _hit(snippet="다른 기준서 발췌")
    assert first.citation_id == again.citation_id
    assert first.snippet_sha256 == again.snippet_sha256
    assert changed.citation_id != first.citation_id
    assert changed.snippet_sha256 != first.snippet_sha256


def test_standard_hit_accepts_provider_citation_id_and_matching_digest() -> None:
    original = _hit(citation_id="provider:KSA-315:26")
    restored = _hit(
        citation_id="provider:KSA-315:26",
        snippet_sha256=original.snippet_sha256.upper(),
    )
    assert restored.citation_id == "provider:KSA-315:26"
    assert restored.snippet_sha256 == original.snippet_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"domain": "tax"},
        {"framework": "  "},
        {"snippet": ""},
        {"score": True},
        {"score": math.nan},
        {"score": 1.01},
        {"effective_date": "2026/01/01"},
        {"retrieved_at": "2026-07-11T03:00:00"},
        {"metadata": []},
    ],
)
def test_standard_hit_rejects_invalid_values(changes: dict) -> None:
    with pytest.raises(AuditModelError):
        _hit(**changes)


def test_standard_hit_rejects_tampered_digest_and_unknown_fields() -> None:
    with pytest.raises(AuditModelError, match="snippet"):
        _hit(snippet_sha256="0" * 64)
    doc = _hit().to_dict()
    doc["unexpected"] = "x"
    with pytest.raises(AuditModelError, match="계약 밖"):
        StandardHit.from_dict(doc)


class StubStandardsRetriever:
    def __init__(self, hits: list[StandardHit]) -> None:
        self.hits = hits
        self.calls: list[dict] = []

    def search(
        self,
        query: str,
        *,
        domain: StandardsDomain | str,
        framework: str,
        effective_date: str | None = None,
        standard_nos: list[str] | None = None,
    ) -> list[StandardHit]:
        self.calls.append({
            "query": query,
            "domain": StandardsDomain(domain),
            "framework": framework,
            "effective_date": effective_date,
            "standard_nos": standard_nos,
        })
        return list(self.hits)


def test_retriever_protocol_is_stub_friendly() -> None:
    hit = _hit()
    retriever = StubStandardsRetriever([hit])
    assert isinstance(retriever, StandardsRetriever)
    assert retriever.search(
        "매출 위험의 식별",
        domain="audit",
        framework="KSA",
        effective_date="2026-01-01",
    ) == [hit]
    assert retriever.calls == [{
        "query": "매출 위험의 식별",
        "domain": StandardsDomain.AUDIT,
        "framework": "KSA",
        "effective_date": "2026-01-01",
        "standard_nos": None,
    }]
