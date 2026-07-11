from __future__ import annotations

import copy
import json
import multiprocessing
import os
import socket
import time
from pathlib import Path

import pytest

from excel_to_skill.audit.auditpaper_mcp import (
    AuditpaperStandardsRetriever,
    DEFAULT_REMOTE_URL,
    FastMCPHTTPCaller,
    MCPConnection,
    RetrievalPolicy,
    _explicit_standard_numbers,
    _tool_result_payload,
    load_mcp_connection,
)
from excel_to_skill.audit.context import build_standards_context
from excel_to_skill.audit.standards import (
    StandardsQueryError,
    StandardsRetrievalFatalError,
)
from excel_to_skill.cli import _build_parser, _cmd_prepare, _load_local_env

from test_audit_prepare import PipelineClient, _package


COLLECTION = "standards_20250829_bgem3"


def _serve_loopback_mcp(port: int) -> None:
    from fastmcp import FastMCP

    app = FastMCP("excel-to-skill-loopback-test")

    @app.tool
    async def echo(value: str) -> dict:
        return {"collection": "loopback-v1", "value": value}

    app.run(
        transport="http",
        host="127.0.0.1",
        port=port,
        path="/mcp",
    )


def _paragraph(
    cid: str,
    *,
    text: str,
    source_type: str = "감사기준",
    standard_no: str = "315",
    para_no: str = "12",
) -> dict:
    return {
        "cid": cid,
        "source_type": source_type,
        "standard_no": standard_no,
        "standard_title": "중요왜곡표시위험의 식별과 평가",
        "para_no": para_no,
        "para_type": "요구사항",
        "section_path": "위험평가",
        "seq": 12,
        "text": text,
        "is_context": False,
    }


class StubCaller:
    def __init__(
        self, responses: dict[str, object], *, echo_applied_filters: bool = False
    ) -> None:
        self.responses = responses
        self.echo_applied_filters = echo_applied_filters
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        args = dict(arguments)
        self.calls.append((name, args))
        response = self.responses[name]
        if callable(response):
            response = response(args)
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        if (
            name == "standards_search"
            and self.echo_applied_filters
            and isinstance(response, dict)
            and "error" not in response
        ):
            response = copy.deepcopy(response)
            response.setdefault("applied", {})["filters"] = {
                field: args[field]
                for field in ("standard_no", "source_type", "para_type")
                if field in args
            }
        return response


def _success_caller() -> StubCaller:
    requirement_cid = "KSA::315::12"
    definition_cid = "KSA::315::정의-위험"
    requirement = "감사인은 중요한 위험을 식별하고 평가한다."
    definition = "위험은 재무제표의 중요한 왜곡 가능성이다."

    def get_paragraph(args):
        cid = args["cid"]
        if cid == requirement_cid:
            paragraph = _paragraph(cid, text=requirement)
        else:
            paragraph = _paragraph(
                cid, text=definition, para_no="정의-위험"
            )
        return {
            "collection": COLLECTION,
            "found": True,
            "paragraphs": [paragraph],
        }

    return StubCaller({
        "standards_define_terms": {
            "collection": COLLECTION,
            "definitions": [],
            "not_found": ["__excel_to_skill_collection_probe__"],
        },
        "standards_search": {
            "collection": COLLECTION,
            "results": [{
                "cid": requirement_cid,
                "score": 0.87,
                "standard_no": "315",
                "standard_title": "중요왜곡표시위험의 식별과 평가",
                "para_no": "12",
                "para_type": "요구사항",
                "section_path": "위험평가",
                "text": requirement,
                "notes": [],
            }],
            "definitions": [{
                "term": "위험",
                "source_cid": definition_cid,
                "standard_no": "315",
                "text": definition,
            }],
            "applied": {"filters": {}},
        },
        "standards_get_paragraph": get_paragraph,
    }, echo_applied_filters=True)


