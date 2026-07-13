"""Lazy LangChain/Anthropic structured-output adapter for audit graphs.

The deterministic audit layers keep accepting the small provider-neutral callable used by
``audit.llm.call_json``.  This adapter lets a compiled LangGraph use ``ChatAnthropic`` without
putting a model object, credentials, messages, or raw responses into graph state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any


class LangChainClientError(RuntimeError):
    """A LangChain model client could not be configured or parsed safely."""


def _usage_event(value: object, *, event_id: str, model: str) -> dict:
    usage = value if isinstance(value, dict) else {}

    def integer(name: str) -> int:
        item = usage.get(name, 0)
        return (
            item
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
            else 0
        )

    details: dict[str, dict[str, int]] = {}
    for name in ("input_token_details", "output_token_details"):
        source = usage.get(name)
        if not isinstance(source, dict):
            continue
        clean = {
            str(key): item
            for key, item in source.items()
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        }
        if clean:
            details[name] = clean
    return {
        "event_id": event_id,
        "provider": "anthropic",
        "model": model,
        "input_tokens": integer("input_tokens"),
        "output_tokens": integer("output_tokens"),
        "total_tokens": integer("total_tokens"),
        **details,
    }


class LangChainAnthropicClient:
    """Callable structured-output client backed by ``ChatAnthropic``.

    Imports remain inside construction so the core, ``prepare`` and legacy ``audit-agent``
    commands do not require the optional ``graph`` extra.  Usage is recorded per request rather
    than accumulated in mutable graph state; the graph copies completed events into its private
    turn artifact.
    """

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int = 8192,
        purpose: str = "audit-chat",
    ) -> None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LangChainClientError(
                f"ANTHROPIC_API_KEY 미설정 — {purpose}에는 API 키가 필요합니다."
            )
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise LangChainClientError(
                "audit-chat에는 graph extra가 필요합니다: uv sync --extra graph"
            ) from e
        self.model = model
        self._llm = ChatAnthropic(
            model=model,
            api_key=key,
            max_tokens=max_tokens,
            temperature=0,
            max_retries=0,
        )
        self._runnables: dict[str, Any] = {}
        self._usage_events: list[dict] = []

    @property
    def usage_events(self) -> tuple[dict, ...]:
        """Return immutable snapshots of request-level token usage reported by LangChain."""
        return tuple(copy.deepcopy(self._usage_events))

    def _structured(self, schema: dict):
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        runnable = self._runnables.get(digest)
        if runnable is None:
            tool = {
                "name": "emit_audit_conversation_turn",
                "description": "요청된 감사 대화 turn 스키마에 맞는 객체 하나를 방출한다.",
                "input_schema": copy.deepcopy(schema),
            }
            runnable = self._llm.with_structured_output(
                tool,
                method="function_calling",
                include_raw=True,
            )
            self._runnables[digest] = runnable
        return runnable

    def __call__(self, *, system: str, user: str, schema: dict) -> dict:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError as e:  # pragma: no cover - guarded by construction in real installs
            raise LangChainClientError(
                "audit-chat LangChain runtime을 불러올 수 없습니다."
            ) from e
        messages = [
            SystemMessage(content=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]),
            HumanMessage(content=user),
        ]
        try:
            result = self._structured(schema).invoke(messages)
        except Exception as e:  # noqa: BLE001 - provider boundary
            raise LangChainClientError(f"LangChain Anthropic 호출 실패: {e}") from e
        if not isinstance(result, dict):
            raise LangChainClientError("LangChain structured output 결과가 객체가 아닙니다.")
        parsing_error = result.get("parsing_error")
        parsed = result.get("parsed")
        if parsing_error is not None:
            raise LangChainClientError(
                f"LangChain structured output 파싱 실패: {parsing_error}"
            )
        if not isinstance(parsed, dict):
            raise LangChainClientError("LangChain structured output parsed 결과가 객체가 아닙니다.")
        raw = result.get("raw")
        usage = getattr(raw, "usage_metadata", None)
        self._usage_events.append(_usage_event(
            usage,
            event_id=f"request:{len(self._usage_events) + 1}",
            model=self.model,
        ))
        return parsed


def build_langchain_anthropic_client(
    model: str,
    *,
    max_tokens: int = 8192,
    purpose: str = "audit-chat",
) -> LangChainAnthropicClient:
    """Build the lazy-imported production client used by ``audit-chat``."""
    return LangChainAnthropicClient(
        model=model,
        max_tokens=max_tokens,
        purpose=purpose,
    )


__all__ = [
    "LangChainAnthropicClient",
    "LangChainClientError",
    "build_langchain_anthropic_client",
]
