"""Bounded workbook-inspection integration with the persistent audit graph."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

import excel_to_skill.audit.conversation as conversation_module
from excel_to_skill.audit.conversation import (
    render_audit_conversation_markdown,
    run_audit_conversation_turn,
)

from test_audit_consume_gate import _write_committed_bundle
from test_audit_conversation_aggregate import _prepared_aggregate


def _inspection_tool(
    *,
    sheet: str,
    cell_range: str,
    scope_id: str | None = None,
    source: str = "ledger",
) -> dict:
    tool = {
        "name": "workbook_inspection",
        "query": None,
        "kind": None,
        "limit": 1,
        "operation": "inspect_range",
        "sheet": sheet,
        "range": cell_range,
        "parameters": {"source": source, "limit": 20},
    }
    if scope_id is None:
        tool["item_id"] = None
    else:
        tool.update({"item_ref": None, "scope_id": scope_id})
    return {"action": "tool", "tool": tool, "final": None}


def _inspection_final(refs: list[str]) -> dict:
    return {
        "action": "final",
        "tool": None,
        "final": {
            "abstained": False,
            "abstention_code": None,
            "selections": [],
            "inspection_refs": refs,
        },
    }


class WorkbookInspectionClient:
    usage_events: list[dict] = []

    def __init__(self, sheet: str) -> None:
        self.sheet = sheet
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        payload = json.loads(kwargs["user"])
        self.calls.append(payload)
        capability = payload["capabilities"]["workbook_inspection"]
        assert capability["enabled"] is True
        assert capability["source_preference"] == "package_ledger_first"
        assert capability["raw_source_available"] is False
        if len(self.calls) == 1:
            return _inspection_tool(sheet=self.sheet, cell_range="A1:B2")
        result = next(
            item["result"] for item in payload["observations"]
            if item.get("tool") == "workbook_inspection"
        )
        return _inspection_final([result["inspection_ref"]])


def _artifact_documents(root: Path, kind: str) -> list[dict]:
    result: list[dict] = []
    for path in root.glob("threads/*/objects/*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("kind") == kind:
            result.append(document)
    return result


def test_workbook_inspection_uses_ledger_and_stays_turn_scoped_private(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    sheet = meta["sheets"][0]["name"]
    runtime_root = tmp_path / "runtime"
    client = WorkbookInspectionClient(sheet)

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="A1:B2 범위를 다시 확인해줘",
        thread_id="inspection-thread",
        client=client,
        runtime_root=runtime_root,
        workbook_inspection=True,
    )

    response = result["response"]
    assert response["answer"]["abstained"] is True
    supplement = response["workbook_inspection"]
    assert supplement["status"] == "computed"
    assert supplement["review_status"] == "unreviewed"
    assert supplement["documentation_status"] == "not_documented"
    assert supplement["turn_scoped"] is True
    assert supplement["outside_prepared_bundle"] is True
    inspected = supplement["inspections"][0]
    assert inspected["source"]["kind"] == "package_ledger"
    assert inspected["input"] == {
        "operation": "inspect_range",
        "sheet": sheet,
        "range": "A1:B2",
        "parameters": {"source": "ledger", "limit": 20},
    }
    assert inspected["inspection_ref"] == supplement["selected_refs"][0]
    rendered = render_audit_conversation_markdown(result)
    assert "Workbook 추가 검사 (미검토 계산)" in rendered
    assert "computed / unreviewed / not_documented" in rendered

    observations = _artifact_documents(runtime_root, "observations")
    assert any(
        item.get("tool") == "workbook_inspection"
        and item.get("result", {}).get("inspection_ref") == inspected["inspection_ref"]
        for document in observations
        for item in document["payload"]["value"]
    )
    database = runtime_root / "checkpoints.sqlite3"
    with sqlite3.connect(database) as connection:
        checkpoint_blobs = b"".join(
            bytes(row[0]) if isinstance(row[0], (bytes, bytearray)) else str(row[0]).encode()
            for row in connection.execute("SELECT checkpoint FROM checkpoints")
        )
    assert inspected["inspection_ref"].encode() not in checkpoint_blobs
    assert json.dumps(inspected["result"], ensure_ascii=False).encode() not in checkpoint_blobs


def test_inspection_result_is_not_reexposed_as_next_turn_authority(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    sheet = meta["sheets"][0]["name"]
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    first_client = WorkbookInspectionClient(sheet)
    first = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="범위를 확인해줘",
        thread_id="inspection-focus-thread",
        client=first_client,
        checkpointer=saver,
        runtime_root=root,
        workbook_inspection=True,
    )
    inspection_ref = first["response"]["workbook_inspection"]["selected_refs"][0]

    class FollowupClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.payload: dict | None = None

        def __call__(self, **kwargs):
            self.payload = json.loads(kwargs["user"])
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "insufficient_evidence",
                    "selections": [],
                },
            }

    followup = FollowupClient()
    run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="그 계산을 근거로 결론 내릴 수 있어?",
        thread_id="inspection-focus-thread",
        client=followup,
        checkpointer=saver,
        runtime_root=root,
        workbook_inspection=False,
    )
    assert followup.payload is not None
    assert inspection_ref not in json.dumps(
        followup.payload["observations"], ensure_ascii=False
    )
    assert all(
        item.get("tool") != "workbook_inspection"
        for item in followup.payload["observations"]
    )


def test_aggregate_inspection_rejects_cross_sheet_request_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("cross-sheet request must fail before core execution")

    monkeypatch.setattr(conversation_module, "run_workbook_inspection", forbidden)

    class CrossSheetClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.main_calls = 0
            self.error: dict | None = None

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.main_calls += 1
            if self.main_calls == 1:
                accounts = payload["observations"][0]["result"]["accounts"]
                main = next(item for item in accounts if item["scope"]["sheet"] == "Main")
                return _inspection_tool(
                    sheet="Other",
                    cell_range="A1:B2",
                    scope_id=main["scope"]["id"],
                )
            observation = next(
                item for item in payload["observations"]
                if item.get("tool") == "workbook_inspection"
            )
            self.error = observation["result"]
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "retrieval_incomplete",
                    "selections": [],
                },
            }

    client = CrossSheetClient()
    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="Main과 Other를 섞어 확인해줘",
        thread_id="aggregate-inspection-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        workbook_inspection=True,
    )

    assert calls == 0
    assert client.error == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "workbook 추가 검사를 완료하지 못했습니다.",
        }
    }
    assert "workbook_inspection" not in result["response"]


def test_aggregate_inspection_binds_result_to_one_exact_source_scope(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)

    class ExactScopeClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls = 0
            self.scope_id: str | None = None

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.calls += 1
            if self.calls == 1:
                accounts = payload["observations"][0]["result"]["accounts"]
                main = next(item for item in accounts if item["scope"]["sheet"] == "Main")
                self.scope_id = main["scope"]["id"]
                return _inspection_tool(
                    sheet="Main",
                    cell_range="A1:B2",
                    scope_id=self.scope_id,
                )
            inspected = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "workbook_inspection"
            )
            return _inspection_final([inspected["inspection_ref"]])

    client = ExactScopeClient()
    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="Main 시트 A1:B2를 확인해줘",
        thread_id="aggregate-exact-inspection-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        workbook_inspection=True,
    )

    inspected = result["response"]["workbook_inspection"]["inspections"][0]
    assert inspected["scope"] == {
        "kind": "sheet",
        "sheet": "Main",
        "id": client.scope_id,
    }
    assert inspected["scope_id"] == client.scope_id
    assert inspected["input"]["sheet"] == "Main"


def test_aggregate_turn_cannot_mix_two_exact_source_sheets(tmp_path: Path) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)

    class MixedScopeClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls = 0
            self.scopes: dict[str, str] = {}
            self.results: list[dict] = []

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.calls += 1
            accounts = payload["observations"][0]["result"]["accounts"]
            self.scopes = {
                item["scope"]["sheet"]: item["scope"]["id"] for item in accounts
            }
            self.results = [
                item["result"] for item in payload["observations"]
                if item.get("tool") == "workbook_inspection"
            ]
            if self.calls == 1:
                return _inspection_tool(
                    sheet="Main", cell_range="A1:B2", scope_id=self.scopes["Main"]
                )
            if self.calls == 2:
                return _inspection_tool(
                    sheet="Other", cell_range="A1:B2", scope_id=self.scopes["Other"]
                )
            first_ref = next(
                item["inspection_ref"] for item in self.results
                if item.get("schema_version") == "audit_workbook_inspection.v1"
            )
            return _inspection_final([first_ref])

    client = MixedScopeClient()
    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="main-model",
        question="Main과 Other를 각각 검사해줘",
        thread_id="aggregate-mixed-inspection-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        workbook_inspection=True,
    )

    assert client.results[-1] == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "workbook 추가 검사를 완료하지 못했습니다.",
        }
    }
    inspections = result["response"]["workbook_inspection"]["inspections"]
    assert len(inspections) == 1
    assert inspections[0]["scope"]["sheet"] == "Main"


def test_raw_inspection_requires_same_range_ledger_observation_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    sheet = meta["sheets"][0]["name"]
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("raw source must not run before ledger-first inspection")

    monkeypatch.setattr(conversation_module, "run_workbook_inspection", forbidden)

    class RawFirstClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls = 0
            self.error: dict | None = None

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.calls += 1
            if self.calls == 1:
                return _inspection_tool(
                    sheet=sheet,
                    cell_range="A1:B2",
                    source="raw",
                )
            self.error = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "workbook_inspection"
            )
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "retrieval_incomplete",
                    "selections": [],
                },
            }

    client = RawFirstClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="원본 범위를 바로 읽어줘",
        thread_id="raw-first-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        workbook_inspection=True,
    )

    assert calls == 0
    assert client.error == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "workbook 추가 검사를 완료하지 못했습니다.",
        }
    }
    assert "workbook_inspection" not in result["response"]


def test_inspection_request_count_is_bounded_to_two(tmp_path: Path) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    sheet = meta["sheets"][0]["name"]

    class BoundedClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.main_calls = 0
            self.results: list[dict] = []

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            self.main_calls += 1
            self.results = [
                item["result"] for item in payload["observations"]
                if item.get("tool") == "workbook_inspection"
            ]
            if self.main_calls <= 3:
                return _inspection_tool(
                    sheet=sheet,
                    cell_range=("A1", "B1", "C1")[self.main_calls - 1],
                )
            refs = [
                item["inspection_ref"] for item in self.results
                if item.get("schema_version") == "audit_workbook_inspection.v1"
            ]
            return _inspection_final(refs)

    client = BoundedClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="세 범위를 확인해줘",
        thread_id="bounded-inspection-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        workbook_inspection=True,
    )

    assert len(result["response"]["workbook_inspection"]["inspections"]) == 2
    assert client.results[-1] == {
        "error": {
            "code": "INSPECTION_LIMIT_EXCEEDED",
            "message": "workbook 추가 검사를 완료하지 못했습니다.",
        }
    }