def test_adapter_discovers_collection_maps_filters_and_verifies_every_cid() -> None:
    caller = _success_caller()
    retriever = AuditpaperStandardsRetriever(
        caller,
        policy=RetrievalPolicy(top_k=5, max_definitions=2, upstream_retries=0),
    )
    descriptor = retriever.descriptor(retrieved_at="2026-07-11T00:00:00Z")
    hits = retriever.search(
        "KSA 315 위험 식별 요구사항",
        domain="audit",
        framework="KSA",
        effective_date="2026-12-31",
    )

    assert descriptor["corpus_version"] == COLLECTION
    assert descriptor["corpus_id"] == "auditpaper-standards"
    assert len(descriptor["config_sha256"]) == 64
    assert [hit.document_id for hit in hits] == [
        "KSA::315::12",
        "KSA::315::정의-위험",
    ]
    assert hits[0].score == 0.87 and hits[1].score is None
    assert all(hit.effective_date is None for hit in hits)
    assert all(hit.corpus_version == COLLECTION for hit in hits)
    search_call = next(args for name, args in caller.calls if name == "standards_search")
    assert search_call == {
        "query": "KSA 315 위험 식별 요구사항",
        "source_type": ["감사기준"],
        "top_k": 5,
        "include_examples": False,
        "standard_no": ["315"],
    }
    get_calls = [args for name, args in caller.calls if name == "standards_get_paragraph"]
    assert get_calls == [
        {"cid": "KSA::315::12", "context": 0},
        {"cid": "KSA::315::정의-위험", "context": 0},
    ]


def test_definition_excerpt_is_replaced_with_verified_full_paragraph() -> None:
    caller = _success_caller()
    search = caller.responses["standards_search"]
    search["definitions"][0]["text"] = "중요한 왜곡 가능성"
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    hits = retriever.search("위험", domain="audit", framework="KSA")

    assert hits[1].document_id == "KSA::315::정의-위험"
    assert hits[1].snippet == "위험은 재무제표의 중요한 왜곡 가능성이다."
    assert hits[1].metadata["search_text_sha256"] != (
        hits[1].metadata["paragraph_text_sha256"]
    )


def test_definition_excerpt_must_come_from_verified_paragraph() -> None:
    caller = _success_caller()
    caller.responses["standards_search"]["definitions"][0]["text"] = (
        "직조회 원문과 무관한 정의"
    )
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    with pytest.raises(StandardsRetrievalFatalError, match="definition excerpt"):
        retriever.search("위험", domain="audit", framework="KSA")


@pytest.mark.parametrize(("query", "expected"), [
    ("GUIDE 2016-1", ["2016-1"]),
    ("KSA ASSR-3000", ["ASSR-3000"]),
    ("KSA FRMK-1", ["FRMK-1"]),
    ("KSA PS2", ["PS2"]),
    ("KSA-315", ["315"]),
    ("KSA::315::12", ["315"]),
    ("KSA 315 및 330, 500", ["315", "330", "500"]),
    ("감사기준서 제315호 및 제330호", ["315", "330"]),
    ("KSA 315 및 2025년 개정", ["315"]),
    ("KSA 315, 2025 개정판", ["315"]),
    ("K-IFRS 1115와 2026년 적용", ["1115"]),
])
def test_explicit_standard_numbers_support_corpus_number_grammar(
    query: str, expected: list[str]
) -> None:
    assert _explicit_standard_numbers(query) == expected


def test_structured_standard_numbers_override_query_heuristics() -> None:
    caller = _success_caller()
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    retriever.search(
        "위험평가 기준",
        domain="audit",
        framework="KSA",
        standard_nos=["315"],
    )

    search_call = next(args for name, args in caller.calls if name == "standards_search")
    assert search_call["standard_no"] == ["315"]


