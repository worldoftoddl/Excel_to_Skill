---
name: fx4-defined-names-3e3addf7e0e5
description: "스프레드시트 1매 — (빈 시트). (의미 주석 미승인)"
---

# fx4_defined_names.xlsx

## ① 원본 메타

- 파일명: `fx4_defined_names.xlsx`
- sha256(앞 12자): `3e3addf7e0e5`
- converter_version: `<normalized>`
- 형식: `xlsx` · loader_path: `openpyxl_normal`
- 구성: 시트 1매

## ② 구성 목록

- `Data` (`layout/Data.html`, used range `A1:B3`) — (머리 텍스트 없음)

## ③ 참조 관계

- 참조 엣지: 0건
- observability: 통합문서 전체 관찰됨

## ④ 진단 요약

- 외부 링크: 0건
- 정의된 이름: 전역 4 · 시트 1
- 숨김: 시트 0 · 행 0 · 열 0
- 빈 칸 참조 수식: 0건
- layout 절단: 0건

## ⑤ 리소스 사용법

**원본 JSON(`data/*.json`·`cells.jsonl`)을 통째로 읽지 마십시오.** 다음 명령으로 **개요 → 시트 → 셀** 순으로 단계 조회하십시오(각 결과는 출력 예산 안에서 반환):

- `excel-to-skill overview <이 폴더> [--sheet <시트>]` — 개요(셀 원문 없음). `--sheet`로 그 시트의 구간 상세
- `excel-to-skill inspect <이 폴더> --sheet <시트> [--range A1:B10 | --cell A1]` — 지정 범위 셀만
- `excel-to-skill search <이 폴더> --query <문자열> [--sheet <시트>]` — 값·수식 부분일치(상한)
- `excel-to-skill refs <이 폴더> --cell <시트!A1>` — 그 셀의 출입 참조 엣지

- 반환 셀 레코드는 `sheet`·`cell`·`value`·`formula`를 포함합니다. **셀 내용·문서 의미에 관한 주장에는 그 `시트!셀` 근거를 제시하고, 파일 형식·시트 수 같은 구조 정보는 `overview` 필드를 근거로 제시하십시오.**
- 원자료(필요 시 직접 읽기): 원장 `data/cells.jsonl` · 참조 `data/references.json` · 진단 `data/diagnostics.json` · 레이아웃 `layout/*.html`.
- 앵커 속성 `data-cell`: layout HTML의 각 `<td>` `data-cell` 값은 `cells.jsonl`의 `cell` 주소와 문자 단위로 일치합니다.

## ⑥ 해석

의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오.
