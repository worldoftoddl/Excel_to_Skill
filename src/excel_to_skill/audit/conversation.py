"""Compiled, persistent conversation graph over one committed audit bundle.

Only control-plane values and content-addressed references enter LangGraph checkpoints.  Raw
questions, workbook observations, model decisions, and hydrated answers stay in a private store
and are revalidated whenever a node loads them.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, TypedDict

import jsonschema

from .. import cache
from .agent import (
    AuditAgentError,
    _AuditAgentRuntime,
    _AuditAgentTurnState,
    _apply_audit_agent_tool_turn,
    _audit_agent_observation_witness,
    _assert_bundle_unchanged,
    _bundle_identity,
    _finalize_audit_agent_turn,
    _merge_observed,
    _new_audit_agent_turn_state,
    _prepare_audit_agent_runtime,
    _request_audit_agent_model_turn,
    _validated_response,
    render_audit_agent_markdown,
)
from .aggregate_agent import (
    AuditAggregateAgentChangedError,
    AuditAggregateAgentError,
    _AuditAggregateAgentRuntime,
    _aggregate_focus_records,
    _aggregate_generator_profile,
    _aggregate_observation_witness,
    _apply_audit_aggregate_agent_tool_turn,
    _assert_aggregate_agent_unchanged,
    _finalize_audit_aggregate_agent_turn,
    _new_audit_aggregate_agent_turn_state,
    _prepare_audit_aggregate_agent_runtime,
    _request_audit_aggregate_agent_model_turn,
    _validated_aggregate_response,
    render_audit_aggregate_agent_markdown,
)
from .consume import AuditConsumeError, _audit_get_loaded, load_validated_audit_bundle
from .conversation_store import (
    ArtifactRef,
    ConversationArtifactStore,
    ConversationArtifactStoreError,
)
from .llm import AuditLLMError, load_schema
from .model import AuditModelError
from .procedure_planning import (
    PLANNING_RESULT_SCHEMA,
    ProcedurePlanningError,
    ProcedurePlanningRuntime,
    procedure_plan_summary,
    run_procedure_planning,
    standard_basis_ref,
    validate_procedure_plan,
    validate_procedure_plan_summary,
    workbook_basis_ref,
)
from .scope import AuditScopeError, resolve_scope
from .standards_research import (
    RESEARCH_RESULT_SCHEMA,
    StandardsResearchError,
    StandardsResearchRuntime,
    research_records,
    research_summary,
    run_standards_research,
    validate_research_result,
    validate_research_summary,
)
from .workbook_inspection import (
    INSPECTION_RESULT_SCHEMA,
    WorkbookInspectionError,
    inspection_records,
    inspection_summary,
    run_workbook_inspection,
    validate_workbook_inspection_result,
    validate_workbook_inspection_summary,
)


CONVERSATION_VERSION = "0.1.0"
CONVERSATION_PROMPT = "audit_conversation_v1.md"
RUNTIME_DIR = ".audit_runtime/conversations"
QUESTION_SCHEMA = "audit_conversation.question.v1"
OBSERVATIONS_SCHEMA = "audit_conversation.observations.v1"
DECISION_SCHEMA = "audit_conversation.decision.v1"
RESPONSE_SCHEMA = "audit_conversation.response.v1"
USAGE_SCHEMA = "audit_conversation.usage.v1"
TURN_RECORD_SCHEMA = "audit_conversation.turn_record.v1"
HISTORY_SCHEMA = "audit_conversation.history.v1"
RESULT_SCHEMA = "audit_conversation_turn_result.schema.json"
MAX_HISTORY_TURNS = 100
MAX_FOCUS_TURNS = 3
MAX_FOCUS_RECORDS = 40
MAX_RESEARCH_REQUESTS = 1
MAX_PLANNING_REQUESTS = 1
MAX_INSPECTION_REQUESTS = 2

_THREAD_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_OBSERVED_KINDS = (
    "fact",
    "relation",
    "standard_citation",
    "statement",
)


class AuditConversationError(RuntimeError):
    """The persistent audit conversation could not safely complete one turn."""


class AuditConversationBundleChangedError(AuditConversationError):
    """A thread was resumed against a different committed audit bundle."""


class _CheckpointSafeNodeError(RuntimeError):
    """Fixed-code node failure safe for LangGraph's persisted ``__error__`` write."""


class ConversationInput(TypedDict):
    question_ref: ArtifactRef
    requested_bundle: dict
    invocation_id: str


class AuditConversationState(TypedDict, total=False):
    question_ref: ArtifactRef
    requested_bundle: dict
    invocation_id: str
    bound_bundle: dict
    history_ref: ArtifactRef | None
    turn_index: int
    resumed: bool
    observations_ref: ArtifactRef | None
    decision_ref: ArtifactRef | None
    answer_ref: ArtifactRef | None
    usage_ref: ArtifactRef | None
    turn_record_ref: ArtifactRef | None
    observed_ids: dict[str, list[str]]
    used_tools: list[str]
    discovery_complete: bool
    step: int
    route: str
    status: str


class ConversationOutput(TypedDict):
    answer_ref: ArtifactRef
    bound_bundle: dict
    history_ref: ArtifactRef
    resumed: bool
    status: str
    turn_index: int
    turn_record_ref: ArtifactRef
    usage_ref: ArtifactRef


@dataclass
class _CountedChildModelClient:
    """Count only child-model calls that actually cross the client boundary."""

    client: object
    calls: int = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return self.client(**kwargs)