def test_adapter_retries_only_upstream_envelope() -> None:
    caller = _success_caller()
    caller.responses["standards_search"] = [
        {
            "collection": COLLECTION,
            "error": {
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "encoder warming",
                "hint": "retry",
            },
        },
        caller.responses["standards_search"],
    ]
    delays: list[float] = []
    retriever = AuditpaperStandardsRetriever(
        caller,
        policy=RetrievalPolicy(upstream_retries=1, retry_delays=(0.25,)),
        sleeper=delays.append,
    )

    hits = retriever.search("위험", domain="audit", framework="KSA")
    assert hits and delays == [0.25]
    assert len([call for call in caller.calls if call[0] == "standards_search"]) == 2


def test_invalid_input_is_query_error_but_collection_drift_is_fatal() -> None:
    invalid = StubCaller({
        "standards_define_terms": {
            "collection": COLLECTION, "definitions": [], "not_found": []
        },
        "standards_search": {
            "collection": COLLECTION,
            "error": {"code": "INVALID_INPUT", "message": "bad", "hint": "fix"},
        },
    })
    retriever = AuditpaperStandardsRetriever(
        invalid, policy=RetrievalPolicy(upstream_retries=0)
    )
    with pytest.raises(StandardsQueryError, match="INVALID_INPUT"):
        retriever.search("질의", domain="audit", framework="KSA")

    drift = _success_caller()
    drift.responses["standards_search"] = {
        **drift.responses["standards_search"],
        "collection": "new_collection",
    }
    retriever = AuditpaperStandardsRetriever(
        drift, policy=RetrievalPolicy(upstream_retries=0)
    )
    with pytest.raises(StandardsRetrievalFatalError, match="collection drift"):
        retriever.search("질의", domain="audit", framework="KSA")


def test_search_get_text_or_cid_contract_mismatch_is_fatal() -> None:
    caller = _success_caller()

    def changed_paragraph(args):
        return {
            "collection": COLLECTION,
            "found": True,
            "paragraphs": [_paragraph(args["cid"], text="변경된 원문")],
        }

    caller.responses["standards_get_paragraph"] = changed_paragraph
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(max_definitions=0, upstream_retries=0)
    )
    with pytest.raises(StandardsRetrievalFatalError, match="원문 불일치"):
        retriever.search("질의", domain="audit", framework="KSA")


def test_applied_filters_must_echo_the_requested_filters() -> None:
    caller = _success_caller()
    caller.echo_applied_filters = False
    caller.responses["standards_search"]["applied"]["filters"]["source_type"] = [
        "회계기준"
    ]
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    with pytest.raises(StandardsRetrievalFatalError, match="applied filters"):
        retriever.search("질의", domain="audit", framework="KSA")


def test_applied_filters_reject_unrequested_extra_filter() -> None:
    caller = _success_caller()
    caller.echo_applied_filters = False
    caller.responses["standards_search"]["applied"]["filters"] = {
        "source_type": ["감사기준"],
        "standard_no": ["999"],
    }
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    with pytest.raises(StandardsRetrievalFatalError, match="applied filters"):
        retriever.search("질의", domain="audit", framework="KSA")


def test_result_must_stay_inside_requested_standard_numbers() -> None:
    caller = _success_caller()
    search = caller.responses["standards_search"]
    search["results"] = [{
        "cid": "KSA::330::1",
        "score": 0.7,
        "standard_no": "330",
        "standard_title": "중요왜곡표시위험의 식별과 평가",
        "para_no": "1",
        "para_type": "요구사항",
        "section_path": "목적",
        "text": "감사인은 평가된 위험에 대응한다.",
        "notes": [],
    }]
    search["definitions"] = []

    def get_paragraph(args):
        cid = args["cid"]
        return {
            "collection": COLLECTION,
            "found": True,
            "paragraphs": [_paragraph(
                cid,
                text="감사인은 평가된 위험에 대응한다.",
                standard_no="330",
                para_no="1",
            )],
        }

    caller.responses["standards_get_paragraph"] = get_paragraph
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    with pytest.raises(StandardsRetrievalFatalError, match="요청 standard_no 밖"):
        retriever.search("KSA 315 위험", domain="audit", framework="KSA")


