from __future__ import annotations

import json
from pathlib import Path

import pytest

from excel_to_skill.audit import AuditModelError
from excel_to_skill.audit.sources import WorkbookSourceResolver


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    (pkg / "meta.json").write_text(
        json.dumps({"sheets": [{"name": "Main", "dimensions": "A1:C5"}]}),
        encoding="utf-8",
    )
    cells = [
        {"sheet": "Main", "cell": "A1", "row": 1, "col": 1,
         "value": "제목", "formula": None, "border": False},
        {"sheet": "Main", "cell": "B2", "row": 2, "col": 2,
         "value": None, "formula": None, "border": True},
        {"sheet": "Main", "cell": "C5", "row": 5, "col": 3,
         "value": None, "formula": "SUM(A1:B2)", "border": False},
    ]
    (pkg / "data" / "cells.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cells),
        encoding="utf-8",
    )
    return pkg


def test_resolve_requires_actual_ledger_record_and_binds_digest(tmp_path: Path) -> None:
    resolver = WorkbookSourceResolver(_package(tmp_path))
    resolved = resolver.resolve("Main!A1:B2")
    assert resolved.cell_count == 2
    assert len(resolved.content_sha256) == 64
    source = resolved.to_dict(role="narrative")
    assert source == {
        "id": source["id"],
        "kind": "workbook",
        "sheet": "Main",
        "range": "A1:B2",
        "role": "narrative",
        "content_sha256": resolved.content_sha256,
    }
    assert source["id"].startswith("source:")


def test_style_only_slot_is_a_resolvable_source(tmp_path: Path) -> None:
    resolver = WorkbookSourceResolver(_package(tmp_path))
    resolved = resolver.resolve("Main!B2")
    assert resolved.cell_count == 1
    assert resolver.cells_for("Main!B2")[0]["border"] is True


def test_digest_changes_when_resolved_content_changes(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    before = WorkbookSourceResolver(pkg).resolve("Main!A1").content_sha256
    ledger = pkg / "data" / "cells.jsonl"
    text = ledger.read_text(encoding="utf-8").replace("제목", "변경된 제목")
    ledger.write_text(text, encoding="utf-8")
    after = WorkbookSourceResolver(pkg).resolve("Main!A1").content_sha256
    assert after != before


@pytest.mark.parametrize(
    "ref",
    [
        "A1",
        "Unknown!A1",
        "Main!A3",
        "Main!A:A",
        "Main!not-a-cell",
    ],
)
def test_resolve_rejects_unbound_or_invalid_sources(tmp_path: Path, ref: str) -> None:
    resolver = WorkbookSourceResolver(_package(tmp_path))
    with pytest.raises(AuditModelError):
        resolver.resolve(ref)
