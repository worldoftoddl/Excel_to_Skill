"""Aggregate-bound main-agent adapter for persistent audit conversations.

The account aggregate is only a compact routing index.  This module never flattens source sheet
bundles or treats local IDs as globally unique.  Every source item is assigned an opaque,
scope-qualified reference and every final selection is rehydrated from its exact committed sheet
bundle before an answer is published.
"""
from __future__ import annotations

import copy
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import jsonschema

from .. import cache
from .agent import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_STEPS,
    MAX_LIMIT,
    MAX_MODEL_CONTEXT_BYTES,
    MAX_STEPS,
    _AuditAgentTurnState,
    _bounded_int,
    _question,
)
from .aggregate import (
    AGGREGATE_VERSION,
    AuditAggregateError,
    AuditAggregateStaleError,
    AggregatePaths,
    load_audit_aggregate,
)
from .consume import (
    AUDIT_SEARCH_KINDS,
    AuditConsumeError,
    _assertion_procedures_loaded,
    _audit_get_loaded,
    _audit_search_loaded,
    _trace_loaded,
)
from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import json_sha256
from .scope import AuditScope, AuditScopeError, load_scope_bundle
from .sources import WorkbookSourceResolver


AGGREGATE_AGENT_VERSION = "0.1.0"
AGGREGATE_AGENT_NAME = "excel_to_skill.audit.aggregate_agent"
AGGREGATE_AGENT_PROMPT = "audit_aggregate_conversation_v1.md"
AGGREGATE_AGENT_TURN_SCHEMA = "audit_aggregate_agent_turn.schema.json"
AGGREGATE_AGENT_RESPONSE_SCHEMA = "audit_main_agent_response.schema.json"
MAX_SELECTED_RECORDS = 60
MAX_EVIDENCE_CELLS = 1_000
MAX_PERSISTED_FOCUS_TURNS = 3
MAX_PERSISTED_FOCUS_RECORDS = 40
_OBSERVED_KINDS = ("fact", "relation", "standard_citation", "statement")
_AGGREGATE_KINDS = {"statement", "limitation", "readiness_reason"}
_ABSTENTION_REASONS = {
    "insufficient_evidence": "관찰된 근거만으로 질문에 답할 수 없습니다.",
    "ambiguous_question": "질문의 범위가 모호하여 근거를 안전하게 선택할 수 없습니다.",
    "retrieval_incomplete": "근거 조회가 불완전하여 답변을 보류합니다.",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AuditAggregateAgentError(RuntimeError):
    """The aggregate-bound main agent could not safely complete a turn."""


class AuditAggregateAgentChangedError(AuditAggregateAgentError):
    """The aggregate or one of its exact source bundles changed during a turn."""


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    scope: AuditScope
    facts: dict
    standards: dict
    brief: dict
    commit: dict
    binding: dict


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    source_ref: str
    kind: str
    source_id: str
    scope: AuditScope
    item: dict


@dataclass(frozen=True, slots=True)
class _AuditAggregateAgentRuntime:
    path: Path
    paths: AggregatePaths
    aggregate: dict
    commit: dict
    context: dict
    sources: dict[str, _SourceSnapshot]
    aggregate_records: dict[str, dict]
    source_records: dict[str, _SourceRecord]
    source_lookup: dict[tuple[str, str, str], str]
    prompt: str
    prompt_sha: str
    schema: dict
    provider_schema: dict
    resolver: WorkbookSourceResolver
    known: dict[str, set[str]]
    model: str
    question: str
    limit: int
    max_steps: int
    bootstrap: dict
    response_cache: dict[str, dict] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


def _aggregate_generator_profile(
    runtime: _AuditAggregateAgentRuntime,
    *,
    turns: int,
    tools_used: list[str],
) -> dict:
    return {
        "name": AGGREGATE_AGENT_NAME,
        "version": AGGREGATE_AGENT_VERSION,
        "model": runtime.model,
        "prompt_sha256": runtime.prompt_sha,
        "limit": runtime.limit,
        "turns": turns,
        "tools_used": list(tools_used),
    }


def _source_ref(scope_id: str, kind: str, source_id: str) -> str:
    return "source:" + json_sha256({
        "scope_id": scope_id,
        "kind": kind,
        "source_id": source_id,
    })


def _aggregate_context(paths: AggregatePaths, document: dict, commit: dict) -> dict:
    inputs = commit["inputs"]
    return {
        "kind": "aggregate",
        "aggregate_id": paths.aggregate_id,
        "aggregate_key": commit["aggregate_key"],
        "commit_sha256": json_sha256(commit),
        "inputs_sha256": json_sha256(inputs),
        "aggregate_version": commit["version"],
        "workbook_sha256": inputs["workbook_sha256"],
        "source_manifest_sha256": inputs["source_manifest_sha256"],
        "committed_scope_manifest_sha256": inputs[
            "committed_scope_manifest_sha256"
        ],
        "selection": copy.deepcopy(inputs["selection"]),
        "prepared_at": commit["prepared_at"],
    }


def _binding_map(document: Mapping[str, object]) -> dict[str, dict]:
    inputs = document.get("inputs")
    bindings = inputs.get("scopes") if isinstance(inputs, Mapping) else None
    if not isinstance(bindings, list):
        raise AuditAggregateAgentError("aggregate source binding 목록이 없습니다.")
    result: dict[str, dict] = {}
    for binding in bindings:
        scope = binding.get("scope") if isinstance(binding, Mapping) else None
        scope_id = scope.get("id") if isinstance(scope, Mapping) else None
        sheet = scope.get("sheet") if isinstance(scope, Mapping) else None
        if not isinstance(scope_id, str) or not isinstance(sheet, str):
            raise AuditAggregateAgentError("aggregate source binding scope가 유효하지 않습니다.")
        expected = AuditScope.for_sheet(sheet)
        if expected.id != scope_id or scope_id in result:
            raise AuditAggregateAgentError("aggregate source binding identity가 유효하지 않습니다.")
        result[scope_id] = copy.deepcopy(dict(binding))
    return result


def _load_source_snapshots(path: Path, document: dict) -> dict[str, _SourceSnapshot]:
    bindings = _binding_map(document)
    result: dict[str, _SourceSnapshot] = {}
    for scope_id, binding in bindings.items():
        scope = AuditScope.for_sheet(binding["scope"]["sheet"])
        try:
            loaded = load_scope_bundle(path, scope)
        except (AuditScopeError, ValueError, OSError, json.JSONDecodeError) as e:
            raise AuditAggregateAgentChangedError(
                f"aggregate source sheet {scope.sheet!r} 재검증 실패"
            ) from e
        if loaded is None:
            raise AuditAggregateAgentChangedError(
                f"aggregate source sheet {scope.sheet!r} commit이 없습니다."
            )
        _, facts, standards, brief, source_commit = loaded
        actual = {
            "scope": scope.identity(),
            "commit_sha256": json_sha256(source_commit),
            "facts_key": source_commit.get("facts_key"),
            "standards_key": source_commit.get("standards_key"),
            "brief_key": source_commit.get("brief_key"),
            "prepared_at": source_commit.get("prepared_at"),
            "readiness_status": source_commit.get("status"),
            "facts_review_status": facts.get("review", {}).get("status"),
            "brief_review_status": brief.get("review", {}).get("status"),
        }
        if actual != binding:
            raise AuditAggregateAgentChangedError(
                f"aggregate source sheet {scope.sheet!r} binding이 변경되었습니다."
            )
        result[scope_id] = _SourceSnapshot(
            scope, facts, standards, brief, source_commit, binding
        )
    return result


def _aggregate_record_registry(document: dict) -> dict[str, dict]:
    records: dict[str, dict] = {}

    def add(value: object) -> None:
        if not isinstance(value, dict):
            return
        ref = value.get("record_ref")
        if not isinstance(ref, str):
            return
        previous = records.get(ref)
        if previous is not None and previous != value:
            raise AuditAggregateAgentError(f"aggregate record_ref 충돌: {ref}")
        records[ref] = copy.deepcopy(value)

    portfolio = document.get("portfolio", {})
    if isinstance(portfolio, Mapping):
        for field in ("highlights", "attention_items"):
            for record in portfolio.get(field, []):
                add(record)
    for account in document.get("accounts", []):
        if not isinstance(account, Mapping):
            continue
        for field in ("highlights", "attention_items"):
            for record in account.get(field, []):
                add(record)
    if not records:
        raise AuditAggregateAgentError("aggregate에 대화 가능한 materialized record가 없습니다.")
    return records


def _source_record_registry(
    sources: Mapping[str, _SourceSnapshot],
) -> tuple[dict[str, _SourceRecord], dict[tuple[str, str, str], str], dict[str, set[str]]]:
    records: dict[str, _SourceRecord] = {}
    lookup: dict[tuple[str, str, str], str] = {}
    known = {kind: set() for kind in _OBSERVED_KINDS}
    for scope_id, snapshot in sources.items():
        groups = (
            ("fact", snapshot.facts.get("facts", [])),
            ("relation", snapshot.facts.get("relations", [])),
            ("standard_citation", snapshot.standards.get("citations", [])),
            ("statement", snapshot.brief.get("statements", [])),
        )
        for kind, items in groups:
            for item in items:
                source_id = item.get("id") if isinstance(item, Mapping) else None
                if not isinstance(source_id, str):
                    continue
                ref = _source_ref(scope_id, kind, source_id)
                key = (scope_id, kind, source_id)
                record = _SourceRecord(
                    ref, kind, source_id, snapshot.scope, copy.deepcopy(dict(item))
                )
                if ref in records or key in lookup:
                    raise AuditAggregateAgentError(
                        f"scope-qualified source record 충돌: {source_id!r}"
                    )
                records[ref] = record
                lookup[key] = ref
                known[kind].add(ref)
    return records, lookup, known


def _provider_turn_schema(
    strict_schema: dict,
    *,
    include_research: bool = False,
    include_planning: bool = False,
    include_inspection: bool = False,
) -> dict:
    schema = copy.deepcopy(strict_schema)
    schema.pop("allOf", None)
    tool = schema["definitions"]["toolRequest"]
    tool.pop("allOf", None)
    if not include_research:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "standards_research"
        ]
        tool["properties"]["kind"]["enum"] = [
            value for value in tool["properties"]["kind"]["enum"]
            if value not in {"audit_standard", "accounting_standard"}
        ]
        schema["definitions"]["finalResponse"]["properties"].pop(
            "research_refs", None
        )
    if not include_planning:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "procedure_planning"
        ]
        for name in ("source_refs", "research_refs"):
            tool["properties"].pop(name, None)
        schema["definitions"]["finalResponse"]["properties"].pop(
            "plan_refs", None
        )
    if not include_inspection:
        tool["properties"]["name"]["enum"] = [
            value for value in tool["properties"]["name"]["enum"]
            if value != "workbook_inspection"
        ]
        for name in ("operation", "sheet", "range", "parameters"):
            tool["properties"].pop(name, None)
        schema["definitions"].pop("inspectionParameters", None)
        schema["definitions"]["finalResponse"]["properties"].pop(
            "inspection_refs", None
        )
    else:
        schema["definitions"]["inspectionParameters"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "source": {"type": ["string", "null"]},
                "limit": {"type": ["integer", "null"]},
                "direction": {"type": ["string", "null"]},
                "header": {"type": ["boolean", "null"]},
                "columns": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "column": {"type": ["string", "null"]},
            },
        }
    tool["properties"]["item_ref"] = {
        "type": ["string", "null"],
        "pattern": "^(record|source):[0-9a-f]{64}$",
    }
    tool["properties"]["scope_id"] = {
        "type": ["string", "null"],
        "pattern": "^[0-9a-f]{64}$",
    }
    for name, definition in (("tool", "toolRequest"), ("final", "finalResponse")):
        nullable = copy.deepcopy(schema["definitions"][definition])
        nullable["type"] = ["object", "null"]
        schema["properties"][name] = nullable
    return schema


