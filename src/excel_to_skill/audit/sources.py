"""Workbook-source resolution for audit facts.

The legacy V2 check only proves that a coordinate falls inside a worksheet's used range.  Audit
facts need a stronger invariant: every cited range must resolve to at least one emitted ledger
record, and the exact resolved records are bound by a deterministic digest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openpyxl.utils import range_boundaries

from .model import AuditModelError, json_sha256, require_non_empty


_DIGEST_FIELDS = (
    "sheet",
    "cell",
    "row",
    "col",
    "value",
    "formula",
    "cached_value",
    "data_type",
    "number_format",
    "merged_range",
    "bold",
    "border",
    "fill",
)


@dataclass(frozen=True, slots=True)
class ResolvedWorkbookSource:
    """A workbook locator bound to concrete deterministic cell-ledger records."""

    ref: str
    sheet: str
    cell_range: str
    cell_count: int
    content_sha256: str

    def to_dict(self, *, role: str, source_id: str | None = None) -> dict:
        role = require_non_empty(role, field="role")
        if source_id is None:
            identity = {
                'sheet': self.sheet,
                'range': self.cell_range,
                'role': role,
                'content_sha256': self.content_sha256,
            }
            source_id = "source:" + json_sha256(identity)[:20]
        return {
            "id": require_non_empty(source_id, field="source_id"),
            "kind": "workbook",
            "sheet": self.sheet,
            "range": self.cell_range,
            "role": role,
            "content_sha256": self.content_sha256,
        }


class WorkbookSourceResolver:
    """Resolve absolute worksheet addresses against a package's ``cells.jsonl`` ledger."""

    def __init__(self, pkg: Path | str) -> None:
        self.pkg = Path(pkg)
        self.meta = self._load_json("meta.json")
        sheets = self.meta.get("sheets", [])
        if not isinstance(sheets, list) or any(not isinstance(s, dict) for s in sheets):
            raise AuditModelError("meta.sheets가 객체 배열이 아닙니다.")
        self.sheet_names = {
            s.get("name") for s in sheets if isinstance(s.get("name"), str)
        }
        self.cells = self._load_cells()

    def _load_json(self, rel: str) -> dict:
        path = self.pkg / rel
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise AuditModelError(f"{rel} 없음: {self.pkg}") from e
        except json.JSONDecodeError as e:
            raise AuditModelError(f"{rel} JSON 파싱 실패: {e}") from e
        if not isinstance(doc, dict):
            raise AuditModelError(f"{rel}은 JSON 객체여야 합니다.")
        return doc

    def _load_cells(self) -> dict[str, list[dict]]:
        path = self.pkg / "data" / "cells.jsonl"
        if not path.is_file():
            raise AuditModelError("data/cells.jsonl 없음")
        grouped: dict[str, list[dict]] = {}
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    cell = json.loads(line)
                except json.JSONDecodeError as e:
                    raise AuditModelError(f"cells.jsonl {lineno}행 파싱 실패: {e}") from e
                if not isinstance(cell, dict):
                    raise AuditModelError(f"cells.jsonl {lineno}행은 JSON 객체여야 합니다.")
                sheet = cell.get("sheet")
                if not isinstance(sheet, str):
                    raise AuditModelError(f"cells.jsonl {lineno}행 sheet가 문자열이 아닙니다.")
                if not isinstance(cell.get("row"), int) or not isinstance(cell.get("col"), int):
                    raise AuditModelError(f"cells.jsonl {lineno}행 row/col이 정수가 아닙니다.")
                grouped.setdefault(sheet, []).append(cell)
        for cells in grouped.values():
            cells.sort(key=lambda c: (c["row"], c["col"], c.get("cell", "")))
        return grouped

    @staticmethod
    def _parse(ref: str) -> tuple[str, tuple[int, int, int, int]]:
        ref = require_non_empty(ref, field="workbook source ref")
        if "!" not in ref:
            raise AuditModelError(f"workbook source는 'Sheet!A1' 절대주소여야 합니다: {ref!r}")
        sheet, coord = ref.rsplit("!", 1)
        if not sheet:
            raise AuditModelError(f"workbook source의 시트명이 비었습니다: {ref!r}")
        try:
            bounds = range_boundaries(coord)
        except (TypeError, ValueError) as e:
            raise AuditModelError(f"workbook source 주소 파싱 실패: {ref!r}") from e
        if any(value is None for value in bounds):
            raise AuditModelError(f"전행/전열 범위는 허용하지 않습니다: {ref!r}")
        return sheet, bounds

    def resolve(self, ref: str) -> ResolvedWorkbookSource:
        sheet, bounds = self._parse(ref)
        min_col, min_row, max_col, max_row = bounds
        if sheet not in self.sheet_names:
            raise AuditModelError(f"workbook source의 시트가 meta에 없습니다: {sheet!r}")
        matched = [
            cell
            for cell in self.cells.get(sheet, [])
            if min_row <= cell["row"] <= max_row and min_col <= cell["col"] <= max_col
        ]
        if not matched:
            raise AuditModelError(
                f"workbook source가 cells.jsonl의 실제 레코드를 가리키지 않습니다: {ref}"
            )
        digest_payload = [
            {field: cell.get(field) for field in _DIGEST_FIELDS}
            for cell in matched
        ]
        return ResolvedWorkbookSource(
            ref=ref,
            sheet=sheet,
            cell_range=ref.rsplit("!", 1)[1],
            cell_count=len(matched),
            content_sha256=json_sha256(digest_payload),
        )

    def cells_for(self, ref: str) -> list[dict]:
        """Return resolved raw ledger records for a trusted locator (consumer trace use)."""
        sheet, (min_col, min_row, max_col, max_row) = self._parse(ref)
        if sheet not in self.sheet_names:
            raise AuditModelError(f"workbook source의 시트가 meta에 없습니다: {sheet!r}")
        matched = [
            dict(cell)
            for cell in self.cells.get(sheet, [])
            if min_row <= cell["row"] <= max_row and min_col <= cell["col"] <= max_col
        ]
        if not matched:
            raise AuditModelError(
                f"workbook source가 cells.jsonl의 실제 레코드를 가리키지 않습니다: {ref}"
            )
        return matched
