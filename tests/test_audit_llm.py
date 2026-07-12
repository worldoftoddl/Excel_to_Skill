from __future__ import annotations

from collections import deque

import pytest

from excel_to_skill.audit.llm import AuditLLMError, call_json, load_schema


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "string"}},
}


class StubClient:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def test_call_json_accepts_dict_and_fenced_json() -> None:
    first = StubClient([{"value": "dict"}])
    assert call_json(
        first, system="s", user="u", schema=_SCHEMA, label="unit"
    ) == {"value": "dict"}

    second = StubClient(['```json\n{"value":"text"}\n```'])
    assert call_json(
        second, system="s", user="u", schema=_SCHEMA, label="unit"
    ) == {"value": "text"}


def test_call_json_retries_schema_failure_with_error_context() -> None:
    client = StubClient([{"wrong": 1}, {"value": "fixed"}])
    messages: list[str] = []
    result = call_json(
        client,
        system="system",
        user="original",
        schema=_SCHEMA,
        label="facts",
        retries=1,
        eprint=messages.append,
    )
    assert result == {"value": "fixed"}
    assert len(client.calls) == 2
    assert client.calls[0]["user"] == "original"
    assert "재시도" in client.calls[1]["user"]
    assert messages and "검증 실패" in messages[0]


def test_call_json_retries_input_dependent_semantic_failure() -> None:
    client = StubClient([{"value": "unsupported"}, {"value": "fixed"}])

    def validate(document: dict) -> None:
        if document["value"] == "unsupported":
            raise AuditLLMError("input record does not support this value")

    result = call_json(
        client,
        system="system",
        user="original",
        schema=_SCHEMA,
        semantic_validator=validate,
        label="facts",
        retries=1,
    )

    assert result == {"value": "fixed"}
    assert len(client.calls) == 2
    assert "input record does not support" in client.calls[1]["user"]


def test_call_json_does_not_echo_invalid_instance_in_retry_diagnostics() -> None:
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"enum": ["allowed"]}},
    }
    secret = "SENSITIVE-WORKBOOK-VALUE"
    client = StubClient([{"value": secret}, {"value": "allowed"}])
    messages: list[str] = []

    assert call_json(
        client,
        system="system",
        user="original",
        schema=schema,
        label="facts",
        retries=1,
        eprint=messages.append,
    ) == {"value": "allowed"}

    assert secret not in messages[0]
    assert secret not in client.calls[1]["user"]
    assert "/value: value is not in the allowed enum" in messages[0]


def test_call_json_does_not_echo_sensitive_additional_property_name() -> None:
    secret = "SENSITIVE-WORKBOOK-PROPERTY"
    client = StubClient([{secret: "x"}, {"value": "allowed"}])
    messages: list[str] = []

    call_json(
        client,
        system="system",
        user="original",
        schema=_SCHEMA,
        label="facts",
        retries=1,
        eprint=messages.append,
    )

    assert secret not in messages[0]
    assert secret not in client.calls[1]["user"]


def test_call_json_does_not_hide_provider_failure() -> None:
    client = StubClient([RuntimeError("offline")])
    with pytest.raises(AuditLLMError, match="offline"):
        call_json(client, system="s", user="u", schema=_SCHEMA, label="brief")
    assert len(client.calls) == 1


def test_call_json_raises_after_retry_exhaustion() -> None:
    client = StubClient(["not-json", []])
    with pytest.raises(AuditLLMError, match="응답 검증 실패"):
        call_json(
            client, system="s", user="u", schema=_SCHEMA, label="facts", retries=1
        )


def test_load_schema_uses_repository_schema_directory() -> None:
    schema = load_schema("audit_facts.schema.json")
    assert schema["title"] == "data/audit_facts.json"
