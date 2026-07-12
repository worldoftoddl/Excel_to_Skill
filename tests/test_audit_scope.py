from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from excel_to_skill.audit.contract import bundle_keys
from excel_to_skill.audit.scope import (
    AuditScope,
    AuditScopeError,
    WORKBOOK_SCOPE,
    audit_scopes_plan,
    build_scope_commit,
    bundle_paths,
    dependency_sheets,
    load_scope_bundle,
    resolve_scope,
    sheet_model_context,
    scope_bundle_keys,
    validate_scope_commit,
    write_scope_commit_atomic,
)
from excel_to_skill.audit.model import json_sha256
from excel_to_skill.verify import verify_package

from test_audit_validate import _bundle as _validation_bundle


_SHA = "a" * 64


def _planning_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "scope-plan"
    (pkg / "data").mkdir(parents=True)
    meta = {
        "source": {"sha256": _SHA},
        "sheets": [
            {"name": "Main", "dimensions": "A1:B2"},
            {"name": "Lookup / 2025", "dimensions": "A1"},
        ],
    }
    cells = [
        {"sheet": "Main", "cell": "A1", "row": 1, "col": 1, "value": "합계"},
        {
            "sheet": "Main",
            "cell": "B2",
            "row": 2,
            "col": 2,
            "formula": "'Lookup / 2025'!A1",
        },
        {
            "sheet": "Lookup / 2025",
            "cell": "A1",
            "row": 1,
            "col": 1,
            "value": 10,
        },
    ]
    references = {
        "edges": [{
            "from": "Main!B2",
            "to": "Lookup / 2025!A1",
            "formula": "'Lookup / 2025'!A1",
            "ref_type": "cell",
        }],
        "impacts": {"Lookup / 2025!A1": ["Main!B2"]},
        "external_refs": [],
        "unresolved": [],
        "observability": {"workbook": "full", "note": None},
    }
    (pkg / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (pkg / "data/cells.jsonl").write_text(
        "".join(json.dumps(cell, ensure_ascii=False) + "\n" for cell in cells),
        encoding="utf-8",
    )
    (pkg / "data/references.json").write_text(
        json.dumps(references, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return pkg


def _write_scope_artifacts(paths, facts: dict, context: dict, brief: dict) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    for path, document in zip(
        paths.artifacts, (facts, context, brief), strict=True
    ):
        path.write_text(
            json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def test_resolve_scope_requires_exact_meta_sheet_and_hashes_directory(
    tmp_path: Path,
) -> None:
    pkg = _planning_package(tmp_path)
    scope = resolve_scope(pkg, sheet="Lookup / 2025")
    paths = bundle_paths(pkg, scope)

    assert scope.identity() == {
        "kind": "sheet",
        "sheet": "Lookup / 2025",
        "id": hashlib.sha256("Lookup / 2025".encode("utf-8")).hexdigest(),
    }
    assert paths.data_dir == pkg / "data/audit_scopes/sheets" / scope.id
    assert "Lookup" not in paths.data_dir.name
    assert bundle_paths(pkg, WORKBOOK_SCOPE).facts == pkg / "data/audit_facts.json"
    assert bundle_paths(pkg, WORKBOOK_SCOPE).commit == pkg / "meta.json"

    with pytest.raises(AuditScopeError, match="없는 시트"):
        resolve_scope(pkg, sheet="lookup / 2025")
    with pytest.raises(AuditScopeError, match="동시에"):
        resolve_scope(pkg, sheet="Main", scope=AuditScope.for_sheet("Main"))


def test_dependencies_and_scope_plan_are_deterministic(tmp_path: Path) -> None:
    pkg = _planning_package(tmp_path)

    assert dependency_sheets(pkg, "Main") == ("Lookup / 2025",)
    assert dependency_sheets(pkg, "Lookup / 2025") == ()
    assert audit_scopes_plan(pkg) == audit_scopes_plan(pkg)

    plan = audit_scopes_plan(pkg)
    assert plan["workbook"]["scope"] == {"kind": "workbook"}
    assert plan["workbook"]["sheet_count"] == 2
    assert plan["workbook"]["cell_count"] == 3
    assert plan["workbook"]["region_count"] == 2
    assert plan["workbook"]["analyzable"] is True
    assert plan["workbook"]["state"] == "not_prepared"
    assert plan["workbook"]["estimated_calls"] == {
        "facts": 3, "brief": 1, "total_llm": 4,
    }
    assert plan["all_sheets"]["estimated_calls"] == {
        "facts": 4,
        "brief": 2,
        "total_llm": 6,
    }
    assert [(item["scope"]["sheet"], item["cell_count"], item["region_count"])
            for item in plan["sheets"]] == [
        ("Main", 2, 1),
        ("Lookup / 2025", 1, 1),
    ]
    assert [item["dimensions"] for item in plan["sheets"]] == ["A1:B2", "A1"]
    assert all(item["analyzable"] for item in plan["sheets"])
    assert all(item["state"] == "not_prepared" for item in plan["sheets"])
    assert plan["sheets"][0]["dependency_sheets"] == ["Lookup / 2025"]


def test_sheet_model_context_marks_dependencies_as_unobserved_references(
    tmp_path: Path,
) -> None:
    pkg = _planning_package(tmp_path)
    scope = AuditScope.for_sheet("Main")

    context = sheet_model_context(pkg, scope)

    assert context["scope"] == scope.identity()
    assert context["observed_sheets"] == ["Main"]
    assert context["only_selected_sheet_observed"] is True
    assert context["dependency_sheets"] == ["Lookup / 2025"]
    assert context["dependency_role"] == "formula_reference_indicator_only"
    assert context["dependency_sheet_contents_observed"] is False
    assert "Do not make workbook-wide conclusions" in context["interpretation_rule"]
    with pytest.raises(AuditScopeError, match="sheet scope"):
        sheet_model_context(pkg, WORKBOOK_SCOPE)


def test_empty_workbook_plan_counts_consolidation_and_brief_calls(
    tmp_path: Path,
) -> None:
    pkg = _planning_package(tmp_path)
    (pkg / "data/cells.jsonl").write_text("", encoding="utf-8")

    plan = audit_scopes_plan(pkg)

    assert plan["workbook"]["cell_count"] == 0
    assert plan["workbook"]["region_count"] == 0
    assert plan["workbook"]["analyzable"] is False
    assert plan["workbook"]["estimated_calls"] == {
        "facts": 1,
        "brief": 1,
        "total_llm": 2,
    }
    assert plan["all_sheets"]["estimated_calls"] == {
        "facts": 0,
        "brief": 0,
        "total_llm": 0,
    }


def test_sheet_bundle_keys_are_scope_bound_without_changing_workbook_keys(
    tmp_path: Path,
) -> None:
    _, facts, context, brief = _validation_bundle(tmp_path)
    main = AuditScope.for_sheet("Main")
    other = AuditScope.for_sheet("Other")

    assert scope_bundle_keys(WORKBOOK_SCOPE, facts, context, brief) == bundle_keys(
        facts, context, brief
    )
    assert scope_bundle_keys(main, facts, context, brief) != scope_bundle_keys(
        other, facts, context, brief
    )
    assert scope_bundle_keys(main, facts, context, brief) != bundle_keys(
        facts, context, brief
    )


def test_commit_binds_scope_current_inputs_artifacts_and_loads_atomically(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _validation_bundle(tmp_path)
    scope = AuditScope.for_sheet("Main")
    paths = bundle_paths(pkg, scope)
    _write_scope_artifacts(paths, facts, context, brief)
    commit = build_scope_commit(
        pkg,
        scope,
        facts,
        context,
        brief,
        prepared_at="2026-07-12T00:00:00Z",
    )

    assert not paths.commit.exists()
    assert write_scope_commit_atomic(pkg, scope, commit) == paths.commit
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    assert loaded[0] == paths
    assert loaded[1:4] == (facts, context, brief)
    assert loaded[4] == commit
    assert not list(paths.data_dir.glob(".commit.*.tmp"))

    copied = copy.deepcopy(commit)
    copied["scope"] = AuditScope.for_sheet("Other").identity()
    with pytest.raises(AuditScopeError, match="요청한 시트"):
        validate_scope_commit(pkg, scope, copied, facts, context, brief)


def test_commit_fails_closed_after_cells_or_artifact_changes(tmp_path: Path) -> None:
    pkg, facts, context, brief = _validation_bundle(tmp_path)
    scope = AuditScope.for_sheet("Main")
    paths = bundle_paths(pkg, scope)
    _write_scope_artifacts(paths, facts, context, brief)
    commit = build_scope_commit(pkg, scope, facts, context, brief)
    write_scope_commit_atomic(pkg, scope, commit)

    with (pkg / "data/cells.jsonl").open("a", encoding="utf-8") as file:
        file.write("\n")
    with pytest.raises(AuditScopeError, match="workbook/cells digest"):
        load_scope_bundle(pkg, scope)

    # Restore the exact deterministic ledger, then alter a linked artifact while keeping JSON.
    (pkg / "data/cells.jsonl").write_text(
        '{"sheet": "Main", "cell": "A1", "row": 1, "col": 1, '
        '"value": "매출 위험", "formula": null}\n'
        '{"sheet": "Main", "cell": "B2", "row": 2, "col": 2, '
        '"value": "미해결", "formula": null, "border": true}\n',
        encoding="utf-8",
    )
    changed = copy.deepcopy(brief)
    changed["summary"]["text"] = "변조된 요약"
    paths.brief.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AuditScopeError, match="artifact key"):
        load_scope_bundle(pkg, scope)


def test_scope_commit_rejects_direct_evidence_from_another_sheet(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _validation_bundle(tmp_path)
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sheets"].append({"name": "Other", "dimensions": "A1"})
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    foreign = copy.deepcopy(facts)
    foreign["sources"][0]["sheet"] = "Other"
    foreign["sources"][0]["range"] = "A1"
    # Cross-document hashes must be internally current so the scope boundary is the failure.
    foreign_context = copy.deepcopy(context)
    foreign_context["input"]["audit_facts_sha256"] = json_sha256(foreign)
    foreign_brief = copy.deepcopy(brief)
    foreign_brief["inputs"]["audit_facts_sha256"] = json_sha256(foreign)
    foreign_brief["inputs"]["standards_context_sha256"] = json_sha256(foreign_context)

    scope = AuditScope.for_sheet("Main")
    # The general bundle validator rejects the non-existent foreign cell before the scope check;
    # add it to the deterministic ledger with the digest expected by the source record.
    with (pkg / "data/cells.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps({
            "sheet": "Other", "cell": "A1", "row": 1, "col": 1,
            "value": "매출 위험", "formula": None,
        }) + "\n")
    # Use the actual foreign cell digest, then refresh dependent document hashes.
    from excel_to_skill.audit.sources import WorkbookSourceResolver

    foreign["sources"][0]["content_sha256"] = (
        WorkbookSourceResolver(pkg).resolve("Other!A1").content_sha256
    )
    foreign_context["input"]["audit_facts_sha256"] = json_sha256(foreign)
    foreign_brief["inputs"]["audit_facts_sha256"] = json_sha256(foreign)
    foreign_brief["inputs"]["standards_context_sha256"] = json_sha256(foreign_context)

    commit = {
        "schema_version": "audit_scope_commit.v1",
        "scope": scope.identity(),
        "inputs": {
            "workbook_sha256": _SHA,
            "cells_sha256": hashlib.sha256(
                (pkg / "data/cells.jsonl").read_bytes()
            ).hexdigest(),
        },
        "present": True,
        "status": foreign_brief["readiness"]["status"],
        "version": "0.1.0",
        "facts_key": scope_bundle_keys(
            scope, foreign, foreign_context, foreign_brief
        )[0],
        "standards_key": scope_bundle_keys(
            scope, foreign, foreign_context, foreign_brief
        )[1],
        "brief_key": scope_bundle_keys(
            scope, foreign, foreign_context, foreign_brief
        )[2],
        "prepared_at": "2026-07-12T00:00:00Z",
        "review_status": "draft",
    }
    with pytest.raises(AuditScopeError, match="다른 시트"):
        validate_scope_commit(
            pkg, scope, commit, foreign, foreign_context, foreign_brief
        )


def test_verify_checks_committed_sheet_scopes_and_ignores_uncommitted_staging(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief = _validation_bundle(tmp_path)
    scope = AuditScope.for_sheet("Main")
    paths = bundle_paths(pkg, scope)
    _write_scope_artifacts(paths, facts, context, brief)

    # Artifacts alone are staging and are not advertised to readers or verify.
    assert all(check.name != "audit_scopes" for check in verify_package(pkg).checks)

    commit = build_scope_commit(pkg, scope, facts, context, brief)
    write_scope_commit_atomic(pkg, scope, commit)
    checked = next(
        check for check in verify_package(pkg).checks
        if check.name == "audit_scopes"
    )
    assert checked.ok

    commit["brief_key"] = "0" * 64
    paths.commit.write_text(json.dumps(commit), encoding="utf-8")
    checked = next(
        check for check in verify_package(pkg).checks
        if check.name == "audit_scopes"
    )
    assert not checked.ok
    assert "artifact key" in checked.detail
