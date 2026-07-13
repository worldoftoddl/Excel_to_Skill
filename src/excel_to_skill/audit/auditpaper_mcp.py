"""`auditpaper-standards` 원격 MCP를 :class:`StandardsRetriever`로 연결한다.

서버의 검색 점수는 후보 순위 선정에만 쓰고, 실제 citation 본문은 채택한 모든 CID를
``standards_get_paragraph(context=0)`` 직조회 또는 같은 collection+CID의 검증 cache로
확정한다. 검색과 확정 전문이 서로 다르거나 실행 도중 collection이 바뀌면 partial 결과를
게시하지 않고 prepare 전체를 중단한다. 서버는 구조화 시행일을 제공하지 않으므로 이를
hit에 추정해 채우지 않는다.

FastMCP는 선택 의존성이다. 실제 HTTP caller를 만들 때만 지연 import하므로 core 변환,
검증, stub 기반 테스트는 FastMCP 설치 없이 동작한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

from .model import AuditModelError, StandardsDomain, json_sha256, require_non_empty
from .standards import (
    StandardHit,
    StandardsQueryError,
    StandardsRetrievalFatalError,
)


ADAPTER_VERSION = "0.2.0"
SERVER_NAME = "auditpaper-standards"
SEARCH_TOOL = "standards_search"
GET_TOOL = "standards_get_paragraph"
DEFINE_TOOL = "standards_define_terms"
DEFAULT_REMOTE_URL = "https://toddl-auditpaper-mcp.hf.space/mcp"

_COLLECTION_PROBE_TERM = "__excel_to_skill_collection_probe__"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def build_retriever_descriptor(
    *,
    collection: str,
    policy: "RetrievalPolicy",
    retrieved_at: str,
) -> dict:
    """Build the adapter recipe identity without opening an MCP connection."""
    return {
        "name": SERVER_NAME,
        "version": ADAPTER_VERSION,
        "mcp_server": SERVER_NAME,
        "tool": f"{SEARCH_TOOL}+{GET_TOOL}",
        "corpus_id": SERVER_NAME,
        "corpus_version": require_non_empty(collection, field="collection"),
        "config_sha256": json_sha256(policy.to_dict()),
        "retrieved_at": retrieved_at,
    }


_CID_RE = re.compile(r"^(KSA|KIFRS|GUIDE)::([^:]+)::(.+)$")
_STANDARD_NO_PATTERN = r"(?:[A-Z]{2,8}-?\d+(?:-\d+)?|\d+(?:-\d+)?)"
_STANDARD_NO_RE = re.compile(rf"^{_STANDARD_NO_PATTERN}$", re.I)
_STANDARD_PATTERNS = (
    re.compile(
        r"\b(?:KSA|K-?IFRS|KIFRS|GUIDE)\s*(?:::\s*|[:# -]+)"
        rf"({_STANDARD_NO_PATTERN})(?=$|[^A-Za-z0-9-])",
        re.I,
    ),
    re.compile(
        r"(?:감사기준서?|기업회계기준서?)\s*제?\s*"
        rf"({_STANDARD_NO_PATTERN})\s*호?(?=$|[^A-Za-z0-9-])",
        re.I,
    ),
)
_STANDARD_CONTINUATION_RE = re.compile(
    rf"\s*(?:,|/|·|및|또는|과|와)\s*제?\s*"
    rf"({_STANDARD_NO_PATTERN})\s*호?(?=$|[^A-Za-z0-9-])",
    re.I,
)
_GUIDE_REFERENCE_RE = re.compile(r"\bGUIDE\s*(?:::\s*|[:# -]+)", re.I)
_SOURCE_DOMAIN = {
    "감사기준": StandardsDomain.AUDIT,
    "회계기준": StandardsDomain.ACCOUNTING,
}
_PREFIX_SOURCE_TYPE = {
    "KSA": "감사기준",
    "KIFRS": "회계기준",
    "GUIDE": "실무지침",
}
_PARA_TYPES = {"정의", "참조", "부록", "요구사항", "적용지침", "본문"}


class MCPToolCaller(Protocol):
    """FastMCP transport와 무관한 동기 도구 호출 경계."""

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """MCP tool 결과의 애플리케이션 payload 객체를 반환한다."""
        ...


class _AuditpaperToolError(StandardsQueryError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """서버·LLM 입력을 제한하고 cache recipe에 포함할 검색 정책."""

    top_k: int = 5
    max_definitions: int = 2
    max_text_chars: int = 40_000
    max_total_chars: int = 160_000
    max_citations: int = 40
    max_run_text_chars: int = 240_000
    include_examples: bool = False
    upstream_retries: int = 2
    retry_delays: tuple[float, ...] = (0.5, 2.0)

    def __post_init__(self) -> None:
        if isinstance(self.top_k, bool) or not 1 <= self.top_k <= 8:
            raise AuditModelError("top_k는 1 이상 8 이하 정수여야 합니다.")
        if isinstance(self.max_definitions, bool) or not 0 <= self.max_definitions <= 5:
            raise AuditModelError("max_definitions는 0 이상 5 이하 정수여야 합니다.")
        if self.max_text_chars < 1 or self.max_total_chars < self.max_text_chars:
            raise AuditModelError(
                "max_text_chars는 양수이고 max_total_chars 이하여야 합니다."
            )
        if isinstance(self.max_citations, bool) or not 1 <= self.max_citations <= 100:
            raise AuditModelError("max_citations는 1 이상 100 이하 정수여야 합니다.")
        if self.max_run_text_chars < self.max_total_chars:
            raise AuditModelError(
                "max_run_text_chars는 query별 max_total_chars 이상이어야 합니다."
            )
        if isinstance(self.upstream_retries, bool) or not 0 <= self.upstream_retries <= 5:
            raise AuditModelError("upstream_retries는 0 이상 5 이하 정수여야 합니다.")
        if any(delay < 0 for delay in self.retry_delays):
            raise AuditModelError("retry_delays는 음수일 수 없습니다.")

    def to_dict(self) -> dict:
        return {
            "top_k": self.top_k,
            "max_definitions": self.max_definitions,
            "max_text_chars": self.max_text_chars,
            "max_total_chars": self.max_total_chars,
            "max_citations": self.max_citations,
            "max_run_text_chars": self.max_run_text_chars,
            "include_examples": self.include_examples,
            "upstream_retries": self.upstream_retries,
            "retry_delays": list(self.retry_delays),
            "verify_tool": GET_TOOL,
            "verify_context": 0,
        }


@dataclass(frozen=True, slots=True)
class MCPConnection:
    server_name: str
    url: str
    headers: dict[str, str] = field(repr=False)


class FastMCPHTTPCaller:
    """하나의 FastMCP HTTP 세션을 전용 event-loop thread에서 유지한다."""

    def __init__(
        self,
        connection: MCPConnection,
        *,
        init_timeout: float = 180.0,
        call_timeout: float = 360.0,
    ) -> None:
        self.connection = connection
        self.init_timeout = float(init_timeout)
        self.call_timeout = float(call_timeout)
        if self.init_timeout <= 0 or self.call_timeout <= 0:
            raise AuditModelError("MCP timeout은 양수여야 합니다.")
        _validate_http_url(connection.url)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._stop_event = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._startup_error: BaseException | None = None
        self._closed = False

    def __enter__(self) -> FastMCPHTTPCaller:
        self._ensure_started()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._closed:
                raise StandardsRetrievalFatalError("MCP caller가 이미 종료되었습니다.")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="excel-to-skill-auditpaper-mcp",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=self.init_timeout + 5):
            raise StandardsRetrievalFatalError("MCP initialize timeout")
        if self._startup_error is not None:
            message = _safe_exception_message(self._startup_error)
            raise StandardsRetrievalFatalError(f"MCP 연결 실패: {message}") from self._startup_error
        if self._loop is None or self._client is None:
            raise StandardsRetrievalFatalError("MCP client session이 준비되지 않았습니다.")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._session_lifetime())
        except BaseException as e:  # 전용 thread의 실패를 동기 호출자에게 전달
            self._startup_error = e
            self._ready.set()
        finally:
            self._client = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException:
                pass
            loop.close()

    async def _session_lifetime(self) -> None:
        try:
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
        except ImportError as e:
            raise RuntimeError(
                "FastMCP 미설치 — `uv sync --extra prepare`를 실행하세요."
            ) from e

        transport = StreamableHttpTransport(
            self.connection.url,
            headers=dict(self.connection.headers),
        )
        self._stop_event = asyncio.Event()
        async with Client(
            transport,
            name="excel-to-skill",
            init_timeout=self.init_timeout,
            timeout=self.call_timeout,
        ) as client:
            self._client = client
            self._ready.set()
            await self._stop_event.wait()

    async def _call_async(self, name: str, arguments: dict[str, object]):
        if self._client is None:
            raise RuntimeError("MCP client session이 닫혔습니다.")
        return await self._client.call_tool(
            name,
            arguments,
            timeout=self.call_timeout,
        )

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        self._ensure_started()
        assert self._loop is not None
        with self._call_lock:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._call_async(name, dict(arguments)), self._loop
                )
            except BaseException as e:
                raise StandardsRetrievalFatalError(
                    f"MCP session 사용 실패({name}): {_safe_exception_message(e)}"
                ) from e
            try:
                result = future.result(timeout=self.call_timeout + 5)
            except FutureTimeoutError as e:
                future.cancel()
                raise StandardsRetrievalFatalError(
                    f"MCP tool timeout: {name}"
                ) from e
            except StandardsRetrievalFatalError:
                raise
            except BaseException as e:
                raise StandardsRetrievalFatalError(
                    f"MCP tool 호출 실패({name}): {_safe_exception_message(e)}"
                ) from e
        return _tool_result_payload(result, tool=name)

    def close(self) -> None:
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            loop, stop_event, thread = self._loop, self._stop_event, self._thread
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)


class AuditpaperStandardsRetriever:
    """검색 결과를 직조회 원문으로 검증하는 auditpaper MCP adapter."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        policy: RetrievalPolicy | None = None,
        expected_collection: str | None = None,
        paragraph_cache_dir: Path | str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.caller = caller
        self.policy = policy or RetrievalPolicy()
        self._collection = (
            require_non_empty(expected_collection, field="expected_collection")
            if expected_collection is not None
            else None
        )
        self._sleeper = sleeper
        self._paragraph_cache_dir = (
            Path(paragraph_cache_dir) if paragraph_cache_dir is not None else None
        )
        self._paragraph_cache: dict[tuple[str, str], dict] = {}
        self._run_cids: set[str] = set()
        self._run_text_chars = 0

    @property
    def collection(self) -> str | None:
        return self._collection

    def discover_collection(self) -> str:
        """인코더를 기다리지 않는 용어 조회로 현재 동결 snapshot ID를 확정한다."""
        payload = self._call_with_retry(
            DEFINE_TOOL,
            {"terms": [_COLLECTION_PROBE_TERM]},
        )
        collection = _contract_text(payload.get("collection"), field="collection")
        self._pin_collection(collection)
        return collection

    def descriptor(self, *, retrieved_at: str) -> dict:
        collection = self._collection or self.discover_collection()
        return build_retriever_descriptor(
            collection=collection,
            policy=self.policy,
            retrieved_at=retrieved_at,
        )

    def search(
        self,
        query: str,
        *,
        domain: StandardsDomain | str,
        framework: str | None,
        effective_date: str | None = None,
        standard_nos: list[str] | None = None,
    ) -> list[StandardHit]:
        try:
            query_domain = StandardsDomain(domain)
        except (TypeError, ValueError) as e:
            raise StandardsQueryError(f"지원하지 않는 standards domain: {domain!r}") from e
        try:
            query_text = require_non_empty(query, field="query")
        except AuditModelError as e:
            raise StandardsQueryError(str(e)) from e
        if len(query_text) > 500:
            raise StandardsQueryError("auditpaper standards_search query는 500자 이하여야 합니다.")
        if _GUIDE_REFERENCE_RE.search(query_text):
            raise StandardsQueryError(
                "실무지침(GUIDE)은 현재 provenance 유형으로 안전하게 표현할 수 없어 "
                "prepare에서 지원하지 않습니다."
            )
        source_types = _source_types(query_domain, framework)
        if self._collection is None:
            self.discover_collection()

        arguments: dict[str, object] = {
            "query": query_text,
            "source_type": source_types,
            "top_k": self.policy.top_k,
            "include_examples": self.policy.include_examples,
        }
        resolved_standard_nos = (
            _normalize_standard_numbers(standard_nos)
            if standard_nos is not None
            else _explicit_standard_numbers(query_text)
        )
        if resolved_standard_nos:
            arguments["standard_no"] = resolved_standard_nos

        payload = self._call_with_retry(SEARCH_TOOL, arguments)
        _validate_applied_filters(payload, arguments)
        results = _object_list(payload.get("results"), field="standards_search.results")
        definitions = _object_list(
            payload.get("definitions", []), field="standards_search.definitions"
        )

        candidates: list[tuple[dict, str]] = [
            (item, "result") for item in results[: self.policy.top_k]
        ]
        candidates.extend(
            (item, "definition")
            for item in definitions[: self.policy.max_definitions]
        )
        hits: list[StandardHit] = []
        seen_cids: set[str] = set()
        total_chars = 0
        pending_new_cids: set[str] = set()
        pending_new_chars = 0
        for candidate, role in candidates:
            if role == "result" and _requires_original_paragraph(candidate):
                # 서버가 발췌 대조표/참조 문단이라고 경고한 후보는 원전 CID가 아니므로
                # authoritative citation으로 승격하지 않는다.
                continue
            cid_field = "cid" if role == "result" else "source_cid"
            cid = _contract_text(candidate.get(cid_field), field=cid_field)
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            hit = self._verified_hit(
                candidate,
                cid=cid,
                role=role,
                domain=query_domain,
                framework=framework,
                effective_date=effective_date,
                allowed_standard_nos=(
                    set(resolved_standard_nos) if role == "result" else set()
                ),
            )
            if hit is None:
                # 서버가 자동 주입한 정의는 query domain 밖의 기준서일 수 있다.
                # 잘못된 provenance로 승격하지 않고 검증 후 제외한다.
                continue
            snippet_chars = len(hit.snippet)
            if snippet_chars > self.policy.max_text_chars:
                raise StandardsQueryError(
                    f"검증 문단이 단건 text 상한을 초과합니다: {cid} ({snippet_chars}자)",
                    limitation_code="retrieval_capped",
                )
            total_chars += snippet_chars
            if total_chars > self.policy.max_total_chars:
                raise StandardsQueryError(
                    "검증 기준서 원문 합계가 retrieval 상한을 초과합니다: "
                    f"{total_chars}자",
                    limitation_code="retrieval_capped",
                )
            if cid not in self._run_cids:
                pending_new_cids.add(cid)
                pending_new_chars += snippet_chars
                if len(self._run_cids | pending_new_cids) > self.policy.max_citations:
                    raise StandardsQueryError(
                        "prepare 전체 unique citation 상한을 초과했습니다.",
                        limitation_code="retrieval_capped",
                    )
                if (
                    self._run_text_chars + pending_new_chars
                    > self.policy.max_run_text_chars
                ):
                    raise StandardsQueryError(
                        "prepare 전체 standards 원문 예산을 초과했습니다.",
                        limitation_code="retrieval_capped",
                    )
            hits.append(hit)
        self._run_cids.update(pending_new_cids)
        self._run_text_chars += pending_new_chars
        return hits

    def _verified_hit(
        self,
        candidate: Mapping[str, object],
        *,
        cid: str,
        role: str,
        domain: StandardsDomain,
        framework: str | None,
        effective_date: str | None,
        allowed_standard_nos: set[str],
    ) -> StandardHit | None:
        paragraph = self._get_verified_paragraph(cid)
        match = _CID_RE.fullmatch(cid)
        if match is None:
            raise StandardsRetrievalFatalError(f"auditpaper CID 형식 불일치: {cid!r}")
        prefix, cid_standard_no, cid_para_no = match.groups()
        source_type = _contract_text(paragraph.get("source_type"), field="source_type")
        expected_source_type = _PREFIX_SOURCE_TYPE[prefix]
        if source_type != expected_source_type:
            raise StandardsRetrievalFatalError(
                f"CID prefix/source_type 불일치: {cid!r} / {source_type!r}"
            )

        for candidate_field, paragraph_field in (
            ("standard_no", "standard_no"),
            ("standard_title", "standard_title"),
            ("para_no", "para_no"),
        ):
            expected = candidate.get(candidate_field)
            actual = paragraph.get(paragraph_field)
            if expected is not None and str(expected) != str(actual):
                raise StandardsRetrievalFatalError(
                    f"search/get metadata 불일치({cid}, {candidate_field})"
                )
        if str(paragraph.get("standard_no")) != cid_standard_no:
            raise StandardsRetrievalFatalError(
                f"CID/paragraph standard_no 불일치: {cid!r}"
            )
        if str(paragraph.get("para_no")) != cid_para_no:
            raise StandardsRetrievalFatalError(
                f"CID/paragraph para_no 불일치: {cid!r}"
            )
        if allowed_standard_nos and cid_standard_no not in allowed_standard_nos:
            raise StandardsRetrievalFatalError(
                f"요청 standard_no 밖의 검색 결과: {cid!r}"
            )
        search_text = _contract_text(candidate.get("text"), field=f"{role}.text")
        paragraph_text = _contract_text(paragraph.get("text"), field="paragraph.text")
        if role == "result" and search_text != paragraph_text:
            raise StandardsRetrievalFatalError(f"search/get 원문 불일치: {cid!r}")
        if (
            role == "definition"
            and re.sub(r"\s+", "", search_text)
            not in re.sub(r"\s+", "", paragraph_text)
        ):
            raise StandardsRetrievalFatalError(
                f"definition excerpt가 직조회 원문에 포함되지 않습니다: {cid!r}"
            )

        if source_type == "실무지침":
            if role == "definition":
                return None
            raise StandardsRetrievalFatalError(
                f"지원하지 않는 실무지침 검색 결과: {cid!r}"
            )
        if _SOURCE_DOMAIN[source_type] is not domain:
            if role == "definition":
                return None
            raise StandardsRetrievalFatalError(
                f"query domain/source_type 불일치: {domain.value!r} / {source_type!r}"
            )
        para_type = _contract_text(paragraph.get("para_type"), field="paragraph.para_type")
        if role == "result" and not self.policy.include_examples and para_type == "부록":
            raise StandardsRetrievalFatalError(
                f"include_examples=false인데 부록 결과가 반환되었습니다: {cid!r}"
            )
        resolved_framework = _framework_for_hit(prefix, framework)

        score = _relative_score(candidate.get("score")) if role == "result" else None
        return StandardHit(
            domain=domain,
            framework=resolved_framework,
            document_id=cid,
            paragraph=_contract_text(paragraph.get("para_no"), field="paragraph.para_no"),
            title=_contract_text(
                paragraph.get("standard_title"), field="paragraph.standard_title"
            ),
            snippet=paragraph_text,
            score=score,
            edition=None,
            # 서버는 시행일 구조화 필드를 제공하지 않는다. query 적용일을 복사하지 않는다.
            effective_date=None,
            source_uri=(
                f"auditpaper://{quote(self._collection or '', safe='')}/"
                f"{quote(cid, safe='')}"
            ),
            citation_id=f"standard:{json_sha256({'collection': self._collection, 'cid': cid})[:20]}",
            corpus_id=SERVER_NAME,
            corpus_version=self._collection,
            retriever_version=ADAPTER_VERSION,
            metadata={
                "source_cid": cid,
                "source_type": source_type,
                "standard_no": str(paragraph.get("standard_no")),
                "para_type": para_type,
                "section_path": paragraph.get("section_path"),
                "seq": paragraph.get("seq"),
                "verified_by": GET_TOOL,
                "retrieval_role": role,
                "search_text_sha256": hashlib.sha256(
                    search_text.encode("utf-8")
                ).hexdigest(),
                "paragraph_text_sha256": hashlib.sha256(
                    paragraph_text.encode("utf-8")
                ).hexdigest(),
            },
        )

    def _get_verified_paragraph(self, cid: str) -> dict:
        collection = self._collection
        if collection is None:
            raise StandardsRetrievalFatalError("collection이 확정되지 않았습니다.")
        cache_key = (collection, cid)
        cached = self._paragraph_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        persistent = self._read_persistent_paragraph(collection, cid)
        if persistent is not None:
            try:
                _validate_paragraph_contract(cid, persistent)
            except StandardsRetrievalFatalError:
                self._discard_persistent_paragraph(collection, cid)
            else:
                self._paragraph_cache[cache_key] = persistent
                return dict(persistent)
        try:
            payload = self._call_with_retry(GET_TOOL, {"cid": cid, "context": 0})
        except _AuditpaperToolError as e:
            if e.code in {"NOT_FOUND", "INVALID_INPUT"}:
                raise StandardsRetrievalFatalError(
                    f"검색 CID 직조회 계약 오류: {cid!r}: {e}"
                ) from e
            raise
        if payload.get("found") is not True:
            raise StandardsRetrievalFatalError(f"검색 CID 직조회 실패: {cid!r}")
        paragraphs = _object_list(
            payload.get("paragraphs"), field="standards_get_paragraph.paragraphs"
        )
        targets = [
            item
            for item in paragraphs
            if item.get("cid") == cid and item.get("is_context") is False
        ]
        if len(targets) != 1:
            raise StandardsRetrievalFatalError(
                f"직조회 target 문단은 정확히 1건이어야 합니다: {cid!r} ({len(targets)}건)"
            )
        target = dict(targets[0])
        # Only a fully verified canonical paragraph may cross the persistent-cache boundary.
        _validate_paragraph_contract(cid, target)
        self._paragraph_cache[cache_key] = target
        self._write_persistent_paragraph(collection, cid, target)
        return dict(target)

    def get_verified_paragraph(self, cid: str) -> dict:
        """Return one exact, collection-pinned paragraph for dynamic research.

        The same strict ``standards_get_paragraph(context=0)`` contract and content-addressed
        collection+CID cache used by ``search`` applies here.  Exposing this small public boundary
        lets application code resolve a child model's opaque candidate selection without trusting
        any paragraph text authored or copied by that model.
        """
        if not isinstance(cid, str) or _CID_RE.fullmatch(cid) is None:
            raise StandardsRetrievalFatalError(
                f"auditpaper CID 형식 불일치: {cid!r}"
            )
        if self._collection is None:
            self.discover_collection()
        return self._get_verified_paragraph(cid)

    def _paragraph_cache_path(self, collection: str, cid: str) -> Path | None:
        if self._paragraph_cache_dir is None:
            return None
        collection_key = hashlib.sha256(collection.encode("utf-8")).hexdigest()
        cid_key = hashlib.sha256(cid.encode("utf-8")).hexdigest()
        return self._paragraph_cache_dir / collection_key / f"{cid_key}.json"

    def _read_persistent_paragraph(self, collection: str, cid: str) -> dict | None:
        path = self._paragraph_cache_path(collection, cid)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, Mapping)
            or payload.get("collection") != collection
            or payload.get("cid") != cid
            or not isinstance(payload.get("paragraph"), Mapping)
        ):
            return None
        paragraph = dict(payload["paragraph"])
        if paragraph.get("cid") != cid:
            return None
        try:
            digest = json_sha256(paragraph)
        except AuditModelError:
            return None
        if payload.get("paragraph_sha256") != digest:
            return None
        return paragraph

    def _discard_persistent_paragraph(self, collection: str, cid: str) -> None:
        path = self._paragraph_cache_path(collection, cid)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_persistent_paragraph(
        self, collection: str, cid: str, paragraph: Mapping[str, object]
    ) -> None:
        path = self._paragraph_cache_path(collection, cid)
        if path is None:
            return
        try:
            payload = {
                "collection": collection,
                "cid": cid,
                "paragraph_sha256": json_sha256(dict(paragraph)),
                "paragraph": dict(paragraph),
            }
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(encoded)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, AuditModelError):
            # 원문 조회 성공이 cache 저장 실패 때문에 query 실패로 바뀌어서는 안 된다.
            return

    def _call_with_retry(self, tool: str, arguments: Mapping[str, object]) -> dict:
        attempts = self.policy.upstream_retries + 1
        for attempt in range(attempts):
            try:
                raw = self.caller.call_tool(tool, arguments)
            except StandardsRetrievalFatalError:
                raise
            except Exception as e:
                raise StandardsRetrievalFatalError(
                    f"MCP transport/프로토콜 오류({tool}): {_safe_exception_message(e)}"
                ) from e
            if not isinstance(raw, Mapping):
                raise StandardsRetrievalFatalError(f"MCP payload는 객체여야 합니다: {tool}")
            payload = dict(raw)
            collection = _contract_text(payload.get("collection"), field="collection")
            self._pin_collection(collection)
            error = payload.get("error")
            if error is None:
                return payload
            if not isinstance(error, Mapping):
                raise StandardsRetrievalFatalError(f"MCP error 봉투가 객체가 아닙니다: {tool}")
            code = _contract_text(error.get("code"), field="error.code")
            message = _contract_text(error.get("message"), field="error.message")
            hint = error.get("hint")
            detail = f"{code}: {message}"
            if isinstance(hint, str) and hint.strip():
                detail += f" ({hint.strip()})"
            if code == "UPSTREAM_UNAVAILABLE" and attempt + 1 < attempts:
                delay = (
                    self.policy.retry_delays[attempt]
                    if attempt < len(self.policy.retry_delays)
                    else 0.0
                )
                self._sleeper(delay)
                continue
            if code in {"UPSTREAM_UNAVAILABLE", "INVALID_INPUT", "NOT_FOUND"}:
                raise _AuditpaperToolError(code, detail)
            raise StandardsRetrievalFatalError(detail)
        raise AssertionError("unreachable")

    def _pin_collection(self, collection: str) -> None:
        if self._collection is None:
            self._collection = collection
        elif collection != self._collection:
            raise StandardsRetrievalFatalError(
                f"MCP collection drift: {self._collection!r} -> {collection!r}"
            )


