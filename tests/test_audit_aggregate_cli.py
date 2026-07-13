from __future__ import annotations

import json

import pytest

import excel_to_skill.audit.aggregate as aggregate_module
from excel_to_skill.cli import _build_parser, _cmd_audit_aggregate, main

from test_audit_aggregate import SelectionClient, _package
from test_audit_validate import _bundle as _validation_bundle


def test_parser_requires_explicit_aggregate_selection(tmp_path) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["audit-aggregate", str(tmp_path)])


def test_plan_uses_no_model_factory_and_reports_compact_payload(
    tmp_path, capsys
) -> None:
    pkg = _package(tmp_path)
    args = _build_parser().parse_args([
        "audit-aggregate",
        str(pkg),
        "--all-committed-sheets",
        "--plan",
        "--model",
        "stub-model",
    ])
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("--plan must not construct a model client")

    assert _cmd_audit_aggregate(args, client_factory=factory) == 0
    assert factory_calls == 0
    output = json.loads(capsys.readouterr().out)
    assert output["selection"]["mode"] == "all_committed_sheets"
    assert output["coverage"]["included_sheets"] == ["Main", "Other"]
    assert output["coverage"]["model_context_bytes"] < 600_000
    assert output["estimated_model_calls"] == 1


def test_main_dispatches_aggregate_plan_without_loading_provider(
    tmp_path, capsys
) -> None:
    pkg = _package(tmp_path)

    assert main([
        "audit-aggregate",
        str(pkg),
        "--sheet",
        "Main",
        "--plan",
        "--model",
        "stub-model",
    ]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["coverage"]["included_sheets"] == ["Main"]


def test_repeated_sheets_dedupe_in_workbook_order_and_json_output(
    tmp_path, capsys
) -> None:
    pkg = _package(tmp_path)
    args = _build_parser().parse_args([
        "audit-aggregate",
        str(pkg),
        "--sheet",
        "Other",
        "--sheet",
        "Main",
        "--sheet",
        "Other",
        "--model",
        "stub-model",
        "--json",
    ])
    client = SelectionClient()

    assert _cmd_audit_aggregate(args, client_factory=lambda: client) == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert client.calls == 1
    assert output["coverage"]["included_sheets"] == ["Main", "Other"]
    assert [account["scope"]["sheet"] for account in output["accounts"]] == [
        "Main", "Other",
    ]
    assert "status=partial" in captured.err


def test_markdown_cache_hit_does_not_construct_client(tmp_path, capsys) -> None:
    pkg = _package(tmp_path)
    first_args = _build_parser().parse_args([
        "audit-aggregate", str(pkg), "--sheet", "Main", "--model", "stub-model",
    ])
    assert _cmd_audit_aggregate(
        first_args, client_factory=lambda: SelectionClient()
    ) == 0
    capsys.readouterr()

    factory_calls = 0

    def forbidden_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("cache hit must not construct a model client")

    assert _cmd_audit_aggregate(first_args, client_factory=forbidden_factory) == 0
    captured = capsys.readouterr()
    assert factory_calls == 0
    assert captured.out.startswith("# 계정별 종합 브리핑")
    assert "cache" in captured.err

    forced_plan = _build_parser().parse_args([
        "audit-aggregate", str(pkg), "--sheet", "Main", "--model", "stub-model",
        "--plan", "--force",
    ])
    assert _cmd_audit_aggregate(
        forced_plan, client_factory=forbidden_factory
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["force"] is True
    assert plan["cache_available"] is False
    assert plan["estimated_model_calls"] == 1
    assert factory_calls == 0


def test_invalid_or_absent_scope_fails_before_model_factory(tmp_path, capsys) -> None:
    pkg = _package(tmp_path / "unknown")
    args = _build_parser().parse_args([
        "audit-aggregate", str(pkg), "--sheet", "Unknown", "--model", "stub-model",
    ])
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("invalid selection must fail before model")

    assert _cmd_audit_aggregate(args, client_factory=factory) == 1
    assert factory_calls == 0
    assert "없는 시트" in capsys.readouterr().err

    empty_pkg, _, _, _ = _validation_bundle(tmp_path / "none")
    (empty_pkg / "data/references.json").write_text(
        json.dumps({"edges": []}) + "\n", encoding="utf-8"
    )
    no_commits = _build_parser().parse_args([
        "audit-aggregate",
        str(empty_pkg),
        "--all-committed-sheets",
        "--plan",
        "--model",
        "stub-model",
    ])
    assert _cmd_audit_aggregate(no_commits, client_factory=factory) == 1
    assert factory_calls == 0
    assert "commit된 시트" in capsys.readouterr().err


def test_publish_oserror_returns_clean_cli_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    args = _build_parser().parse_args([
        "audit-aggregate", str(pkg), "--sheet", "Main", "--model", "stub-model",
    ])

    def fail_write(_path, _text):
        raise OSError("disk full")

    monkeypatch.setattr(aggregate_module, "_atomic_write_text", fail_write)
    assert _cmd_audit_aggregate(
        args, client_factory=lambda: SelectionClient()
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disk full" in captured.err