@dataclass
class AuditConversationRuntime:
    """Non-checkpointed dependencies and validated package snapshot for one invocation."""

    thread_id: str
    package_path: Path
    sheet: str | None
    store: ConversationArtifactStore
    agent: _AuditAgentRuntime | _AuditAggregateAgentRuntime | None
    blocked_response: dict | None
    aggregate_id: str | None = None
    client: object | None = None
    client_factory: object | None = None
    standards_research_enabled: bool = False
    procedure_planning_enabled: bool = False
    workbook_inspection_enabled: bool = False
    standards_retriever: object | None = None
    standards_retriever_factory: object | None = None
    workbook_source_provider: object | None = None
    eprint: object | None = None
    _resolved_client: object | None = field(default=None, init=False, repr=False)
    _usage_start: int = field(default=0, init=False, repr=False)
    _research_retrievers: dict[str, object] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _research_results: dict[str, list[dict]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _research_request_count: int = field(default=0, init=False, repr=False)
    _planning_results: dict[str, list[dict]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _planning_request_count: int = field(default=0, init=False, repr=False)
    _inspection_results: dict[str, list[dict]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _inspection_request_count: int = field(default=0, init=False, repr=False)
    _child_model_call_count: int = field(default=0, init=False, repr=False)
    _node_failure: tuple[str, Exception] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @property
    def bundle(self) -> dict:
        if isinstance(self.agent, _AuditAgentRuntime):
            return self.agent.bundle
        if isinstance(self.agent, _AuditAggregateAgentRuntime):
            return self.agent.context
        if self.blocked_response is not None:
            return self.blocked_response["bundle"]
        raise AuditConversationError("conversation runtime에 audit bundle이 없습니다.")

    def model_client(self):
        if self._resolved_client is not None:
            return self._resolved_client
        selected = self.client
        if selected is None:
            if not callable(self.client_factory):
                raise AuditConversationError(
                    "audit-chat 모델 client 또는 client_factory가 필요합니다."
                )
            try:
                selected = self.client_factory()
            except Exception as e:  # noqa: BLE001 - provider factory boundary
                raise AuditConversationError(
                    "audit-chat 모델 client 생성에 실패했습니다."
                ) from e
        events = getattr(selected, "usage_events", ())
        self._usage_start = len(events) if isinstance(events, (list, tuple)) else 0
        self._resolved_client = selected
        return selected

    def usage_events(self) -> list[dict]:
        if self._resolved_client is None:
            return []
        events = getattr(self._resolved_client, "usage_events", ())
        if not isinstance(events, (list, tuple)):
            return []
        result: list[dict] = []
        for value in events[self._usage_start:]:
            if isinstance(value, dict):
                result.append(copy.deepcopy(value))
        return result

    def record_node_failure(self, node: str, error: Exception) -> None:
        self._node_failure = (node, error)

    def capabilities(self) -> dict:
        return {
            "standards_research": {
                "enabled": self.standards_research_enabled,
                "max_requests": MAX_RESEARCH_REQUESTS,
                "max_candidates": 5,
                "max_selected": 3,
                "result_status": "ephemeral_unreviewed_turn_scoped",
            },
            "procedure_planning": {
                "enabled": self.procedure_planning_enabled,
                "max_requests": MAX_PLANNING_REQUESTS,
                "min_candidates": 3,
                "max_candidates": 5,
                "result_status": "proposed_unreviewed_not_evidenced",
            },
            "workbook_inspection": {
                "enabled": self.workbook_inspection_enabled,
                "max_requests": MAX_INSPECTION_REQUESTS,
                "max_selected": MAX_INSPECTION_REQUESTS,
                "one_sheet_per_request": True,
                "one_range_per_request": True,
                "source_preference": "package_ledger_first",
                "raw_source_available": self.workbook_source_provider is not None,
                "operations": [
                    "inspect_range",
                    "inspect_formula_dependencies",
                    "profile_table",
                    "find_duplicates",
                    "find_outliers",
                ],
                "result_status": "computed_unreviewed_not_documented_turn_scoped",
            },
        }

    def research_retriever(self, expected_collection: str):
        cached = self._research_retrievers.get(expected_collection)
        if cached is not None:
            return cached
        selected = self.standards_retriever
        if selected is None:
            if not callable(self.standards_retriever_factory):
                raise StandardsResearchError(
                    "UPSTREAM_UNAVAILABLE",
                    "동적 기준서 조회 retriever가 설정되지 않았습니다."
                )
            try:
                selected = self.standards_retriever_factory(expected_collection)
            except Exception as e:  # noqa: BLE001 - lazy MCP factory boundary
                raise StandardsResearchError(
                    "UPSTREAM_UNAVAILABLE",
                    "동적 기준서 조회 연결을 준비하지 못했습니다."
                ) from e
        if not callable(getattr(selected, "search", None)) or not callable(
            getattr(selected, "get_verified_paragraph", None)
        ):
            raise StandardsResearchError(
                "CONTRACT_MISMATCH",
                "동적 기준서 retriever 계약이 유효하지 않습니다."
            )
        self._research_retrievers[expected_collection] = selected
        return selected

    def close_research(self) -> None:
        closed: set[int] = set()
        for value in [*self._research_retrievers.values(), self.standards_retriever_factory]:
            if value is None or id(value) in closed:
                continue
            closed.add(id(value))
            close = getattr(value, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort external cleanup
                    pass


def _assert_runtime_bundle_current(context: AuditConversationRuntime) -> None:
    """Re-pass the committed-bundle gate immediately around conversation commit/read."""
    try:
        if isinstance(context.agent, _AuditAggregateAgentRuntime):
            _assert_aggregate_agent_unchanged(context.agent)
            return
        if isinstance(context.agent, _AuditAgentRuntime):
            _assert_bundle_unchanged(
                context.agent.path,
                context.agent.facts,
                context.agent.context,
                context.agent.brief_doc,
                context.agent.bundle,
                context.agent.scope,
            )
            return
        loaded = load_validated_audit_bundle(
            context.package_path,
            sheet=context.sheet,
        )
        assert loaded is not None
        path, facts, standards, brief = loaded
        scope = resolve_scope(path, sheet=context.sheet)
        current = _bundle_identity(path, facts, standards, brief, scope)
    except (
        AuditAgentError,
        AuditAggregateAgentError,
        AuditConsumeError,
        AuditScopeError,
        OSError,
    ) as e:
        raise AuditConversationBundleChangedError(
            "audit-chat commit 직전 committed audit bundle을 재검증할 수 없습니다."
        ) from e
    if current != context.bundle:
        raise AuditConversationBundleChangedError(
            "audit-chat commit 직전 committed audit bundle이 변경되었습니다."
        )


def _checkpoint_safe_node(name: str, function):
    """Persist only a fixed failure code while retaining a runtime-side error."""

    def guarded(state: AuditConversationState, runtime):
        try:
            return function(state, runtime)
        except Exception as error:  # noqa: BLE001 - graph checkpoint boundary
            context = getattr(runtime, "context", None)
            if isinstance(context, AuditConversationRuntime):
                context.record_node_failure(name, error)
            raise _CheckpointSafeNodeError(f"AUDIT_CHAT_NODE_FAILED:{name}") from None

    guarded.__name__ = f"checkpoint_safe_{name}"
    return guarded


def _context(runtime) -> AuditConversationRuntime:
    value = getattr(runtime, "context", None)
    if not isinstance(value, AuditConversationRuntime):
        raise AuditConversationError("audit-chat graph runtime context가 없습니다.")
    return value


def _thread_id(value: str | None) -> str:
    if value is None:
        return "audit-" + uuid.uuid4().hex
    if not isinstance(value, str) or _THREAD_RE.fullmatch(value) is None:
        raise AuditConversationError(
            "thread는 영문자·숫자로 시작하는 1~128자 ID여야 합니다."
        )
    return value


def _thread_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(value: object, *, field_name: str) -> ArtifactRef:
    if not isinstance(value, dict):
        raise AuditConversationError(f"{field_name} artifact ref가 없습니다.")
    return value  # strict validation happens in ConversationArtifactStore.load


def _load_payload(
    context: AuditConversationRuntime,
    ref: object,
    *,
    kind: str,
    schema: str,
) -> object:
    try:
        return context.store.load(
            context.thread_id,
            _ref(ref, field_name=kind),
            expected_kind=kind,
            expected_schema_version=schema,
        )
    except ConversationArtifactStoreError as e:
        raise AuditConversationError(f"{kind} artifact 검증 실패: {e}") from e


def _invocation_payload(
    context: AuditConversationRuntime,
    state: AuditConversationState,
    ref: object,
    *,
    kind: str,
    schema: str,
) -> object:
    payload = _load_payload(context, ref, kind=kind, schema=schema)
    if not isinstance(payload, dict) or set(payload) != {"invocation_id", "value"}:
        raise AuditConversationError(f"{kind} artifact payload 형식이 유효하지 않습니다.")
    if payload.get("invocation_id") != state.get("invocation_id"):
        raise AuditConversationError(f"{kind} artifact invocation이 현재 turn과 다릅니다.")
    return payload["value"]


def _write_invocation_payload(
    context: AuditConversationRuntime,
    state: AuditConversationState,
    *,
    kind: str,
    schema: str,
    value: object,
) -> ArtifactRef:
    invocation_id = state.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise AuditConversationError("conversation invocation_id가 유효하지 않습니다.")
    try:
        return context.store.write(
            context.thread_id,
            kind=kind,
            schema_version=schema,
            payload={"invocation_id": invocation_id, "value": value},
        )
    except ConversationArtifactStoreError as e:
        raise AuditConversationError(f"{kind} artifact 저장 실패: {e}") from e


def _observed_json(value: dict[str, set[str]]) -> dict[str, list[str]]:
    return {kind: sorted(value.get(kind, set())) for kind in _OBSERVED_KINDS}


def _restore_observed(
    value: object,
    agent: _AuditAgentRuntime | _AuditAggregateAgentRuntime,
) -> dict[str, set[str]]:
    if not isinstance(value, dict) or set(value) != set(_OBSERVED_KINDS):
        raise AuditConversationError("checkpoint observed_ids 형식이 유효하지 않습니다.")
    observed: dict[str, set[str]] = {}
    for kind in _OBSERVED_KINDS:
        items = value.get(kind)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise AuditConversationError(f"checkpoint observed_ids.{kind}가 유효하지 않습니다.")
        if len(items) != len(set(items)):
            raise AuditConversationError(f"checkpoint observed_ids.{kind}에 중복이 있습니다.")
        unknown = set(items) - agent.known[kind]
        if unknown:
            raise AuditConversationError(
                f"checkpoint observed_ids.{kind}에 bundle 밖 ID가 있습니다."
            )
        observed[kind] = set(items)
    return observed


def _load_observations(
    context: AuditConversationRuntime,
    state: AuditConversationState,
) -> list[dict]:
    value = _invocation_payload(
        context,
        state,
        state.get("observations_ref"),
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
    )
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AuditConversationError("observations artifact는 객체 배열이어야 합니다.")
    return copy.deepcopy(value)


def _research_request_key(value: object) -> str:
    if not isinstance(value, dict):
        raise AuditConversationError("standards_research input이 유효하지 않습니다.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _research_error(code: str) -> dict:
    allowed = {
        "RESEARCH_DISABLED",
        "RESEARCH_LIMIT_EXCEEDED",
        "INVALID_REQUEST",
        "CORPUS_DRIFT",
        "UPSTREAM_UNAVAILABLE",
        "CONTRACT_MISMATCH",
        "NO_RESULTS",
        "LIMIT_EXCEEDED",
    }
    selected = code if code in allowed else "UPSTREAM_UNAVAILABLE"
    return {
        "error": {
            "code": selected,
            "message": "동적 기준서 조회를 완료하지 못했습니다.",
        }
    }


def _planning_request_key(value: object) -> str:
    if not isinstance(value, dict):
        raise AuditConversationError("procedure_planning input이 유효하지 않습니다.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _planning_error(code: str) -> dict:
    allowed = {
        "PLANNING_DISABLED",
        "PLANNING_LIMIT_EXCEEDED",
        "RESEARCH_REQUIRED",
        "INVALID_REQUEST",
        "UPSTREAM_UNAVAILABLE",
        "CONTRACT_MISMATCH",
        "LIMIT_EXCEEDED",
    }
    selected = code if code in allowed else "UPSTREAM_UNAVAILABLE"
    return {
        "error": {
            "code": selected,
            "message": "감사 test 후보 계획을 완료하지 못했습니다.",
        }
    }


def _inspection_request_key(value: object) -> str:
    if not isinstance(value, dict):
        raise AuditConversationError("workbook_inspection input이 유효하지 않습니다.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inspection_error(code: str) -> dict:
    allowed = {
        "INSPECTION_DISABLED",
        "INSPECTION_LIMIT_EXCEEDED",
        "DUPLICATE_REQUEST",
        "INVALID_REQUEST",
        "SOURCE_UNAVAILABLE",
        "CONTRACT_MISMATCH",
        "UPSTREAM_UNAVAILABLE",
    }
    selected = code if code in allowed else "UPSTREAM_UNAVAILABLE"
    return {
        "error": {
            "code": selected,
            "message": "workbook 추가 검사를 완료하지 못했습니다.",
        }
    }


def _inspection_binding(
    context: AuditConversationRuntime,
    request: dict,
) -> tuple[dict, object, str | None]:
    agent = context.agent
    if agent is None:
        raise AuditConversationError(
            "차단된 conversation은 workbook inspection을 사용할 수 없습니다."
        )
    neutral = {
        "query": request.get("query"),
        "kind": request.get("kind"),
        "limit": request.get("limit"),
    }
    neutral_id_name = (
        "item_ref" if isinstance(agent, _AuditAggregateAgentRuntime) else "item_id"
    )
    neutral[neutral_id_name] = request.get(neutral_id_name)
    if neutral != {
        "query": None,
        "kind": None,
        "limit": 1,
        neutral_id_name: None,
    }:
        raise AuditConversationError(
            "workbook_inspection의 query/kind/item/limit control이 유효하지 않습니다."
        )
    canonical = {
        name: copy.deepcopy(request.get(name))
        for name in ("operation", "sheet", "range", "parameters")
    }
    if any(value is None for value in canonical.values()):
        raise AuditConversationError(
            "workbook_inspection에는 operation/sheet/range/parameters가 필요합니다."
        )
    if isinstance(agent, _AuditAggregateAgentRuntime):
        scope_id = request.get("scope_id")
        exposed_scope_ids = {
            account.get("scope", {}).get("id")
            for account in agent.bootstrap.get("accounts", [])
            if isinstance(account, dict)
        }
        snapshot = agent.sources.get(scope_id) if isinstance(scope_id, str) else None
        if snapshot is None or scope_id not in exposed_scope_ids:
            raise AuditConversationError(
                "workbook_inspection scope_id는 aggregate bootstrap의 exact source여야 합니다."
            )
        if canonical["sheet"] != snapshot.scope.sheet:
            raise AuditConversationError(
                "workbook_inspection은 aggregate source sheet를 바꿀 수 없습니다."
            )
        return canonical, snapshot.scope, scope_id
    if request.get("scope_id") is not None:
        raise AuditConversationError(
            "일반 workbook_inspection에는 aggregate scope_id를 사용할 수 없습니다."
        )
    if agent.scope.kind == "sheet" and canonical["sheet"] != agent.scope.sheet:
        raise AuditConversationError(
            "workbook_inspection sheet가 committed sheet scope와 다릅니다."
        )
    return canonical, agent.scope, None


def _assert_inspection_ledger_first(
    request: dict,
    prior_observations: list[dict],
) -> None:
    if not isinstance(request, dict):
        raise AuditConversationError("workbook_inspection input이 유효하지 않습니다.")
    parameters = request.get("parameters")
    if (
        request.get("operation") != "inspect_range"
        or not isinstance(parameters, dict)
        or parameters.get("source") != "raw"
    ):
        return
    for observation in prior_observations:
        if observation.get("tool") != "workbook_inspection":
            continue
        prior_request = observation.get("input")
        result = observation.get("result")
        prior_parameters = (
            prior_request.get("parameters")
            if isinstance(prior_request, dict)
            else None
        )
        if (
            isinstance(prior_request, dict)
            and isinstance(prior_parameters, dict)
            and prior_request.get("operation") == "inspect_range"
            and prior_request.get("sheet") == request.get("sheet")
            and prior_request.get("range") == request.get("range")
            and prior_request.get("scope_id") == request.get("scope_id")
            and prior_parameters.get("source") == "ledger"
            and isinstance(result, dict)
            and result.get("schema_version") == INSPECTION_RESULT_SCHEMA
            and result.get("source", {}).get("kind") == "package_ledger"
        ):
            return
    raise AuditConversationError(
        "raw workbook inspection 전에 같은 sheet/range의 package ledger 검사가 필요합니다."
    )


def _assert_inspection_scope_consistent(
    request: dict,
    prior_observations: list[dict],
) -> None:
    if not isinstance(request, dict):
        raise AuditConversationError("workbook_inspection input이 유효하지 않습니다.")
    for observation in prior_observations:
        if observation.get("tool") != "workbook_inspection":
            continue
        result = observation.get("result")
        prior = observation.get("input")
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != INSPECTION_RESULT_SCHEMA
            or not isinstance(prior, dict)
        ):
            continue
        if (
            prior.get("sheet") != request.get("sheet")
            or prior.get("scope_id") != request.get("scope_id")
        ):
            raise AuditConversationError(
                "한 turn의 workbook_inspection은 하나의 exact source sheet에 고정됩니다."
            )
        return


def _inspection_observation_result(
    context: AuditConversationRuntime,
    observation: object,
    *,
    expected_result: dict | None,
) -> tuple[dict, bool]:
    if (
        not isinstance(observation, dict)
        or set(observation) != {"tool", "input", "result"}
        or observation.get("tool") != "workbook_inspection"
        or not isinstance(observation.get("input"), dict)
        or not isinstance(observation.get("result"), dict)
    ):
        raise AuditConversationError(
            "workbook_inspection observation 형식이 유효하지 않습니다."
        )
    result = observation["result"]
    if result.get("schema_version") == INSPECTION_RESULT_SCHEMA:
        canonical, scope, scope_id = _inspection_binding(
            context,
            observation["input"],
        )
        try:
            validated = validate_workbook_inspection_result(
                result,
                expected_scope=scope.identity(),
                expected_scope_id=scope_id,
            )
        except WorkbookInspectionError as e:
            raise AuditConversationError(
                "workbook_inspection observation 계약이 유효하지 않습니다."
            ) from e
        if (
            validated.get("operation") != canonical["operation"]
            or validated.get("input") != canonical
        ):
            raise AuditConversationError(
                "workbook_inspection observation binding이 요청과 다릅니다."
            )
        complete = True
    else:
        error = result.get("error")
        if (
            set(result) != {"error"}
            or not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or result != _inspection_error(str(error.get("code")))
        ):
            raise AuditConversationError(
                "workbook_inspection error witness가 유효하지 않습니다."
            )
        validated = copy.deepcopy(result)
        complete = False
    if expected_result is not None and expected_result != validated:
        raise AuditConversationError(
            "workbook_inspection observation이 현재 invocation witness와 다릅니다."
        )
    return validated, complete


def _planning_record_kind(source_kind: str, item: dict) -> str:
    if source_kind == "fact":
        kind = item.get("type")
        if kind in {"account", "risk", "assertion", "procedure"}:
            return str(kind)
        if kind == "open_item":
            return "gap"
    if source_kind == "relation":
        return "relation"
    if source_kind == "statement" and item.get("type") == "gap":
        return "gap"
    return "other"


def _planning_record_text(source_kind: str, item: dict) -> str:
    if source_kind == "relation":
        return " ".join(
            str(value)
            for value in (
                item.get("type"),
                item.get("from_fact_id"),
                "->",
                item.get("to_fact_id"),
                item.get("status"),
            )
            if value is not None
        )[:8_000]
    if source_kind == "statement":
        value = item.get("text")
    else:
        value = item.get("description") or item.get("value")
    text = " ".join(str(value or "").split())
    return text[:8_000] or f"typed {source_kind} record"


def _planning_workbook_basis(
    *,
    scope: dict,
    source_kind: str,
    source_ref: str,
    item: dict,
    aggregate_source: bool = False,
) -> dict:
    record = {
        "typed_kind": "planning_workbook_basis",
        "basis_ref": "",
        "scope": copy.deepcopy(scope),
        "source_kind": "source_record" if aggregate_source else source_kind,
        "record_kind": _planning_record_kind(source_kind, item),
        "source_ref": source_ref,
        "text": _planning_record_text(source_kind, item),
        "status": item.get("status") if isinstance(item.get("status"), str) else None,
        "confidence": (
            item.get("confidence")
            if isinstance(item.get("confidence"), (int, float))
            and not isinstance(item.get("confidence"), bool)
            else None
        ),
    }
    record["basis_ref"] = workbook_basis_ref(record)
    return record


def _planning_prepared_standard_basis(
    *,
    scope: dict,
    source_ref: str,
    citation: dict,
    expected_collection: str,
) -> dict | None:
    metadata = citation.get("provider_metadata")
    cid = citation.get("document_id")
    text = citation.get("snippet")
    if (
        not isinstance(metadata, dict)
        or metadata.get("verified_by") != "standards_get_paragraph"
        or metadata.get("source_cid") != cid
        or citation.get("corpus_version") != expected_collection
        or not isinstance(cid, str)
        or not isinstance(text, str)
        or not text.strip()
    ):
        return None
    record = {
        "typed_kind": "planning_standard_basis",
        "basis_ref": "",
        "scope": copy.deepcopy(scope),
        "origin": "prepared_citation",
        "source_ref": source_ref,
        "collection": expected_collection,
        "cid": cid,
        "domain": citation.get("domain"),
        "framework": citation.get("framework"),
        "source_type": metadata.get("source_type"),
        "standard_no": str(metadata.get("standard_no")),
        "standard_title": citation.get("title"),
        "para_no": str(citation.get("paragraph")),
        "para_type": metadata.get("para_type"),
        "section_path": metadata.get("section_path"),
        "text": text,
        "text_sha256": citation.get("snippet_sha256"),
        "effective_date_verified": False,
        "verified_by": "standards_get_paragraph",
    }
    try:
        record["basis_ref"] = standard_basis_ref(record)
    except (ProcedurePlanningError, AuditModelError):
        return None
    return record


def _planning_research_standard_basis(record: dict) -> dict:
    basis = {
        "typed_kind": "planning_standard_basis",
        "basis_ref": "",
        "scope": copy.deepcopy(record["scope"]),
        "origin": "ephemeral_research",
        "source_ref": record["research_ref"],
        "collection": record["collection"],
        "cid": record["cid"],
        "domain": record["domain"],
        "framework": record["framework"],
        "source_type": record["source_type"],
        "standard_no": record["standard_no"],
        "standard_title": record["standard_title"],
        "para_no": record["para_no"],
        "para_type": record["para_type"],
        "section_path": record["section_path"],
        "text": record["text"],
        "text_sha256": record["text_sha256"],
        "effective_date_verified": record["effective_date_verified"],
        "verified_by": record["verified_by"],
    }
    basis["basis_ref"] = standard_basis_ref(basis)
    return basis


def _research_observation_result(
    context: AuditConversationRuntime,
    observation: object,
    *,
    expected_result: dict | None,
) -> tuple[dict, bool]:
    if (
        not isinstance(observation, dict)
        or set(observation) != {"tool", "input", "result"}
        or observation.get("tool") != "standards_research"
        or not isinstance(observation.get("input"), dict)
        or not isinstance(observation.get("result"), dict)
    ):
        raise AuditConversationError(
            "standards_research observation 형식이 유효하지 않습니다."
        )
    result = observation["result"]
    if result.get("schema_version") == RESEARCH_RESULT_SCHEMA:
        try:
            validated = validate_research_result(result)
        except StandardsResearchError as e:
            raise AuditConversationError(
                "standards_research observation 계약이 유효하지 않습니다."
            ) from e
        canonical, collection, scope = _research_binding(
            context,
            observation["input"],
        )
        expected_request = {
            key: canonical[key]
            for key in ("domain", "framework", "scope_id", "limit")
        }
        if (
            validated.get("collection") != collection
            or validated.get("scope") != scope
            or validated.get("request") != expected_request
            or any(
                record.get("collection") != collection
                or record.get("scope") != scope
                or record.get("domain") != canonical["domain"]
                or record.get("framework") != canonical["framework"]
                for record in validated.get("records", [])
            )
        ):
            raise AuditConversationError(
                "standards_research observation binding이 committed scope와 다릅니다."
            )
        complete = validated["status"] in {"completed", "no_results"}
    else:
        error = result.get("error")
        if (
            set(result) != {"error"}
            or not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or result != _research_error(str(error.get("code")))
        ):
            raise AuditConversationError(
                "standards_research error witness가 유효하지 않습니다."
            )
        validated = copy.deepcopy(result)
        complete = False
    if expected_result is not None and expected_result != validated:
        raise AuditConversationError(
            "standards_research observation이 현재 invocation witness와 다릅니다."
        )
    return validated, complete


def _planning_observation_result(
    observation: object,
    *,
    expected_result: dict | None,
) -> dict:
    if (
        not isinstance(observation, dict)
        or set(observation) != {"tool", "input", "result"}
        or observation.get("tool") != "procedure_planning"
        or not isinstance(observation.get("input"), dict)
        or not isinstance(observation.get("result"), dict)
    ):
        raise AuditConversationError(
            "procedure_planning observation 형식이 유효하지 않습니다."
        )
    result = observation["result"]
    if result.get("schema_version") == PLANNING_RESULT_SCHEMA:
        try:
            validated = validate_procedure_plan(result)
        except ProcedurePlanningError as e:
            raise AuditConversationError(
                "procedure_planning observation 계약이 유효하지 않습니다."
            ) from e
    else:
        error = result.get("error")
        if (
            set(result) != {"error"}
            or not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or result != _planning_error(str(error.get("code")))
        ):
            raise AuditConversationError(
                "procedure_planning error witness가 유효하지 않습니다."
            )
        validated = copy.deepcopy(result)
    if expected_result is not None and expected_result != validated:
        raise AuditConversationError(
            "procedure_planning observation이 현재 invocation witness와 다릅니다."
        )
    return validated


def _planning_request_witness(request: dict) -> dict:
    return {
        "objective": request["objective"],
        "target": copy.deepcopy(request["target"]),
        "candidate_count": request["candidate_count"],
        "existing_procedure_refs": list(request["existing_procedure_refs"]),
        "workbook_basis_refs": [
            item["basis_ref"] for item in request["workbook_basis"]
        ],
        "standards_basis_refs": [
            item["basis_ref"] for item in request["standards_basis"]
        ],
    }


def _base_observations_and_supplements(
    context: AuditConversationRuntime,
    observations: list[dict],
    *,
    require_runtime_witness: bool,
) -> tuple[list[dict], bool, list[str], list[dict]]:
    agent = context.agent
    if agent is None:
        raise AuditConversationError("research observation을 검증할 agent가 없습니다.")
    bootstrap_tools = (
        ["aggregate_brief"]
        if isinstance(agent, _AuditAggregateAgentRuntime)
        else ["brief", "assertion_procedures"]
    )
    start = len(bootstrap_tools)
    base: list[dict] = []
    research_complete = True
    used_tools = list(bootstrap_tools)
    successful = 0
    successful_plans = 0
    successful_inspections = 0
    seen_research_requests: dict[str, int] = {}
    seen_planning_requests: dict[str, int] = {}
    seen_inspection_requests: dict[str, int] = {}
    available_research: dict[str, dict] = {}
    available_inspections: dict[str, dict] = {}
    planning_witnesses: list[dict] = []
    for index, observation in enumerate(observations):
        tool = observation.get("tool") if isinstance(observation, dict) else None
        if tool == "standards_research":
            key = _research_request_key(observation.get("input"))
            occurrence = seen_research_requests.get(key, 0)
            seen_research_requests[key] = occurrence + 1
            expected = None
            if require_runtime_witness:
                values = context._research_results.get(key, [])
                if occurrence >= len(values):
                    raise AuditConversationError(
                        "standards_research runtime witness가 누락되었습니다."
                    )
                expected = values[occurrence]
            result, complete = _research_observation_result(
                context,
                observation,
                expected_result=expected,
            )
            if result.get("schema_version") == RESEARCH_RESULT_SCHEMA:
                successful += 1
            if successful > MAX_RESEARCH_REQUESTS:
                raise AuditConversationError(
                    "한 turn에 성공한 standards_research가 상한을 초과했습니다."
                )
            research_complete = research_complete and complete
            if result.get("schema_version") == RESEARCH_RESULT_SCHEMA:
                current_records = research_records(result)
                if set(available_research) & set(current_records):
                    raise AuditConversationError(
                        "standards_research ref가 observation 사이에서 중복되었습니다."
                    )
                available_research.update(current_records)
        elif tool == "procedure_planning":
            key = _planning_request_key(observation.get("input"))
            occurrence = seen_planning_requests.get(key, 0)
            seen_planning_requests[key] = occurrence + 1
            expected = None
            if require_runtime_witness:
                values = context._planning_results.get(key, [])
                if occurrence >= len(values):
                    raise AuditConversationError(
                        "procedure_planning runtime witness가 누락되었습니다."
                    )
                expected = values[occurrence]
            result = _planning_observation_result(
                observation,
                expected_result=expected,
            )
            if result.get("schema_version") == PLANNING_RESULT_SCHEMA:
                successful_plans += 1
            if successful_plans > MAX_PLANNING_REQUESTS:
                raise AuditConversationError(
                    "한 turn에 성공한 procedure_planning이 상한을 초과했습니다."
                )
            planning_witnesses.append({
                "observation": copy.deepcopy(observation),
                "result": result,
                "base_prefix": copy.deepcopy(base),
                "research": copy.deepcopy(available_research),
            })
        elif tool == "workbook_inspection":
            if (
                isinstance(observation.get("result"), dict)
                and observation["result"].get("schema_version")
                == INSPECTION_RESULT_SCHEMA
            ):
                _assert_inspection_scope_consistent(
                    observation.get("input"),
                    observations[:index],
                )
                _assert_inspection_ledger_first(
                    observation.get("input"),
                    observations[:index],
                )
            key = _inspection_request_key(observation.get("input"))
            occurrence = seen_inspection_requests.get(key, 0)
            seen_inspection_requests[key] = occurrence + 1
            expected = None
            if require_runtime_witness:
                values = context._inspection_results.get(key, [])
                if occurrence >= len(values):
                    raise AuditConversationError(
                        "workbook_inspection runtime witness가 누락되었습니다."
                    )
                expected = values[occurrence]
            result, complete = _inspection_observation_result(
                context,
                observation,
                expected_result=expected,
            )
            if result.get("schema_version") == INSPECTION_RESULT_SCHEMA:
                successful_inspections += 1
                try:
                    current_records = inspection_records(result)
                except WorkbookInspectionError as e:
                    raise AuditConversationError(
                        "workbook_inspection typed ref를 검증할 수 없습니다."
                    ) from e
                if set(available_inspections) & set(current_records):
                    raise AuditConversationError(
                        "workbook_inspection ref가 observation 사이에서 중복되었습니다."
                    )
                available_inspections.update(current_records)
            if successful_inspections > MAX_INSPECTION_REQUESTS:
                raise AuditConversationError(
                    "한 turn에 성공한 workbook_inspection이 상한을 초과했습니다."
                )
            research_complete = research_complete and complete
        else:
            base.append(observation)
        if (
            index >= start
            and isinstance(tool, str)
            and tool not in {"conversation_focus", "answer_validation", "agent_protocol"}
        ):
            used_tools.append(tool)
    return base, research_complete, used_tools, planning_witnesses


def _validate_planning_prefixes(
    context: AuditConversationRuntime,
    witnesses: list[dict],
    *,
    expected_focus: dict | None,
    expected_model: str,
    expected_invocation_id: str,
) -> None:
    agent = context.agent
    if agent is None:
        raise AuditConversationError("procedure planning prefix를 검증할 agent가 없습니다.")
    if not isinstance(expected_invocation_id, str) or not expected_invocation_id:
        raise AuditConversationError("procedure planning invocation witness가 없습니다.")
    for witness in witnesses:
        result = witness["result"]
        if result.get("schema_version") != PLANNING_RESULT_SCHEMA:
            continue
        try:
            if isinstance(agent, _AuditAggregateAgentRuntime):
                _, observed, _, _ = _aggregate_observation_witness(
                    agent,
                    witness["base_prefix"],
                    expected_limit=agent.limit,
                    expected_focus=expected_focus,
                )
            else:
                _, observed, _ = _audit_agent_observation_witness(
                    agent,
                    witness["base_prefix"],
                    expected_focus=expected_focus,
                )
            canonical, scope = _planning_binding(
                context,
                witness["observation"]["input"],
                observed=observed,
                research=witness["research"],
            )
        except (
            AuditAgentError,
            AuditAggregateAgentError,
            AuditConversationError,
            ProcedurePlanningError,
        ) as e:
            raise AuditConversationError(
                "procedure_planning 요청 시점의 typed 근거 권한을 재생할 수 없습니다."
            ) from e
        if (
            result.get("scope") != scope
            or result.get("binding", {}).get("bundle_sha256")
            != hashlib.sha256(
                json.dumps(
                    context.bundle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            or result.get("binding", {}).get("invocation_sha256")
            != hashlib.sha256(expected_invocation_id.encode("utf-8")).hexdigest()
            or result.get("request") != _planning_request_witness(canonical)
            or result.get("basis_catalog") != {
                "workbook": canonical["workbook_basis"],
                "standards": canonical["standards_basis"],
            }
            or result.get("worker", {}).get("model") != expected_model
        ):
            raise AuditConversationError(
                "procedure_planning 결과 binding이 요청 시점의 근거와 다릅니다."
            )


def _turn_state(
    context: AuditConversationRuntime,
    state: AuditConversationState,
) -> _AuditAgentTurnState:
    agent = context.agent
    if agent is None:
        raise AuditConversationError("차단된 conversation에는 agent turn state가 없습니다.")
    observations = _load_observations(context, state)
    try:
        expected_focus, _ = _focus_observation(context, state.get("history_ref"))
        (
            base_observations,
            research_complete,
            replayed_full_tools,
            planning_witnesses,
        ) = _base_observations_and_supplements(
            context,
            observations,
            require_runtime_witness=True,
        )
        if isinstance(agent, _AuditAggregateAgentRuntime):
            (
                replayed_discovery,
                replayed_observed,
                replayed_tools,
                _,
            ) = _aggregate_observation_witness(
                agent,
                base_observations,
                expected_limit=agent.limit,
                expected_focus=expected_focus,
            )
        else:
            (
                replayed_discovery,
                replayed_observed,
                replayed_tools,
            ) = _audit_agent_observation_witness(
                agent,
                base_observations,
                expected_focus=expected_focus,
            )
    except (AuditAgentError, AuditAggregateAgentError) as e:
        raise AuditConversationError(
            "conversation observations deterministic replay가 실패했습니다."
        ) from e
    _validate_planning_prefixes(
        context,
        planning_witnesses,
        expected_focus=expected_focus,
        expected_model=agent.model,
        expected_invocation_id=state.get("invocation_id"),
    )
    if [
        tool
        for tool in replayed_full_tools
        if tool not in {
            "standards_research", "procedure_planning", "workbook_inspection",
        }
    ] != replayed_tools:
        raise AuditConversationError(
            "conversation supplement tool replay 순서가 deterministic witness와 다릅니다."
        )
    replayed_tools = replayed_full_tools
    replayed_discovery = replayed_discovery and research_complete
    replay_start = 1 if isinstance(agent, _AuditAggregateAgentRuntime) else 2
    seen_requests = {
        (
            _research_request_key(item["input"])
            if item.get("tool") == "standards_research"
            else _planning_request_key(item["input"])
            if item.get("tool") == "procedure_planning"
            else _inspection_request_key(item["input"])
            if item.get("tool") == "workbook_inspection"
            else json.dumps(item["input"], ensure_ascii=False, sort_keys=True)
        )
        for item in observations[replay_start:]
        if isinstance(item.get("input"), dict)
        and item.get("tool") != "conversation_focus"
    }
    used_tools = state.get("used_tools")
    if not isinstance(used_tools, list) or any(
        not isinstance(item, str) for item in used_tools
    ):
        raise AuditConversationError("checkpoint used_tools가 유효하지 않습니다.")
    if used_tools != replayed_tools:
        raise AuditConversationError(
            "checkpoint used_tools가 observations replay와 다릅니다."
        )
    discovery_complete = state.get("discovery_complete")
    if not isinstance(discovery_complete, bool):
        raise AuditConversationError("checkpoint discovery_complete가 유효하지 않습니다.")
    if discovery_complete is not replayed_discovery:
        raise AuditConversationError(
            "checkpoint discovery_complete가 observations replay와 다릅니다."
        )
    checkpoint_observed = _restore_observed(state.get("observed_ids"), agent)
    if checkpoint_observed != replayed_observed:
        raise AuditConversationError(
            "checkpoint observed_ids가 observations replay와 다릅니다."
        )
    return _AuditAgentTurnState(
        observations=observations,
        used_tools=list(replayed_tools),
        observed=replayed_observed,
        discovery_complete=replayed_discovery,
        seen_tool_requests=seen_requests,
    )


def _history(
    context: AuditConversationRuntime,
    value: object,
) -> list[ArtifactRef]:
    if value is None:
        return []
    payload = _load_payload(
        context,
        value,
        kind="history",
        schema=HISTORY_SCHEMA,
    )
    if not isinstance(payload, dict) or set(payload) != {"turn_refs"}:
        raise AuditConversationError("conversation history 형식이 유효하지 않습니다.")
    refs = payload["turn_refs"]
    if not isinstance(refs, list) or any(not isinstance(item, dict) for item in refs):
        raise AuditConversationError("conversation history turn_refs가 유효하지 않습니다.")
    if len(refs) > MAX_HISTORY_TURNS:
        raise AuditConversationError(
            f"conversation history가 {MAX_HISTORY_TURNS} turn 상한을 초과했습니다."
        )
    return list(refs)


def _turn_record(
    context: AuditConversationRuntime,
    ref: object,
) -> dict:
    payload = _load_payload(
        context,
        ref,
        kind="turn_record",
        schema=TURN_RECORD_SCHEMA,
    )
    required = {
        "turn_index",
        "invocation_id",
        "question_ref",
        "response_ref",
        "usage_ref",
    }
    allowed = required | {"observations_ref", "execution"}
    if (
        not isinstance(payload, dict)
        or not required <= set(payload)
        or not set(payload) <= allowed
    ):
        raise AuditConversationError("conversation turn record 형식이 유효하지 않습니다.")
    if (
        not isinstance(payload.get("turn_index"), int)
        or isinstance(payload["turn_index"], bool)
        or payload["turn_index"] < 1
    ):
        raise AuditConversationError("conversation turn index가 유효하지 않습니다.")
    if not isinstance(payload.get("invocation_id"), str):
        raise AuditConversationError("conversation turn invocation이 유효하지 않습니다.")
    for name in ("question_ref", "response_ref", "usage_ref"):
        if not isinstance(payload.get(name), dict):
            raise AuditConversationError(f"conversation turn {name}가 유효하지 않습니다.")
    if "observations_ref" in payload and not isinstance(
        payload.get("observations_ref"), dict
    ):
        raise AuditConversationError(
            "conversation turn observations_ref가 유효하지 않습니다."
        )
    if "execution" in payload:
        _execution_witness(payload["execution"])
    return payload


def _execution_witness(value: object) -> dict:
    fields = {
        "name", "version", "model", "prompt_sha256", "limit", "turns", "tools_used",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise AuditConversationError(
            "conversation turn execution witness 형식이 유효하지 않습니다."
        )
    turns = value.get("turns")
    limit = value.get("limit")
    tools_used = value.get("tools_used")
    if (
        value.get("name") != "excel_to_skill.audit.aggregate_agent"
        or not isinstance(value.get("version"), str)
        or not value["version"]
        or not isinstance(value.get("model"), str)
        or not value["model"]
        or not isinstance(value.get("prompt_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["prompt_sha256"]) is None
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 200
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or not 1 <= turns <= 12
        or not isinstance(tools_used, list)
        or not tools_used
        or tools_used[0] != "aggregate_brief"
        or any(not isinstance(item, str) for item in tools_used)
    ):
        raise AuditConversationError(
            "conversation turn execution witness 값이 유효하지 않습니다."
        )
    return {
        "name": value["name"],
        "version": value["version"],
        "model": value["model"],
        "prompt_sha256": value["prompt_sha256"],
        "limit": limit,
        "turns": turns,
        "tools_used": list(tools_used),
    }


def _history_entries(
    context: AuditConversationRuntime,
    value: object,
) -> list[dict]:
    records = [_turn_record(context, ref) for ref in _history(context, value)]
    for index, record in enumerate(records, 1):
        if record["turn_index"] != index:
            raise AuditConversationError("conversation history turn 순서가 연속적이지 않습니다.")
    return records


def _question_from_ref(
    context: AuditConversationRuntime,
    ref: object,
    *,
    invocation_id: str,
) -> str:
    payload = _load_payload(
        context,
        ref,
        kind="question",
        schema=QUESTION_SCHEMA,
    )
    if not isinstance(payload, dict) or set(payload) != {"invocation_id", "question"}:
        raise AuditConversationError("conversation question 형식이 유효하지 않습니다.")
    if payload.get("invocation_id") != invocation_id:
        raise AuditConversationError("conversation question invocation이 일치하지 않습니다.")
    question = payload.get("question")
    if not isinstance(question, str) or not question:
        raise AuditConversationError("conversation question이 비어 있습니다.")
    return question


def _response_from_ref(
    context: AuditConversationRuntime,
    ref: object,
    *,
    invocation_id: str,
    observations_ref: object = None,
    expected_focus: dict | None = None,
    expected_execution: object = None,
) -> dict:
    payload = _load_payload(
        context,
        ref,
        kind="response",
        schema=RESPONSE_SCHEMA,
    )
    if not isinstance(payload, dict) or set(payload) != {"invocation_id", "value"}:
        raise AuditConversationError("conversation response 형식이 유효하지 않습니다.")
    if payload.get("invocation_id") != invocation_id:
        raise AuditConversationError("conversation response invocation이 일치하지 않습니다.")
    response = payload.get("value")
    if not isinstance(response, dict):
        raise AuditConversationError("conversation response 본문이 유효하지 않습니다.")
    observations: list[dict] | None = None
    if isinstance(observations_ref, dict):
        loaded_observations = _invocation_payload(
            context,
            {"invocation_id": invocation_id},
            observations_ref,
            kind="observations",
            schema=OBSERVATIONS_SCHEMA,
        )
        if not isinstance(loaded_observations, list) or any(
            not isinstance(item, dict) for item in loaded_observations
        ):
            raise AuditConversationError(
                "conversation response observations witness가 유효하지 않습니다."
            )
        observations = loaded_observations
    if isinstance(context.agent, _AuditAggregateAgentRuntime):
        try:
            execution = _execution_witness(expected_execution)
            if observations is None:
                raise AuditAggregateAgentError(
                    "aggregate observations witness가 없습니다."
                )
            base_observations, research_complete, full_tools, planning_witnesses = (
                _base_observations_and_supplements(
                    context,
                    observations,
                    require_runtime_witness=False,
                )
            )
            discovery_complete, observed, tools_used, turns = (
                _aggregate_observation_witness(
                    context.agent,
                    base_observations,
                    expected_limit=execution["limit"],
                    expected_focus=expected_focus,
                )
            )
            supplement_turns = sum(
                item.get("tool") in {
                    "standards_research", "procedure_planning", "workbook_inspection",
                }
                for item in observations
            )
            if (
                execution["turns"] != turns + supplement_turns
                or execution["tools_used"] != full_tools
                or [
                    tool
                    for tool in full_tools
                    if tool not in {
                        "standards_research", "procedure_planning", "workbook_inspection",
                    }
                ]
                != tools_used
            ):
                raise AuditAggregateAgentError(
                    "aggregate observations replay가 turn execution witness와 다릅니다."
                )
            discovery_complete = discovery_complete and research_complete
            validated = _validated_aggregate_response(
                context.agent,
                response,
                expected_discovery_complete=discovery_complete,
                expected_observed=observed,
                expected_generator=execution,
            )
        except AuditAggregateAgentError as e:
            raise AuditConversationError(
                f"aggregate conversation response 검증 실패: {e}"
            ) from e
        _validate_planning_prefixes(
            context,
            planning_witnesses,
            expected_focus=expected_focus,
            expected_model=validated["generator"]["model"],
            expected_invocation_id=invocation_id,
        )
    else:
        validated = _validated_response(response)
        if observations is not None:
            _, _, _, planning_witnesses = _base_observations_and_supplements(
                context,
                observations,
                require_runtime_witness=False,
            )
            _validate_planning_prefixes(
                context,
                planning_witnesses,
                expected_focus=expected_focus,
                expected_model=validated["generator"]["model"],
                expected_invocation_id=invocation_id,
            )
    successful_research = bool(observations) and any(
        item.get("tool") == "standards_research"
        and isinstance(item.get("result"), dict)
        and item["result"].get("schema_version") == RESEARCH_RESULT_SCHEMA
        for item in observations
    )
    summary = validated.get("standards_research")
    if summary is not None:
        if observations is None:
            raise AuditConversationError(
                "standards_research response에 observations witness가 없습니다."
            )
        try:
            validate_research_summary(summary, observations=observations)
        except StandardsResearchError as e:
            raise AuditConversationError(
                "standards_research response가 typed observation과 다릅니다."
            ) from e
    elif successful_research:
        raise AuditConversationError(
            "성공한 standards_research response summary가 누락되었습니다."
        )
    successful_plan = bool(observations) and any(
        item.get("tool") == "procedure_planning"
        and isinstance(item.get("result"), dict)
        and item["result"].get("schema_version") == PLANNING_RESULT_SCHEMA
        for item in observations
    )
    plan_summary = validated.get("procedure_plan")
    if plan_summary is not None:
        if observations is None:
            raise AuditConversationError(
                "procedure_plan response에 observations witness가 없습니다."
            )
        try:
            validate_procedure_plan_summary(
                plan_summary,
                observations=observations,
            )
        except ProcedurePlanningError as e:
            raise AuditConversationError(
                "procedure_plan response가 typed observation과 다릅니다."
            ) from e
    elif successful_plan:
        raise AuditConversationError(
            "성공한 procedure_planning response summary가 누락되었습니다."
        )
    inspection_supplement = validated.get("workbook_inspection")
    if inspection_supplement is not None:
        if observations is None:
            raise AuditConversationError(
                "workbook_inspection response에 observations witness가 없습니다."
            )
        try:
            validate_workbook_inspection_summary(
                inspection_supplement,
                observations=observations,
            )
        except WorkbookInspectionError as e:
            raise AuditConversationError(
                "workbook_inspection response가 typed observation과 다릅니다."
            ) from e
    return validated


def _aggregate_focus_projection(
    agent: _AuditAggregateAgentRuntime,
    prior_turns: list[dict],
    prior_responses: list[dict],
) -> tuple[dict | None, dict[str, set[str]]]:
    focus_turns = prior_turns[-MAX_FOCUS_TURNS:]
    focus_responses = prior_responses[-MAX_FOCUS_TURNS:]
    observed = {kind: set() for kind in _OBSERVED_KINDS}
    records: list[dict] = []
    seen_refs: set[str] = set()
    for response in reversed(focus_responses):
        try:
            candidates, candidate_observed = _aggregate_focus_records(agent, response)
        except AuditAggregateAgentError as e:
            raise AuditConversationError(
                f"aggregate conversation focus 재조회 실패: {e}"
            ) from e
        for wrapper in candidates:
            ref = wrapper.get("record_ref") or wrapper.get("source_ref")
            if not isinstance(ref, str) or ref in seen_refs:
                continue
            seen_refs.add(ref)
            records.append(wrapper)
            for kind in _OBSERVED_KINDS:
                if ref in candidate_observed[kind]:
                    observed[kind].add(ref)
            if len(records) >= MAX_FOCUS_RECORDS:
                break
        if len(records) >= MAX_FOCUS_RECORDS:
            break
    if not focus_turns:
        return None, observed
    return {
        "tool": "conversation_focus",
        "input": {"max_turns": MAX_FOCUS_TURNS, "max_records": MAX_FOCUS_RECORDS},
        "result": {
            "prior_turns": copy.deepcopy(focus_turns),
            "records": records,
            "authorization": (
                "Only exact refs in typed records are evidence for this turn; refs and "
                "local IDs in prior question or answer prose are not authorized."
            ),
        },
    }, observed


def _aggregate_focus_from_history_entries(
    context: AuditConversationRuntime,
    entries: list[dict],
) -> tuple[dict | None, dict[str, set[str]]]:
    agent = context.agent
    if not isinstance(agent, _AuditAggregateAgentRuntime):
        raise AuditConversationError("aggregate focus를 source bundle agent에 적용할 수 없습니다.")
    prior_turns: list[dict] = []
    prior_responses: list[dict] = []
    for record in entries:
        expected_focus, _ = _aggregate_focus_projection(
            agent,
            prior_turns,
            prior_responses,
        )
        question = _question_from_ref(
            context,
            record["question_ref"],
            invocation_id=record["invocation_id"],
        )
        response = _response_from_ref(
            context,
            record["response_ref"],
            invocation_id=record["invocation_id"],
            observations_ref=record.get("observations_ref"),
            expected_focus=expected_focus,
            expected_execution=record.get("execution"),
        )
        if response.get("context") != agent.context:
            raise AuditConversationBundleChangedError(
                "conversation history 응답의 audit aggregate가 현재 aggregate와 다릅니다."
            )
        if response.get("question") != question:
            raise AuditConversationError(
                "conversation history 질문과 응답이 일치하지 않습니다."
            )
        prior_turns.append({
            "turn_index": record["turn_index"],
            "question": question,
            "answer": response["answer"],
        })
        prior_responses.append(response)
    return _aggregate_focus_projection(agent, prior_turns, prior_responses)


def _focus_observation(
    context: AuditConversationRuntime,
    history_ref: object,
) -> tuple[dict | None, dict[str, set[str]]]:
    agent = context.agent
    if agent is None:
        return None, {kind: set() for kind in _OBSERVED_KINDS}
    all_entries = _history_entries(context, history_ref)
    if isinstance(agent, _AuditAggregateAgentRuntime):
        return _aggregate_focus_from_history_entries(context, all_entries)
    entries = all_entries[-MAX_FOCUS_TURNS:]
    prior_turns: list[dict] = []
    prior_responses: list[dict] = []
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in entries:
        question = _question_from_ref(
            context,
            record["question_ref"],
            invocation_id=record["invocation_id"],
        )
        response = _response_from_ref(
            context,
            record["response_ref"],
            invocation_id=record["invocation_id"],
            observations_ref=record.get("observations_ref"),
        )
        if response.get("bundle") != agent.bundle:
            raise AuditConversationBundleChangedError(
                "conversation history 응답의 audit bundle이 현재 bundle과 다릅니다."
            )
        if response.get("question") != question:
            raise AuditConversationError("conversation history 질문과 응답이 일치하지 않습니다.")
        prior_turns.append({
            "turn_index": record["turn_index"],
            "question": question,
            "answer": response["answer"],
        })
        prior_responses.append(response)
    for prior in reversed(prior_turns):
        for claim in prior["answer"].get("claims", []):
            for kind, field_name in (
                ("statement", "statement_ids"),
                ("fact", "fact_ids"),
                ("relation", "relation_ids"),
                ("standard_citation", "standard_citation_ids"),
            ):
                for item_id in claim.get(field_name, []):
                    key = (kind, item_id)
                    if key not in seen and item_id in agent.known[kind]:
                        seen.add(key)
                        candidates.append(key)
                    if len(candidates) >= MAX_FOCUS_RECORDS:
                        break
                if len(candidates) >= MAX_FOCUS_RECORDS:
                    break
            if len(candidates) >= MAX_FOCUS_RECORDS:
                break
        if len(candidates) >= MAX_FOCUS_RECORDS:
            break
    observed = {kind: set() for kind in _OBSERVED_KINDS}
    records: list[dict] = []
    for kind, item_id in candidates:
        try:
            result = _audit_get_loaded(
                agent.facts,
                agent.context,
                agent.brief_doc,
                item_id=item_id,
            )
        except AuditConsumeError as e:
            raise AuditConversationError(
                f"conversation focus record 재조회 실패: {e}"
            ) from e
        if result.get("kind") != kind or result.get("item", {}).get("id") != item_id:
            raise AuditConversationError("conversation focus typed record가 일치하지 않습니다.")
        records.append(result)
        observed[kind].add(item_id)
    if not prior_turns:
        return None, observed
    return {
        "tool": "conversation_focus",
        "input": {"max_turns": MAX_FOCUS_TURNS, "max_records": MAX_FOCUS_RECORDS},
        "result": {
            "prior_turns": prior_turns,
            "records": records,
            "authorization": (
                "Only exact IDs in records are typed evidence for this turn; IDs in prior "
                "question or answer prose are not authorized."
            ),
        },
    }, observed


def _bind_turn(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    requested = state.get("requested_bundle")
    if requested != context.bundle:
        raise AuditConversationError("graph input bundle과 runtime bundle이 일치하지 않습니다.")
    bound = state.get("bound_bundle")
    if bound is not None and bound != requested:
        raise AuditConversationBundleChangedError(
            "이 conversation thread는 다른 audit bundle에 묶여 있습니다. 새 thread를 사용하세요."
        )
    invocation_id = state.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise AuditConversationError("graph input invocation_id가 유효하지 않습니다.")
    question = _question_from_ref(
        context,
        state.get("question_ref"),
        invocation_id=invocation_id,
    )
    expected_question = (
        context.agent.question
        if context.agent is not None
        else context.blocked_response.get("question")
        if context.blocked_response is not None
        else None
    )
    if question != expected_question:
        raise AuditConversationError("graph question과 검증된 agent question이 다릅니다.")
    previous_turn_index = state.get("turn_index", 0)
    if not isinstance(previous_turn_index, int) or previous_turn_index < 0:
        raise AuditConversationError("checkpoint turn_index가 유효하지 않습니다.")
    history_ref = state.get("history_ref")
    entries = _history_entries(context, history_ref)
    if len(entries) != previous_turn_index:
        raise AuditConversationError("checkpoint turn_index와 history 길이가 다릅니다.")
    if previous_turn_index >= MAX_HISTORY_TURNS:
        raise AuditConversationError(
            f"한 thread는 최대 {MAX_HISTORY_TURNS} turn입니다. 새 thread를 사용하세요."
        )
    return {
        "bound_bundle": copy.deepcopy(requested),
        "history_ref": history_ref,
        "turn_index": previous_turn_index,
        "resumed": previous_turn_index > 0,
        "observations_ref": None,
        "decision_ref": None,
        "answer_ref": None,
        "usage_ref": None,
        "turn_record_ref": None,
        "observed_ids": {kind: [] for kind in _OBSERVED_KINDS},
        "used_tools": [],
        "discovery_complete": False,
        "step": 0,
        "route": "blocked" if context.blocked_response is not None else "bootstrap",
        "status": "bound",
    }


def _after_bind(state: AuditConversationState) -> str:
    return "store_blocked" if state.get("route") == "blocked" else "bootstrap"


def _bootstrap(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    agent = context.agent
    if agent is None:
        raise AuditConversationError("runnable conversation에 agent runtime이 없습니다.")
    turn_state = (
        _new_audit_aggregate_agent_turn_state(agent)
        if isinstance(agent, _AuditAggregateAgentRuntime)
        else _new_audit_agent_turn_state(agent)
    )
    focus, focus_observed = _focus_observation(context, state.get("history_ref"))
    if focus is not None:
        turn_state.observations.append(focus)
        _merge_observed(turn_state.observed, focus_observed)
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    return {
        "observations_ref": observations_ref,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "status": "deciding",
    }


def _research_binding(
    context: AuditConversationRuntime,
    request: dict,
) -> tuple[dict, str, dict]:
    agent = context.agent
    if agent is None:
        raise AuditConversationError("차단된 conversation은 standards research를 사용할 수 없습니다.")
    query = request.get("query")
    kind = request.get("kind")
    limit = request.get("limit")
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise AuditConversationError("standards_research query는 1~500자여야 합니다.")
    domain_framework = {
        "audit_standard": ("audit", "KSA"),
        "accounting_standard": ("accounting", "K-IFRS"),
    }.get(kind)
    if domain_framework is None:
        raise AuditConversationError("standards_research kind가 유효하지 않습니다.")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 5
    ):
        raise AuditConversationError("standards_research limit은 1~5여야 합니다.")
    domain, framework = domain_framework
    if isinstance(agent, _AuditAggregateAgentRuntime):
        if request.get("item_ref") is not None:
            raise AuditConversationError("standards_research에는 item_ref를 사용할 수 없습니다.")
        scope_id = request.get("scope_id")
        exposed_scope_ids = {
            account.get("scope", {}).get("id")
            for account in agent.bootstrap.get("accounts", [])
            if isinstance(account, dict)
        }
        snapshot = agent.sources.get(scope_id) if isinstance(scope_id, str) else None
        if snapshot is None or scope_id not in exposed_scope_ids:
            raise AuditConversationError(
                "standards_research scope_id는 aggregate bootstrap의 정확한 source여야 합니다."
            )
        retriever = snapshot.standards.get("retriever")
        expected_collection = (
            retriever.get("corpus_version") if isinstance(retriever, dict) else None
        )
        scope = snapshot.scope.identity()
    else:
        if request.get("item_id") is not None:
            raise AuditConversationError("standards_research에는 item_id를 사용할 수 없습니다.")
        scope_id = None
        expected_collection = agent.bundle.get("standards_corpus_version")
        scope = agent.scope.identity()
    if not isinstance(expected_collection, str) or not expected_collection:
        raise AuditConversationError(
            "committed standards collection을 research에 고정할 수 없습니다."
        )
    canonical = {
        "query": " ".join(query.split()),
        "domain": domain,
        "framework": framework,
        "scope_id": scope_id,
        "limit": limit,
    }
    return canonical, expected_collection, scope


def _available_research_records(observations: list[dict]) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for observation in observations:
        if observation.get("tool") != "standards_research":
            continue
        result = observation.get("result")
        if (
            isinstance(result, dict)
            and result.get("schema_version") == RESEARCH_RESULT_SCHEMA
        ):
            try:
                current = research_records(result)
            except StandardsResearchError as e:
                raise AuditConversationError(
                    "procedure_planning의 research basis를 검증할 수 없습니다."
                ) from e
            overlap = set(records) & set(current)
            if overlap:
                raise AuditConversationError(
                    "procedure_planning research ref가 중복되었습니다."
                )
            records.update(current)
    return records


def _planning_target(workbook_basis: list[dict]) -> tuple[dict, list[str]]:
    by_kind = {
        kind: [item for item in workbook_basis if item["record_kind"] == kind]
        for kind in ("account", "risk", "assertion", "procedure")
    }
    if len(by_kind["risk"]) != 1 or len(by_kind["assertion"]) != 1:
        raise AuditConversationError(
            "procedure_planning은 정확히 하나의 관찰된 risk와 assertion이 필요합니다."
        )
    if len(by_kind["account"]) > 1:
        raise AuditConversationError(
            "procedure_planning account 근거는 최대 하나여야 합니다."
        )
    target = {
        "account_ref": (
            by_kind["account"][0]["basis_ref"] if by_kind["account"] else None
        ),
        "risk_ref": by_kind["risk"][0]["basis_ref"],
        "assertion_ref": by_kind["assertion"][0]["basis_ref"],
    }
    return target, [item["basis_ref"] for item in by_kind["procedure"]]


def _planning_binding(
    context: AuditConversationRuntime,
    request: dict,
    *,
    observed: dict[str, set[str]],
    research: dict[str, dict],
) -> tuple[dict, dict]:
    agent = context.agent
    if agent is None:
        raise AuditConversationError(
            "차단된 conversation은 procedure planning을 사용할 수 없습니다."
        )
    query = request.get("query")
    limit = request.get("limit")
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise AuditConversationError("procedure_planning query는 1~500자여야 합니다.")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 3 <= limit <= 5
    ):
        raise AuditConversationError("procedure_planning limit은 3~5여야 합니다.")
    research_refs = request.get("research_refs")
    if (
        not isinstance(research_refs, list)
        or len(research_refs) > 3
        or len(research_refs) != len(set(research_refs))
        or any(not isinstance(ref, str) or ref not in research for ref in research_refs)
    ):
        raise AuditConversationError(
            "procedure_planning research_refs가 현재 turn의 선행 결과와 다릅니다."
        )

    workbook_basis: list[dict] = []
    standards_basis: list[dict] = []
    if isinstance(agent, _AuditAggregateAgentRuntime):
        source_refs = request.get("source_refs")
        scope_id = request.get("scope_id")
        observed_refs = set().union(*observed.values())
        if (
            not isinstance(source_refs, list)
            or not source_refs
            or len(source_refs) > 20
            or len(source_refs) != len(set(source_refs))
            or any(
                not isinstance(ref, str)
                or not ref.startswith("source:")
                or ref not in observed_refs
                or ref not in agent.source_records
                for ref in source_refs
            )
        ):
            raise AuditConversationError(
                "procedure_planning source_refs가 관찰된 exact source record가 아닙니다."
            )
        snapshot = agent.sources.get(scope_id) if isinstance(scope_id, str) else None
        exposed_scope_ids = {
            account.get("scope", {}).get("id")
            for account in agent.bootstrap.get("accounts", [])
            if isinstance(account, dict)
        }
        if snapshot is None or scope_id not in exposed_scope_ids:
            raise AuditConversationError(
                "procedure_planning scope_id는 aggregate bootstrap의 exact source여야 합니다."
            )
        if any(agent.source_records[ref].scope.id != scope_id for ref in source_refs):
            raise AuditConversationError(
                "procedure_planning은 여러 source sheet를 결합할 수 없습니다."
            )
        scope = snapshot.scope.identity()
        retriever = snapshot.standards.get("retriever")
        expected_collection = (
            retriever.get("corpus_version") if isinstance(retriever, dict) else None
        )
        if not isinstance(expected_collection, str) or not expected_collection:
            raise AuditConversationError(
                "procedure_planning standards collection을 source scope에 고정할 수 없습니다."
            )
        for ref in source_refs:
            source = agent.source_records[ref]
            if source.kind == "standard_citation":
                basis = _planning_prepared_standard_basis(
                    scope=scope,
                    source_ref=ref,
                    citation=source.item,
                    expected_collection=expected_collection,
                )
                if basis is None:
                    raise AuditConversationError(
                        "선택한 prepared standard citation은 원문 검증 메타데이터가 부족합니다."
                    )
                standards_basis.append(basis)
            else:
                workbook_basis.append(_planning_workbook_basis(
                    scope=scope,
                    source_kind=source.kind,
                    source_ref=ref,
                    item=source.item,
                    aggregate_source=True,
                ))
    else:
        fact_ids = request.get("fact_ids")
        relation_ids = request.get("relation_ids")
        citation_ids = request.get("standard_citation_ids")
        selected_groups = (
            ("fact", fact_ids, 20),
            ("relation", relation_ids, 20),
            ("standard_citation", citation_ids, 10),
        )
        for kind, values, maximum in selected_groups:
            if (
                not isinstance(values, list)
                or len(values) > maximum
                or len(values) != len(set(values))
                or any(
                    not isinstance(item_id, str)
                    or item_id not in observed[kind]
                    for item_id in values
                )
            ):
                raise AuditConversationError(
                    f"procedure_planning {kind} IDs가 현재 typed 관찰과 다릅니다."
                )
        scope = agent.scope.identity()
        expected_collection = agent.bundle.get("standards_corpus_version")
        if not isinstance(expected_collection, str) or not expected_collection:
            raise AuditConversationError(
                "procedure_planning standards collection을 bundle에 고정할 수 없습니다."
            )
        fact_map = {item["id"]: item for item in agent.facts.get("facts", [])}
        relation_map = {
            item["id"]: item for item in agent.facts.get("relations", [])
        }
        citation_map = {
            item["id"]: item for item in agent.context.get("citations", [])
        }
        for item_id in fact_ids:
            item = fact_map.get(item_id)
            if item is None:
                raise AuditConversationError("planning fact가 committed bundle에 없습니다.")
            workbook_basis.append(_planning_workbook_basis(
                scope=scope,
                source_kind="fact",
                source_ref=item_id,
                item=item,
            ))
        for item_id in relation_ids:
            item = relation_map.get(item_id)
            if item is None:
                raise AuditConversationError("planning relation이 committed bundle에 없습니다.")
            workbook_basis.append(_planning_workbook_basis(
                scope=scope,
                source_kind="relation",
                source_ref=item_id,
                item=item,
            ))
        for item_id in citation_ids:
            citation = citation_map.get(item_id)
            if citation is None:
                raise AuditConversationError(
                    "planning standard citation이 committed bundle에 없습니다."
                )
            basis = _planning_prepared_standard_basis(
                scope=scope,
                source_ref=item_id,
                citation=citation,
                expected_collection=expected_collection,
            )
            if basis is None:
                raise AuditConversationError(
                    "선택한 prepared standard citation은 원문 검증 메타데이터가 부족합니다."
                )
            standards_basis.append(basis)

    for ref in research_refs:
        record = research[ref]
        if record.get("scope") != scope or record.get("collection") != expected_collection:
            raise AuditConversationError(
                "procedure_planning research ref가 target scope/collection과 다릅니다."
            )
        standards_basis.append(_planning_research_standard_basis(record))
    target, existing = _planning_target(workbook_basis)
    canonical = {
        "objective": " ".join(query.split()),
        "target": target,
        "workbook_basis": workbook_basis,
        "standards_basis": standards_basis,
        "existing_procedure_refs": existing,
        "candidate_count": limit,
    }
    return canonical, scope


def _run_research_request(
    context: AuditConversationRuntime,
    request: dict,
    *,
    invocation_id: str,
) -> dict:
    if not context.standards_research_enabled:
        return _research_error("RESEARCH_DISABLED")
    if context._research_request_count >= MAX_RESEARCH_REQUESTS:
        return _research_error("RESEARCH_LIMIT_EXCEEDED")
    context._research_request_count += 1
    research_client: _CountedChildModelClient | None = None
    try:
        canonical, collection, scope = _research_binding(context, request)
        retriever = context.research_retriever(collection)
        bundle_sha256 = hashlib.sha256(
            json.dumps(
                context.bundle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        research_client = _CountedChildModelClient(context.model_client())
        return run_standards_research(
            canonical,
            runtime=StandardsResearchRuntime(
                retriever=retriever,
                client=research_client,
                model=context.agent.model,
                expected_collection=collection,
                invocation_id=invocation_id,
                bundle_sha256=bundle_sha256,
                scope=scope,
                eprint=context.eprint,
            ),
        )
    except StandardsResearchError as e:
        return _research_error(e.code)
    except AuditConversationError:
        return _research_error("INVALID_REQUEST")
    except Exception:  # noqa: BLE001 - no provider detail may reach model/checkpoint
        return _research_error("UPSTREAM_UNAVAILABLE")
    finally:
        if research_client is not None:
            context._child_model_call_count += research_client.calls


def _run_planning_request(
    context: AuditConversationRuntime,
    request: dict,
    *,
    invocation_id: str,
    observed: dict[str, set[str]],
    observations: list[dict],
) -> dict:
    if not context.procedure_planning_enabled:
        return _planning_error("PLANNING_DISABLED")
    if context._planning_request_count >= MAX_PLANNING_REQUESTS:
        return _planning_error("PLANNING_LIMIT_EXCEEDED")
    planning_client: _CountedChildModelClient | None = None
    try:
        canonical, scope = _planning_binding(
            context,
            request,
            observed=observed,
            research=_available_research_records(observations),
        )
        if not canonical["standards_basis"] and context.standards_research_enabled:
            return _planning_error("RESEARCH_REQUIRED")
        context._planning_request_count += 1
        bundle_sha256 = hashlib.sha256(
            json.dumps(
                context.bundle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        planning_client = _CountedChildModelClient(context.model_client())
        return run_procedure_planning(
            canonical,
            runtime=ProcedurePlanningRuntime(
                client=planning_client,
                model=context.agent.model,
                invocation_id=invocation_id,
                bundle_sha256=bundle_sha256,
                scope=scope,
                eprint=context.eprint,
            ),
        )
    except ProcedurePlanningError as e:
        return _planning_error(e.code)
    except AuditConversationError:
        return _planning_error("INVALID_REQUEST")
    except Exception:  # noqa: BLE001 - no provider detail may reach model/checkpoint
        return _planning_error("UPSTREAM_UNAVAILABLE")
    finally:
        if planning_client is not None:
            context._child_model_call_count += planning_client.calls


def _run_inspection_request(
    context: AuditConversationRuntime,
    request: dict,
    *,
    prior_observations: list[dict],
) -> dict:
    if not context.workbook_inspection_enabled:
        return _inspection_error("INSPECTION_DISABLED")
    if context._inspection_request_count >= MAX_INSPECTION_REQUESTS:
        return _inspection_error("INSPECTION_LIMIT_EXCEEDED")
    context._inspection_request_count += 1
    try:
        _assert_inspection_scope_consistent(request, prior_observations)
        _assert_inspection_ledger_first(request, prior_observations)
        canonical, scope, scope_id = _inspection_binding(context, request)
        _assert_runtime_bundle_current(context)
        result = run_workbook_inspection(
            context.package_path,
            canonical,
            scope=scope,
            scope_id=scope_id,
            source_provider=context.workbook_source_provider,
        )
        _assert_runtime_bundle_current(context)
        return result
    except WorkbookInspectionError as e:
        mapped = {
            "INVALID_INPUT": "INVALID_REQUEST",
            "INVALID_REQUEST": "INVALID_REQUEST",
            "LIMIT_EXCEEDED": "INVALID_REQUEST",
            "SOURCE_UNAVAILABLE": "SOURCE_UNAVAILABLE",
            "SOURCE_DIGEST_MISMATCH": "SOURCE_UNAVAILABLE",
            "SOURCE_LIMIT_EXCEEDED": "SOURCE_UNAVAILABLE",
            "INVALID_SOURCE_BINDING": "SOURCE_UNAVAILABLE",
            "SOURCE_CONTRACT_MISMATCH": "SOURCE_UNAVAILABLE",
            "RAW_FORMAT_UNSUPPORTED": "SOURCE_UNAVAILABLE",
            "PACKAGE_INVALID": "CONTRACT_MISMATCH",
            "CONTRACT_MISMATCH": "CONTRACT_MISMATCH",
        }.get(e.code, "UPSTREAM_UNAVAILABLE")
        return _inspection_error(mapped)
    except AuditConversationBundleChangedError:
        raise
    except AuditConversationError:
        return _inspection_error("INVALID_REQUEST")
    except Exception:  # noqa: BLE001 - provider details must remain private
        return _inspection_error("UPSTREAM_UNAVAILABLE")


def _decide(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    agent = context.agent
    if agent is None:
        raise AuditConversationError("decide node에 agent runtime이 없습니다.")
    step = state.get("step")
    if not isinstance(step, int) or step < 0:
        raise AuditConversationError("checkpoint step이 유효하지 않습니다.")
    if step + context._child_model_call_count >= agent.max_steps:
        raise AuditConversationError(
            f"{agent.max_steps}회 안에 근거가 검증된 최종 답변을 만들지 못했습니다. "
            "이 상한에는 child research/planning 모델 호출도 포함됩니다."
        )
    turn_state = _turn_state(context, state)
    try:
        if isinstance(agent, _AuditAggregateAgentRuntime):
            decision = _request_audit_aggregate_agent_model_turn(
                agent,
                turn_state,
                client=context.model_client(),
                step=step + 1,
                child_model_calls=context._child_model_call_count,
                capabilities=context.capabilities(),
                eprint=context.eprint,
            )
        else:
            decision = _request_audit_agent_model_turn(
                agent,
                turn_state,
                client=context.model_client(),
                step=step + 1,
                child_model_calls=context._child_model_call_count,
                capabilities=context.capabilities(),
                eprint=context.eprint,
            )
    except (AuditAgentError, AuditAggregateAgentError) as e:
        if "600KB 모델 예산" in str(e):
            raise AuditConversationError(
                "audit agent 입력이 600KB 모델 예산을 초과했습니다. "
                "--limit을 낮추거나 질문 범위를 좁혀 주세요."
            ) from e
        raise AuditConversationError(
            "audit-chat 모델 호출 또는 구조화 응답 검증에 실패했습니다."
        ) from e
    decision_ref = _write_invocation_payload(
        context,
        state,
        kind="decision",
        schema=DECISION_SCHEMA,
        value=decision,
    )
    route = "finalize"
    if decision.get("action") == "tool":
        request = decision.get("tool")
        if isinstance(request, dict) and request.get("name") == "standards_research":
            route = "research"
        elif isinstance(request, dict) and request.get("name") == "procedure_planning":
            route = "planning"
        elif isinstance(request, dict) and request.get("name") == "workbook_inspection":
            route = "inspection"
        else:
            route = "tool"
    return {
        "decision_ref": decision_ref,
        "step": step + 1,
        "route": route,
        "status": "executing",
    }


def _after_decide(state: AuditConversationState) -> str:
    if state.get("route") == "research":
        return "execute_research"
    if state.get("route") == "planning":
        return "execute_plan"
    if state.get("route") == "inspection":
        return "execute_inspection"
    return "execute_tool" if state.get("route") == "tool" else "finalize"


def _decision(
    context: AuditConversationRuntime,
    state: AuditConversationState,
) -> dict:
    value = _invocation_payload(
        context,
        state,
        state.get("decision_ref"),
        kind="decision",
        schema=DECISION_SCHEMA,
    )
    if not isinstance(value, dict):
        raise AuditConversationError("decision artifact는 객체여야 합니다.")
    return value


def _execute_tool(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    agent = context.agent
    if agent is None:
        raise AuditConversationError("tool node에 agent runtime이 없습니다.")
    turn_state = _turn_state(context, state)
    try:
        if isinstance(agent, _AuditAggregateAgentRuntime):
            _apply_audit_aggregate_agent_tool_turn(
                agent, turn_state, _decision(context, state)
            )
        else:
            _apply_audit_agent_tool_turn(agent, turn_state, _decision(context, state))
    except (AuditAgentError, AuditAggregateAgentError) as e:
        raise AuditConversationError("audit-chat 도구 실행 검증에 실패했습니다.") from e
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    return {
        "observations_ref": observations_ref,
        "decision_ref": None,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "status": "deciding",
    }


def _execute_research(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    if context.agent is None:
        raise AuditConversationError("research node에 agent runtime이 없습니다.")
    turn_state = _turn_state(context, state)
    decision = _decision(context, state)
    request = decision.get("tool")
    if (
        decision.get("action") != "tool"
        or not isinstance(request, dict)
        or request.get("name") != "standards_research"
        or decision.get("final") is not None
    ):
        raise AuditConversationError(
            "research node decision 계약이 유효하지 않습니다."
        )
    request_key = _research_request_key(request)
    invocation_id = state.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise AuditConversationError("research invocation_id가 유효하지 않습니다.")
    if request_key in turn_state.seen_tool_requests:
        result = _research_error("RESEARCH_LIMIT_EXCEEDED")
    else:
        turn_state.seen_tool_requests.add(request_key)
        result = _run_research_request(
            context,
            request,
            invocation_id=invocation_id,
        )
    context._research_results.setdefault(request_key, []).append(copy.deepcopy(result))
    turn_state.observations.append({
        "tool": "standards_research",
        "input": copy.deepcopy(request),
        "result": copy.deepcopy(result),
    })
    turn_state.used_tools.append("standards_research")
    turn_state.discovery_complete = (
        turn_state.discovery_complete
        and result.get("schema_version") == RESEARCH_RESULT_SCHEMA
    )
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    return {
        "observations_ref": observations_ref,
        "decision_ref": None,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "status": "deciding",
    }


def _execute_plan(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    if context.agent is None:
        raise AuditConversationError("planning node에 agent runtime이 없습니다.")
    turn_state = _turn_state(context, state)
    decision = _decision(context, state)
    request = decision.get("tool")
    if (
        decision.get("action") != "tool"
        or not isinstance(request, dict)
        or request.get("name") != "procedure_planning"
        or decision.get("final") is not None
    ):
        raise AuditConversationError("planning node decision 계약이 유효하지 않습니다.")
    request_key = _planning_request_key(request)
    invocation_id = state.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise AuditConversationError("planning invocation_id가 유효하지 않습니다.")
    if request_key in turn_state.seen_tool_requests:
        result = _planning_error("PLANNING_LIMIT_EXCEEDED")
    else:
        turn_state.seen_tool_requests.add(request_key)
        result = _run_planning_request(
            context,
            request,
            invocation_id=invocation_id,
            observed=turn_state.observed,
            observations=turn_state.observations,
        )
    context._planning_results.setdefault(request_key, []).append(copy.deepcopy(result))
    turn_state.observations.append({
        "tool": "procedure_planning",
        "input": copy.deepcopy(request),
        "result": copy.deepcopy(result),
    })
    turn_state.used_tools.append("procedure_planning")
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    return {
        "observations_ref": observations_ref,
        "decision_ref": None,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "status": "deciding",
    }


def _execute_inspection(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    turn_state = _turn_state(context, state)
    decision = _decision(context, state)
    request = decision.get("tool")
    if (
        decision.get("action") != "tool"
        or not isinstance(request, dict)
        or request.get("name") != "workbook_inspection"
    ):
        raise AuditConversationError(
            "inspection node에는 workbook_inspection 요청이 필요합니다."
        )
    request_key = _inspection_request_key(request)
    if request_key in turn_state.seen_tool_requests:
        result = _inspection_error("DUPLICATE_REQUEST")
    else:
        turn_state.seen_tool_requests.add(request_key)
        result = _run_inspection_request(
            context,
            request,
            prior_observations=turn_state.observations,
        )
    context._inspection_results.setdefault(request_key, []).append(
        copy.deepcopy(result)
    )
    turn_state.observations.append({
        "tool": "workbook_inspection",
        "input": copy.deepcopy(request),
        "result": copy.deepcopy(result),
    })
    turn_state.used_tools.append("workbook_inspection")
    turn_state.discovery_complete = (
        turn_state.discovery_complete
        and result.get("schema_version") == INSPECTION_RESULT_SCHEMA
    )
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    return {
        "observations_ref": observations_ref,
        "decision_ref": None,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "status": "deciding",
    }


def _finalize(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    agent = context.agent
    if agent is None:
        raise AuditConversationError("finalize node에 agent runtime이 없습니다.")
    turn_state = _turn_state(context, state)
    step = state.get("step")
    if not isinstance(step, int) or step < 1:
        raise AuditConversationError("finalize step이 유효하지 않습니다.")
    decision = _decision(context, state)
    final_plan = decision.get("final")
    research_refs: list[str] = []
    plan_refs: list[str] = []
    inspection_refs: list[str] = []
    if isinstance(final_plan, dict):
        raw_refs = final_plan.get("research_refs", [])
        if not isinstance(raw_refs, list) or any(
            not isinstance(ref, str) for ref in raw_refs
        ):
            raise AuditConversationError("final research_refs가 유효하지 않습니다.")
        research_refs = list(raw_refs)
        raw_plan_refs = final_plan.get("plan_refs", [])
        if not isinstance(raw_plan_refs, list) or any(
            not isinstance(ref, str) for ref in raw_plan_refs
        ):
            raise AuditConversationError("final plan_refs가 유효하지 않습니다.")
        plan_refs = list(raw_plan_refs)
        raw_inspection_refs = final_plan.get("inspection_refs", [])
        if not isinstance(raw_inspection_refs, list) or any(
            not isinstance(ref, str) for ref in raw_inspection_refs
        ):
            raise AuditConversationError("final inspection_refs가 유효하지 않습니다.")
        inspection_refs = list(raw_inspection_refs)
    base_decision = copy.deepcopy(decision)
    base_final = base_decision.get("final")
    if isinstance(base_final, dict):
        base_final.pop("research_refs", None)
        base_final.pop("plan_refs", None)
        base_final.pop("inspection_refs", None)
        if (
            (research_refs or plan_refs or inspection_refs)
            and not base_final.get("selections")
            and base_final.get("abstained") is False
        ):
            # Research/planning/inspection supplements never become committed workpaper evidence.
            # The inner evidence answer remains explicitly abstained when they are the only output.
            base_final["abstained"] = True
            base_final["abstention_code"] = "insufficient_evidence"
    try:
        if isinstance(agent, _AuditAggregateAgentRuntime):
            response = _finalize_audit_aggregate_agent_turn(
                agent,
                turn_state,
                base_decision,
                step=step,
            )
        else:
            response = _finalize_audit_agent_turn(
                agent,
                turn_state,
                base_decision,
                step=step,
            )
    except (AuditAgentError, AuditAggregateAgentError) as e:
        if isinstance(e, AuditAggregateAgentChangedError) or (
            "bundle identity가 변경" in str(e)
            or "package bundle identity" in str(e)
        ):
            raise AuditConversationBundleChangedError(
                "audit-chat 실행 중 committed audit bundle이 변경되었습니다."
            ) from e
        raise AuditConversationError("audit-chat 최종 근거 검증에 실패했습니다.") from e
    if response is not None:
        try:
            supplement = research_summary(
                turn_state.observations,
                selected_refs=research_refs,
            )
        except StandardsResearchError:
            turn_state.observations.append({
                "tool": "answer_validation",
                "result": {
                    "error": {
                        "code": "UNGROUNDED_RESEARCH_FINAL",
                        "message": "선택한 동적 기준서 ref가 현재 turn의 typed 결과와 다릅니다.",
                    }
                },
            })
            response = None
        else:
            if supplement is not None:
                response = copy.deepcopy(response)
                response["standards_research"] = supplement
                response = (
                    _validated_aggregate_response(agent, response)
                    if isinstance(agent, _AuditAggregateAgentRuntime)
                    else _validated_response(response)
                )
    if response is not None:
        try:
            inspection_supplement = inspection_summary(
                turn_state.observations,
                selected_refs=inspection_refs,
            )
        except WorkbookInspectionError:
            turn_state.observations.append({
                "tool": "answer_validation",
                "result": {
                    "error": {
                        "code": "UNGROUNDED_INSPECTION_FINAL",
                        "message": (
                            "선택한 inspection ref가 현재 turn의 typed 결과와 다릅니다."
                        ),
                    }
                },
            })
            response = None
        else:
            if inspection_supplement is not None:
                response = copy.deepcopy(response)
                response["workbook_inspection"] = inspection_supplement
                response = (
                    _validated_aggregate_response(agent, response)
                    if isinstance(agent, _AuditAggregateAgentRuntime)
                    else _validated_response(response)
                )
    if response is not None:
        try:
            plan_supplement = procedure_plan_summary(
                turn_state.observations,
                selected_refs=plan_refs,
            )
            successful_plan = any(
                item.get("tool") == "procedure_planning"
                and isinstance(item.get("result"), dict)
                and item["result"].get("schema_version") == PLANNING_RESULT_SCHEMA
                for item in turn_state.observations
            )
            if successful_plan and plan_supplement is None:
                raise ProcedurePlanningError(
                    "CONTRACT_MISMATCH",
                    "생성된 procedure plan ref가 final에서 선택되지 않았습니다.",
                )
        except ProcedurePlanningError:
            turn_state.observations.append({
                "tool": "answer_validation",
                "result": {
                    "error": {
                        "code": "UNGROUNDED_PLANNING_FINAL",
                        "message": (
                            "선택한 procedure plan ref가 현재 turn의 typed 결과와 다릅니다."
                        ),
                    }
                },
            })
            response = None
        else:
            if plan_supplement is not None:
                response = copy.deepcopy(response)
                response["procedure_plan"] = plan_supplement
                response = (
                    _validated_aggregate_response(agent, response)
                    if isinstance(agent, _AuditAggregateAgentRuntime)
                    else _validated_response(response)
                )
    observations_ref = _write_invocation_payload(
        context,
        state,
        kind="observations",
        schema=OBSERVATIONS_SCHEMA,
        value=turn_state.observations,
    )
    if response is None:
        return {
            "observations_ref": observations_ref,
            "decision_ref": None,
            "observed_ids": _observed_json(turn_state.observed),
            "used_tools": turn_state.used_tools,
            "discovery_complete": turn_state.discovery_complete,
            "route": "retry",
            "status": "deciding",
        }
    response_ref = _write_invocation_payload(
        context,
        state,
        kind="response",
        schema=RESPONSE_SCHEMA,
        value=response,
    )
    return {
        "observations_ref": observations_ref,
        "decision_ref": None,
        "answer_ref": response_ref,
        "observed_ids": _observed_json(turn_state.observed),
        "used_tools": turn_state.used_tools,
        "discovery_complete": turn_state.discovery_complete,
        "route": "commit",
        "status": "finalized",
    }


def _after_finalize(state: AuditConversationState) -> str:
    return "commit_turn" if state.get("route") == "commit" else "decide"


def _store_blocked(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    response = context.blocked_response
    if not isinstance(response, dict):
        raise AuditConversationError("blocked response가 없습니다.")
    response_ref = _write_invocation_payload(
        context,
        state,
        kind="response",
        schema=RESPONSE_SCHEMA,
        value=_validated_response(response),
    )
    return {
        "answer_ref": response_ref,
        "route": "commit",
        "status": "finalized",
    }


def _usage_summary(events: list[dict]) -> dict:
    clean: list[dict] = []
    for index, event in enumerate(events, 1):
        if not isinstance(event, dict):
            continue
        provider = str(event.get("provider", "unknown")).strip()[:100] or "unknown"
        model = str(event.get("model", "unknown")).strip()[:200] or "unknown"
        item = {
            "event_id": f"request:{index}",
            "provider": provider,
            "model": model,
        }
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = event.get(name, 0)
            item[name] = (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else 0
            )
        for name in ("input_token_details", "output_token_details"):
            value = event.get(name)
            if not isinstance(value, dict):
                continue
            details = {
                str(key)[:100]: count
                for key, count in value.items()
                if isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
            }
            if details:
                item[name] = details
        clean.append(item)
    return {
        "requests": clean,
        "request_count": len(clean),
        "input_tokens": sum(
            item.get("input_tokens", 0)
            for item in clean
            if isinstance(item.get("input_tokens", 0), int)
        ),
        "output_tokens": sum(
            item.get("output_tokens", 0)
            for item in clean
            if isinstance(item.get("output_tokens", 0), int)
        ),
        "total_tokens": sum(
            item.get("total_tokens", 0)
            for item in clean
            if isinstance(item.get("total_tokens", 0), int)
        ),
    }


def _validated_usage_summary(events: list[dict]) -> dict:
    summary = _usage_summary(events)
    try:
        result_schema = load_schema(RESULT_SCHEMA)
        definitions = result_schema["definitions"]
        usage_schema = {
            "$schema": result_schema.get(
                "$schema",
                "http://json-schema.org/draft-07/schema#",
            ),
            "$ref": "#/definitions/usage",
            "definitions": definitions,
        }
        jsonschema.validate(summary, usage_schema)
    except Exception as e:  # noqa: BLE001 - fail closed before the turn commit
        raise AuditConversationError(
            "conversation usage 기록이 허용된 요청 수 또는 계약을 초과했습니다."
        ) from e
    return summary


def _commit_turn_locked(
    state: AuditConversationState,
    context: AuditConversationRuntime,
) -> dict:
    """Validate and publish one private turn while the package snapshot is pinned."""
    invocation_id = state.get("invocation_id")
    if not isinstance(invocation_id, str):
        raise AuditConversationError("commit invocation_id가 유효하지 않습니다.")
    canonical_turn_state = (
        _turn_state(context, state) if context.agent is not None else None
    )
    previous = _history_entries(context, state.get("history_ref"))
    expected_focus = None
    execution = None
    if isinstance(context.agent, _AuditAggregateAgentRuntime):
        expected_focus, _ = _aggregate_focus_from_history_entries(context, previous)
        execution = _execution_witness(
            _aggregate_generator_profile(
                context.agent,
                turns=state.get("step"),
                tools_used=canonical_turn_state.used_tools,
            )
        )
    response = _response_from_ref(
        context,
        state.get("answer_ref"),
        invocation_id=invocation_id,
        observations_ref=state.get("observations_ref"),
        expected_focus=expected_focus,
        expected_execution=execution,
    )
    response_binding = response.get(
        "context"
        if isinstance(context.agent, _AuditAggregateAgentRuntime)
        else "bundle"
    )
    if response_binding != state.get("bound_bundle"):
        raise AuditConversationBundleChangedError(
            "최종 response의 bundle이 thread binding과 일치하지 않습니다."
        )
    question = _question_from_ref(
        context,
        state.get("question_ref"),
        invocation_id=invocation_id,
    )
    if response.get("question") != question:
        raise AuditConversationError("최종 response의 질문이 현재 turn과 다릅니다.")
    usage_ref = _write_invocation_payload(
        context,
        state,
        kind="usage",
        schema=USAGE_SCHEMA,
        value=_validated_usage_summary(context.usage_events()),
    )
    turn_index = len(previous) + 1
    try:
        turn_payload = {
            "turn_index": turn_index,
            "invocation_id": invocation_id,
            "question_ref": state["question_ref"],
            "response_ref": state["answer_ref"],
            "usage_ref": usage_ref,
        }
        observations_ref = state.get("observations_ref")
        if isinstance(observations_ref, dict):
            turn_payload["observations_ref"] = observations_ref
        elif isinstance(context.agent, _AuditAggregateAgentRuntime):
            raise AuditConversationError(
                "aggregate conversation observations witness가 없습니다."
            )
        if execution is not None:
            turn_payload["execution"] = execution
        turn_record_ref = context.store.write(
            context.thread_id,
            kind="turn_record",
            schema_version=TURN_RECORD_SCHEMA,
            payload=turn_payload,
        )
        prior_refs = _history(context, state.get("history_ref"))
        history_ref = context.store.write(
            context.thread_id,
            kind="history",
            schema_version=HISTORY_SCHEMA,
            payload={"turn_refs": [*prior_refs, turn_record_ref]},
        )
    except ConversationArtifactStoreError as e:
        raise AuditConversationError(f"conversation turn commit 저장 실패: {e}") from e
    return {
        "history_ref": history_ref,
        "turn_index": turn_index,
        "usage_ref": usage_ref,
        "turn_record_ref": turn_record_ref,
        "status": "completed",
    }


def _commit_turn(state: AuditConversationState, runtime) -> dict:
    context = _context(runtime)
    with cache.package_lock(context.package_path):
        _assert_runtime_bundle_current(context)
        return _commit_turn_locked(state, context)


def build_audit_conversation_graph(checkpointer):
    """Compile the dynamic conversation workflow with an explicit root checkpointer."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise AuditConversationError(
            "audit-chat에는 graph extra가 필요합니다: uv sync --extra graph"
        ) from e
    if checkpointer is None or isinstance(checkpointer, bool):
        raise AuditConversationError("conversation root graph에는 실제 checkpointer가 필요합니다.")
    builder = StateGraph(
        AuditConversationState,
        context_schema=AuditConversationRuntime,
        input_schema=ConversationInput,
        output_schema=ConversationOutput,
    )
    builder.add_node("bind_turn", _checkpoint_safe_node("bind_turn", _bind_turn))
    builder.add_node("bootstrap", _checkpoint_safe_node("bootstrap", _bootstrap))
    builder.add_node("decide", _checkpoint_safe_node("decide", _decide))
    builder.add_node(
        "execute_tool",
        _checkpoint_safe_node("execute_tool", _execute_tool),
    )
    builder.add_node(
        "execute_research",
        _checkpoint_safe_node("execute_research", _execute_research),
    )
    builder.add_node(
        "execute_plan",
        _checkpoint_safe_node("execute_plan", _execute_plan),
    )
    builder.add_node(
        "execute_inspection",
        _checkpoint_safe_node("execute_inspection", _execute_inspection),
    )
    builder.add_node("finalize", _checkpoint_safe_node("finalize", _finalize))
    builder.add_node(
        "store_blocked",
        _checkpoint_safe_node("store_blocked", _store_blocked),
    )
    builder.add_node(
        "commit_turn",
        _checkpoint_safe_node("commit_turn", _commit_turn),
    )
    builder.add_edge(START, "bind_turn")
    builder.add_conditional_edges(
        "bind_turn",
        _after_bind,
        {"bootstrap": "bootstrap", "store_blocked": "store_blocked"},
    )
    builder.add_edge("bootstrap", "decide")
    builder.add_conditional_edges(
        "decide",
        _after_decide,
        {
            "execute_tool": "execute_tool",
            "execute_research": "execute_research",
            "execute_plan": "execute_plan",
            "execute_inspection": "execute_inspection",
            "finalize": "finalize",
        },
    )
    builder.add_edge("execute_tool", "decide")
    builder.add_edge("execute_research", "decide")
    builder.add_edge("execute_plan", "decide")
    builder.add_edge("execute_inspection", "decide")
    builder.add_conditional_edges(
        "finalize",
        _after_finalize,
        {"decide": "decide", "commit_turn": "commit_turn"},
    )
    builder.add_edge("store_blocked", "commit_turn")
    builder.add_edge("commit_turn", END)
    return builder.compile(checkpointer=checkpointer, name="audit_conversation")


def _private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


@contextmanager
def _sqlite_saver(root: Path) -> Iterator[object]:
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as e:
        raise AuditConversationError(
            "audit-chat SQLite persistence에는 graph extra가 필요합니다."
        ) from e
    database = root / "checkpoints.sqlite3"
    if database.is_symlink():
        raise AuditConversationError("conversation checkpoint DB는 symbolic link일 수 없습니다.")
    if database.exists():
        try:
            if not stat.S_ISREG(database.stat(follow_symlinks=False).st_mode):
                raise AuditConversationError("conversation checkpoint DB는 일반 파일이어야 합니다.")
        except OSError as e:
            raise AuditConversationError(f"conversation checkpoint DB 확인 실패: {e}") from e
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database, flags, 0o600)
        except FileExistsError:
            if database.is_symlink():
                raise AuditConversationError(
                    "conversation checkpoint DB는 symbolic link일 수 없습니다."
                )
        except OSError as e:
            raise AuditConversationError(f"conversation checkpoint DB 생성 실패: {e}") from e
        else:
            os.close(descriptor)
    connection = sqlite3.connect(database, timeout=30, check_same_thread=False)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        serializer = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        )
        saver = SqliteSaver(connection, serde=serializer)
        saver.setup()
        _private_mode(database, 0o600)
        yield saver
    finally:
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.exists() and not path.is_symlink():
                _private_mode(path, 0o600)


def _invoke_graph(
    *,
    checkpointer,
    context: AuditConversationRuntime,
    question_ref: ArtifactRef,
    invocation_id: str,
) -> dict:
    graph = build_audit_conversation_graph(checkpointer)
    max_steps = context.agent.max_steps if context.agent is not None else 1
    config = {
        "configurable": {"thread_id": _thread_digest(context.thread_id)},
        "recursion_limit": max_steps * 3 + 12,
    }
    try:
        result = graph.invoke(
            {
                "question_ref": question_ref,
                "requested_bundle": copy.deepcopy(context.bundle),
                "invocation_id": invocation_id,
            },
            config=config,
            context=context,
        )
    except _CheckpointSafeNodeError:
        failure = context._node_failure
        if failure is None:
            raise AuditConversationError(
                "audit-chat node가 안전하게 완료되지 않았습니다."
            ) from None
        node, error = failure
        if isinstance(error, AuditConversationBundleChangedError):
            raise AuditConversationBundleChangedError(str(error)) from None
        if isinstance(error, AuditConversationError):
            raise AuditConversationError(str(error)) from None
        raise AuditConversationError(
            f"audit-chat {node} 단계가 안전하게 완료되지 않았습니다 "
            f"({type(error).__name__})."
        ) from None
    except Exception as e:  # noqa: BLE001 - graph runtime boundary
        raise AuditConversationError(
            "audit-chat graph runtime이 안전하게 완료되지 않았습니다."
        ) from e
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise AuditConversationError("audit-chat graph가 완료 상태를 반환하지 않았습니다.")
    return result


def _turn_result(
    context: AuditConversationRuntime,
    graph_result: dict,
    *,
    invocation_id: str,
    question_ref: ArtifactRef,
) -> dict:
    turn_record_ref = graph_result.get("turn_record_ref")
    history_refs = _history(context, graph_result.get("history_ref"))
    history_records = _history_entries(context, graph_result.get("history_ref"))
    turn_index = graph_result.get("turn_index")
    if not isinstance(turn_index, int) or len(history_refs) != turn_index:
        raise AuditConversationError(
            "conversation graph turn_index와 committed history 길이가 다릅니다."
        )
    if not history_refs or history_refs[-1] != turn_record_ref:
        raise AuditConversationError(
            "conversation graph의 최신 turn record가 committed history와 다릅니다."
        )
    record = history_records[-1]
    if record["turn_index"] != turn_index or record["invocation_id"] != invocation_id:
        raise AuditConversationError(
            "conversation graph turn record identity가 현재 invocation과 다릅니다."
        )
    if record["question_ref"] != question_ref:
        raise AuditConversationError(
            "conversation graph question ref가 현재 invocation과 다릅니다."
        )
    if record["response_ref"] != graph_result.get("answer_ref"):
        raise AuditConversationError(
            "conversation graph response ref가 committed turn record와 다릅니다."
        )
    if record["usage_ref"] != graph_result.get("usage_ref"):
        raise AuditConversationError(
            "conversation graph usage ref가 committed turn record와 다릅니다."
        )
    expected_focus = None
    if isinstance(context.agent, _AuditAggregateAgentRuntime):
        expected_focus, _ = _aggregate_focus_from_history_entries(
            context,
            history_records[:-1],
        )
    response = _response_from_ref(
        context,
        record["response_ref"],
        invocation_id=record["invocation_id"],
        observations_ref=record.get("observations_ref"),
        expected_focus=expected_focus,
        expected_execution=record.get("execution"),
    )
    usage = _invocation_payload(
        context,
        {"invocation_id": record["invocation_id"]},
        record["usage_ref"],
        kind="usage",
        schema=USAGE_SCHEMA,
    )
    if not isinstance(usage, dict):
        raise AuditConversationError("conversation usage summary가 유효하지 않습니다.")
    document = {
        "schema_version": "audit_conversation_turn_result.v1",
        "thread_id": context.thread_id,
        "turn_index": turn_index,
        "resumed": graph_result["resumed"],
        "bundle": copy.deepcopy(graph_result["bound_bundle"]),
        "response": response,
        "usage": usage,
    }
    try:
        jsonschema.validate(document, load_schema(RESULT_SCHEMA))
    except (jsonschema.ValidationError, AuditLLMError) as e:
        raise AuditConversationError(f"conversation turn result 계약 검증 실패: {e}") from e
    response_binding = response.get(
        "context"
        if isinstance(context.agent, _AuditAggregateAgentRuntime)
        else "bundle"
    )
    if document["bundle"] != response_binding:
        raise AuditConversationError("conversation result bundle과 response bundle이 다릅니다.")
    if document["resumed"] != (document["turn_index"] > 1):
        raise AuditConversationError("conversation resumed 상태와 turn_index가 다릅니다.")
    requests = usage["requests"]
    if usage["request_count"] != len(requests):
        raise AuditConversationError("conversation usage request_count가 실제 목록과 다릅니다.")
    for field_name in ("input_tokens", "output_tokens", "total_tokens"):
        if usage[field_name] != sum(item[field_name] for item in requests):
            raise AuditConversationError(
                f"conversation usage {field_name} 합계가 request 목록과 다릅니다."
            )
    return document


def run_audit_conversation_turn(
    pkg: Path | str,
    *,
    model: str,
    question: str,
    thread_id: str | None = None,
    sheet: str | None = None,
    aggregate_id: str | None = None,
    limit: int = 100,
    max_steps: int = 6,
    client=None,
    client_factory=None,
    standards_research: bool = False,
    procedure_planning: bool = False,
    workbook_inspection: bool = False,
    standards_retriever=None,
    standards_retriever_factory=None,
    workbook_source_provider=None,
    checkpointer=None,
    runtime_root: Path | str | None = None,
    eprint=None,
) -> dict:
    """Run one resumable question/answer turn over an exact committed bundle."""
    selected_thread = _thread_id(thread_id)
    if not isinstance(question, str) or not question.strip():
        raise AuditConversationError("audit-chat question이 비어 있습니다.")
    if sheet is not None and aggregate_id is not None:
        raise AuditConversationError("sheet와 aggregate_id는 함께 사용할 수 없습니다.")
    if not isinstance(standards_research, bool):
        raise AuditConversationError("standards_research는 boolean이어야 합니다.")
    if not isinstance(procedure_planning, bool):
        raise AuditConversationError("procedure_planning은 boolean이어야 합니다.")
    if not isinstance(workbook_inspection, bool):
        raise AuditConversationError("workbook_inspection은 boolean이어야 합니다.")
    try:
        if aggregate_id is not None:
            agent = _prepare_audit_aggregate_agent_runtime(
                pkg,
                aggregate_id=aggregate_id,
                model=model,
                question=question,
                limit=limit,
                max_steps=max_steps,
            )
            blocked = None
        else:
            agent, blocked = _prepare_audit_agent_runtime(
                pkg,
                model=model,
                question=question,
                sheet=sheet,
                limit=limit,
                max_steps=max_steps,
                prompt_name=CONVERSATION_PROMPT,
            )
    except (AuditAgentError, AuditAggregateAgentError, AuditConsumeError) as e:
        raise AuditConversationError(str(e)) from e
    path = Path(pkg)
    root = Path(runtime_root) if runtime_root is not None else path / RUNTIME_DIR
    try:
        store = ConversationArtifactStore(root)
    except ConversationArtifactStoreError as e:
        raise AuditConversationError(f"conversation private store 준비 실패: {e}") from e
    invocation_id = uuid.uuid4().hex
    normalized_question = (
        agent.question if agent is not None else blocked.get("question") if blocked else None
    )
    if not isinstance(normalized_question, str):
        raise AuditConversationError("검증된 conversation question이 없습니다.")
    try:
        question_ref = store.write(
            selected_thread,
            kind="question",
            schema_version=QUESTION_SCHEMA,
            payload={
                "invocation_id": invocation_id,
                "question": normalized_question,
            },
        )
    except ConversationArtifactStoreError as e:
        raise AuditConversationError(f"conversation question 저장 실패: {e}") from e
    context = AuditConversationRuntime(
        thread_id=selected_thread,
        package_path=path,
        sheet=sheet,
        store=store,
        agent=agent,
        blocked_response=blocked,
        aggregate_id=aggregate_id,
        client=client,
        client_factory=client_factory,
        standards_research_enabled=standards_research,
        procedure_planning_enabled=procedure_planning,
        workbook_inspection_enabled=workbook_inspection,
        standards_retriever=standards_retriever,
        standards_retriever_factory=standards_retriever_factory,
        workbook_source_provider=workbook_source_provider,
        eprint=eprint,
    )
    thread_digest = _thread_digest(selected_thread)
    try:
        with cache.package_lock(store.root / "threads" / thread_digest):
            if checkpointer is not None:
                graph_result = _invoke_graph(
                    checkpointer=checkpointer,
                    context=context,
                    question_ref=question_ref,
                    invocation_id=invocation_id,
                )
            else:
                with _sqlite_saver(store.root) as saver:
                    graph_result = _invoke_graph(
                        checkpointer=saver,
                        context=context,
                        question_ref=question_ref,
                        invocation_id=invocation_id,
                    )
            return _turn_result(
                context,
                graph_result,
                invocation_id=invocation_id,
                question_ref=question_ref,
            )
    finally:
        context.close_research()


def render_audit_conversation_markdown(result: dict) -> str:
    """Render conversation metadata followed by the existing grounded answer view."""
    thread = result.get("thread_id")
    turn_index = result.get("turn_index")
    resumed = "재개" if result.get("resumed") else "신규"
    usage = result.get("usage", {})
    response = result.get("response")
    if not isinstance(response, dict):
        raise AuditConversationError("render할 conversation response가 없습니다.")
    header = (
        f"> 대화 thread: `{thread}` · turn {turn_index} · {resumed}\n"
        f"> LLM 요청 {usage.get('request_count', 0)}회 · "
        f"input {usage.get('input_tokens', 0)} / output {usage.get('output_tokens', 0)} tokens\n\n"
    )
    research = response.get("standards_research")
    research_markdown = ""
    if isinstance(research, dict):
        citations = research.get("citations", [])
        research_markdown = (
            "## 동적 기준서 조사 (미검토)\n\n"
            "> 이 내용은 현재 turn에만 유효한 ephemeral 보조 근거이며 prepared bundle의 "
            "일부가 아닙니다. 시행일 적합성도 검증되지 않았습니다.\n\n"
        )
        if citations:
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                title = html.escape(str(citation.get("standard_title", "기준서")))
                cid = html.escape(str(citation.get("cid", "")))
                collection = html.escape(str(citation.get("collection", "")))
                text = html.escape(str(citation.get("text", "")), quote=False)
                text = re.sub(r"([\\`*\[\]])", r"\\\1", " ".join(text.split()))
                research_markdown += (
                    f"- **{title}** · `{cid}` · collection `{collection}`\n"
                    f"  - {text}\n"
                )
            research_markdown += "\n"
        else:
            research_markdown += "관련성이 충분한 검증 문단을 선택하지 못했습니다.\n\n"
    plan = response.get("procedure_plan")
    plan_markdown = ""
    if isinstance(plan, dict):
        plan_markdown = (
            "## 추천 감사 test 후보 (미검토 제안)\n\n"
            "> 이 후보들은 `proposed / unreviewed / not_evidenced` 상태이며, 조서에 "
            "수행됐다는 사실이나 필수 절차를 의미하지 않습니다. 표본 수·금액 기준·선정 "
            "간격은 감사인이 별도로 설계해야 합니다.\n\n"
        )

        def clean(value: object) -> str:
            escaped = html.escape(" ".join(str(value or "").split()), quote=False)
            return re.sub(r"([\\`*\[\]])", r"\\\1", escaped)

        candidates = plan.get("candidates", [])
        if plan.get("status") == "no_plan" or not candidates:
            plan_markdown += "현재 근거만으로 안전한 후보 묶음을 만들지 못했습니다.\n\n"
        else:
            request = plan.get("request", {})
            catalog = plan.get("basis_catalog", {})
            workbook_catalog = {
                item.get("basis_ref"): item
                for item in catalog.get("workbook", [])
                if isinstance(item, dict) and isinstance(item.get("basis_ref"), str)
            } if isinstance(catalog, dict) else {}
            standards_catalog = {
                item.get("basis_ref"): item
                for item in catalog.get("standards", [])
                if isinstance(item, dict) and isinstance(item.get("basis_ref"), str)
            } if isinstance(catalog, dict) else {}
            if isinstance(request, dict):
                plan_markdown += f"- 계획 목적: {clean(request.get('objective'))}\n"
                target = request.get("target", {})
                if isinstance(target, dict):
                    target_labels = (
                        ("계정", "account_ref"),
                        ("위험", "risk_ref"),
                        ("주장", "assertion_ref"),
                    )
                    rendered_targets = []
                    for label, key in target_labels:
                        record = workbook_catalog.get(target.get(key))
                        if isinstance(record, dict):
                            rendered_targets.append(
                                f"{label}: {clean(record.get('text'))}"
                            )
                    if rendered_targets:
                        plan_markdown += "- 대상: " + "; ".join(rendered_targets) + "\n"
                plan_markdown += "\n"
            role_labels = {
                "primary": "Primary",
                "alternative": "Alternative",
                "complementary": "Complementary",
            }

            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                role = role_labels.get(
                    str(candidate.get("portfolio_role")),
                    str(candidate.get("portfolio_role", "")),
                )
                plan_markdown += (
                    f"### {candidate.get('rank')}. {clean(candidate.get('title'))} "
                    f"— {clean(role)}\n\n"
                    f"- 목적: {clean(candidate.get('objective'))}\n"
                    f"- 접근법: {clean(candidate.get('approach'))}\n"
                )
                methods = candidate.get("evidence_methods", [])
                if isinstance(methods, list) and methods:
                    plan_markdown += "- 증거 획득 방법: " + "; ".join(
                        clean(item) for item in methods
                    ) + "\n"
                steps = candidate.get("steps", [])
                if isinstance(steps, list) and steps:
                    plan_markdown += "- 수행 단계:\n" + "".join(
                        f"  {index}. {clean(item)}\n"
                        for index, item in enumerate(steps, start=1)
                    )
                applicability = candidate.get("applicability", {})
                if isinstance(applicability, dict):
                    conditions = applicability.get("conditions_for_use", [])
                    disqualifiers = applicability.get("disqualifiers", [])
                    assessment = clean(applicability.get("assessment"))
                    plan_markdown += f"- 적용 판단: {assessment}\n"
                    if conditions:
                        plan_markdown += "- 적용 조건: " + "; ".join(
                            clean(item) for item in conditions
                        ) + "\n"
                    if disqualifiers:
                        plan_markdown += "- 적용 배제/제약 조건: " + "; ".join(
                            clean(item) for item in disqualifiers
                        ) + "\n"
                for label, key in (
                    ("필요 증거", "evidence_to_obtain"),
                    ("장점", "strengths"),
                    ("한계", "limitations"),
                    ("전제조건", "prerequisites"),
                    ("후속 확인사항", "open_questions"),
                ):
                    values = candidate.get(key, [])
                    if isinstance(values, list) and values:
                        plan_markdown += f"- {label}: " + "; ".join(
                            clean(item) for item in values
                        ) + "\n"
                standard_records = [
                    standards_catalog.get(ref)
                    for ref in candidate.get("standards_basis_refs", [])
                    if isinstance(ref, str)
                ]
                rendered_standards = [
                    f"{clean(item.get('cid'))} ({clean(item.get('standard_title'))})"
                    for item in standard_records
                    if isinstance(item, dict)
                ]
                if rendered_standards:
                    plan_markdown += "- 기준서 근거: " + "; ".join(
                        rendered_standards
                    ) + "\n"
                plan_markdown += "\n"
            combinations = plan.get("recommended_combinations", [])
            if isinstance(combinations, list) and combinations:
                plan_markdown += "### 권장 테스트 묶음\n\n"
                for combination in combinations:
                    if not isinstance(combination, dict):
                        continue
                    keys = ", ".join(
                        clean(item) for item in combination.get("candidate_keys", [])
                    )
                    plan_markdown += (
                        f"- `{clean(combination.get('combination_key'))}` ({keys}): "
                        f"{clean(combination.get('rationale'))}\n"
                    )
                    tradeoffs = combination.get("tradeoffs", [])
                    if isinstance(tradeoffs, list) and tradeoffs:
                        plan_markdown += "  - 조합 trade-off: " + "; ".join(
                            clean(item) for item in tradeoffs
                        ) + "\n"
                plan_markdown += "\n"
        for label, key in (("가정", "assumptions"), ("확인할 사항", "open_questions")):
            values = plan.get(key, [])
            if isinstance(values, list) and values:
                plan_markdown += f"### {label}\n\n"
                plan_markdown += "".join(f"- {clean(item)}\n" for item in values)
                plan_markdown += "\n"
    inspection = response.get("workbook_inspection")
    inspection_markdown = ""
    if isinstance(inspection, dict):
        inspection_markdown = (
            "## Workbook 추가 검사 (미검토 계산)\n\n"
            "> 이 결과는 `computed / unreviewed / not_documented` 상태인 현재 turn의 "
            "보조 관찰입니다. 조서에 기록된 사실이나 수행된 감사절차가 아닙니다.\n\n"
        )

        def inspection_text(value: object) -> str:
            escaped = html.escape(" ".join(str(value or "").split()), quote=False)
            return re.sub(r"([\\`*\[\]])", r"\\\1", escaped)

        for item in inspection.get("inspections", []):
            if not isinstance(item, dict):
                continue
            request = item.get("input", {})
            source = item.get("source", {})
            computed = item.get("result", {})
            if not isinstance(request, dict) or not isinstance(computed, dict):
                continue
            inspection_markdown += (
                f"### {inspection_text(item.get('operation'))} · "
                f"{inspection_text(request.get('sheet'))}!"
                f"{inspection_text(request.get('range'))}\n\n"
                f"- source: `{inspection_text(source.get('kind') if isinstance(source, dict) else None)}`\n"
                f"- ref: `{inspection_text(item.get('inspection_ref'))}`\n"
            )
            metric_keys = (
                "range_area", "returned", "total_records", "total_dependencies",
                "row_count", "data_row_count", "column_count", "total_groups",
                "total_duplicate_rows", "numeric_count", "total_outliers", "truncated",
            )
            metrics = [
                f"{key}={inspection_text(computed.get(key))}"
                for key in metric_keys if key in computed
            ]
            if metrics:
                inspection_markdown += "- 결과 요약: " + "; ".join(metrics) + "\n"
            for key in ("cells", "dependencies", "columns", "groups", "outliers"):
                values = computed.get(key)
                if not isinstance(values, list) or not values:
                    continue
                inspection_markdown += f"- {key} (최대 10건 표시):\n"
                for value in values[:10]:
                    compact = json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    inspection_markdown += f"  - {inspection_text(compact)}\n"
            inspection_markdown += "\n"
    if response.get("schema_version") == "audit_main_agent_response.v1":
        return (
            header
            + research_markdown
            + plan_markdown
            + inspection_markdown
            + render_audit_aggregate_agent_markdown(response)
        )
    return (
        header
        + research_markdown
        + plan_markdown
        + inspection_markdown
        + render_audit_agent_markdown(_validated_response(response))
    )


__all__ = [
    "AuditConversationBundleChangedError",
    "AuditConversationError",
    "AuditConversationRuntime",
    "CONVERSATION_VERSION",
    "build_audit_conversation_graph",
    "render_audit_conversation_markdown",
    "run_audit_conversation_turn",
]
