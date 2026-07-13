"""Dynamic standards-research integration with the persistent main graph."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver

from excel_to_skill.audit.conversation import run_audit_conversation_turn
from excel_to_skill.audit.model import StandardsDomain
from excel_to_skill.audit.standards import StandardHit

from test_audit_consume_gate import _write_committed_bundle
from test_audit_conversation_aggregate import _prepared_aggregate


def _paragraph(cid: str, text: str) -> dict:
    prefix, standard_no, para_no = cid.split("::", 2)
    return {
        "cid": cid,
        "source_type": "감사기준" if prefix == "KSA" else "회계기준",
        "standard_no": standard_no,
        "standard_title": "외부조회",
        "para_no": para_no,
        "para_type": "요구사항",
        "section_path": "감사증거 > 외부조회",
        "seq": 7,
        "text": text,
        "is_context": False,
    }


class ResearchRetriever:
    def __init__(self, collection: str) -> None:
        self._collection = collection
        self.cid = "KSA::505::7"
        self.text = "감사인은 외부조회 절차를 설계하고 수행한다."
        self.search_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.closed = False

    @property
    def collection(self) -> str:
        return self._collection

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        paragraph = _paragraph(self.cid, self.text)
        return [StandardHit(
            domain=StandardsDomain.AUDIT,
            framework="KSA",
            document_id=self.cid,
            paragraph=paragraph["para_no"],
            title=paragraph["standard_title"],
            snippet=self.text,
            score=0.9,
            corpus_id="auditpaper-standards",
            corpus_version=self._collection,
            retriever_version="stub",
            metadata={
                "source_cid": self.cid,
                "source_type": paragraph["source_type"],
                "standard_no": paragraph["standard_no"],
                "para_type": paragraph["para_type"],
                "section_path": paragraph["section_path"],
                "seq": paragraph["seq"],
                "verified_by": "standards_get_paragraph",
                "paragraph_text_sha256": hashlib.sha256(
                    self.text.encode("utf-8")
                ).hexdigest(),
            },
        )]

    def get_verified_paragraph(self, cid: str):
        self.get_calls.append(cid)
        return _paragraph(cid, self.text)

    def close(self) -> None:
        self.closed = True


class ResearchConversationClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.usage_events: list[dict] = []
        self.main_calls = 0

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["user"])
        self.usage_events.append({
            "provider": "stub",
            "model": "stub-model",
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        })
        if "candidates" in payload:
            assert set(payload) == {"request", "candidates", "limits"}
            return {
                "abstained": False,
                "selected_candidate_refs": [
                    payload["candidates"][0]["candidate_ref"]
                ],
            }
        self.main_calls += 1
        capability = payload["capabilities"]["standards_research"]
        assert capability["enabled"] is True
        if self.main_calls == 1:
            return {
                "action": "tool",
                "tool": {
                    "name": "standards_research",
                    "query": "외부조회 절차의 감사기준 요구사항",
                    "kind": "audit_standard",
                    "item_id": None,
                    "limit": 3,
                },
                "final": None,
            }
        research = next(
            item["result"] for item in payload["observations"]
            if item.get("tool") == "standards_research"
        )
        return {
            "action": "final",
            "tool": None,
            "final": {
                "abstained": False,
                "abstention_code": None,
                "selections": [],
                "research_refs": [research["records"][0]["research_ref"]],
            },
        }


def test_workbook_conversation_runs_research_lazily_and_keeps_it_supplemental(
    tmp_path: Path,
) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)
    collection = standards["retriever"]["corpus_version"]
    retriever = ResearchRetriever(collection)
    client = ResearchConversationClient()

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 관련 기준은?",
        thread_id="research-thread",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        standards_retriever=retriever,
    )

    response = result["response"]
    assert response["answer"]["abstained"] is True
    research = response["standards_research"]
    assert research["review_status"] == "unreviewed"
    assert research["turn_scoped"] is True
    assert research["outside_prepared_bundle"] is True
    assert research["effective_date_verified"] is False
    assert research["citations"][0]["cid"] == "KSA::505::7"
    assert research["citations"][0]["research_ref"].startswith("research:")
    assert retriever.get_calls == ["KSA::505::7"]
    assert retriever.closed is True
    assert result["usage"]["request_count"] == 3
    assert len(client.calls) == 3


def test_collection_drift_fails_closed_before_child_selection(
    tmp_path: Path,
) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)
    expected_collection = standards["retriever"]["corpus_version"]
    actual_collection = f"{expected_collection}-drifted"
    retriever = ResearchRetriever(actual_collection)

    class CorpusDriftClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.main_calls = 0
            self.child_calls = 0
            self.research_observation: dict | None = None

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if "candidates" in payload:
                self.child_calls += 1
                raise AssertionError(
                    "collection drift must fail before child candidate selection"
                )
            self.main_calls += 1
            if self.main_calls == 1:
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "외부조회 절차의 감사기준 요구사항",
                        "kind": "audit_standard",
                        "item_id": None,
                        "limit": 3,
                    },
                    "final": None,
                }
            self.research_observation = next(
                item for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            assert self.research_observation["result"] == {
                "error": {
                    "code": "CORPUS_DRIFT",
                    "message": "동적 기준서 조회를 완료하지 못했습니다.",
                }
            }
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "retrieval_incomplete",
                    "selections": [],
                    "research_refs": [],
                },
            }

    client = CorpusDriftClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 관련 기준을 다시 확인해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        max_steps=2,
        standards_research=True,
        standards_retriever=retriever,
    )

    assert expected_collection != actual_collection
    assert client.main_calls == 2
    assert client.child_calls == 0
    assert len(client.calls) == 2
    assert client.research_observation is not None
    assert retriever.search_calls == []
    assert retriever.get_calls == []
    assert retriever.closed is True
    response = result["response"]
    assert response["answer"]["abstained"] is True
    assert response["answer"]["abstention_reason"] == (
        "근거 조회가 불완전하여 답변을 보류합니다."
    )
    assert response["coverage"]["discovery_complete"] is False
    assert response["generator"]["tools_used"] == [
        "brief",
        "assertion_procedures",
        "standards_research",
    ]
    assert "standards_research" not in response


def test_empty_research_does_not_consume_a_child_model_step(tmp_path: Path) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)

    class EmptyRetriever(ResearchRetriever):
        def search(self, query, **kwargs):
            self.search_calls.append({"query": query, **kwargs})
            return []

    class NoResultClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            assert "candidates" not in payload
            if len(self.calls) == 1:
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "검색 결과가 없는 기준 질의",
                        "kind": "audit_standard",
                        "item_id": None,
                        "limit": 3,
                    },
                    "final": None,
                }
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "retrieval_incomplete",
                    "selections": [],
                    "research_refs": [],
                },
            }

    retriever = EmptyRetriever(standards["retriever"]["corpus_version"])
    client = NoResultClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="검색 결과가 없는 기준을 확인해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        max_steps=2,
        standards_research=True,
        standards_retriever=retriever,
    )

    assert len(client.calls) == 2
    assert len(retriever.search_calls) == 1
    assert retriever.get_calls == []
    assert result["response"]["standards_research"]["status"] == "no_results"


def test_disabled_research_never_constructs_retriever_and_returns_fixed_error(
    tmp_path: Path,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    factory_calls: list[str] = []
    responses = iter((
        {
            "action": "tool",
            "tool": {
                "name": "standards_research",
                "query": "외부조회 요구사항",
                "kind": "audit_standard",
                "item_id": None,
                "limit": 3,
            },
            "final": None,
        },
        {
            "action": "final",
            "tool": None,
            "final": {
                "abstained": True,
                "abstention_code": "retrieval_incomplete",
                "selections": [],
            },
        },
    ))

    class Client:
        usage_events: list[dict] = []
        calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            return next(responses)

    def factory(collection: str):
        factory_calls.append(collection)
        raise AssertionError("disabled research must stay lazy")

    client = Client()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 기준은?",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=False,
        standards_retriever_factory=factory,
    )

    assert result["response"]["answer"]["abstained"] is True
    assert "standards_research" not in result["response"]
    assert factory_calls == []
    second_payload = json.loads(client.calls[1]["user"])
    observation = next(
        item for item in second_payload["observations"]
        if item.get("tool") == "standards_research"
    )
    assert observation["result"]["error"] == {
        "code": "RESEARCH_DISABLED",
        "message": "동적 기준서 조회를 완료하지 못했습니다.",
    }


def test_ephemeral_research_is_not_reexposed_in_next_turn_focus(tmp_path: Path) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    saver = InMemorySaver()
    first = ResearchConversationClient()
    run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 관련 기준은?",
        thread_id="research-focus-thread",
        client=first,
        checkpointer=saver,
        runtime_root=root,
        standards_research=True,
        standards_retriever=ResearchRetriever(
            standards["retriever"]["corpus_version"]
        ),
    )

    class FollowupClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            serialized = json.dumps(payload, ensure_ascii=False)
            assert "research:" not in serialized
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
    second = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="그 내용이 조서에 수행됐나?",
        thread_id="research-focus-thread",
        client=followup,
        checkpointer=saver,
        runtime_root=root,
        standards_research=False,
    )

    assert second["turn_index"] == 2
    focus = json.loads(followup.calls[0]["user"])["observations"][-1]
    assert focus["tool"] == "conversation_focus"
    assert "research:" not in json.dumps(focus, ensure_ascii=False)


def test_aggregate_research_is_pinned_to_selected_source_scope(tmp_path: Path) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    created: list[tuple[str, ResearchRetriever]] = []

    def factory(collection: str):
        retriever = ResearchRetriever(collection)
        created.append((collection, retriever))
        return retriever

    class AggregateResearchClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.main_calls = 0

        def __call__(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if "candidates" in payload:
                return {
                    "abstained": False,
                    "selected_candidate_refs": [
                        payload["candidates"][0]["candidate_ref"]
                    ],
                }
            self.main_calls += 1
            if self.main_calls == 1:
                scope_id = payload["observations"][0]["result"]["accounts"][0][
                    "scope"
                ]["id"]
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "외부조회 절차의 감사기준 요구사항",
                        "kind": "audit_standard",
                        "item_ref": None,
                        "scope_id": scope_id,
                        "limit": 3,
                    },
                    "final": None,
                }
            research = next(
                item["result"] for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": False,
                    "abstention_code": None,
                    "selections": [],
                    "research_refs": [research["records"][0]["research_ref"]],
                },
            }

    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="stub-model",
        question="외부조회 기준은?",
        client=AggregateResearchClient(),
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        standards_retriever_factory=factory,
    )

    assert result["response"]["schema_version"] == "audit_main_agent_response.v1"
    supplement = result["response"]["standards_research"]
    assert supplement["scope"]["id"] in result["bundle"]["selection"]["scope_ids"]
    assert len(created) == 1
    expected_collection, retriever = created[0]
    assert supplement["collection"] == expected_collection
    assert retriever.closed is True


def test_research_failure_does_not_block_a_committed_grounded_answer(
    tmp_path: Path,
) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)

    class FailingRetriever(ResearchRetriever):
        def search(self, query, **kwargs):
            self.search_calls.append({"query": query, **kwargs})
            raise RuntimeError("provider detail must not escape")

    class FallbackClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if len(self.calls) == 1:
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "외부조회 감사기준",
                        "kind": "audit_standard",
                        "item_id": None,
                        "limit": 3,
                    },
                    "final": None,
                }
            observation = next(
                item for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            assert observation["result"] == {
                "error": {
                    "code": "UPSTREAM_UNAVAILABLE",
                    "message": "동적 기준서 조회를 완료하지 못했습니다.",
                }
            }
            assert "provider detail" not in kwargs["user"]
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": False,
                    "abstention_code": None,
                    "selections": [
                        {"kind": "statement", "ids": ["statement:fact"]}
                    ],
                },
            }

    retriever = FailingRetriever(standards["retriever"]["corpus_version"])
    client = FallbackClient()
    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="조서와 외부 기준을 함께 설명해줘",
        client=client,
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        standards_retriever=retriever,
    )

    assert result["response"]["answer"]["abstained"] is False
    assert result["response"]["answer"]["claims"][0]["statement_ids"] == [
        "statement:fact"
    ]
    assert "standards_research" not in result["response"]
    assert result["response"]["coverage"]["discovery_complete"] is False
    assert len(retriever.search_calls) == 1
    assert retriever.get_calls == []
    assert retriever.closed is True


def test_second_research_request_is_bounded_without_a_second_mcp_call(
    tmp_path: Path,
) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)
    retriever = ResearchRetriever(standards["retriever"]["corpus_version"])

    class BoundedResearchClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.main_calls = 0
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if "candidates" in payload:
                return {
                    "abstained": False,
                    "selected_candidate_refs": [
                        payload["candidates"][0]["candidate_ref"]
                    ],
                }
            self.main_calls += 1
            if self.main_calls <= 2:
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": f"외부조회 기준 질의 {self.main_calls}",
                        "kind": "audit_standard",
                        "item_id": None,
                        "limit": 3,
                    },
                    "final": None,
                }
            research_observations = [
                item for item in payload["observations"]
                if item.get("tool") == "standards_research"
            ]
            assert research_observations[1]["result"] == {
                "error": {
                    "code": "RESEARCH_LIMIT_EXCEEDED",
                    "message": "동적 기준서 조회를 완료하지 못했습니다.",
                }
            }
            research_ref = research_observations[0]["result"]["records"][0][
                "research_ref"
            ]
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": False,
                    "abstention_code": None,
                    "selections": [
                        {"kind": "statement", "ids": ["statement:fact"]}
                    ],
                    "research_refs": [research_ref],
                },
            }

    result = run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 기준을 두 번 확인해줘",
        client=BoundedResearchClient(),
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        standards_retriever=retriever,
    )

    assert len(retriever.search_calls) == 1
    assert retriever.get_calls == ["KSA::505::7"]
    assert result["response"]["coverage"]["discovery_complete"] is False
    assert len(result["response"]["standards_research"]["citations"]) == 1


def test_aggregate_research_rejects_unexposed_scope_before_retriever_factory(
    tmp_path: Path,
) -> None:
    pkg, aggregate = _prepared_aggregate(tmp_path)
    factory_calls: list[str] = []

    def factory(collection: str):
        factory_calls.append(collection)
        raise AssertionError("invalid aggregate scope must fail before MCP construction")

    class InvalidScopeClient:
        usage_events: list[dict] = []

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            payload = json.loads(kwargs["user"])
            if len(self.calls) == 1:
                return {
                    "action": "tool",
                    "tool": {
                        "name": "standards_research",
                        "query": "외부조회 기준",
                        "kind": "audit_standard",
                        "item_ref": None,
                        "scope_id": "f" * 64,
                        "limit": 3,
                    },
                    "final": None,
                }
            observation = next(
                item for item in payload["observations"]
                if item.get("tool") == "standards_research"
            )
            assert observation["result"]["error"]["code"] == "INVALID_REQUEST"
            return {
                "action": "final",
                "tool": None,
                "final": {
                    "abstained": True,
                    "abstention_code": "retrieval_incomplete",
                    "selections": [],
                },
            }

    result = run_audit_conversation_turn(
        pkg,
        aggregate_id=aggregate.paths.aggregate_id,
        model="stub-model",
        question="노출되지 않은 범위의 기준은?",
        client=InvalidScopeClient(),
        checkpointer=InMemorySaver(),
        runtime_root=tmp_path / "runtime",
        standards_research=True,
        standards_retriever_factory=factory,
    )

    assert factory_calls == []
    assert result["response"]["answer"]["abstained"] is True
    assert "standards_research" not in result["response"]


def test_research_text_stays_out_of_checkpoints_and_prepared_artifacts(
    tmp_path: Path,
) -> None:
    pkg, _, standards, _ = _write_committed_bundle(tmp_path)
    root = tmp_path / "runtime"
    prepared_before = {
        path.name: path.read_bytes()
        for path in (
            pkg / "data/audit_facts.json",
            pkg / "data/standards_context.json",
            pkg / "data/audit_brief.json",
        )
    }
    retriever = ResearchRetriever(standards["retriever"]["corpus_version"])

    run_audit_conversation_turn(
        pkg,
        model="stub-model",
        question="외부조회 관련 기준은?",
        thread_id="research-sqlite-thread",
        client=ResearchConversationClient(),
        runtime_root=root,
        standards_research=True,
        standards_retriever=retriever,
    )

    checkpoint_bytes = b"".join(
        path.read_bytes() for path in root.glob("checkpoints.sqlite3*")
    )
    assert retriever.text.encode("utf-8") not in checkpoint_bytes
    assert "외부조회 관련 기준은?".encode("utf-8") not in checkpoint_bytes
    assert b"research:" not in checkpoint_bytes
    assert b"MCP_AUTH_TOKEN" not in checkpoint_bytes
    with sqlite3.connect(root / "checkpoints.sqlite3") as connection:
        for table in ("checkpoints", "writes"):
            child_rows = connection.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE checkpoint_ns LIKE 'execute_research:%'"
            ).fetchone()
            assert child_rows == (0,)
    private_objects = b"".join(
        path.read_bytes() for path in root.glob("threads/*/objects/*.json")
    )
    assert retriever.text.encode("utf-8") in private_objects
    prepared_after = {
        path.name: path.read_bytes()
        for path in (
            pkg / "data/audit_facts.json",
            pkg / "data/standards_context.json",
            pkg / "data/audit_brief.json",
        )
    }
    assert prepared_after == prepared_before
