"""V9 픽스처 스냅샷 테스트 (§8.2).

자체 제작 소형 xlsx 3종(tests/fixtures/, 생성기 make_fixtures.py)을 convert해
결정론 산출을 스냅샷으로 고정하고, 픽스처별 핵심 진단 포인트를 못박는다.

스냅샷 갱신:  UPDATE_SNAPSHOTS=1 python -m pytest tests/test_v9_fixtures.py
스냅샷은 tests/snapshots/{fixture}/ 아래 사람이 diff로 읽는 텍스트(json indent 고정,
jsonl 원문). meta.json은 가변값(generated_at·converter_version) 정규화 후 비교.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from excel_to_skill.cli import _convert_one
from excel_to_skill.meta import _converter_version
from excel_to_skill.verify import verify_package

FX_DIR = Path(__file__).parent / "fixtures"
SNAP_DIR = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

FIXTURES = ["fx1_merge_formula", "fx2_refs", "fx3_slots_hidden"]
_RAW_SNAPSHOTS = ["data/cells.jsonl", "data/references.json", "data/diagnostics.json"]


# ── 공통 헬퍼 ────────────────────────────────────────────────
def _convert(stem: str, out_root: Path) -> Path:
    return _convert_one(FX_DIR / f"{stem}.xlsx", out_root, force=True, cv=_converter_version())


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _cells(pkg: Path) -> list[dict]:
    lines = (pkg / "data/cells.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def _edges(pkg: Path) -> set[tuple[str, str, str]]:
    refs = _read_json(pkg / "data/references.json")
    return {(e["from"], e["to"], e["ref_type"]) for e in refs["edges"]}


def _meta_norm(pkg: Path) -> dict:
    d = _read_json(pkg / "meta.json")
    d.pop("generated_at", None)  # 매 변환 가변
    d.pop("converter_version", None)  # 릴리스마다 가변
    return d


def _assert_snapshot(name: str, text: str) -> None:
    path = SNAP_DIR / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"스냅샷 없음: {path} — UPDATE_SNAPSHOTS=1로 생성"
    assert text == path.read_text(encoding="utf-8"), f"스냅샷 불일치: {name}"


# ── 스냅샷 고정 ──────────────────────────────────────────────
@pytest.mark.parametrize("stem", FIXTURES)
def test_snapshot(stem: str, tmp_path: Path) -> None:
    pkg = _convert(stem, tmp_path)
    for rel in _RAW_SNAPSHOTS:
        _assert_snapshot(f"{stem}/{Path(rel).name}", (pkg / rel).read_text(encoding="utf-8"))
    meta_text = json.dumps(_meta_norm(pkg), ensure_ascii=False, indent=2) + "\n"
    _assert_snapshot(f"{stem}/meta.norm.json", meta_text)


# ── verify V1·V3 ─────────────────────────────────────────────
@pytest.mark.parametrize("stem", FIXTURES)
def test_verify_v1_v3(stem: str, tmp_path: Path) -> None:
    pkg = _convert(stem, tmp_path)
    result = verify_package(pkg, source=FX_DIR / f"{stem}.xlsx")
    assert result.ok, [(c.name, c.detail) for c in result.checks if not c.ok]
    v3 = next(c for c in result.checks if c.name == "V3")
    assert v3.ok and not v3.skipped  # 원본 제공 → 실제 재현성 수행


# ── 픽스처별 핵심 진단 포인트 ────────────────────────────────
def test_fx1_merge_anchor_and_formula_edges(tmp_path: Path) -> None:
    pkg = _convert("fx1_merge_formula", tmp_path)
    cells = {f'{c["sheet"]}!{c["cell"]}': c for c in _cells(pkg)}
    # 병합 anchor는 cells에 남고 병합 자식(B1)은 빠진다
    assert cells["Data!A1"]["merged_range"] == "A1:B1"
    assert "Data!B1" not in cells
    # 셀 수식 edge ×2 + 범위 수식 edge ×1
    edges = _edges(pkg)
    assert ("Data!B4", "Data!B3", "cell") in edges
    assert ("Data!B4", "Data!B2", "cell") in edges
    assert ("Data!B5", "Data!B2:B3", "range") in edges


def test_fx2_cross_sheet_range_indirect(tmp_path: Path) -> None:
    pkg = _convert("fx2_refs", tmp_path)
    refs = _read_json(pkg / "data/references.json")
    edges = {(e["from"], e["to"], e["ref_type"]) for e in refs["edges"]}
    assert ("S2!B1", "S1!A1", "cell") in edges  # 시트간 셀 참조
    assert ("S2!B2", "S1!A1:A3", "range") in edges  # 범위 참조
    # INDIRECT는 edge가 아니라 unresolved
    assert any(u["cell"] == "S2!B3" and u["reason"] == "indirect" for u in refs["unresolved"])
    assert all("S2!B3" != e["from"] for e in refs["edges"])
    # impacts는 edges의 역인덱스
    assert refs["impacts"]["S1!A1"] == ["S2!B1"]


def test_fx3_blank_slot_and_hidden(tmp_path: Path) -> None:
    pkg = _convert("fx3_slots_hidden", tmp_path)
    cells = {f'{c["sheet"]}!{c["cell"]}': c for c in _cells(pkg)}
    # 테두리만 있는 빈 입력 슬롯이 cells에 남는다(값 없음)
    assert cells["Main!B1"]["value"] is None and cells["Main!B1"]["border"] is True
    diag = _read_json(pkg / "data/diagnostics.json")
    # 빈 칸을 참조하는 수식이 blank_source_formulas에 잡힌다
    assert {"cell": "Main!B3", "source": "Main!B1"} in diag["blank_source_formulas"]
    # 숨김 시트·행·열
    assert "숨김시트" in diag["hidden"]["sheets"]
    assert diag["hidden"]["rows_count"] >= 1
    assert diag["hidden"]["cols_count"] >= 1