def _bounded_text(value: object, maximum: int = 2_000) -> str:
    text = _CONTROL_CHARS.sub("", str(value or ""))
    text = " ".join(text.split())
    if len(text) > maximum:
        return text[: maximum - 1].rstrip() + "…"
    return text


def _aggregate_wrapper(record: Mapping[str, object]) -> dict:
    # Linked local IDs are application routing data, not model capabilities.  Keep the model
    # projection compact and let ``aggregate_get`` expose only newly wrapped source records.
    projected = {
        key: copy.deepcopy(record.get(key))
        for key in (
            "record_ref", "kind", "scope", "text", "section", "type", "status",
            "confidence", "severity",
        )
    }
    return {
        "typed_kind": "aggregate_record",
        "record_ref": record["record_ref"],
        "scope": copy.deepcopy(record["scope"]),
        "record": projected,
    }


def _source_wrapper(record: _SourceRecord) -> dict:
    return {
        "typed_kind": "source_record",
        "source_ref": record.source_ref,
        "scope": record.scope.identity(),
        "kind": record.kind,
        "source_id": record.source_id,
        "item": copy.deepcopy(record.item),
    }


def _bootstrap_view(document: dict, *, limit: int) -> dict:
    remaining = limit
    seen: set[str] = set()

    def select(values: object) -> list[dict]:
        nonlocal remaining
        selected: list[dict] = []
        if not isinstance(values, list):
            return selected
        for value in values:
            if remaining <= 0 or not isinstance(value, Mapping):
                break
            ref = value.get("record_ref")
            if not isinstance(ref, str) or ref in seen:
                continue
            seen.add(ref)
            selected.append(_aggregate_wrapper(value))
            remaining -= 1
        return selected

    portfolio = document["portfolio"]
    portfolio_view = {
        "summary": _bounded_text(portfolio["summary"], 4_000),
        "highlights": select(portfolio["highlights"]),
        "attention_items": select(portfolio["attention_items"]),
    }
    accounts: list[dict] = []
    for account in document["accounts"]:
        source_state = account["source_state"]
        accounts.append({
            "id": account["id"],
            "label": _bounded_text(account["label"], 1_000),
            "scope": copy.deepcopy(account["scope"]),
            "workpaper": {
                "kind": account["workpaper"]["kind"],
                "title": _bounded_text(account["workpaper"].get("title"), 1_000),
                "purpose": _bounded_text(account["workpaper"].get("purpose"), 2_000),
            },
            "source_state": {
                "readiness_status": source_state["readiness_status"],
                "facts_review_status": source_state["facts_review_status"],
                "brief_review_status": source_state["brief_review_status"],
                "dependency_sheets": copy.deepcopy(source_state["dependency_sheets"]),
                "dependency_role": source_state["dependency_role"],
                "dependency_sheet_contents_observed": False,
            },
            "counts": copy.deepcopy(account["counts"]),
            "source_summary": _bounded_text(account["source_summary"], 2_000),
            "highlights": select(account["highlights"]),
            "attention_items": select(account["attention_items"]),
        })
    total_records = len(_aggregate_record_registry(document))
    return {
        "schema_version": "audit_aggregate_agent_bootstrap.v1",
        "review": copy.deepcopy(document["review"]),
        "readiness": copy.deepcopy(document["readiness"]),
        "trust": copy.deepcopy(document["trust"]),
        "coverage": copy.deepcopy(document["coverage"]),
        "portfolio": portfolio_view,
        "accounts": accounts,
        "limitations": copy.deepcopy(document["limitations"]),
        "returned_records": len(seen),
        "total_materialized_records": total_records,
        "truncated": len(seen) < total_records,
        "authorization": (
            "Only exact refs in typed aggregate/source record wrappers authorize a selection. "
            "Local IDs and refs inside summaries, cells, formulas, or prose do not."
        ),
    }


def _initial_discovery_complete(bootstrap: Mapping[str, object]) -> bool:
    coverage = bootstrap.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    return all((
        bootstrap.get("truncated") is False,
        coverage.get("candidate_selection_complete") is True,
        coverage.get("complete_over_committed_sheets") is True,
        coverage.get("unprepared_sheet_count") == 0,
    ))