def load_mcp_connection(
    *,
    config_path: Path | str | None = None,
    server_name: str = SERVER_NAME,
    url: str | None = None,
    token_env: str = "MCP_AUTH_TOKEN",
    environ: Mapping[str, str] | None = None,
) -> MCPConnection:
    """CLI/env/`.mcp.json`에서 URL·헤더를 읽되 token을 인자나 산출물에 남기지 않는다."""
    env = os.environ if environ is None else environ
    token = env.get(token_env)
    headers: dict[str, str] = {}
    configured_url: str | None = None
    if config_path is not None:
        path = Path(config_path)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise AuditModelError(f"MCP config 파일이 없습니다: {path}") from e
        except json.JSONDecodeError as e:
            raise AuditModelError(f"MCP config JSON 파싱 실패: {path}: {e}") from e
        servers = config.get("mcpServers") if isinstance(config, Mapping) else None
        server = servers.get(server_name) if isinstance(servers, Mapping) else None
        if not isinstance(server, Mapping):
            raise AuditModelError(f"MCP config에 서버가 없습니다: {server_name!r}")
        configured_url = _expand_env(server.get("url"), env, field="MCP url")
        raw_headers = server.get("headers", {})
        if not isinstance(raw_headers, Mapping):
            raise AuditModelError("MCP config headers는 객체여야 합니다.")
        for key, value in raw_headers.items():
            if not isinstance(key, str):
                raise AuditModelError("MCP header 이름은 문자열이어야 합니다.")
            if token and key.casefold() == "authorization":
                # 환경변수 token이 우선이면 오래된 config secret/placeholder를 읽지 않는다.
                continue
            headers[key] = _expand_env(value, env, field=f"MCP header {key}")

    resolved_url = (
        url or env.get("AUDITPAPER_MCP_URL") or configured_url or DEFAULT_REMOTE_URL
    )
    if token:
        headers = {
            key: value
            for key, value in headers.items()
            if key.casefold() != "authorization"
        }
        headers["Authorization"] = f"Bearer {token}"
    authorization_keys = [
        key for key in headers if key.casefold() == "authorization"
    ]
    if not authorization_keys:
        raise AuditModelError(
            f"MCP Bearer token 없음 — 환경변수 {token_env} 또는 .mcp.json header가 필요합니다."
        )
    if len(authorization_keys) > 1:
        raise AuditModelError(
            "MCP config에 대소문자만 다른 Authorization header가 중복되었습니다."
        )
    _validate_http_url(resolved_url)
    return MCPConnection(server_name=server_name, url=resolved_url, headers=headers)


