"""§4.4 data/cells.jsonl 방출기 — WorkbookIR → 원장 JSONL (결정론 계층).

포함 규칙(§4.4): `value` 또는 `formula`가 있는 모든 셀 + 값·수식이 없어도
(a) 병합 anchor (b) 테두리 보유 (c) 배경색 보유인 셀. 병합 자식 셀과
그 외 완전 빈 셀(굵게 단독 포함)은 제외.

IR에 없는 병합 anchor(내용도 유의 서식도 없는 셀)는 값 필드 null로
합성한다. 서식 플래그는 False — IR에 없다는 것은 어느 로더 경로에서든
"서식 레코드 없음이 관찰됨"이다(read_only의 EmptyCell도 XML에 셀
레코드 자체가 없는 경우라 서식이 구조적으로 존재할 수 없다). P6의
관찰 불가(null)가 아니라 없음 관찰(False)이 맞다.

직렬화(P2): 고정 필드 순서, 시트 순 → row → col 정렬, 한 줄 = 한 셀,
ensure_ascii=False. 날짜·시각 계열(datetime/date/time)은 ISO 8601 문자열.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path

from openpyxl.utils import range_boundaries

from .extractor import CellIR, SheetIR, WorkbookIR


def _json_value(v: object) -> object:
    if isinstance(v, datetime):  # datetime은 date의 서브클래스 — 먼저 검사
        return v.isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    return v


def _merge_maps(
    sheet: SheetIR,
) -> tuple[dict[tuple[int, int], str], set[tuple[int, int]]]:
    """병합 범위 → (anchor 좌표 → 범위 문자열, 자식 좌표 집합)."""
    anchors: dict[tuple[int, int], str] = {}
    children: set[tuple[int, int]] = set()
    for ref in sheet.merged_ranges:
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        anchors[(min_row, min_col)] = ref
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                if (r, c) != (min_row, min_col):
                    children.add((r, c))
    return anchors, children


def _record(sheet_name: str, c: CellIR, merged_range: str | None) -> dict:
    # 필드 순서 고정(P2) — 지시서 §4.4 예시와 동일
    return {
        "sheet": sheet_name,
        "cell": c.coord,
        "row": c.row,
        "col": c.col,
        "value": _json_value(c.value),
        "formula": c.formula,
        "cached_value": _json_value(c.cached_value),
        "data_type": c.data_type,
        "number_format": c.number_format,
        "merged_range": merged_range,
        "bold": c.bold,
        "border": c.border,
        "fill": c.fill,
    }


def iter_cell_records(ir: WorkbookIR) -> Iterator[dict]:
    """§4.4 포함 규칙을 적용해 고정 정렬 순서로 레코드를 낸다."""
    for sheet in ir.sheets:
        anchors, children = _merge_maps(sheet)
        for key in sorted(set(sheet.cells) | set(anchors)):
            if key in children:
                continue
            merged_range = anchors.get(key)
            c = sheet.cells.get(key)
            if c is None:
                # 내용·유의 서식 없는 병합 anchor — §4.4 (a)에 따라 합성
                row, col = key
                c = CellIR(row=row, col=col, bold=False, border=False)
            elif not c.significant and merged_range is None:
                continue  # bold-only 등 비포함 셀
            yield _record(sheet.name, c, merged_range)


def write_cells_jsonl(ir: WorkbookIR, out_path: Path) -> int:
    """cells.jsonl을 쓰고 줄 수를 반환한다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in iter_cell_records(ir):
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":"),
                               allow_nan=False))
            f.write("\n")
            count += 1
    return count
