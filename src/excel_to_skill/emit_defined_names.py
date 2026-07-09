"""§4.0 data/defined_names_full.json 방출기 — 정의된 이름 전량 덤프 (결정론 계층).

`--full-names` 지정 시에만 방출한다. diagnostics.defined_names는 샘플 ≤20으로
요약하지만, 감사계약 파일은 전역 1,363 + 시트 594 = 1,957개(#REF! 429·레거시
경로 32)라 요약으로는 다 못 본다. 이 파일이 전건을 담는다.

P7(오너 확정): value는 **전문**을 내보내되 이메일만 mask_pii로 마스킹한다.
"full"은 마스킹 해제가 아니라 "전건 + 값 전문"이라는 뜻이다. #REF!·레거시 경로는
마스킹 대상이 아니므로 그대로 둔다. name은 diagnostics 샘플과 동일하게 원문 유지.

순서(오너 확정): names는 **추출 순서 그대로** — 원본 workbook의 이름표 순서를
보존해 감사 추적에 맞춘다(별도 정렬 없음). 카운트 4종은 diagnostics.defined_names와
같은 값이 되도록 같은 _name_flags로 도출한다(단일 출처).

P2 직렬화: 고정 필드 순서, indent=2·ensure_ascii=False·allow_nan=False·끝 개행.
"""
from __future__ import annotations

import json
from pathlib import Path

from .emit_diag import _name_flags
from .emit_refs import mask_pii
from .extractor import WorkbookIR


def build_defined_names_full(ir: WorkbookIR) -> dict:
    """defined_names_full.json 문서(dict)를 만든다. 필드 순서 고정."""
    names: list[dict] = []
    global_total = sheet_scoped_total = 0
    broken_ref_count = legacy_path_count = 0

    for dn in ir.defined_names:
        if dn.scope is None:
            global_total += 1
        else:
            sheet_scoped_total += 1
        flags = _name_flags(dn.value or "")  # diagnostics와 동일 규칙
        if "broken_ref" in flags:
            broken_ref_count += 1
        if "legacy_path" in flags:
            legacy_path_count += 1
        names.append(
            {
                "name": dn.name,
                "scope": dn.scope,  # None = 전역, 아니면 시트명
                "value": None if dn.value is None else mask_pii(dn.value),
                "flags": flags,
            }
        )

    return {
        "global_total": global_total,
        "sheet_scoped_total": sheet_scoped_total,
        "broken_ref_count": broken_ref_count,
        "legacy_path_count": legacy_path_count,
        "names": names,
    }


def write_defined_names_full(ir: WorkbookIR, out_path: Path) -> dict:
    """defined_names_full.json을 쓰고 문서(dict)를 반환한다."""
    doc = build_defined_names_full(ir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    return doc
