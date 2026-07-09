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

from pathlib import Path

from .emit_html import assign_layout_filenames
from .emit_refs import mask_pii
from .extractor import SheetIR, WorkbookIR

_UNAPPROVED = "의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오."
_OBSERV_KO = {
    "full": "통합문서 전체 관찰됨",
    "unavailable_xls": "관찰 불가(.xls)",
    "not_applicable_docx": "해당 없음(docx)",
}


def _name_slug(stem: str) -> str:
    """frontmatter name — 소문자-하이픈 slug(ASCII 영숫자만, 그 외 하이픈)."""
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
    return "-".join(parts) or "untitled"


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


def build_skill_md(
    ir: WorkbookIR,
    *,
    meta: dict,
    references: dict,
    diagnostics: dict,
    layout_filenames: dict[str, str] | None = None,
) -> str:
    """draft SKILL.md 문자열을 만든다(결정론)."""
    if layout_filenames is None:
        layout_filenames = assign_layout_filenames(ir.sheets)
    src = meta["source"]
    sha12 = src["sha256"][:12]
    heads = {sh.name: _head_text(sh) for sh in ir.sheets}

    # ── frontmatter ───────────────────────────────────────────
    head_join = " / ".join(
        mask_pii(heads[sh.name][0]) if heads[sh.name] else "(빈 시트)"
        for sh in ir.sheets
    )
    desc = f"스프레드시트 {len(ir.sheets)}매 — {head_join}. (의미 주석 미승인)"
    lines = [
        "---",
        f"name: {_name_slug(Path(src['filename']).stem)}",
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
        f"- 구성: 시트 {len(ir.sheets)}매",
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
        "- 원장(한 줄 = 한 셀): `data/cells.jsonl`",
        "- 참조 그래프: `data/references.json`",
        "- 구조 진단: `data/diagnostics.json`",
        "- 레이아웃: `layout/*.html`",
        "- **이 패키지는 앵커 속성 `data-cell`을 씁니다.** layout HTML의 각 "
        "`<td>` `data-cell` 값은 `cells.jsonl`의 `cell` 주소와 문자 단위로 "
        "일치합니다. 표를 근거로 답할 때 그 주소로 원장을 검색하십시오.",
        "",
    ]

    # ⑥ 해석 (draft: 항상 미승인 한 줄)
    lines += ["## ⑥ 해석", "", _UNAPPROVED, ""]

    return "\n".join(lines)


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