def _prepare_audit_aggregate_agent_runtime(
    pkg: Path | str,
    *,
    aggregate_id: str,
    model: str,
    question: str,
    limit: int = DEFAULT_LIMIT,
    max_steps: int = DEFAULT_MAX_STEPS,
    prompt_name: str = AGGREGATE_AGENT_PROMPT,
) -> _AuditAggregateAgentRuntime:
    path = Path(pkg)
    lim = _bounded_int(limit, name="limit", default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
    steps = _bounded_int(
        max_steps, name="max_steps", default=DEFAULT_MAX_STEPS, maximum=MAX_STEPS
    )
    user_question = _question(question)
    if user_question is None:
        raise AuditAggregateAgentError("aggregate conversation question이 비어 있습니다.")
    try:
        with cache.package_lock(path):
            paths, document, commit = load_audit_aggregate(path, aggregate_id)
            sources = _load_source_snapshots(path, document)
    except (AuditAggregateError, AuditAggregateStaleError) as e:
        raise AuditAggregateAgentError(f"aggregate commit 검증 실패: {e}") from e
    context = _aggregate_context(paths, document, commit)
    aggregate_records = _aggregate_record_registry(document)
    source_records, source_lookup, known = _source_record_registry(sources)
    known["statement"].update(aggregate_records)
    prompt, prompt_sha = load_prompt(prompt_name)
    schema = load_schema(AGGREGATE_AGENT_TURN_SCHEMA)
    try:
        resolver = WorkbookSourceResolver(path)
    except Exception as e:  # noqa: BLE001 - immutable ledger boundary
        raise AuditAggregateAgentError("aggregate source ledger 재조회 실패") from e
    bootstrap = _bootstrap_view(document, limit=lim)
    return _AuditAggregateAgentRuntime(
        path=path,
        paths=paths,
        aggregate=document,
        commit=commit,
        context=context,
        sources=sources,
        aggregate_records=aggregate_records,
        source_records=source_records,
        source_lookup=source_lookup,
        prompt=prompt,
        prompt_sha=prompt_sha,
        schema=schema,
        provider_schema=_provider_turn_schema(schema),
        resolver=resolver,
        known=known,
        model=model,
        question=user_question,
        limit=lim,
        max_steps=steps,
        bootstrap=bootstrap,
    )


def _assert_aggregate_agent_unchanged(runtime: _AuditAggregateAgentRuntime) -> None:
    try:
        paths, document, commit = load_audit_aggregate(
            runtime.path, runtime.paths.aggregate_id
        )
        sources = _load_source_snapshots(runtime.path, document)
    except (AuditAggregateError, AuditAggregateStaleError, AuditAggregateAgentError) as e:
        raise AuditAggregateAgentChangedError(
            "aggregate 또는 source bundle을 재검증할 수 없습니다."
        ) from e
    current = _aggregate_context(paths, document, commit)
    current_bindings = {
        scope_id: snapshot.binding for scope_id, snapshot in sources.items()
    }
    expected_bindings = {
        scope_id: snapshot.binding for scope_id, snapshot in runtime.sources.items()
    }
    if current != runtime.context or current_bindings != expected_bindings:
        raise AuditAggregateAgentChangedError(
            "aggregate conversation 실행 중 source snapshot이 변경되었습니다."
        )


def _observed_from_result(
    runtime: _AuditAggregateAgentRuntime, value: object
) -> dict[str, set[str]]:
    observed = {kind: set() for kind in _OBSERVED_KINDS}

    def visit(item: object) -> None:
        if isinstance(item, dict):
            typed = item.get("typed_kind")
            if typed == "aggregate_record":
                ref = item.get("record_ref")
                record = runtime.aggregate_records.get(ref) if isinstance(ref, str) else None
                if (
                    record is not None
                    and item == _aggregate_wrapper(record)
                ):
                    observed["statement"].add(ref)
                return
            if typed == "source_record":
                ref = item.get("source_ref")
                record = runtime.source_records.get(ref) if isinstance(ref, str) else None
                if (
                    record is not None
                    and item == _source_wrapper(record)
                ):
                    observed[record.kind].add(record.source_ref)
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return observed


def _merge_observed(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for kind in _OBSERVED_KINDS:
        target[kind].update(source[kind])


def _new_audit_aggregate_agent_turn_state(
    runtime: _AuditAggregateAgentRuntime,
) -> _AuditAgentTurnState:
    observation = {
        "tool": "aggregate_brief",
        "input": {"limit": runtime.limit},
        "result": copy.deepcopy(runtime.bootstrap),
    }
    observed = _observed_from_result(runtime, observation["result"])
    return _AuditAgentTurnState(
        observations=[observation],
        used_tools=["aggregate_brief"],
        observed=observed,
        discovery_complete=_initial_discovery_complete(runtime.bootstrap),
        seen_tool_requests=set(),
    )


def _serialize_payload(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MODEL_CONTEXT_BYTES:
        raise AuditAggregateAgentError(
            "aggregate main-agent 입력이 600KB 모델 예산을 초과했습니다. "
            "--limit을 낮추거나 질문 범위를 좁혀 주세요."
        )
    return encoded


def _request_audit_aggregate_agent_model_turn(
    runtime: _AuditAggregateAgentRuntime,
    state: _AuditAgentTurnState,
    *,
    client,
    step: int,
    child_model_calls: int = 0,
    capabilities: dict | None = None,
    eprint=None,
) -> dict:
    remaining_model_calls = max(
        0,
        runtime.max_steps - step - child_model_calls,
    )
    payload = {
        "request": {"mode": "answer", "question": runtime.question},
        "context": runtime.context,
        "trust": {
            "aggregate_review_status": runtime.aggregate["review"]["status"],
            "source_unreviewed": runtime.aggregate["trust"]["source_unreviewed"],
            "readiness": runtime.aggregate["readiness"],
        },
        "turn": step,
        "remaining_turns": remaining_model_calls,
        "remaining_model_calls": remaining_model_calls,
        "observations": state.observations,
    }
    if capabilities is not None:
        payload["capabilities"] = copy.deepcopy(capabilities)
    try:
        research_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("standards_research", {}).get("enabled") is True
        )
        planning_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("procedure_planning", {}).get("enabled") is True
        )
        inspection_enabled = (
            isinstance(capabilities, dict)
            and capabilities.get("workbook_inspection", {}).get("enabled") is True
        )
        return call_json(
            client,
            system=runtime.prompt,
            user=_serialize_payload(payload),
            schema=(
                _provider_turn_schema(
                    runtime.schema,
                    include_research=research_enabled,
                    include_planning=planning_enabled,
                    include_inspection=inspection_enabled,
                )
                if research_enabled or planning_enabled or inspection_enabled
                else runtime.provider_schema
            ),
            validation_schema=runtime.schema,
            label="audit aggregate main agent",
            retries=0,
            eprint=eprint or (lambda *args: None),
        )
    except AuditLLMError as e:
        raise AuditAggregateAgentError(str(e)) from e


def _tool_error(message: str) -> dict:
    return {"error": {"code": "INVALID_TOOL_REQUEST", "message": message}}


def _scope(runtime: _AuditAggregateAgentRuntime, scope_id: object) -> _SourceSnapshot:
    if not isinstance(scope_id, str) or scope_id not in runtime.sources:
        raise AuditAggregateAgentError(
            "scope_id는 aggregate account index에 노출된 정확한 source scope여야 합니다."
        )
    return runtime.sources[scope_id]


def _linked_source_wrappers(
    runtime: _AuditAggregateAgentRuntime, record: Mapping[str, object]
) -> list[dict]:
    scope = record.get("scope")
    scope_id = scope.get("id") if isinstance(scope, Mapping) else None
    if not isinstance(scope_id, str) or scope_id not in runtime.sources:
        raise AuditAggregateAgentError("aggregate record scope가 source manifest에 없습니다.")
    linked: list[dict] = []
    values = [
        ("statement", record.get("source_id")),
        *(("fact", value) for value in record.get("fact_ids", [])),
        *(("relation", value) for value in record.get("relation_ids", [])),
        *(("standard_citation", value) for value in record.get("standard_citation_ids", [])),
    ]
    seen: set[str] = set()
    for kind, source_id in values:
        if not isinstance(source_id, str):
            continue
        ref = runtime.source_lookup.get((scope_id, kind, source_id))
        if ref is not None and ref not in seen:
            seen.add(ref)
            linked.append(_source_wrapper(runtime.source_records[ref]))
    return linked


def _aggregate_search(
    runtime: _AuditAggregateAgentRuntime, *, query: str, kind: str | None, limit: int
) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise AuditAggregateAgentError("aggregate_search에는 query가 필요합니다.")
    if kind is not None and kind not in _AGGREGATE_KINDS:
        raise AuditAggregateAgentError("aggregate_search kind가 유효하지 않습니다.")
    folded = query.casefold()
    matches = [
        record for record in runtime.aggregate_records.values()
        if (kind is None or record.get("kind") == kind)
        and folded in "\n".join(str(record.get(field) or "") for field in (
            "text", "section", "type", "status", "severity"
        )).casefold()
    ]
    return {
        "query": query,
        "kind": kind,
        "returned": min(len(matches), limit),
        "total_matches": len(matches),
        "truncated": len(matches) > limit,
        "matches": [_aggregate_wrapper(record) for record in matches[:limit]],
    }


def _wrap_search_result(
    runtime: _AuditAggregateAgentRuntime,
    scope_id: str,
    result: dict,
) -> dict:
    matches: list[dict] = []
    for match in result["matches"]:
        kind = match.get("kind")
        item = match.get("item")
        source_id = item.get("id") if isinstance(item, Mapping) else None
        ref = runtime.source_lookup.get((scope_id, kind, source_id))
        if ref is None:
            raise AuditAggregateAgentError("source_search 결과를 scope-qualified ref로 만들 수 없습니다.")
        matches.append(_source_wrapper(runtime.source_records[ref]))
    return {
        "scope": runtime.sources[scope_id].scope.identity(),
        "query": result["query"],
        "kind": result["kind"],
        "returned": result["returned"],
        "total_matches": result["total_matches"],
        "truncated": result["truncated"],
        "matches": matches,
    }


def _wrap_assertion_result(
    runtime: _AuditAggregateAgentRuntime,
    scope_id: str,
    result: dict,
) -> dict:
    def wrapper(kind: str, item: Mapping[str, object]) -> dict:
        source_id = item.get("id")
        ref = runtime.source_lookup.get((scope_id, kind, source_id))
        if ref is None:
            raise AuditAggregateAgentError(
                "assertion_procedures 결과를 scope-qualified ref로 만들 수 없습니다."
            )
        return _source_wrapper(runtime.source_records[ref])

    pairs: list[dict] = []
    for pair in result["pairs"]:
        pairs.append({
            "assertion": wrapper("fact", pair["assertion"]),
            "procedure": wrapper("fact", pair["procedure"]),
            "mapping_status": pair["mapping_status"],
            "test_relations": [wrapper("relation", item) for item in pair["test_relations"]],
            "produced_facts": [wrapper("fact", item) for item in pair["produced_facts"]],
            "produces_relations": [
                wrapper("relation", item) for item in pair["produces_relations"]
            ],
            "truncated": pair["truncated"],
        })
    return {
        "scope": runtime.sources[scope_id].scope.identity(),
        **{
            key: copy.deepcopy(value)
            for key, value in result.items()
            if key not in {
                "pairs", "unpaired_assertions", "unpaired_procedures", "trace_ids",
                "facts_review_status", "facts_reviewed_at", "facts_review_note",
                "facts_review_note_truncated", "facts_unreviewed", "review_status",
                "reviewed_at", "review_note", "review_note_truncated", "unreviewed",
            }
        },
        "pairs": pairs,
        "unpaired_assertions": [
            wrapper("fact", item) for item in result["unpaired_assertions"]
        ],
        "unpaired_procedures": [
            wrapper("fact", item) for item in result["unpaired_procedures"]
        ],
    }


def _trace_source(
    runtime: _AuditAggregateAgentRuntime,
    record: _SourceRecord,
    *,
    limit: int | None = None,
) -> dict:
    snapshot = runtime.sources[record.scope.id]
    result = _trace_loaded(
        runtime.path,
        snapshot.facts,
        snapshot.standards,
        snapshot.brief,
        item_id=record.source_id,
        limit=runtime.limit if limit is None else limit,
        resolver=runtime.resolver,
    )
    return {
        "selection": _source_wrapper(record),
        "trace": result,
        "trace_complete": not result.get("truncated", False),
    }


def _trace_aggregate(
    runtime: _AuditAggregateAgentRuntime,
    record: dict,
    *,
    limit: int | None = None,
) -> dict:
    scope_id = record["scope"]["id"]
    snapshot = runtime.sources.get(scope_id)
    if snapshot is None or snapshot.scope.identity() != record["scope"]:
        raise AuditAggregateAgentError("aggregate record가 정확한 source scope에 매핑되지 않습니다.")
    source_id = record.get("source_id")
    source_record: dict | None = None
    trace: dict | None = None
    if record.get("kind") == "statement" and isinstance(source_id, str):
        ref = runtime.source_lookup.get((scope_id, "statement", source_id))
        if ref is None:
            raise AuditAggregateAgentError("aggregate statement source가 없습니다.")
        source = runtime.source_records[ref]
        source_record = _source_wrapper(source)
        trace = _trace_source(runtime, source, limit=limit)["trace"]
    elif record.get("kind") == "limitation" and isinstance(source_id, str):
        located = _audit_get_loaded(
            snapshot.facts, snapshot.standards, snapshot.brief, item_id=source_id
        )
        if located.get("kind") != "brief_limitation":
            raise AuditAggregateAgentError("aggregate limitation source kind가 다릅니다.")
        source_record = {
            "typed_kind": "source_record_metadata",
            "scope": snapshot.scope.identity(),
            "kind": located["kind"],
            "source_id": source_id,
            "item": copy.deepcopy(located["item"]),
        }
        trace = {"record_only": True, "item": copy.deepcopy(located["item"])}
    elif record.get("kind") == "readiness_reason" and source_id is None:
        reasons = snapshot.brief.get("readiness", {}).get("reasons", [])
        if record.get("text") not in reasons:
            raise AuditAggregateAgentError("aggregate readiness reason source가 없습니다.")
        trace = {"record_only": True, "reason": record["text"]}
    else:
        raise AuditAggregateAgentError("지원하지 않는 aggregate record source입니다.")
    return {
        "selection": _aggregate_wrapper(record),
        "source_record": source_record,
        "trace": trace,
        "trace_complete": not bool(trace and trace.get("truncated")),
    }


def _execute_tool(
    runtime: _AuditAggregateAgentRuntime,
    request: Mapping[str, object],
    observed: Mapping[str, set[str]],
    *,
    maximum_limit: int | None = None,
) -> dict:
    try:
        jsonschema.validate(
            dict(request),
            {
                "$schema": runtime.schema.get(
                    "$schema",
                    "http://json-schema.org/draft-07/schema#",
                ),
                "$ref": "#/definitions/toolRequest",
                "definitions": runtime.schema["definitions"],
            },
        )
    except (jsonschema.ValidationError, KeyError) as e:
        raise AuditAggregateAgentError(
            "aggregate tool request schema가 유효하지 않습니다."
        ) from e
    name = request.get("name")
    query = request.get("query")
    kind = request.get("kind")
    item_ref = request.get("item_ref")
    scope_id = request.get("scope_id")
    requested_limit = request.get("limit")
    if not isinstance(requested_limit, int) or isinstance(requested_limit, bool):
        raise AuditAggregateAgentError("tool.limit은 정수여야 합니다.")
    limit = min(
        requested_limit,
        runtime.limit if maximum_limit is None else maximum_limit,
    )
    if name == "aggregate_search":
        if item_ref is not None or scope_id is not None:
            raise AuditAggregateAgentError("aggregate_search에는 item_ref/scope_id를 사용할 수 없습니다.")
        return _aggregate_search(runtime, query=query, kind=kind, limit=limit)
    if name == "aggregate_get":
        if query is not None or kind is not None or scope_id is not None:
            raise AuditAggregateAgentError("aggregate_get에는 query/kind/scope_id를 사용할 수 없습니다.")
        if not isinstance(item_ref, str) or item_ref not in observed["statement"]:
            raise AuditAggregateAgentError("aggregate_get ref는 관찰된 aggregate record여야 합니다.")
        record = runtime.aggregate_records.get(item_ref)
        if record is None:
            raise AuditAggregateAgentError("aggregate_get ref가 aggregate record가 아닙니다.")
        return {
            "record": _aggregate_wrapper(record),
            "linked_source_records": _linked_source_wrappers(runtime, record),
        }
    if name == "source_search":
        if item_ref is not None or not isinstance(query, str) or not query.strip():
            raise AuditAggregateAgentError("source_search에는 scope_id/query가 필요합니다.")
        if kind is not None and kind not in AUDIT_SEARCH_KINDS:
            raise AuditAggregateAgentError("source_search kind가 유효하지 않습니다.")
        snapshot = _scope(runtime, scope_id)
        result = _audit_search_loaded(
            snapshot.facts, snapshot.brief, query=query, kind=kind, limit=limit
        )
        return _wrap_search_result(runtime, snapshot.scope.id, result)
    if name == "assertion_procedures":
        if item_ref is not None or kind is not None:
            raise AuditAggregateAgentError(
                "assertion_procedures에는 item_ref/kind를 사용할 수 없습니다."
            )
        snapshot = _scope(runtime, scope_id)
        result = _assertion_procedures_loaded(
            snapshot.facts, snapshot.brief, query=query, limit=limit
        )
        return _wrap_assertion_result(runtime, snapshot.scope.id, result)
    if name == "source_get":
        if query is not None or kind is not None or scope_id is not None:
            raise AuditAggregateAgentError("source_get에는 query/kind/scope_id를 사용할 수 없습니다.")
        record = runtime.source_records.get(item_ref) if isinstance(item_ref, str) else None
        if record is None or item_ref not in observed[record.kind]:
            raise AuditAggregateAgentError("source_get ref는 관찰된 source record여야 합니다.")
        snapshot = runtime.sources[record.scope.id]
        located = _audit_get_loaded(
            snapshot.facts, snapshot.standards, snapshot.brief, item_id=record.source_id
        )
        if located.get("kind") != record.kind or located.get("item") != record.item:
            raise AuditAggregateAgentError("source_get record가 runtime registry와 다릅니다.")
        return {"record": _source_wrapper(record)}
    if name == "trace":
        if query is not None or kind is not None or scope_id is not None:
            raise AuditAggregateAgentError("trace에는 query/kind/scope_id를 사용할 수 없습니다.")
        if isinstance(item_ref, str) and item_ref in runtime.aggregate_records:
            if item_ref not in observed["statement"]:
                raise AuditAggregateAgentError("trace ref는 관찰된 aggregate record여야 합니다.")
            return _trace_aggregate(
                runtime,
                runtime.aggregate_records[item_ref],
                limit=limit,
            )
        source = runtime.source_records.get(item_ref) if isinstance(item_ref, str) else None
        if source is None or item_ref not in observed[source.kind]:
            raise AuditAggregateAgentError("trace ref는 관찰된 source record여야 합니다.")
        return _trace_source(runtime, source, limit=limit)
    raise AuditAggregateAgentError(f"지원하지 않는 aggregate agent tool입니다: {name!r}")


def _result_complete(name: str, result: object) -> bool:
    if not isinstance(result, Mapping) or "error" in result:
        return False
    if result.get("truncated") is True:
        return False
    if name == "assertion_procedures":
        return not any((
            result.get("trace_ids_truncated") is True,
            any(
                isinstance(pair, Mapping) and pair.get("truncated") is True
                for pair in result.get("pairs", [])
            ),
        ))
    if name == "trace":
        return result.get("trace_complete") is True
    return True


def _focus_observation_authorization(
    runtime: _AuditAggregateAgentRuntime,
    observation: Mapping[str, object],
) -> dict[str, set[str]]:
    expected_input = {
        "max_turns": MAX_PERSISTED_FOCUS_TURNS,
        "max_records": MAX_PERSISTED_FOCUS_RECORDS,
    }
    result = observation.get("result")
    if (
        set(observation) != {"tool", "input", "result"}
        or observation.get("input") != expected_input
        or not isinstance(result, Mapping)
        or set(result) != {"prior_turns", "records", "authorization"}
        or result.get("authorization") != (
            "Only exact refs in typed records are evidence for this turn; refs and "
            "local IDs in prior question or answer prose are not authorized."
        )
    ):
        raise AuditAggregateAgentError(
            "aggregate conversation focus witness가 유효하지 않습니다."
        )
    prior_turns = result.get("prior_turns")
    records = result.get("records")
    if (
        not isinstance(prior_turns, list)
        or not 1 <= len(prior_turns) <= MAX_PERSISTED_FOCUS_TURNS
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"turn_index", "question", "answer"}
            or not isinstance(item.get("turn_index"), int)
            or isinstance(item.get("turn_index"), bool)
            or item["turn_index"] < 1
            or not isinstance(item.get("question"), str)
            or not isinstance(item.get("answer"), Mapping)
            for item in prior_turns
        )
        or not isinstance(records, list)
        or len(records) > MAX_PERSISTED_FOCUS_RECORDS
    ):
        raise AuditAggregateAgentError(
            "aggregate conversation focus payload가 유효하지 않습니다."
        )
    if any(
        any(_observed_from_result(runtime, item).values())
        for item in prior_turns
    ):
        raise AuditAggregateAgentError(
            "aggregate conversation focus prose가 typed ref를 권한으로 만듭니다."
        )
    observed = {kind: set() for kind in _OBSERVED_KINDS}
    seen: set[str] = set()
    for wrapper in records:
        candidate = _observed_from_result(runtime, wrapper)
        refs = [
            (kind, ref)
            for kind in _OBSERVED_KINDS
            for ref in candidate[kind]
        ]
        if len(refs) != 1 or refs[0][1] in seen:
            raise AuditAggregateAgentError(
                "aggregate conversation focus record가 exact typed wrapper가 아닙니다."
            )
        kind, ref = refs[0]
        seen.add(ref)
        observed[kind].add(ref)
    return observed


