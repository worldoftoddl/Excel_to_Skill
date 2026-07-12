from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from excel_to_skill import cache
from excel_to_skill.audit.prepare import AuditPrepareError, prepare_package
from excel_to_skill.audit.consume import audit_get, audit_search, brief, trace
from excel_to_skill.audit.standards import StandardHit
from excel_to_skill.audit.validate import validate_audit_package
from excel_to_skill.cli import _convert_one, main
from excel_to_skill.meta import _converter_version
from excel_to_skill.verify import verify_package
from excel_to_skill.consume import ConsumeError, overview


FIXTURES = Path(__file__).parent / "fixtures"
_WHEN = "2026-07-11T00:00:00Z"
_DESCRIPTOR = {
    "name": "stub-standards",
    "version": "1",
    "mcp_server": "stub-server",
    "tool": "search",
    "corpus_id": "stub-corpus",
    "corpus_version": "2026.1",
    "retrieved_at": _WHEN,
}


def _package(tmp_path: Path) -> Path:
    return _convert_one(
        FIXTURES / "fx1_merge_formula.xlsx",
        tmp_path / "converted",
        force=True,
        cv=_converter_version(),
    )


class StubRetriever:
    def __init__(self, *, corpus_version: str = "2026.1") -> None:
        self.calls: list[dict] = []
        self.hit = StandardHit(
            domain="audit",
            framework="KSA",
            document_id="KSA-315",
            paragraph="26",
            title="위험의 식별과 평가",
            snippet="감사인은 관련 위험을 식별하고 평가한다.",
            score=0.9,
            edition="2026",
            effective_date="2026-01-01",
            source_uri="standards://ksa/315/26",
            corpus_id="stub-corpus",
            corpus_version=corpus_version,
            retriever_version="1",
            retrieved_at=_WHEN,
        )

    def search(
        self, query, *, domain, framework, effective_date=None, standard_nos=None
    ):
        self.calls.append({
            "query": query,
            "domain": domain,
            "framework": framework,
            "effective_date": effective_date,
            "standard_nos": standard_nos,
        })
        return [self.hit]


class FailingRetriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(
        self, query, *, domain, framework, effective_date=None, standard_nos=None
    ):
        self.calls.append({
            "query": query,
            "domain": domain,
            "framework": framework,
            "effective_date": effective_date,
            "standard_nos": standard_nos,
        })
        raise RuntimeError("standards service unavailable")


