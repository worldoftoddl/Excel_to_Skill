"""Aggregate-bound procedure-planning scope and authority tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

from excel_to_skill.audit.aggregate import aggregate_audit_package
from excel_to_skill.audit.conversation import run_audit_conversation_turn
from excel_to_skill.audit.scope import AuditScope, load_scope_bundle

from test_audit_aggregate import SelectionClient, _refresh_links, _write_scope
from test_audit_conversation_aggregate import _prepared_aggregate
from test_audit_conversation_research import ResearchRetriever
from test_audit_procedure_planning import _worker


def _planning_fact(
    fact_id: str,
    fact_type: str,
    description: str,
    *,
    status: str = "documented",
    normalized_code: str | None = None,
) -> dict:
    return {
        "id": fact_id,
        "type": fact_type,
        "description": description,
        "status": status,
        "normalized_code": normalized_code,
        "value": None,
        "unit": None,
        "severity": None,
        "confidence": 0.9,
        "source_ids": ["source:1"],
    }


def _prepared_planning_aggregate(tmp_path: Path):
    """Extend both committed source scopes, then publish a fresh exact aggregate."""
    pkg, _ = _prepared_aggregate(tmp_path)
    for sheet in ("Main", "Other"):
        scope = AuditScope.for_sheet(sheet)
        loaded = load_scope_bundle(pkg, scope)
        assert loaded is not None
        _, facts, standards, brief, _ = loaded
        facts = copy.deepcopy(facts)
        standards = copy.deepcopy(standards)
        brief = copy.deepcopy(brief)

        risk = next(item for item in facts["facts"] if item["id"] == "fact:risk")
        risk["description"] = f"계획대상 {sheet} 가공 매출 위험"
        facts["facts"].extend([
            _planning_fact("fact:account", "account", f"계획대상 {sheet} 매출채권"),
            _planning_fact(
                "fact:assertion",
                "assertion",
                f"계획대상 {sheet} 실재성",
                normalized_code="existence",
            ),
            _planning_fact(
                "fact:procedure",
                "procedure",
                f"계획대상 {sheet} 기존 명세서 대사",
                status="performed",
            ),
        ])
        brief["workpaper"]["fact_ids"].extend([
            "fact:account", "fact:assertion", "fact:procedure",
        ])
        _refresh_links(facts, standards, brief)
        _write_scope(pkg, sheet, facts, standards, brief)

    aggregate = aggregate_audit_package(
        pkg,
        all_committed_sheets=True,
        model="aggregate-model",
        client=SelectionClient(),
        generated_at="2026-07-13T13:00:00Z",
    )
    return pkg, aggregate


def _scope_id(payload: dict, sheet: str) -> str:
    accounts = payload["observations"][0]["result"]["accounts"]
    return next(
        item["scope"]["id"] for item in accounts
        if item["scope"]["sheet"] == sheet
    )


def _tool(
    name: str,
    *,
    query: str | None,
    kind: str | None,
    item_ref: str | None,
    scope_id: str | None,
    limit: int,
    **extra,
) -> dict:
    return {
        "action": "tool",
        "tool": {
            "name": name,
            "query": query,
            "kind": kind,
            "item_ref": item_ref,
            "scope_id": scope_id,
            "limit": limit,
            **extra,
        },
        "final": None,
    }


def _final(*, plan_ref: str | None = None) -> dict:
    final = {
        "abstained": plan_ref is None,
        "abstention_code": "retrieval_incomplete" if plan_ref is None else None,
        "selections": [],
    }
    if plan_ref is not None:
        final["plan_refs"] = [plan_ref]
    return {"action": "final", "tool": None, "final": final}


def _planning_source_refs(payload: dict, *, sheet: str) -> list[str]:
    search = next(
        item["result"] for item in payload["observations"]
        if item.get("tool") == "source_search"
        and item.get("result", {}).get("scope", {}).get("sheet") == sheet
    )
    matches = search["matches"]
    refs = [
        item["source_ref"] for item in matches
        if item["kind"] == "fact"
        and item["item"]["type"] in {"account", "risk", "assertion", "procedure"}
    ]
    assert {item["item"]["type"] for item in matches} >= {
        "account", "risk", "assertion", "procedure",
    }
    return refs


class SameScopePlanningClient:
    usage_events: list[dict] = []

    def __init__(self) -> None:
        self.main_calls = 0
        self.research_child_calls = 0
        self.planning_child_calls = 0
        self.scope_id: str | None = None
        self.plan_request: dict | None = None

    def __call__(self, **kwargs):
        payload = json.loads(kwargs["user"])
        if "candidates" in payload:
            self.research_child_calls += 1
            return {
                "abstained": False,
                "selected_candidate_refs": [payload["candidates"][0]["candidate_ref"]],
            }
        if "workbook_basis" in payload:
            self.planning_child_calls += 1
            self.plan_request = payload
            return _worker(payload)

        self.main_calls += 1
        if self.main_calls == 1:
            self.scope_id = _scope_id(payload, "Main")
            return _tool(
                "standards_research",
                query="평가된 위험에 대응하는 감사절차",
                kind="audit_standard",
                item_ref=None,
                scope_id=self.scope_id,
                limit=3,
            )
        if self.main_calls == 2:
            return _tool(
                "source_search",
                query="계획대상 Main",
                kind="fact",
                item_ref=None,
                scope_id=self.scope_id,
                limit=20,
            )
        if self.main_calls == 3:
            research = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            return _tool(
                "procedure_planning",
                query="이 위험과 주장에 대응할 여러 감사 test를 추천해줘.",
                kind=None,
                item_ref=None,
                scope_id=self.scope_id,
                limit=3,
                source_refs=_planning_source_refs(payload, sheet="Main"),
                research_refs=[research["records"][0]["research_ref"]],
            )
        plan = next(
            item["result"] for item in payload["observations"]
            if item.get("tool") == "procedure_planning"
        )
        return _final(plan_ref=plan["plan_ref"])


def test_aggregate_planning_uses_one_exact_source_scope_and_returns_plan(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_planning_aggregate(tmp_path)
    client = SameScopePlanningClient()
    retriever = ResearchRetriever("2026.1")

    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="stub-model",
        question="Main 매출 위험에 어떤 test를 할 수 있어?",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        procedure_planning=True,
        standards_retriever=retriever,
    )

    plan = result["response"]["procedure_plan"]
    assert plan["status"] == "completed"
    assert plan["scope"] == {
        "kind": "sheet", "sheet": "Main", "id": client.scope_id,
    }
    assert len(plan["candidates"]) == 3
    assert [item["portfolio_role"] for item in plan["candidates"]] == [
        "primary", "alternative", "complementary",
    ]
    assert len(plan["recommended_combinations"]) == 2
    assert result["response"]["answer"]["abstained"] is True
    assert client.research_child_calls == 1
    assert client.planning_child_calls == 1
    assert client.plan_request is not None
    assert {
        item["scope"]["sheet"] for item in client.plan_request["workbook_basis"]
    } == {"Main"}
    assert {
        item["scope"]["sheet"] for item in client.plan_request["standards_basis"]
    } == {"Main"}
    assert retriever.closed is True


class InvalidAggregatePlanningClient:
    usage_events: list[dict] = []

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.main_calls = 0
        self.planning_child_calls = 0
        self.plan_observation: dict | None = None
        self.main_scope: str | None = None
        self.other_scope: str | None = None

    def __call__(self, **kwargs):
        payload = json.loads(kwargs["user"])
        if "workbook_basis" in payload:
            self.planning_child_calls += 1
            raise AssertionError("invalid aggregate scope must fail before child planning")
        self.main_calls += 1
        if self.main_calls == 1:
            self.main_scope = _scope_id(payload, "Main")
            self.other_scope = _scope_id(payload, "Other")
            return _tool(
                "source_search", query="계획대상 Main", kind="fact",
                item_ref=None, scope_id=self.main_scope, limit=20,
            )
        if self.main_calls == 2:
            return _tool(
                "source_search", query="계획대상 Other", kind="fact",
                item_ref=None, scope_id=self.other_scope, limit=20,
            )
        if self.main_calls == 3:
            main_refs = _planning_source_refs(payload, sheet="Main")
            if self.mode == "mixed_refs":
                other_refs = _planning_source_refs(payload, sheet="Other")
                refs = [*main_refs, other_refs[0]]
                scope_id = self.main_scope
            else:
                refs = main_refs
                scope_id = self.other_scope
            return _tool(
                "procedure_planning",
                query="범위가 잘못된 test 추천 요청",
                kind=None,
                item_ref=None,
                scope_id=scope_id,
                limit=3,
                source_refs=refs,
                research_refs=[],
            )
        self.plan_observation = next(
            item for item in payload["observations"]
            if item.get("tool") == "procedure_planning"
        )
        return _final()


@pytest.mark.parametrize("mode", ["mixed_refs", "mismatched_scope"])
def test_aggregate_planning_rejects_cross_sheet_or_mismatched_scope_before_child(
    tmp_path: Path,
    mode: str,
) -> None:
    pkg, aggregate = _prepared_planning_aggregate(tmp_path)
    client = InvalidAggregatePlanningClient(mode)

    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="stub-model",
        question="범위를 섞어서 test를 추천해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / f"runtime-{mode}",
        procedure_planning=True,
    )

    assert client.planning_child_calls == 0
    assert client.plan_observation is not None
    assert client.plan_observation["result"] == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "감사 test 후보 계획을 완료하지 못했습니다.",
        }
    }
    assert "procedure_plan" not in result["response"]


class CrossScopeResearchPlanningClient:
    usage_events: list[dict] = []

    def __init__(self) -> None:
        self.main_calls = 0
        self.research_child_calls = 0
        self.planning_child_calls = 0
        self.main_scope: str | None = None
        self.other_scope: str | None = None
        self.plan_observation: dict | None = None

    def __call__(self, **kwargs):
        payload = json.loads(kwargs["user"])
        if "candidates" in payload:
            self.research_child_calls += 1
            return {
                "abstained": False,
                "selected_candidate_refs": [payload["candidates"][0]["candidate_ref"]],
            }
        if "workbook_basis" in payload:
            self.planning_child_calls += 1
            raise AssertionError("cross-scope research ref must fail before child planning")
        self.main_calls += 1
        if self.main_calls == 1:
            self.main_scope = _scope_id(payload, "Main")
            self.other_scope = _scope_id(payload, "Other")
            return _tool(
                "standards_research",
                query="평가된 위험에 대응하는 감사절차",
                kind="audit_standard",
                item_ref=None,
                scope_id=self.main_scope,
                limit=3,
            )
        if self.main_calls == 2:
            return _tool(
                "source_search", query="계획대상 Other", kind="fact",
                item_ref=None, scope_id=self.other_scope, limit=20,
            )
        if self.main_calls == 3:
            research = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            return _tool(
                "procedure_planning",
                query="Other 위험에 Main 기준 조사 결과를 사용해줘",
                kind=None,
                item_ref=None,
                scope_id=self.other_scope,
                limit=3,
                source_refs=_planning_source_refs(payload, sheet="Other"),
                research_refs=[research["records"][0]["research_ref"]],
            )
        self.plan_observation = next(
            item for item in payload["observations"]
            if item.get("tool") == "procedure_planning"
        )
        return _final()


def test_aggregate_planning_rejects_current_turn_research_from_another_scope(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_planning_aggregate(tmp_path)
    client = CrossScopeResearchPlanningClient()
    retriever = ResearchRetriever("2026.1")

    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="stub-model",
        question="다른 sheet 기준 조사 결과로 test를 추천해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime-cross-research",
        standards_research=True,
        procedure_planning=True,
        standards_retriever=retriever,
    )

    assert client.research_child_calls == 1
    assert client.planning_child_calls == 0
    assert client.plan_observation is not None
    assert client.plan_observation["result"] == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "감사 test 후보 계획을 완료하지 못했습니다.",
        }
    }
    assert "procedure_plan" not in result["response"]
    assert retriever.closed is True