def _aggregate_observation_witness(
    runtime: _AuditAggregateAgentRuntime,
    observations: object,
    *,
    expected_limit: object = None,
    expected_focus: Mapping[str, object] | None = None,
) -> tuple[bool, dict[str, set[str]], list[str], int]:
    """Reconstruct discovery and typed authorization from persisted observations."""
    if (
        not isinstance(expected_limit, int)
        or isinstance(expected_limit, bool)
        or not 1 <= expected_limit <= MAX_LIMIT
    ):
        raise AuditAggregateAgentError(
            "aggregate observations limit witness가 유효하지 않습니다."
        )
    if not isinstance(observations, list) or any(
        not isinstance(item, Mapping) for item in observations
    ):
        raise AuditAggregateAgentError("aggregate observations witness가 유효하지 않습니다.")
    if not observations:
        raise AuditAggregateAgentError("aggregate observations bootstrap witness가 없습니다.")
    expected_bootstrap = _bootstrap_view(runtime.aggregate, limit=expected_limit)
    complete = _initial_discovery_complete(expected_bootstrap)
    observed = {kind: set() for kind in _OBSERVED_KINDS}
    used_tools = ["aggregate_brief"]
    decision_observation_count = 0
    seen_tool_requests: set[str] = set()
    saw_focus = False
    for index, observation in enumerate(observations):
        tool = observation.get("tool")
        if tool == "aggregate_brief":
            if (
                index != 0
                or set(observation) != {"tool", "input", "result"}
                or observation.get("input") != {"limit": expected_limit}
                or observation.get("result") != expected_bootstrap
            ):
                raise AuditAggregateAgentError(
                    "aggregate observations bootstrap witness가 runtime과 다릅니다."
                )
            _merge_observed(
                observed,
                _observed_from_result(runtime, expected_bootstrap),
            )
        elif index == 0:
            raise AuditAggregateAgentError(
                "aggregate observations bootstrap witness가 첫 기록이 아닙니다."
            )
        elif tool == "conversation_focus":
            if (
                saw_focus
                or index != 1
                or expected_focus is None
                or observation != expected_focus
            ):
                raise AuditAggregateAgentError(
                    "aggregate conversation focus witness가 prior turn 기록과 다릅니다."
                )
            saw_focus = True
            _merge_observed(
                observed,
                _focus_observation_authorization(runtime, observation),
            )
        elif tool == "answer_validation":
            decision_observation_count += 1
            if (
                set(observation) != {"tool", "result"}
                or not isinstance(observation.get("result"), Mapping)
                or set(observation["result"]) != {"error"}
                or any(_observed_from_result(runtime, observation["result"]).values())
            ):
                raise AuditAggregateAgentError(
                    "aggregate answer validation witness가 유효하지 않습니다."
                )
        elif tool == "agent_protocol":
            decision_observation_count += 1
            if observation not in ({
                "tool": "agent_protocol",
                "result": _tool_error(
                    "action=tool에는 tool 객체와 final=null이 필요합니다."
                ),
            }, {
                "tool": "agent_protocol",
                "result": _tool_error(
                    "action=final에는 tool=null과 final 객체가 필요합니다."
                ),
            }):
                raise AuditAggregateAgentError(
                    "aggregate agent protocol witness가 유효하지 않습니다."
                )
            complete = False
        else:
            decision_observation_count += 1
            request = observation.get("input")
            if not isinstance(tool, str) or not isinstance(request, Mapping):
                raise AuditAggregateAgentError(
                    "aggregate tool observation witness가 유효하지 않습니다."
                )
            request_key = json.dumps(request, ensure_ascii=False, sort_keys=True)
            if request_key in seen_tool_requests:
                expected_result = _tool_error(
                    "동일한 tool request를 반복할 수 없습니다."
                )
            else:
                seen_tool_requests.add(request_key)
                try:
                    expected_result = _execute_tool(
                        runtime,
                        request,
                        observed,
                        maximum_limit=expected_limit,
                    )
                except (AuditAggregateAgentError, AuditConsumeError) as e:
                    expected_result = _tool_error(str(e))
            expected_observation = {
                "tool": str(request.get("name")),
                "input": request,
                "result": expected_result,
            }
            if observation != expected_observation:
                raise AuditAggregateAgentError(
                    "aggregate tool result witness가 deterministic replay와 다릅니다."
                )
            complete = complete and _result_complete(tool, expected_result)
            used_tools.append(tool)
            _merge_observed(
                observed,
                _observed_from_result(runtime, expected_result),
            )
    if (expected_focus is not None) != saw_focus:
        raise AuditAggregateAgentError(
            "aggregate conversation focus witness 유무가 prior turn 기록과 다릅니다."
        )
    return complete, observed, used_tools, decision_observation_count + 1


