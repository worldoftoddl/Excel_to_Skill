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

    def __call__(self, *, system, user, schema=None):
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


def test_tampered_approved_skill_fails_verify(tmp_path: Path) -> None:
    """승인 후 SKILL.md를 훼손하면 verify가 잡는다(V3 제외 공백 보정)."""
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _annotated_pkg(tmp_path)
    review.approve(pkg)
    assert verify_package(pkg, source=src).ok  # 승인 직후 정상

    (pkg / "SKILL.md").write_text("tampered skill", encoding="utf-8")
    result = verify_package(pkg, source=src)
    assert not result.ok  # 과거엔 통과하던 공백
    skill = next(c for c in result.checks if c.name == "SKILL")
    assert not skill.ok and not skill.skipped
    # 원본 없이도 잡힌다
    assert not verify_package(pkg, source=None).ok


def test_meta_annotation_tracks_semantics_state(tmp_path: Path) -> None:
    """meta.annotation이 annotate/approve/reject 상태를 반영한다(모순 제거)."""
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    meta0 = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    assert meta0["annotation"] == {
        "present": False, "annotator_version": None,
        "review_status": None, "annotation_key": None,
    }

    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    a = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))["annotation"]
    assert a["present"] is True and a["review_status"] == "draft" and a["annotator_version"]

    review.approve(pkg)
    assert json.loads((pkg / "meta.json").read_text(encoding="utf-8"))["annotation"]["review_status"] == "approved"
    review.reject(pkg, note="x")
    assert json.loads((pkg / "meta.json").read_text(encoding="utf-8"))["annotation"]["review_status"] == "rejected"
    # meta가 바뀌어도 verify(V3 포함, source)로 통과 — annotation은 V3 meta 비교서 제외
    assert verify_package(pkg, source=FX_DIR / "fx1_merge_formula.xlsx").ok


def test_approve_refused_for_partial_annotation(tmp_path: Path) -> None:
    """부분 주석(excluded 있음, annotation_key 미완료)은 verify가 통과해도 승인 거부."""
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    # 시트 2연속 실패 → excluded=['Data'], annotation_key=None(partial)
    annotate_package(pkg, client=StubClient(["not json", "not json", _WB_OK]))
    sem = _sem(pkg)
    assert sem["sheets"] == [] and sem["review"]["status"] == "draft"
    assert verify_package(pkg).ok  # V1/V2는 통과(빈 sheets 허용)

    with pytest.raises(review.ReviewError):  # 그래도 승인은 거부
        review.approve(pkg)
    assert _sem(pkg)["review"]["status"] == "draft"  # 상태 불변

    # 완료 주석(--force)으로 다시 만들면 승인 가능
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]), force=True)
    assert review.approve(pkg)["status"] == "approved"


def test_approve_is_package_standalone(tmp_path: Path) -> None:
    """완료 marker가 meta.annotation에 있으므로, 폴더만 복사해도(부모 _index 없이) 승인된다."""
    import shutil

    pkg = _annotated_pkg(tmp_path)  # 완료 주석
    # meta.annotation.annotation_key(패키지 내부 marker)가 채워졌는지
    ann = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))["annotation"]
    assert ann["annotation_key"]

    copied = tmp_path / "elsewhere" / "pkgcopy"
    copied.parent.mkdir(parents=True)
    shutil.copytree(pkg, copied)  # _index.json은 tmp_path에 있고 복사 안 됨
    assert not (copied.parent / "_index.json").exists()

    res = review.approve(copied)  # 패키지 파일만으로 승인 동작
    assert res["status"] == "approved"
    assert json.loads((copied / "data/semantics.json").read_text())["review"]["status"] == "approved"


def test_review_without_semantics_errors(tmp_path: Path) -> None:
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    assert not (pkg / "data/semantics.json").is_file()
    with pytest.raises(review.ReviewError):
        review.approve(pkg)


def test_verify_catches_meta_semantics_mismatch(tmp_path: Path) -> None:
    """meta.annotation ↔ semantics.review가 어긋나면 annotation 검사가 잡는다."""
    pkg = _annotated_pkg(tmp_path)
    review.approve(pkg)
    assert verify_package(pkg).ok  # 일관된 상태

    def _ann(p):
        return next(c for c in verify_package(p).checks if c.name == "annotation")

    # meta.annotation.present=false로 수동 훼손 → 불일치 검출
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    meta["annotation"]["present"] = False
    (pkg / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    c = _ann(pkg)
    assert not c.ok and "present" in c.detail
    assert not verify_package(pkg).ok

    # review_status 어긋남도 검출
    meta["annotation"] = {"present": True, "annotator_version": "0.1.0", "review_status": "draft"}
    (pkg / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    c2 = _ann(pkg)
    assert not c2.ok and "review_status" in c2.detail


def test_verify_catches_tampered_annotation_key(tmp_path: Path) -> None:
    """meta.annotation.annotation_key가 non-null인데 재계산과 다르면 verify가 잡는다."""
    pkg = _annotated_pkg(tmp_path)  # 완료 → annotation_key 채워짐
    assert verify_package(pkg).ok

    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    meta["annotation"]["annotation_key"] = "0" * 64  # 가짜 완료 키 주입
    (pkg / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    c = next(x for x in verify_package(pkg).checks if x.name == "annotation")
    assert not c.ok and "annotation_key" in c.detail
    assert not verify_package(pkg).ok


def test_verify_allows_partial_draft_null_key(tmp_path: Path) -> None:
    """partial annotate(annotation_key=null)는 손상이 아니라 미완료 draft로 verify 통과."""
    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    annotate_package(pkg, client=StubClient(["not json", "not json", _WB_OK]))  # partial
    assert json.loads((pkg / "meta.json").read_text())["annotation"]["annotation_key"] is None
    c = next(x for x in verify_package(pkg).checks if x.name == "annotation")
    assert c.ok  # partial은 허용(완료성은 approve가 거부)
    assert verify_package(pkg).ok


def test_review_updates_index_review_status(tmp_path: Path) -> None:
    """approve/reject가 _index.json의 review_status도 갱신한다."""
    from excel_to_skill import cache

    pkg = _annotated_pkg(tmp_path)
    root, dirname = pkg.parent, pkg.name
    assert cache.load_index(root)["entries"][dirname]["review_status"] == "draft"
    review.approve(pkg)
    assert cache.load_index(root)["entries"][dirname]["review_status"] == "approved"
    review.reject(pkg, note="x")
    assert cache.load_index(root)["entries"][dirname]["review_status"] == "rejected"


def test_skill_from_package_reproduces_draft(tmp_path: Path) -> None:
    """semantics 없는 패키지의 build_skill_md_from_package == convert-time draft SKILL."""
    from excel_to_skill.emit_skill_md import build_skill_md_from_package

    pkg = _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )
    assert build_skill_md_from_package(pkg) == _skill(pkg)