def _validate_paragraph_contract(cid: str, paragraph: Mapping[str, object]) -> None:
    match = _CID_RE.fullmatch(cid)
    if match is None:
        raise StandardsRetrievalFatalError(f"auditpaper CID 형식 불일치: {cid!r}")
    prefix, cid_standard_no, cid_para_no = match.groups()
    if paragraph.get("cid") != cid or paragraph.get("is_context") is not False:
        raise StandardsRetrievalFatalError(
            f"직조회 target identity 불일치: {cid!r}"
        )
    source_type = _contract_text(paragraph.get("source_type"), field="source_type")
    if source_type != _PREFIX_SOURCE_TYPE[prefix]:
        raise StandardsRetrievalFatalError(
            f"CID prefix/source_type 불일치: {cid!r} / {source_type!r}"
        )
    if _contract_text(paragraph.get("standard_no"), field="paragraph.standard_no") != (
        cid_standard_no
    ):
        raise StandardsRetrievalFatalError(
            f"CID/paragraph standard_no 불일치: {cid!r}"
        )
    if _contract_text(paragraph.get("para_no"), field="paragraph.para_no") != cid_para_no:
        raise StandardsRetrievalFatalError(
            f"CID/paragraph para_no 불일치: {cid!r}"
        )
    _contract_text(paragraph.get("standard_title"), field="paragraph.standard_title")
    _contract_text(paragraph.get("text"), field="paragraph.text")
    para_type = _contract_text(paragraph.get("para_type"), field="paragraph.para_type")
    if para_type not in _PARA_TYPES:
        raise StandardsRetrievalFatalError(
            f"지원하지 않는 paragraph para_type: {para_type!r}"
        )
    seq = paragraph.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise StandardsRetrievalFatalError("paragraph.seq는 정수여야 합니다.")


