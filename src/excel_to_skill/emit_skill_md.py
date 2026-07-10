"""§4.2 SKILL.md draft 방출기 — 패키지 진입점 표지 (혼합 계층의 결정론 골격).

draft 단계(주석 미승인)의 SKILL.md만 만든다. 승인판(semantics 기반 재생성)은
M3 소관. 본문은 순서 고정 ①~⑥(§4.2):

  ① 원본 메타 요약 — 파일명·sha 12자·converter_version·구성·loader_path
  ② 구성 목록 — 시트마다 layout 파일·used range·머리 텍스트 원문(주소 병기)
  ③ 참조 관계 요약 — 엣지 수·대표 3건·observability(P6)
  ④ 진단 요약 — 외부링크·정의이름·숨김·빈칸참조·절단(개수만, 원문 미노출)
  ⑤ 리소스 사용법 — 원장 파일명 + 앵커 속성명 data-cell(⑧ 값 계약) 명시
  ⑥ [해석] — 미승인이면 "구조 데이터로 직접 해석" 한 줄, 승인 시에만 렌더

머리 텍스트 규칙(§4.2): 시트 used range에서 (row,col) 사전식 최소 위치의 비공백
텍스트 셀 원문. draft description·구성 목록이 공유한다.

P3: 판단·요약(권고) 문장 금지, 기계적 사실만. P7: 머리 텍스트에 mask_pii(이메일).
진단 수치는 이미 마스킹된 diagnostics dict에서 가져오므로 원문이 새지 않는다.
개행 LF, 끝 개행(결정론).
"""
from __future__ import annotations

import json
from pathlib import Path

from openpyxl.utils import coordinate_to_tuple

from .emit_html import assign_layout_filenames
from .emit_refs import mask_pii
from .extractor import SheetIR, WorkbookIR

_UNAPPROVED = "의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오."
_OBSERV_KO = {
    "full": "통합문서 전체 관찰됨",
    "unavailable_xls": "관찰 불가(.xls)",
    "not_applicable_docx": "해당 없음(docx)",
}


def _name_slug(stem: str, sha12: str) -> str:
    """frontmatter name — `{ascii_slug_or_untitled}-{sha12}`.

    ASCII 영숫자만 소문자-하이픈으로 남기고(한글 등은 하이픈), sha256 앞 12자를
    접미해 유일성을 보장한다(한글 파일 다수 코퍼스에서 `untitled` 충돌 방지).
    """
    parts = []
    cur = []
    for ch in stem.lower():
        if ch.isascii() and ch.isalnum():
            cur.append(ch)
        elif cur:
            parts.append("".join(cur))
            cur = []
    if cur:
        parts.append("".join(cur))
    ascii_slug = "-".join(parts) or "untitled"
    return f"{ascii_slug}-{sha12}"


