from __future__ import annotations

import json
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import excel_to_skill.audit.review as review_module
from excel_to_skill import cache
from excel_to_skill.audit.agent import render_audit_agent_markdown, run_audit_agent
from excel_to_skill.audit.consume import brief as consume_brief
from excel_to_skill.audit.contract import bundle_keys
from excel_to_skill.audit.model import json_sha256
from excel_to_skill.audit.prepare import prepare_package
from excel_to_skill.audit.review import (
    AuditReviewError,
    approve_audit_package,
    reject_audit_package,
)
from excel_to_skill.audit.scope import load_scope_bundle, resolve_scope
from excel_to_skill.audit.validate import validate_audit_package
from excel_to_skill.cli import _cmd_audit_review

from test_audit_prepare import (
    PipelineClient,
    StubRetriever,
    _DESCRIPTOR,
    _WHEN,
    _package,
)


def _prepared(tmp_path: Path) -> Path:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    return pkg


def _prepared_sheet(tmp_path: Path) -> Path:
    pkg = _package(tmp_path)
    prepare_package(
        pkg,
        sheet="Data",
        client=PipelineClient(),
        retriever=StubRetriever(),
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
        generated_at=_WHEN,
    )
    return pkg


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_audit_review_approves_facts_and_brief_and_preserves_cache_hit(
    tmp_path: Path,
) -> None:
    pkg = _prepared(tmp_path)
    original_meta = _read(pkg / "meta.json")

    result = approve_audit_package(
        pkg, reviewed_at="2026-07-12T03:00:00Z"
    )

    facts = _read(pkg / "data/audit_facts.json")
    context = _read(pkg / "data/standards_context.json")
    brief_doc = _read(pkg / "data/audit_brief.json")
    expected_review = {
        "status": "approved",
        "reviewed_at": "2026-07-12T03:00:00Z",
        "note": None,
    }
    assert facts["review"] == expected_review
    assert brief_doc["review"] == expected_review
    assert result["status"] == "approved"
    validate_audit_package(pkg)

    keys = bundle_keys(facts, context, brief_doc)
    meta = _read(pkg / "meta.json")
    assert meta["audit_preparation"]["prepared_at"] == (
        original_meta["audit_preparation"]["prepared_at"]
    )
    assert meta["audit_preparation"]["review_status"] == "approved"
    assert tuple(meta["audit_preparation"][name] for name in (
        "facts_key", "standards_key", "brief_key"
    )) == keys
    consumed = consume_brief(pkg)
    assert consumed["facts_review_status"] == "approved"
    assert consumed["review_status"] == "approved"
    assert consumed["unreviewed"] is False

    state = cache.get_audit(pkg.parent, pkg.name)
    assert state is not None
    assert tuple(state[name] for name in (
        "facts_key", "standards_key", "brief_key"
    )) == keys
    cached = prepare_package(
        pkg,
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("approved cache hit must not build a model client")
        ),
        retriever=None,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
    )
    assert cached.cached is True


def test_rejected_audit_bundle_blocks_agent_without_model(tmp_path: Path) -> None:
    pkg = _prepared(tmp_path)
    reject_audit_package(
        pkg,
        note="표본선정 근거 보완 필요",
        reviewed_at="2026-07-12T03:00:00Z",
    )
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("rejected bundle must not call model")

    response = run_audit_agent(pkg, model="stub-model", client_factory=factory)

    assert called is False
    assert response["answer"]["abstained"] is True
    assert response["trust"]["source_facts_review_status"] == "rejected"
    assert response["trust"]["source_brief_review_status"] == "rejected"
    assert response["trust"]["source_facts_reviewed_at"] == "2026-07-12T03:00:00Z"
    assert response["trust"]["source_brief_reviewed_at"] == "2026-07-12T03:00:00Z"
    assert response["trust"]["source_facts_review_note"] == "표본선정 근거 보완 필요"
    assert response["trust"]["source_brief_review_note"] == "표본선정 근거 보완 필요"
    assert "표본선정 근거 보완 필요" in response["answer"]["abstention_reason"]

    briefing = consume_brief(pkg)
    assert briefing["facts_review_note"] == "표본선정 근거 보완 필요"
    assert briefing["review_note"] == "표본선정 근거 보완 필요"
    assert briefing["facts_reviewed_at"] == "2026-07-12T03:00:00Z"
    assert briefing["reviewed_at"] == "2026-07-12T03:00:00Z"
    rendered = render_audit_agent_markdown(response)
    assert "표본선정 근거 보완 필요" in rendered