class PipelineClient:
    def __init__(self, *, fail_brief: bool = False) -> None:
        self.calls: list[str] = []
        self.fact_id: str | None = None
        self.fail_brief = fail_brief

    def __call__(self, *, system: str, user: str, schema: dict):
        properties = schema.get("properties", {})
        if "region_id" in properties:
            payload = json.loads(user)
            self.calls.append("region")
            return {
                "region_id": payload["region_id"],
                "facts": [{
                    "local_id": "purpose",
                    "type": "workpaper_attribute",
                    "description": "매출 요약 구조가 문서화되어 있다.",
                    "status": "documented",
                    "normalized_code": "workpaper_purpose",
                    "value": None,
                    "unit": None,
                    "severity": None,
                    "confidence": 0.9,
                    "sources": [{"ref": "Data!A1", "role": "label"}],
                }],
                "limitations": [],
            }
        if "workpaper" in properties and "standard_queries" in properties:
            payload = json.loads(user)
            self.calls.append("consolidate")
            self.fact_id = payload["facts"][0]["id"]
            return {
                "workpaper": {
                    "kind": "supporting_schedule",
                    "title": "매출 요약",
                    "entity": None,
                    "period_start": None,
                    "period_end": None,
                    "audit_phase": "unknown",
                    "document_state": "partially_completed",
                    "purpose": "매출 데이터를 요약한다.",
                    "source_ids": [payload["sources"][0]["id"]],
                },
                "relations": [],
                "standard_queries": [{
                    "id": "query:purpose",
                    "query": "감사인의 위험 식별 및 평가 요구사항",
                    "domain": "audit",
                    "framework": "KSA",
                    "effective_date": "2026-01-01",
                    "fact_ids": [self.fact_id],
                    "rationale": "조서 목적의 감사기준 맥락 확인",
                }],
            }
        if "readiness" in properties:
            self.calls.append("brief")
            if self.fail_brief:
                raise RuntimeError("brief unavailable")
            if self.fact_id is None:
                self.fact_id = self._fact_id_from_brief_user(user)
            citation_ids = self._citation_ids_from_brief_user(user)
            statements = [{
                "id": "statement:fact",
                "section": "identity_scope",
                "type": "documented_fact",
                "text": "워크북에 매출 요약 구조가 문서화되어 있다.",
                "status": "documented",
                "confidence": 0.9,
                "fact_ids": [self.fact_id],
                "relation_ids": [],
                "standard_citation_ids": [],
            }]
            statement_ids = ["statement:fact"]
            if citation_ids:
                statements.append({
                    "id": "statement:standard",
                    "section": "standards",
                    "type": "authoritative_context",
                    "text": "감사기준은 관련 위험의 식별과 평가를 요구한다.",
                    "status": "documented",
                    "confidence": 0.9,
                    "fact_ids": [],
                    "relation_ids": [],
                    "standard_citation_ids": [citation_ids[0]],
                })
                statement_ids.append("statement:standard")
            return {
                "readiness": {
                    "status": "ready" if citation_ids else "partial",
                    "reasons": [
                        "workbook 사실과 기준서 문맥 연결 완료"
                        if citation_ids else "기준서 조회가 완료되지 않음"
                    ],
                    "open_item_fact_ids": [],
                },
                "workpaper": {
                    "kind": "supporting_schedule",
                    "title": "매출 요약",
                    "entity": None,
                    "period_start": None,
                    "period_end": None,
                    "audit_phase": "unknown",
                    "document_state": "partially_completed",
                    "purpose": "매출 데이터를 요약한다.",
                    "fact_ids": [self.fact_id],
                },
                "summary": {
                    "text": (
                        "매출 요약 구조와 관련 위험평가 문맥이 준비되었다."
                        if citation_ids else "매출 요약 구조만 확인되었다."
                    ),
                    "statement_ids": statement_ids,
                },
                "statements": statements,
                "limitations": [],
            }
        raise AssertionError(f"unexpected schema properties: {sorted(properties)}")

    @staticmethod
    def _fact_id_from_brief_user(user: str) -> str:
        marker = "# audit_facts (workbook-only)\n"
        facts_text = user.split(marker, 1)[1].split(
            "\n\n# standards_context", 1
        )[0]
        return json.loads(facts_text)["facts"][0]["id"]

    @staticmethod
    def _citation_ids_from_brief_user(user: str) -> list[str]:
        marker = "# standards_context (authoritative context only)\n"
        context_text = user.split(marker, 1)[1].split(
            "\n\nCreate the model-authored", 1
        )[0]
        return [item["id"] for item in json.loads(context_text)["citations"]]