def test_cid_paragraph_segment_must_match_verified_para_no() -> None:
    caller = _success_caller()
    search = caller.responses["standards_search"]
    search["results"][0]["para_no"] = "13"
    search["definitions"] = []

    def get_paragraph(args):
        return {
            "collection": COLLECTION,
            "found": True,
            "paragraphs": [_paragraph(
                args["cid"],
                text="감사인은 중요한 위험을 식별하고 평가한다.",
                para_no="13",
            )],
        }

    caller.responses["standards_get_paragraph"] = get_paragraph
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(max_definitions=0, upstream_retries=0)
    )

    with pytest.raises(StandardsRetrievalFatalError, match="CID/paragraph para_no"):
        retriever.search("질의", domain="audit", framework="KSA")


def test_verified_paragraph_cache_persists_collection_cid_and_full_text(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "paragraph-cache"
    first_caller = _success_caller()
    first = AuditpaperStandardsRetriever(
        first_caller,
        policy=RetrievalPolicy(max_definitions=0, upstream_retries=0),
        paragraph_cache_dir=cache_dir,
    )
    first_hits = first.search("위험", domain="audit", framework="KSA")
    cache_files = list(cache_dir.rglob("*.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert cached["collection"] == COLLECTION
    assert cached["cid"] == "KSA::315::12"
    assert cached["paragraph"]["text"] == first_hits[0].snippet

    second_caller = _success_caller()
    second_caller.responses["standards_get_paragraph"] = AssertionError(
        "persistent cache hit must not call get_paragraph"
    )
    second = AuditpaperStandardsRetriever(
        second_caller,
        policy=RetrievalPolicy(max_definitions=0, upstream_retries=0),
        paragraph_cache_dir=cache_dir,
    )
    assert second.search("위험", domain="audit", framework="KSA")[0].snippet == (
        first_hits[0].snippet
    )
    assert not any(
        name == "standards_get_paragraph" for name, _ in second_caller.calls
    )

    cache_files[0].write_text("{}", encoding="utf-8")
    third_caller = _success_caller()
    third = AuditpaperStandardsRetriever(
        third_caller,
        policy=RetrievalPolicy(max_definitions=0, upstream_retries=0),
        paragraph_cache_dir=cache_dir,
    )
    third.search("위험", domain="audit", framework="KSA")
    assert any(name == "standards_get_paragraph" for name, _ in third_caller.calls)


def test_invalid_get_paragraph_response_never_poisons_persistent_cache(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "paragraph-cache"
    bad_caller = _success_caller()

    def bad_get(args):
        return {
            "collection": COLLECTION,
            "found": True,
            "paragraphs": [_paragraph(
                args["cid"],
                text="감사인은 중요한 위험을 식별하고 평가한다.",
                para_no="13",
            )],
        }

    bad_caller.responses["standards_get_paragraph"] = bad_get
    first = AuditpaperStandardsRetriever(
        bad_caller,
        policy=RetrievalPolicy(max_definitions=0, upstream_retries=0),
        paragraph_cache_dir=cache_dir,
    )
    with pytest.raises(StandardsRetrievalFatalError, match="CID/paragraph para_no"):
        first.search("위험", domain="audit", framework="KSA")
    assert list(cache_dir.rglob("*.json")) == []

    good_caller = _success_caller()
    second = AuditpaperStandardsRetriever(
        good_caller,
        policy=RetrievalPolicy(max_definitions=0, upstream_retries=0),
        paragraph_cache_dir=cache_dir,
    )
    assert second.search("위험", domain="audit", framework="KSA")
    assert any(name == "standards_get_paragraph" for name, _ in good_caller.calls)


def test_guide_queries_are_rejected_before_any_mcp_call() -> None:
    caller = _success_caller()
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(upstream_retries=0)
    )

    with pytest.raises(StandardsQueryError, match="실무지침"):
        retriever.search("GUIDE 2016-1", domain="audit", framework="GUIDE")
    with pytest.raises(StandardsQueryError, match="실무지침"):
        retriever.search("GUIDE-2016-1", domain="audit", framework="KSA")
    assert caller.calls == []


def test_reference_table_warning_is_not_promoted_to_authoritative_citation() -> None:
    caller = _success_caller()
    search = caller.responses["standards_search"]
    search["results"][0].update({
        "para_type": "참조",
        "notes": ["발췌 대조표 — 원전 문단 우선 인용"],
    })
    search["definitions"] = []
    retriever = AuditpaperStandardsRetriever(
        caller, policy=RetrievalPolicy(max_definitions=0, upstream_retries=0)
    )

    assert retriever.search("질의", domain="audit", framework="KSA") == []
    assert not any(name == "standards_get_paragraph" for name, _ in caller.calls)


def test_connection_loader_uses_env_without_exposing_token(tmp_path: Path) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "auditpaper-standards": {
                "type": "http",
                "url": "${REMOTE_URL}",
                "headers": {"Authorization": "Bearer ${CONFIG_TOKEN}"},
            }
        }
    }), encoding="utf-8")
    connection = load_mcp_connection(
        config_path=config,
        environ={
            "REMOTE_URL": "https://example.test/mcp",
            "CONFIG_TOKEN": "config-secret",
            "MCP_AUTH_TOKEN": "env-secret",
        },
    )
    assert connection == MCPConnection(
        server_name="auditpaper-standards",
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer env-secret"},
    )

    with pytest.raises(Exception) as exc_info:
        load_mcp_connection(
            url="http://public.example/mcp",
            environ={"MCP_AUTH_TOKEN": "do-not-print-me"},
        )
    assert "do-not-print-me" not in str(exc_info.value)


