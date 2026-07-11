from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill.audit.regions import build_regions


def _package(tmp_path: Path, cells: list[dict], sheets: list[str] | None = None) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    names = sheets or list(dict.fromkeys(cell["sheet"] for cell in cells))
    (pkg / "meta.json").write_text(
        json.dumps({"sheets": [{"name": name} for name in names]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (pkg / "data" / "cells.jsonl").write_text(
        "".join(json.dumps(cell, ensure_ascii=False) + "\n" for cell in cells),
        encoding="utf-8",
    )
    return pkg


def _cell(sheet: str, row: int, col: int, *, value=None) -> dict:
    letter = chr(ord("A") + col - 1)
    return {
        "sheet": sheet,
        "cell": f"{letter}{row}",
        "row": row,
        "col": col,
        "value": value,
        "formula": None,
    }


def test_regions_cover_every_cell_and_preserve_sheet_order(tmp_path: Path) -> None:
    cells = [
        _cell("Second", 1, 1, value="later in ledger"),
        _cell("First", 1, 1, value="title"),
        _cell("First", 2, 1, value="body"),
        _cell("First", 10, 2, value="new block"),
    ]
    regions = build_regions(_package(tmp_path, cells, ["First", "Second"]), row_gap=3)

    assert [r.sheet for r in regions] == ["First", "First", "Second"]
    assert [r.cell_range for r in regions] == ["A1:A2", "B10", "A1"]
    assert sum(len(r.cells) for r in regions) == len(cells)
    assert len({c["cell"] + r.sheet for r in regions for c in r.cells}) == len(cells)


def test_regions_split_on_cell_and_row_span_caps(tmp_path: Path) -> None:
    cells = [_cell("S", row, 1, value=row) for row in range(1, 7)]
    regions = build_regions(
        _package(tmp_path, cells), max_rows=2, max_cells=3, row_gap=10
    )
    assert [r.cell_range for r in regions] == ["A1:A2", "A3:A4", "A5:A6"]


def test_wide_row_is_chunked_without_loss(tmp_path: Path) -> None:
    cells = [_cell("S", 1, col, value=col) for col in range(1, 7)]
    regions = build_regions(_package(tmp_path, cells), max_cells=2)
    assert [len(r.cells) for r in regions] == [2, 2, 2]
    assert [r.cell_range for r in regions] == ["A1:B1", "C1:D1", "E1:F1"]
    assert all(region.context_cells == () for region in regions)


def test_prompt_payload_omits_null_fields_but_keeps_false(tmp_path: Path) -> None:
    cell = _cell("S", 1, 1)
    cell.update({"bold": False, "border": True})
    region = build_regions(_package(tmp_path, [cell]))[0]
    payload = region.prompt_payload()
    assert payload["region_id"] == "region:00001"
    assert payload["cells"] == [
        {"cell": "A1", "row": 1, "col": 1, "bold": False, "border": True}
    ]
    assert payload["read_only_context"] == {
        "kind": "header_or_legend",
        "source_eligible": False,
        "context_evidence_eligible": True,
        "context_evidence_role": "label",
        "requires_current_region_source": True,
        "cells": [],
    }


def test_continuation_region_carries_three_header_rows_as_read_only_context(
    tmp_path: Path,
) -> None:
    cells = []
    for row in range(27, 30):
        for col in range(1, 18):
            cell = _cell("WP", row, col, value=f"헤더 {row}-{col}")
            cell["bold"] = True
            cells.append(cell)
    cells.extend(_cell("WP", 30, col, value=col) for col in range(1, 18))

    regions = build_regions(
        _package(tmp_path, cells), max_rows=60, max_cells=51, row_gap=3
    )

    assert [region.cell_range for region in regions] == ["A27:Q29", "A30:Q30"]
    continuation = regions[1]
    assert len(continuation.context_cells) == 51
    assert {cell["row"] for cell in continuation.context_cells} == {27, 28, 29}
    assert not (
        {(cell["row"], cell["col"]) for cell in continuation.context_cells}
        & {(cell["row"], cell["col"]) for cell in continuation.cells}
    )
    context = continuation.prompt_payload()["read_only_context"]
    assert context["source_eligible"] is False
    assert len(context["cells"]) == 51
    assert len(context["cells"]) <= 72


def test_row_gap_starts_independent_block_without_prior_context(tmp_path: Path) -> None:
    cells = [
        _cell("S", 1, 1, value="경영진 주장"),
        _cell("S", 1, 2, value="감사절차"),
        _cell("S", 10, 1, value="독립 표"),
        _cell("S", 10, 2, value="별도 데이터"),
    ]
    regions = build_regions(
        _package(tmp_path, cells), max_cells=2, row_gap=3
    )

    assert [region.cell_range for region in regions] == ["A1:B1", "A10:B10"]
    assert regions[1].context_cells == ()


def test_short_data_rows_are_not_misclassified_as_header_context(
    tmp_path: Path,
) -> None:
    cells = [
        _cell("S", 1, 1, value="고객A"),
        _cell("S", 1, 2, value="정상"),
        _cell("S", 2, 1, value="고객B"),
        _cell("S", 2, 2, value="완료"),
        _cell("S", 3, 1, value="고객C"),
        _cell("S", 3, 2, value="검사대상"),
    ]

    regions = build_regions(
        _package(tmp_path, cells), max_cells=4, row_gap=3
    )

    assert [region.cell_range for region in regions] == ["A1:B2", "A3:B3"]
    assert regions[1].context_cells == ()


def test_unstyled_assertion_legend_is_carried_to_continuation_region(
    tmp_path: Path,
) -> None:
    cells = [
        _cell("S", 1, 1, value="A"),
        _cell("S", 1, 2, value="Accuracy"),
        _cell("S", 1, 3, value="E"),
        _cell("S", 1, 4, value="Existence"),
        _cell("S", 2, 1, value="절차-1"),
        _cell("S", 2, 2, value="A"),
        _cell("S", 2, 3, value="절차-2"),
        _cell("S", 2, 4, value="E"),
    ]

    regions = build_regions(
        _package(tmp_path, cells), max_cells=4, row_gap=3
    )

    assert [cell["cell"] for cell in regions[1].context_cells] == [
        "A1", "B1", "C1", "D1"
    ]


def test_extended_unstyled_assertion_legend_codes_are_carried(
    tmp_path: Path,
) -> None:
    cells = [
        _cell("S", 1, 1, value="ACC"),
        _cell("S", 1, 2, value="Accuracy"),
        _cell("S", 1, 3, value="COMP"),
        _cell("S", 1, 4, value="Completeness"),
        _cell("S", 2, 1, value="절차-1"),
        _cell("S", 2, 2, value="ACC"),
        _cell("S", 2, 3, value="절차-2"),
        _cell("S", 2, 4, value="COMP"),
    ]

    regions = build_regions(
        _package(tmp_path, cells), max_cells=4, row_gap=3
    )

    assert [cell["cell"] for cell in regions[1].context_cells] == [
        "A1", "B1", "C1", "D1"
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_rows": 0}, "max_rows"),
        ({"max_cells": 0}, "max_rows"),
        ({"row_gap": -1}, "row_gap"),
        ({"max_context_rows": -1}, "max_context_rows"),
        ({"max_context_cells": -1}, "max_context_cells"),
    ],
)
def test_region_limits_reject_invalid_values(
    tmp_path: Path, kwargs: dict, message: str
) -> None:
    pkg = _package(tmp_path, [_cell("S", 1, 1)])
    with pytest.raises(ValueError, match=message):
        build_regions(pkg, **kwargs)
