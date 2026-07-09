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

from excel_to_skill import cache
from excel_to_skill.cli import _convert_one
from excel_to_skill.meta import _converter_version
from excel_to_skill.verify import verify_package

FX_DIR = Path(__file__).parent / "fixtures"
SNAP_DIR = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

FIXTURES = ["fx1_merge_formula", "fx2_refs", "fx3_slots_hidden", "fx4_defined_names"]
_RAW_SNAPSHOTS = ["data/cells.jsonl", "data/references.json", "data/diagnostics.json"]


# ── 공통 헬퍼 ────────────────────────────────────────────────
def _convert(stem: str, out_root: Path, *, full_names: bool = False) -> Path:
    return _convert_one(
        FX_DIR / f"{stem}.xlsx",
        out_root,
        force=True,
        cv=_converter_version(),
        full_names=full_names,
    )


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


def _skill_norm(pkg: Path) -> str:
    """SKILL.md에서 converter_version 줄만 placeholder로 치환(릴리스마다 가변).

    나머지(sha12·name·description·머리 텍스트·구성·진단)는 픽스처 바이트 기반
    결정론이라 그대로 스냅샷 대상. meta.norm과 같은 '버전값만 정규화' 취급.
    """
    out = []
    for line in (pkg / "SKILL.md").read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("- converter_version: "):
            out.append("- converter_version: `<normalized>`\n")
        else:
            out.append(line)
    return "".join(out)


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
    # layout HTML — 가변값 없음, 정렬 glob 원문 그대로 고정
    for html in sorted((pkg / "layout").glob("*.html")):
        _assert_snapshot(f"{stem}/layout/{html.name}", html.read_text(encoding="utf-8"))
    # SKILL.md — converter_version만 정규화(meta.norm과 같은 취급)
    _assert_snapshot(f"{stem}/SKILL.norm.md", _skill_norm(pkg))


# ── verify V1·V3 ─────────────────────────────────────────────
@pytest.mark.parametrize("stem", FIXTURES)
def test_verify_v1_v3(stem: str, tmp_path: Path) -> None:
    pkg = _convert(stem, tmp_path)
    result = verify_package(pkg, source=FX_DIR / f"{stem}.xlsx")
    assert result.ok, [(c.name, c.detail) for c in result.checks if not c.ok]
    v3 = next(c for c in result.checks if c.name == "V3")
    assert v3.ok and not v3.skipped  # 원본 제공 → 실제 재현성 수행


# ── --full-names 전량 덤프(§4.0) ─────────────────────────────
def test_full_names_dump_and_flag(tmp_path: Path) -> None:
    """fx4를 --full-names로 변환 — 덤프·full_dump_present·교차검증·V3."""
    pkg = _convert("fx4_defined_names", tmp_path, full_names=True)

    # 전량 덤프 존재 + 결정론 스냅샷(합성 데이터, 이메일은 마스킹돼 유출 없음)
    dnf = pkg / "data/defined_names_full.json"
    assert dnf.is_file()
    _assert_snapshot(
        "fx4_defined_names/defined_names_full.json", dnf.read_text(encoding="utf-8")
    )

    # diagnostics.full_dump_present == True, meta 자기증언
    diag = _read_json(pkg / "data/diagnostics.json")
    assert diag["defined_names"]["full_dump_present"] is True
    meta = _read_json(pkg / "meta.json")
    assert meta["conversion_params"]["full_names"] is True

    # 덤프 카운트 == diagnostics 카운트(단일 출처), 이원 집계·플래그
    doc = _read_json(dnf)
    for k in ("global_total", "sheet_scoped_total", "broken_ref_count", "legacy_path_count"):
        assert doc[k] == diag["defined_names"][k]
    assert doc["global_total"] == 4 and doc["sheet_scoped_total"] == 1
    assert doc["broken_ref_count"] == 1 and doc["legacy_path_count"] == 1
    # 이메일 P7 마스킹(원문 미노출)
    assert any(n["value"] == '"u***@example.com"' for n in doc["names"])
    assert not any(n["value"] and "user@example.com" in n["value"] for n in doc["names"])

    # verify: full_names 체크 통과 + V1/V3 통과
    result = verify_package(pkg, source=FX_DIR / "fx4_defined_names.xlsx")
    assert result.ok, [(c.name, c.detail) for c in result.checks if not c.ok]
    fn = next(c for c in result.checks if c.name == "full_names")
    assert fn.ok and not fn.skipped


