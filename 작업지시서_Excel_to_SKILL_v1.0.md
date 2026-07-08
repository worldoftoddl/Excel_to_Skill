# 작업지시서 — Excel_to_SKILL v1.0

| 항목 | 내용 |
|---|---|
| 발행일 | 2026-07-08 |
| 상태 | 확정 (구현 착수 가능) |
| 구현 담당 | Claude Code |
| 검토·승인 | 선생님 (프로젝트 오너) |
| 선행 문서 | auditPaper_assist Stage 3 v1.2 (조서 변환·캐시 계층) — 본 프로젝트로 **대체** |

---

## 0. 목적과 위치

### 0.1 한 줄 정의

임의의 Excel 통합문서를 읽어, AI 에이전트가 **근거(셀 좌표) 추적 가능하게** 소비할 수 있는 스킬 패키지 — Markdown 진입점(SKILL.md) + HTML 레이아웃 + JSON/JSONL 데이터 + LLM 의미 주석 — 로 변환하는 독립 CLI 도구.

### 0.2 리포지토리와 경계

- 신규 독립 리포지토리: `worldoftoddl/Excel_to_SKILL`
- 이 리포는 **auditPaper_assist의 자산(routing_gold.json, 기준서 코퍼스, MCP 서버)을 일절 참조하지 않는다.** 이유: 범용 도구로 성립해야 하며, 특정 프로젝트의 정답 데이터에 결합되면 도구의 독립성과 소비 측 평가의 청결성이 모두 훼손된다.
- 역방향 소비(auditPaper_assist가 본 도구의 산출물을 입력으로 쓰는 것)는 그쪽 프로젝트의 통합 규칙이며 본 리포의 관심사가 아니다.
- 후속 조치(본 리포 범위 외, 기록용): auditPaper_assist의 `docs/workorders/` 내 Stage 3 v1.2 지시서에 "Excel_to_SKILL로 대체됨" 표기.

### 0.3 확정된 설계 결정

본 지시서에 선행한 설계 검토에서 아래 분기가 확정되었다. 구현 중 이 결정을 뒤집는 편차는 §10 절차를 따른다.

| # | 분기 | 결정 |
|---|---|---|
| ① | 프로젝트 위치 | 독립 리포 (v1.2 흡수·대체) |
| ② | SKILL.md 규격 | Claude Agent Skills frontmatter(`name`, `description`) 정식 준수 |
| ③ | 시트 본문 표현 | **HTML 일원화.** 마크다운 본문 전사 없음. SKILL.md는 얇은 안내서 |
| ④ | 정의된 이름 | diagnostics에 통계+샘플(상한 20). 전량 덤프는 `--full-names` 옵션 |
| ⑤ | 1차 대상 범위 | 빈 템플릿 스프레드시트 32종(xlsx 30 + xls 2). 값 채워진 실조서 최적화는 2차(행 상한 안전핀만 선반영). docx는 범위 외(오류+힌트) |
| ⑥ | 검토 워크플로 | semantics에 `review.status`(draft/approved/rejected) 도입 |
| ⑦ | description 생성 | 2단 — draft는 구조 사실 기반, approved 후 의미 기반으로 재생성 |
| L4 | 의미 계층 | **변환기 산출물로 포함**(`semantics.json`). 단 §1의 규율 3종(출처 2계급, 근거 강제, 캐시 층 분리) 적용 |

---

## 1. 불변 원칙 (위반 시 반려)

**P1. 출처 2계급.** 산출물은 두 계급으로 나뉘며 파일 단위로 분리된다. 혼입 금지.
- **결정론 계층**: `meta.json`, `cells.jsonl`, `references.json`, `diagnostics.json`, `layout/*.html` — 코드만으로 생성. LLM 호출 0회.
- **해석 계층**: `semantics.json` 및 SKILL.md의 [해석] 섹션 — LLM 생성. 동결·검토 대상.

**P2. 결정론 보장.** 동일 입력 파일 + 동일 `converter_version` → 결정론 계층은 재실행 시 동일 출력. 이를 위해 타임스탬프류 가변 값은 `meta.json`에만 존재하고, 모든 `.json`은 `sort_keys=True, indent=2, ensure_ascii=False`, `cells.jsonl`은 고정 필드 순서 + 고정 정렬(시트 순 → row → col)로 직렬화한다.