def test_audit_review_publish_failure_rolls_back_complete_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg = _prepared(tmp_path)
    paths = [
        pkg / "data/audit_facts.json",
        pkg / "data/standards_context.json",
        pkg / "data/audit_brief.json",
        pkg / "SKILL.md",
        pkg / "meta.json",
    ]
    before = [path.read_bytes() for path in paths]

    def fail_commit(*_args, **_kwargs):
        raise OSError("injected audit review commit failure")

    monkeypatch.setattr(review_module, "set_audit_preparation", fail_commit)
    with pytest.raises(AuditReviewError, match="commit failure"):
        approve_audit_package(pkg, reviewed_at="2026-07-12T03:00:00Z")

    assert [path.read_bytes() for path in paths] == before
    validate_audit_package(pkg)


def test_audit_review_cli_and_reject_note_contract(tmp_path: Path, capsys) -> None:
    pkg = _prepared(tmp_path)
    with pytest.raises(AuditReviewError, match="--note"):
        reject_audit_package(pkg, note="")

    args = Namespace(path=str(pkg), approve=True, reject=False, note=None)
    assert _cmd_audit_review(args) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(pkg / "SKILL.md")
    assert "status=approved" in captured.err


def test_audit_review_clears_unwitnessed_stage_recipes(tmp_path: Path) -> None:
    pkg = _prepared(tmp_path)
    cache.update_audit(pkg.parent, pkg.name, facts_recipe_key="corrupt")

    approve_audit_package(pkg, reviewed_at="2026-07-12T03:00:00Z")

    state = cache.get_audit(pkg.parent, pkg.name)
    assert state is not None
    assert state["facts_recipe_key"] is None
    assert state["standards_recipe_key"] is None
    assert state["brief_recipe_key"] is None


def test_sheet_audit_review_updates_only_selected_scope_and_preserves_cache_hit(
    tmp_path: Path,
) -> None:
    pkg = _prepared_sheet(tmp_path)
    scope = resolve_scope(pkg, sheet="Data")
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    paths, _, _, _, old_commit = loaded
    root_before = {
        "meta": (pkg / "meta.json").read_bytes(),
        "skill": (pkg / "SKILL.md").read_bytes(),
    }
    root_cache_before = cache.get_audit(pkg.parent, pkg.name)
    sibling_id = "f" * 64
    cache.update_audit_scope(
        pkg.parent,
        pkg.name,
        sibling_id,
        facts_key="sibling-facts",
        status="partial",
    )
    sibling_before = cache.get_audit_scope(pkg.parent, pkg.name, sibling_id)

    result = approve_audit_package(
        pkg,
        sheet="Data",
        reviewed_at="2026-07-12T03:00:00Z",
    )

    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    _, facts, context, brief_doc, commit = loaded
    expected_review = {
        "status": "approved",
        "reviewed_at": "2026-07-12T03:00:00Z",
        "note": None,
    }
    assert facts["review"] == expected_review
    assert brief_doc["review"] == expected_review
    assert context["input"]["audit_facts_sha256"] == json_sha256(facts)
    assert brief_doc["inputs"]["audit_facts_sha256"] == json_sha256(facts)
    assert brief_doc["inputs"]["standards_context_sha256"] == json_sha256(context)
    assert commit["review_status"] == "approved"
    assert commit["prepared_at"] == old_commit["prepared_at"]
    assert result == {
        "status": "approved",
        "scope": scope.identity(),
        "facts": str(paths.facts),
        "brief": str(paths.brief),
        "commit": str(paths.commit),
    }
    assert (pkg / "meta.json").read_bytes() == root_before["meta"]
    assert (pkg / "SKILL.md").read_bytes() == root_before["skill"]
    assert cache.get_audit(pkg.parent, pkg.name) == root_cache_before
    assert cache.get_audit_scope(pkg.parent, pkg.name, sibling_id) == sibling_before

    state = cache.get_audit_scope(pkg.parent, pkg.name, scope.id)
    assert state is not None
    assert tuple(state[name] for name in (
        "facts_key", "standards_key", "brief_key"
    )) == tuple(commit[name] for name in (
        "facts_key", "standards_key", "brief_key"
    ))
    cached = prepare_package(
        pkg,
        sheet="Data",
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("approved sheet cache hit must not build a model client")
        ),
        retriever=None,
        retriever_descriptor=_DESCRIPTOR,
        model="stub-model",
    )
    assert cached.cached is True


