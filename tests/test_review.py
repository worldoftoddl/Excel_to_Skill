"""M3 3단계: review(승인/반려) + 승인판 SKILL.md 재생성 테스트(§7·§4.2).

annotate는 stub 클라이언트로 semantics(draft)를 만든 뒤, review로 승인/반려하고
SKILL.md·review 블록·verify를 검증한다. 실 LLM·anthropic 불요.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill import review
from excel_to_skill.annotator import annotate_package
from excel_to_skill.cli import _convert_one
from excel_to_skill.meta import _converter_version
from excel_to_skill.verify import verify_package

FX_DIR = Path(__file__).parent / "fixtures"

_SHEET_OK = json.dumps({
    "name": "Data", "purpose": "합계 표", "evidence": ["Data!A1"], "confidence": 0.9,
    "sections": [{
        "range": "A1:B5", "semantic_type": "table_header",
        "evidence": ["Data!A1:B1"], "confidence": 0.8,
        "fields": [{"label_cell": "A1", "value_cell": "B1", "role": "머리"}],
    }],
}, ensure_ascii=False)
_WB_OK = json.dumps({
    "workbook_claims": [
        {"claim": "단일 합계 표 워크북", "evidence": ["Data!A1:B5"], "confidence": 0.9}
    ]
}, ensure_ascii=False)


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, *, system, user):
        return self.responses.pop(0)


def _annotated_pkg(tmp_path: Path) -> Path:
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    return pkg


def _sem(pkg: Path) -> dict:
    return json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))


def _skill(pkg: Path) -> str:
    return (pkg / "SKILL.md").read_text(encoding="utf-8")


def test_approve_sets_status_and_renders_interpretation(tmp_path: Path) -> None:
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _annotated_pkg(tmp_path)
    # 승인 전: draft, SKILL ⑥은 미승인 한 줄
    assert _sem(pkg)["review"]["status"] == "draft"
    assert "미승인" in _skill(pkg) or "직접 해석" in _skill(pkg)

    res = review.approve(pkg)
    assert res["status"] == "approved"
    rv = _sem(pkg)["review"]
    assert rv["status"] == "approved" and rv["reviewed_at"] and rv["note"] is None

    skill = _skill(pkg)
    assert "## ⑥ 해석 (승인됨)" in skill
    assert "단일 합계 표 워크북" in skill  # workbook_claim 렌더
    assert "합계 표" in skill and "`Data!A1`" in skill  # 시트 purpose + evidence 주소
    # frontmatter description이 승인판(claim)으로 교체됨
    assert "단일 합계 표 워크북" in skill.split("---")[1]

    # 승인 후에도 verify 통과(V3는 SKILL.md 제외 규칙 덕에 --source로도 OK)
    result = verify_package(pkg, source=src)
    assert result.ok, [(c.name, c.detail) for c in result.checks if not c.skipped and not c.ok]


def test_approve_refused_when_v2_fails(tmp_path: Path) -> None:
    pkg = _annotated_pkg(tmp_path)
    # semantics를 V2-불량으로 훼손(used range 밖 주소)
    sem = _sem(pkg)
    sem["sheets"][0]["evidence"] = ["Data!ZZ999"]
    (pkg / "data/semantics.json").write_text(
        json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    before = _skill(pkg)
    with pytest.raises(review.ReviewError):
        review.approve(pkg)
    # 거부 시 상태·SKILL 불변
    assert _sem(pkg)["review"]["status"] == "draft"
    assert _skill(pkg) == before


def test_reject_requires_note_and_stays_unapproved(tmp_path: Path) -> None:
    pkg = _annotated_pkg(tmp_path)
    with pytest.raises(review.ReviewError):
        review.reject(pkg, note=None)
    with pytest.raises(review.ReviewError):
        review.reject(pkg, note="   ")

    res = review.reject(pkg, note="근거 부족")
    assert res["status"] == "rejected"
    rv = _sem(pkg)["review"]
    assert rv["status"] == "rejected" and rv["note"] == "근거 부족" and rv["reviewed_at"]
    # SKILL ⑥은 미승인 유지(승인 의미 미노출)
    skill = _skill(pkg)
    assert "## ⑥ 해석 (승인됨)" not in skill
    assert "직접 해석" in skill


def test_approve_then_reject_reverts_skill(tmp_path: Path) -> None:
    pkg = _annotated_pkg(tmp_path)
    review.approve(pkg)
    assert "## ⑥ 해석 (승인됨)" in _skill(pkg)
    review.reject(pkg, note="재검토")
    skill = _skill(pkg)
    assert "## ⑥ 해석 (승인됨)" not in skill  # 승인 의미가 사라짐
    assert "직접 해석" in skill
    assert _sem(pkg)["review"]["status"] == "rejected"


def test_review_without_semantics_errors(tmp_path: Path) -> None:
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    assert not (pkg / "data/semantics.json").is_file()
    with pytest.raises(review.ReviewError):
        review.approve(pkg)


def test_skill_from_package_reproduces_draft(tmp_path: Path) -> None:
    """semantics 없는 패키지의 build_skill_md_from_package == convert-time draft SKILL."""
    from excel_to_skill.emit_skill_md import build_skill_md_from_package

    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    assert build_skill_md_from_package(pkg) == _skill(pkg)