**P3. 원문 충실.** 결정론 계층은 셀에 있는 것만 옮긴다. 요약·재서술·정규화·트리밍 금지(값은 원문 그대로; HTML 이스케이프는 표현 규칙으로 허용). "이 시트는 ~용 조서다" 같은 의미 부여는 결정론 계층 어디에도 들어가지 않는다 — 그것은 전부 `semantics.json`의 몫이다.

**P4. 근거 강제.** `semantics.json`의 모든 주장(claim/purpose/section)은 `evidence`(셀 좌표 또는 범위 배열)와 `confidence`를 가진다. 근거 없는 주장은 스키마 검증에서 거부한다.

**P5. 생성 메타 박제.** 해석 계층은 `generator` 블록(model, annotator_version, prompt_sha, temperature, generated_at)을 필수 포함한다. 해석의 재현 조건을 파일 자체가 증언해야 한다.

**P6. 관찰 불가와 부재의 구분.** 형식 한계로 볼 수 없는 것(예: .xls의 수식 원문)은 "없음"이 아니라 `observability: unavailable`로 명시한다. 에이전트가 "참조가 없다"고 오추론하지 않게 하기 위함이다.

**P7. 개인정보 마스킹.** 진단 샘플에 등장하는 이메일은 로컬파트 마스킹(`j***@domain`), 실명 의심 문자열은 인용하지 않는다. 대상 코퍼스에 90년대 작성자 실명·이메일이 실재함이 확인되었다(§2 함정 4).

---

## 2. 대상 입력과 실측 제약 (구현 전 필독)

2026-07-08, openpyxl 3.1.5 환경에서 대상 코퍼스 32종을 전수 검증한 결과다. 아래 함정은 추측이 아니라 **관측 사실**이다.

**함정 1 — 일반 로드가 5개 파일에서 죽는다.** `openpyxl.load_workbook()` 일반 모드가 다음 5종에서 `ColumnDimension.__init__() got an unexpected keyword argument` 오류로 실패한다: `1300A 독립성준수검토조서`, `21002700 위험평가`(시트 21), `4000 계정별 실증절차`(시트 36), `70007570 그룹감사`(시트 23), `91009800 내부회계관리제도`(시트 25). **read_only 모드 폴백으로 5종 전부 성공 확인.** 공교롭게 코퍼스에서 가장 큰 파일들이므로 폴백은 선택이 아니라 필수다.

**함정 2 — read_only 모드는 병합 정보 접근에 제약이 있다.** 폴백 경로에서 병합 범위가 필요하면 `zipfile`로 `xl/worksheets/sheet*.xml`의 `<mergeCells>` 요소를 직접 파싱하는 3차 안전망을 쓴다.

**함정 3 — formula와 cached_value는 한 번의 로드로 못 얻는다.** openpyxl은 `data_only=False`에서 수식 원문을, `data_only=True`에서 마지막 저장 계산값을 준다. 둘 다 필요하므로 **이중 로드**하고 셀 단위로 병합한다.

**함정 4 — 정의된 이름이 심각하게 오염되어 있다.** 예: `11001300 감사계약.xlsx` 한 파일에 정의된 이름 **1,363개**. 내용은 `#REF!` 깨진 참조 다수, 90년대 파일 경로(`C:\My Documents\98년\...`, `.mdb` 경로), 과거 작성자 실명·이메일, 자판 연타 이름들. 동일 오염 세트가 여러 파일에 복제되어 있다(971개 계열도 별도 존재). **전량을 산출물 본문에 넣으면 노이즈가 본문을 압도한다** — 그래서 결정 ④(요약+샘플)다.

**함정 5 — .xls 2종(7540, 8400)은 xlrd로만 읽히고 수식 원문 접근이 불가하다.** 값 전사는 가능. 참조 그래프는 P6에 따라 관찰 불가로 표기한다.

**함정 6 — 외부 링크 잔존.** 여러 파일에 외부 통합문서 링크 10개가 남아 있다. 수식의 `[n]시트!참조` 형태 외부 참조는 이 링크 테이블과 조인해야 대상 파일명을 알 수 있다.

**규모 감각(설계 검증용 참고치):** 시트 약 164개, 수식 약 4,000개, 그중 시트 간 참조 약 380개, 병합 셀 2,600개 이상.

**지원 형식:** `.xlsx`(openpyxl), `.xls`(xlrd ≥ 2.0). `.docx` 등 그 외 형식은 명시적 오류 + 힌트 메시지("지원 형식: xlsx, xls. 변환 후 재시도")로 거절한다.

