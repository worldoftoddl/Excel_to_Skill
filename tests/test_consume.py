"""소비 인터페이스(§10) 테스트 — overview/inspect/search/refs + CLI 배선.

전부 결정론(패키지 파일만). 단계 조회 계약(개요=셀원문 없음, inspect=범위 한정,
search/refs=예산 상한, 셀 레코드 최소 sheet·cell·value·formula)을 못박는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill.annotator import annotate_package
from excel_to_skill.cli import _convert_one, main
from excel_to_skill.consume import (
    ConsumeError,
    inspect,
    overview,
    refs,
    search,
)
from excel_to_skill.meta import _converter_version

FX_DIR = Path(__file__).parent / "fixtures"

_SHEET_OK = json.dumps({
    "name": "Data", "purpose": "합계 표", "evidence": ["Data!A1"], "confidence": 0.9,
    "sections": [{
        "range": "A1:B5", "semantic_type": "table_header",
        "evidence": ["Data!A1:B1"], "confidence": 0.8,
    }],
}, ensure_ascii=False)
_WB_OK = json.dumps({
    "workbook_claims": [{"claim": "표 하나", "evidence": ["Data!A1:B5"], "confidence": 0.9}]
}, ensure_ascii=False)


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, *, system, user, schema=None):
        return self.responses.pop(0)


def _pkg(tmp_path: Path, fx: str = "fx1_merge_formula.xlsx") -> Path:
    return _convert_one(FX_DIR / fx, tmp_path, force=True, cv=_converter_version())


def _first_string_cell(pkg: Path, sheet: str) -> dict | None:
    for c in inspect(pkg, sheet=sheet)["cells"]:
        if isinstance(c["value"], str) and c["value"].strip():
            return c
    return None


# ── overview ─────────────────────────────────────────────────
def test_overview_structure_and_no_interpretation_without_semantics(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    ov = overview(pkg)
    assert [s["name"] for s in ov["sheets"]] == ["Data"]
    assert ov["counts"]["cells"] > 0
    assert ov["annotation"]["present"] is False
    assert "interpretation" not in ov  # semantics 없으면 해석 계층 미노출(M2 조건)


def test_overview_has_no_raw_cell_values(tmp_path: Path) -> None:
    """개요는 셀 원문을 담지 않는다(단계 조회 강제)."""
    pkg = _pkg(tmp_path)
    cell = _first_string_cell(pkg, "Data")
    assert cell is not None
    assert cell["value"] not in json.dumps(overview(pkg), ensure_ascii=False)


def test_overview_draft_hides_interpretation_content(tmp_path: Path) -> None:
    """draft는 상태·건수만 — 목적·claim 텍스트 등 내용은 은닉(review 계약 준수)."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))  # draft
    ov = overview(pkg)
    assert "interpretation" not in ov
    st = ov["interpretation_status"]
    assert st["review_status"] == "draft"
    assert st["workbook_claims"] == 1 and st["sheets_annotated"] == 1
    assert st["sections_total"] == 1
    dump = json.dumps(ov, ensure_ascii=False)
    assert "합계 표" not in dump and "표 하나" not in dump  # 내용 미노출


def test_overview_approved_exposes_summary_only(tmp_path: Path) -> None:
    """approved만 해석 노출, 그것도 워크북 주장 + 시트 purpose·구간수·의미유형 집계까지만."""
    from excel_to_skill import review

    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    review.approve(pkg)
    interp = overview(pkg)["interpretation"]
    assert interp["review_status"] == "approved"
    assert interp["workbook_claims"][0]["claim"] == "표 하나"
    sh = interp["sheets"][0]
    assert sh["name"] == "Data" and sh["purpose"] == "합계 표"
    assert sh["section_count"] == 1 and sh["semantic_types"] == {"table_header": 1}
    assert "sections" not in sh  # 구간 상세는 워크북 개요에 없음(--sheet로 분리)


