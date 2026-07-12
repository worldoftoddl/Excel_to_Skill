from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import excel_to_skill.audit.agent as agent_module
import excel_to_skill.audit.auditpaper_mcp as mcp_module
import excel_to_skill.audit.consume as consume_module
import excel_to_skill.audit.prepare as prepare_module
import excel_to_skill.audit.review as review_module
import excel_to_skill.audit.scope as scope_module
from excel_to_skill.audit.scope import AuditScope, bundle_paths
from excel_to_skill.cli import (
    _build_parser,
    _cmd_audit_agent,
    _cmd_audit_consume,
    _cmd_audit_review,
    _cmd_audit_scopes,
    _cmd_prepare,
)


_SHA = "a" * 64


def _package(tmp_path: Path) -> Path:
    """A small deterministic package with workbook-order and an empty sheet."""
    pkg = tmp_path / "scope-cli"
    (pkg / "data").mkdir(parents=True)
    meta = {
        "source": {"sha256": _SHA},
        "sheets": [
            {"name": "Second", "dimensions": "A1:B2"},
            {"name": "Empty", "dimensions": None},
            {"name": "First", "dimensions": "A1"},
        ],
    }
    cells = [
        {
            "sheet": "Second",
            "cell": "A1",
            "row": 1,
            "col": 1,
            "value": "합계",
        },
        {
            "sheet": "Second",
            "cell": "B2",
            "row": 2,
            "col": 2,
            "formula": "First!A1",
        },
        {
            "sheet": "First",
            "cell": "A1",
            "row": 1,
            "col": 1,
            "value": 10,
        },
    ]
    references = {
        "edges": [{
            "from": "Second!B2",
            "to": "First!A1",
            "formula": "First!A1",
            "ref_type": "cell",
        }],
        "impacts": {"First!A1": ["Second!B2"]},
        "external_refs": [],
        "unresolved": [],
        "observability": {"workbook": "full", "note": None},
    }
    (pkg / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (pkg / "data/cells.jsonl").write_text(
        "".join(
            json.dumps(cell, ensure_ascii=False) + "\n" for cell in cells
        ),
        encoding="utf-8",
    )
    (pkg / "data/references.json").write_text(
        json.dumps(references, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return pkg


def _plan() -> dict:
    rows = [
        {
            "scope": AuditScope.for_sheet("Second").identity(),
            "cell_count": 2,
            "region_count": 1,
            "analyzable": True,
            "estimated_calls": {"facts": 2, "brief": 1, "total_llm": 3},
        },
        {
            "scope": AuditScope.for_sheet("Empty").identity(),
            "cell_count": 0,
            "region_count": 0,
            "analyzable": False,
            "estimated_calls": {"facts": 0, "brief": 0, "total_llm": 0},
        },
        {
            "scope": AuditScope.for_sheet("First").identity(),
            "cell_count": 1,
            "region_count": 1,
            "analyzable": True,
            "estimated_calls": {"facts": 2, "brief": 1, "total_llm": 3},
        },
    ]
    return {
        "schema_version": "audit_scope_plan.v1",
        "workbook": {
            "scope": {"kind": "workbook"},
            "cell_count": 3,
            "region_count": 2,
            "analyzable": True,
            "estimated_calls": {"facts": 3, "brief": 1, "total_llm": 4},
        },
        "all_sheets": {
            "sheet_count": 2,
            "total_sheet_count": 3,
            "skipped_empty_sheet_count": 1,
            "cell_count": 3,
            "region_count": 2,
            "estimated_calls": {"facts": 4, "brief": 2, "total_llm": 6},
        },
        "sheets": rows,
    }


def _cache_standards_descriptor(pkg: Path, *sheets: str) -> None:
    for sheet in sheets:
        path = bundle_paths(pkg, AuditScope.for_sheet(sheet)).standards
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"retriever": {"corpus_version": "stub-v1"}}) + "\n",
            encoding="utf-8",
        )


def _stub_prepare_calls(monkeypatch, pkg: Path) -> list[str]:
    calls: list[str] = []

    def fake_prepare(package, **kwargs):
        assert Path(package) == pkg
        selected = kwargs["scope"]
        calls.append(selected.sheet or "workbook")
        paths = bundle_paths(pkg, selected)
        return SimpleNamespace(
            brief_path=paths.brief,
            status="partial",
            cached=True,
        )

    monkeypatch.setattr(prepare_module, "prepare_package", fake_prepare)
    return calls


