"""§4.5 수식 참조 파서 — 토크나이저+정규식 수준(완전 AST 불요, 지시서 명시).

수식 원문(선행 '=' 없음, extractor가 XML 원문으로 복원)에서 셀·범위 참조를
뽑는다. 지시서 §4.5 파서 요구:
  ① 따옴표 시트명 `'시트 명'!A1` — 이스케이프('' → ') 해제
  ② `$` 절대 참조 — 좌표(coord)는 정규화, 원문(raw)은 보존
  ③ 범위는 범위 노드 하나(셀 폭발 금지) — `B4:C10`, `A:C`, `1:5` 모두 한 토큰
  ④ `[n]` 외부 참조는 external_index로 표시 — 조인은 emit_refs가 담당
  ⑤ INDIRECT/OFFSET의 동적 목표 판정은 emit_refs가 담당(파서는 정적 참조만)

알려진 한계(토크나이저 수준의 의도된 단순화):
  - 3D 참조(`Sheet1:Sheet3!A1`)는 마지막 시트만 잡힌다.
  - 문자열 리터럴("…") 안의 참조 모양 텍스트는 무시한다(공백 치환 후 스캔).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RefToken:
    raw: str                    # 수식 내 원문 그대로 (따옴표·$·[n] 포함)
    sheet: str | None           # 시트명(따옴표 해제). None = 수식이 있는 그 시트
    coord: str                  # 정규화 좌표 — $ 제거·대문자 (예: B4, B4:C10, A:C)
    ref_type: str               # "cell" | "range"
    external_index: int | None  # [n] 외부 참조 색인, 없으면 None


# 시트명: 따옴표 없는 형태는 문자·_·비ASCII로 시작(숫자 시작 시트는 반드시 따옴표).
# [n] 외부 참조 뒤에는 숫자 시작 시트명이 그대로 올 수 있다(예: [2]2600!A1).
_SHEET_PLAIN = r"[A-Za-z_À-￿][A-Za-z0-9_.À-￿]*"
_SHEET_EXT = r"\[[0-9]+\][A-Za-z0-9_.À-￿]+"
_CELL = r"\$?[A-Za-z]{1,3}\$?[0-9]{1,7}"
_COL = r"\$?[A-Za-z]{1,3}"
_ROW = r"\$?[0-9]{1,7}"
_BODY = rf"{_CELL}:{_CELL}|{_COL}:{_COL}|{_ROW}:{_ROW}|{_CELL}"

_REF_RE = re.compile(
    rf"""
    (?<![A-Za-z0-9_.$!\]])              # 식별자 꼬리·다른 참조의 일부 배제
    (?:
        (?:
            (?P<quoted>'(?:[^']|'')+')  # ① 따옴표 시트명 (내부에 [n] 가능)
          | (?P<plain>{_SHEET_EXT}|{_SHEET_PLAIN})
        )!
        (?P<body>{_BODY})
      |
        (?P<bare>{_CELL}:{_CELL}|{_COL}:{_COL}|{_CELL})
        (?![A-Za-z0-9_(])               # 함수명(LOG10 등)·긴 식별자 배제
    )
    """,
    re.VERBOSE,
)

_EXT_PREFIX_RE = re.compile(r"^\[([0-9]+)\]")


def _blank_strings(formula: str) -> str:
    """문자열 리터럴을 같은 길이의 공백으로 치환해 위치를 보존한다."""
    out = list(formula)
    i = 0
    while i < len(out):
        if out[i] == '"':
            j = i + 1
            while j < len(out):
                if out[j] == '"':
                    if j + 1 < len(out) and out[j + 1] == '"':  # "" 이스케이프
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i, min(j + 1, len(out))):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _normalize_coord(body: str) -> str:
    return body.replace("$", "").upper()


def parse_formula(formula: str) -> list[RefToken]:
    """수식에서 정적 참조 토큰을 등장 순서대로 뽑는다."""
    tokens: list[RefToken] = []
    for m in _REF_RE.finditer(_blank_strings(formula)):
        raw = formula[m.start():m.end()]
        external_index: int | None = None

        if m.group("bare") is not None:
            sheet = None
            body = m.group("bare")
        else:
            body = m.group("body")
            if m.group("quoted") is not None:
                sheet = m.group("quoted")[1:-1].replace("''", "'")
            else:
                sheet = m.group("plain")
            ext = _EXT_PREFIX_RE.match(sheet)
            if ext:
                external_index = int(ext.group(1))
                sheet = sheet[ext.end():]

        coord = _normalize_coord(body)
        tokens.append(
            RefToken(
                raw=raw,
                sheet=sheet,
                coord=coord,
                ref_type="range" if ":" in coord else "cell",
                external_index=external_index,
            )
        )
    return tokens