def _aggregate_discovery_complete(
    runtime: _AuditAggregateAgentRuntime,
    observations: object,
    *,
    expected_limit: object = None,
    expected_focus: Mapping[str, object] | None = None,
) -> bool:
    """Compatibility projection of the complete persisted observation witness."""
    complete, _, _, _ = _aggregate_observation_witness(
        runtime,
        observations,
        expected_limit=expected_limit,
        expected_focus=expected_focus,
    )
    return complete


def _apply_audit_aggregate_agent_tool_turn(
    runtime: _AuditAggregateAgentRuntime,
    state: _AuditAgentTurnState,
    turn: dict,
) -> None:
    if not isinstance(turn.get("tool"), dict) or turn.get("final") is not None:
        state.observations.append({
            "tool": "agent_protocol",
            "result": _tool_error("action=tool에는 tool 객체와 final=null이 필요합니다."),
        })
        state.discovery_complete = False
        return
    request = turn["tool"]
    request_key = json.dumps(request, ensure_ascii=False, sort_keys=True)
    if request_key in state.seen_tool_requests:
        result = _tool_error("동일한 tool request를 반복할 수 없습니다.")
    else:
        state.seen_tool_requests.add(request_key)
        try:
            result = _execute_tool(runtime, request, state.observed)
        except (AuditAggregateAgentError, AuditConsumeError) as e:
            result = _tool_error(str(e))
    tool_name = str(request.get("name"))
    state.observations.append({"tool": tool_name, "input": request, "result": result})
    state.used_tools.append(tool_name)
    _merge_observed(state.observed, _observed_from_result(runtime, result))
    state.discovery_complete = state.discovery_complete and _result_complete(tool_name, result)


