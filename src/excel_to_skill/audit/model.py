"""감사 의미 계층의 작은 공통 타입·검증 도구.

이 모듈은 파일이나 네트워크를 건드리지 않는다. 감사 사실 추출, 기준서 조회,
brief 합성이 같은 문자열 enum과 결정론 JSON 규칙을 공유하게 하는 것이 목적이다.
구체적인 산출물 계약은 각 JSON schema가 담당하고, 여기서는 Python 경계에서 즉시
잡아야 할 값 오류만 방어한다.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum


class AuditModelError(ValueError):
    """감사 의미 모델 값이 공통 계약을 만족하지 않을 때 발생한다."""


class StandardsDomain(StrEnum):
    """기준서 조회 영역. framework(KSA, K-IFRS 등)와는 별도 축이다."""

    AUDIT = "audit"
    ACCOUNTING = "accounting"


class SourceKind(StrEnum):
    """brief 주장을 뒷받침하는 출처 계층."""

    WORKBOOK = "workbook"
    AUDIT_STANDARD = "audit_standard"
    ACCOUNTING_STANDARD = "accounting_standard"


def require_non_empty(value: object, *, field: str) -> str:
    """값을 앞뒤 공백 없는 비어 있지 않은 문자열로 정규화한다."""
    if not isinstance(value, str) or not value.strip():
        raise AuditModelError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")
    return value.strip()


def validate_iso_date(value: str | None, *, field: str) -> str | None:
    """선택 ISO 날짜(`YYYY-MM-DD`)를 검증하고 원문을 반환한다."""
    if value is None:
        return None
    text = require_non_empty(value, field=field)
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise AuditModelError(f"{field}는 YYYY-MM-DD 형식이어야 합니다: {text!r}")
    try:
        date.fromisoformat(text)
    except ValueError as e:
        raise AuditModelError(f"{field}가 유효한 날짜가 아닙니다: {text!r}") from e
    return text


def validate_iso_datetime(value: str | None, *, field: str) -> str | None:
    """선택 ISO 8601 시각을 검증한다. 재현성을 위해 timezone을 필수로 한다."""
    if value is None:
        return None
    text = require_non_empty(value, field=field)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as e:
        raise AuditModelError(f"{field}가 유효한 ISO 8601 시각이 아닙니다: {text!r}") from e
    if "T" not in text or parsed.tzinfo is None:
        raise AuditModelError(f"{field}에는 시각과 timezone이 필요합니다: {text!r}")
    return text


def _validate_json_value(value: object, *, path: str = "$") -> None:
    """결정론 JSON helper가 받을 수 있는 값인지 재귀적으로 검사한다."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditModelError(f"JSON 값은 NaN/Infinity일 수 없습니다: {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditModelError(f"JSON 객체 키는 문자열이어야 합니다: {path}")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    raise AuditModelError(f"JSON으로 표현할 수 없는 값입니다: {path} ({type(value).__name__})")


def canonical_json(value: object) -> str:
    """JSON 값을 키 정렬·공백 제거·UTF-8 문자 보존 형태로 직렬화한다."""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_sha256(value: object) -> str:
    """`canonical_json` 바이트의 SHA-256 hex digest를 반환한다."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