---

## 3. CLI 계약

```text
excel-to-skill convert <파일> [--out DIR] [--force] [--no-annotate]
                       [--force-annotate] [--full-names] [--max-rows N] [--model M]
excel-to-skill convert --all <디렉터리> [동일 옵션]
excel-to-skill annotate <파일 | 패키지경로> [--model M] [--force-annotate]
excel-to-skill review  <패키지경로> --approve | --reject [--note "..."]
excel-to-skill verify  <패키지경로>
```

- `convert`: 전체 파이프라인 실행. 성공 시 **패키지 경로를 stdout 마지막 줄로** 출력(스크립트 연계 계약).
- `--no-annotate`: 해석 계층 생략. **API 키가 없는 환경에서도 결정론 계층 패키지가 완결 성립**해야 한다(P1의 실전 검증 경로).
- `--force`: 캐시 무시, 추출부터 전부 재실행. `--force-annotate`: 주석만 재생성.
- `--max-rows N`: 시트당 행 상한. 기본 5,000(템플릿에는 넉넉, 실조서 안전핀). 초과 시 첫 N행 + 말미 5행만 전사하고 절단 사실을 diagnostics에 기록.
- `annotate`: 기존 패키지에 해석 계층만 (재)생성. `review`: `semantics.json`의 `review.status` 전이 및 SKILL.md 재생성. `verify`: §8의 V1~V3 검증 실행, 실패 시 비영(非零) exit code.
- 기본 출력 디렉터리: `./converted/`. 색인: `converted/_index.json`.

---

## 4. 산출물 계약 (패키지)

### 4.0 패키지 구조와 명명

```text
converted/{원본stem_slug}_{sha256 앞 12자리}/
├── SKILL.md                  # 진입점 [혼합: 골격은 결정론, [해석] 섹션만 해석 계층]
├── meta.json                 # 변환 출처 정보 [결정론]
├── layout/
│   └── {시트명_slug}.html    # 시트별 레이아웃 [결정론]
└── data/
    ├── cells.jsonl           # 셀 원장 [결정론]
    ├── references.json       # 참조 그래프 [결정론]
    ├── diagnostics.json      # 구조 진단 [결정론]
    ├── defined_names_full.json  # --full-names 시에만 [결정론]
    └── semantics.json        # 의미 주석 [해석] — --no-annotate 시 부재
```

slug 규칙: 공백→`_`, 경로 위험 문자 제거, 한글 유지. 시트명 충돌 시 `_2` 접미.

### 4.1 meta.json

가변 값(타임스탬프)이 허용되는 **유일한** 결정론 계층 파일.

```json
{
  "tool": "excel_to_skill",
  "converter_version": "0.1.0",
  "source": { "filename": "감사조서서식_1100~1300 감사계약.xlsx",
              "sha256": "…전체 64자…", "size_bytes": 0, "format": "xlsx" },
  "loader_path": "openpyxl_normal | openpyxl_read_only | openpyxl_read_only+xml_merge | xlrd",
  "sheets": [ { "name": "1100", "dimensions": "A1:F13", "max_row": 13, "max_col": 6 } ],
  "generated_at": "ISO8601",
  "annotation": { "present": false, "annotator_version": null, "review_status": null }
}
```

### 4.2 SKILL.md

**frontmatter** (Claude Agent Skills 규격):
- `name`: 원본 stem의 소문자-하이픈 slug.
- `description`: **2단 생성.**
  - *draft(주석 전/미승인)*: 구조 사실만. 형식: `"스프레드시트 {N}매 — {각 시트의 머리 텍스트 원문을 ' / '로 나열}. (의미 주석 미승인)"`. **머리 텍스트의 결정론 규칙**: 해당 시트 used range에서 (row, col) 사전식 최소 위치의 비공백 텍스트 셀 원문. 규칙이 고정이므로 이 인용은 해석이 아니라 사실 전사다.
  - *approved*: `semantics.workbook_claims`의 최상위 claim 문장을 활용해 재생성.

