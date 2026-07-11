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
