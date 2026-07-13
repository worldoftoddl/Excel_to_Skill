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


def _args(
    pkg: Path,
    *,
    thread: str | None,
    json_output: bool,
    aggregate_id: str | None = None,
) -> Namespace:
    return Namespace(
        path=str(pkg),
        sheet=None,
        aggregate_id=aggregate_id,
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


def test_audit_chat_parser_makes_sheet_and_aggregate_mutually_exclusive() -> None:
    parser = _build_parser()
    aggregate_id = "a" * 64

    parsed = parser.parse_args([
        "audit-chat",
        "/tmp/package",
        "--aggregate-id",
        aggregate_id,
        "--question",
        "전체 핵심 위험은?",
    ])
    assert parsed.sheet is None
    assert parsed.aggregate_id == aggregate_id

    with pytest.raises(SystemExit):
        parser.parse_args([
            "audit-chat",
            "/tmp/package",
            "--sheet",
            "C",
            "--aggregate-id",
            aggregate_id,
            "--question",
            "전체 핵심 위험은?",
        ])


def test_audit_chat_parser_accepts_opt_in_research_without_literal_token() -> None:
    parser = _build_parser()
    parsed = parser.parse_args([
        "audit-chat",
        "/tmp/package",
        "--question",
        "외부조회 기준은?",
        "--standards-research",
        "--standards-research-top-k",
        "4",
        "--standards-research-definitions",
        "2",
        "--mcp-token-env",
        "CUSTOM_MCP_TOKEN",
    ])

    assert parsed.standards_research is True
    assert parsed.standards_research_top_k == 4
    assert parsed.standards_research_definitions == 2
    assert parsed.mcp_token_env == "CUSTOM_MCP_TOKEN"
    assert not hasattr(parsed, "mcp_token")


def test_audit_chat_parser_accepts_opt_in_procedure_planning() -> None:
    parser = _build_parser()
    parsed = parser.parse_args([
        "audit-chat",
        "/tmp/package",
        "--question",
        "이 위험에 어떤 테스트를 할 수 있어?",
        "--procedure-planning",
    ])

    assert parsed.procedure_planning is True


@pytest.mark.parametrize(
    ("planning_enabled", "expected_max_tokens"),
    [(False, 8192), (True, 16384)],
)
def test_audit_chat_default_client_reserves_output_budget_for_planning(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    planning_enabled: bool,
    expected_max_tokens: int,
) -> None:
    from excel_to_skill.audit import langchain_client as client_module

    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    captured: dict[str, object] = {}

    def fake_builder(model: str, *, max_tokens: int, purpose: str):
        captured.update({
            "model": model,
            "max_tokens": max_tokens,
            "purpose": purpose,
        })
        return StubClient([_final()])

    monkeypatch.setattr(
        client_module,
        "build_langchain_anthropic_client",
        fake_builder,
    )
    args = _args(pkg, thread=None, json_output=True)
    args.procedure_planning = planning_enabled

    assert _cmd_audit_chat(args) == 0
    capsys.readouterr()
    assert captured == {
        "model": "stub-model",
        "max_tokens": expected_max_tokens,
        "purpose": "audit-chat",
    }


def test_audit_chat_cli_forwards_aggregate_id(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    aggregate_id = "b" * 64
    captured: dict = {}

    def fake_run(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return {"forwarded": True}

    monkeypatch.setattr(
        "excel_to_skill.audit.conversation.run_audit_conversation_turn",
        fake_run,
    )

    assert _cmd_audit_chat(
        _args(
            pkg,
            thread="aggregate-thread",
            json_output=True,
            aggregate_id=aggregate_id,
        ),
        client_factory=lambda: StubClient([]),
    ) == 0

    assert json.loads(capsys.readouterr().out) == {"forwarded": True}
    assert captured["path"] == pkg
    assert captured["sheet"] is None
    assert captured["aggregate_id"] == aggregate_id


def test_audit_chat_cli_forwards_opt_in_research_factory(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    captured: dict = {}
    sentinel_factory = object()

    def fake_run(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return {"research_forwarded": True}

    monkeypatch.setattr(
        "excel_to_skill.audit.conversation.run_audit_conversation_turn",
        fake_run,
    )
    args = _args(pkg, thread="research-cli", json_output=True)
    args.standards_research = True
    args.standards_research_top_k = 5
    args.standards_research_definitions = 1

    assert _cmd_audit_chat(
        args,
        client_factory=lambda: StubClient([]),
        standards_retriever_factory=sentinel_factory,
    ) == 0

    assert json.loads(capsys.readouterr().out) == {"research_forwarded": True}
    assert captured["standards_research"] is True
    assert captured["standards_retriever_factory"] is sentinel_factory


def test_audit_chat_cli_forwards_opt_in_procedure_planning(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    captured: dict = {}

    def fake_run(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return {"planning_forwarded": True}

    monkeypatch.setattr(
        "excel_to_skill.audit.conversation.run_audit_conversation_turn",
        fake_run,
    )
    args = _args(pkg, thread="planning-cli", json_output=True)
    args.procedure_planning = True

    assert _cmd_audit_chat(
        args,
        client_factory=lambda: StubClient([]),
    ) == 0

    assert json.loads(capsys.readouterr().out) == {"planning_forwarded": True}
    assert captured["procedure_planning"] is True


def test_audit_chat_default_research_factory_is_lazy_and_budgets_definitions(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    connection_calls: list[dict] = []
    created: dict = {}

    def fake_load_mcp_connection(**kwargs):
        connection_calls.append(kwargs)
        return "connection"

    class FakeCaller:
        def __init__(self, connection, **kwargs) -> None:
            created["caller"] = {"connection": connection, **kwargs}

    class FakeRetriever:
        def __init__(self, caller, **kwargs) -> None:
            created["retriever"] = {"caller": caller, **kwargs}

    def fake_run(_path, **kwargs):
        assert connection_calls == []
        retriever = kwargs["standards_retriever_factory"]("collection-1")
        assert isinstance(retriever, FakeRetriever)
        return {"lazy_default": True}

    monkeypatch.setattr(
        "excel_to_skill.audit.auditpaper_mcp.load_mcp_connection",
        fake_load_mcp_connection,
    )
    monkeypatch.setattr(
        "excel_to_skill.audit.auditpaper_mcp.FastMCPHTTPCaller",
        FakeCaller,
    )
    monkeypatch.setattr(
        "excel_to_skill.audit.auditpaper_mcp.AuditpaperStandardsRetriever",
        FakeRetriever,
    )
    monkeypatch.setattr(
        "excel_to_skill.audit.conversation.run_audit_conversation_turn",
        fake_run,
    )
    args = _args(pkg, thread="research-lazy", json_output=True)
    args.standards_research = True
    args.standards_research_top_k = 4
    args.standards_research_definitions = 2

    assert _cmd_audit_chat(
        args,
        client_factory=lambda: StubClient([]),
    ) == 0

    assert json.loads(capsys.readouterr().out) == {"lazy_default": True}
    assert len(connection_calls) == 1
    policy = created["retriever"]["policy"]
    assert policy.top_k == 4
    assert policy.max_definitions == 2
    assert policy.max_citations == 6
    assert created["retriever"]["expected_collection"] == "collection-1"


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