**본문 구성(순서 고정):**
1. 원본 메타 요약 — 파일명, sha256 앞 12자, converter_version, 시트 수, loader_path.
2. 시트 목록 — 시트명, 머리 텍스트 원문(좌표 병기), used range, 병합 수.
3. 참조 관계 요약 — 시트 간 참조 엣지 수와 대표 3건, "상세: data/references.json".
4. 진단 요약 — 외부 링크 수, 정의된 이름 총수·깨진 참조 수, 절단 여부, "상세: data/diagnostics.json".
5. 리소스 사용법 — 병합·레이아웃은 `layout/*.html`(모든 셀에 `data-cell` 좌표), 셀 검색은 `rg '패턴' data/cells.jsonl`, 참조 추적은 `references.json`의 `edges`/`impacts`, 해석은 `semantics.json`(모든 주장에 evidence 좌표).
6. **[해석]** — `review.status == approved`일 때만 렌더. semantics의 workbook_claims와 시트별 purpose를 evidence 좌표와 함께 서술. draft/부재 시 이 섹션 대신 한 줄: "의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오."

### 4.3 layout/{시트}.html

- `<table data-sheet="1200">` 단일 테이블. 병합은 `colspan`/`rowspan`으로 보존하고 병합 자식 칸의 `<td>`는 생성하지 않는다(스팬 점유 맵으로 스킵).
- 모든 `<td>`에 `data-cell="B4"` 좌표 각인 — 에이전트가 HTML만 읽고도 셀 좌표로 인용 가능하게 하는 핵심 장치.
- 수식 셀: `data-formula` 속성에 원문 보존. 표시 텍스트는 v1.2 규약 계승 — 계산값이 있으면 값, 값 없이 수식만 있으면 `[수식: ='1100'!B4]`.
- 스타일 최소주의: 굵은 글씨 `class="b"`, 테두리 보유 `class="bd"`, 기본값 아닌 배경색만 `style="background:#RRGGBB"`. 그 외 서식(폰트·크기·정렬)은 버린다 — 토큰 예산 보호.
- 행 상한(§3 `--max-rows`) 적용, 절단 시 `<tr>`로 중략 마커 삽입.

### 4.4 data/cells.jsonl

한 줄 = 한 셀. **포함 규칙(결정론):**
- 포함: `value` 또는 `formula`가 있는 모든 셀.
- 추가 포함: 값·수식이 모두 없어도 (a) 병합 범위의 anchor이거나 (b) 테두리 보유이거나 (c) 배경색 보유인 셀 — **빈 입력 슬롯의 사실 보존**(감사조서의 미기재 칸은 의미 있는 구조다).
- 제외: 병합 범위의 자식 셀(anchor에 `merged_range`로 대표됨), 위 조건에 해당 없는 완전 빈 셀.

```json
{"sheet":"1200","cell":"B4","row":4,"col":2,"value":null,"formula":"'1100'!B4","cached_value":0,"data_type":"f","number_format":"General","merged_range":null,"bold":false,"border":true,"fill":null}
```

필드 순서·정렬은 P2에 따라 고정. `.xls` 경로에서는 `formula: null` + `data_type`은 xlrd 타입 매핑, 서식 플래그는 취득 가능한 범위만(불가 항목은 null).

### 4.5 data/references.json

```json
{
  "edges": [
    { "from": "1200!B4", "to": "1100!B4", "formula": "'1100'!B4", "ref_type": "cell" },
    { "from": "…", "to": "1100!A1:B10", "formula": "…", "ref_type": "range" }
  ],
  "impacts": { "1100!B4": ["1200!B4", "1300!B4"] },
  "external_refs": [ { "cell": "…", "raw": "[2]2600!A1", "target": "외부링크 색인 2의 대상 경로(마스킹 규칙 적용)" } ],
  "unresolved": [ { "cell": "…", "formula": "INDIRECT(…)", "reason": "indirect" } ],
  "observability": { "workbook": "full | unavailable_xls", "note": null }
}
```

**수식 파서 요구사항** (완전한 수식 AST는 불요 — 토크나이저 + 정규식 수준이면 충분하나, 아래는 반드시 처리):
1. 따옴표 시트명 `'시트 명'!A1` (공백·특수문자 시트명).
2. 절대 참조 `$A$1` → 좌표는 `A1`로 정규화하되 `formula` 필드에는 원문 보존.
3. 범위 참조 `A1:B10` → **범위 노드 하나**로 기록(셀 단위 폭발 금지).
4. 외부 통합문서 `[n]시트!참조` → 외부 링크 테이블과 조인해 `external_refs`로 분리.
5. `INDIRECT`/`OFFSET` 등 동적 참조 → 정적 해석 시도하지 말고 `unresolved`로.
6. `impacts`는 `edges`에서 **파생**한 역인덱스(별도 진실 원천 아님).

