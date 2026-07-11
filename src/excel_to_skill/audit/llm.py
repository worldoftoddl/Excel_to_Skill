"""Small provider-neutral structured-output boundary for audit artifacts."""
from __future__ import annotations

import hashlib
import json

import jsonschema

from ..resources import PROMPT_DIR, SCHEMA_DIR

_SCHEMA_DIR = SCHEMA_DIR
_PROMPT_DIR = PROMPT_DIR


class AuditLLMError(RuntimeError):
    """A model call could not produce a valid audit artifact unit."""


def load_schema(name: str) -> dict:
    path = _SCHEMA_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AuditLLMError(f"schema 없음: {name}") from e
    except json.JSONDecodeError as e:
        raise AuditLLMError(f"schema JSON 파싱 실패({name}): {e}") from e


def load_prompt(name: str) -> tuple[str, str]:
    path = _PROMPT_DIR / name
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise AuditLLMError(f"prompt 없음: {name}") from e
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _extract_json(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        first_newline = value.find("\n")
        if first_newline >= 0:
            value = value[first_newline + 1 :]
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    doc = json.loads(value.strip())
    if not isinstance(doc, dict):
        raise AuditLLMError("모델 응답은 JSON 객체여야 합니다.")
    return doc


def call_json(
    client,
    *,
    system: str,
    user: str,
    schema: dict,
    label: str,
    retries: int = 1,
    eprint=None,
) -> dict:
    """Call an injected structured-output client and enforce a JSON Schema.

    The client contract matches the existing annotator boundary:
    ``client(system=..., user=..., schema=...) -> dict | JSON string``.  Invalid JSON/schema
    output is retried with a compact correction request; provider/network errors are surfaced
    immediately and never converted into a partial audit bundle.
    """
    if retries < 0:
        raise ValueError("retries는 0 이상이어야 합니다.")
    eprint = eprint or (lambda *args: None)
    attempt_user = user
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = client(system=system, user=attempt_user, schema=schema)
        except Exception as e:  # noqa: BLE001 - provider boundary; retain causal exception
            raise AuditLLMError(f"{label} 모델 호출 실패: {e}") from e
        try:
            if isinstance(raw, dict):
                doc = raw
            elif isinstance(raw, str):
                doc = _extract_json(raw)
            else:
                raise AuditLLMError(
                    f"{label} 모델 응답 형식이 dict/string이 아닙니다: {type(raw).__name__}"
                )
            jsonschema.validate(doc, schema)
            return doc
        except (json.JSONDecodeError, jsonschema.ValidationError, AuditLLMError) as e:
            last_error = e
            if attempt < retries:
                eprint(f"[audit prepare] {label} 응답 검증 실패 → 재시도: {e}")
                attempt_user = (
                    user
                    + "\n\n[재시도] 직전 응답이 JSON Schema를 충족하지 못했습니다: "
                    + str(e)
                    + "\n설명 없이 입력 스키마에 맞는 JSON 객체 하나만 출력하세요."
                )
    raise AuditLLMError(f"{label} 응답 검증 실패: {last_error}")