def test_env_token_removes_case_insensitive_config_authorization(tmp_path: Path) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "auditpaper-standards": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {
                    "authorization": "Bearer ${MISSING_CONFIG_TOKEN}",
                    "X-Client": "excel-to-skill",
                },
            }
        }
    }), encoding="utf-8")

    connection = load_mcp_connection(
        config_path=config,
        environ={"MCP_AUTH_TOKEN": "fresh-secret"},
    )

    assert connection.headers == {
        "X-Client": "excel-to-skill",
        "Authorization": "Bearer fresh-secret",
    }


def test_connection_loader_uses_documented_fixed_remote_by_default() -> None:
    connection = load_mcp_connection(
        environ={"MCP_AUTH_TOKEN": "secret"},
    )
    assert connection.url == DEFAULT_REMOTE_URL
    assert connection.headers == {"Authorization": "Bearer secret"}


def test_cli_loads_local_dotenv_without_overriding_exported_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "exported-key")
    (tmp_path / ".env").write_text(
        "MCP_AUTH_TOKEN=file-token\nANTHROPIC_API_KEY=file-key\n",
        encoding="utf-8",
    )

    _load_local_env()

    assert os.environ["MCP_AUTH_TOKEN"] == "file-token"
    assert os.environ["ANTHROPIC_API_KEY"] == "exported-key"


def test_tool_result_payload_prefers_parsed_data_and_unwraps_structured() -> None:
    class Result:
        is_error = False
        data = {"collection": COLLECTION, "results": []}
        structured_content = {"result": {"collection": "ignored"}}
        content = []

    assert _tool_result_payload(Result(), tool="search")["collection"] == COLLECTION
    Result.data = None
    assert _tool_result_payload(Result(), tool="search") == {
        "collection": "ignored"
    }


def test_fastmcp_http_caller_round_trip_on_loopback() -> None:
    pytest.importorskip("fastmcp")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    except PermissionError:
        pytest.skip("sandbox가 loopback socket 생성을 허용하지 않음")
    process = multiprocessing.Process(target=_serve_loopback_mcp, args=(port,), daemon=True)
    process.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("loopback FastMCP server did not start")

        caller = FastMCPHTTPCaller(
            MCPConnection(
                server_name="loopback",
                url=f"http://127.0.0.1:{port}/mcp",
                headers={},
            ),
            init_timeout=10,
            call_timeout=10,
        )
        try:
            assert caller.call_tool("echo", {"value": "ok"}) == {
                "collection": "loopback-v1",
                "value": "ok",
            }
        finally:
            caller.close()
    finally:
        process.terminate()
        process.join(timeout=5)