def test_sheet_rejection_reason_is_scope_local_and_consumer_visible(
    tmp_path: Path,
) -> None:
    pkg = _prepared_sheet(tmp_path)
    root_before = (pkg / "meta.json").read_bytes()

    result = reject_audit_package(
        pkg,
        sheet="Data",
        note="표본 모집단 근거 보완 필요",
        reviewed_at="2026-07-12T03:00:00Z",
    )

    briefing = consume_brief(pkg, sheet="Data")
    assert result["status"] == "rejected"
    assert briefing["facts_review_status"] == "rejected"
    assert briefing["review_status"] == "rejected"
    assert briefing["facts_review_note"] == "표본 모집단 근거 보완 필요"
    assert briefing["review_note"] == "표본 모집단 근거 보완 필요"
    assert (pkg / "meta.json").read_bytes() == root_before


def test_sheet_approval_is_not_blocked_by_unrelated_root_or_sibling_damage(
    tmp_path: Path,
) -> None:
    pkg = _prepared_sheet(tmp_path)
    # Neither artifact is part of the selected Data scope.  They intentionally make global
    # verify fail while the deterministic ledger and selected scope remain valid.
    (pkg / "data/audit_facts.json").write_text("{}\n", encoding="utf-8")
    sibling = pkg / "data/audit_scopes/sheets" / ("f" * 64)
    sibling.mkdir(parents=True)
    (sibling / "commit.json").write_text("{}\n", encoding="utf-8")
    (pkg / "SKILL.md").unlink()
    meta = _read(pkg / "meta.json")
    meta["annotation"] = {"present": "damaged"}
    meta["audit_preparation"] = {"present": "damaged"}
    (pkg / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = approve_audit_package(
        pkg,
        sheet="Data",
        reviewed_at="2026-07-12T03:00:00Z",
    )

    assert result["status"] == "approved"
    scope = resolve_scope(pkg, sheet="Data")
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    assert loaded[1]["review"]["status"] == "approved"
    assert not (pkg / "SKILL.md").exists()
    unchanged_meta = _read(pkg / "meta.json")
    assert unchanged_meta["annotation"] == {"present": "damaged"}
    assert unchanged_meta["audit_preparation"] == {"present": "damaged"}


def test_sheet_approval_still_requires_shared_deterministic_core(
    tmp_path: Path,
) -> None:
    pkg = _prepared_sheet(tmp_path)
    (pkg / "data/references.json").unlink()

    with pytest.raises(AuditReviewError, match="audit_scope_core:files"):
        approve_audit_package(
            pkg,
            sheet="Data",
            reviewed_at="2026-07-12T03:00:00Z",
        )


def test_sheet_audit_review_commit_failure_rolls_back_scope_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg = _prepared_sheet(tmp_path)
    scope = resolve_scope(pkg, sheet="Data")
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    paths = loaded[0]
    protected = [*paths.artifacts, paths.commit]
    before = [path.read_bytes() for path in protected]
    root_before = [
        (pkg / "meta.json").read_bytes(),
        (pkg / "SKILL.md").read_bytes(),
    ]

    def fail_commit(*_args, **_kwargs):
        raise OSError("injected sheet review commit failure")

    monkeypatch.setattr(review_module, "write_scope_commit_atomic", fail_commit)
    with pytest.raises(AuditReviewError, match="commit failure"):
        approve_audit_package(
            pkg,
            sheet="Data",
            reviewed_at="2026-07-12T03:00:00Z",
        )

    assert [path.read_bytes() for path in protected] == before
    assert [
        (pkg / "meta.json").read_bytes(),
        (pkg / "SKILL.md").read_bytes(),
    ] == root_before
    assert load_scope_bundle(pkg, scope) is not None


def test_concurrent_sheet_reviews_serialize_to_valid_complete_commits(
    tmp_path: Path,
) -> None:
    pkg = _prepared_sheet(tmp_path)

    def reject(pair: tuple[str, str]) -> dict:
        note, reviewed_at = pair
        return reject_audit_package(
            pkg,
            sheet="Data",
            note=note,
            reviewed_at=reviewed_at,
        )

    attempts = [
        ("첫 번째 반려 사유", "2026-07-12T03:00:00Z"),
        ("두 번째 반려 사유", "2026-07-12T03:01:00Z"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reject, attempts))

    assert [result["status"] for result in results] == ["rejected", "rejected"]
    scope = resolve_scope(pkg, sheet="Data")
    loaded = load_scope_bundle(pkg, scope)
    assert loaded is not None
    _, facts, context, brief_doc, commit = loaded
    assert facts["review"] == brief_doc["review"]
    assert (facts["review"]["note"], facts["review"]["reviewed_at"]) in attempts
    assert context["input"]["audit_facts_sha256"] == json_sha256(facts)
    assert brief_doc["inputs"]["audit_facts_sha256"] == json_sha256(facts)
    assert brief_doc["inputs"]["standards_context_sha256"] == json_sha256(context)
    assert commit["review_status"] == "rejected"
