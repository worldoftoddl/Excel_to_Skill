"""Deterministic identities and commit gates for worksheet-scoped audit bundles.

The historical workbook audit bundle keeps its fixed ``data/audit_*.json`` paths and
``meta.audit_preparation`` marker.  A worksheet scope instead lives below a hash-only directory
and advertises its three artifacts through a local ``commit.json`` written last.  This module is
the bounded foundation for that second storage shape; orchestration and consumer routing remain
in their existing modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jsonschema import Draft7Validator

from .. import cache
from ..meta import _now_iso
from ..resources import SCHEMA_DIR
from .contract import PREPARE_VERSION, bundle_keys
from .regions import build_regions
from .validate import AuditValidationError, validate_audit_bundle


SCOPE_COMMIT_SCHEMA_VERSION = "audit_scope_commit.v1"
_SCOPE_ROOT = Path("data/audit_scopes/sheets")
_ARTIFACT_NAMES = (
    "audit_facts.json",
    "standards_context.json",
    "audit_brief.json",
)


class AuditScopeError(RuntimeError):
    """A requested audit scope or its committed bundle is invalid."""


@dataclass(frozen=True, slots=True)
class AuditScope:
    """Canonical analysis boundary; workbook is legacy, sheet is independently committed."""

    kind: str
    sheet: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"workbook", "sheet"}:
            raise ValueError("audit scope kind는 workbook/sheet 중 하나여야 합니다.")
        if self.kind == "workbook":
            if self.sheet is not None:
                raise ValueError("workbook scope에는 sheet를 지정할 수 없습니다.")
            return
        if not isinstance(self.sheet, str) or not self.sheet:
            raise ValueError("sheet scope에는 비어 있지 않은 정확한 시트명이 필요합니다.")

    @classmethod
    def workbook(cls) -> "AuditScope":
        return cls("workbook")

    @classmethod
    def for_sheet(cls, sheet: str) -> "AuditScope":
        return cls("sheet", sheet)

    @property
    def id(self) -> str:
        if self.kind == "workbook":
            return "workbook"
        assert self.sheet is not None
        return hashlib.sha256(self.sheet.encode("utf-8")).hexdigest()

    def identity(self) -> dict:
        if self.kind == "workbook":
            return {"kind": "workbook"}
        return {"kind": "sheet", "sheet": self.sheet, "id": self.id}


WORKBOOK_SCOPE = AuditScope.workbook()


@dataclass(frozen=True, slots=True)
class BundlePaths:
    """All authoritative paths for one resolved audit scope."""

    package: Path
    scope: AuditScope
    data_dir: Path
    facts: Path
    standards: Path
    brief: Path
    commit: Path

    @property
    def artifacts(self) -> tuple[Path, Path, Path]:
        return self.facts, self.standards, self.brief


def _read_json_object(path: Path, *, label: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AuditScopeError(f"{label} 없음: {path}") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AuditScopeError(f"{label} 읽기 실패: {e}") from e
    if not isinstance(document, dict):
        raise AuditScopeError(f"{label}은 JSON 객체여야 합니다.")
    return document


def _meta_sheet_names(pkg: Path) -> tuple[str, ...]:
    meta = _read_json_object(pkg / "meta.json", label="meta.json")
    sheets = meta.get("sheets")
    if not isinstance(sheets, list):
        raise AuditScopeError("meta.sheets는 배열이어야 합니다.")
    names: list[str] = []
    for index, sheet in enumerate(sheets):
        if not isinstance(sheet, dict) or not isinstance(sheet.get("name"), str):
            raise AuditScopeError(f"meta.sheets[{index}].name이 유효하지 않습니다.")
        names.append(sheet["name"])
    if len(names) != len(set(names)):
        raise AuditScopeError("meta.sheets에 중복 시트명이 있습니다.")
    return tuple(names)


def resolve_scope(
    pkg: Path | str,
    *,
    sheet: str | None = None,
    scope: AuditScope | None = None,
) -> AuditScope:
    """Resolve and validate one exact scope against the converted package metadata."""
    path = Path(pkg)
    if scope is not None and sheet is not None:
        raise AuditScopeError("scope와 sheet를 동시에 지정할 수 없습니다.")
    selected = scope or (AuditScope.for_sheet(sheet) if sheet is not None else WORKBOOK_SCOPE)
    names = _meta_sheet_names(path)
    if selected.kind == "sheet" and selected.sheet not in names:
        raise AuditScopeError(f"meta.json에 없는 시트입니다: {selected.sheet!r}")
    return selected


def bundle_paths(pkg: Path | str, scope: AuditScope = WORKBOOK_SCOPE) -> BundlePaths:
    """Return fixed legacy paths for workbook or hash-namespaced paths for one sheet."""
    path = Path(pkg)
    selected = resolve_scope(path, scope=scope)
    if selected.kind == "workbook":
        data_dir = path / "data"
        commit = path / "meta.json"
    else:
        data_dir = path / _SCOPE_ROOT / selected.id
        commit = data_dir / "commit.json"
    return BundlePaths(
        package=path,
        scope=selected,
        data_dir=data_dir,
        facts=data_dir / _ARTIFACT_NAMES[0],
        standards=data_dir / _ARTIFACT_NAMES[1],
        brief=data_dir / _ARTIFACT_NAMES[2],
        commit=commit,
    )


def _address_sheet(address: object) -> str | None:
    if not isinstance(address, str) or "!" not in address:
        return None
    sheet, _ = address.rsplit("!", 1)
    return sheet or None


def dependency_sheets(pkg: Path | str, sheet: str) -> tuple[str, ...]:
    """Return direct cross-sheet formula dependencies in workbook sheet order."""
    path = Path(pkg)
    selected = resolve_scope(path, sheet=sheet)
    assert selected.sheet is not None
    names = _meta_sheet_names(path)
    references = _read_json_object(
        path / "data/references.json", label="data/references.json"
    )
    edges = references.get("edges")
    if not isinstance(edges, list):
        raise AuditScopeError("data/references.json edges는 배열이어야 합니다.")
    found: set[str] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise AuditScopeError("data/references.json edge는 객체여야 합니다.")
        from_sheet = _address_sheet(edge.get("from"))
        to_sheet = _address_sheet(edge.get("to"))
        if from_sheet == selected.sheet and to_sheet not in {None, selected.sheet}:
            if to_sheet not in names:
                raise AuditScopeError(
                    f"references edge가 meta에 없는 내부 시트를 가리킵니다: {to_sheet!r}"
                )
            found.add(to_sheet)
    return tuple(name for name in names if name in found)


def sheet_model_context(pkg: Path | str, scope: AuditScope) -> dict:
    """Describe the exact worksheet observation boundary exposed to semantic models."""
    path = Path(pkg)
    selected = resolve_scope(path, scope=scope)
    if selected.kind != "sheet":
        raise AuditScopeError("sheet model context는 sheet scope에만 사용합니다.")
    assert selected.sheet is not None
    return {
        "scope": selected.identity(),
        "observed_sheets": [selected.sheet],
        "only_selected_sheet_observed": True,
        "dependency_sheets": list(dependency_sheets(path, selected.sheet)),
        "dependency_role": "formula_reference_indicator_only",
        "dependency_sheet_contents_observed": False,
        "interpretation_rule": (
            "Treat the result as analysis of only the selected sheet. "
            "Do not make workbook-wide conclusions. Dependency sheet names indicate formula "
            "references only; their cell contents were not observed and are not evidence."
        ),
    }


def scope_bundle_keys(
    scope: AuditScope,
    facts: dict,
    standards: dict,
    brief: dict,
) -> tuple[str, str, str]:
    """Compute legacy workbook keys or sheet-identity-bound artifact keys."""
    base = bundle_keys(facts, standards, brief)
    if scope.kind == "workbook":
        return base
    identity = scope.identity()
    facts_key = cache.artifact_key(
        "audit_facts", {"scope": identity, "artifact_key": base[0]}
    )
    standards_key = cache.artifact_key(
        "standards_context",
        {"scope": identity, "facts_key": facts_key, "artifact_key": base[1]},
    )
    brief_key = cache.artifact_key(
        "audit_brief",
        {
            "scope": identity,
            "facts_key": facts_key,
            "standards_key": standards_key,
            "artifact_key": base[2],
        },
    )
    return facts_key, standards_key, brief_key


def _current_inputs(pkg: Path) -> dict:
    meta = _read_json_object(pkg / "meta.json", label="meta.json")
    source = meta.get("source")
    workbook_sha = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(workbook_sha, str):
        raise AuditScopeError("meta.source.sha256이 유효하지 않습니다.")
    cells_path = pkg / "data/cells.jsonl"
    try:
        cells_sha = cache.file_sha256(cells_path)
    except OSError as e:
        raise AuditScopeError(f"data/cells.jsonl digest 계산 실패: {e}") from e
    return {"workbook_sha256": workbook_sha, "cells_sha256": cells_sha}


def build_scope_commit(
    pkg: Path | str,
    scope: AuditScope,
    facts: dict,
    standards: dict,
    brief: dict,
    *,
    version: str = PREPARE_VERSION,
    prepared_at: str | None = None,
) -> dict:
    """Build, then fully validate, the final marker for a sheet bundle."""
    path = Path(pkg)
    selected = resolve_scope(path, scope=scope)
    if selected.kind != "sheet":
        raise AuditScopeError("scope commit marker는 sheet scope에만 사용합니다.")
    keys = scope_bundle_keys(selected, facts, standards, brief)
    document = {
        "schema_version": SCOPE_COMMIT_SCHEMA_VERSION,
        "scope": selected.identity(),
        "inputs": _current_inputs(path),
        "present": True,
        "status": brief.get("readiness", {}).get("status"),
        "version": version,
        "facts_key": keys[0],
        "standards_key": keys[1],
        "brief_key": keys[2],
        "prepared_at": prepared_at or _now_iso(),
        "review_status": brief.get("review", {}).get("status"),
    }
    validate_scope_commit(path, selected, document, facts, standards, brief)
    return document


def validate_scope_commit(
    pkg: Path | str,
    scope: AuditScope,
    commit: object,
    facts: object,
    standards: object,
    brief: object,
) -> None:
    """Validate schema, provenance boundary, input digests, and exact artifact keys."""
    path = Path(pkg)
    selected = resolve_scope(path, scope=scope)
    if selected.kind != "sheet":
        raise AuditScopeError("sheet scope commit만 검증할 수 있습니다.")
    schema = _read_json_object(
        SCHEMA_DIR / "audit_scope_commit.schema.json",
        label="audit scope commit schema",
    )
    validator = Draft7Validator(schema)
    errors = sorted(
        validator.iter_errors(commit),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise AuditScopeError(f"audit scope commit schema 불일치({location}): {first.message}")
    assert isinstance(commit, dict)
    if commit.get("scope") != selected.identity():
        raise AuditScopeError("commit.scope가 요청한 시트 scope와 일치하지 않습니다.")
    if commit.get("inputs") != _current_inputs(path):
        raise AuditScopeError("commit.inputs가 현재 workbook/cells digest와 일치하지 않습니다.")
    if commit.get("version") != PREPARE_VERSION:
        raise AuditScopeError("scope commit version이 현재 prepare 계약과 일치하지 않습니다.")
    if not all(isinstance(document, dict) for document in (facts, standards, brief)):
        raise AuditScopeError("scope bundle artifact는 모두 JSON 객체여야 합니다.")
    try:
        validate_audit_bundle(path, facts, standards, brief)
    except AuditValidationError as e:
        detail = "; ".join(e.problems[:5])
        raise AuditScopeError(f"scope audit bundle 검증 실패: {detail}") from e
    wrong_sources = sorted({
        source.get("sheet")
        for source in facts.get("sources", [])
        if isinstance(source, dict) and source.get("sheet") != selected.sheet
    }, key=repr)
    if wrong_sources:
        raise AuditScopeError(
            f"sheet scope 직접 근거가 다른 시트를 포함합니다: {wrong_sources}"
        )
    expected_keys = scope_bundle_keys(selected, facts, standards, brief)
    advertised_keys = tuple(
        commit.get(name) for name in ("facts_key", "standards_key", "brief_key")
    )
    if advertised_keys != expected_keys:
        raise AuditScopeError("scope commit artifact key가 일치하지 않습니다.")
    if commit.get("status") != brief.get("readiness", {}).get("status"):
        raise AuditScopeError("scope commit status가 brief readiness와 일치하지 않습니다.")
    if commit.get("review_status") != brief.get("review", {}).get("status"):
        raise AuditScopeError("scope commit review_status가 brief review와 일치하지 않습니다.")


def write_scope_commit_atomic(
    pkg: Path | str,
    scope: AuditScope,
    document: dict,
) -> Path:
    """Atomically replace a prevalidated sheet commit marker and fsync its directory."""
    paths = bundle_paths(pkg, scope)
    if paths.scope.kind != "sheet":
        raise AuditScopeError("scope commit marker는 sheet scope에만 쓸 수 있습니다.")
    facts, standards, brief = (
        _read_json_object(artifact, label=str(artifact.relative_to(paths.package)))
        for artifact in paths.artifacts
    )
    validate_scope_commit(
        paths.package, paths.scope, document, facts, standards, brief
    )
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".commit.", suffix=".tmp", dir=paths.data_dir
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(document, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, paths.commit)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(paths.data_dir, flags)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)
    return paths.commit


def read_scope_commit(
    pkg: Path | str,
    scope: AuditScope,
    *,
    allow_absent: bool = False,
) -> dict | None:
    """Read one sheet marker; artifact validation is performed by ``load_scope_bundle``."""
    paths = bundle_paths(pkg, scope)
    if paths.scope.kind != "sheet":
        raise AuditScopeError("scope commit marker는 sheet scope에만 존재합니다.")
    if allow_absent and not paths.commit.is_file():
        return None
    return _read_json_object(paths.commit, label="audit scope commit.json")


def load_scope_bundle(
    pkg: Path | str,
    scope: AuditScope,
    *,
    allow_absent: bool = False,
) -> tuple[BundlePaths, dict, dict, dict, dict] | None:
    """Load a sheet bundle only after its final marker and all bindings validate."""
    paths = bundle_paths(pkg, scope)
    commit = read_scope_commit(pkg, paths.scope, allow_absent=allow_absent)
    if commit is None:
        return None
    facts, standards, brief = (
        _read_json_object(artifact, label=str(artifact.relative_to(paths.package)))
        for artifact in paths.artifacts
    )
    validate_scope_commit(
        paths.package, paths.scope, commit, facts, standards, brief
    )
    return paths, facts, standards, brief, commit


def _scope_state(pkg: Path, scope: AuditScope) -> dict:
    """Return a fail-closed, non-throwing status view for the planning command."""
    try:
        if scope.kind == "sheet":
            paths = bundle_paths(pkg, scope)
            if not paths.commit.is_file():
                return {
                    "state": "not_prepared",
                    "prepared": False,
                    "readiness": None,
                    "review_status": None,
                }
            loaded = load_scope_bundle(pkg, scope)
            assert loaded is not None
            brief = loaded[3]
        else:
            meta = _read_json_object(pkg / "meta.json", label="meta.json")
            marker = meta.get("audit_preparation")
            paths = bundle_paths(pkg, scope)
            if not isinstance(marker, dict) or marker.get("present") is not True:
                return {
                    "state": "not_prepared",
                    "prepared": False,
                    "readiness": None,
                    "review_status": None,
                }
            facts, standards, brief = (
                _read_json_object(
                    artifact, label=str(artifact.relative_to(paths.package))
                )
                for artifact in paths.artifacts
            )
            validate_audit_bundle(pkg, facts, standards, brief)
            expected = scope_bundle_keys(scope, facts, standards, brief)
            advertised = tuple(
                marker.get(name)
                for name in ("facts_key", "standards_key", "brief_key")
            )
            if marker.get("version") != PREPARE_VERSION or advertised != expected:
                raise AuditScopeError("workbook audit commit marker가 현재 bundle과 다릅니다.")
            if marker.get("status") != brief.get("readiness", {}).get("status"):
                raise AuditScopeError("workbook audit readiness marker가 다릅니다.")
            if marker.get("review_status") != brief.get("review", {}).get("status"):
                raise AuditScopeError("workbook audit review marker가 다릅니다.")
        return {
            "state": "prepared",
            "prepared": True,
            "readiness": brief.get("readiness", {}).get("status"),
            "review_status": brief.get("review", {}).get("status"),
        }
    except Exception as e:  # planning must describe a damaged optional scope, not abort all rows
        return {
            "state": "invalid",
            "prepared": False,
            "readiness": None,
            "review_status": None,
            "error": str(e),
        }


def audit_scopes_plan(pkg: Path | str) -> dict:
    """Return a deterministic, read-only workbook/sheet LLM workload plan."""
    path = Path(pkg)
    meta = _read_json_object(path / "meta.json", label="meta.json")
    names = _meta_sheet_names(path)
    sheet_meta = {
        item["name"]: item
        for item in meta.get("sheets", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    regions = build_regions(path)
    by_sheet: dict[str, list] = {name: [] for name in names}
    for region in regions:
        by_sheet.setdefault(region.sheet, []).append(region)

    sheet_plans: list[dict] = []
    for name in names:
        selected = AuditScope.for_sheet(name)
        selected_regions = by_sheet.get(name, [])
        cell_count = sum(len(region.cells) for region in selected_regions)
        region_count = len(selected_regions)
        analyzable = region_count > 0
        calls = {
            "facts": region_count + 1 if analyzable else 0,
            "brief": 1 if analyzable else 0,
            "total_llm": region_count + 2 if analyzable else 0,
        }
        item = {
            "scope": selected.identity(),
            "dimensions": sheet_meta.get(name, {}).get("dimensions"),
            "cell_count": cell_count,
            "region_count": region_count,
            "analyzable": analyzable,
            "dependency_sheets": list(dependency_sheets(path, name)),
            "estimated_calls": calls,
        }
        item.update(_scope_state(path, selected))
        sheet_plans.append(item)
    total_cells = sum(item["cell_count"] for item in sheet_plans)
    total_regions = len(regions)
    analyzable_sheets = [item for item in sheet_plans if item["analyzable"]]
    workbook_analyzable = total_regions > 0
    workbook = {
        "scope": WORKBOOK_SCOPE.identity(),
        "sheet_count": len(names),
        "cell_count": total_cells,
        "region_count": total_regions,
        "analyzable": workbook_analyzable,
        "estimated_calls": {
            "facts": total_regions + 1,
            "brief": 1,
            "total_llm": total_regions + 2,
        },
    }
    workbook.update(_scope_state(path, WORKBOOK_SCOPE))
    return {
        "schema_version": "audit_scope_plan.v1",
        "workbook": workbook,
        "all_sheets": {
            "sheet_count": len(analyzable_sheets),
            "total_sheet_count": len(names),
            "skipped_empty_sheet_count": len(names) - len(analyzable_sheets),
            "cell_count": sum(item["cell_count"] for item in analyzable_sheets),
            "region_count": sum(item["region_count"] for item in analyzable_sheets),
            "estimated_calls": {
                "facts": sum(
                    item["estimated_calls"]["facts"] for item in analyzable_sheets
                ),
                "brief": len(analyzable_sheets),
                "total_llm": sum(
                    item["estimated_calls"]["total_llm"]
                    for item in analyzable_sheets
                ),
            },
        },
        "sheets": sheet_plans,
    }


__all__ = [
    "AuditScope",
    "AuditScopeError",
    "BundlePaths",
    "SCOPE_COMMIT_SCHEMA_VERSION",
    "WORKBOOK_SCOPE",
    "audit_scopes_plan",
    "build_scope_commit",
    "bundle_paths",
    "dependency_sheets",
    "load_scope_bundle",
    "read_scope_commit",
    "resolve_scope",
    "sheet_model_context",
    "scope_bundle_keys",
    "validate_scope_commit",
    "write_scope_commit_atomic",
]
