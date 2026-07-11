"""Agent 소비 인터페이스(§10) — 패키지를 '개요 → 시트 → 셀'로 **단계 조회**한다.

원본 JSON(cells.jsonl·semantics.json 등)을 통째로 컨텍스트에 붓는 것을 막고, 필요한
부분만 예산 안에서 반환하는 읽기 함수를 제공한다. 전부 **결정론**(LLM 무관)이고 패키지
파일만 읽으며, `anthropic`을 건드리지 않는다(P1 무관).

  - overview            : 셀 원문 없이 소스·시트·상태·카운트 요약. semantics가 **승인됨
                          (approved)** 일 때만 해석을 노출하되, 워크북 주장 + 시트별
                          purpose·구간 수·의미유형 집계까지만(구간 상세는 2단계 조회).
                          draft/rejected는 상태·건수만(승인 전 주석 은닉 계약 준수).
  - overview --sheet S  : 승인된 시트의 구간 상세(range·의미유형·근거·필드)를 예산 안에서.
  - inspect             : 지정 시트의 범위/셀 원장 발췌.
  - search              : 값·수식 부분일치를 상한까지.
  - refs                : 지정 셀의 출입 참조 엣지만.

모든 결과는 출력 예산(하드 상한)으로 제한하고 returned·total·truncated를 함께 알린다.
셀 레코드는 최소 sheet·cell·value·formula를 유지하고, null·false 보조 필드는 생략한다.
손상·누락·잘못된 입력은 조용한 빈 결과가 아니라 ConsumeError로 통일한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from openpyxl.utils import range_boundaries

# 출력 예산 — A/B 양측이 동일하게 쓰고, --limit로 조정하되 HARD_LIMIT를 넘지 못한다.
INSPECT_LIMIT = 200            # inspect 기본 셀 수
SEARCH_LIMIT = 30             # search 기본 매치 수
REFS_LIMIT = 100             # refs 방향별 기본 엣지 수
OVERVIEW_SECTION_LIMIT = 100  # overview --sheet 구간 기본 수
HARD_LIMIT = 2000            # 모든 --limit의 하드 상한(원장 전체 덤프 차단)


class ConsumeError(RuntimeError):
    """조회 대상이 없거나 인자가 잘못됨(파일 누락·JSON 손상·주소 문법·시트 부재 등)."""


# ── 로더(누락·손상은 ConsumeError로 통일) ─────────────────────
def _load_required(pkg: Path, rel: str) -> dict:
    p = pkg / rel
    if not p.is_file():
        raise ConsumeError(f"{rel} 없음 — 손상되거나 패키지가 아닙니다.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConsumeError(f"{rel} JSON 파싱 실패: {e}") from e


def _load_optional(pkg: Path, rel: str) -> dict | None:
    p = pkg / rel
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConsumeError(f"{rel} JSON 파싱 실패: {e}") from e


def _iter_cells(pkg: Path):
    f = pkg / "data" / "cells.jsonl"
    if not f.is_file():
        raise ConsumeError("data/cells.jsonl 없음 — 손상되거나 패키지가 아닙니다.")
    with f.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield json.loads(s)  # 손상 행은 조용히 건너뛰지 않고 오류로
            except json.JSONDecodeError as e:
                raise ConsumeError(f"cells.jsonl {i}행 파싱 실패: {e}") from e


# ── 공통 검증·뷰 ──────────────────────────────────────────────
def _clamp_limit(limit, default: int) -> int:
    if limit is None:
        return default
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ConsumeError(f"limit은 정수여야 합니다: {limit!r}")
    return max(1, min(limit, HARD_LIMIT))  # 1..HARD_LIMIT로 클램프(예산 우회 차단)


def _bounds(ref: str) -> tuple[int, int, int, int]:
    """A1 / A1:B10 → (min_col,min_row,max_col,max_row). 전열(A:B)·전행(1:5)·문법오류 거부."""
    try:
        b = range_boundaries(ref)
    except (ValueError, TypeError) as e:
        raise ConsumeError(f"주소/범위 파싱 실패: {ref!r} ({e})") from e
    if any(x is None for x in b):
        raise ConsumeError(f"완전한 셀 범위가 아닙니다(A:B/1:5 불가): {ref!r}")
    return b


def _sheet_names(meta: dict) -> list[str]:
    return [s.get("name") for s in meta.get("sheets", [])]


def _require_sheet(meta: dict, sheet: str) -> None:
    if sheet not in _sheet_names(meta):
        raise ConsumeError(f"시트 없음: {sheet!r}")


def _cell_view(o: dict) -> dict:
    """소비용 셀 뷰 — sheet·cell·value·formula는 항상, 보조 필드는 있을 때만."""
    v = {"sheet": o.get("sheet"), "cell": o.get("cell"),
         "value": o.get("value"), "formula": o.get("formula")}
    if o.get("cached_value") is not None:
        v["cached_value"] = o["cached_value"]
    if o.get("data_type") is not None:
        v["data_type"] = o["data_type"]
    nf = o.get("number_format")
    if nf and nf != "General":  # 날짜·백분율·금액 구분용(General은 무정보라 생략)
        v["number_format"] = nf
    if o.get("bold"):
        v["bold"] = True
    if o.get("border"):
        v["border"] = True
    if o.get("fill"):  # 채움 존재 플래그(색상값 자체는 미노출)
        v["fill"] = True
    if o.get("merged_range") is not None:
        v["merged_range"] = o["merged_range"]
    return v


# ── overview ─────────────────────────────────────────────────
def _sheet_summary(s: dict) -> dict:
    secs = s.get("sections", []) or []
    hist: dict[str, int] = {}
    for sec in secs:
        t = sec.get("semantic_type", "?")
        hist[t] = hist.get(t, 0) + 1
    return {"name": s.get("name"), "purpose": s.get("purpose"),
            "confidence": s.get("confidence"), "section_count": len(secs),
            "semantic_types": hist}


def _sheet_overview(pkg: Path, meta: dict, sheet: str, limit) -> dict:
    _require_sheet(meta, sheet)
    dim = next(s.get("dimensions") for s in meta["sheets"] if s.get("name") == sheet)
    out = {"sheet": sheet, "dimensions": dim, "review_status": None,
           "returned": 0, "total_sections": 0, "truncated": False, "sections": []}
    sem = _load_optional(pkg, "data/semantics.json")
    if not isinstance(sem, dict):
        return out
    status = sem.get("review", {}).get("status")
    out["review_status"] = status
    if status != "approved":  # 승인 전(draft/rejected)은 구간 상세 비노출
        return out
    ssheet = next((s for s in sem.get("sheets", []) if s.get("name") == sheet), None)
    if ssheet is None:
        return out
    out["purpose"] = ssheet.get("purpose")
    out["confidence"] = ssheet.get("confidence")
    secs = ssheet.get("sections", []) or []
    lim = _clamp_limit(limit, OVERVIEW_SECTION_LIMIT)
    out["total_sections"] = len(secs)
    view = []
    for sec in secs[:lim]:
        d = {"range": sec.get("range"), "semantic_type": sec.get("semantic_type"),
             "confidence": sec.get("confidence"), "evidence": sec.get("evidence")}
        flds = sec.get("fields", []) or []
        if flds:
            d["fields"] = [{"label_cell": f.get("label_cell"),
                            "value_cell": f.get("value_cell"), "role": f.get("role")}
                           for f in flds]
        view.append(d)
    out["sections"] = view
    out["returned"] = len(view)
    out["truncated"] = len(secs) > len(view)
    return out


def overview(pkg, *, sheet: str | None = None, limit=None) -> dict:
    """패키지/시트 개요. sheet를 주면 그 시트의 (승인된) 구간 상세를 예산 안에서 반환.

    해석 계층은 review.status=='approved'일 때만 내용을 노출한다(승인 전 은닉 계약). draft는
    상태·건수만, rejected는 상태만 알린다. 워크북 개요는 셀 값·구간 근거 상세를 담지 않는다.
    """
    pkg = Path(pkg)
    meta = _load_required(pkg, "meta.json")
    # Audit files are agent-visible only as one committed, validated bundle.  In particular,
    # overview must not bypass the key gate used by brief/audit-search when a publish was
    # interrupted or an artifact/meta field was changed later.
    from .audit.consume import AuditConsumeError, load_validated_audit_bundle

    try:
        audit_bundle = load_validated_audit_bundle(pkg, allow_absent=True)
    except AuditConsumeError as e:
        raise ConsumeError(f"감사 prepare 상태가 손상되었습니다: {e}") from e
    if sheet is not None:
        return _sheet_overview(pkg, meta, sheet, limit)

    refs_ = _load_required(pkg, "data/references.json")
    diag = _load_required(pkg, "data/diagnostics.json")
    n_cells = sum(1 for _ in _iter_cells(pkg))
    src = meta.get("source", {})
    ann = meta.get("annotation", {})
    out: dict = {
        "source": {"filename": src.get("filename"), "format": src.get("format"),
                   "sha12": (src.get("sha256") or "")[:12]},
        "converter_version": meta.get("converter_version"),
        "sheets": [{"name": s.get("name"), "dimensions": s.get("dimensions"),
                    "max_row": s.get("max_row"), "max_col": s.get("max_col")}
                   for s in meta.get("sheets", [])],
        "counts": {"cells": n_cells, "edges": len(refs_.get("edges", []))},
        "observability": refs_.get("observability", {}).get("workbook"),
        "diagnostics": {
            "hidden_sheets": len(diag.get("hidden", {}).get("sheets", [])),
            "truncations": len(diag.get("truncations", [])),
            "blank_source_formulas": len(diag.get("blank_source_formulas", [])),
        },
        "annotation": {"present": ann.get("present", False),
                       "review_status": ann.get("review_status")},
    }
    if audit_bundle is not None:
        _, facts_doc, standards_doc, brief_doc = audit_bundle
        brief_review = brief_doc.get("review", {}).get("status")
        readiness = brief_doc.get("readiness")
        out["audit_preparation"] = {
            "present": True,
            "status": readiness.get("status") if isinstance(readiness, dict) else None,
            "review_status": brief_review,
            "unreviewed": brief_review != "approved",
            "readiness": readiness,
            "counts": {
                "facts": len(facts_doc.get("facts", [])),
                "relations": len(facts_doc.get("relations", [])),
                "standards_citations": len(standards_doc.get("citations", [])),
                "brief_statements": len(brief_doc.get("statements", [])),
            },
        }
    sem = _load_optional(pkg, "data/semantics.json")
    if isinstance(sem, dict):
        status = sem.get("review", {}).get("status")
        claims = sem.get("workbook_claims", [])
        sheets = sem.get("sheets", [])
        if status == "approved":  # 승인된 해석만 내용 노출(구간 상세는 --sheet로)
            out["interpretation"] = {
                "review_status": "approved",
                "workbook_claims": [
                    {"claim": c.get("claim"), "evidence": c.get("evidence"),
                     "confidence": c.get("confidence")} for c in claims],
                "sheets": [_sheet_summary(s) for s in sheets],
            }
        else:  # draft/rejected — 상태(+draft는 건수)만, 내용 은닉
            st = {"review_status": status}
            if status == "draft":
                st["workbook_claims"] = len(claims)
                st["sheets_annotated"] = len(sheets)
                st["sections_total"] = sum(len(s.get("sections", []) or []) for s in sheets)
            out["interpretation_status"] = st
    return out


# ── inspect ──────────────────────────────────────────────────
def inspect(pkg, *, sheet: str, range: str | None = None, cell: str | None = None,
            limit=None) -> dict:
    """지정 시트의 범위/셀 원장을 예산 안에서 반환(범위/셀 미지정 시 시트 전체)."""
    pkg = Path(pkg)
    meta = _load_required(pkg, "meta.json")
    _require_sheet(meta, sheet)
    lim = _clamp_limit(limit, INSPECT_LIMIT)
    ref = cell or range
    if ref is not None:
        min_c, min_r, max_c, max_r = _bounds(ref)
        used = ref
    else:
        dims = next(s.get("dimensions") for s in meta["sheets"] if s.get("name") == sheet)
        min_c, min_r, max_c, max_r = _bounds(dims)
        used = dims
    cells, total = [], 0
    for o in _iter_cells(pkg):
        if o.get("sheet") != sheet:
            continue
        r, c = o.get("row"), o.get("col")
        if r is None or c is None or not (min_r <= r <= max_r and min_c <= c <= max_c):
            continue
        total += 1
        if len(cells) < lim:
            cells.append(_cell_view(o))
    return {"sheet": sheet, "range": used, "returned": len(cells),
            "total_in_range": total, "truncated": total > len(cells), "cells": cells}


# ── search ───────────────────────────────────────────────────
def search(pkg, *, query: str, sheet: str | None = None, limit=None) -> dict:
    """값·수식에 대한 대소문자 무시 부분일치를 상한까지 반환한다."""
    pkg = Path(pkg)
    if not query:
        raise ConsumeError("query가 비어 있습니다.")
    meta = _load_required(pkg, "meta.json")
    if sheet is not None:
        _require_sheet(meta, sheet)
    lim = _clamp_limit(limit, SEARCH_LIMIT)
    q = query.lower()
    matches, total = [], 0
    for o in _iter_cells(pkg):
        if sheet is not None and o.get("sheet") != sheet:
            continue
        parts = []
        if o.get("value") is not None:
            parts.append(str(o["value"]))
        if o.get("formula") is not None:
            parts.append(str(o["formula"]))
        if q in "\n".join(parts).lower():
            total += 1
            if len(matches) < lim:
                matches.append(_cell_view(o))
    return {"query": query, "sheet": sheet, "returned": len(matches),
            "total_matches": total, "truncated": total > len(matches), "matches": matches}


# ── refs ─────────────────────────────────────────────────────
def _in_ref(cell: str, ref: str) -> bool:
    """cell(절대 'S!A1')이 ref(절대 'S!A1' 또는 'S!A1:B2') 범위에 속하나."""
    if "!" not in cell or "!" not in ref:
        return cell == ref
    cs, ca = cell.split("!", 1)
    rs, ra = ref.split("!", 1)
    if cs != rs:
        return False
    try:
        ccol, crow, _, _ = range_boundaries(ca)
        min_c, min_r, max_c, max_r = range_boundaries(ra)
    except (ValueError, TypeError):
        return cell == ref
    if None in (ccol, crow, min_c, min_r, max_c, max_r):
        return cell == ref
    return min_r <= crow <= max_r and min_c <= ccol <= max_c


def refs(pkg, *, cell: str, limit=None) -> dict:
    """지정 셀(절대 'Sheet!A1')의 출입 참조 엣지만 반환한다.

    outgoing = 이 셀의 수식이 참조하는 대상(from==cell). incoming = 이 셀(또는 이 셀을
    포함하는 범위)을 참조하는 수식들(to==cell 또는 to 범위가 cell 포함).
    """
    pkg = Path(pkg)
    if "!" not in cell:
        raise ConsumeError(f"셀은 'Sheet!A1' 절대주소여야 합니다: {cell!r}")
    _bounds(cell.split("!", 1)[1])  # 주소 문법 검증(전열/전행·오류 거부)
    lim = _clamp_limit(limit, REFS_LIMIT)
    edges = _load_required(pkg, "data/references.json").get("edges", [])
    outgoing, incoming = [], []
    out_total = in_total = 0
    for e in edges:
        frm, to = e.get("from"), e.get("to")
        if frm == cell:
            out_total += 1
            if len(outgoing) < lim:
                outgoing.append({"to": to, "ref_type": e.get("ref_type"),
                                 "reason": e.get("reason")})
        if to == cell or _in_ref(cell, str(to)):
            in_total += 1
            if len(incoming) < lim:
                incoming.append({"from": frm, "ref_type": e.get("ref_type"),
                                 "reason": e.get("reason")})
    return {"cell": cell, "returned": len(outgoing) + len(incoming),
            "outgoing": outgoing, "incoming": incoming,
            "outgoing_total": out_total, "incoming_total": in_total,
            "truncated": out_total > len(outgoing) or in_total > len(incoming)}