def test_full_names_off_absent(tmp_path: Path) -> None:
    """--full-names 없이 변환하면 덤프 부재 + full_dump_present False."""
    pkg = _convert("fx4_defined_names", tmp_path)  # full_names=False
    assert not (pkg / "data/defined_names_full.json").is_file()
    diag = _read_json(pkg / "data/diagnostics.json")
    assert diag["defined_names"]["full_dump_present"] is False


# ── M2 보정①: 캐시가 conversion_params를 본다 ────────────────
def test_cache_respects_conversion_params(tmp_path: Path) -> None:
    """옵션이 바뀌면 stale hit 없이 재생성한다(--full-names·--max-rows).

    같은 sha·같은 converter_version이어도 conversion_params가 다르면 다른 패키지다.
    캐시가 이를 무시하면 옛 옵션 패키지를 그대로 반환하는 버그가 난다.
    """
    root = tmp_path / "out"
    src = FX_DIR / "fx4_defined_names.xlsx"
    cv = _converter_version()

    # 1) full_names=False로 첫 변환(miss)
    p1 = _convert_one(src, root, force=False, cv=cv, full_names=False)
    assert not (p1 / "data/defined_names_full.json").is_file()
    # 2) 같은 옵션 재변환 → hit, 덤프 여전히 없음
    p2 = _convert_one(src, root, force=False, cv=cv, full_names=False)
    assert p2 == p1 and not (p2 / "data/defined_names_full.json").is_file()
    # 3) full_names=True로 변환 → params_changed miss라 재생성, 덤프가 생겨야 정상
    #    (stale hit이었다면 이전 패키지를 반환해 덤프가 없었을 것)
    p3 = _convert_one(src, root, force=False, cv=cv, full_names=True)
    assert p3 == p1  # 같은 sha → 같은 폴더
    assert (p3 / "data/defined_names_full.json").is_file()

    # probe 사유 직접 확인: 색인엔 지금 full_names=True 항목 → False로 조회하면 params_changed
    pr = cache.probe(
        root, src, converter_version=cv,
        conversion_params={"max_rows": 5000, "full_names": False},
    )
    assert not pr.hit and pr.reason == "params_changed"
    # 색인 항목에 conversion_params가 실제로 기록됐는지
    entry = cache.load_index(root)["entries"][pr.package_dir]
    assert entry["conversion_params"] == {"max_rows": 5000, "full_names": True}


# ── M2 보정②: verify가 SKILL.md·layout 훼손을 잡는다 ─────────
def test_verify_covers_skill_and_layout(tmp_path: Path) -> None:
    """V1은 SKILL.md·layout 존재를, V3는 그 내용 재현성을 검증한다."""
    import shutil

    src = FX_DIR / "fx1_merge_formula.xlsx"
    pkg = _convert("fx1_merge_formula", tmp_path)
    assert verify_package(pkg, source=src).ok  # 정상 패키지는 통과

    def _check(p: Path, name: str):
        return next(c for c in verify_package(p, source=src).checks if c.name == name)

    # SKILL.md 내용 훼손 → V3 실패(detail에 SKILL.md)
    t1 = tmp_path / "t1"; shutil.copytree(pkg, t1)
    (t1 / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    c1 = _check(t1, "V3")
    assert not c1.ok and "SKILL.md" in c1.detail

    # layout html 내용 훼손 → V3 실패(detail에 layout/)
    t2 = tmp_path / "t2"; shutil.copytree(pkg, t2)
    next((t2 / "layout").glob("*.html")).write_text("<x>t</x>", encoding="utf-8")
    c2 = _check(t2, "V3")
    assert not c2.ok and "layout/" in c2.detail

    # SKILL.md 삭제 → files 실패 + source를 줘도 크래시 없이 V3 생략(선행 실패)
    t3 = tmp_path / "t3"; shutil.copytree(pkg, t3)
    (t3 / "SKILL.md").unlink()
    res3 = verify_package(t3, source=src)  # 과거 FileNotFoundError로 크래시하던 경로
    assert not res3.ok
    f3 = next(c for c in res3.checks if c.name == "files")
    assert not f3.ok and "SKILL.md" in f3.detail
    assert next(c for c in res3.checks if c.name == "V3").skipped

    # layout html 전부 삭제 → files 실패 + source 줘도 크래시 없이 V3 생략
    t4 = tmp_path / "t4"; shutil.copytree(pkg, t4)
    for h in (t4 / "layout").glob("*.html"):
        h.unlink()
    res4 = verify_package(t4, source=src)
    assert not res4.ok
    f4 = next(c for c in res4.checks if c.name == "files")
    assert not f4.ok and "layout" in f4.detail
    assert next(c for c in res4.checks if c.name == "V3").skipped


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