def test_prepare_cli_wires_fake_mcp_without_network(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pkg = _package(tmp_path)
    caller = _success_caller()
    monkeypatch.setenv("MCP_AUTH_TOKEN", "stub-token-not-written")
    args = _build_parser().parse_args([
        "prepare",
        str(pkg),
        "--mcp-url",
        "https://example.test/mcp",
        "--model",
        "stub-model",
        "--standards-top-k",
        "3",
        "--standards-definitions",
        "1",
    ])

    result = _cmd_prepare(
        args,
        client_factory=lambda _model: PipelineClient(),
        caller_factory=lambda _connection: caller,
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.strip() == str(pkg / "data/audit_brief.json")
    assert "stub-token-not-written" not in captured.out + captured.err
    assert f"standards collection 고정: {COLLECTION}" in captured.err
    context = json.loads((pkg / "data/standards_context.json").read_text(encoding="utf-8"))
    assert context["retriever"]["corpus_version"] == COLLECTION
    assert any(
        item["code"] == "effective_date_unverified"
        for item in context["limitations"]
    )


def test_fatal_retrieval_contract_error_aborts_context_instead_of_publishing_partial() -> None:
    class FatalRetriever:
        def search(self, *args, **kwargs):
            raise StandardsRetrievalFatalError("collection drift")

    facts = {
        "source": {"filename": "audit.xlsx", "sha256": "a" * 64, "format": "xlsx"},
        "standard_queries": [{
            "id": "query:1",
            "query": "감사 위험",
            "domain": "audit",
            "framework": "KSA",
            "effective_date": None,
            "fact_ids": ["fact:1"],
            "rationale": "관련 기준 확인",
        }],
    }
    with pytest.raises(StandardsRetrievalFatalError, match="collection drift"):
        build_standards_context(
            facts,
            FatalRetriever(),
            retriever_descriptor={
                "name": "stub",
                "version": "1",
                "mcp_server": "stub",
                "tool": "search",
                "corpus_id": "stub",
                "corpus_version": "v1",
                "retrieved_at": "2026-07-11T00:00:00Z",
            },
        )


def test_run_citation_cap_becomes_explicit_query_limitation() -> None:
    caller = _success_caller()
    retriever = AuditpaperStandardsRetriever(
        caller,
        policy=RetrievalPolicy(
            max_citations=1,
            max_definitions=1,
            upstream_retries=0,
        ),
    )
    descriptor = retriever.descriptor(retrieved_at="2026-07-11T00:00:00Z")
    facts = {
        "source": {"filename": "audit.xlsx", "sha256": "a" * 64, "format": "xlsx"},
        "standard_queries": [{
            "id": "query:1", "query": "위험", "domain": "audit",
            "framework": "KSA", "effective_date": None,
            "fact_ids": ["fact:1"], "rationale": "기준 확인",
        }],
    }

    context = build_standards_context(
        facts,
        retriever,
        retriever_descriptor=descriptor,
    )
    assert context["queries"][0]["status"] == "error"
    assert "retrieval_capped" in {item["code"] for item in context["limitations"]}


def test_single_paragraph_cap_is_a_query_limitation_not_contract_failure() -> None:
    caller = _success_caller()
    retriever = AuditpaperStandardsRetriever(
        caller,
        policy=RetrievalPolicy(
            max_text_chars=10,
            max_total_chars=100,
            max_run_text_chars=100,
            max_definitions=0,
            upstream_retries=0,
        ),
    )

    with pytest.raises(StandardsQueryError) as exc_info:
        retriever.search("위험", domain="audit", framework="KSA")

    assert exc_info.value.limitation_code == "retrieval_capped"
