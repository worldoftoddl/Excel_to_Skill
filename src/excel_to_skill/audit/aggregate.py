"""Account-level briefing over independently committed worksheet audit scopes.

The aggregator never directly sends the workbook ledger, workbook sources, full audit facts, or
standards-context passage fields to its model.  A committed brief sentence may itself quote or
summarize those sources.  The code builds a bounded whitelist dossier from committed sheet briefs, lets
the model select opaque scope-qualified record references, and materializes every selected value
back from the already validated in-memory bundles.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft7Validator

from .. import cache
from ..meta import _now_iso
from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import json_sha256
from .prepare import _atomic_write_text, _restore_files, _snapshot_files
from .scope import (
    AuditScope,
    AuditScopeError,
    bundle_paths,
    load_scope_bundle,
    resolve_scope,
)


AGGREGATE_VERSION = "0.1.0"
AGGREGATE_PROMPT = "audit_aggregate_v1.md"
AGGREGATE_SCHEMA = "audit_account_brief.schema.json"
AGGREGATE_COMMIT_SCHEMA = "audit_account_brief_commit.schema.json"
AGGREGATE_SCHEMA_VERSION = "audit_account_brief.v1"
AGGREGATE_COMMIT_SCHEMA_VERSION = "audit_account_brief_commit.v1"
MAX_MODEL_CONTEXT_BYTES = 600_000
_RETRY_MESSAGE_RESERVE_BYTES = 2_048
MAX_SELECTED_SCOPES = 64
MAX_SCOPE_HIGHLIGHTS = 12
MAX_SCOPE_ATTENTION = 12
MAX_PORTFOLIO_RECORDS = 24
MAX_MODEL_SCOPE_HIGHLIGHTS = 4
MAX_MODEL_SCOPE_ATTENTION = 4
MAX_MODEL_PORTFOLIO_RECORDS = 12
_AGGREGATE_ROOT = Path("data/audit_aggregates")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SEVERITY_ORDER = {"high": 0, "moderate": 1, "low": 2}


class AuditAggregateError(RuntimeError):
    """The requested aggregate cannot be built from a complete trusted snapshot."""


class AuditAggregateStaleError(AuditAggregateError):
    """A valid published aggregate no longer matches its selected source snapshot."""


@dataclass(frozen=True, slots=True)
class AggregatePaths:
    package: Path
    aggregate_id: str
    data_dir: Path
    brief: Path
    commit: Path


@dataclass(frozen=True, slots=True)
class AggregateResult:
    package: Path
    paths: AggregatePaths
    document: dict
    cached: bool


@dataclass(frozen=True, slots=True)
class _ScopeSnapshot:
    scope: AuditScope
    facts: dict
    standards: dict
    brief: dict
    commit: dict
    binding: dict
    file_digests: tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class _Capture:
    package: Path
    mode: str
    workbook_sheets: tuple[str, ...]
    committed_sheets: tuple[str, ...]
    snapshots: tuple[_ScopeSnapshot, ...]
    input_state: dict
    dependencies: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _Dossier:
    payload: dict
    serialized: str
    context_bytes: int
    registry: dict[str, dict]
    highlight_refs: dict[str, tuple[str, ...]]
    attention_refs: dict[str, tuple[str, ...]]


def _read_json_object(path: Path, *, label: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AuditAggregateError(f"{label} 없음: {path}") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AuditAggregateError(f"{label} 읽기 실패: {e}") from e
    if not isinstance(document, dict):
        raise AuditAggregateError(f"{label}은 JSON 객체여야 합니다.")
    return document


def _schema_problems(document: object, schema_name: str) -> list[str]:
    schema = load_schema(schema_name)
    errors = sorted(
        Draft7Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    problems: list[str] = []
    for error in errors:
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        problems.append(f"{location or '/'}: {error.message}")
    return problems


def _aggregate_input_problems(document: object) -> list[str]:
    """Validate commit inputs with the exact artifact input definition before routing."""
    artifact_schema = load_schema(AGGREGATE_SCHEMA)
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$ref": "#/definitions/inputs",
        "definitions": artifact_schema["definitions"],
    }
    errors = sorted(
        Draft7Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        "/" + "/".join(str(part) for part in error.absolute_path)
        + f": {error.message}"
        for error in errors
    ]


def _meta_sheet_names(pkg: Path) -> tuple[str, ...]:
    meta = _read_json_object(pkg / "meta.json", label="meta.json")
    sheets = meta.get("sheets")
    if not isinstance(sheets, list):
        raise AuditAggregateError("meta.sheets는 배열이어야 합니다.")
    names: list[str] = []
    for index, item in enumerate(sheets):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise AuditAggregateError(f"meta.sheets[{index}].name이 유효하지 않습니다.")
        names.append(item["name"])
    if not names:
        raise AuditAggregateError("meta.sheets가 비어 있습니다.")
    if len(names) != len(set(names)):
        raise AuditAggregateError("meta.sheets에 중복 시트명이 있습니다.")
    return tuple(names)


def _current_inputs(pkg: Path) -> dict:
    meta = _read_json_object(pkg / "meta.json", label="meta.json")
    source = meta.get("source")
    workbook_sha = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(workbook_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", workbook_sha):
        raise AuditAggregateError("meta.source.sha256이 유효하지 않습니다.")
    sheets = meta.get("sheets")
    if not isinstance(sheets, list):
        raise AuditAggregateError("meta.sheets가 유효하지 않습니다.")
    try:
        cells_sha = cache.file_sha256(pkg / "data/cells.jsonl")
        references_sha = cache.file_sha256(pkg / "data/references.json")
    except OSError as e:
        raise AuditAggregateError(f"deterministic input digest 계산 실패: {e}") from e
    return {
        "workbook_sha256": workbook_sha,
        "cells_sha256": cells_sha,
        "sheet_manifest_sha256": json_sha256(sheets),
        "references_sha256": references_sha,
    }


def _scope_binding(
    scope: AuditScope,
    facts: Mapping[str, object],
    brief: Mapping[str, object],
    commit: Mapping[str, object],
) -> dict:
    facts_review = facts.get("review")
    brief_review = brief.get("review")
    return {
        "scope": scope.identity(),
        "commit_sha256": json_sha256(commit),
        "facts_key": commit.get("facts_key"),
        "standards_key": commit.get("standards_key"),
        "brief_key": commit.get("brief_key"),
        "prepared_at": commit.get("prepared_at"),
        "readiness_status": commit.get("status"),
        "facts_review_status": (
            facts_review.get("status") if isinstance(facts_review, Mapping) else None
        ),
        "brief_review_status": (
            brief_review.get("status") if isinstance(brief_review, Mapping) else None
        ),
    }


def _bundle_file_digests(paths) -> tuple[str, str, str, str]:
    try:
        values = tuple(
            cache.file_sha256(path)
            for path in (paths.commit, *paths.artifacts)
        )
    except OSError as e:
        raise AuditAggregateError(f"scope bundle file digest 계산 실패: {e}") from e
    assert len(values) == 4
    return values[0], values[1], values[2], values[3]


def _load_snapshot(pkg: Path, sheet: str) -> _ScopeSnapshot:
    try:
        scope = resolve_scope(pkg, sheet=sheet)
        paths = bundle_paths(pkg, scope)
        before = _bundle_file_digests(paths)
        loaded = load_scope_bundle(pkg, scope)
    except (AuditScopeError, ValueError) as e:
        raise AuditAggregateError(f"시트 {sheet!r} commit 검증 실패: {e}") from e
    assert loaded is not None
    _, facts, standards, brief, commit = loaded
    after = _bundle_file_digests(paths)
    if before != after:
        raise AuditAggregateError(
            f"시트 {sheet!r} bundle이 snapshot 검증 중 변경되었습니다."
        )
    binding = _scope_binding(scope, facts, brief, commit)
    rejected = {
        binding["facts_review_status"], binding["brief_review_status"]
    } & {"rejected"}
    if rejected:
        raise AuditAggregateError(
            f"시트 {sheet!r}는 반려된 audit scope이므로 aggregate할 수 없습니다."
        )
    if binding["readiness_status"] == "not_ready":
        raise AuditAggregateError(
            f"시트 {sheet!r}의 audit brief가 not_ready이므로 aggregate할 수 없습니다."
        )
    return _ScopeSnapshot(
        scope,
        facts,
        standards,
        brief,
        commit,
        binding,
        after,
    )


def _marker_sheets(pkg: Path, names: Sequence[str]) -> tuple[str, ...]:
    marked: list[str] = []
    for name in names:
        paths = bundle_paths(pkg, AuditScope.for_sheet(name))
        if paths.commit.is_file():
            marked.append(name)
    return tuple(marked)


def _address_sheet(address: object) -> str | None:
    if not isinstance(address, str) or "!" not in address:
        return None
    sheet, _ = address.rsplit("!", 1)
    return sheet or None


def _dependency_map(
    pkg: Path,
    names: Sequence[str],
    *,
    expected_sha256: str,
) -> dict[str, tuple[str, ...]]:
    path = pkg / "data/references.json"
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AuditAggregateError(f"data/references.json 읽기 실패: {e}") from e
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AuditAggregateError(
            "aggregate snapshot 중 data/references.json이 변경되었습니다."
        )
    edges = document.get("edges") if isinstance(document, Mapping) else None
    if not isinstance(edges, list):
        raise AuditAggregateError("data/references.json edges는 배열이어야 합니다.")
    known = set(names)
    found: dict[str, set[str]] = {name: set() for name in names}
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise AuditAggregateError("data/references.json edge는 객체여야 합니다.")
        source = _address_sheet(edge.get("from"))
        target = _address_sheet(edge.get("to"))
        if source in known and target not in {None, source}:
            if target not in known:
                raise AuditAggregateError(
                    f"references edge가 meta에 없는 내부 시트를 가리킵니다: {target!r}"
                )
            found[source].add(target)
    return {
        name: tuple(candidate for candidate in names if candidate in found[name])
        for name in names
    }


def _capture_sources(
    pkg: Path | str,
    *,
    sheets: Sequence[str] | None = None,
    all_committed_sheets: bool = False,
) -> _Capture:
    path = Path(pkg)
    if not path.is_dir() or not (path / "meta.json").is_file():
        raise AuditAggregateError(f"패키지 meta.json이 없습니다: {path}")
    if bool(sheets) == bool(all_committed_sheets):
        raise AuditAggregateError(
            "sheets 또는 all_committed_sheets 중 정확히 하나를 선택해야 합니다."
        )
    input_state = _current_inputs(path)
    names = _meta_sheet_names(path)
    committed = _marker_sheets(path, names)
    if all_committed_sheets:
        # A scope becomes authoritative only when commit.json exists.  Artifact-only staging from
        # an interrupted or in-progress prepare is deliberately invisible, just like the sheet
        # consumer/verify gate; it remains counted as unprepared coverage.
        selected_names = list(committed)
        if not selected_names:
            raise AuditAggregateError("commit된 시트 audit scope가 없습니다.")
        mode = "all_committed_sheets"
    else:
        requested = list(sheets or [])
        if any(not isinstance(name, str) or not name for name in requested):
            raise AuditAggregateError("--sheet에는 비어 있지 않은 정확한 시트명이 필요합니다.")
        unknown = sorted(set(requested) - set(names))
        if unknown:
            raise AuditAggregateError(f"meta.json에 없는 시트입니다: {unknown}")
        requested_set = set(requested)
        selected_names = [name for name in names if name in requested_set]
        mode = "explicit_sheets"
    if len(selected_names) > MAX_SELECTED_SCOPES:
        raise AuditAggregateError(
            f"aggregate 시트는 최대 {MAX_SELECTED_SCOPES}개입니다: {len(selected_names)}"
        )
    snapshots = tuple(_load_snapshot(path, name) for name in selected_names)
    dependencies = _dependency_map(
        path,
        names,
        expected_sha256=input_state["references_sha256"],
    )
    capture = _Capture(
        path, mode, names, committed, snapshots, input_state, dependencies
    )
    _assert_sources_unchanged(capture)
    return capture


def _assert_sources_unchanged(capture: _Capture) -> None:
    if _current_inputs(capture.package) != capture.input_state:
        raise AuditAggregateError(
            "aggregate 처리 중 workbook/cells/sheet manifest/references가 변경되었습니다."
        )
    # Coverage reports the current committed-sheet count and omitted sheet names in both modes.
    # Recheck the marker set even for an explicit selection so a newly committed sibling cannot
    # make ``complete_over_committed_sheets=true`` stale while the model call is in flight.
    current = _marker_sheets(capture.package, capture.workbook_sheets)
    if current != capture.committed_sheets:
        raise AuditAggregateError(
            "aggregate 처리 중 commit된 시트 scope 집합이 변경되었습니다."
        )
    for snapshot in capture.snapshots:
        paths = bundle_paths(capture.package, snapshot.scope)
        current_digests = _bundle_file_digests(paths)
        if current_digests != snapshot.file_digests:
            raise AuditAggregateError(
                f"aggregate 처리 중 시트 {snapshot.scope.sheet!r} bundle이 변경되었습니다."
            )


def _aggregate_inputs(capture: _Capture) -> dict:
    bindings = [copy.deepcopy(snapshot.binding) for snapshot in capture.snapshots]
    selection = {
        "mode": capture.mode,
        "scope_ids": [snapshot.scope.id for snapshot in capture.snapshots],
    }
    return {
        **copy.deepcopy(capture.input_state),
        "selection": selection,
        "committed_scope_manifest_sha256": json_sha256({
            "sheets": list(capture.committed_sheets)
        }),
        "source_manifest_sha256": json_sha256(bindings),
        "scopes": bindings,
    }


def aggregate_paths(capture: _Capture) -> AggregatePaths:
    identity = {"mode": capture.mode}
    if capture.mode == "explicit_sheets":
        identity["scope_ids"] = [
            snapshot.scope.id for snapshot in capture.snapshots
        ]
    aggregate_id = json_sha256(identity)
    data_dir = capture.package / _AGGREGATE_ROOT / aggregate_id
    return AggregatePaths(
        package=capture.package,
        aggregate_id=aggregate_id,
        data_dir=data_dir,
        brief=data_dir / "account_brief.json",
        commit=data_dir / "commit.json",
    )


def _bounded_model_text(value: object, *, maximum: int = 4_000) -> str:
    text = _CONTROL_CHARS.sub("", str(value or ""))
    text = " ".join(text.split())
    if len(text) > maximum:
        return text[: maximum - 1].rstrip() + "…"
    return text


def _record_ref(scope: AuditScope, kind: str, local_key: str) -> str:
    return "record:" + json_sha256({
        "scope_id": scope.id,
        "kind": kind,
        "local_key": local_key,
    })


def _safe_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _statement_record(scope: AuditScope, statement: Mapping[str, object]) -> dict:
    source_id = statement.get("id")
    if not isinstance(source_id, str):
        raise AuditAggregateError("audit_brief statement id가 유효하지 않습니다.")
    ref = _record_ref(scope, "statement", source_id)
    return {
        "record_ref": ref,
        "kind": "statement",
        "scope": scope.identity(),
        "source_id": source_id,
        "text": statement.get("text"),
        "section": statement.get("section"),
        "type": statement.get("type"),
        "status": statement.get("status"),
        "confidence": statement.get("confidence"),
        "severity": None,
        "fact_ids": _safe_string_list(statement.get("fact_ids")),
        "relation_ids": _safe_string_list(statement.get("relation_ids")),
        "standard_citation_ids": _safe_string_list(
            statement.get("standard_citation_ids")
        ),
    }


def _limitation_record(scope: AuditScope, limitation: Mapping[str, object]) -> dict:
    source_id = limitation.get("id")
    if not isinstance(source_id, str):
        raise AuditAggregateError("audit_brief limitation id가 유효하지 않습니다.")
    return {
        "record_ref": _record_ref(scope, "limitation", source_id),
        "kind": "limitation",
        "scope": scope.identity(),
        "source_id": source_id,
        "text": limitation.get("description"),
        "section": "limitations",
        "type": "limitation",
        "status": "unresolved",
        "confidence": None,
        "severity": limitation.get("severity"),
        "fact_ids": [],
        "relation_ids": [],
        "standard_citation_ids": [],
    }


def _reason_record(scope: AuditScope, index: int, reason: str) -> dict:
    local_key = f"{index}:{json_sha256(reason)}"
    return {
        "record_ref": _record_ref(scope, "readiness_reason", local_key),
        "kind": "readiness_reason",
        "scope": scope.identity(),
        "source_id": None,
        "text": reason,
        "section": "readiness",
        "type": "readiness_reason",
        "status": "unresolved",
        "confidence": None,
        "severity": None,
        "fact_ids": [],
        "relation_ids": [],
        "standard_citation_ids": [],
    }


def _register_record(registry: dict[str, dict], record: dict) -> None:
    ref = record["record_ref"]
    previous = registry.get(ref)
    if previous is not None and previous != record:
        raise AuditAggregateError(f"aggregate record_ref 충돌: {ref}")
    registry[ref] = record


def _account_label(snapshot: _ScopeSnapshot) -> tuple[str, str, str | None]:
    for fact in snapshot.facts.get("facts", []):
        if not isinstance(fact, Mapping) or fact.get("type") != "account":
            continue
        description = fact.get("description")
        if isinstance(description, str) and description.strip():
            fact_id = fact.get("id")
            return (
                " ".join(description.split()),
                "account_fact",
                fact_id if isinstance(fact_id, str) else None,
            )
    workpaper = snapshot.brief.get("workpaper")
    title = workpaper.get("title") if isinstance(workpaper, Mapping) else None
    if isinstance(title, str) and title.strip():
        return " ".join(title.split()), "workpaper_title", None
    assert snapshot.scope.sheet is not None
    return snapshot.scope.sheet, "sheet_name", None


def _workpaper(snapshot: _ScopeSnapshot) -> dict:
    source = snapshot.brief.get("workpaper")
    if not isinstance(source, Mapping):
        raise AuditAggregateError("audit_brief.workpaper가 유효하지 않습니다.")
    return {
        key: source.get(key)
        for key in (
            "kind", "title", "entity", "period_start", "period_end",
            "audit_phase", "document_state", "purpose", "fact_ids",
        )
    }


def _counts(snapshot: _ScopeSnapshot) -> dict:
    facts = [item for item in snapshot.facts.get("facts", []) if isinstance(item, Mapping)]
    relations = [
        item for item in snapshot.facts.get("relations", []) if isinstance(item, Mapping)
    ]
    statements = [
        item for item in snapshot.brief.get("statements", []) if isinstance(item, Mapping)
    ]
    limitations = [
        item for item in snapshot.brief.get("limitations", []) if isinstance(item, Mapping)
    ]
    fact_types = Counter(item.get("type") for item in facts)
    return {
        "facts": len(facts),
        "relations": len(relations),
        "statements": len(statements),
        "limitations": len(limitations),
        "standards_citations": len(snapshot.standards.get("citations", [])),
        "assertions": fact_types["assertion"],
        "procedures": fact_types["procedure"],
        "results": fact_types["result"],
        "findings": fact_types["finding"],
        "tests_relations": sum(item.get("type") == "tests" for item in relations),
    }


def _candidate_view(record: Mapping[str, object]) -> dict:
    return {
        "record_ref": record["record_ref"],
        "kind": record["kind"],
        "section": record["section"],
        "type": record["type"],
        "status": record["status"],
        "confidence": record["confidence"],
        "severity": record["severity"],
        "text": _bounded_model_text(record["text"]),
    }


def _serialize_model_payload(payload: dict, *, enforce: bool = True) -> tuple[str, int]:
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    size = len(serialized.encode("utf-8"))
    if enforce and size > MAX_MODEL_CONTEXT_BYTES:
        raise AuditAggregateError(
            "aggregate model context가 600KB 상한을 초과했습니다: "
            f"{size} bytes. --sheet로 범위를 줄이세요."
        )
    return serialized, size


def _build_dossier(capture: _Capture, *, enforce_limit: bool = True) -> _Dossier:
    registry: dict[str, dict] = {}
    highlight_refs: dict[str, tuple[str, ...]] = {}
    attention_refs: dict[str, tuple[str, ...]] = {}
    scope_payloads: list[dict] = []
    for snapshot in capture.snapshots:
        statements = [
            item for item in snapshot.brief.get("statements", [])
            if isinstance(item, Mapping)
        ]
        statement_records = [_statement_record(snapshot.scope, item) for item in statements]
        for record in statement_records:
            _register_record(registry, record)
        by_source_id = {record["source_id"]: record for record in statement_records}
        summary = snapshot.brief.get("summary")
        summary_ids = (
            _safe_string_list(summary.get("statement_ids"))
            if isinstance(summary, Mapping) else []
        )
        ordered_statements: list[dict] = []
        for source_id in summary_ids:
            record = by_source_id.get(source_id)
            if record is not None and record not in ordered_statements:
                ordered_statements.append(record)
        for record in statement_records:
            if record not in ordered_statements:
                ordered_statements.append(record)
        highlights = ordered_statements[:MAX_SCOPE_HIGHLIGHTS]

        gap_records = [
            record for record in statement_records if record.get("type") == "gap"
        ]
        attention: list[dict] = []
        limitations = [
            item for item in snapshot.brief.get("limitations", [])
            if isinstance(item, Mapping)
        ]
        limitations = sorted(
            enumerate(limitations),
            key=lambda pair: (
                _SEVERITY_ORDER.get(str(pair[1].get("severity")), 99), pair[0]
            ),
        )
        limitation_records: list[dict] = []
        for _, limitation in limitations:
            record = _limitation_record(snapshot.scope, limitation)
            _register_record(registry, record)
            limitation_records.append(record)
        readiness = snapshot.brief.get("readiness")
        reasons = (
            _safe_string_list(readiness.get("reasons"))
            if isinstance(readiness, Mapping) else []
        )
        reason_records: list[dict] = []
        for index, reason in enumerate(reasons):
            record = _reason_record(snapshot.scope, index, reason)
            _register_record(registry, record)
            reason_records.append(record)
        # High/moderate limitations cannot be crowded out by numerous gap statements.  Gaps and
        # readiness reasons follow, then low/unknown limitations.  Any remaining truncation is
        # surfaced deterministically in aggregate coverage/readiness below.
        attention.extend(
            record for record in limitation_records
            if record.get("severity") in {"high", "moderate"}
        )
        attention.extend(gap_records)
        attention.extend(reason_records)
        attention.extend(
            record for record in limitation_records
            if record.get("severity") not in {"high", "moderate"}
        )
        deduped_attention: list[dict] = []
        for record in attention:
            if record not in deduped_attention:
                deduped_attention.append(record)
        attention = deduped_attention[:MAX_SCOPE_ATTENTION]
        scope_id = snapshot.scope.id
        highlight_refs[scope_id] = tuple(record["record_ref"] for record in highlights)
        attention_refs[scope_id] = tuple(record["record_ref"] for record in attention)
        label, _, _ = _account_label(snapshot)
        workpaper = _workpaper(snapshot)
        counts = _counts(snapshot)
        summary_text = summary.get("text") if isinstance(summary, Mapping) else ""
        scope_payloads.append({
            "scope_id": scope_id,
            "sheet": snapshot.scope.sheet,
            "account_label": _bounded_model_text(label, maximum=1_000),
            "workpaper": {
                "kind": workpaper["kind"],
                "title": _bounded_model_text(workpaper["title"], maximum=1_000),
                "purpose": _bounded_model_text(workpaper["purpose"]),
            },
            "source_state": {
                "readiness_status": snapshot.binding["readiness_status"],
                "facts_review_status": snapshot.binding["facts_review_status"],
                "brief_review_status": snapshot.binding["brief_review_status"],
            },
            "metrics": {
                "fact_count": counts["facts"],
                "relation_count": counts["relations"],
                "statement_count": counts["statements"],
                "limitation_count": counts["limitations"],
                "standards_citation_count": counts["standards_citations"],
                "assertion_count": counts["assertions"],
                "procedure_count": counts["procedures"],
                "result_count": counts["results"],
                "finding_count": counts["findings"],
                "tests_relation_count": counts["tests_relations"],
            },
            "source_summary": _bounded_model_text(summary_text, maximum=5_000),
            "highlight_candidates": [_candidate_view(record) for record in highlights],
            "attention_candidates": [_candidate_view(record) for record in attention],
        })
    if not registry:
        raise AuditAggregateError("선택된 시트 brief에 aggregate 후보 record가 없습니다.")
    payload = {
        "schema_version": "audit_aggregate_dossier.v1",
        "selection": {
            "mode": capture.mode,
            "scope_ids": [snapshot.scope.id for snapshot in capture.snapshots],
        },
        "scopes": scope_payloads,
    }
    serialized, size = _serialize_model_payload(payload, enforce=enforce_limit)
    return _Dossier(
        payload, serialized, size, registry, highlight_refs, attention_refs
    )


def _model_plan_schema(scope_count: int) -> dict:
    record_refs = {
        "type": "array",
        "items": {"type": "string", "pattern": "^record:[0-9a-f]{64}$"},
        "uniqueItems": True,
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "scope_selections",
            "portfolio_highlight_record_refs",
            "portfolio_attention_record_refs",
        ],
        "properties": {
            "scope_selections": {
                "type": "array",
                "minItems": scope_count,
                "maxItems": scope_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scope_id", "highlight_record_refs", "attention_record_refs"
                    ],
                    "properties": {
                        "scope_id": {
                            "type": "string", "pattern": "^[0-9a-f]{64}$"
                        },
                        "highlight_record_refs": {
                            **copy.deepcopy(record_refs),
                            "maxItems": MAX_MODEL_SCOPE_HIGHLIGHTS,
                        },
                        "attention_record_refs": {
                            **copy.deepcopy(record_refs),
                            "maxItems": MAX_MODEL_SCOPE_ATTENTION,
                        },
                    },
                },
            },
            "portfolio_highlight_record_refs": {
                **copy.deepcopy(record_refs),
                "maxItems": MAX_MODEL_PORTFOLIO_RECORDS,
            },
            "portfolio_attention_record_refs": {
                **copy.deepcopy(record_refs),
                "maxItems": MAX_MODEL_PORTFOLIO_RECORDS,
            },
        },
    }


def _validate_model_plan(document: dict, capture: _Capture, dossier: _Dossier) -> None:
    selections = document.get("scope_selections")
    if not isinstance(selections, list):
        raise AuditLLMError("scope_selections가 배열이 아닙니다.")
    scope_ids = [snapshot.scope.id for snapshot in capture.snapshots]
    returned = [
        item.get("scope_id") if isinstance(item, Mapping) else None for item in selections
    ]
    if len(returned) != len(set(returned)) or set(returned) != set(scope_ids):
        raise AuditLLMError("scope_selections가 입력 scope 집합과 일치하지 않습니다.")
    for item in selections:
        assert isinstance(item, Mapping)
        scope_id = item["scope_id"]
        highlights = item["highlight_record_refs"]
        attention = item["attention_record_refs"]
        allowed_highlights = set(dossier.highlight_refs[scope_id])
        allowed_attention = set(dossier.attention_refs[scope_id])
        if not set(highlights) <= allowed_highlights:
            raise AuditLLMError(f"scope {scope_id}의 관찰하지 않은 highlight ref입니다.")
        if not set(attention) <= allowed_attention:
            raise AuditLLMError(f"scope {scope_id}의 관찰하지 않은 attention ref입니다.")
        if allowed_highlights and not highlights:
            raise AuditLLMError(f"scope {scope_id}의 highlight 선택이 비어 있습니다.")
        if allowed_attention and not attention:
            raise AuditLLMError(f"scope {scope_id}의 attention 선택이 비어 있습니다.")
    all_highlights = {
        ref for refs in dossier.highlight_refs.values() for ref in refs
    }
    all_attention = {
        ref for refs in dossier.attention_refs.values() for ref in refs
    }
    portfolio_highlights = document["portfolio_highlight_record_refs"]
    portfolio_attention = document["portfolio_attention_record_refs"]
    if not set(portfolio_highlights) <= all_highlights:
        raise AuditLLMError("관찰하지 않은 portfolio highlight ref입니다.")
    if not set(portfolio_attention) <= all_attention:
        raise AuditLLMError("관찰하지 않은 portfolio attention ref입니다.")
    if all_highlights and not portfolio_highlights:
        raise AuditLLMError("portfolio highlight 선택이 비어 있습니다.")
    if all_attention and not portfolio_attention:
        raise AuditLLMError("portfolio attention 선택이 비어 있습니다.")


def _materialize(refs: Sequence[str], registry: Mapping[str, dict]) -> list[dict]:
    return [copy.deepcopy(registry[ref]) for ref in refs]


def _dedupe_refs(refs: Sequence[str], *, maximum: int) -> list[str]:
    result: list[str] = []
    for ref in refs:
        if ref not in result:
            result.append(ref)
        if len(result) == maximum:
            break
    return result


def _scope_attention_refs(
    scope_id: str,
    selected: Sequence[str],
    dossier: _Dossier,
) -> list[str]:
    mandatory = [
        ref for ref in dossier.attention_refs[scope_id]
        if dossier.registry[ref].get("severity") == "high"
    ]
    return _dedupe_refs(
        [*mandatory, *selected], maximum=MAX_SCOPE_ATTENTION
    )


def _portfolio_attention_refs(
    selected: Sequence[str],
    dossier: _Dossier,
) -> list[str]:
    mandatory = [
        ref for refs in dossier.attention_refs.values() for ref in refs
        if dossier.registry[ref].get("severity") == "high"
    ]
    return _dedupe_refs(
        [*mandatory, *selected], maximum=MAX_PORTFOLIO_RECORDS
    )


def _account_base(capture: _Capture, snapshot: _ScopeSnapshot) -> dict:
    label, label_source, label_fact_id = _account_label(snapshot)
    summary = snapshot.brief.get("summary")
    summary_text = summary.get("text") if isinstance(summary, Mapping) else None
    if not isinstance(summary_text, str) or not summary_text.strip():
        raise AuditAggregateError("audit_brief.summary.text가 비어 있습니다.")
    summary_statement_ids = _safe_string_list(summary.get("statement_ids"))
    readiness = snapshot.brief.get("readiness")
    reasons = (
        _safe_string_list(readiness.get("reasons"))
        if isinstance(readiness, Mapping) else []
    )
    assert snapshot.scope.sheet is not None
    return {
        "id": "account:" + json_sha256({"scope_id": snapshot.scope.id}),
        "label": label,
        "label_source": label_source,
        "label_fact_id": label_fact_id,
        "scope": snapshot.scope.identity(),
        "workpaper": _workpaper(snapshot),
        "source_state": {
            "prepared_at": snapshot.binding["prepared_at"],
            "readiness_status": snapshot.binding["readiness_status"],
            "readiness_reasons": reasons,
            "facts_review_status": snapshot.binding["facts_review_status"],
            "brief_review_status": snapshot.binding["brief_review_status"],
            # Formula dependency names are indicators only.  Their contents are not observed and
            # they never auto-enroll another scope in the aggregate selection.
            "dependency_sheets": list(
                capture.dependencies[snapshot.scope.sheet]
            ),
            "dependency_role": "formula_reference_indicator_only",
            "dependency_sheet_contents_observed": False,
        },
        "counts": _counts(snapshot),
        "source_summary": summary_text,
        "source_summary_statement_ids": summary_statement_ids,
        "source_summary_record_refs": [
            _record_ref(snapshot.scope, "statement", statement_id)
            for statement_id in summary_statement_ids
        ],
    }


def _aggregate_limitation(code: str, description: str, severity: str, scope_ids: list[str]) -> dict:
    identity = {"code": code, "scope_ids": scope_ids, "description": description}
    return {
        "id": "aggregate_limitation:" + json_sha256(identity),
        "code": code,
        "description": description,
        "severity": severity,
        "scope_ids": scope_ids,
    }


def _static_fields(capture: _Capture, dossier: _Dossier) -> dict:
    unreviewed = [
        snapshot.scope.id for snapshot in capture.snapshots
        if snapshot.binding["facts_review_status"] != "approved"
        or snapshot.binding["brief_review_status"] != "approved"
    ]
    partial = [
        snapshot.scope.id for snapshot in capture.snapshots
        if snapshot.binding["readiness_status"] == "partial"
    ]
    selected_names = [snapshot.scope.sheet for snapshot in capture.snapshots]
    selected_set = set(selected_names)
    omitted = [name for name in capture.committed_sheets if name not in selected_set]
    exposed_by_scope = {
        snapshot.scope.id: (
            set(dossier.highlight_refs[snapshot.scope.id])
            | set(dossier.attention_refs[snapshot.scope.id])
        )
        for snapshot in capture.snapshots
    }
    source_by_scope = {
        snapshot.scope.id: {
            ref for ref, record in dossier.registry.items()
            if record.get("scope", {}).get("id") == snapshot.scope.id
        }
        for snapshot in capture.snapshots
    }
    truncated_scopes = [
        snapshot.scope.id for snapshot in capture.snapshots
        if len(exposed_by_scope[snapshot.scope.id])
        < len(source_by_scope[snapshot.scope.id])
    ]
    limitations: list[dict] = []
    if unreviewed:
        limitations.append(_aggregate_limitation(
            "source_unreviewed",
            "하나 이상의 입력 시트 audit bundle이 승인되지 않았습니다.",
            "moderate",
            unreviewed,
        ))
    if partial:
        limitations.append(_aggregate_limitation(
            "partial_source",
            "하나 이상의 입력 시트 audit brief가 partial 상태입니다.",
            "moderate",
            partial,
        ))
    if omitted:
        omitted_ids = [
            AuditScope.for_sheet(name).id for name in omitted
        ]
        limitations.append(_aggregate_limitation(
            "selection_subset",
            "명시적으로 선택하지 않은 commit된 시트 scope가 있습니다. 이 결과는 workbook 전체 브리핑이 아닙니다.",
            "low",
            omitted_ids,
        ))
    if truncated_scopes:
        limitations.append(_aggregate_limitation(
            "candidate_truncated",
            "compact model dossier의 시트별 후보 상한으로 일부 source brief record가 모델 선택 대상에서 제외되었습니다.",
            "moderate",
            truncated_scopes,
        ))
    status = "partial" if partial or truncated_scopes else "ready"
    readiness_reasons: list[str] = []
    if partial:
        readiness_reasons.append("선택된 시트 중 partial 상태의 audit brief가 있습니다.")
    if truncated_scopes:
        readiness_reasons.append(
            "compact dossier 후보 상한으로 일부 source brief record가 모델에 노출되지 않았습니다."
        )
    if not readiness_reasons:
        readiness_reasons.append("선택된 모든 시트 audit brief가 ready 상태입니다.")
    all_approved = not unreviewed
    unique_candidates = set()
    for refs in dossier.highlight_refs.values():
        unique_candidates.update(refs)
    for refs in dossier.attention_refs.values():
        unique_candidates.update(refs)
    source_candidate_count = len(dossier.registry)
    omitted_candidate_count = source_candidate_count - len(unique_candidates)
    coverage = {
        "selection_complete": True,
        "grouping_mode": "one_sheet_scope_per_account_section",
        "complete_over_committed_sheets": not omitted,
        "workbook_sheet_count": len(capture.workbook_sheets),
        "committed_sheet_count": len(capture.committed_sheets),
        "selected_sheet_count": len(capture.snapshots),
        "unprepared_sheet_count": len(capture.workbook_sheets) - len(capture.committed_sheets),
        "included_sheets": selected_names,
        "omitted_committed_sheets": omitted,
        "model_context_bytes": dossier.context_bytes,
        "model_context_limit_bytes": MAX_MODEL_CONTEXT_BYTES,
        "candidate_source_record_count": source_candidate_count,
        "candidate_record_count": len(unique_candidates),
        "omitted_candidate_record_count": omitted_candidate_count,
        "candidate_selection_complete": omitted_candidate_count == 0,
    }
    summary = (
        f"commit된 시트 audit brief {len(capture.snapshots)}건을 "
        "시트 scope별 계정 섹션으로 종합했습니다. workbook ledger와 standards_context 원문 필드는 "
        "aggregate 모델 입력에 직접 포함하지 않았습니다. 다만 source brief 문장이 원문을 "
        "요약하거나 인용한 내용은 후보 문장에 남을 수 있습니다."
    )
    return {
        "inputs": _aggregate_inputs(capture),
        "readiness": {"status": status, "reasons": readiness_reasons},
        "trust": {
            "all_sources_approved": all_approved,
            "source_unreviewed": not all_approved,
            "aggregate_unreviewed": True,
        },
        "coverage": coverage,
        "portfolio_summary": summary,
        "account_bases": [
            _account_base(capture, snapshot) for snapshot in capture.snapshots
        ],
        "limitations": limitations,
    }


def _build_document(
    capture: _Capture,
    dossier: _Dossier,
    model_plan: dict,
    *,
    model: str,
    prompt_sha256: str,
    generated_at: str,
) -> dict:
    static = _static_fields(capture, dossier)
    selections = {
        item["scope_id"]: item for item in model_plan["scope_selections"]
    }
    accounts: list[dict] = []
    for base in static["account_bases"]:
        scope_id = base["scope"]["id"]
        selection = selections[scope_id]
        accounts.append({
            **copy.deepcopy(base),
            "highlights": _materialize(
                selection["highlight_record_refs"], dossier.registry
            ),
            "attention_items": _materialize(
                _scope_attention_refs(
                    scope_id, selection["attention_record_refs"], dossier
                ),
                dossier.registry,
            ),
        })
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "inputs": static["inputs"],
        "generator": {
            "name": "excel_to_skill.audit.aggregate",
            "version": AGGREGATE_VERSION,
            "model": model,
            "prompt_sha256": prompt_sha256,
            "generated_at": generated_at,
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "readiness": static["readiness"],
        "trust": static["trust"],
        "coverage": static["coverage"],
        "portfolio": {
            "summary": static["portfolio_summary"],
            "highlights": _materialize(
                model_plan["portfolio_highlight_record_refs"], dossier.registry
            ),
            "attention_items": _materialize(
                _portfolio_attention_refs(
                    model_plan["portfolio_attention_record_refs"], dossier
                ),
                dossier.registry,
            ),
        },
        "accounts": accounts,
        "limitations": static["limitations"],
    }


def _validate_record_list(
    records: object,
    *,
    registry: Mapping[str, dict],
    allowed: set[str],
    label: str,
    require_one: bool = False,
    require_all_high: bool = False,
) -> None:
    if not isinstance(records, list):
        raise AuditAggregateError(f"{label}은 배열이어야 합니다.")
    refs = [
        record.get("record_ref") if isinstance(record, Mapping) else None
        for record in records
    ]
    if len(refs) != len(set(refs)):
        raise AuditAggregateError(f"{label}에 중복 record_ref가 있습니다.")
    if require_one and allowed and not refs:
        raise AuditAggregateError(f"{label}이 비어 있습니다.")
    for index, ref in enumerate(refs):
        if ref not in allowed or ref not in registry:
            raise AuditAggregateError(f"{label}[{index}]가 관찰하지 않은 record_ref입니다.")
        if records[index] != registry[ref]:
            raise AuditAggregateError(f"{label}[{index}]가 source record와 일치하지 않습니다.")
    if require_all_high:
        high_allowed = {
            ref for ref in allowed if registry[ref].get("severity") == "high"
        }
        if not high_allowed <= set(refs):
            raise AuditAggregateError(
                f"{label}에 high-severity record 전체가 포함되지 않았습니다."
            )


def validate_audit_aggregate(
    capture: _Capture,
    dossier: _Dossier,
    document: object,
    *,
    expected_model: str | None = None,
    expected_prompt_sha256: str | None = None,
) -> None:
    problems = _schema_problems(document, AGGREGATE_SCHEMA)
    if problems:
        raise AuditAggregateError(
            "audit account brief schema 불일치: " + "; ".join(problems[:5])
        )
    assert isinstance(document, dict)
    static = _static_fields(capture, dossier)
    for key in ("inputs", "readiness", "trust", "coverage", "limitations"):
        if document.get(key) != static[key]:
            raise AuditAggregateError(f"aggregate.{key}가 현재 source snapshot과 다릅니다.")
    portfolio = document["portfolio"]
    if portfolio.get("summary") != static["portfolio_summary"]:
        raise AuditAggregateError("aggregate portfolio summary가 코드 생성값과 다릅니다.")
    generator = document["generator"]
    if generator.get("version") != AGGREGATE_VERSION:
        raise AuditAggregateError("aggregate generator version이 현재 계약과 다릅니다.")
    if expected_model is not None and generator.get("model") != expected_model:
        raise AuditAggregateError("aggregate generator model이 요청과 다릅니다.")
    if (
        expected_prompt_sha256 is not None
        and generator.get("prompt_sha256") != expected_prompt_sha256
    ):
        raise AuditAggregateError("aggregate prompt digest가 현재 계약과 다릅니다.")
    accounts = document["accounts"]
    if len(accounts) != len(static["account_bases"]):
        raise AuditAggregateError("aggregate account 수가 source scope 수와 다릅니다.")
    for index, (account, base) in enumerate(zip(accounts, static["account_bases"], strict=True)):
        actual_base = {
            key: value for key, value in account.items()
            if key not in {"highlights", "attention_items"}
        }
        if actual_base != base:
            raise AuditAggregateError(f"aggregate.accounts[{index}] 정적 필드가 다릅니다.")
        scope_id = base["scope"]["id"]
        _validate_record_list(
            account["highlights"],
            registry=dossier.registry,
            allowed=set(dossier.highlight_refs[scope_id]),
            label=f"accounts[{index}].highlights",
            require_one=True,
        )
        _validate_record_list(
            account["attention_items"],
            registry=dossier.registry,
            allowed=set(dossier.attention_refs[scope_id]),
            label=f"accounts[{index}].attention_items",
            require_one=True,
            require_all_high=True,
        )
    _validate_record_list(
        portfolio["highlights"],
        registry=dossier.registry,
        allowed={ref for refs in dossier.highlight_refs.values() for ref in refs},
        label="portfolio.highlights",
        require_one=True,
    )
    _validate_record_list(
        portfolio["attention_items"],
        registry=dossier.registry,
        allowed={ref for refs in dossier.attention_refs.values() for ref in refs},
        label="portfolio.attention_items",
        require_one=True,
    )
    portfolio_high_allowed = {
        ref for refs in dossier.attention_refs.values() for ref in refs
        if dossier.registry[ref].get("severity") == "high"
    }
    portfolio_attention_refs = {
        record["record_ref"] for record in portfolio["attention_items"]
    }
    if (
        portfolio_high_allowed
        and not (portfolio_attention_refs & portfolio_high_allowed)
    ):
        raise AuditAggregateError(
            "portfolio.attention_items에 high-severity record가 없습니다."
        )


def _build_commit(paths: AggregatePaths, document: dict, *, prepared_at: str) -> dict:
    return {
        "schema_version": AGGREGATE_COMMIT_SCHEMA_VERSION,
        "aggregate_id": paths.aggregate_id,
        "inputs": copy.deepcopy(document["inputs"]),
        "present": True,
        "status": document["readiness"]["status"],
        "version": AGGREGATE_VERSION,
        "aggregate_key": json_sha256(document),
        "prepared_at": prepared_at,
        "review_status": "draft",
    }


def _validate_commit(
    paths: AggregatePaths,
    capture: _Capture,
    dossier: _Dossier,
    document: dict,
    commit: object,
    *,
    expected_model: str | None = None,
    expected_prompt_sha256: str | None = None,
) -> None:
    _validate_published_pair(
        paths,
        document,
        commit,
        expected_model=expected_model,
        expected_prompt_sha256=expected_prompt_sha256,
    )
    validate_audit_aggregate(
        capture,
        dossier,
        document,
        expected_model=expected_model,
        expected_prompt_sha256=expected_prompt_sha256,
    )


def _validate_published_pair(
    paths: AggregatePaths,
    document: object,
    commit: object,
    *,
    expected_model: str | None = None,
    expected_prompt_sha256: str | None = None,
) -> None:
    """Validate artifact/commit self-integrity before consulting mutable sources."""
    document_problems = _schema_problems(document, AGGREGATE_SCHEMA)
    if document_problems:
        raise AuditAggregateError(
            "audit account brief schema 불일치: "
            + "; ".join(document_problems[:5])
        )
    problems = _schema_problems(commit, AGGREGATE_COMMIT_SCHEMA)
    if problems:
        raise AuditAggregateError(
            "audit aggregate commit schema 불일치: " + "; ".join(problems[:5])
        )
    assert isinstance(document, dict) and isinstance(commit, dict)
    if commit.get("aggregate_id") != paths.aggregate_id:
        raise AuditAggregateError("aggregate commit id가 선택 범위와 다릅니다.")
    if commit.get("inputs") != document["inputs"]:
        raise AuditAggregateError("aggregate commit inputs가 artifact와 다릅니다.")
    if commit.get("version") != AGGREGATE_VERSION:
        raise AuditAggregateError("aggregate commit version이 현재 계약과 다릅니다.")
    if commit.get("aggregate_key") != json_sha256(document):
        raise AuditAggregateError("aggregate commit artifact digest가 일치하지 않습니다.")
    if commit.get("status") != document["readiness"]["status"]:
        raise AuditAggregateError("aggregate commit readiness가 artifact와 다릅니다.")
    if commit.get("review_status") != document["review"]["status"]:
        raise AuditAggregateError("aggregate commit review status가 artifact와 다릅니다.")
    if commit.get("prepared_at") != document["generator"]["generated_at"]:
        raise AuditAggregateError("aggregate commit prepared_at이 generator와 다릅니다.")
    generator = document["generator"]
    if generator.get("version") != AGGREGATE_VERSION:
        raise AuditAggregateError("aggregate generator version이 현재 계약과 다릅니다.")
    if expected_model is not None and generator.get("model") != expected_model:
        raise AuditAggregateError("aggregate generator model이 요청과 다릅니다.")
    if (
        expected_prompt_sha256 is not None
        and generator.get("prompt_sha256") != expected_prompt_sha256
    ):
        raise AuditAggregateError("aggregate prompt digest가 현재 계약과 다릅니다.")


def _load_cached(
    paths: AggregatePaths,
    capture: _Capture,
    dossier: _Dossier,
    *,
    model: str,
    prompt_sha256: str,
) -> dict | None:
    exists = (paths.brief.is_file(), paths.commit.is_file())
    if not any(exists):
        return None
    if not all(exists):
        return None
    try:
        document = _read_json_object(paths.brief, label="account_brief.json")
        commit = _read_json_object(paths.commit, label="aggregate commit.json")
        _validate_commit(
            paths,
            capture,
            dossier,
            document,
            commit,
            expected_model=model,
            expected_prompt_sha256=prompt_sha256,
        )
        _assert_sources_unchanged(capture)
    except (AuditAggregateError, AuditLLMError):
        return None
    return document


def load_audit_aggregate(
    pkg: Path | str,
    aggregate_id: str,
) -> tuple[AggregatePaths, dict, dict]:
    """Load one published aggregate only after revalidating every source scope."""
    if not re.fullmatch(r"[0-9a-f]{64}", aggregate_id):
        raise AuditAggregateError(f"aggregate_id가 유효하지 않습니다: {aggregate_id!r}")
    path = Path(pkg)
    data_dir = path / _AGGREGATE_ROOT / aggregate_id
    brief_path = data_dir / "account_brief.json"
    commit_path = data_dir / "commit.json"
    commit = _read_json_object(commit_path, label="aggregate commit.json")
    commit_problems = _schema_problems(commit, AGGREGATE_COMMIT_SCHEMA)
    if commit_problems:
        raise AuditAggregateError(
            "audit aggregate commit schema 불일치: "
            + "; ".join(commit_problems[:5])
        )
    inputs = commit.get("inputs")
    input_problems = _aggregate_input_problems(inputs)
    if input_problems:
        raise AuditAggregateError(
            "aggregate commit inputs schema 불일치: "
            + "; ".join(input_problems[:5])
        )
    selection = inputs.get("selection") if isinstance(inputs, Mapping) else None
    scopes = inputs.get("scopes") if isinstance(inputs, Mapping) else None
    if not isinstance(selection, Mapping) or not isinstance(scopes, list):
        raise AuditAggregateError("aggregate commit selection/scopes가 유효하지 않습니다.")
    mode = selection.get("mode")
    advertised_paths = AggregatePaths(
        package=path,
        aggregate_id=aggregate_id,
        data_dir=data_dir,
        brief=brief_path,
        commit=commit_path,
    )
    document = _read_json_object(brief_path, label="account_brief.json")
    _, prompt_sha = load_prompt(AGGREGATE_PROMPT)
    _validate_published_pair(
        advertised_paths,
        document,
        commit,
        expected_prompt_sha256=prompt_sha,
    )
    source_sheets = [
        binding["scope"]["sheet"] for binding in scopes
    ]
    try:
        raw_historical = _capture_sources(path, sheets=source_sheets)
    except AuditAggregateError as e:
        raise AuditAggregateStaleError(
            f"aggregate source snapshot을 더 이상 재구성할 수 없습니다: {e}"
        ) from e
    coverage = document["coverage"]
    stored_names = [
        *coverage["included_sheets"],
        *coverage["omitted_committed_sheets"],
    ]
    stored_set = set(stored_names)
    if (
        len(stored_set) != coverage["committed_sheet_count"]
        or not stored_set <= set(raw_historical.workbook_sheets)
    ):
        raise AuditAggregateError(
            "aggregate coverage의 generation-time commit manifest가 유효하지 않습니다."
        )
    historical = replace(
        raw_historical,
        mode=mode,
        committed_sheets=tuple(
            name for name in raw_historical.workbook_sheets if name in stored_set
        ),
    )
    historical_paths = aggregate_paths(historical)
    if historical_paths.aggregate_id != aggregate_id:
        raise AuditAggregateError(
            "aggregate directory가 advertised selection identity와 다릅니다."
        )
    if commit.get("inputs") != _aggregate_inputs(historical):
        raise AuditAggregateStaleError(
            "aggregate selected source snapshot이 현재 bundle과 다릅니다."
        )
    historical_dossier = _build_dossier(historical)
    _validate_commit(
        advertised_paths,
        historical,
        historical_dossier,
        document,
        commit,
        expected_prompt_sha256=prompt_sha,
    )
    try:
        current = (
            raw_historical
            if mode == "explicit_sheets"
            else _capture_sources(path, all_committed_sheets=True)
        )
    except AuditAggregateError as e:
        raise AuditAggregateStaleError(
            f"aggregate current selection을 재구성할 수 없습니다: {e}"
        ) from e
    current_paths = aggregate_paths(current)
    if current_paths.aggregate_id != aggregate_id:
        raise AuditAggregateStaleError(
            "aggregate directory가 현재 선택 identity와 다릅니다."
        )
    if commit.get("inputs") != _aggregate_inputs(current):
        raise AuditAggregateStaleError(
            "aggregate inputs가 현재 source scope/coverage snapshot과 다릅니다."
        )
    _assert_sources_unchanged(current)
    return advertised_paths, document, commit


def plan_audit_aggregate(
    pkg: Path | str,
    *,
    sheets: Sequence[str] | None = None,
    all_committed_sheets: bool = False,
    model: str,
    force: bool = False,
) -> dict:
    capture = _capture_sources(
        pkg, sheets=sheets, all_committed_sheets=all_committed_sheets
    )
    dossier = _build_dossier(capture, enforce_limit=False)
    _, prompt_sha = load_prompt(AGGREGATE_PROMPT)
    paths = aggregate_paths(capture)
    cache_available = False
    if not force and dossier.context_bytes <= MAX_MODEL_CONTEXT_BYTES:
        cache_available = _load_cached(
            paths,
            capture,
            dossier,
            model=model,
            prompt_sha256=prompt_sha,
        ) is not None
    static = _static_fields(capture, dossier)
    within_limit = dossier.context_bytes <= MAX_MODEL_CONTEXT_BYTES
    return {
        "schema_version": "audit_aggregate_plan.v1",
        "aggregate_id": paths.aggregate_id,
        "selection": static["inputs"]["selection"],
        "source_manifest_sha256": static["inputs"]["source_manifest_sha256"],
        "coverage": static["coverage"],
        "model_context_within_limit": within_limit,
        "generation_blocked_reason": (
            None
            if within_limit
            else "model_context_limit_exceeded; select fewer sheets"
        ),
        "cache_available": cache_available,
        "force": force,
        "estimated_model_calls": 0 if cache_available or not within_limit else 1,
        "output": str(paths.brief),
    }


def aggregate_audit_package(
    pkg: Path | str,
    *,
    sheets: Sequence[str] | None = None,
    all_committed_sheets: bool = False,
    model: str,
    client=None,
    client_factory=None,
    force: bool = False,
    generated_at: str | None = None,
    eprint=None,
) -> AggregateResult:
    """Build and atomically publish one account-level aggregate selection."""
    eprint = eprint or (lambda *args: None)
    capture = _capture_sources(
        pkg, sheets=sheets, all_committed_sheets=all_committed_sheets
    )
    dossier = _build_dossier(capture)
    prompt, prompt_sha = load_prompt(AGGREGATE_PROMPT)
    paths = aggregate_paths(capture)
    if not force:
        cached = _load_cached(
            paths,
            capture,
            dossier,
            model=model,
            prompt_sha256=prompt_sha,
        )
        if cached is not None:
            eprint(f"[audit aggregate cache hit] {paths.aggregate_id}")
            return AggregateResult(capture.package, paths, cached, True)
    _assert_sources_unchanged(capture)
    if client is None:
        if client_factory is None:
            raise AuditAggregateError(
                "cache miss인 audit aggregate에는 client 또는 client_factory가 필요합니다."
            )
        client = client_factory()
    _assert_sources_unchanged(capture)
    schema = _model_plan_schema(len(capture.snapshots))
    model_plan = call_json(
        client,
        system=prompt,
        user=dossier.serialized,
        schema=schema,
        semantic_validator=lambda document: _validate_model_plan(
            document, capture, dossier
        ),
        label="audit aggregate selection",
        retries=(
            1
            if dossier.context_bytes
            <= MAX_MODEL_CONTEXT_BYTES - _RETRY_MESSAGE_RESERVE_BYTES
            else 0
        ),
        eprint=eprint,
    )
    _assert_sources_unchanged(capture)
    generated_at = generated_at or _now_iso()
    document = _build_document(
        capture,
        dossier,
        model_plan,
        model=model,
        prompt_sha256=prompt_sha,
        generated_at=generated_at,
    )
    validate_audit_aggregate(
        capture,
        dossier,
        document,
        expected_model=model,
        expected_prompt_sha256=prompt_sha,
    )
    commit = _build_commit(paths, document, prepared_at=generated_at)
    _validate_commit(
        paths,
        capture,
        dossier,
        document,
        commit,
        expected_model=model,
        expected_prompt_sha256=prompt_sha,
    )
    with cache.package_lock(capture.package):
        _assert_sources_unchanged(capture)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        snapshot = _snapshot_files([paths.brief, paths.commit])
        try:
            _atomic_write_text(
                paths.brief,
                json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n",
            )
            _atomic_write_text(
                paths.commit,
                json.dumps(commit, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n",
            )
        except BaseException:
            _restore_files(snapshot)
            raise
    return AggregateResult(capture.package, paths, document, False)


def _markdown_text(value: object) -> str:
    text = _CONTROL_CHARS.sub("", str(value))
    text = " ".join(text.split())
    text = html.escape(text, quote=False)
    text = re.sub(r"([\\`*_\[\]~|])", r"\\\1", text)
    text = re.sub(r"^([#+-])", r"\\\1", text)
    return re.sub(r"^(\d+)([.)])(?=\s)", r"\1\\\2", text)


def _render_record(record: Mapping[str, object]) -> list[str]:
    lines = [f"- {_markdown_text(record.get('text'))}"]
    details = [
        f"유형: {_markdown_text(record.get('kind'))}",
        f"상태: {_markdown_text(record.get('status'))}",
        f"시트: {_markdown_text(record.get('scope', {}).get('sheet'))}",
    ]
    if isinstance(record.get("confidence"), (int, float)):
        details.append(f"신뢰도: {record['confidence']:.2f}")
    lines.append("  - " + " · ".join(details))
    source_id = record.get("source_id")
    if source_id:
        lines.append(
            "  - source: "
            + _markdown_text(record.get("scope", {}).get("id"))
            + " / "
            + _markdown_text(source_id)
        )
    for label, key in (
        ("조서 fact", "fact_ids"),
        ("관계", "relation_ids"),
        ("기준서 citation", "standard_citation_ids"),
    ):
        values = record.get(key)
        if isinstance(values, list) and values:
            shown = values[:5]
            suffix = f" · 외 {len(values) - len(shown)}건" if len(values) > len(shown) else ""
            lines.append(
                f"  - {label}: "
                + ", ".join(_markdown_text(value) for value in shown)
                + suffix
            )
    return lines


def render_audit_aggregate_markdown(document: Mapping[str, object]) -> str:
    """Render an aggregate without hydrating raw cells or standards passages."""
    coverage = document["coverage"]
    trust = document["trust"]
    readiness = document["readiness"]
    lines = ["# 계정별 종합 브리핑", ""]
    lines.append(
        "> 범위: workbook "
        f"{coverage['workbook_sheet_count']}개 시트 · "
        f"현재 commit {coverage['committed_sheet_count']}건 · "
        f"선택 {coverage['selected_sheet_count']}건 · "
        f"미준비 {coverage['unprepared_sheet_count']}건 · "
        f"준비 상태: {readiness['status']} · aggregate 검토: draft"
    )
    lines.append(
        "> 입력 검토: "
        + ("모두 승인" if trust["all_sources_approved"] else "미승인 source 포함")
        + " · ledger/standards_context 원문 필드는 직접 보내지 않음(brief 인용문은 포함 가능)"
    )
    if not coverage["complete_over_committed_sheets"]:
        omitted = ", ".join(_markdown_text(item) for item in coverage["omitted_committed_sheets"])
        lines.append(f"> 주의: 선택 밖 commit 시트가 있습니다: {omitted}")
    portfolio = document["portfolio"]
    lines.extend(["", _markdown_text(portfolio["summary"])])
    show_portfolio_records = coverage["selected_sheet_count"] > 1
    if show_portfolio_records and portfolio["highlights"]:
        lines.extend(["", "## 전체 핵심", ""])
        for record in portfolio["highlights"]:
            lines.extend(_render_record(record))
    if show_portfolio_records and portfolio["attention_items"]:
        lines.extend(["", "## 전체 확인 필요", ""])
        for record in portfolio["attention_items"]:
            lines.extend(_render_record(record))
    for account in document["accounts"]:
        lines.extend(["", f"## {_markdown_text(account['label'])}", ""])
        state = account["source_state"]
        lines.append(
            "> 시트: "
            f"{_markdown_text(account['scope']['sheet'])} · "
            f"준비: {state['readiness_status']} · "
            f"facts 검토: {state['facts_review_status']} · "
            f"brief 검토: {state['brief_review_status']}"
        )
        dependencies = state.get("dependency_sheets", [])
        if dependencies:
            lines.append(
                "> 수식 참조 표시: "
                + ", ".join(_markdown_text(item) for item in dependencies)
                + " · 대상 시트 내용 미관찰 · 자동 포함 아님"
            )
        lines.extend(["", _markdown_text(account["source_summary"])])
        if account["highlights"]:
            lines.extend(["", "### 핵심", ""])
            for record in account["highlights"]:
                lines.extend(_render_record(record))
        if account["attention_items"]:
            lines.extend(["", "### 확인 필요", ""])
            for record in account["attention_items"]:
                lines.extend(_render_record(record))
    if document["limitations"]:
        lines.extend(["", "## 종합 범위의 한계", ""])
        for limitation in document["limitations"]:
            lines.append(
                f"- {_markdown_text(limitation['description'])} "
                f"({_markdown_text(limitation['severity'])})"
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "AGGREGATE_VERSION",
    "AuditAggregateError",
    "AuditAggregateStaleError",
    "AggregatePaths",
    "AggregateResult",
    "MAX_MODEL_CONTEXT_BYTES",
    "aggregate_audit_package",
    "load_audit_aggregate",
    "plan_audit_aggregate",
    "render_audit_aggregate_markdown",
]
