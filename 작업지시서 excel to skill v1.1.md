# 작업지시서 — Excel_to_SKILL v1.1

| 항목 | 내용 |
|---|---|
| 발행일 | 2026-07-08 (v1.0 동일자 개정) |
| 상태 | 확정 (구현 착수 가능) |
| 구현 담당 | Claude Code |
| 검토·승인 | 선생님 (프로젝트 오너) |
| 선행 문서 | 본 문서 v1.0 — **본 개정판으로 전면 대체.** auditPaper_assist Stage 3 v1.2 — 본 프로젝트로 대체 |

## 개정 이력 (v1.0 → v1.1)

M1 로더 구현 검증 결과 2건과 DOCX 확장 설계 합의를 반영한 통합 개정이다.

1. **§5 used range 재계산 규칙 명문화** — 구현자 편차 D-01 승인·소급 명문화(부록 B).
2. **§4.4 직렬화 규칙 추가** — 날짜·시각 등 비JSON 타입의 ISO 8601 고정 직렬화(P2 보강).
3. **§4.6·V6 정의된 이름 이원 집계** — 전역/시트 스코프 분리. 근거: M1 검증에서 시트 스코프 594개 추가 발견(함정 7).
4. **§12 신설 — DOCX 확장(M4)** — 분기 ⑤ 개정. 봉투 동일·주소 형식 고유 원칙.
5. **분기 ⑧ 확정** — 앵커 속성명 병행(xlsx `data-cell` / docx `data-anchor`) + 공통 값 계약.
6. **분기 ⑨ 확정** — 본문 텍스트 인용(기준서 등) 추출은 결정론 계층에서 제외.
7. **P6 확장** — 관찰 3상태(관찰됨 / unavailable / not_applicable).
8. §9 마일스톤에 M4 추가, §8 V2에 형식별 주소 파서, §2·§3·§7·§11 자구 정합.

---

## 0. 목적과 위치

### 0.1 한 줄 정의

임의의 스프레드시트(xlsx·xls)와 워드 문서(docx)를 읽어, AI 에이전트가 **근거(주소) 추적 가능하게** 소비할 수 있는 스킬 패키지 — Markdown 진입점(SKILL.md) + HTML 레이아웃 + JSON/JSONL 데이터 + LLM 의미 주석 — 로 변환하는 독립 CLI 도구.

### 0.2 리포지토리와 경계

- 신규 독립 리포지토리: `worldoftoddl/Excel_to_SKILL`. 도구명은 유지하고 README에 지원 범위(스프레드시트 + 워드)를 명시한다. docx 지원은 M4 확장 모듈이다.
- 이 리포는 **auditPaper_assist의 자산(routing_gold.json, 기준서 코퍼스, MCP 서버)을 일절 참조하지 않는다.** 이유: 범용 도구로 성립해야 하며, 특정 프로젝트의 정답 데이터에 결합되면 도구의 독립성과 소비 측 평가의 청결성이 모두 훼손된다.
- 역방향 소비(auditPaper_assist가 본 도구의 산출물을 입력으로 쓰는 것)는 그쪽 프로젝트의 통합 규칙이며 본 리포의 관심사가 아니다.
- 후속 조치(본 리포 범위 외, 기록용): auditPaper_assist의 `docs/workorders/` 내 Stage 3 v1.2 지시서에 "Excel_to_SKILL로 대체됨" 표기.

### 0.3 확정된 설계 결정

구현 중 이 결정을 뒤집는 편차는 §10 절차를 따른다.

| # | 분기 | 결정 |
|---|---|---|
| ① | 프로젝트 위치 | 독립 리포 (v1.2 흡수·대체) |
| ② | SKILL.md 규격 | Claude Agent Skills frontmatter(`name`, `description`) 정식 준수 |
| ③ | 본문 표현 | **HTML 일원화.** 마크다운 본문 전사 없음. 근거 보강: 대상 조서의 마크다운 변환 실물에서 서식 오염(`****` 노이즈 90회/문서)과 표 병합 소실(다열 표 내 1칸 구간 헤더) 확인 — 마크다운은 주소 속성과 colspan/rowspan을 표현할 수 없다. SKILL.md가 마크다운의 자리다 |
| ④ | 정의된 이름 | diagnostics에 이원 집계(전역/시트 스코프)+샘플(상한 20). 전량 덤프는 `--full-names` |
| ⑤ | 대상 범위 (개정) | M1~M3: 스프레드시트 32종(xlsx 30 + xls 2). **M4: docx 14종 편입.** hwp·legacy .doc은 범위 외(거절+힌트). 값 채워진 실조서 최적화는 후속 과제(행 상한 안전핀만 선반영) |
| ⑥ | 검토 워크플로 | semantics에 `review.status`(draft/approved/rejected) 도입 |
| ⑦ | description 생성 | 2단 — draft는 구조 사실 기반, approved 후 의미 기반으로 재생성 |
| ⑧ | 앵커 속성명 (신규) | **형식별 병행**: xlsx `data-cell`, docx `data-anchor`. 공통 계약은 §4.3/§12.3의 값 일치 조항 — 속성명은 어휘, 값 규칙이 약속이다 |
| ⑨ | 본문 인용 추출 (신규) | "K-IFRS 1115 문단 60" 류 텍스트 인용의 정규식 추출은 **결정론 계층에서 제외.** 도메인 어휘를 범용 도구에 심지 않는다. 해당 해석은 어노테이터가 evidence와 함께 수행하거나 소비 측 후처리의 몫. (`--ref-patterns` 사용자 정규식 훅은 범위 외 아이디어로 기록만) |
| L4 | 의미 계층 | 변환기 산출물로 포함(`semantics.json`). §1의 규율 3종(출처 2계급, 근거 강제, 캐시 층 분리) 적용 |

