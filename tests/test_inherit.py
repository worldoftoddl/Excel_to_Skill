"""M3 4b: 승계 규칙 테스트(§6) — converter_version만 올라 재변환 시 semantics 이월.

가짜 cv로 버전 범프를 시뮬레이션한다(probe.reason=version_changed). LLM은 stub.
V3(--source)는 재변환에 실 converter_version을 써 가짜 cv와 어긋나므로, 승계 검증은
V1·V2·SKILL·annotation 검사로 한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from excel_to_skill import cache, review
from excel_to_skill.annotator import annotate_package
from excel_to_skill.cli import _convert_one
from excel_to_skill.verify import verify_package

FX_DIR = Path(__file__).parent / "fixtures"

_SHEET_OK = json.dumps({
    "name": "Data", "purpose": "합계 표", "evidence": ["Data!A1"], "confidence": 0.9,
    "sections": [{
        "range": "A1:B5", "semantic_type": "table_header",
        "evidence": ["Data!A1:B1"], "confidence": 0.8,
    }],
}, ensure_ascii=False)
_WB_OK = json.dumps({
    "workbook_claims": [{"claim": "합계 표", "evidence": ["Data!A1:B5"], "confidence": 0.9}]
}, ensure_ascii=False)


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, *, system, user, schema=None):
        return self.responses.pop(0)


def _approved_pkg(root: Path, *, cv: str) -> Path:
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _convert_one(src, root, force=True, cv=cv)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    review.approve(pkg)
    return pkg


def test_inherit_carries_approved_semantics_on_version_bump(tmp_path: Path) -> None:
    root = tmp_path / "out"
    pkg = _approved_pkg(root, cv="cvA")
    assert json.loads((pkg / "data/semantics.json").read_text())["review"]["status"] == "approved"

    # converter_version만 올려 재변환 → 승계
    pkg2 = _convert_one(FX_DIR / "fx1_merge_formula.xlsx", root, force=False, cv="cvB")
    assert pkg2 == pkg  # 같은 dirname

    # version_changed 트리거는 _index 항목의 cv(cvA)↔probe cv(cvB)로 판정된다.
    # meta.converter_version은 cv 인자가 아니라 실 버전에서 오므로 여기선 검증 대상 아님.
    meta = json.loads((pkg2 / "meta.json").read_text(encoding="utf-8"))
    sem = json.loads((pkg2 / "data/semantics.json").read_text(encoding="utf-8"))
    assert cache.load_index(root)["entries"][pkg.name]["converter_version"] == "cvB"
    assert sem["review"]["status"] == "approved"  # V2 통과 → approved 유지
    assert meta["annotation"]["review_status"] == "approved"
    assert meta["annotation"]["annotation_key"]  # 이월된 완료 키
    # SKILL 승인판 유지
    assert "## ⑥ 해석 (승인됨)" in (pkg2 / "SKILL.md").read_text(encoding="utf-8")
    # _index도 이월(주석 키·리뷰 상태)
    entry = cache.load_index(root)["entries"][pkg.name]
    assert entry["review_status"] == "approved" and entry["annotation_key"]
    # V1·V2·SKILL·annotation 전부 통과(원본 없이)
    assert verify_package(pkg2).ok


def test_inherit_downgrades_to_draft_on_v2_failure(tmp_path: Path) -> None:
    root = tmp_path / "out"
    pkg = _approved_pkg(root, cv="cvA")
    # 구 semantics를 used range 밖 주소로 훼손(승계 후 V2가 실패하도록). _index의
    # annotation_key는 그대로라 _should_inherit는 여전히 참.
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    sem["sheets"][0]["evidence"] = ["Data!ZZ999"]
    (pkg / "data/semantics.json").write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")

    pkg2 = _convert_one(FX_DIR / "fx1_merge_formula.xlsx", root, force=False, cv="cvB")
    sem2 = json.loads((pkg2 / "data/semantics.json").read_text(encoding="utf-8"))
    assert sem2["review"]["status"] == "draft"  # V2 실패 → draft 강등
    assert "V2" in (sem2["review"]["note"] or "")
    meta2 = json.loads((pkg2 / "meta.json").read_text(encoding="utf-8"))
    assert meta2["annotation"]["review_status"] == "draft"
    # 깨진 evidence가 이월됐으므로 verify는 V2에서 실패(=재주석 필요 신호)
    checks = verify_package(pkg2).checks
    assert next(c for c in checks if c.name == "V2").ok is False
    assert next(c for c in checks if c.name == "annotation").ok  # 상태 자체는 일관


def test_no_inherit_without_annotation(tmp_path: Path) -> None:
    """주석 없는 패키지는 버전 범프 재변환에서 승계 대상이 아니다(정상 재생성)."""
    root = tmp_path / "out"
    src = FX_DIR / "fx1_merge_formula.xlsx"
    _convert_one(src, root, force=True, cv="cvA")  # annotate 안 함
    pkg2 = _convert_one(src, root, force=False, cv="cvB")
    assert not (pkg2 / "data/semantics.json").is_file()
    meta = json.loads((pkg2 / "meta.json").read_text(encoding="utf-8"))
    assert meta["annotation"]["present"] is False
    entry = cache.load_index(root)["entries"][pkg2.name]
    assert entry["annotation_key"] is None and entry["review_status"] is None


def test_no_inherit_when_params_also_change(tmp_path: Path) -> None:
    """버전+옵션(max_rows)이 동시에 바뀌면 승계하지 않는다(§6: converter_version만)."""
    root = tmp_path / "out"
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _convert_one(src, root, force=True, cv="cvA", max_rows=5000)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    review.approve(pkg)
    # cv도 바뀌고 max_rows도 바뀜 → reason=version_changed지만 conv_params 다름 → 미승계
    pkg2 = _convert_one(src, root, force=False, cv="cvB", max_rows=1)
    assert not (pkg2 / "data/semantics.json").is_file()
    entry = cache.load_index(root)["entries"][pkg2.name]
    assert entry["annotation_key"] is None and entry["review_status"] is None


def test_no_inherit_when_package_marker_missing(tmp_path: Path) -> None:
    """meta.annotation.annotation_key(패키지 내부 marker)가 없으면 승계 안 함 + verify 실패."""
    root = tmp_path / "out"
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _approved_pkg(root, cv="cvA")

    # 패키지 내부 완료 marker를 null로 훼손(=_index만 key 보유). approved+null이므로
    # verify가 실패해야 하고(4b 보정), 버전 범프 재변환도 승계하지 않아야 한다.
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    meta["annotation"]["annotation_key"] = None
    (pkg / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    ann = next(c for c in verify_package(pkg).checks if c.name == "annotation")
    assert not ann.ok and "approved" in ann.detail

    pkg2 = _convert_one(src, root, force=False, cv="cvB")
    assert not (pkg2 / "data/semantics.json").is_file()  # marker 불일치 → 미승계


def test_no_inherit_when_generator_tampered(tmp_path: Path) -> None:
    """generator가 훼손돼 재계산 키가 안 맞으면 승계 거부(approve·verify와 같은 기준)."""
    root = tmp_path / "out"
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _approved_pkg(root, cv="cvA")

    # semantics.generator.model 훼손 → meta.annotation_key(실제 model 기반)와 재계산 불일치
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    sem["generator"]["model"] = "tampered-model"
    (pkg / "data/semantics.json").write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")
    ann = next(c for c in verify_package(pkg).checks if c.name == "annotation")
    assert not ann.ok  # verify가 이미 실패시킴

    pkg2 = _convert_one(src, root, force=False, cv="cvB")
    assert not (pkg2 / "data/semantics.json").is_file()  # 승계 거부


def test_no_inherit_on_force(tmp_path: Path) -> None:
    """--force 재변환은 승계하지 않는다(§6: converter_version만 올랐을 때만)."""
    root = tmp_path / "out"
    _approved_pkg(root, cv="cvA")
    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg2 = _convert_one(src, root, force=True, cv="cvA")  # force → reason=force
    assert not (pkg2 / "data/semantics.json").is_file()  # 승계 안 됨(새 결정론만)
    entry = cache.load_index(root)["entries"][pkg2.name]
    assert entry["annotation_key"] is None