def _yaml_dq(s: str) -> str:
    """YAML 큰따옴표 스칼라로 안전 인용(개행·탭은 공백으로)."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    for ws in ("\n", "\r", "\t"):
        s = s.replace(ws, " ")
    return f'"{s}"'


def _head_text(sheet: SheetIR) -> tuple[str, str] | None:
    """(row,col) 사전식 최소의 비공백 텍스트 셀 → (원문, 좌표). 없으면 None."""
    best_key: tuple[int, int] | None = None
    best: tuple[str, str] | None = None
    for (r, c), cell in sheet.cells.items():
        v = cell.value
        if isinstance(v, str) and v.strip():
            key = (r, c)
            if best_key is None or key < best_key:
                best_key = key
                best = (v, cell.coord)
    return best


def _approved_description(semantics: dict, n_sheets: int) -> str:
    """승인판 description(§4.2) — workbook_claims 최상위 claim 문장."""
    claims = semantics.get("workbook_claims", [])
    if claims and isinstance(claims[0], dict) and claims[0].get("claim"):
        return mask_pii(str(claims[0]["claim"])).strip()
    sheets = semantics.get("sheets", [])
    if sheets:
        joined = " / ".join(
            mask_pii(str(s.get("purpose", ""))) for s in sheets if isinstance(s, dict)
        )
        return f"스프레드시트 {n_sheets}매 — {joined}. (승인됨)"
    return f"스프레드시트 {n_sheets}매 — 승인된 의미 주석."


def _conf(x) -> str:
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return "?"


def _ev(addrs) -> str:
    return ", ".join(f"`{a}`" for a in (addrs or []))


def _render_interpretation(semantics: dict) -> list[str]:
    """⑥ 해석(승인판) — **요약만** 렌더한다(워크북 주장·시트 purpose·구간 수).

    구간·근거·필드 상세를 여기 전부 실으면 Agent가 SKILL 전체를 읽는 순간 단계 조회가
    무력화된다. 상세는 `overview <폴더> --sheet <시트>`로 내려가도록 안내만 남긴다.
    """
    rev = semantics.get("review", {})
    out = [
        "## ⑥ 해석 (승인됨)",
        "",
        f"- 검토: 승인됨 · {rev.get('reviewed_at')}",
        "- 구간·근거·필드 상세는 `excel-to-skill overview <이 폴더> --sheet <시트>`로 "
        "조회하십시오(SKILL에는 요약만 싣습니다).",
        "",
    ]
    claims = semantics.get("workbook_claims", [])
    if claims:
        out.append("### 워크북 주장")
        for c in claims:
            out.append(
                f"- {mask_pii(str(c.get('claim', '')))} — 근거 {_ev(c.get('evidence'))}"
                f" · confidence {_conf(c.get('confidence'))}"
            )
        out.append("")
    sheets = semantics.get("sheets", [])
    if sheets:
        out.append("### 시트 의미(요약)")
        for s in sheets:
            secs = s.get("sections", []) or []
            out.append(
                f"- `{s.get('name')}`: {mask_pii(str(s.get('purpose', '')))}"
                f" — 구간 {len(secs)}개 · confidence {_conf(s.get('confidence'))}"
            )
        out.append("")
    return out


def _render_skill_md(
    *,
    meta: dict,
    references: dict,
    diagnostics: dict,
    layout_filenames: dict[str, str],
    heads: dict[str, tuple[str, str] | None],
    semantics: dict | None = None,
) -> str:
    """SKILL.md 문자열(결정론). review.status=="approved"면 승인판으로 렌더.

    ①~⑤(구조 사실)는 계층 무관 동일하고, description(2단 §4.2)과 ⑥ 해석만 승인 여부로
    갈린다. heads/layout_filenames는 호출자가 IR(convert) 또는 패키지 파일(review)에서
    복원해 넘긴다 — 이 함수 자체는 IR에 의존하지 않는다.
    """
    src = meta["source"]
    sha12 = src["sha256"][:12]
    approved = bool(semantics) and semantics.get("review", {}).get("status") == "approved"

    # ── frontmatter ───────────────────────────────────────────
    if approved:
        desc = _approved_description(semantics, len(meta["sheets"]))
    else:
        head_join = " / ".join(
            mask_pii(heads[sh["name"]][0]) if heads.get(sh["name"]) else "(빈 시트)"
            for sh in meta["sheets"]
        )
        desc = f"스프레드시트 {len(meta['sheets'])}매 — {head_join}. (의미 주석 미승인)"
    lines = [
        "---",
        f"name: {_name_slug(Path(src['filename']).stem, sha12)}",
        f"description: {_yaml_dq(desc)}",
        "---",
        "",
        f"# {src['filename']}",
        "",
    ]

    # ① 원본 메타 요약
    lines += [
        "## ① 원본 메타",
        "",
        f"- 파일명: `{src['filename']}`",
        f"- sha256(앞 12자): `{sha12}`",
        f"- converter_version: `{meta['converter_version']}`",
        f"- 형식: `{src['format']}` · loader_path: `{meta['loader_path']}`",
        f"- 구성: 시트 {len(meta['sheets'])}매",
        "",
    ]

    # ② 구성 목록
    lines.append("## ② 구성 목록")
    lines.append("")
    for sh in meta["sheets"]:
        name = sh["name"]
        fname = layout_filenames.get(name, "")
        ht = heads.get(name)
        head_str = (
            f" — {mask_pii(ht[0])} (`{ht[1]}`)" if ht else " — (머리 텍스트 없음)"
        )
        lines.append(
            f"- `{name}` (`layout/{fname}`, used range `{sh['dimensions']}`)"
            f"{head_str}"
        )
    lines.append("")

    # ③ 참조 관계 요약
    edges = references.get("edges", [])
    lines += ["## ③ 참조 관계", "", f"- 참조 엣지: {len(edges)}건"]
    if edges:
        lines.append("- 대표:")
        for e in edges[:3]:
            lines.append(f"  - `{e['from']}` → `{e['to']}` ({e['ref_type']})")
    obs = references.get("observability", {})
    workbook = obs.get("workbook", "full")
    lines.append(f"- observability: {_OBSERV_KO.get(workbook, workbook)}")
    if obs.get("note"):
        lines.append(f"  - note: {obs['note']}")
    unresolved = references.get("unresolved", [])
    if unresolved:
        lines.append(f"- 미해결(INDIRECT/OFFSET 등): {len(unresolved)}건")
    lines.append("")

    # ④ 진단 요약
    ext = diagnostics.get("external_links", {})
    ext_count = ext.get("count")
    dn = diagnostics.get("defined_names", {})
    hid = diagnostics.get("hidden", {})
    lines += [
        "## ④ 진단 요약",
        "",
        f"- 외부 링크: {'관찰 불가' if ext_count is None else f'{ext_count}건'}",
        f"- 정의된 이름: 전역 {dn.get('global_total', 0)} · "
        f"시트 {dn.get('sheet_scoped_total', 0)}",
        f"- 숨김: 시트 {len(hid.get('sheets', []))} · "
        f"행 {hid.get('rows_count', 0)} · 열 {hid.get('cols_count', 0)}",
        f"- 빈 칸 참조 수식: {len(diagnostics.get('blank_source_formulas', []))}건",
        f"- layout 절단: {len(diagnostics.get('truncations', []))}건",
    ]
    if diagnostics.get("format_limitations"):
        lines.append(f"- 형식 한계: {diagnostics['format_limitations']}")
    lines.append("")

    # ⑤ 리소스 사용법
    lines += [
        "## ⑤ 리소스 사용법",
        "",
        "**원본 JSON(`data/*.json`·`cells.jsonl`)을 통째로 읽지 마십시오.** 다음 명령으로 "
        "**개요 → 시트 → 셀** 순으로 단계 조회하십시오(각 결과는 출력 예산 안에서 반환):",
        "",
        "- `excel-to-skill overview <이 폴더> [--sheet <시트>]` — 개요(셀 원문 없음). "
        "`--sheet`로 그 시트의 구간 상세",
        "- `excel-to-skill inspect <이 폴더> --sheet <시트> [--range A1:B10 | --cell A1]`"
        " — 지정 범위 셀만",
        "- `excel-to-skill search <이 폴더> --query <문자열> [--sheet <시트>]`"
        " — 값·수식 부분일치(상한)",
        "- `excel-to-skill refs <이 폴더> --cell <시트!A1>` — 그 셀의 출입 참조 엣지",
        "",
        "- 반환 셀 레코드는 `sheet`·`cell`·`value`·`formula`를 포함합니다. "
        "**셀 내용·문서 의미에 관한 주장에는 그 `시트!셀` 근거를 제시하고, 파일 형식·시트 "
        "수 같은 구조 정보는 `overview` 필드를 근거로 제시하십시오.**",
        "- 원자료(필요 시 직접 읽기): 원장 `data/cells.jsonl` · 참조 "
        "`data/references.json` · 진단 `data/diagnostics.json` · 레이아웃 `layout/*.html`.",
        "- 앵커 속성 `data-cell`: layout HTML의 각 `<td>` `data-cell` 값은 "
        "`cells.jsonl`의 `cell` 주소와 문자 단위로 일치합니다.",
        "",
    ]

    # ⑥ 해석 — 승인판이면 semantics 렌더, 아니면 미승인 한 줄
    if approved:
        lines += _render_interpretation(semantics)
    else:
        lines += ["## ⑥ 해석", "", _UNAPPROVED, ""]

    return "\n".join(lines)


def build_skill_md(
    ir: WorkbookIR,
    *,
    meta: dict,
    references: dict,
    diagnostics: dict,
    layout_filenames: dict[str, str] | None = None,
    semantics: dict | None = None,
) -> str:
    """convert 경로: IR에서 heads·layout 파일명을 얻어 SKILL.md를 만든다.

    semantics 없이 부르면 draft(convert 기본). review는 아래 from_package를 쓴다.
    """
    if layout_filenames is None:
        layout_filenames = assign_layout_filenames(ir.sheets)
    heads = {sh.name: _head_text(sh) for sh in ir.sheets}
    return _render_skill_md(
        meta=meta,
        references=references,
        diagnostics=diagnostics,
        layout_filenames=layout_filenames,
        heads=heads,
        semantics=semantics,
    )


def _heads_from_cells(pkg: Path, meta: dict) -> dict[str, tuple[str, str] | None]:
    """cells.jsonl에서 시트별 (row,col) 사전식 최소 비공백 텍스트 셀 → (원문, 좌표).

    IR의 _head_text와 같은 규칙을 원장(디스크)에서 복원한다(review는 IR이 없음).
    """
    best: dict[str, tuple[tuple[int, int], tuple[str, str]]] = {}
    f = pkg / "data" / "cells.jsonl"
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                o = json.loads(s)
            except json.JSONDecodeError:
                continue
            v = o.get("value")
            if not (isinstance(v, str) and v.strip()):
                continue
            sheet, coord = o.get("sheet"), o.get("cell")
            try:
                key = coordinate_to_tuple(coord)  # (row, col)
            except (ValueError, TypeError):
                continue
            if sheet not in best or key < best[sheet][0]:
                best[sheet] = (key, (v, coord))
    return {
        sh["name"]: (best[sh["name"]][1] if sh["name"] in best else None)
        for sh in meta["sheets"]
    }


def _layout_filenames_from_pkg(pkg: Path, meta: dict) -> dict[str, str]:
    """layout/*.html의 data-sheet 마커로 시트명 → 파일명 매핑을 복원한다."""
    import html as _html

    texts = {
        f.name: f.read_text(encoding="utf-8")
        for f in sorted((pkg / "layout").glob("*.html"))
    }
    result: dict[str, str] = {}
    for sh in meta["sheets"]:
        name = sh["name"]
        marker = f'data-sheet="{_html.escape(name, quote=True)}"'
        result[name] = next((fn for fn, t in texts.items() if marker in t), "")
    return result


def build_skill_md_from_package(pkg: Path) -> str:
    """review 경로: 패키지 파일만으로 SKILL.md를 재구성한다(IR 없음).

    review.status가 approved면 승인판, 아니면 미승인(draft/rejected)으로 렌더한다.
    """
    pkg = Path(pkg)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    references = json.loads((pkg / "data/references.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((pkg / "data/diagnostics.json").read_text(encoding="utf-8"))
    sem_path = pkg / "data" / "semantics.json"
    semantics = (
        json.loads(sem_path.read_text(encoding="utf-8")) if sem_path.is_file() else None
    )
    return _render_skill_md(
        meta=meta,
        references=references,
        diagnostics=diagnostics,
        layout_filenames=_layout_filenames_from_pkg(pkg, meta),
        heads=_heads_from_cells(pkg, meta),
        semantics=semantics,
    )


def write_skill_md(
    ir: WorkbookIR,
    out_path: Path,
    *,
    meta: dict,
    references: dict,
    diagnostics: dict,
    layout_filenames: dict[str, str] | None = None,
) -> str:
    """SKILL.md를 쓰고 문자열을 반환한다."""
    text = build_skill_md(
        ir,
        meta=meta,
        references=references,
        diagnostics=diagnostics,
        layout_filenames=layout_filenames,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text