### 4.6 data/diagnostics.json

전 항목이 기계적 사실이어야 한다(P3). 최소 항목:

```json
{
  "loader_path": "…",
  "external_links": { "count": 10, "targets_sample": ["…(경로 마스킹)"] },
  "defined_names": { "total": 1363, "broken_ref_count": 0, "legacy_path_count": 0,
                     "samples": [ { "name": "…", "value_head": "…60자…", "flags": ["broken_ref"] } ],
                     "sample_cap": 20, "full_dump_present": false },
  "pii_suspects": { "emails_masked": ["j***@…"], "legacy_paths_count": 0 },
  "blank_source_formulas": [ { "cell": "1200!B4", "source": "1100!B4" } ],
  "hidden": { "sheets": [], "rows_count": 0, "cols_count": 0 },
  "truncations": [ { "sheet": "…", "kept_rows": 5000, "total_rows": 0 } ],
  "format_limitations": null
}
```

`blank_source_formulas`: 참조 원본 셀이 빈 값이라 표시값이 0/이상 날짜로 보일 수 있는 수식 목록 — 사실 기록이며, "확인 필요" 같은 권고 문장은 쓰지 않는다.

### 4.7 data/semantics.json — 해석 계층 (L4)

```json
{
  "generator": { "model": "…", "annotator_version": "0.1.0",
                 "prompt_sha": "…prompts/annotator_v1.md의 sha256…",
                 "temperature": 0, "generated_at": "ISO8601" },
  "review": { "status": "draft", "reviewed_at": null, "note": null },
  "workbook_claims": [
    { "claim": "감사계약 단계(수임~독립성 확인)의 조서 묶음",
      "evidence": ["1100!A2", "1200!A2", "1300!A2"], "confidence": 0.95 }
  ],
  "sheets": [
    { "name": "1300",
      "purpose": "독립성·윤리·품질관리기준 준수의 문서화",
      "evidence": ["1300!A2", "1300!A8:A16"], "confidence": 0.9,
      "sections": [
        { "range": "A4:F5", "semantic_type": "metadata_fields",
          "fields": [ { "label_cell": "A4", "value_cell": "B4", "role": "회사명 입력 슬롯" } ],
          "evidence": ["1300!A4", "1300!B4"], "confidence": 0.9 }
      ] }
  ]
}
```

- `semantic_type` 권장 어휘(개방형 — 자유 문자열 허용): `title`, `metadata_fields`, `table_header`, `procedure_item`, `checklist`, `signature_block`, `reference_note`, `input_slot_group`, `other`.
- 블록의 의미 판별(문서 구역이 제목인지, 키-값 입력부인지 등)은 **휴리스틱 코드로 구현하지 않는다.** 전부 어노테이터의 몫이며 evidence를 동반한다. (근거: 46종 이질 템플릿에서 휴리스틱 오탐 + 결정론 계층의 해석 오염 방지.)

---

## 5. 로더 사양 (폴백 사다리)

```text
1차  openpyxl 일반 로드 (data_only=False → 수식 / data_only=True → 캐시값, 이중 로드)
2차  1차 예외 시: read_only=True 폴백 (동일하게 이중 로드)
3차  2차에서 병합 정보 미취득 시: zipfile로 sheet*.xml <mergeCells> 직파싱
4차  .xls: xlrd (formatting_info=True). 수식 원문 없음 → P6 표기
```

- 어떤 경로로 열렸는지 `meta.loader_path`와 `diagnostics.loader_path`에 기록한다.
- 로더 예외는 파일 단위로 격리한다 — `--all` 배치에서 한 파일의 실패가 전체를 중단시키지 않고, 실패 목록을 종료 시 요약 출력한다.

---

## 6. 캐시 사양 (2단)

| 층 | 키 | 무효화 트리거 |
|---|---|---|
| 추출 캐시(결정론 계층) | `sha256(file) + converter_version` | 파일 내용 변경 또는 변환기 버전 업 |
| 주석 캐시(해석 계층) | `sha256(file) + annotator_version + model + prompt_sha` | 파일 변경, 어노테이터/모델/프롬프트 변경 |