def _sanitize_plan(value: Mapping[str, object]) -> dict:
    return {
        "abstained": value.get("abstained"),
        "abstention_code": value.get("abstention_code"),
        "selections": [
            {"kind": item.get("kind"), "refs": list(item.get("refs", []))}
            for item in value.get("selections", [])
            if isinstance(item, Mapping)
        ],
    }


def _validate_plan(
    runtime: _AuditAggregateAgentRuntime,
    state: _AuditAgentTurnState,
    plan: dict,
) -> list[str]:
    problems: list[str] = []
    selections = plan["selections"]
    if plan["abstained"]:
        if plan["abstention_code"] not in _ABSTENTION_REASONS:
            problems.append("abstained=true이면 유효한 abstention_code가 필요합니다.")
        if selections:
            problems.append("abstained=true이면 selections는 비어 있어야 합니다.")
    else:
        if plan["abstention_code"] is not None:
            problems.append("abstained=false이면 abstention_code는 null이어야 합니다.")
        if not selections:
            problems.append("abstained=false이면 selection이 필요합니다.")
    selected: set[str] = set()
    for index, selection in enumerate(selections):
        kind = selection["kind"]
        for ref in selection["refs"]:
            if ref in selected:
                problems.append(f"selections[{index}]에 중복 ref가 있습니다: {ref}")
                continue
            selected.add(ref)
            if kind == "aggregate_record":
                if ref not in runtime.aggregate_records:
                    problems.append(f"selections[{index}] unknown aggregate ref: {ref}")
                elif ref not in state.observed["statement"]:
                    problems.append(f"selections[{index}] unobserved aggregate ref: {ref}")
            elif kind == "source_record":
                record = runtime.source_records.get(ref)
                if record is None:
                    problems.append(f"selections[{index}] unknown source ref: {ref}")
                elif ref not in state.observed[record.kind]:
                    problems.append(f"selections[{index}] unobserved source ref: {ref}")
            else:
                problems.append(f"selections[{index}] kind가 유효하지 않습니다: {kind!r}")
    if len(selected) > MAX_SELECTED_RECORDS:
        problems.append(f"선택 record 수가 상한 {MAX_SELECTED_RECORDS}을 초과합니다.")
    return problems


def _basis(value: object) -> str:
    return value if value in {
        "documented_fact", "authoritative_context", "synthesis", "gap"
    } else "gap"


def _source_claim(runtime: _AuditAggregateAgentRuntime, record: _SourceRecord) -> dict:
    item = record.item
    if record.kind == "statement":
        text = item.get("text")
        section = item.get("section") or "answer"
        basis = _basis(item.get("type"))
        status = item.get("status")
        confidence = item.get("confidence")
    elif record.kind == "fact":
        text = item.get("description") or item.get("value") or item.get("normalized_code")
        section = "answer"
        basis = "documented_fact"
        status = item.get("status") or "documented"
        confidence = item.get("confidence")
    elif record.kind == "relation":
        snapshot = runtime.sources[record.scope.id]
        facts = {
            fact.get("id"): fact for fact in snapshot.facts.get("facts", [])
            if isinstance(fact, Mapping)
        }
        left = facts.get(item.get("from_fact_id"), {})
        right = facts.get(item.get("to_fact_id"), {})
        text = (
            f"{left.get('description') or item.get('from_fact_id')} "
            f"--{item.get('type')}--> "
            f"{right.get('description') or item.get('to_fact_id')}"
        )
        section = "procedures"
        basis = "documented_fact"
        status = item.get("status") or "unknown"
        confidence = item.get("confidence")
    else:
        text = item.get("snippet") or item.get("cid") or record.source_id
        section = "standards"
        basis = "authoritative_context"
        status = "verified"
        confidence = 1.0
    return {
        "section": str(section),
        "text": _bounded_text(text, 8_000),
        "basis": basis,
        "status": status if isinstance(status, str) else None,
        "confidence": confidence if isinstance(confidence, (int, float)) else None,
        "scope": record.scope.identity(),
        "aggregate_record_refs": [],
        "source_record_refs": [record.source_ref],
    }


def _aggregate_claim(record: dict) -> dict:
    return {
        "section": str(record.get("section") or "answer"),
        "text": _bounded_text(record.get("text"), 8_000),
        "basis": _basis(record.get("type")),
        "status": record.get("status") if isinstance(record.get("status"), str) else None,
        "confidence": (
            record.get("confidence")
            if isinstance(record.get("confidence"), (int, float)) else None
        ),
        "scope": copy.deepcopy(record["scope"]),
        "aggregate_record_refs": [record["record_ref"]],
        "source_record_refs": [],
    }


def _evidence_for_aggregate(
    runtime: _AuditAggregateAgentRuntime,
    record: dict,
    *,
    limit: int | None = None,
) -> dict:
    hydrated = _trace_aggregate(runtime, record, limit=limit)
    source_record = hydrated.get("source_record")
    source_kind = (
        source_record.get("kind") if isinstance(source_record, Mapping)
        else str(record.get("kind"))
    )
    return {
        "selection_ref": record["record_ref"],
        "selection_kind": "aggregate_record",
        "scope": copy.deepcopy(record["scope"]),
        "source_kind": str(source_kind),
        "source_id": record.get("source_id"),
        "aggregate_record": copy.deepcopy(record),
        "source_record": copy.deepcopy(source_record),
        "source_binding": copy.deepcopy(runtime.sources[record["scope"]["id"]].binding),
        "trace": copy.deepcopy(hydrated["trace"]),
        "trace_complete": hydrated["trace_complete"],
    }


def _evidence_for_source(
    runtime: _AuditAggregateAgentRuntime,
    record: _SourceRecord,
    *,
    limit: int | None = None,
) -> dict:
    hydrated = _trace_source(runtime, record, limit=limit)
    return {
        "selection_ref": record.source_ref,
        "selection_kind": "source_record",
        "scope": record.scope.identity(),
        "source_kind": record.kind,
        "source_id": record.source_id,
        "aggregate_record": None,
        "source_record": copy.deepcopy(record.item),
        "source_binding": copy.deepcopy(runtime.sources[record.scope.id].binding),
        "trace": copy.deepcopy(hydrated["trace"]),
        "trace_complete": hydrated["trace_complete"],
    }


def _selected_refs(plan: Mapping[str, object]) -> list[tuple[str, str]]:
    return [
        (selection["kind"], ref)
        for selection in plan["selections"]
        for ref in selection["refs"]
    ]


def _evidence_cell_count(evidence: list[dict]) -> int:
    return sum(
        len(item.get("trace", {}).get("cells", []))
        for item in evidence
        if isinstance(item.get("trace"), Mapping)
    )


def _notices(runtime: _AuditAggregateAgentRuntime, *, complete: bool) -> list[dict]:
    coverage = runtime.aggregate["coverage"]
    notices: list[dict] = []
    if runtime.aggregate["trust"]["source_unreviewed"]:
        notices.append({
            "code": "SOURCE_UNREVIEWED",
            "severity": "moderate",
            "text": "하나 이상의 source sheet bundle이 승인되지 않았습니다.",
        })
    if runtime.aggregate["readiness"]["status"] != "ready":
        notices.append({
            "code": "AGGREGATE_PARTIAL",
            "severity": "moderate",
            "text": "계정별 aggregate 준비 상태가 partial입니다.",
        })
    if not coverage["complete_over_committed_sheets"]:
        notices.append({
            "code": "SELECTION_SUBSET",
            "severity": "moderate",
            "text": "이 aggregate는 commit된 모든 시트를 포함하지 않습니다.",
        })
    if coverage["unprepared_sheet_count"]:
        notices.append({
            "code": "UNPREPARED_SHEETS",
            "severity": "moderate",
            "text": "아직 독립 prepare가 완료되지 않은 workbook 시트가 있습니다.",
        })
    if not coverage["candidate_selection_complete"]:
        notices.append({
            "code": "CANDIDATE_TRUNCATED",
            "severity": "moderate",
            "text": "aggregate 후보 상한으로 일부 source brief record가 제외되었습니다.",
        })
    if not complete:
        notices.append({
            "code": "COVERAGE_INCOMPLETE",
            "severity": "moderate",
            "text": "현재 답변은 aggregate 또는 근거 추적 범위 전체를 대표하지 않습니다.",
        })
    return notices


