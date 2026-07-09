"""§4.6 data/diagnostics.json 방출기 — WorkbookIR → 구조 진단 (결정론 계층).

전 항목이 기계적 사실(P3). 권고·의견 문장("확인 필요" 등)은 넣지 않는다.

- loader_path: 어느 로더 경로로 열렸는지(§5).
- external_links: 외부 링크 개수 + 표본(≤20, P7 마스킹). None(관찰 불가, xls)이면
  count=null로 두어 "관찰 불가"와 "0개"를 구분한다(P6).
- defined_names: 전역/시트 스코프 이원 집계(V6). broken_ref_count는 값에 `#REF!`
  포함, legacy_path_count는 값에 옛 파일 경로(드라이브·UNC·file://) 포함.
- pii_suspects: emails_masked는 정의이름 값·외부 링크에서 발견한 이메일(마스킹).
  legacy_paths_count는 **본문 셀에서 발견된** 경로 수 — 정의이름 경로와 의미를
  가른다. 이 단계는 정의이름만 훑으므로 본문 셀 경로는 0(정의이름 경로는
  defined_names.legacy_path_count에 이미 셌다). 같은 값을 양쪽에 넣지 말 것.
- blank_source_formulas: "값·수식 있는 셀이 빈 셀을 참조"하는 목록. edges 조인으로
  구한다 — 범위 참조는 제외, 대상 주소가 원장에 아예 없는 경우(완전 빈 셀)도 포함.
- hidden: 숨김 시트 이름 + 숨김 행·열 총계.
- truncations: --max-rows 절단 기록. 절단 로직은 뒤 단계라 지금은 빈 배열.
- format_limitations: 형식 한계 문자열(xls) 또는 null(xlsx).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .emit_refs import build_references, mask_pii
from .extractor import WorkbookIR

_BROKEN_REF_RE = re.compile(r"#REF!")
# 옛 파일 경로: 드라이브(C:\), UNC(\\서버), file:// URI. 감사계약 정의이름에서 32건.
_LEGACY_PATH_RE = re.compile(r"[A-Za-z]:\\|\\\\|file://")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")

_SAMPLE_CAP = 20
_VALUE_HEAD = 60


def _name_flags(value: str) -> list[str]:
    flags: list[str] = []
    if _BROKEN_REF_RE.search(value):
        flags.append("broken_ref")
    if _LEGACY_PATH_RE.search(value):
        flags.append("legacy_path")
    return flags


def build_diagnostics(ir: WorkbookIR, references: dict | None = None) -> dict:
    """diagnostics.json 문서(dict)를 만든다. 필드 순서는 §4.6 스키마 고정."""
    if references is None:
        references = build_references(ir)

    # ── 외부 링크 ─────────────────────────────────────────────
    links = ir.external_links
    if links is None:  # P6: 관찰 불가(xls) ≠ 0개
        external = {"count": None, "targets_sample": []}
    else:
        external = {
            "count": len(links),
            "targets_sample": [mask_pii(x) for x in links[:_SAMPLE_CAP]],
        }

    # ── 정의된 이름 이원 집계 ─────────────────────────────────
    global_total = sheet_scoped_total = 0
    broken_ref_count = legacy_path_count = 0
    emails_masked: list[str] = []
    seen_emails: set[str] = set()
    flagged: list[dict] = []
    plain: list[dict] = []

    for dn in ir.defined_names:
        if dn.scope is None:
            global_total += 1
        else:
            sheet_scoped_total += 1
        value = dn.value or ""
        if _BROKEN_REF_RE.search(value):
            broken_ref_count += 1
        if _LEGACY_PATH_RE.search(value):
            legacy_path_count += 1
        for m in _EMAIL_RE.finditer(value):
            masked = mask_pii(m.group(0))
            if masked not in seen_emails:
                seen_emails.add(masked)
                emails_masked.append(masked)

        flags = _name_flags(value)
        sample = {
            "name": dn.name,
            "value_head": mask_pii(value)[:_VALUE_HEAD],
            "flags": flags,
        }
        (flagged if flags else plain).append(sample)

    # 표본: 플래그 달린 것 우선, 등장 순 유지, 상한 20(둘 다 결정론).
    samples = (flagged + plain)[:_SAMPLE_CAP]

    # 외부 링크에서도 이메일 수집(마스킹)
    for x in links or []:
        for m in _EMAIL_RE.finditer(x):
            masked = mask_pii(m.group(0))
            if masked not in seen_emails:
                seen_emails.add(masked)
                emails_masked.append(masked)

    # ── 빈 칸 참조 수식 ───────────────────────────────────────
    lut: dict[str, dict[str, object]] = {}
    for sh in ir.sheets:
        lut[sh.name] = {c.coord: c for c in sh.cells.values()}
    blank_source: list[dict] = []
    for e in references.get("edges", []):
        if e["ref_type"] == "range":
            continue
        to = e["to"]
        sheet, _, coord = to.partition("!")
        cell = lut.get(sheet, {}).get(coord)
        if cell is None or not cell.has_content:  # 완전 빈 셀 포함
            blank_source.append({"cell": e["from"], "source": to})

    # ── 숨김 ──────────────────────────────────────────────────
    hidden_sheets = [s.name for s in ir.sheets if s.state in ("hidden", "veryHidden")]
    rows_count = sum(len(s.hidden_rows) for s in ir.sheets if s.hidden_rows)
    cols_count = sum(len(s.hidden_cols) for s in ir.sheets if s.hidden_cols)

    return {
        "loader_path": ir.loader_path,
        "external_links": external,
        "defined_names": {
            "global_total": global_total,
            "sheet_scoped_total": sheet_scoped_total,
            "broken_ref_count": broken_ref_count,
            "legacy_path_count": legacy_path_count,
            "samples": samples,
            "sample_cap": _SAMPLE_CAP,
            "full_dump_present": False,
        },
        "pii_suspects": {
            "emails_masked": emails_masked,
            # 본문 셀 출처 경로 — 이 단계는 정의이름만 훑으므로 0.
            # 정의이름 경로는 defined_names.legacy_path_count에 있다(중복 금지).
            "legacy_paths_count": 0,
        },
        "blank_source_formulas": blank_source,
        "hidden": {
            "sheets": hidden_sheets,
            "rows_count": rows_count,
            "cols_count": cols_count,
        },
        "truncations": [],
        "format_limitations": ir.format_limitations,
    }


def write_diagnostics(
    ir: WorkbookIR, out_path: Path, references: dict | None = None
) -> dict:
    """diagnostics.json을 쓰고 문서를 반환한다."""
    doc = build_diagnostics(ir, references)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    return doc