- `converted/_index.json` 항목: `{원본명, sha256, 패키지경로, converter_version, annotation_key, review_status, 최종생성시각}`.
- **승계 규칙**: `converter_version`만 올라 결정론 계층을 재생성하는 경우, 원본 sha와 annotation_key가 동일하면 기존 `semantics.json`을 승계한다(해석은 원본 파일에 대한 것이므로). 단 승계 직후 §8 V2(evidence 실재성)를 재검증하고, 실패 시 `review.status`를 `draft`로 강등하고 그 사유를 `review.note`에 기록한다.
- 같은 파일이 이름만 바뀌어 와도 sha 히트(v1.2 계승). 셀 하나만 바뀌어도 미스.

---

## 7. 어노테이터 사양 (해석 계층 생성기)

- **입력 자료**: 해당 통합문서의 `layout/*.html`(구조 감각) + `cells.jsonl`(좌표 근거) + `references.json` 요약. 시트 단위로 처리하며, 큰 시트의 분할·발췌 전략은 구현 재량(편차 기록 대상 아님).
- **호출 규약**: temperature 0, 응답은 §4.7 스키마의 JSON만(설명 텍스트 금지). 스키마 불일치 시 오류 내용을 첨부해 1회 재시도, 재실패 시 해당 시트를 `sheets`에서 제외하고 diagnostics가 아닌 **stderr와 exit code로** 보고(진단 파일은 결정론 계층이므로 LLM 실패 기록을 넣지 않는다).
- **지식 사용 규칙**: 일반 도메인 지식(회계·감사 용어 이해 등)은 사용 가능. 단 **모든 주장은 이 파일의 셀에서 evidence를 인용**해야 하며, 파일 외부의 특정 데이터셋·정답표를 참조하는 코드 경로는 금지(§0.2).
- **프롬프트 관리**: `prompts/annotator_v1.md` 단일 파일. `prompt_sha`는 이 파일의 sha256. 프롬프트에는 P4(근거 강제), 개방형 어휘, "관찰한 것만 주장하고 불확실하면 confidence를 낮춰라"를 명문화한다.
- **모델·키**: `ANTHROPIC_API_KEY` 환경변수 + `--model` 플래그. 기본 모델명은 코드 상수 1곳 + README에만 기재(본 지시서에 하드코딩하지 않음 — 구현 시점의 안정 모델을 선택하라).
- **review 명령**: `--approve` 시 `review.status=approved`, `reviewed_at` 기록, SKILL.md를 의미 기반 description과 [해석] 섹션 포함으로 재생성. `--reject` 시 `rejected` + note 필수.

---

## 8. 검증과 수용 기준

### 8.1 verify 명령 (패키지 단위, 3종)

- **V1 스키마 검증**: `schemas/*.schema.json`(JSON Schema)으로 references/diagnostics/semantics/meta 검증.
- **V2 evidence 실재성**: semantics의 모든 evidence 좌표가 (a) 형식 유효, (b) 실존 시트, (c) 해당 시트 used range 내인지 검증. **approve 전 필수 통과.**
- **V3 재현성**: 동일 입력 2회 변환 → 결정론 계층 파일들이 동일한지 비교(meta.json은 `generated_at` 필드 제외 정규화 비교).

### 8.2 코퍼스 수용 기준 (마일스톤 완료 판정)

- **V4 전수 변환**: 대상 32종 전수 성공(성공률 100%). 함정 1의 5개 파일은 `loader_path`가 read_only 계열로 기록되어야 함.
- **V5 골든 참조 체크**: `11001300 감사계약` 패키지의 references.json에 다음 6개 엣지가 정확히 존재 — `1200!B4→1100!B4`, `1200!D4→1100!D4`, `1200!B5→1100!B5`, `1300!B4→1100!B4`, `1300!D4→1200!D4`, `1300!B5→1100!B5`.
- **V6 진단 골든 체크**: 같은 패키지의 diagnostics에 `external_links.count == 10`, `defined_names.total == 1363`, 샘플 수 ≤ 20, 이메일 마스킹 적용.
- **V7 .xls 경로**: 7540·8400 두 파일 변환 성공 + references의 `observability == "unavailable_xls"`.
- **V8 무키 환경**: `ANTHROPIC_API_KEY` 미설정 상태에서 `--no-annotate --all` 전수 성공.
- **V9 픽스처 스냅샷**: 자체 제작 소형 xlsx 픽스처 3종(각각 병합+수식, 시트간 참조+범위 참조+INDIRECT, 빈 입력슬롯+숨김 행·시트 포함)에 대한 산출물 스냅샷 테스트를 pytest로 고정.