def test_prepare_builds_and_validates_three_layer_bundle(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    client = PipelineClient()
    retriever = StubRetriever()
    result = prepare_package(
        pkg,
        client=client,
        retriever=retriever,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )

    assert result.cached is False and result.status == "ready"
    assert client.calls == ["region", "consolidate", "brief"]
    assert len(retriever.calls) == 1
    assert all(path.is_file() for path in (
        result.facts_path, result.standards_path, result.brief_path
    ))
    validate_audit_package(pkg)
    verified = verify_package(pkg)
    assert verified.ok
    assert next(check for check in verified.checks if check.name == "audit").ok
    reproducible = verify_package(pkg, FIXTURES / "fx1_merge_formula.xlsx")
    assert reproducible.ok
    assert next(check for check in reproducible.checks if check.name == "V3").ok
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    assert meta["audit_preparation"]["status"] == "ready"
    assert meta["audit_preparation"]["review_status"] == "draft"
    assert all(len(meta["audit_preparation"][key]) == 64 for key in (
        "facts_key", "standards_key", "brief_key"
    ))
    cache_state = cache.get_audit(pkg.parent, pkg.name)
    assert cache_state is not None
    assert cache_state["prepare_version"] == "0.1.0"
    assert all(len(cache_state[key]) == 64 for key in (
        "facts_recipe_key", "standards_recipe_key", "brief_recipe_key"
    ))
    skill = (pkg / "SKILL.md").read_text(encoding="utf-8")
    assert "감사조서 Brief (commit-gated)" in skill
    assert "excel-to-skill brief" in skill
    assert "매출 요약 구조와 관련 위험평가 문맥" not in skill
    assert "SKILL만으로 상태·요약을 추정하지 마십시오" in skill


def test_prepare_full_cache_hit_calls_neither_model_nor_retriever(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    (pkg / "SKILL.md").unlink()

    def exploding_factory():
        raise AssertionError("model client must not be built on a full cache hit")

    result = prepare_package(
        pkg,
        client=None,
        client_factory=exploding_factory,
        retriever=None,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
    )
    assert result.cached is True
    assert (pkg / "SKILL.md").is_file()
    assert "감사조서 Brief" in (pkg / "SKILL.md").read_text(encoding="utf-8")


def test_corrupt_non_authoritative_index_is_a_cache_miss_and_is_rebuilt(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    index_path = pkg.parent / "_index.json"
    index_path.write_bytes(b"{broken")

    result = prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert result.cached is False
    rebuilt = cache.load_index(pkg.parent)
    entry = rebuilt["entries"][pkg.name]
    assert entry["package_path"] == pkg.name
    assert entry["audit"]["prepare_version"] == "0.1.0"
    assert cache.get_audit(pkg.parent, pkg.name) is not None


def test_root_index_lock_preserves_updates_for_different_packages(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "converted"
    cache.save_index(root, {
        "index_version": 1,
        "entries": {
            "pkg-a": {"audit": {}},
            "pkg-b": {"audit": {}},
        },
    })
    first_in_save = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    errors: list[BaseException] = []
    original_save = cache._save_index_unlocked

    def delayed_save(path: Path, index: dict) -> None:
        if threading.current_thread().name == "index-first":
            first_in_save.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("timed out waiting to release first index writer")
        original_save(path, index)

    monkeypatch.setattr(cache, "_save_index_unlocked", delayed_save)

    def update(dirname: str, status: str, *, done: threading.Event | None = None) -> None:
        try:
            cache.update_audit(root, dirname, status=status)
        except BaseException as e:
            errors.append(e)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(
        target=update, args=("pkg-a", "ready"), name="index-first"
    )
    second = threading.Thread(
        target=update,
        args=("pkg-b", "partial"),
        kwargs={"done": second_done},
        name="index-second",
    )
    first.start()
    assert first_in_save.wait(timeout=5)
    second.start()
    assert not second_done.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    index = cache.load_index(root)
    assert index["entries"]["pkg-a"]["audit"]["status"] == "ready"
    assert index["entries"]["pkg-b"]["audit"]["status"] == "partial"


def test_atomic_index_replace_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "converted"
    original = {
        "index_version": 1,
        "entries": {"pkg-a": {"audit": {"status": "ready"}}},
    }
    cache.save_index(root, original)
    before = (root / "_index.json").read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected index replace failure")

    monkeypatch.setattr(cache.os, "replace", fail_replace)
    with pytest.raises(OSError, match="index replace failure"):
        cache.save_index(root, {
            "index_version": 1,
            "entries": {"pkg-b": {"audit": {"status": "partial"}}},
        })

    assert (root / "_index.json").read_bytes() == before
    assert not list(root.glob("._index.json.*.tmp"))


def test_publish_replaces_skill_before_final_meta_commit(
    tmp_path: Path, monkeypatch
) -> None:
    import excel_to_skill.audit.prepare as prepare_module

    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    events: list[str] = []
    original_replace = Path.replace
    original_set_audit = prepare_module.set_audit_preparation

    def recording_replace(self: Path, target):
        target_path = Path(target)
        if target_path.parent == pkg / "data":
            events.append(f"artifact:{target_path.name}")
        elif target_path == pkg / "SKILL.md":
            events.append("skill")
        return original_replace(self, target)

    def recording_set_audit(*args, **kwargs):
        assert "skill" in events
        events.append("meta")
        return original_set_audit(*args, **kwargs)

    monkeypatch.setattr(Path, "replace", recording_replace)
    monkeypatch.setattr(prepare_module, "set_audit_preparation", recording_set_audit)

    prepare_module.prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(corpus_version="2026.2"),
        retriever_descriptor={**_DESCRIPTOR, "corpus_version": "2026.2"},
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert events[-2:] == ["skill", "meta"]


def test_corpus_version_change_invalidates_prepare_cache(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    facts_before = (pkg / "data/audit_facts.json").read_bytes()
    client = PipelineClient()
    retriever = StubRetriever(corpus_version="2026.2")
    result = prepare_package(
        pkg,
        client=client,
        retriever=retriever,
        retriever_descriptor={**_DESCRIPTOR, "corpus_version": "2026.2"},
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )
    assert result.cached is False
    assert client.calls == ["brief"]
    assert len(retriever.calls) == 1
    assert (pkg / "data/audit_facts.json").read_bytes() == facts_before


def test_brief_version_change_reuses_facts_and_standards(
    tmp_path: Path, monkeypatch
) -> None:
    import excel_to_skill.audit.brief as brief_module
    import excel_to_skill.audit.prepare as prepare_module

    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    facts_before = (pkg / "data/audit_facts.json").read_bytes()
    context_before = (pkg / "data/standards_context.json").read_bytes()
    monkeypatch.setattr(brief_module, "BRIEF_VERSION", "0.5.0")
    monkeypatch.setattr(prepare_module, "BRIEF_VERSION", "0.5.0")
    client = PipelineClient()

    class ExplodingRetriever:
        def search(self, *args, **kwargs):
            raise AssertionError("retriever must not be called")

    result = prepare_module.prepare_package(
        pkg,
        client=client,
        retriever=ExplodingRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert result.cached is False
    assert client.calls == ["brief"]
    assert (pkg / "data/audit_facts.json").read_bytes() == facts_before
    assert (pkg / "data/standards_context.json").read_bytes() == context_before
    brief_doc = json.loads(result.brief_path.read_text(encoding="utf-8"))
    assert brief_doc["generator"]["version"] == "0.5.0"


def test_schema_stale_brief_reuses_valid_facts_and_standards(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    facts_before = (pkg / "data/audit_facts.json").read_bytes()
    context_before = (pkg / "data/standards_context.json").read_bytes()
    brief_path = pkg / "data/audit_brief.json"
    stale_brief = json.loads(brief_path.read_text(encoding="utf-8"))
    for statement in stale_brief["statements"]:
        statement.pop("relation_ids")
    brief_path.write_text(
        json.dumps(stale_brief, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    client = PipelineClient()

    result = prepare_package(
        pkg,
        client=client,
        retriever=None,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert result.cached is False
    assert client.calls == ["brief"]
    assert result.status == "ready"
    assert (pkg / "data/audit_facts.json").read_bytes() == facts_before
    assert (pkg / "data/standards_context.json").read_bytes() == context_before
    validate_audit_package(pkg)


@pytest.mark.parametrize("present", [False, None])
def test_upstream_cache_requires_present_commit_marker(
    tmp_path: Path,
    present: bool | None,
) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if present is None:
        meta["audit_preparation"].pop("present")
    else:
        meta["audit_preparation"]["present"] = present
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    client = PipelineClient()
    retriever = StubRetriever()

    result = prepare_package(
        pkg,
        client=client,
        retriever=retriever,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert result.cached is False
    assert client.calls == ["region", "consolidate", "brief"]
    assert len(retriever.calls) == 1


def test_recipe_cache_mirror_must_match_current_artifact_keys(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    cache.update_audit(pkg.parent, pkg.name, facts_key="0" * 64)
    client = PipelineClient()
    retriever = StubRetriever()

    result = prepare_package(
        pkg,
        client=client,
        retriever=retriever,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert result.cached is False
    assert client.calls == ["region", "consolidate", "brief"]
    assert len(retriever.calls) == 1


def test_error_context_is_retried_while_facts_are_reused(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    failing_retriever = FailingRetriever()
    first = prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=failing_retriever,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    assert first.status == "partial"
    assert len(failing_retriever.calls) == 1
    failed_context = json.loads(first.standards_path.read_text(encoding="utf-8"))
    assert failed_context["queries"][0]["status"] == "error"
    facts_before = first.facts_path.read_bytes()

    client = PipelineClient()
    retriever = StubRetriever()
    second = prepare_package(
        pkg,
        client=client,
        retriever=retriever,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )

    assert second.cached is False
    assert second.status == "ready"
    assert client.calls == ["brief"]
    assert len(retriever.calls) == 1
    assert second.facts_path.read_bytes() == facts_before
    recovered_context = json.loads(second.standards_path.read_text(encoding="utf-8"))
    assert recovered_context["queries"][0]["status"] == "success"

    class Exploding:
        def __call__(self, **kwargs):
            raise AssertionError("model must not be called")

        def search(self, *args, **kwargs):
            raise AssertionError("retriever must not be called")

    third = prepare_package(
        pkg,
        client=Exploding(),
        retriever=Exploding(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
    )
    assert third.cached is True


def test_prepare_version_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:
    import excel_to_skill.audit.prepare as prepare_module

    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    monkeypatch.setattr(prepare_module, "PREPARE_VERSION", "0.2.0")
    client = PipelineClient()
    result = prepare_module.prepare_package(
        pkg,
        client=client,
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at="2026-07-12T00:00:00Z",
    )
    assert result.cached is False
    assert client.calls == ["region", "consolidate", "brief"]
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    assert meta["audit_preparation"]["version"] == "0.2.0"


def test_failed_force_prepare_preserves_previous_ready_bundle(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    paths = [pkg / "data" / name for name in (
        "audit_facts.json", "standards_context.json", "audit_brief.json"
    )]
    before = [path.read_bytes() for path in paths]
    meta_before = (pkg / "meta.json").read_bytes()

    with pytest.raises(AuditPrepareError, match="brief unavailable"):
        prepare_package(
            pkg,
            client=PipelineClient(fail_brief=True),
            retriever=StubRetriever(),
            retriever_descriptor={**_DESCRIPTOR, "version": "2"},
            model="stub-model",
            force=True,
            generated_at="2026-07-12T00:00:00Z",
        )
    assert [path.read_bytes() for path in paths] == before
    assert (pkg / "meta.json").read_bytes() == meta_before
    validate_audit_package(pkg)


def test_publish_write_failure_rolls_back_all_artifacts_and_views(
    tmp_path: Path, monkeypatch
) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    protected = [
        pkg / "data" / "audit_facts.json",
        pkg / "data" / "standards_context.json",
        pkg / "data" / "audit_brief.json",
        pkg / "meta.json",
        pkg / "SKILL.md",
    ]
    before = {path: path.read_bytes() for path in protected}
    original_replace = Path.replace
    publishes = 0

    def flaky_replace(self: Path, target):
        nonlocal publishes
        target_path = Path(target)
        if target_path.parent == pkg / "data" and target_path.name in {
            "audit_facts.json", "standards_context.json", "audit_brief.json"
        }:
            publishes += 1
            if publishes == 2:
                raise OSError("injected second publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    with pytest.raises(AuditPrepareError, match="second publish failure"):
        prepare_package(
            pkg,
            client=PipelineClient(),
            retriever=StubRetriever(corpus_version="2026.2"),
            retriever_descriptor={**_DESCRIPTOR, "corpus_version": "2026.2"},
            model="stub-model",
            force=True,
            generated_at="2026-07-12T00:00:00Z",
        )
    assert {path: path.read_bytes() for path in protected} == before
    validate_audit_package(pkg)


def test_prepared_bundle_is_query_ready_and_draft_is_explicit(
    tmp_path: Path, capsys
) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )

    briefing = brief(pkg)
    assert briefing["unreviewed"] is True
    assert briefing["readiness"]["status"] == "ready"
    searched = audit_search(pkg, query="매출")
    assert searched["total_matches"] >= 2
    item = audit_get(pkg, item_id="statement:fact")
    assert item["kind"] == "statement"
    traced = trace(pkg, item_id="statement:fact")
    assert traced["facts"] and traced["sources"]
    assert traced["cells"][0]["sheet"] == "Data"
    assert traced["cells"][0]["cell"] == "A1"

    assert main(["brief", str(pkg), "--limit", "1"]) == 0
    cli_doc = json.loads(capsys.readouterr().out)
    assert cli_doc["unreviewed"] is True
    assert cli_doc["returned"] == 1


def test_verify_and_overview_reject_meta_audit_state_tampering(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["audit_preparation"]["version"] = "bogus"
    meta["audit_preparation"]["review_status"] = "approved"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    check = next(c for c in verify_package(pkg).checks if c.name == "audit")
    assert not check.ok
    with pytest.raises(ConsumeError, match="감사 prepare 상태가 손상"):
        overview(pkg)
