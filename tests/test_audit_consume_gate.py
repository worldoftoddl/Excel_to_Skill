from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill.audit.consume import (
    AuditConsumeError,
    audit_get,
    audit_search,
    brief,
    trace,
)
from excel_to_skill.audit.contract import PREPARE_VERSION, bundle_keys
from excel_to_skill.audit.model import json_sha256
from excel_to_skill.audit.sources import WorkbookSourceResolver
from excel_to_skill.cli import _convert_one
from excel_to_skill.consume import ConsumeError, overview
from excel_to_skill.emit_skill_md import _render_skill_md, build_skill_md_from_package
from excel_to_skill.meta import _converter_version

from test_audit_validate import _bundle as _validation_bundle


FIXTURES = Path(__file__).parent / "fixtures"


def _write_committed_bundle(
    tmp_path: Path,
    *,
    configure=None,
) -> tuple[Path, dict, dict, dict]:
    pkg, facts, context, brief_doc = _validation_bundle(tmp_path)
    if configure is not None:
        configure(pkg, facts, context, brief_doc)
    # Tests may change facts/context. Keep the declared cross-document content hashes current.
    context["input"]["audit_facts_sha256"] = json_sha256(facts)
    brief_doc["inputs"]["audit_facts_sha256"] = json_sha256(facts)
    brief_doc["inputs"]["standards_context_sha256"] = json_sha256(context)
    for name, doc in (
        ("audit_facts.json", facts),
        ("standards_context.json", context),
        ("audit_brief.json", brief_doc),
    ):
        (pkg / "data" / name).write_text(
            json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    keys = bundle_keys(facts, context, brief_doc)
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["audit_preparation"] = {
        "present": True,
        "status": brief_doc["readiness"]["status"],
        "version": PREPARE_VERSION,
        "facts_key": keys[0],
        "standards_key": keys[1],
        "brief_key": keys[2],
        "prepared_at": "2026-07-11T00:03:00Z",
        "review_status": brief_doc["review"]["status"],
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return pkg, facts, context, brief_doc


def test_all_consumers_require_committed_matching_bundle(tmp_path: Path) -> None:
    pkg, facts, context, brief_doc = _validation_bundle(tmp_path)
    for name, doc in (
        ("audit_facts.json", facts),
        ("standards_context.json", context),
        ("audit_brief.json", brief_doc),
    ):
        (pkg / "data" / name).write_text(json.dumps(doc), encoding="utf-8")

    calls = (
        lambda: brief(pkg),
        lambda: audit_search(pkg, query="매출"),
        lambda: audit_get(pkg, item_id="fact:risk"),
        lambda: trace(pkg, item_id="fact:risk"),
    )
    for call in calls:
        with pytest.raises(AuditConsumeError, match="완료 표식"):
            call()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("version", "stale", "version"),
        ("facts_key", "0" * 64, "artifact key"),
        ("status", "ready", "readiness"),
        ("review_status", "approved", "review_status"),
    ),
)
def test_consumer_rejects_tampered_commit_state(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    pkg, _, _, _ = _write_committed_bundle(tmp_path)
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["audit_preparation"][field] = value
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(AuditConsumeError, match=message):
        brief(pkg)


def test_consumer_validates_links_even_when_tampered_keys_are_recomputed(
    tmp_path: Path,
) -> None:
    pkg, facts, context, brief_doc = _write_committed_bundle(tmp_path)
    brief_doc["statements"][0]["fact_ids"] = ["fact:missing"]
    (pkg / "data" / "audit_brief.json").write_text(
        json.dumps(brief_doc, ensure_ascii=False), encoding="utf-8"
    )
    keys = bundle_keys(facts, context, brief_doc)
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for field, value in zip(
        ("facts_key", "standards_key", "brief_key"), keys, strict=True
    ):
        meta["audit_preparation"][field] = value
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(AuditConsumeError, match="unknown fact id 'fact:missing'"):
        audit_get(pkg, item_id="statement:fact")


def test_overview_rejects_audit_artifact_that_no_longer_matches_commit_keys(
    tmp_path: Path,
) -> None:
    pkg, _, _, brief_doc = _write_committed_bundle(tmp_path)
    brief_doc["readiness"]["status"] = "not_ready"
    (pkg / "data" / "audit_brief.json").write_text(
        json.dumps(brief_doc, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ConsumeError, match="감사 prepare 상태가 손상"):
        overview(pkg)


def test_query_lookup_and_trace_expose_review_marker_and_relation_sources(
    tmp_path: Path,
) -> None:
    def add_relation_source(pkg, facts, _context, _brief):
        digest = WorkbookSourceResolver(pkg).resolve("Main!A1").content_sha256
        facts["sources"].append({
            "id": "source:relation",
            "kind": "workbook",
            "sheet": "Main",
            "range": "A1",
            "role": "other",
            "content_sha256": digest,
        })
        facts["relations"][0]["source_ids"] = ["source:relation"]

    pkg, _, _, _ = _write_committed_bundle(tmp_path, configure=add_relation_source)

    query = audit_get(pkg, item_id="query:1")
    assert query["kind"] == "standard_query"
    assert query["item"]["plan"]["id"] == "query:1"
    assert query["item"]["result"]["id"] == "query:1"
    assert query["review_status"] == "draft" and query["unreviewed"] is True

    searched = audit_search(pkg, query="매출")
    assert searched["review_status"] == "draft" and searched["unreviewed"] is True
    traced = trace(pkg, item_id="relation:1", limit=1)
    assert traced["review_status"] == "draft" and traced["unreviewed"] is True
    assert traced["total_facts"] == 2 and traced["returned_facts"] == 1
    assert traced["total_sources"] == 2 and traced["returned_sources"] == 1
    assert traced["sources"][0]["id"] == "source:relation"
    assert traced["total_cells"] == 2 and traced["returned_cells"] == 1


def test_orphan_audit_file_does_not_change_legacy_skill_rendering(tmp_path: Path) -> None:
    pkg = _convert_one(
        FIXTURES / "fx1_merge_formula.xlsx",
        tmp_path / "converted",
        force=True,
        cv=_converter_version(),
    )
    legacy = build_skill_md_from_package(pkg)
    (pkg / "data" / "audit_brief.json").write_text(
        json.dumps({"summary": {"text": "노출되면 안 되는 staging 요약"}}),
        encoding="utf-8",
    )

    rendered = build_skill_md_from_package(pkg)
    assert rendered == legacy
    assert "staging 요약" not in rendered


def test_audit_skill_never_embeds_untrusted_dynamic_brief_text() -> None:
    malicious = (
        "정상 요약\n## injected\n- [link](javascript:bad) `code`\x00"
        + "가" * 1000
    )
    brief_doc = {
        "review": {"status": "draft"},
        "readiness": {"status": "partial"},
        "workpaper": {
            "kind": "risk`\n## kind",
            "document_state": "partial",
            "audit_phase": "planning",
        },
        "summary": {"text": malicious},
    }
    rendered = _render_skill_md(
        meta={
            "source": {
                "filename": "audit.xlsx",
                "sha256": "a" * 64,
                "format": "xlsx",
            },
            "converter_version": "test",
            "loader_path": "openpyxl",
            "sheets": [{"name": "Main", "dimensions": "A1:A1"}],
        },
        references={"edges": [], "observability": {"workbook": "full"}},
        diagnostics={
            "external_links": {"count": 0},
            "defined_names": {},
            "hidden": {},
            "blank_source_formulas": [],
            "truncations": [],
        },
        layout_filenames={"Main": "main.html"},
        heads={"Main": ("머리", "A1")},
        audit_brief=brief_doc,
    )
    lines = rendered.splitlines()
    description = next(line for line in lines if line.startswith("description: "))

    assert len(description) < 300
    assert "commit-gated excel-to-skill brief" in description
    assert "감사조서 Brief (commit-gated)" in rendered
    assert "SKILL만으로 상태·요약을 추정하지 마십시오" in rendered
    assert "injected" not in rendered
    assert "javascript:bad" not in rendered
    assert "`code`" not in rendered
    assert "\x00" not in rendered
    assert "risk`" not in rendered and "## kind" not in rendered