---

## 1. 불변 원칙 (위반 시 반려)

**P1. 출처 2계급.** 산출물은 두 계급으로 나뉘며 파일 단위로 분리된다. 혼입 금지.
- **결정론 계층**: `meta.json`, 원장(`cells.jsonl`/`blocks.jsonl`), `references.json`, `diagnostics.json`, `layout/*.html` — 코드만으로 생성. LLM 호출 0회.
- **해석 계층**: `semantics.json` 및 SKILL.md의 [해석] 섹션 — LLM 생성. 동결·검토 대상.

**P2. 결정론 보장.** 동일 입력 파일 + 동일 `converter_version` → 결정론 계층은 재실행 시 동일 출력. 타임스탬프류 가변 값은 `meta.json`에만 존재한다. 모든 `.json`은 `sort_keys=True, indent=2, ensure_ascii=False`, 원장 jsonl은 고정 필드 순서 + 고정 정렬(스프레드시트: 시트 순 → row → col / docx: 문서 순서)로 직렬화한다.

**P3. 원문 충실.** 결정론 계층은 문서에 있는 것만 옮긴다. 요약·재서술·정규화·트리밍 금지(값은 원문 그대로; HTML 이스케이프는 표현 규칙으로 허용). 의미 부여("이 시트는 ~용 조서다", "이 블록은 입력 영역이다")는 결정론 계층 어디에도 들어가지 않는다 — 전부 `semantics.json`의 몫이다. **존재하지 않는 구조의 날조도 금지다**: docx에 셀 격자 좌표를 발명하지 않는다(§12.2).

**P4. 근거 강제.** `semantics.json`의 모든 주장(claim/purpose/section)은 `evidence`(주소 또는 범위 배열)와 `confidence`를 가진다. 근거 없는 주장은 스키마 검증에서 거부한다.

**P5. 생성 메타 박제.** 해석 계층은 `generator` 블록(model, annotator_version, prompt_sha, temperature, generated_at)을 필수 포함한다.

**P6. 관찰 3상태의 구분.** ① 관찰됨(값 또는 빈 배열 — 빈 배열은 "개념이 있고 봤으나 없었음"), ② `unavailable` — 형식 한계로 볼 수 없음(예: .xls의 수식 원문), ③ `not_applicable` — 형식에 그 개념 자체가 없음(예: docx의 수식 그래프). 에이전트가 "참조가 없다"고 오추론하지 않게 하기 위함이다.

**P7. 개인정보 마스킹.** 진단 샘플의 이메일은 로컬파트 마스킹(`j***@domain`), 실명 의심 문자열은 인용하지 않는다. 대상 코퍼스에 90년대 작성자 실명·이메일이 실재함이 확인되었다(§2 함정 4).

---

## 2. 대상 입력과 실측 제약 (구현 전 필독)

2026-07-08 기준, openpyxl 3.1.5 환경에서 스프레드시트 32종 전수 검증 + M1 로더 구현 검증 + docx 사본 조사의 결과다. 아래 함정은 추측이 아니라 **관측 사실**이다.

**함정 1 — 일반 로드가 5개 파일에서 죽는다.** `openpyxl.load_workbook()` 일반 모드가 다음 5종에서 `ColumnDimension.__init__() got an unexpected keyword argument` 오류로 실패한다: `1300A 독립성준수검토조서`, `21002700 위험평가`(시트 21), `4000 계정별 실증절차`(시트 36), `70007570 그룹감사`(시트 23), `91009800 내부회계관리제도`(시트 25). read_only 폴백으로 5종 전부 성공이 구현 검증에서 재확인되었다.

**함정 2 — read_only 모드는 병합 정보 접근에 제약이 있다.** 폴백 경로에서는 `zipfile`로 `xl/worksheets/sheet*.xml`의 `<mergeCells>`를 직접 파싱한다(3차 안전망). 일반 로드 파일에서 openpyxl 병합과 XML 직파싱 결과의 일치가 교차 검증되었다.

**함정 3 — formula와 cached_value는 한 번의 로드로 못 얻는다.** `data_only=False`에서 수식 원문, `data_only=True`에서 마지막 저장 계산값. **이중 로드** 후 셀 단위 병합.