---

## 9. 마일스톤과 보고

| 단계 | 범위 | 수용 기준 |
|---|---|---|
| **M1 결정론 추출** | 로더 사다리(§5), extractor(내부 Workbook IR — 외부 파일로 내보내지 않음), cells/references/diagnostics/meta 산출, 2단 캐시 중 추출 캐시, `convert --no-annotate`, `verify`(V1·V3), 픽스처 테스트 | V1, V3, V4, V5, V6, V7, V8, V9 |
| **M2 표현 계층** | layout HTML 렌더러, SKILL.md draft 생성기, `--all` 일괄 시드, `--max-rows` 절단 | M1 기준 유지 + 32종 시드 완료 + HTML data-cell 각인 확인 |
| **M3 해석 계층** | annotator, semantics 스키마·V2 검증, 주석 캐시, `annotate`/`review` 명령, 승인판 SKILL.md 재생성, 32종 draft 주석 일괄 시드 | V2 포함 전체 + draft 시드 32종 |

- 각 마일스톤 종료 시 보고: 커밋 SHA, 수용 기준 체크표(항목별 통과/실패), 편차 목록(§10), 다음 단계 착수 전 검토 대기.
- 내부 Workbook IR은 파이썬 자료구조로만 존재한다. `workbook.ir.json` 류의 중간 파일을 산출물에 추가하지 않는다(패키지 표면적 최소화 — 확정 결정).

---

## 10. 편차 관리

- 지시서와 실물이 충돌하면 **실물이 이긴다 — 단, 기록하라.** 증거 기반 편차는 허용하며, 리포 루트 `DEVIATIONS.md`에 `D-번호 / 지시서 조항 / 실물 증거 / 취한 조치` 형식으로 기록 후 마일스톤 보고에 포함해 검토를 요청한다. (선행 프로젝트에서 구현자의 증거 기반 편차가 지시서 오류를 여러 차례 바로잡은 관행의 계승이다.)
- 단 §1 불변 원칙(P1~P7)과 §0.3 확정 결정의 변경은 편차가 아니라 **설계 변경**이며, 구현 전 승인을 받아야 한다.

---

## 11. 기술 스택과 저장소 골격

- Python 3.11+, `openpyxl`(3.1.x — 호환성 문제는 §5 폴백으로 흡수), `xlrd`(≥2.0, xls 전용), `anthropic`(어노테이터 전용 — M1·M2는 import조차 하지 않는 모듈 경계 유지), `jsonschema`, 표준 `zipfile`/`xml`. 테스트 `pytest`. 패키징 `pyproject.toml`(uv 권장). 무거운 프레임워크 금지.

```text
Excel_to_SKILL/
├── src/excel_to_skill/
│   ├── cli.py            # §3 명령 라우팅
│   ├── loader.py         # §5 폴백 사다리
│   ├── extractor.py      # 내부 Workbook IR
│   ├── emit_cells.py / emit_refs.py / emit_diag.py / emit_html.py / emit_skill_md.py
│   ├── refparse.py       # §4.5 수식 참조 파서
│   ├── annotator.py      # §7 (해석 계층 전용 — anthropic import는 여기서만)
│   └── cache.py          # §6
├── prompts/annotator_v1.md
├── schemas/{meta,references,diagnostics,semantics}.schema.json
├── tests/  (픽스처 xlsx 포함)
├── DEVIATIONS.md
└── README.md             # 사용법, 기본 모델, 패키지 계약 요약
```

---

## 부록 A. 소비 시나리오 (수용의 최종 감각)

Claude Code 에이전트에게 패키지 디렉터리만 주고 "이 조서의 목적과 작성해야 할 항목, 각 판단의 근거 셀은?"이라고 물었을 때 — 에이전트가 SKILL.md로 진입해, semantics의 주장을 evidence 좌표로 검증하거나(승인본) 구조 데이터만으로 스스로 해석하며(draft/`--no-annotate`), `rg`로 cells.jsonl을 검색하고 references.json으로 시트 간 전파를 추적해, **모든 답변 문장에 `시트!셀` 근거를 달 수 있는 상태** — 이것이 이 도구가 성공한 상태다.

*— 작업지시서 끝. 문의·편차는 마일스톤 보고에 취합.*