def _source_types(domain: StandardsDomain, framework: str | None) -> list[str]:
    if framework is None:
        return ["감사기준"] if domain is StandardsDomain.AUDIT else ["회계기준"]
    normalized = re.sub(r"[^A-Z가-힣]", "", framework.upper())
    if normalized in {"KSA", "감사기준", "감사기준서"}:
        if domain is not StandardsDomain.AUDIT:
            raise StandardsQueryError("accounting query에 KSA framework를 사용할 수 없습니다.")
        return ["감사기준"]
    if normalized in {"KIFRS", "회계기준", "기업회계기준", "기업회계기준서"}:
        if domain is not StandardsDomain.ACCOUNTING:
            raise StandardsQueryError("audit query에 K-IFRS framework를 사용할 수 없습니다.")
        return ["회계기준"]
    if normalized in {"GUIDE", "실무지침"}:
        raise StandardsQueryError(
            "실무지침(GUIDE)은 현재 provenance 유형으로 안전하게 표현할 수 없어 "
            "prepare에서 지원하지 않습니다."
        )
    raise StandardsQueryError(f"auditpaper MCP가 지원하지 않는 framework: {framework!r}")


def _framework_for_hit(prefix: str, requested: str | None) -> str:
    inferred = {"KSA": "KSA", "KIFRS": "K-IFRS", "GUIDE": "GUIDE"}[prefix]
    if requested is None:
        return inferred
    normalized = re.sub(r"[^A-Z가-힣]", "", requested.upper())
    compatible = {
        "KSA": {"KSA", "감사기준", "감사기준서"},
        "KIFRS": {"KIFRS", "회계기준", "기업회계기준", "기업회계기준서"},
        "GUIDE": {"GUIDE", "실무지침"},
    }[prefix]
    if normalized not in compatible:
        raise StandardsRetrievalFatalError(
            f"query framework/CID prefix 불일치: {requested!r} / {prefix!r}"
        )
    # context 계약은 query에 기록된 framework 문자열과 hit가 정확히 같아야 한다.
    return requested


