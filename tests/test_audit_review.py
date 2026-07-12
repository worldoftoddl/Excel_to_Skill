from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import excel_to_skill.audit.review as review_module
from excel_to_skill import cache
from excel_to_skill.audit.agent import render_audit_agent_markdown, run_audit_agent
from excel_to_skill.audit.consume import brief as consume_brief
from excel_to_skill.audit.contract import bundle_keys
from excel_to_skill.audit.prepare import prepare_package
from excel_to_skill.audit.review import (
    AuditReviewError,
    approve_audit_package,
    reject_audit_package,
)
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
