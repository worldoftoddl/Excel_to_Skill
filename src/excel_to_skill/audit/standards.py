"""감사·회계기준 RAG 조회의 공급자 독립 계약.

여기에는 MCP나 네트워크 클라이언트가 없다. 실제 연결부는 후속 adapter가
`StandardsRetriever`를 구현하고, 단위 테스트·오프라인 실행은 같은 Protocol을
만족하는 작은 stub을 주입한다. 조회 결과는 `StandardHit`에서 즉시 정규화되어
기준서 근거와 workbook 근거가 섞이지 않게 한다.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .model import (
    AuditModelError,
    StandardsDomain,
    canonical_json,
    json_sha256,
    require_non_empty,
    validate_iso_date,
    validate_iso_datetime,
)


class StandardsQueryError(RuntimeError):
    """한 기준서 query에 국한되어 partial context로 격리할 수 있는 조회 오류."""

    def __init__(self, message: str, *, limitation_code: str | None = None) -> None:
        self.limitation_code = limitation_code
        super().__init__(message)


class StandardsRetrievalFatalError(RuntimeError):
    """출처 계약·인증·collection 일관성이 깨져 prepare 전체를 중단해야 하는 오류."""


@dataclass(frozen=True, slots=True)
class StandardHit:
    """RAG가 반환한 기준서 문단/발췌 1건과 그 provenance.

    `citation_id`와 `snippet_sha256`을 공급자가 주지 않으면 결정론적으로 만든다.
    공급자가 준 snippet digest는 실제 snippet과 반드시 일치해야 한다. `metadata`는
    JSON 값만 허용하고 깊은 사본으로 보관하므로 stub 입력을 나중에 바꿔도 hit가
    달라지지 않는다.
    """

    domain: StandardsDomain | str
    framework: str
    document_id: str
    snippet: str
    paragraph: str | None = None
    title: str | None = None
    score: float | None = None
    edition: str | None = None
    effective_date: str | None = None
    source_uri: str | None = None
    query_id: str | None = None
    corpus_id: str | None = None
    corpus_version: str | None = None
    retriever_version: str | None = None
    retrieved_at: str | None = None
    citation_id: str | None = None
    snippet_sha256: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            domain = StandardsDomain(self.domain)
        except (TypeError, ValueError) as e:
            values = ", ".join(d.value for d in StandardsDomain)
            raise AuditModelError(f"domain은 다음 중 하나여야 합니다: {values}") from e
        object.__setattr__(self, "domain", domain)

        for name in ("framework", "document_id", "snippet"):
            object.__setattr__(self, name, require_non_empty(getattr(self, name), field=name))
        for name in (
            "paragraph",
            "title",
            "edition",
            "source_uri",
            "query_id",
            "corpus_id",
            "corpus_version",
            "retriever_version",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_non_empty(value, field=name))

        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise AuditModelError("score는 유한한 숫자이거나 null이어야 합니다.")
            normalized_score = float(self.score)
            if not math.isfinite(normalized_score):
                raise AuditModelError("score는 유한한 숫자이거나 null이어야 합니다.")
            if not 0.0 <= normalized_score <= 1.0:
                raise AuditModelError("score는 0 이상 1 이하이거나 null이어야 합니다.")
            object.__setattr__(self, "score", normalized_score)

        object.__setattr__(
            self,
            "effective_date",
            validate_iso_date(self.effective_date, field="effective_date"),
        )
        object.__setattr__(
            self,
            "retrieved_at",
            validate_iso_datetime(self.retrieved_at, field="retrieved_at"),
        )

        if not isinstance(self.metadata, Mapping):
            raise AuditModelError("metadata는 JSON 객체여야 합니다.")
        # canonical round-trip은 JSON 가능성 검사와 외부 입력으로부터의 깊은 복사를 겸한다.
        metadata = json.loads(canonical_json(dict(self.metadata)))
        object.__setattr__(self, "metadata", metadata)

        content_sha = hashlib.sha256(self.snippet.encode("utf-8")).hexdigest()
        if self.snippet_sha256 is not None:
            supplied = require_non_empty(self.snippet_sha256, field="snippet_sha256").lower()
            if supplied != content_sha:
                raise AuditModelError("snippet_sha256이 snippet 내용과 일치하지 않습니다.")
        object.__setattr__(self, "snippet_sha256", content_sha)

        citation_id = self.citation_id
        if citation_id is None:
            identity = {
                "domain": domain.value,
                "framework": self.framework,
                "document_id": self.document_id,
                "paragraph": self.paragraph,
                "edition": self.edition,
                "effective_date": self.effective_date,
                "snippet_sha256": content_sha,
            }
            citation_id = f"standard:{json_sha256(identity)[:20]}"
        else:
            citation_id = require_non_empty(citation_id, field="citation_id")
        object.__setattr__(self, "citation_id", citation_id)

    def to_dict(self) -> dict:
        """고정 필드 순서의 JSON 객체로 변환한다."""
        return {
            "citation_id": self.citation_id,
            "domain": self.domain.value,
            "framework": self.framework,
            "document_id": self.document_id,
            "paragraph": self.paragraph,
            "title": self.title,
            "snippet": self.snippet,
            "snippet_sha256": self.snippet_sha256,
            "score": self.score,
            "edition": self.edition,
            "effective_date": self.effective_date,
            "source_uri": self.source_uri,
            "query_id": self.query_id,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "retriever_version": self.retriever_version,
            "retrieved_at": self.retrieved_at,
            "metadata": json.loads(canonical_json(dict(self.metadata))),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StandardHit:
        """`to_dict` 형태를 엄격히 읽어 `StandardHit`을 만든다."""
        if not isinstance(value, Mapping):
            raise AuditModelError("StandardHit 입력은 JSON 객체여야 합니다.")
        allowed = {
            "citation_id",
            "domain",
            "framework",
            "document_id",
            "paragraph",
            "title",
            "snippet",
            "snippet_sha256",
            "score",
            "edition",
            "effective_date",
            "source_uri",
            "query_id",
            "corpus_id",
            "corpus_version",
            "retriever_version",
            "retrieved_at",
            "metadata",
        }
        extra = sorted(set(value) - allowed)
        if extra:
            raise AuditModelError(f"StandardHit 계약 밖 필드: {extra}")
        required = ("domain", "framework", "document_id", "snippet")
        missing = [name for name in required if name not in value]
        if missing:
            raise AuditModelError(f"StandardHit 필수 필드 누락: {missing}")
        return cls(
            domain=value["domain"],
            framework=value["framework"],
            document_id=value["document_id"],
            snippet=value["snippet"],
            paragraph=value.get("paragraph"),
            title=value.get("title"),
            score=value.get("score"),
            edition=value.get("edition"),
            effective_date=value.get("effective_date"),
            source_uri=value.get("source_uri"),
            query_id=value.get("query_id"),
            corpus_id=value.get("corpus_id"),
            corpus_version=value.get("corpus_version"),
            retriever_version=value.get("retriever_version"),
            retrieved_at=value.get("retrieved_at"),
            citation_id=value.get("citation_id"),
            snippet_sha256=value.get("snippet_sha256"),
            metadata=value.get("metadata", {}),
        )


@runtime_checkable
class StandardsRetriever(Protocol):
    """기준서 RAG 어댑터의 최소 동기 인터페이스.

    실제 MCP 구현은 이 Protocol 밖의 별도 모듈에서 제공한다. 호출자는 framework와
    기준 적용일을 명시할 수 있고, 구현자는 관련도 순 `StandardHit` 목록을 반환한다.
    """

    def search(
        self,
        query: str,
        *,
        domain: StandardsDomain | str,
        framework: str | None,
        effective_date: str | None = None,
        standard_nos: list[str] | None = None,
    ) -> list[StandardHit]:
        """기준서 corpus를 검색한다."""
        ...