def _validate_response_semantics(
    runtime: _AuditAggregateAgentRuntime, document: dict
) -> None:
    if document["context"] != runtime.context:
        raise AuditAggregateAgentError("main-agent response context가 runtime과 다릅니다.")
    trust = document["trust"]
    expected_trust = runtime.aggregate["trust"]
    if trust["all_sources_approved"] != expected_trust["all_sources_approved"]:
        raise AuditAggregateAgentError("main-agent source approval 집계가 다릅니다.")
    if trust["source_unreviewed"] != expected_trust["source_unreviewed"]:
        raise AuditAggregateAgentError("main-agent source review 집계가 다릅니다.")
    if trust["readiness"] != runtime.aggregate["readiness"]:
        raise AuditAggregateAgentError("main-agent readiness가 aggregate와 다릅니다.")
    if document["package"] != runtime.path.name:
        raise AuditAggregateAgentError("main-agent package identity가 runtime과 다릅니다.")
    answer = document["answer"]
    if answer["title"] != "계정별 감사조서 질의 답변":
        raise AuditAggregateAgentError("main-agent answer title이 코드 생성값과 다릅니다.")
    if answer["suggested_questions"] != []:
        raise AuditAggregateAgentError(
            "main-agent suggested questions가 코드 생성값과 다릅니다."
        )
    if answer["abstained"]:
        if answer["abstention_reason"] not in set(_ABSTENTION_REASONS.values()):
            raise AuditAggregateAgentError("main-agent abstention reason이 유효하지 않습니다.")
    elif answer["abstention_reason"] is not None:
        raise AuditAggregateAgentError("main-agent non-abstained reason은 null이어야 합니다.")
    evidence = document["evidence"]["records"]
    evidence_refs = [item["selection_ref"] for item in evidence]
    if len(evidence_refs) != len(set(evidence_refs)):
        raise AuditAggregateAgentError("main-agent evidence ref가 중복되었습니다.")
    claim_refs = [
        ref for claim in document["answer"]["claims"]
        for field in ("aggregate_record_refs", "source_record_refs")
        for ref in claim[field]
    ]
    if len(claim_refs) != len(set(claim_refs)):
        raise AuditAggregateAgentError("main-agent claim ref가 중복되었습니다.")
    if set(claim_refs) != set(evidence_refs):
        raise AuditAggregateAgentError("main-agent claims와 evidence ref가 일치하지 않습니다.")
    for item in evidence:
        scope_id = item["scope"]["id"]
        snapshot = runtime.sources.get(scope_id)
        if snapshot is None or item["scope"] != snapshot.scope.identity():
            raise AuditAggregateAgentError("main-agent evidence source scope가 유효하지 않습니다.")
        if item["source_binding"] != snapshot.binding:
            raise AuditAggregateAgentError("main-agent evidence source binding이 다릅니다.")
        selection_ref = item["selection_ref"]
        if item["selection_kind"] == "aggregate_record":
            record = runtime.aggregate_records.get(selection_ref)
            if (
                record is None
                or item["aggregate_record"] != record
                or item["source_id"] != record.get("source_id")
                or item["scope"] != record.get("scope")
            ):
                raise AuditAggregateAgentError(
                    "main-agent aggregate evidence가 runtime registry와 다릅니다."
                )
        else:
            record = runtime.source_records.get(selection_ref)
            if (
                record is None
                or item["aggregate_record"] is not None
                or item["source_record"] != record.item
                or item["source_kind"] != record.kind
                or item["source_id"] != record.source_id
                or item["scope"] != record.scope.identity()
            ):
                raise AuditAggregateAgentError(
                    "main-agent source evidence가 runtime registry와 다릅니다."
                )
        trace = item.get("trace")
        if isinstance(trace, Mapping):
            for field in ("cells", "relation_direct_cells", "endpoint_cells"):
                for cell in trace.get(field, []):
                    if isinstance(cell, Mapping) and cell.get("sheet") != snapshot.scope.sheet:
                        raise AuditAggregateAgentError(
                            "main-agent evidence가 source scope 밖 cell을 포함합니다."
                        )
    response_limit = document["generator"]["limit"]
    expected_claims: list[dict] = []
    expected_evidence: list[dict] = []
    for item in evidence:
        selection_ref = item["selection_ref"]
        if item["selection_kind"] == "aggregate_record":
            record = runtime.aggregate_records[selection_ref]
            expected_claims.append(_aggregate_claim(record))
            expected_evidence.append(
                _evidence_for_aggregate(runtime, record, limit=response_limit)
            )
        else:
            record = runtime.source_records[selection_ref]
            expected_claims.append(_source_claim(runtime, record))
            expected_evidence.append(
                _evidence_for_source(runtime, record, limit=response_limit)
            )
    if document["answer"]["claims"] != expected_claims:
        raise AuditAggregateAgentError(
            "main-agent claim materialization이 exact source record와 다릅니다."
        )
    if evidence != expected_evidence:
        raise AuditAggregateAgentError(
            "main-agent evidence hydration이 exact source trace와 다릅니다."
        )
    cell_count = _evidence_cell_count(evidence)
    if cell_count > MAX_EVIDENCE_CELLS:
        raise AuditAggregateAgentError(
            f"main-agent cell evidence가 전역 상한 {MAX_EVIDENCE_CELLS}을 초과합니다."
        )
    coverage = document["coverage"]
    evidence_complete = bool(evidence) and all(
        item["trace_complete"] for item in evidence
    )
    if coverage["record_count"] != len(evidence):
        raise AuditAggregateAgentError("main-agent evidence count가 coverage와 다릅니다.")
    if coverage["evidence_complete"] != evidence_complete:
        raise AuditAggregateAgentError("main-agent evidence completeness가 다릅니다.")
    if coverage["complete"] != (
        coverage["discovery_complete"] and coverage["evidence_complete"]
    ):
        raise AuditAggregateAgentError("main-agent complete 계산이 다릅니다.")
    historical_bootstrap = _bootstrap_view(
        runtime.aggregate,
        limit=response_limit,
    )
    if coverage["discovery_complete"] and not _initial_discovery_complete(
        historical_bootstrap
    ):
        raise AuditAggregateAgentError(
            "불완전한 aggregate bootstrap을 discovery complete로 승격할 수 없습니다."
        )
    aggregate_coverage = runtime.aggregate["coverage"]
    expected_coverage = {
        "aggregate_candidate_selection_complete": aggregate_coverage[
            "candidate_selection_complete"
        ],
        "complete_over_committed_sheets": aggregate_coverage[
            "complete_over_committed_sheets"
        ],
        "unprepared_sheet_count": aggregate_coverage["unprepared_sheet_count"],
    }
    for field, expected in expected_coverage.items():
        if coverage[field] != expected:
            raise AuditAggregateAgentError(
                f"main-agent coverage.{field}가 aggregate와 다릅니다."
            )
    if coverage["scope_count"] != len({item["scope"]["id"] for item in evidence}):
        raise AuditAggregateAgentError("main-agent evidence scope count가 다릅니다.")
    if document["package_limitations"] != runtime.aggregate["limitations"]:
        raise AuditAggregateAgentError("main-agent package limitations가 aggregate와 다릅니다.")
    expected_notices = _notices(runtime, complete=coverage["complete"])
    if document["notices"] != expected_notices:
        raise AuditAggregateAgentError("main-agent notices가 코드 생성값과 다릅니다.")
    generator = document["generator"]
    if generator["version"] != AGGREGATE_AGENT_VERSION:
        raise AuditAggregateAgentError("main-agent generator identity가 runtime과 다릅니다.")


def _validated_aggregate_response(
    runtime: _AuditAggregateAgentRuntime,
    document: dict,
    *,
    expected_discovery_complete: bool | None = None,
    expected_observed: Mapping[str, set[str]] | None = None,
    expected_generator: Mapping[str, object] | None = None,
) -> dict:
    digest = json_sha256(document)
    witness_presence = (
        expected_discovery_complete is not None,
        expected_observed is not None,
        expected_generator is not None,
    )
    if any(witness_presence) and not all(witness_presence):
        raise AuditAggregateAgentError(
            "main-agent persisted response witness가 완전하지 않습니다."
        )
    observed_digest = "none"
    if expected_observed is not None:
        if set(expected_observed) != set(_OBSERVED_KINDS):
            raise AuditAggregateAgentError(
                "main-agent observed authorization witness가 유효하지 않습니다."
            )
        normalized_observed: dict[str, list[str]] = {}
        for kind in _OBSERVED_KINDS:
            values = expected_observed.get(kind)
            if not isinstance(values, set) or any(
                not isinstance(value, str) for value in values
            ):
                raise AuditAggregateAgentError(
                    "main-agent observed authorization witness가 유효하지 않습니다."
                )
            normalized_observed[kind] = sorted(values)
        observed_digest = json_sha256(normalized_observed)
    execution_digest = (
        "none"
        if expected_generator is None
        else json_sha256(dict(expected_generator))
    )
    cache_key = (
        f"{digest}:{expected_discovery_complete!r}:"
        f"{observed_digest}:{execution_digest}"
    )
    cached = runtime.response_cache.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    try:
        jsonschema.validate(document, load_schema(AGGREGATE_AGENT_RESPONSE_SCHEMA))
    except (jsonschema.ValidationError, AuditLLMError) as e:
        raise AuditAggregateAgentError(f"main-agent response schema 불일치: {e}") from e
    _validate_response_semantics(runtime, document)
    if (
        expected_discovery_complete is not None
        and document["coverage"]["discovery_complete"]
        is not expected_discovery_complete
    ):
        raise AuditAggregateAgentError(
            "main-agent discovery coverage가 observations witness와 다릅니다."
        )
    if expected_observed is not None:
        for item in document["evidence"]["records"]:
            kind = (
                "statement"
                if item["selection_kind"] == "aggregate_record"
                else item["source_kind"]
            )
            if item["selection_ref"] not in expected_observed[kind]:
                raise AuditAggregateAgentError(
                    "main-agent evidence ref가 observations authorization witness에 "
                    "없습니다."
                )
    if expected_generator is not None:
        if document["generator"] != expected_generator:
            raise AuditAggregateAgentError(
                "main-agent generator가 execution witness와 다릅니다."
            )
    validated = copy.deepcopy(document)
    runtime.response_cache[cache_key] = validated
    return copy.deepcopy(validated)