def _explicit_standard_numbers(query: str) -> list[str]:
    found: set[str] = set()
    for pattern in _STANDARD_PATTERNS:
        for match in pattern.finditer(query):
            if _looks_like_year_context(query, match):
                continue
            found.add(match.group(1).upper())
            cursor = match.end()
            while continuation := _STANDARD_CONTINUATION_RE.match(query, cursor):
                if _looks_like_year_context(query, continuation):
                    break
                found.add(continuation.group(1).upper())
                cursor = continuation.end()
    return sorted(found)


def _looks_like_year_context(query: str, match: re.Match[str]) -> bool:
    token = match.group(1)
    if not re.fullmatch(r"(?:19|20)\d{2}", token):
        return False
    suffix = query[match.end(1):].lstrip()
    if suffix.startswith("호"):
        return False
    return suffix.startswith(("년", "개정", "적용"))


def _normalize_standard_numbers(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > 20:
        raise StandardsQueryError(
            "standard_nos는 1~20개의 기준서 번호 배열이어야 합니다."
        )
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _STANDARD_NO_RE.fullmatch(value.strip()):
            raise StandardsQueryError(f"지원하지 않는 기준서 번호 형식: {value!r}")
        normalized.append(value.strip().upper())
    if len(normalized) != len(set(normalized)):
        raise StandardsQueryError("standard_nos에 중복 기준서 번호가 있습니다.")
    return sorted(normalized)


def _validate_applied_filters(
    payload: Mapping[str, object], arguments: Mapping[str, object]
) -> None:
    """서버가 회신한 실제 필터가 요청과 같은지 확인한다."""
    requested = {
        field: arguments[field]
        for field in ("standard_no", "source_type", "para_type")
        if field in arguments
    }
    if not requested:
        return
    applied = payload.get("applied")
    filters = applied.get("filters") if isinstance(applied, Mapping) else None
    if not isinstance(filters, Mapping):
        raise StandardsRetrievalFatalError(
            "standards_search.applied.filters가 없어 요청 필터 적용을 검증할 수 없습니다."
        )
    if dict(filters) != requested:
        raise StandardsRetrievalFatalError(
            "standards_search applied filters가 요청 filters와 일치하지 않습니다."
        )


def _relative_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if 0.0 <= score <= 1.0 else None


def _requires_original_paragraph(candidate: Mapping[str, object]) -> bool:
    if candidate.get("para_type") == "참조":
        return True
    notes = candidate.get("notes")
    return isinstance(notes, list) and any(
        isinstance(note, str) and "원전 문단 우선 인용" in note
        for note in notes
    )


def _object_list(value: object, *, field: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise StandardsRetrievalFatalError(f"{field}는 객체 배열이어야 합니다.")
    return [dict(item) for item in value]


def _contract_text(value: object, *, field: str) -> str:
    try:
        return require_non_empty(value, field=field)
    except AuditModelError as e:
        raise StandardsRetrievalFatalError(f"MCP 응답 계약 불일치: {e}") from e


def _tool_result_payload(result, *, tool: str) -> Mapping[str, object]:
    if getattr(result, "is_error", False):
        raise StandardsRetrievalFatalError(f"MCP protocol tool error: {tool}")
    data = getattr(result, "data", None)
    if isinstance(data, Mapping):
        return dict(data)
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, Mapping):
        if isinstance(structured.get("result"), Mapping):
            return dict(structured["result"])
        return dict(structured)
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            return dict(payload)
    raise StandardsRetrievalFatalError(f"MCP tool 결과가 JSON 객체가 아닙니다: {tool}")


def _expand_env(value: object, env: Mapping[str, str], *, field: str) -> str:
    text = require_non_empty(value, field=field)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = env.get(name)
        if resolved is None:
            raise AuditModelError(f"{field} 환경변수 미설정: {name}")
        return resolved

    return _ENV_RE.sub(replace, text)


def _validate_http_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AuditModelError(f"유효한 MCP HTTP URL이 아닙니다: {value!r}")
    if parsed.username or parsed.password:
        raise AuditModelError("MCP URL에 자격증명을 넣지 마세요. Bearer header를 사용하세요.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise AuditModelError("원격 MCP Bearer token 전송에는 HTTPS가 필요합니다.")


def _safe_exception_message(error: BaseException) -> str:
    detail = str(error).strip() or type(error).__name__
    # Authorization header 값이 예외에 포함되는 비정상 client를 방어한다.
    detail = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", detail)
    return detail[:1000]


__all__ = [
    "ADAPTER_VERSION",
    "AuditpaperStandardsRetriever",
    "DEFAULT_REMOTE_URL",
    "FastMCPHTTPCaller",
    "MCPConnection",
    "MCPToolCaller",
    "RetrievalPolicy",
    "load_mcp_connection",
]
