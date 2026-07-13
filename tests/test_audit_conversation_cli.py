from __future__ import annotations

import json
from argparse import Namespace
from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from excel_to_skill.cli import _build_parser, _cmd_audit_chat

from test_audit_consume_gate import _write_committed_bundle


class StubClient:
    def __init__(self, responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.popleft()


def _final() -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": False,
            "abstention_code": None,
            "selections": [{"kind": "statement", "ids": ["statement:fact"]}],
        },
    }


def _args(pkg: Path, *, thread: str | None, json_output: bool) -> Namespace:
    return Namespace(
        path=str(pkg),
        sheet=None,
        question="핵심 위험은?",
        thread=thread,
        model="stub-model",
        limit=100,
        max_steps=6,
        json=json_output,
    )


def test_audit_chat_cli_persists_and_resumes_thread(
    tmp_path: Path,
    capsys,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    first_client = StubClient([_final()])

    assert _cmd_audit_chat(
        _args(pkg, thread="cli-thread", json_output=True),
        client_factory=lambda: first_client,
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["schema_version"] == "audit_conversation_turn_result.v1"
    assert first["thread_id"] == "cli-thread"
    assert first["turn_index"] == 1 and first["resumed"] is False
    assert first["response"]["trust"]["answer_review_status"] == "unreviewed"

    second_client = StubClient([_final()])
    assert _cmd_audit_chat(
        _args(pkg, thread="cli-thread", json_output=True),
        client_factory=lambda: second_client,
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["turn_index"] == 2 and second["resumed"] is True
    payload = json.loads(second_client.calls[0]["user"])
    assert payload["observations"][-1]["tool"] == "conversation_focus"


def test_audit_chat_cli_prints_generated_thread_in_markdown(
    tmp_path: Path,
    capsys,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)

    assert _cmd_audit_chat(
        _args(pkg, thread=None, json_output=False),
        client_factory=lambda: StubClient([_final()]),
    ) == 0

    output = capsys.readouterr().out
    assert "> 대화 thread: `audit-" in output
    assert "turn 1 · 신규" in output
    assert "# 위험평가 질의 답변" in output


def test_audit_chat_parser_requires_question_and_accepts_thread() -> None:
    parser = _build_parser()
    parsed = parser.parse_args([
        "audit-chat",
        "/tmp/package",
        "--question",
        "결론은?",
        "--thread",
        "thread-7",
    ])
    assert parsed.cmd == "audit-chat"
    assert parsed.question == "결론은?"
    assert parsed.thread == "thread-7"

    with pytest.raises(SystemExit):
        parser.parse_args(["audit-chat", "/tmp/package"])


def test_audit_chat_cli_rejects_non_package_before_client_factory(
    tmp_path: Path,
    capsys,
) -> None:
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("must not construct client")

    assert _cmd_audit_chat(
        _args(tmp_path / "missing", thread="t", json_output=True),
        client_factory=factory,
    ) == 1
    assert called is False
    assert "패키지 폴더가 아님" in capsys.readouterr().err