def _finalize_audit_aggregate_agent_turn(
    runtime: _AuditAggregateAgentRuntime,
    state: _AuditAgentTurnState,
    turn: dict,
    *,
    step: int,
) -> dict | None:
    if turn.get("action") != "final" or turn.get("tool") is not None or not isinstance(
        turn.get("final"), dict
    ):
        state.observations.append({
            "tool": "agent_protocol",
            "result": _tool_error("action=final에는 tool=null과 final 객체가 필요합니다."),
        })
        state.discovery_complete = False
        return None
    plan = _sanitize_plan(turn["final"])
    problems = _validate_plan(runtime, state, plan)
    if problems:
        state.observations.append({
            "tool": "answer_validation",
            "result": {"error": {"code": "UNGROUNDED_FINAL", "message": problems[:10]}},
        })
        return None
    _assert_aggregate_agent_unchanged(runtime)
    claims: list[dict] = []
    evidence: list[dict] = []
    if not plan["abstained"]:
        for kind, ref in _selected_refs(plan):
            if kind == "aggregate_record":
                record = runtime.aggregate_records[ref]
                claims.append(_aggregate_claim(record))
                evidence.append(_evidence_for_aggregate(runtime, record))
            else:
                record = runtime.source_records[ref]
                claims.append(_source_claim(runtime, record))
                evidence.append(_evidence_for_source(runtime, record))
    cell_count = _evidence_cell_count(evidence)
    if cell_count > MAX_EVIDENCE_CELLS:
        state.observations.append({
            "tool": "answer_validation",
            "result": _tool_error(
                f"최종 cell evidence가 전역 상한 {MAX_EVIDENCE_CELLS}을 초과합니다."
            ),
        })
        return None
    _assert_aggregate_agent_unchanged(runtime)
    evidence_complete = bool(evidence) and all(
        item["trace_complete"] for item in evidence
    )
    complete = state.discovery_complete and evidence_complete
    answer = {
        "title": "계정별 감사조서 질의 답변",
        "abstained": plan["abstained"],
        "abstention_reason": (
            _ABSTENTION_REASONS[plan["abstention_code"]]
            if plan["abstained"] else None
        ),
        "claims": claims,
        "suggested_questions": [],
    }
    coverage = runtime.aggregate["coverage"]
    generator = _aggregate_generator_profile(
        runtime,
        turns=step,
        tools_used=state.used_tools,
    )
    response = {
        "schema_version": "audit_main_agent_response.v1",
        "mode": "answer",
        "question": runtime.question,
        "package": runtime.path.name,
        "context": copy.deepcopy(runtime.context),
        "generator": generator,
        "trust": {
            "all_sources_approved": runtime.aggregate["trust"]["all_sources_approved"],
            "source_unreviewed": runtime.aggregate["trust"]["source_unreviewed"],
            "aggregate_review_status": "draft",
            "answer_review_status": "unreviewed",
            "readiness": copy.deepcopy(runtime.aggregate["readiness"]),
        },
        "answer": answer,
        "evidence": {"records": evidence},
        "coverage": {
            "complete": complete,
            "discovery_complete": state.discovery_complete,
            "evidence_complete": evidence_complete,
            "record_count": len(evidence),
            "scope_count": len({item["scope"]["id"] for item in evidence}),
            "aggregate_candidate_selection_complete": coverage[
                "candidate_selection_complete"
            ],
            "complete_over_committed_sheets": coverage[
                "complete_over_committed_sheets"
            ],
            "unprepared_sheet_count": coverage["unprepared_sheet_count"],
        },
        "notices": _notices(runtime, complete=complete),
        "package_limitations": copy.deepcopy(runtime.aggregate["limitations"]),
    }
    return _validated_aggregate_response(
        runtime,
        response,
        expected_discovery_complete=state.discovery_complete,
        expected_observed=state.observed,
        expected_generator=generator,
    )


def _aggregate_focus_records(
    runtime: _AuditAggregateAgentRuntime, response: dict
) -> tuple[list[dict], dict[str, set[str]]]:
    validated = _validated_aggregate_response(runtime, response)
    refs: list[str] = []
    for claim in validated["answer"]["claims"]:
        refs.extend(claim["aggregate_record_refs"])
        refs.extend(claim["source_record_refs"])
    wrappers: list[dict] = []
    observed = {kind: set() for kind in _OBSERVED_KINDS}
    for ref in dict.fromkeys(refs):
        if ref in runtime.aggregate_records:
            wrapper = _aggregate_wrapper(runtime.aggregate_records[ref])
            observed["statement"].add(ref)
        else:
            source = runtime.source_records.get(ref)
            if source is None:
                raise AuditAggregateAgentError("prior aggregate response에 unknown ref가 있습니다.")
            wrapper = _source_wrapper(source)
            observed[source.kind].add(ref)
        wrappers.append(wrapper)
    return wrappers, observed


def _markdown_text(value: object) -> str:
    text = _CONTROL_CHARS.sub("", str(value))
    text = " ".join(text.split())
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*\[\]])", r"\\\1", text)


def render_audit_aggregate_agent_markdown(response: dict) -> str:
    answer = response["answer"]
    context = response["context"]
    lines = [f"# {_markdown_text(answer['title'])}", ""]
    lines.append(
        f"> 분석 범위: 계정 aggregate `{_markdown_text(context['aggregate_id'])}`"
    )
    trust = response["trust"]
    coverage = response["coverage"]
    lines.append(
        "> source 승인: "
        f"{'전체 승인' if trust['all_sources_approved'] else '미승인 포함'} · "
        f"aggregate 검토: {trust['aggregate_review_status']} · "
        f"이 답변 검토: {trust['answer_review_status']} · "
        f"준비 상태: {trust['readiness']['status']}"
    )
    lines.append(
        "> 범위: commit 시트 전체 포함="
        f"{coverage['complete_over_committed_sheets']} · 미준비 시트="
        f"{coverage['unprepared_sheet_count']} · 후보 선택 완전="
        f"{coverage['aggregate_candidate_selection_complete']}"
    )
    for notice in response["notices"]:
        lines.append(
            f"> 주의({_markdown_text(notice['code'])}): {_markdown_text(notice['text'])}"
        )
    lines.extend(["", f"질문: {_markdown_text(response['question'])}", ""])
    if answer["abstained"]:
        lines.append(_markdown_text(answer["abstention_reason"]))
    else:
        for claim in answer["claims"]:
            lines.append(
                f"- **{_markdown_text(claim['scope']['sheet'])} · "
                f"{_markdown_text(claim['section'])}**: {_markdown_text(claim['text'])}"
            )
    evidence_map = {
        item["selection_ref"]: item for item in response["evidence"]["records"]
    }
    if evidence_map:
        lines.extend(["", "## 근거 추적", ""])
        for ref, item in evidence_map.items():
            lines.append(
                f"- `{_markdown_text(ref)}` → "
                f"{_markdown_text(item['scope']['sheet'])} / "
                f"{_markdown_text(item['source_kind'])} / "
                f"{_markdown_text(item['source_id'])}"
            )
            trace = item.get("trace")
            if isinstance(trace, Mapping):
                cells = trace.get("cells", [])
                if cells:
                    rendered = ", ".join(
                        f"{_markdown_text(cell.get('sheet'))}!{_markdown_text(cell.get('cell'))}="
                        f"{_markdown_text(cell.get('value'))}"
                        for cell in cells
                    )
                    lines.append(f"  - 셀: {rendered}")
                citations = trace.get("standards_citations", [])
                if citations:
                    rendered = ", ".join(
                        _markdown_text(citation.get("cid") or citation.get("id"))
                        for citation in citations
                    )
                    lines.append(f"  - 기준서: {rendered}")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "AGGREGATE_AGENT_NAME",
    "AGGREGATE_AGENT_VERSION",
    "AuditAggregateAgentChangedError",
    "AuditAggregateAgentError",
    "_AuditAggregateAgentRuntime",
    "_aggregate_focus_records",
    "_aggregate_generator_profile",
    "_aggregate_discovery_complete",
    "_aggregate_observation_witness",
    "_apply_audit_aggregate_agent_tool_turn",
    "_assert_aggregate_agent_unchanged",
    "_finalize_audit_aggregate_agent_turn",
    "_new_audit_aggregate_agent_turn_state",
    "_prepare_audit_aggregate_agent_runtime",
    "_request_audit_aggregate_agent_model_turn",
    "_validated_aggregate_response",
    "render_audit_aggregate_agent_markdown",
]