**함정 4 — 정의된 이름이 심각하게 오염되어 있다.** `11001300 감사계약.xlsx` 한 파일에 전역 정의 이름 1,363개(#REF! 429, 레거시 경로 32, 90년대 실명·이메일 포함). 동일 오염 세트가 여러 파일에 복제되어 있다.

**함정 5 — .xls 2종(7540, 8400)은 xlrd로만 읽히고 수식 원문 접근 불가.** 값 전사는 가능. 참조 그래프는 P6 `unavailable`. 추가 관측: 두 파일은 시트 5개 중 2개가 숨김 상태이고 셀 5.7만·병합 6천 규모의 대형 점검표이며 사실상 동일 원본의 사본이다 — `diagnostics.hidden.sheets`의 실재 대상.

**함정 6 — 외부 링크 잔존.** 여러 파일에 외부 통합문서 링크 10개. 수식의 `[n]시트!참조`는 이 링크 테이블과 조인해야 대상 파일명을 알 수 있다.

**함정 7 — 정의된 이름은 두 계열이다 (v1.1 신규).** M1 구현 검증에서 감사계약 파일의 전역 1,363개 외에 **시트 스코프 594개**가 별도 발견되었다. 진단과 골든 체크(V6)는 두 계열을 분리 집계한다.

**규모 감각(참고치):** 스프레드시트 — 시트 약 164, 수식 약 4,048(구현 검증에서 사전 실측과 정확 일치 확인), 시트 간 참조 약 380, 병합 2,600+. docx — 표 비중 0~96%의 3형상(표 중심 / 혼합 / 순수 산문), 체크박스 글리프 최대 255개/문서(텍스트 사본 기준 근사치; 실물 수치는 M4-a에서 확정).

**지원 형식:** `.xlsx`(openpyxl), `.xls`(xlrd ≥ 2.0), `.docx`(python-docx + lxml, M4부터). 그 외(hwp, legacy .doc 등)는 명시적 오류 + 힌트("지원 형식: xlsx, xls, docx. 변환 후 재시도하십시오")로 거절한다.

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

- `convert`: 전체 파이프라인 실행. 성공 시 **패키지 경로를 stdout 마지막 줄로** 출력(스크립트 연계 계약). M4 이후 docx도 동일 명령으로 수용.
- `--no-annotate`: 해석 계층 생략. **API 키가 없는 환경에서도 결정론 계층 패키지가 완결 성립**해야 한다.
- `--force`: 캐시 무시, 추출부터 전부. `--force-annotate`: 주석만 재생성.
- `--max-rows N`: **표 행 상한**(스프레드시트 시트 행 / docx 표 행). 기본 5,000. 초과 시 첫 N행 + 말미 5행 전사 + diagnostics 기록. docx 문단은 별도 안전핀 10,000개(초과 시 동일 규칙).
- `annotate` / `review` / `verify`: v1.0과 동일. `verify`는 §8의 V1~V3, 실패 시 비영 exit code.
- 기본 출력: `./converted/`, 색인 `converted/_index.json`.

---

## 4. 산출물 계약 — 공통 골격과 스프레드시트 사양

### 4.0 패키지 구조와 명명

```text
converted/{원본stem_slug}_{sha256 앞 12자리}/
├── SKILL.md                  # 진입점 [혼합: 골격은 결정론, [해석] 섹션만 해석 계층]
├── meta.json                 # 변환 출처 정보 [결정론]
├── layout/                   # [결정론]
│   └── {시트명_slug}.html    #   스프레드시트: 시트별 / docx: body.html 단일(§12.3)
└── data/
    ├── cells.jsonl           # 원장 [결정론] — docx는 blocks.jsonl(§12.4)
    ├── references.json       # 참조 그래프 [결정론]
    ├── diagnostics.json      # 구조 진단 [결정론]
    ├── defined_names_full.json  # --full-names 시에만 (스프레드시트) [결정론]
    └── semantics.json        # 의미 주석 [해석] — --no-annotate 시 부재
```

slug 규칙: 공백→`_`, 경로 위험 문자 제거, 한글 유지, 시트명 충돌 시 `_2` 접미. **봉투(파일 배치·역할·semantics 스키마·캐시·CLI·P1~P7)는 형식 공통이고, 주소 문법과 원장 스키마는 형식 고유다** — 어느 부분이 어느 쪽인지의 판별 기준: *형식을 모르는 공용 장치가 만지는 부분인가?* 만지면 공통, 아니면 형식대로.

### 4.1 meta.json

가변 값(타임스탬프)이 허용되는 유일한 결정론 계층 파일.

```json
{
  "tool": "excel_to_skill",
  "converter_version": "0.1.0",
  "source": { "filename": "…", "sha256": "…64자…", "size_bytes": 0, "format": "xlsx|xls|docx" },
  "loader_path": "openpyxl_normal | openpyxl_read_only | openpyxl_read_only+xml_merge | xlrd | python_docx",
  "sheets": [ { "name": "1100", "dimensions": "A1:F13", "max_row": 13, "max_col": 6 } ],
  "generated_at": "ISO8601",
  "annotation": { "present": false, "annotator_version": null, "review_status": null }
}
```

`sheets[].dimensions`는 dimension 레코드가 아니라 **§5의 재계산 used range**다(D-01). docx는 `sheets` 대신 `body: {"paragraphs": N, "tables": K}`를 기록한다.

### 4.2 SKILL.md

**frontmatter** (Claude Agent Skills 규격): `name` = 원본 stem의 소문자-하이픈 slug. `description` = 2단 생성 —
- *draft*: 구조 사실만. `"스프레드시트 {N}매 — {각 시트 머리 텍스트 원문을 ' / '로 나열}. (의미 주석 미승인)"`. **머리 텍스트 결정론 규칙**: 시트 used range에서 (row, col) 사전식 최소 위치의 비공백 텍스트 셀 원문. docx판은 §12.8.
- *approved*: `semantics.workbook_claims` 최상위 claim 문장으로 재생성.

**본문 구성(순서 고정):** ① 원본 메타 요약(파일명·sha 12자·converter_version·구성 요약·loader_path) ② 구성 목록(시트/본문 개요 — 머리 텍스트 원문과 주소 병기) ③ 참조 관계 요약(엣지 수·대표 3건, docx는 P6 상태 명기) ④ 진단 요약 ⑤ **리소스 사용법 — 이 패키지의 원장 파일명과 앵커 속성명(⑧)을 여기서 명시**("이 패키지는 `data-cell`을 씁니다" / "`data-anchor`를 씁니다") ⑥ **[해석]** — `review.status == approved`일 때만 렌더, 미승인 시 "의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오" 한 줄.

### 4.3 layout/{시트}.html (스프레드시트)

- `<table data-sheet="1200">` 단일 테이블. 병합은 `colspan`/`rowspan`, 병합 자식 칸의 `<td>`는 생성하지 않는다(스팬 점유 맵).
- 모든 `<td>`에 `data-cell="B4"` 각인. **공통 값 계약(⑧)**: 레이아웃 HTML의 모든 원장 대응 요소는 자기 주소를 data-속성 값으로 각인하며, 그 값은 원장의 주소 문자열과 **문자 단위로 일치**한다. 속성명은 형식 고유(xlsx `data-cell`, docx `data-anchor`)이며 각 패키지 SKILL.md 사용법 절에 명시된다.
- 수식 셀: `data-formula` 속성에 원문. 표시 텍스트는 계산값이 있으면 값, 없으면 `[수식: ='1100'!B4]`.
- 스타일 최소주의: 굵게 `class="b"`, 테두리 `class="bd"`, 기본값 아닌 배경색만 `style="background:#RRGGBB"`. 그 외 서식은 버린다.
- `--max-rows` 적용, 절단 시 `<tr>` 중략 마커.

### 4.4 data/cells.jsonl (스프레드시트)

한 줄 = 한 셀. **포함 규칙(결정론):** `value` 또는 `formula`가 있는 모든 셀 + 값·수식이 없어도 (a) 병합 anchor (b) 테두리 보유 (c) 배경색 보유인 셀(빈 입력 슬롯의 사실 보존). 병합 자식 셀과 그 외 완전 빈 셀은 제외.

```json
{"sheet":"1200","cell":"B4","row":4,"col":2,"value":null,"formula":"'1100'!B4","cached_value":0,"data_type":"f","number_format":"General","merged_range":null,"bold":false,"border":true,"fill":null}
```

**직렬화 규칙(v1.1 신규, P2 보강):** `value`/`cached_value`가 날짜·시각 계열(datetime, date, time)이면 ISO 8601 문자열로 고정 직렬화한다(예: `time(0,0)` → `"00:00:00"`). 실측 근거: 감사계약 `1200!B5`의 cached_value가 `datetime.time(0, 0)`으로 관측됨 — 빈 날짜 원본을 참조하는 수식의 전형이며, 이 케이스는 diagnostics의 `blank_source_formulas`에도 잡힌다. `.xls` 경로에서는 `formula: null` + 서식 플래그는 취득 가능 범위만(불가 항목 null).

### 4.5 data/references.json

```json
{
  "edges": [ { "from": "1200!B4", "to": "1100!B4", "formula": "'1100'!B4", "ref_type": "cell" } ],
  "impacts": { "1100!B4": ["1200!B4", "1300!B4"] },
  "external_refs": [ { "cell": "…", "raw": "[2]2600!A1", "target": "외부링크 색인 2의 대상(마스킹 규칙 적용)" } ],
  "unresolved": [ { "cell": "…", "formula": "INDIRECT(…)", "reason": "indirect" } ],
  "observability": { "workbook": "full | unavailable_xls | not_applicable_docx", "note": null }
}
```

**수식 파서 요구**(토크나이저+정규식 수준으로 충분, 완전 AST 불요): ① 따옴표 시트명 `'시트 명'!A1` ② `$` 절대 참조 — 좌표는 정규화, `formula`엔 원문 보존 ③ 범위는 **범위 노드 하나**(셀 폭발 금지) ④ `[n]` 외부 참조는 링크 테이블과 조인해 분리 ⑤ INDIRECT/OFFSET은 `unresolved` ⑥ `impacts`는 `edges`의 파생 역인덱스.

### 4.6 data/diagnostics.json

전 항목이 기계적 사실(P3). 최소 항목:

```json
{
  "loader_path": "…",
  "external_links": { "count": 10, "targets_sample": ["…(마스킹)"] },
  "defined_names": { "global_total": 1363, "sheet_scoped_total": 594,
                     "broken_ref_count": 0, "legacy_path_count": 0,
                     "samples": [ { "name": "…", "value_head": "…60자…", "flags": ["broken_ref"] } ],
                     "sample_cap": 20, "full_dump_present": false },
  "pii_suspects": { "emails_masked": ["j***@…"], "legacy_paths_count": 0 },
  "blank_source_formulas": [ { "cell": "1200!B4", "source": "1100!B4" } ],
  "hidden": { "sheets": [], "rows_count": 0, "cols_count": 0 },
  "truncations": [],
  "format_limitations": null
}
```

권고 문장("확인 필요" 등) 금지 — 사실만. docx 항목은 §12.6.

### 4.7 data/semantics.json — 해석 계층 (형식 공통)

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
    { "name": "1300", "purpose": "독립성·윤리·품질관리기준 준수의 문서화",
      "evidence": ["1300!A2", "1300!A8:A16"], "confidence": 0.9,
      "sections": [
        { "range": "A4:F5", "semantic_type": "metadata_fields",
          "fields": [ { "label_cell": "A4", "value_cell": "B4", "role": "회사명 입력 슬롯" } ],
          "evidence": ["1300!A4", "1300!B4"], "confidence": 0.9 } ] }
  ]
}
```

- **스키마는 형식 공통** — evidence가 문자열 배열이므로 주소 문법만 형식을 따른다(docx는 `["p3","t1!r5c2"]`). docx 패키지에서는 `sheets` 대신 `body_claims`(문서 단위)와 `sections`(주소 범위 기반)를 쓴다 — 세부 필드명은 M4-a 후 확정.
- `semantic_type` 권장 어휘(개방형): `title`, `metadata_fields`, `table_header`, `procedure_item`, `checklist`, `signature_block`, `reference_note`, `input_slot_group`, `letter_body`, `qa_pair`, `other`(자유 문자열 허용).
- 블록 의미 판별은 **휴리스틱 코드로 구현하지 않는다.** 전부 어노테이터의 몫이며 evidence를 동반한다.
- ⑨에 따라, 본문 속 기준서 인용의 식별·구조화도 결정론 계층이 아니라 어노테이터가 evidence와 함께 수행할 수 있는 항목이다.

---

## 5. 로더 사양 (폴백 사다리)

형식 라우팅은 확장자 기준(`.xlsx`/`.xls`/`.docx`)이다.

```text
1차  [xlsx] openpyxl 일반 로드 (data_only=False → 수식 / True → 캐시값, 이중 로드)
2차  [xlsx] 1차 예외 시 read_only=True 폴백 (동일 이중 로드)
3차  [xlsx] 2차에서 병합 미취득 시 zipfile로 sheet*.xml <mergeCells> 직파싱
4차  [xls]  xlrd (formatting_info=True). 수식 원문 없음 → P6 unavailable
5차  [docx] python-docx + lxml 직접 접근 (§12.7)
```

**used range 재계산 규칙 (전 로더 경로 공통 — D-01 명문화):** 시트의 used range는 파일의 dimension 레코드를 신뢰하지 않고, **콘텐츠 실재 기준**(값·수식이 있거나 §4.4 포함 규칙상 유의 서식이 있는 셀)의 최대 행·열로 재계산한다. 원점은 A1 고정. `meta.sheets[].dimensions`와 V2의 "범위 내" 판정이 모두 이 값을 쓴다. 근거: dimension 레코드 과대 기록 실측 — 감사계약 1200 시트, 레코드 `A2:H43` vs 36행 이하 실콘텐츠 0건.

- 어떤 경로로 열렸는지 `meta.loader_path`와 `diagnostics.loader_path`에 기록.
- 로더 예외는 파일 단위 격리 — `--all` 배치에서 한 파일의 실패가 전체를 중단시키지 않고, 실패 목록을 종료 시 요약 출력.

---

## 6. 캐시 사양 (2단, 형식 무관)

| 층 | 키 | 무효화 트리거 |
|---|---|---|
| 추출 캐시(결정론 계층) | `sha256(file) + converter_version` | 파일 변경 또는 변환기 버전 업 |
| 주석 캐시(해석 계층) | `sha256(file) + annotator_version + model + prompt_sha` | 파일·어노테이터·모델·프롬프트 변경 |

- `converted/_index.json` 항목: `{원본명, sha256, 패키지경로, converter_version, annotation_key, review_status, 최종생성시각}`.
- **승계 규칙**: `converter_version`만 올라 결정론 계층을 재생성하는 경우, sha와 annotation_key가 동일하면 `semantics.json` 승계. 승계 직후 V2 재검증, 실패 시 `review.status`를 `draft`로 강등하고 사유를 `review.note`에 기록.

---

## 7. 어노테이터 사양 (해석 계층 생성기, 형식 공통)

- **입력 자료**: 해당 문서의 layout HTML(구조 감각) + 원장 jsonl(주소 근거) + references 요약. 단위 분할(시트별/구간별)과 발췌 전략은 구현 재량(편차 기록 대상 아님).
- **호출 규약**: temperature 0, 응답은 §4.7 스키마 JSON만. 스키마 불일치 시 오류 첨부 1회 재시도, 재실패 시 해당 단위를 결과에서 제외하고 **stderr와 exit code로** 보고(진단 파일은 결정론 계층이므로 LLM 실패 기록을 넣지 않는다).
- **지식 사용 규칙**: 일반 도메인 지식은 사용 가능. 단 모든 주장은 이 파일의 주소에서 evidence를 인용해야 하며, 파일 외부의 특정 데이터셋·정답표를 참조하는 코드 경로는 금지(§0.2).
- **프롬프트 관리**: `prompts/annotator_v1.md` 단일 파일, `prompt_sha`는 그 파일의 sha256. 프롬프트에 P4, 개방형 어휘, "관찰한 것만 주장하고 불확실하면 confidence를 낮춰라"를 명문화.
- **모델·키**: `ANTHROPIC_API_KEY` 환경변수 + `--model` 플래그. 기본 모델명은 코드 상수 1곳 + README에만.
- **review 명령**: `--approve` → `approved`+`reviewed_at`, SKILL.md를 의미 기반으로 재생성. `--reject` → `rejected`+note 필수.

---

## 8. 검증과 수용 기준

### 8.1 verify 명령 (패키지 단위)

- **V1 스키마 검증**: `schemas/*.schema.json`으로 meta/references/diagnostics/semantics 검증.
- **V2 evidence 실재성**: semantics의 모든 evidence가 (a) 형식 유효 (b) 실존 대상 (c) 범위 내인지 검증. 주소 문법은 **형식별 파서 플러그인**으로 — 스프레드시트 `시트!셀|범위`(재계산 used range 기준), docx `p{n} | t{k}!r{r}c{c}(/중첩)`(실존 인덱스 기준). **approve 전 필수 통과.**
- **V3 재현성**: 동일 입력 2회 변환 → 결정론 계층 동일(meta.json은 `generated_at` 제외 정규화 비교).

### 8.2 코퍼스 수용 기준 (M1~M3)

- **V4 전수 변환**: 스프레드시트 32종 100% 성공. 함정 1의 5개 파일은 `loader_path`가 read_only 계열.
- **V5 골든 참조**: 감사계약 references.json에 정확히 다음 6엣지 — `1200!B4→1100!B4`, `1200!D4→1100!D4`, `1200!B5→1100!B5`, `1300!B4→1100!B4`, `1300!D4→1200!D4`, `1300!B5→1100!B5`.
- **V6 골든 진단 (v1.1 개정)**: 감사계약 diagnostics — `external_links.count == 10`, `defined_names.global_total == 1363`, `defined_names.sheet_scoped_total == 594`, 샘플 ≤ 20, 이메일 마스킹 적용.
- **V7 .xls**: 7540·8400 변환 성공 + `observability == "unavailable_xls"` + 숨김 시트 2종이 `hidden.sheets`에 기록.
- **V8 무키 환경**: `ANTHROPIC_API_KEY` 미설정에서 `--no-annotate --all` 전수 성공.
- **V9 픽스처 스냅샷**: 자체 제작 소형 xlsx 3종(병합+수식 / 시트간·범위·INDIRECT 참조 / 빈 입력슬롯+숨김) pytest 스냅샷 고정.

M4의 수용 기준은 §12.9~12.10 — 골든 수치는 M4-a 프로파일이 확정한다.

---

## 9. 마일스톤과 보고

| 단계 | 범위 | 수용 기준 |
|---|---|---|
| **M1 결정론 추출** | 로더 사다리(§5), extractor(내부 IR — 외부 파일로 내보내지 않음), cells/references/diagnostics/meta, 추출 캐시, `convert --no-annotate`, `verify`(V1·V3), 픽스처 | V1, V3, V4, V5, V6, V7, V8, V9 |
| **M2 표현 계층** | layout HTML, SKILL.md draft 생성기, `--all` 시드, `--max-rows` | M1 유지 + 32종 시드 + data-cell 값 계약 확인 |
| **M3 해석 계층** | annotator, semantics 스키마·V2, 주석 캐시, `annotate`/`review`, 승인판 SKILL.md, 32종 draft 시드 | V2 포함 전체 + draft 32종 |
| **M4 DOCX 확장 (v1.1 신설)** | (a) **실물 14종 프로파일 노트북** — §12.9 측정 목록 수행, 골든 수치·스키마 세부 확정 → 부록 C로 본 지시서에 추가 승인 (b) docx 로더·blocks/references/diagnostics/body.html/SKILL.md (c) 어노테이터 docx 경로 + 14종 draft 시드 | 14/14 성공, 골든 3종(§12.10) 체크, V1~V3·V8 준용 |

- 각 마일스톤 종료 시 보고: 커밋 SHA + 수용 기준 체크표 + 편차 목록. 다음 단계 착수 전 검토 대기.
- M1 로더 컴포넌트는 본 개정 시점에 이미 검증·수용되었다(부록 B 참조). M1 잔여는 emit·캐시·verify·픽스처.
- 내부 Workbook IR은 파이썬 자료구조로만 존재한다. `workbook.ir.json` 류 중간 파일 금지.

---

## 10. 편차 관리

- 지시서와 실물이 충돌하면 **실물이 이긴다 — 단, 기록하라.** 증거 기반 편차는 허용하며 `DEVIATIONS.md`에 `D-번호 / 지시서 조항 / 실물 증거 / 취한 조치`로 기록 후 마일스톤 보고에 포함한다. 승인 선례: 부록 B의 D-01.
- 단 §1 불변 원칙(P1~P7)과 §0.3 확정 결정의 변경은 편차가 아니라 **설계 변경**이며, 구현 전 승인이 필요하다.

---

## 11. 기술 스택과 저장소 골격

- Python 3.11+, `openpyxl`(3.1.x — 호환성 문제는 §5 폴백으로 흡수), `xlrd`(≥2.0), `python-docx` + `lxml`(M4), `anthropic`(어노테이터 전용 — M1·M2·M4-결정론부는 import조차 하지 않는 모듈 경계), `jsonschema`, 표준 `zipfile`/`xml`. 테스트 `pytest`. 패키징 `pyproject.toml`(uv 권장). 무거운 프레임워크 금지.

```text
Excel_to_SKILL/
├── src/excel_to_skill/
│   ├── cli.py            # §3
│   ├── loader.py         # §5 — 폴백 사다리 (형식 라우팅)
│   ├── extractor.py      # 스프레드시트 내부 IR
│   ├── docx_extractor.py # M4 — 문단·표 IR (python-docx + lxml)
│   ├── emit_cells.py / emit_blocks.py / emit_refs.py / emit_diag.py / emit_html.py / emit_skill_md.py
│   ├── refparse.py       # §4.5 수식 참조 파서
│   ├── annotator.py      # §7 (anthropic import는 여기서만)
│   └── cache.py          # §6
├── prompts/annotator_v1.md
├── schemas/{meta,references,diagnostics,semantics}.schema.json
├── tests/  (픽스처 xlsx 3종 + M4에서 docx 픽스처 추가)
├── DEVIATIONS.md
└── README.md             # 사용법, 지원 범위(스프레드시트+워드), 기본 모델, 패키지 계약 요약
```

---

## 12. DOCX 확장 사양 (M4, v1.1 신설)

### 12.1 원칙 — 봉투는 동일, 주소는 형식 고유

패키지 계약(§4.0 골격, semantics 스키마, 캐시, CLI, P1~P7)은 스프레드시트와 동일하다. 그러나 **셀 격자를 흉내내지 않는다**: 문단에 가짜 `A1` 좌표를 부여하는 것은 존재하지 않는 구조의 날조로 P3 위반이다. docx의 진실은 "본문 = 문단과 표의 문서 순서열"이며 주소도 그 진실을 따른다.

### 12.2 주소 체계 (결정론 규칙)

```text
p{n}              본문 직계 문단. n = 문단 전용 카운터(1-기반, 문서 순서, 빈 문단도 카운트 진행)
t{k}              본문 직계 표. k = 표 전용 카운터(1-기반, 문서 순서)
t{k}!r{r}c{c}     표 셀 (1-기반). 병합은 anchor 셀에 span으로 기록
t{k}!r{r}c{c}/t{m}!…   중첩 표는 경로형
```

결정론 근거: 캐시 키가 파일 sha이므로 "같은 파일 = 같은 주소". 원본이 바뀌면 주소가 흔들리는 게 아니라 새 패키지가 생긴다.

### 12.3 layout/body.html (단일 파일)

- 문단 → `<p data-anchor="p3">…</p>`. 스타일명이 `Heading N`이면 `<hN>` 기계 매핑, 그 외 스타일명은 `data-style` 속성으로 보존. 목록 문단은 `data-list-level`.
- 표 → `<table data-anchor="t1">`, `gridSpan`→`colspan`, `vMerge`→`rowspan` — **스프레드시트 렌더러의 스팬 점유 맵 코드를 재사용**한다. 병합 자식 칸 `<td>` 미생성. 각 셀 `data-anchor="t1!r5c2"`.
- 체크박스 글리프(□■☑ 등)는 텍스트 원문 그대로 보존(P3).
- **⑧ 값 계약 준수**: 속성명은 `data-anchor`, 값은 blocks.jsonl의 anchor와 문자 단위 일치. SKILL.md 사용법 절에 속성명 명시.
- 문단 상한 10,000·표 행 상한 `--max-rows` 준용, 절단 시 중략 마커 + diagnostics 기록.

### 12.4 data/blocks.jsonl

한 줄 = 한 블록. **포함 규칙(결정론):** 문단 — 텍스트가 비어 있으면 제외하되 카운터는 진행(주소 불변). 표 셀 — **빈 셀 포함**(표 셀은 태생이 테두리 칸 = 스프레드시트의 테두리 빈 셀과 동형인 입력 슬롯 보존; 비고란·서명란이 이 규칙으로 살아남는다). 병합 자식 셀은 제외(anchor에 span 기록).

```json
{"anchor":"p3","kind":"paragraph","style":"Heading 2","text":"…","list_level":null}
{"anchor":"t1!r5c2","kind":"table_cell","row":5,"col":2,"grid_span":1,"v_merge":null,"text":"□ 검토함 □ 검토하지 않음 (사유 기술)","checkbox_glyphs":2}
```

정렬은 문서 순서. 필드 순서 고정(P2).

### 12.5 data/references.json (docx)

- `observability.workbook = "not_applicable_docx"` — 수식이라는 개념 자체가 없음(P6 ③상태). `unavailable`(.xls — 있는데 못 봄)과 구별된다.
- 기계적 참조만 수록: 하이퍼링크(대상 URL/내부 앵커), 북마크와 REF 필드.
- **⑨ 확정 반영**: "K-IFRS 1115 문단 60~65" 류 본문 인용 문자열의 정규식 추출은 수록하지 않는다. 도메인 어휘는 범용 도구의 결정론 계층에 들어가지 않는다.

### 12.6 data/diagnostics.json (docx 항목)

`tracked_changes: {insertions, deletions}`(조서 특성상 잔존 시 중대), `comments_count`, `fields_count`, `content_controls_count`(sdt), `nested_tables_count`, `headers_footers_present`, `checkbox_glyph_total`, `truncations`, `loader_path`. rsid·스타일 블로트류 노이즈는 수록하지 않는다.

### 12.7 로더 (docx)

확장자 `.docx` 파일이 진입. python-docx로 문단·표·스타일을 읽고, python-docx가 노출하지 못하는 항목(변경추적 w:ins/w:del, 체크박스의 실제 유형 — 글리프/legacy FORMCHECKBOX/sdt, gridSpan/vMerge, 중첩 구조)은 lxml로 XML 직접 접근한다 — openpyxl+zipfile 패턴과 대칭. 실패 격리는 §5와 동일.

### 12.8 SKILL.md (docx판 규칙)

머리 텍스트 결정론 규칙: 문서 순서상 첫 비공백 문단의 원문(주소 병기). description draft 문법은 §4.2와 동일한 틀 — `"워드 문서 — 문단 {N}·표 {K}. 머리: {첫 문단 원문}. (의미 주석 미승인)"`.

### 12.9 M4-a: 실물 프로파일 (구현 착수 전 필수)

주의: 본 지시서의 docx 관련 수치(표 비중 등)는 **텍스트 변환 사본 기준 근사치**다(검증 환경에 원본 부재). M4-a에서 로컬 원본 14종에 대해 다음을 측정하고, 결과로 골든 수치와 §12.4~12.6 세부를 확정해 **부록 C**로 본 지시서에 추가 승인받는다:

표별 gridSpan/vMerge 수, 체크박스의 실제 유형 분포(글리프 문자 / legacy FORMCHECKBOX 필드 / sdt 콘텐츠 컨트롤), 변경추적·코멘트 잔존, 중첩 표, 머리글/바닥글, 본문 직계 문단·표 수, 필드·하이퍼링크 수.

### 12.10 골든 파일 3종 (3형상 대표)

`4000P-1`(표 중심·체크박스 극단 — 사본 기준 표비중 96%, 글리프 255), `8300A 서면진술서`(혼합 서한), `9720`(순수 산문 — 표 0%). 각각의 골든 수치는 M4-a가 확정한다.

---

## 부록 A. 소비 시나리오 (수용의 최종 감각)

Claude Code 에이전트에게 패키지 디렉터리만 주고 "이 조서의 목적과 작성해야 할 항목, 각 판단의 근거는?"이라고 물었을 때 — 에이전트가 SKILL.md로 진입해 그 패키지의 원장 파일명과 앵커 속성명을 파악하고, semantics의 주장을 evidence 주소로 검증하거나(승인본) 구조 데이터만으로 스스로 해석하며(draft/`--no-annotate`), `rg`로 원장을 검색하고 references로 전파를 추적해, **모든 답변 문장에 `시트!셀` 또는 `p{n}`/`t{k}!r{r}c{c}` 근거를 달 수 있는 상태** — 이것이 이 도구가 성공한 상태다.

## 부록 B. 승인 편차 기록

**D-01 (승인, 2026-07-08) — used range 재계산.** v1.0은 `meta.sheets[].dimensions`의 산출 기준을 명시하지 않았고, M1 로더 구현은 dimension 레코드 대신 콘텐츠 기반 재계산을 채택했다. 오너 측 재검증으로 레코드 과대 기록이 확인되어(감사계약 1200: 레코드 `A2:H43` vs 36행 이하 실콘텐츠 0건) 편차를 승인하고 §5에 규칙으로 명문화했다. 구현 리포 `DEVIATIONS.md`에 본 항목을 전기할 것.

*— 작업지시서 v1.1 끝. 문의·편차는 마일스톤 보고에 취합.*