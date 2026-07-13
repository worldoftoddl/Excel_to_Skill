from __future__ import annotations

from types import SimpleNamespace

import pytest

from excel_to_skill.audit.langchain_client import (
    LangChainAnthropicClient,
    LangChainClientError,
    _usage_event,
)


def test_usage_event_keeps_only_nonnegative_integer_counts() -> None:
    event = _usage_event(
        {
            "input_tokens": 100,
            "output_tokens": -1,
            "total_tokens": "bad",
            "input_token_details": {"cache_read": 80, "bad": 1.5},
        },
        event_id="request:1",
        model="stub-model",
    )
    assert event == {
        "event_id": "request:1",
        "provider": "anthropic",
        "model": "stub-model",
        "input_tokens": 100,
        "output_tokens": 0,
        "total_tokens": 0,
        "input_token_details": {"cache_read": 80},
    }


def test_langchain_client_requires_api_key_before_import(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LangChainClientError, match="ANTHROPIC_API_KEY"):
        LangChainAnthropicClient(model="stub-model")


def test_langchain_client_forces_structured_tool_and_records_usage(monkeypatch) -> None:
    pytest.importorskip("langchain_anthropic")
    import langchain_anthropic

    calls: list[dict] = []

    class Runnable:
        def invoke(self, messages):
            calls.append({"messages": messages})
            return {
                "raw": SimpleNamespace(usage_metadata={
                    "input_tokens": 21,
                    "output_tokens": 4,
                    "total_tokens": 25,
                }),
                "parsed": {"ok": True},
                "parsing_error": None,
            }

    class FakeChatAnthropic:
        def __init__(self, **kwargs):
            calls.append({"constructor": kwargs})

        def with_structured_output(self, tool, **kwargs):
            calls.append({"tool": tool, "options": kwargs})
            return Runnable()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", FakeChatAnthropic)
    client = LangChainAnthropicClient(model="stub-model", max_tokens=123)

    result = client(
        system="system prompt",
        user="user payload",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )

    assert result == {"ok": True}
    assert calls[1]["tool"]["name"] == "emit_audit_conversation_turn"
    assert calls[1]["options"] == {
        "method": "function_calling",
        "include_raw": True,
    }
    assert client.usage_events[0]["total_tokens"] == 25
    assert "test-only-key" not in repr(client.usage_events)