def test_overview_rejected_hides_content(tmp_path: Path) -> None:
    from excel_to_skill import review

    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    review.reject(pkg, note="반려")
    ov = overview(pkg)
    assert "interpretation" not in ov
    assert ov["interpretation_status"] == {"review_status": "rejected"}
    assert "합계 표" not in json.dumps(ov, ensure_ascii=False)


def test_overview_sheet_detail_gated_on_approved(tmp_path: Path) -> None:
    from excel_to_skill import review

    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    d = overview(pkg, sheet="Data")  # draft → 상세 비노출
    assert d["review_status"] == "draft" and d["sections"] == []
    review.approve(pkg)
    a = overview(pkg, sheet="Data")  # approved → 구간 상세
    assert a["review_status"] == "approved" and a["total_sections"] == 1
    assert a["sections"][0]["range"] == "A1:B5"
    assert a["sections"][0]["evidence"] == ["Data!A1:B1"]


def test_overview_unknown_sheet_raises(tmp_path: Path) -> None:
    with pytest.raises(ConsumeError):
        overview(_pkg(tmp_path), sheet="Nope")


# ── inspect ──────────────────────────────────────────────────
def test_inspect_range_filters_and_keeps_min_fields(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    res = inspect(pkg, sheet="Data", range="A1:B2")
    assert res["sheet"] == "Data" and res["range"] == "A1:B2"
    from openpyxl.utils import coordinate_to_tuple
    for c in res["cells"]:
        r, col = coordinate_to_tuple(c["cell"])
        assert 1 <= r <= 2 and 1 <= col <= 2  # 범위 밖 셀 없음
        assert {"sheet", "cell", "value", "formula"} <= set(c)  # 최소 필드 유지


def test_inspect_limit_budget_truncates(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    full = inspect(pkg, sheet="Data")
    assert full["total_in_range"] >= 2  # fx1은 셀이 여럿
    res = inspect(pkg, sheet="Data", limit=1)
    assert res["returned"] == 1 and res["truncated"] is True
    assert res["total_in_range"] == full["total_in_range"]


def test_inspect_unknown_sheet_raises(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    with pytest.raises(ConsumeError):
        inspect(pkg, sheet="Nope")


# ── search ───────────────────────────────────────────────────
def test_search_finds_value_substring_and_caps(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    cell = _first_string_cell(pkg, "Data")
    assert cell is not None
    q = cell["value"][:2]  # 값의 앞 2글자로 부분일치
    res = search(pkg, query=q)
    assert res["total_matches"] >= 1
    assert any(m["cell"] == cell["cell"] for m in res["matches"])
    capped = search(pkg, query=q, limit=1)
    assert capped["returned"] <= 1


def test_search_empty_query_raises(tmp_path: Path) -> None:
    with pytest.raises(ConsumeError):
        search(_pkg(tmp_path), query="")


# ── refs ─────────────────────────────────────────────────────
def test_refs_directional(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    edges = json.loads((pkg / "data/references.json").read_text(encoding="utf-8"))["edges"]
    assert edges  # fx1은 수식 엣지 보유
    e = edges[0]
    out = refs(pkg, cell=e["from"])
    assert any(x["to"] == e["to"] for x in out["outgoing"])  # 출력 방향
    # to가 단일 셀이면 그 셀의 incoming에 원 from이 있어야
    if "!" in e["to"] and ":" not in e["to"].split("!", 1)[1]:
        inc = refs(pkg, cell=e["to"])
        assert any(x["from"] == e["from"] for x in inc["incoming"])


def test_refs_requires_absolute_cell(tmp_path: Path) -> None:
    with pytest.raises(ConsumeError):
        refs(_pkg(tmp_path), cell="A1")  # 시트 접두 없음


# ── 출력 예산(하드 상한) ──────────────────────────────────────
def test_limit_clamped_to_hard_and_positive() -> None:
    from excel_to_skill import consume

    assert consume._clamp_limit(10 ** 9, 100) == consume.HARD_LIMIT
    assert consume._clamp_limit(-5, 100) == 1  # 음수 → 1(빈 결과 아님)
    assert consume._clamp_limit(None, 100) == 100
    with pytest.raises(ConsumeError):
        consume._clamp_limit("x", 100)


def test_inspect_negative_limit_returns_at_least_one(tmp_path: Path) -> None:
    res = inspect(_pkg(tmp_path), sheet="Data", limit=-3)
    assert res["returned"] >= 1  # 음수 limit이 조용한 빈 결과가 되지 않음


def test_inspect_huge_limit_does_not_dump(tmp_path: Path) -> None:
    from excel_to_skill.consume import HARD_LIMIT

    res = inspect(_pkg(tmp_path), sheet="Data", limit=10 ** 9)
    assert res["returned"] <= HARD_LIMIT


# ── 잘못된 입력·손상 파일(크래시/조용한 빈 결과 금지) ──────────
def test_inspect_bad_range_raises(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    with pytest.raises(ConsumeError):
        inspect(pkg, sheet="Data", range="A:B")  # 전열 범위 불가
    with pytest.raises(ConsumeError):
        inspect(pkg, sheet="Data", cell="A")  # 완전한 셀 아님


def test_search_unknown_sheet_raises(tmp_path: Path) -> None:
    with pytest.raises(ConsumeError):
        search(_pkg(tmp_path), query="x", sheet="Nope")


def test_missing_cells_jsonl_raises(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    (pkg / "data/cells.jsonl").unlink()
    with pytest.raises(ConsumeError):
        overview(pkg)
    with pytest.raises(ConsumeError):
        inspect(pkg, sheet="Data")


def test_broken_meta_json_raises(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    (pkg / "meta.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConsumeError):
        overview(pkg)


def test_corrupt_cells_jsonl_line_raises(tmp_path: Path) -> None:
    """손상 JSONL 행은 조용히 건너뛰지 않고 오류."""
    pkg = _pkg(tmp_path)
    p = pkg / "data/cells.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + "{broken line\n", encoding="utf-8")
    with pytest.raises(ConsumeError):
        inspect(pkg, sheet="Data")


# ── 출력 필드(형식·서식) ──────────────────────────────────────
def test_cell_view_omits_null_and_false_aux(tmp_path: Path) -> None:
    """보조 필드는 있을 때만(null·false 생략). value·formula만 null 허용."""
    cells = inspect(_pkg(tmp_path), sheet="Data")["cells"]
    for c in cells:
        assert {"sheet", "cell", "value", "formula"} <= set(c)
        for k, v in c.items():
            if k in ("value", "formula"):
                continue
            assert v is not None and v is not False
    assert any(c.get("formula") for c in cells)  # 수식 셀 존재(fx1)
    assert any(len(c) > 4 for c in cells)  # 보조 필드 실린 셀 존재(병합·형식 등)


# ── 승인판 SKILL 크기 제한 ────────────────────────────────────
def test_approved_skill_is_summary_not_full_dump(tmp_path: Path) -> None:
    from excel_to_skill import review
    from excel_to_skill.emit_skill_md import build_skill_md_from_package

    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    review.approve(pkg)
    skill = build_skill_md_from_package(pkg)
    assert "## ⑥ 해석 (승인됨)" in skill
    assert "--sheet" in skill  # 상세는 소비 명령으로 안내
    assert "구간 1개" in skill  # 요약(구간 수)만
    assert "구간 `A1:B5`" not in skill  # 구간별 상세 렌더 제거
    assert "table_header" not in skill  # 의미유형 상세 미노출


# ── CLI 배선 ─────────────────────────────────────────────────
def test_cli_consume_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pkg = _pkg(tmp_path)
    assert main(["overview", str(pkg)]) == 0
    ov = json.loads(capsys.readouterr().out)
    assert ov["sheets"] and ov["annotation"]["present"] is False

    assert main(["inspect", str(pkg), "--sheet", "Data", "--range", "A1:B2"]) == 0
    ins = json.loads(capsys.readouterr().out)
    assert ins["sheet"] == "Data" and "cells" in ins

    assert main(["inspect", str(pkg), "--sheet", "Nope"]) == 1  # 없는 시트 → exit 1

    assert main(["refs", str(pkg), "--cell", "A1"]) == 1  # 절대주소 아님 → exit 1
