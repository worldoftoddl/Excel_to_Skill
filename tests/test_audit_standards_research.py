"""Isolated dynamic standards-research graph tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from excel_to_skill.audit.model import StandardsDomain
from excel_to_skill.audit.standards import StandardHit, StandardsRetrievalFatalError
from excel_to_skill.audit.standards_research import (
    StandardsResearchError,
    StandardsResearchRuntime,
    research_records,
    research_summary,
    run_standards_research,
    validate_research_summary,
)


COLLECTION = "standards_20250829_bgem3"


def _paragraph(cid: str, text: str) -> dict:
    prefix, standard_no, para_no = cid.split("::", 2)
    return {
        "cid": cid,
        "source_type": "감사기준" if prefix == "KSA" else "회계기준",
        "standard_no": standard_no,
        "standard_title": "테스트 기준서",
        "para_no": para_no,
        "para_type": "요구사항",
        "section_path": "요구사항 > 테스트",
        "seq": 10,
        "text": text,
        "is_context": False,
    }


def _hit(cid: str, text: str) -> StandardHit:
    paragraph = _paragraph(cid, text)
    return StandardHit(
        domain=(
            StandardsDomain.AUDIT if cid.startswith("KSA::")
            else StandardsDomain.ACCOUNTING
        ),
        framework="KSA" if cid.startswith("KSA::") else "K-IFRS",
        document_id=cid,
        paragraph=paragraph["para_no"],
        title=paragraph["standard_title"],
        snippet=text,
        score=0.8,
        corpus_id="auditpaper-standards",
        corpus_version=COLLECTION,
        retriever_version="stub-1",
        metadata={
            "source_cid": cid,
            "source_type": paragraph["source_type"],
            "standard_no": paragraph["standard_no"],
            "para_type": paragraph["para_type"],
            "section_path": paragraph["section_path"],
            "seq": paragraph["seq"],
            "verified_by": "standards_get_paragraph",
            "paragraph_text_sha256": hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
        },
    )


class StubRetriever:
    def __init__(self, hits: list[StandardHit], *, collection: str = COLLECTION) -> None:
        self.hits = hits
        self._collection = collection
        self.search_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.paragraphs = {
            hit.document_id: _paragraph(hit.document_id, hit.snippet) for hit in hits
        }

    @property
    def collection(self) -> str:
        return self._collection

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return list(self.hits)

    def get_verified_paragraph(self, cid: str):
        self.get_calls.append(cid)
        return dict(self.paragraphs[cid])


class SelectingClient:
    def __init__(self, *, count: int = 1, forged_ref: str | None = None) -> None:
        self.count = count
        self.forged_ref = forged_ref
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["user"])
        refs = [item["candidate_ref"] for item in payload["candidates"]]
        selected = [self.forged_ref] if self.forged_ref else refs[: self.count]
        return {
            "abstained": not bool(selected),
            "selected_candidate_refs": selected,
        }


def _runtime(retriever, client) -> StandardsResearchRuntime:
    return StandardsResearchRuntime(
        retriever=retriever,
        client=client,
        model="stub-model",
        expected_collection=COLLECTION,
        invocation_id="invocation-1",
        bundle_sha256="a" * 64,
        scope={"kind": "workbook"},
    )


def _request() -> dict:
    return {
        "query": "외부조회 절차와 관련된 감사기준 요구사항",
        "domain": "audit",
        "framework": "KSA",
        "scope_id": None,
        "limit": 5,
    }


def test_research_child_sees_only_query_and_candidates_then_app_reverifies_selected() -> None:
    hits = [
        _hit("KSA::505::7", "감사인은 외부조회 절차를 설계하고 수행한다."),
        _hit("KSA::505::8", "조회 응답의 신뢰성을 평가한다."),
    ]
    retriever = StubRetriever(hits)
    client = SelectingClient(count=1)

    result = run_standards_research(_request(), runtime=_runtime(retriever, client))

    assert result["status"] == "completed"
    assert result["collection"] == COLLECTION
    assert result["review_status"] == "unreviewed"
    assert result["turn_scoped"] is True
    assert result["outside_prepared_bundle"] is True
    assert result["effective_date_verified"] is False
    assert len(result["records"]) == 1
    assert result["records"][0]["cid"] == "KSA::505::7"
    assert retriever.get_calls == ["KSA::505::7"]
    payload = json.loads(client.calls[0]["user"])
    assert "allOf" not in client.calls[0]["schema"]
    assert set(payload) == {"request", "candidates", "limits"}
    assert "workbook" not in payload and "history" not in payload
    assert all(set(item) == {
        "typed_kind", "candidate_ref", "cid", "source_type", "standard_no",
        "standard_title", "para_no", "para_type", "section_path", "text",
        "text_sha256", "score",
    } for item in payload["candidates"])


def test_research_candidate_budget_reserves_returned_definition_slots() -> None:
    hits = [
        _hit(f"KSA::315::{number}", f"요구사항 {number}")
        for number in range(1, 6)
    ]
    definition = _hit("KSA::315::정의-위험", "위험의 정의")
    definition.metadata["retrieval_role"] = "definition"
    hits.append(definition)
    retriever = StubRetriever(hits)
    client = SelectingClient(count=0)

    result = run_standards_research(
        _request(),
        runtime=_runtime(retriever, client),
    )

    payload = json.loads(client.calls[0]["user"])
    candidate_cids = [item["cid"] for item in payload["candidates"]]
    assert len(candidate_cids) == 5
    assert "KSA::315::정의-위험" in candidate_cids
    assert "KSA::315::5" not in candidate_cids
    assert result["status"] == "no_results"


def test_research_rejects_worker_ref_not_in_typed_candidates() -> None:
    retriever = StubRetriever([
        _hit("KSA::505::7", "감사인은 외부조회 절차를 수행한다."),
    ])
    client = SelectingClient(forged_ref="candidate:" + "f" * 64)

    with pytest.raises(StandardsResearchError, match="관찰하지 않은"):
        run_standards_research(_request(), runtime=_runtime(retriever, client))
    assert retriever.get_calls == []


def test_research_fails_closed_on_collection_drift_before_worker() -> None:
    retriever = StubRetriever(
        [_hit("KSA::505::7", "감사인은 외부조회 절차를 수행한다.")],
        collection="different_collection",
    )
    client = SelectingClient()

    with pytest.raises(StandardsResearchError) as error:
        run_standards_research(_request(), runtime=_runtime(retriever, client))
    assert error.value.code == "CORPUS_DRIFT"
    assert client.calls == []
    assert retriever.search_calls == []


def test_research_rejects_hit_domain_that_differs_from_the_request() -> None:
    retriever = StubRetriever([
        _hit("KIFRS::1115::31", "수행의무를 이행할 때 수익을 인식한다."),
    ])
    client = SelectingClient()

    with pytest.raises(StandardsResearchError) as error:
        run_standards_research(_request(), runtime=_runtime(retriever, client))

    assert error.value.code == "CONTRACT_MISMATCH"
    assert client.calls == []
    assert retriever.get_calls == []


def test_research_rejects_collection_drift_during_selected_cid_get() -> None:
    class DriftOnGetRetriever(StubRetriever):
        def get_verified_paragraph(self, cid: str):
            paragraph = super().get_verified_paragraph(cid)
            self._collection = "changed_during_get"
            return paragraph

    retriever = DriftOnGetRetriever([
        _hit("KSA::505::7", "감사인은 외부조회 절차를 수행한다."),
    ])
    client = SelectingClient()

    with pytest.raises(StandardsResearchError) as error:
        run_standards_research(_request(), runtime=_runtime(retriever, client))

    assert error.value.code == "CORPUS_DRIFT"
    assert retriever.get_calls == ["KSA::505::7"]


def test_research_maps_selected_cid_transport_failure_to_upstream_unavailable() -> None:
    class FailingGetRetriever(StubRetriever):
        def get_verified_paragraph(self, cid: str):
            self.get_calls.append(cid)
            raise StandardsRetrievalFatalError("MCP transport 오류")

    retriever = FailingGetRetriever([
        _hit("KSA::505::7", "감사인은 외부조회 절차를 수행한다."),
    ])

    with pytest.raises(StandardsResearchError) as error:
        run_standards_research(
            _request(),
            runtime=_runtime(retriever, SelectingClient()),
        )

    assert error.value.code == "UPSTREAM_UNAVAILABLE"
    assert retriever.get_calls == ["KSA::505::7"]


def test_no_results_skips_child_and_materializes_no_ephemeral_authority() -> None:
    retriever = StubRetriever([])
    client = SelectingClient()

    result = run_standards_research(_request(), runtime=_runtime(retriever, client))

    assert result["status"] == "no_results"
    assert result["records"] == []
    assert client.calls == []
    assert research_records(result) == {}


def test_response_summary_is_exact_subset_of_typed_research_observation() -> None:
    retriever = StubRetriever([
        _hit("KSA::505::7", "감사인은 외부조회 절차를 수행한다."),
    ])
    result = run_standards_research(
        _request(), runtime=_runtime(retriever, SelectingClient())
    )
    observations = [{
        "tool": "standards_research",
        "input": {"name": "standards_research"},
        "result": result,
    }]
    ref = result["records"][0]["research_ref"]
    summary = research_summary(observations, selected_refs=[ref])

    assert summary is not None
    assert summary["selected_refs"] == [ref]
    assert validate_research_summary(summary, observations=observations) == summary
    changed = json.loads(json.dumps(summary, ensure_ascii=False))
    changed["citations"][0]["text"] = "변조"
    with pytest.raises(StandardsResearchError, match="observation"):
        validate_research_summary(changed, observations=observations)


def test_public_prompt_and_schema_are_packaged() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "prompts/audit_standards_research_v1.md").is_file()
    schema = json.loads(
        (root / "schemas/audit_standards_research_worker.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["selected_candidate_refs"]["maxItems"] == 3