def test_audit_scopes_prints_deterministic_workload_plan(
    tmp_path: Path, capsys
) -> None:
    pkg = _package(tmp_path)
    args = _build_parser().parse_args(["audit-scopes", str(pkg)])

    assert _cmd_audit_scopes(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "audit_scope_plan.v1"
    assert output["workbook"]["region_count"] == 2
    assert output["workbook"]["estimated_calls"]["total_llm"] == 4
    assert output["all_sheets"]["skipped_empty_sheet_count"] == 1
    assert [row["scope"]["sheet"] for row in output["sheets"]] == [
        "Second", "Empty", "First"
    ]
    assert output["sheets"][1]["analyzable"] is False


def test_prepare_repeated_sheet_options_dedupe_in_workbook_order(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    monkeypatch.setattr(scope_module, "audit_scopes_plan", lambda _pkg: _plan())
    _cache_standards_descriptor(pkg, "Second", "First")
    calls = _stub_prepare_calls(monkeypatch, pkg)
    args = _build_parser().parse_args([
        "prepare",
        str(pkg),
        "--sheet",
        "First",
        "--sheet",
        "Second",
        "--sheet",
        "First",
    ])

    assert _cmd_prepare(
        args,
        client_factory=lambda _model: pytest.fail("model factory called"),
        caller_factory=lambda _connection: pytest.fail("MCP factory called"),
    ) == 0

    captured = capsys.readouterr()
    assert calls == ["Second", "First"]
    assert "independent_sheets=2" in captured.err
    assert captured.out.splitlines() == [
        str(bundle_paths(pkg, AuditScope.for_sheet("Second")).brief),
        str(bundle_paths(pkg, AuditScope.for_sheet("First")).brief),
    ]


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (["--sheet", "Unknown"], "없는 시트"),
        (["--sheet", "Empty"], "없는 시트입니다"),
    ],
)
def test_prepare_invalid_sheet_fails_before_model_or_mcp_factory(
    tmp_path: Path,
    monkeypatch,
    capsys,
    selection: list[str],
    message: str,
) -> None:
    pkg = _package(tmp_path)
    monkeypatch.setattr(scope_module, "audit_scopes_plan", lambda _pkg: _plan())
    model_calls = 0
    mcp_calls = 0

    def model_factory(_model):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("model factory must not be reached")

    def mcp_factory(_connection):
        nonlocal mcp_calls
        mcp_calls += 1
        raise AssertionError("MCP factory must not be reached")

    args = _build_parser().parse_args(["prepare", str(pkg), *selection])
    assert _cmd_prepare(
        args, client_factory=model_factory, caller_factory=mcp_factory
    ) == 1

    captured = capsys.readouterr()
    assert model_calls == mcp_calls == 0
    assert "scope 선택 오류" in captured.err
    assert message in captured.err


