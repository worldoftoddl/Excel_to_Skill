"""§4.5 data/references.json 방출기 — WorkbookIR → 참조 그래프 (결정론 계층).

- edges: 통합문서 내부 셀→셀(범위) 참조. 시트 순 → row → col → 수식 내 등장
  순으로 방출, (from, to, ref_type) 중복은 첫 등장만 남긴다(결정론 P2).
- impacts: edges의 파생 역인덱스(§4.5 ⑥) — "이 셀을 누가 참조하나".
  키는 문자열 정렬, 값은 edges 방출 순서.
- external_refs: `[n]` 참조를 외부 링크 테이블과 조인(§4.5 ④). 색인이 링크
  테이블 범위를 벗어나면 target은 null(P6 — 관찰 불가). target에는 P7
  이메일 마스킹을 적용한다.
- unresolved: INDIRECT/OFFSET 포함 수식(§4.5 ⑤) — 동적 목표는 풀지 않고
  사실만 기록. 같은 수식의 정적 참조(예: OFFSET(A1,…)의 A1)는 edges에도
  남는다 — 정적 인자는 관찰된 사실이다.
- observability: P6 3상태. xls(xlrd)는 수식 원문 관찰 불가 → unavailable_xls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .extractor import WorkbookIR
from .refparse import parse_formula

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@")


def mask_pii(text: str) -> str:
    """P7: 이메일 로컬파트 마스킹(j***@domain). emit_diag도 재사용 예정."""
    return _EMAIL_RE.sub(r"\1***@", text)


_UNRESOLVED_FUNCS = (("INDIRECT(", "indirect"), ("OFFSET(", "offset"))


def build_references(ir: WorkbookIR) -> dict:
    """references.json 문서(dict)를 만든다. 필드·순서는 §4.5 스키마 고정."""
    if ir.format == "xls":
        return {
            "edges": [],
            "impacts": {},
            "external_refs": [],
            "unresolved": [],
            "observability": {
                "workbook": "unavailable_xls",
                "note": ir.format_limitations,
            },
        }

    edges: list[dict] = []
    external_refs: list[dict] = []
    unresolved: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    links = ir.external_links or []

    for sheet in ir.sheets:
        for key in sorted(sheet.cells):
            c = sheet.cells[key]
            if c.formula is None:
                continue
            from_addr = f"{sheet.name}!{c.coord}"

            upper = c.formula.upper()
            for marker, reason in _UNRESOLVED_FUNCS:
                if marker in upper:
                    unresolved.append(
                        {"cell": from_addr, "formula": c.formula, "reason": reason}
                    )

            for tok in parse_formula(c.formula):
                if tok.external_index is not None:
                    idx = tok.external_index - 1  # [n]은 1부터
                    target = (
                        mask_pii(links[idx]) if 0 <= idx < len(links) else None
                    )
                    external_refs.append(
                        {"cell": from_addr, "raw": tok.raw, "target": target}
                    )
                    continue
                to_addr = f"{tok.sheet or sheet.name}!{tok.coord}"
                edge_key = (from_addr, to_addr, tok.ref_type)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edges.append(
                    {
                        "from": from_addr,
                        "to": to_addr,
                        "formula": c.formula,
                        "ref_type": tok.ref_type,
                    }
                )

    impacts: dict[str, list[str]] = {}
    for e in edges:
        impacts.setdefault(e["to"], []).append(e["from"])

    return {
        "edges": edges,
        "impacts": {k: impacts[k] for k in sorted(impacts)},
        "external_refs": external_refs,
        "unresolved": unresolved,
        "observability": {"workbook": "full", "note": None},
    }


def write_references(ir: WorkbookIR, out_path: Path) -> dict:
    """references.json을 쓰고 문서를 반환한다."""
    doc = build_references(ir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    return doc
