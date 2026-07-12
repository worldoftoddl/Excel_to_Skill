"""Small provider-neutral structured-output boundary for audit artifacts."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import jsonschema

from ..resources import PROMPT_DIR, SCHEMA_DIR

_SCHEMA_DIR = SCHEMA_DIR
_PROMPT_DIR = PROMPT_DIR


class AuditLLMError(RuntimeError):
    """A model call could not produce a valid audit artifact unit."""


def _validation_error_summary(error: Exception) -> str:
    """Return retry guidance without echoing the full schema or model-produced instance."""
    if isinstance(error, json.JSONDecodeError):
        return f"JSON parse error at line {error.lineno}, column {error.colno}"
    if isinstance(error, jsonschema.ValidationError):
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        location = path if path != "/" else "root"
        validator = error.validator
        if validator == "required":
            detail = error.message
        elif validator == "additionalProperties":
            detail = "object contains properties outside the schema"
        elif validator == "type":
            detail = f"invalid type; expected {error.validator_value!r}"
        elif validator == "enum":
            detail = "value is not in the allowed enum"
        elif validator == "pattern":
            detail = "string does not match the required pattern"
        elif validator in {"oneOf", "anyOf", "allOf"}:
            detail = f"value does not satisfy {validator}"
        else:
            detail = f"failed validator {validator!r}"
        return f"{location}: {detail}"
    return str(error)[:1000]


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
    validation_schema: dict | None = None,
    semantic_validator: Callable[[dict], None] | None = None,
    label: str,
    retries: int = 1,
    eprint=None,
) -> dict:
    """Call an injected structured-output client and enforce a JSON Schema.

    The client contract matches the existing annotator boundary:
    ``client(system=..., user=..., schema=...) -> dict | JSON string``.  Invalid JSON/schema
    output is retried with a compact correction request; provider/network errors are surfaced
    immediately and never converted into a partial audit bundle.  ``validation_schema`` lets a
    caller expose a provider-compatible projection while enforcing a stricter repository schema
    on the returned document.  ``semantic_validator`` runs inside the same retry boundary for
    contracts that depend on the supplied input records rather than JSON shape alone.
    """
    if retries < 0:
        raise ValueError("retries는 0 이상이어야 합니다.")
    eprint = eprint or (lambda *args: None)
    validator_schema = validation_schema or schema
    attempt_user = user
    last_error: str | None = None
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
            jsonschema.validate(doc, validator_schema)
            if semantic_validator is not None:
                semantic_validator(doc)
            return doc
        except (json.JSONDecodeError, jsonschema.ValidationError, AuditLLMError) as e:
            last_error = _validation_error_summary(e)
            if attempt < retries:
                eprint(
                    f"[audit prepare] {label} 응답 검증 실패 → 재시도: "
                    + last_error
                )
                attempt_user = (
                    user
                    + "\n\n[재시도] 직전 응답이 JSON Schema를 충족하지 못했습니다: "
                    + last_error
                    + "\n설명 없이 입력 스키마에 맞는 JSON 객체 하나만 출력하세요."
                )
    raise AuditLLMError(f"{label} 응답 검증 실패: {last_error}")