def test_prepare_all_sheets_calls_each_nonempty_scope_independently(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    monkeypatch.setattr(scope_module, "audit_scopes_plan", lambda _pkg: _plan())
    _cache_standards_descriptor(pkg, "Second", "First")
    calls = _stub_prepare_calls(monkeypatch, pkg)
    args = _build_parser().parse_args([
        "prepare", str(pkg), "--all-sheets"
    ])

    assert _cmd_prepare(
        args,
        client_factory=lambda _model: pytest.fail("model factory called"),
        caller_factory=lambda _connection: pytest.fail("MCP factory called"),
    ) == 0

    captured = capsys.readouterr()
    assert calls == ["Second", "First"]
    assert "Empty" not in captured.out
    assert "independent_sheets=2" in captured.err


def test_prepare_all_sheets_resets_retrieval_budget_per_scope(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    monkeypatch.setattr(scope_module, "audit_scopes_plan", lambda _pkg: _plan())
    instances = []
    passed_retrievers = []

    class FakeRetriever:
        def __init__(
            self,
            caller,
            *,
            policy,
            expected_collection=None,
            paragraph_cache_dir=None,
        ) -> None:
            self.expected_collection = expected_collection
            instances.append(self)

        def discover_collection(self):
            return "stub-v1"

    def fake_prepare(package, **kwargs):
        selected = kwargs["scope"]
        passed_retrievers.append(kwargs["retriever"])
        return SimpleNamespace(
            brief_path=bundle_paths(package, selected).brief,
            status="partial",
            cached=False,
        )

    monkeypatch.setattr(mcp_module, "AuditpaperStandardsRetriever", FakeRetriever)
    monkeypatch.setattr(
        mcp_module, "load_mcp_connection", lambda **_kwargs: object()
    )
    monkeypatch.setattr(prepare_module, "prepare_package", fake_prepare)
    args = _build_parser().parse_args([
        "prepare", str(pkg), "--all-sheets"
    ])

    assert _cmd_prepare(
        args,
        client_factory=lambda _model: pytest.fail("model factory called"),
        caller_factory=lambda _connection: object(),
    ) == 0
    capsys.readouterr()

    assert len(instances) == 3  # one collection probe + one budget per nonempty sheet
    assert passed_retrievers == instances[1:]
    assert passed_retrievers[0] is not passed_retrievers[1]
    assert all(
        retriever.expected_collection == "stub-v1"
        for retriever in passed_retrievers
    )


def test_prepare_preserves_empty_workbook_not_ready_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    empty_plan = _plan()
    empty_plan["workbook"] = {
        **empty_plan["workbook"],
        "cell_count": 0,
        "region_count": 0,
        "analyzable": False,
        "estimated_calls": {"facts": 1, "brief": 1, "total_llm": 2},
    }
    monkeypatch.setattr(
        scope_module, "audit_scopes_plan", lambda _pkg: empty_plan
    )
    (pkg / "data/standards_context.json").write_text(
        json.dumps({"retriever": {"corpus_version": "stub-v1"}}) + "\n",
        encoding="utf-8",
    )
    calls = _stub_prepare_calls(monkeypatch, pkg)
    args = _build_parser().parse_args(["prepare", str(pkg)])

    assert _cmd_prepare(
        args,
        client_factory=lambda _model: pytest.fail("model factory called"),
        caller_factory=lambda _connection: pytest.fail("MCP factory called"),
    ) == 0
    capsys.readouterr()
    assert calls == ["workbook"]


@pytest.mark.parametrize(
    "argv",
    [
        ["brief", "PACKAGE", "--sheet", "Data"],
        ["audit-search", "PACKAGE", "--query", "risk", "--sheet", "Data"],
        ["audit-get", "PACKAGE", "--id", "fact:1", "--sheet", "Data"],
        ["assertion-procedures", "PACKAGE", "--sheet", "Data"],
        ["trace", "PACKAGE", "--id", "fact:1", "--sheet", "Data"],
        ["audit-agent", "PACKAGE", "--sheet", "Data"],
        ["audit-review", "PACKAGE", "--sheet", "Data", "--approve"],
    ],
)
def test_all_audit_reader_agent_and_review_parsers_accept_sheet(
    argv: list[str],
) -> None:
    args = _build_parser().parse_args(argv)
    assert args.sheet == "Data"


def test_audit_reader_commands_forward_sheet_to_every_consumer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    observed: list[tuple[str, str | None]] = []

    def stub(name):
        def call(_pkg, **kwargs):
            observed.append((name, kwargs.get("sheet")))
            return {"command": name, "scope": {"kind": "sheet", "sheet": "First"}}

        return call

    for name in (
        "brief",
        "audit_search",
        "audit_get",
        "assertion_procedures",
        "trace",
    ):
        monkeypatch.setattr(consume_module, name, stub(name))

    commands = [
        ["brief", str(pkg), "--sheet", "First"],
        [
            "audit-search", str(pkg), "--query", "risk", "--sheet", "First"
        ],
        [
            "audit-get", str(pkg), "--id", "fact:1", "--sheet", "First"
        ],
        ["assertion-procedures", str(pkg), "--sheet", "First"],
        ["trace", str(pkg), "--id", "fact:1", "--sheet", "First"],
    ]
    for argv in commands:
        assert _cmd_audit_consume(_build_parser().parse_args(argv)) == 0
        assert json.loads(capsys.readouterr().out)["scope"]["sheet"] == "First"

    assert observed == [
        ("brief", "First"),
        ("audit_search", "First"),
        ("audit_get", "First"),
        ("assertion_procedures", "First"),
        ("trace", "First"),
    ]


def test_audit_agent_and_review_commands_forward_sheet(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    observed: list[tuple[str, str | None]] = []

    def run_agent(_pkg, **kwargs):
        observed.append(("agent", kwargs.get("sheet")))
        return {
            "schema_version": "audit_agent_response.v2",
            "scope": {"kind": "sheet", "sheet": "First"},
        }

    def approve(_pkg, **kwargs):
        observed.append(("review", kwargs.get("sheet")))
        return {"status": "approved", "commit": "scope/commit.json"}

    monkeypatch.setattr(agent_module, "run_audit_agent", run_agent)
    monkeypatch.setattr(review_module, "approve_audit_package", approve)

    agent_args = _build_parser().parse_args([
        "audit-agent", str(pkg), "--sheet", "First", "--json"
    ])
    assert _cmd_audit_agent(
        agent_args,
        client_factory=lambda: pytest.fail("model factory called"),
    ) == 0
    assert json.loads(capsys.readouterr().out)["scope"]["sheet"] == "First"

    review_args = _build_parser().parse_args([
        "audit-review", str(pkg), "--sheet", "First", "--approve"
    ])
    assert _cmd_audit_review(review_args) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "scope/commit.json"
    assert "sheet='First'" in captured.err
    assert observed == [("agent", "First"), ("review", "First")]
