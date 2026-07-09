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

## 개정 이력 (v1.1 → v1.2, M2 진행 중 명문화)

M2 표현 계층(layout HTML·SKILL.md draft·cli 배선) 구현에서 확정된 사항의 소급 명문화다. 코드·스키마·픽스처 스냅샷에 이미 반영됐다.

1. **§4.1 `conversion_params` 신설** — 결정론 출력을 좌우하는 변환 파라미터(`max_rows`)의 자기증언. 재현 '입력'을 (원본 + 파라미터)로 완결.
2. **§4.2 SKILL.md `name` 규칙 개정** — `{ascii_slug_or_untitled}-{sha12}`로 유일성 보장(한글 파일명 `untitled` 충돌 방지).
3. **§3·§4.3 `--max-rows` 실배선·범위 확정** — 절단은 layout HTML 한정, **원장(cells.jsonl) 불변**. 총 행 `> max_rows + 5`일 때만 첫 N + 말미 5행 + 중략 `<tr>`.
4. **§4.6 `truncations` 항목 형태 고정** — `{sheet, kept_head, kept_tail, total_rows, target}` + 스키마 `additionalProperties: false` 엄격화.
5. **§8.1 V3 재변환 규칙 보강** — 재변환은 `meta.conversion_params`를 읽어 수행(비기본 `--max-rows` 패키지도 참 통과).
6. **§3 convert 조립 순서 명문화** — meta → cells → references → layout(절단 계산) → diagnostics(truncations) → SKILL.md.

---

## 개정 이력 (v1.2 → v1.3, `--full-names` 배선 명문화)

정의된 이름 전량 덤프(`--full-names`) 배선에서 확정된 사항의 명문화다. 코드·스키마·픽스처 스냅샷(fx4)에 이미 반영됐다.

1. **§4.0 `defined_names_full.json` 형태 확정** — `{global_total, sheet_scoped_total, broken_ref_count, legacy_path_count, names:[{name, scope, value, flags}]}`. 카운트 4종은 diagnostics와 같은 값(단일 출처), `names`는 추출 순서 보존, `value`는 전문+이메일만 P7 마스킹, 스키마 엄격.
2. **§4.1 `conversion_params.full_names` 편입** — 필드가 `{max_rows, full_names}`로 확장. `--full-names`는 전량 덤프 파일의 **존재 자체**를 좌우하므로 재현 입력에 포함.
3. **§4.6 `full_dump_present` 연동** — 파일 존재와 이 플래그가 반드시 일치(어긋나면 verify 실패).
4. **§3 `--full-names` 실배선** — references 뒤(layout 앞)에 덤프 방출, meta·diagnostics에 플래그 전달, 미적용 고지 제거.
5. **§8.1 full_names 일관성 검사 + V3 보강** — 존재↔플래그 일치 검사(+조건부 스키마), V3 재변환이 `conversion_params.full_names`를 읽어 수행하고 덤프도 대조.
6. **§8.2 V9 fx4 픽스처 추가** — 정의된 이름(전역·시트·`#REF!`·레거시 경로·이메일) 합성 픽스처로 이원 집계·플래그·마스킹·전량 덤프·`full_dump_present` 연동을 스냅샷 고정.

---

## 개정 이력 (v1.3 → v1.4, M2 마감 검수 결함 2건 보정)

M3 착수 전 M2 검수에서 드러난 결함 2건을 닫은 명문화다. 코드·테스트에 이미 반영됐다.

1. **§6 추출 캐시 키에 `conversion_params` 편입** — 캐시 hit 조건이 `sha256 + converter_version + 패키지 실재`만이라, 같은 파일을 `--full-names`·`--max-rows`만 바꿔 재변환하면 옛 옵션 패키지가 stale hit로 그대로 반환되는 결함. `_index.json` 항목에 `conversion_params`(`max_rows`·`full_names`)를 기록하고, probe가 현재 옵션과 다르면 `params_changed` miss로 재생성한다.
2. **§8.1 verify에 M2 산출물 포함** — verify가 `SKILL.md`·`layout/*.html` 훼손을 잡지 못하던 결함. **V1 필수 파일**에 `SKILL.md`와 `layout/*.html`(디렉터리+1개 이상) 존재를 추가하고, **V3 재변환 대조**에 `SKILL.md`(고정 경로 바이트 비교 — 가변값 `converter_version`은 재변환이 같은 값을 써 일치)와 `layout/*.html`(파일 목록·내용)을 포함한다.

---

## 개정 이력 (v1.4 → v1.5, M3 1단계 — semantics 스키마·V2 착수)

M3 해석 계층의 1단계로, **어노테이터(LLM) 없이** semantics 계약과 그 실재성 검증만 먼저 못박은 명문화다. 코드·테스트에 이미 반영됐다(어노테이터·`annotate`/`review`·주석 캐시/승계는 후속 단계, 이 단계에서 `anthropic` import는 없다).

1. **§4.7 `semantics.schema.json` 작성** — 지금까지 미작성이던 해석 계층 스키마를 draft-07·`additionalProperties:false` 엄격 스키마로 확정. 최상위는 `generator`·`review`만 필수로 두고(docx 변형 `body_claims`는 M4-a 후 확장 여지), `workbook_claims[]`·`sheets[]`·`sections[]`의 `evidence`는 `minItems:1`로 **P4(근거 강제)를 스키마 수준에서 못박는다**. `fields[].label_cell`·`value_cell`은 `string|null`(null=빈 슬롯 허용, 문자열이면 유효 주소여야 함).
2. **§8.1 V2 evidence 실재성 구현** — `semantics.json`이 있으면 모든 주소 주장이 (a) 형식 유효 (b) 실존 (c) `meta.sheets[].dimensions`(§5 D-01 재계산 used range) 범위 내인지 검증한다. 주소 문법은 **형식별 파서 플러그인**(`get_address_plugin`)으로 분리 — **스프레드시트만 구현**(`시트!셀`·`시트!범위` 절대 / `fields`는 소속 시트 기준 상대 `A1`), docx는 자리만 열고 `NotImplementedError`→생략. 검증 대상은 `evidence[]` 전부 + `fields[].label_cell`/`value_cell`(문자열일 때) + **`sheets[].name`(meta 실존 시트) + `sections[].range`(소속 시트 기준 상대 셀/범위로 used range 내)**. approve 전 필수 통과이며, semantics가 없으면 검사 자체를 건너뛴다(V1 skipped 관례 종료).
3. **V2 크래시 방어(선행 검사 게이팅)** — V1(`semantics`·`meta`) 스키마가 실패하면(예: `sheets`가 배열이 아님) V2는 구조를 신뢰할 수 없어 크래시 위험이 있으므로 **skipped로 보고**한다(선행 실패가 이미 verify 실패). `collect_evidence_problems`도 리스트·dict 아님을 방어(이중 방어). §8.1 defect-3(files 실패 시 V3 생략)과 같은 계열 — **선행 검사가 실패하면 그에 의존하는 후속 검사는 돌리지 않는다**는 원칙을 verify 전반에 적용.

---

## 개정 이력 (v1.5 → v1.6, M3 2단계 — 어노테이터·`annotate`)

M3 해석 계층의 2단계로, **실제 LLM 호출 경로**를 처음 도입해 `semantics.json`(status=`draft`)을 생성한다. `review`(승인·승인판 SKILL.md 재생성)·주석 캐시/승계·32종 시드는 후속 단계다.

1. **§7 어노테이터 구현** — 새 `src/excel_to_skill/annotator.py`가 **`anthropic`을 import하는 유일한 모듈**이며, 그것도 `build_anthropic_client()` 안에서만 **지연 import**한다(P1 물리적 경계 + optional extra). 클라이언트는 `(*, system, user) -> str` 콜러블로 추상화해 **주입 가능** — 테스트·미리보기는 스텁으로 anthropic 미설치·무네트워크에서 돈다. 입력 = 패키지의 `layout/*.html`+`cells.jsonl`+`references`(요약), 호출 = **시트 단위 → 워크북 단위** 순(구현 재량). `temperature=0`, 응답 JSON을 `semantics.schema.json` 하위 스키마로 검증하고 **이어서 V2 실재성(§8.1 `collect_evidence_problems`)까지 그 단위에 대해 검증**한다(스키마만 통과하고 `Data!ZZ999` 같은 used range 밖 주소를 반환하는 경우를 여기서 잡는다 — draft라도 evidence는 실재해야 함). **스키마·실재성 중 하나라도 실패면 오류 첨부 1회 재시도, 재실패면 그 단위 제외 + stderr**(진단 파일엔 LLM 실패 미기록). 최종 조립 직후 전체 semantics에 대해 스키마 + V2 sanity를 한 번 더 건다(잔존 시 경고). 산출 `generator`(model·`annotator_version`·`prompt_sha`·temperature·generated_at) + `review`=draft. **핵심 계약: annotate의 산출물은 verify V2를 항상 통과한다**(불량 evidence는 제외되어 남지 않음).
2. **프롬프트·모델 상수** — `prompts/annotator_v1.md` 단일 파일, `prompt_sha`=그 파일 sha256(P4·개방형 어휘·"관찰한 것만, 불확실하면 confidence↓"·§0.2 명문화). 기본 모델명은 **코드 상수 1곳(`DEFAULT_MODEL="claude-sonnet-5"`) + README**에만, `--model`로 교체.
3. **§3 `annotate` 서브커맨드** — `annotate <패키지> [--model]`로 **별도 명령**(convert와 분리). cli가 annotator를 지연 import해 convert/verify 경로의 anthropic 무접촉을 보장. stdout=산출 `semantics.json` 경로, 제외 단위는 stderr, exit=제외 있으면 비영. 무키·비패키지는 크래시 아닌 실패(exit 1). `review`만 스텁(exit 2, M3 3단계).

---

## 개정 이력 (v1.6 → v1.7, M3 3단계 — `review`·승인판 SKILL.md)

M3 해석 계층의 3단계로, 승인/반려와 **승인판 SKILL.md 재생성**을 구현한다. `review`는 LLM을 쓰지 않는 **결정론** 명령이다(anthropic 무관). 주석 캐시/승계·32종 시드는 후속.

1. **§7 `review` 서브커맨드** — `review <패키지> (--approve | --reject --note "사유")`. `--approve`는 **승인 전 `verify` 전체 통과가 전제**(오너 확정 — V2만이 아니라 V1·필수파일·full_names 일관성까지; 깨진 패키지는 승인 불가), 통과 시 `review={status:approved, reviewed_at, note:null}`로 갱신하고 승인판 SKILL.md 재생성. `--reject`는 `--note` 필수, `{rejected, reviewed_at, note}`로 갱신하고 SKILL.md를 **미승인 형태로 재생성**(approve→reject 시에도 ⑥가 승인 의미를 노출하지 않음 — 상태와 SKILL 항상 일치).
2. **§4.2 승인판 SKILL.md — IR 없이 패키지 파일에서 재구성** — 내부 IR은 디스크에 없고(§9) review는 원본도 없으므로, `build_skill_md`를 **IR 비의존 코어(`_render_skill_md`)로 리팩터**하고 `build_skill_md_from_package(pkg)`를 신설한다. 머리 텍스트는 `cells.jsonl`에서 (row,col) 사전식 최소 비공백 텍스트 셀로 재계산(IR과 동일 규칙), layout 파일명은 `data-sheet` 마커로 매핑. ①~⑤(구조 사실)는 계층 무관 동일하고, `description`(2단)과 ⑥ 해석만 `review.status`로 갈린다 — approved면 `description`=`workbook_claims[0].claim`, ⑥에 claim·purpose·section·field를 **각 evidence 주소·confidence와 함께** 렌더(P4). (semantics 없는 패키지는 이 함수가 convert-time draft와 바이트 동일 — 회귀 테스트로 고정.)
3. **§8.1 SKILL 자기일관성 검사(V3 대체)** — 승인판 SKILL.md는 해석 계층에서 파생돼 fresh convert(항상 draft)와 바이트가 다르다. 그래서 SKILL.md를 **V3(fresh convert 대조)에서 빼고**, 대신 **`SKILL` 자기일관성 검사**를 둔다(원본 불요·항상 수행): 현재 SKILL.md가 `build_skill_md_from_package(pkg)`(현재 meta·references·diagnostics·cells·layout·semantics에서 재생성)와 바이트 일치하는지 본다. 불일치면 훼손/구버전으로 실패. 이로써 승인판이든 draft든 SKILL 훼손을 원본 없이도 잡는다(초기 검수에서 "semantics 있으면 훼손 SKILL이 통과"하던 공백 보정). 필수 파일 누락 시엔 재생성이 크래시하므로 생략(선행 게이팅).
4. **meta.annotation 운영(모순 제거)** — 종전엔 annotate/review가 `semantics.json`만 갱신하고 `meta.annotation`은 `present:false`로 남아 상태가 모순됐다. 이제 `meta.set_annotation`으로 annotate→`{present:true, annotator_version, review_status:"draft"}`, review→`review_status`(approved/rejected)를 반영한다. `annotation`은 **해석 계층 상태(비결정론)**이므로 **V3 meta 비교에서 제외**(`_meta_norm`이 `generated_at`과 함께 `annotation`도 정규화). 이 필드는 M3 4단계 주석 캐시/승계가 읽는 provenance다.

---

## 개정 이력 (v1.7 → v1.8, M3 4a단계 — 주석 캐시 + meta↔semantics 일관성 검사)

M3 4단계의 첫 하위 단계로, §6 주석 캐시를 배선하고 검수에서 남은 일관성 공백을 닫는다. 승계 규칙(4b)·32종 시드(4c)는 후속.

1. **§6 주석 캐시** — `annotation_key = sha256(file_sha + annotator_version + model + prompt_sha)`(4성분 해시, `cache.annotation_key`). `annotate <패키지>`가 `meta.source.sha256`으로 키를 만들고, `_index.json` 항목의 `annotation_key`가 같고 `semantics.json`이 있으면 **재주석 생략(LLM·클라이언트 미생성)** — `--force`로 무시. 재주석하면 `cache.update_annotation`으로 항목의 `annotation_key`+`review_status="draft"` 기록. review는 `review_status`(approved/rejected)를 `_index.json`·`meta.annotation`·`semantics.review` 세 곳에 함께 반영(단일 진실 유지). 모델·프롬프트·어노테이터·파일이 바뀌면 키가 달라져 miss→재주석.
2. **meta↔semantics 일관성 검사(`annotation`)** — verify에 검사 추가(원본 불요·항상 수행): `semantics.json`이 있으면 `meta.annotation.present==true` + `review_status==semantics.review.status` + `annotator_version==semantics.generator.annotator_version`, 없으면 `present==false`. 어긋나면 실패. `meta.annotation`을 운영 필드로 쓰기로 한 이상, 캐시/승계가 읽기 전에 두(세) 출처의 일치를 verify가 강제한다(검수 지적 공백 보정 — approved인데 `present:false`로 바꾸면 이제 실패).

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

**P2. 결정론 보장.** 동일 입력 파일 + 동일 `converter_version` → 결정론 계층은 재실행 시 동일 출력. 타임스탬프류 가변 값은 `meta.json`에만 존재한다. 모든 `.json`은 **고정 필드 순서**(각 스키마에 선언된 순서대로 삽입, `sort_keys` 미사용) + `indent=2, ensure_ascii=False, allow_nan=False`, 원장 jsonl은 고정 필드 순서 + 고정 정렬(스프레드시트: 시트 순 → row → col / docx: 문서 순서)로 직렬화한다. 파이썬 3.7+는 dict 삽입 순서를 보존하므로 고정 순서로도 재현성이 보장되며, 알파벳 정렬보다 스키마의 논리적 순서를 유지해 가독성이 낫다(구현 관례: emit_refs·emit_diag·meta 공통).

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
- `--max-rows N`: **layout HTML 표 행 상한**(스프레드시트 시트 행 / docx 표 행). 기본 5,000. 초과 시 첫 N행 + 말미 5행 렌더 + `diagnostics.truncations` 기록. **원장(cells.jsonl)은 자르지 않는다**(v1.2 — §4.3). docx 문단은 별도 안전핀 10,000개(초과 시 동일 규칙). 이 값은 `meta.conversion_params.max_rows`에 기록되어 V3 재변환이 그대로 재현한다(§8.1).
- `annotate` / `review` / `verify`: v1.0과 동일. `verify`는 §8의 V1~V3, 실패 시 비영 exit code.
- 기본 출력: `./converted/`, 색인 `converted/_index.json`.

**convert 조립 계약 (v1.1 명문화 — cli 1차 구현 확정):**
- **stdout/stderr 분리**: stdout에는 **패키지 경로만** 출력한다(단일 파일=마지막 한 줄, `--all`=성공한 파일마다 한 줄). 진행 로그·캐시 hit/miss 사유·경고·오류는 **전부 stderr**로 보낸다.
- **generated_at 단일 계산**: 한 번의 convert에서 시각을 1회만 계산해 `meta.json`과 `_index.json` 항목에 **같은 값**을 넣는다.
- **원자적 생성**: 임시 폴더(`.staging_*`)에 모든 산출물을 쓴 뒤 성공하면 최종 폴더로 교체(rename)하고, **그 다음에야** `_index.json`을 upsert한다. 도중 실패 시 임시 폴더만 제거하고 색인은 손대지 않는다 — 반쪽 폴더가 캐시 hit로 잡히지 않는다. 재생성 시 기존 최종 폴더는 클린 슬레이트로 교체하되, **삭제 대상은 반드시 출력 루트 내부로 한정**(root 바깥 경로 삭제는 거부).
- **캐시 hit**: 어떤 파일도 다시 쓰지 않고 기존 패키지 경로만 stdout에 출력한다.
- **`--all`**: 디렉터리 **최상위**의 `*.xlsx`·`*.xls`만 정렬 순회한다(재귀 없음, Excel 임시잠금 `~$*` 제외). 한 파일이 실패해도 나머지는 계속 처리하고, **하나라도 실패하면 최종 exit code는 비영**.
- **M1 1차 범위(갈래 1)**: convert가 조립하는 결정론 산출물은 `meta.json` + `data/{cells.jsonl, references.json, diagnostics.json}`까지다. `SKILL.md`·`layout/*.html`은 방출기(§4.2·§4.3)가 M2에서 생기면 cli에 끼워 넣는다. 그 전까지 `--full-names`(§4.0 defined_names_full)와 `--max-rows` truncation은 **미배선**이며, 지정 시 조용히 무시하지 않고 stderr로 "미적용"을 고지한다. `--force-annotate`·`--model`은 어노테이터(M3)가 붙기 전까지 무의미하므로 동일하게 고지한다. `annotate`/`review`/`verify`는 서브커맨드만 등록한 스텁(**exit 2**)이며 `verify`는 별도 단계에서 구현한다.
- **M2 배선 완료(v1.2 명문화)**: 위 "미배선" 항목 중 `SKILL.md`·`layout/*.html`·`--max-rows`가 M2에서 배선됐다. convert 조립 순서는 **meta → cells → references → layout(절단 계산) → diagnostics(truncations 반영) → SKILL.md**다 — layout을 먼저 써 절단 기록을 받고 그것을 diagnostics에 넘긴 뒤, 그 diagnostics로 SKILL.md를 짓는다. `--max-rows`는 실동작하며(기본 5,000) 미적용 고지는 제거됐다. `--force-annotate`·`--model`은 M3 전까지 고지 유지.
- **`--full-names` 배선 완료(v1.3 명문화)**: 지정 시 `data/defined_names_full.json`(§4.0)을 추가로 방출하고 `diagnostics.defined_names.full_dump_present`를 `true`로 맞춘다. 조립 순서상 references 뒤(layout 앞)에 끼우며, meta·diagnostics에 `full_names` 플래그를 함께 넘긴다. 미적용 고지는 제거됐다. 켜지 않은 패키지는 이 파일이 **없어야 정상**이고 `full_dump_present`는 `false`다 — 파일 존재와 이 플래그가 어긋나면 verify가 실패시킨다(§8.1).

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

`defined_names_full.json`(v1.3 명문화 — `--full-names` 전량 덤프): `diagnostics.defined_names`가 샘플 ≤20으로 요약하는 것과 달리 정의된 이름 **전건**을 담는다(감사계약 파일은 전역 1,363 + 시트 594 = 1,957개). 형태는 `{ "global_total", "sheet_scoped_total", "broken_ref_count", "legacy_path_count", "names": [ { "name", "scope", "value", "flags" } ] }`. 카운트 4종은 `diagnostics.defined_names`와 **같은 값**이어야 한다(같은 규칙으로 도출 — 단일 출처). `names`는 **추출 순서 그대로**(원본 이름표 순서 보존, 감사 추적). `scope`는 전역이면 `null`, 아니면 시트명. `value`는 **전문**을 담되 이메일만 P7 마스킹한다(`#REF!`·레거시 경로는 마스킹 대상이 아니므로 원문) — "full"은 마스킹 해제가 아니라 "전건 + 값 전문"이라는 뜻이다. `name`은 원문 유지. `flags`는 `broken_ref`·`legacy_path`. 스키마(`defined_names_full.schema.json`)가 `additionalProperties: false`로 엄격 검증한다.

### 4.1 meta.json

가변 값(타임스탬프)이 허용되는 유일한 결정론 계층 파일.

```json
{
  "tool": "excel_to_skill",
  "converter_version": "0.1.0",
  "source": { "filename": "…", "sha256": "…64자…", "size_bytes": 0, "format": "xlsx|xls|docx" },
  "loader_path": "openpyxl_normal | openpyxl_read_only | openpyxl_read_only+xml_merge | xlrd | python_docx",
  "conversion_params": { "max_rows": 5000 },
  "sheets": [ { "name": "1100", "dimensions": "A1:F13", "max_row": 13, "max_col": 6 } ],
  "generated_at": "ISO8601",
  "annotation": { "present": false, "annotator_version": null, "review_status": null }
}
```

`sheets[].dimensions`는 dimension 레코드가 아니라 **§5의 재계산 used range**다(D-01). docx는 `sheets` 대신 `body: {"paragraphs": N, "tables": K}`를 기록한다.

`conversion_params`(v1.2 명문화 — M2 확정)는 **결정론 출력을 좌우하는 변환 파라미터의 자기증언**이다. 같은 원본이라도 `--max-rows`가 다르면 `layout/*.html`과 `diagnostics.truncations`가, `--full-names`가 다르면 `data/defined_names_full.json`의 **존재 자체**가 달라지므로, 재현의 '입력'은 (원본 파일 + 변환 파라미터)다. 이 블록이 그 파라미터를 패키지 안에 남겨 V3 재변환이 외부 지식 없이 재현할 수 있게 한다(§8.1). `converter_version`·`generated_at`과 같은 성격의 재현 조건 증언이며, meta.json이 §4.1에서 이미 가변 값 예외 계층이므로 결정론 원칙 위반이 아니다. 필드는 `{ "max_rows": N, "full_names": bool }`이다(v1.3 명문화 — `full_names` 편입). 후속 출력 변경 옵션도 같은 방식으로 이 객체에 필드를 더한다.

### 4.2 SKILL.md

**frontmatter** (Claude Agent Skills 규격): `name` = `{ascii_slug_or_untitled}-{sha256 앞 12자}`(v1.2 명문화 — M2 확정). ascii_slug는 원본 stem을 소문자화해 ASCII 영숫자만 하이픈으로 이어 붙인 것이며(한글 등 비ASCII는 하이픈 처리, 남는 게 없으면 `untitled`), 여기에 sha256 앞 12자를 접미해 **유일성을 보장**한다 — 한글 파일명이 다수인 코퍼스에서 `untitled` 충돌을 막기 위함이다. 원문 정보는 `description`이 보존한다. `description` = 2단 생성 —
- *draft*: 구조 사실만. `"스프레드시트 {N}매 — {각 시트 머리 텍스트 원문을 ' / '로 나열}. (의미 주석 미승인)"`. **머리 텍스트 결정론 규칙**: 시트 used range에서 (row, col) 사전식 최소 위치의 비공백 텍스트 셀 원문. docx판은 §12.8.
- *approved*: `semantics.workbook_claims` 최상위 claim 문장으로 재생성.

**본문 구성(순서 고정):** ① 원본 메타 요약(파일명·sha 12자·converter_version·구성 요약·loader_path) ② 구성 목록(시트/본문 개요 — 머리 텍스트 원문과 주소 병기) ③ 참조 관계 요약(엣지 수·대표 3건, docx는 P6 상태 명기) ④ 진단 요약 ⑤ **리소스 사용법 — 이 패키지의 원장 파일명과 앵커 속성명(⑧)을 여기서 명시**("이 패키지는 `data-cell`을 씁니다" / "`data-anchor`를 씁니다") ⑥ **[해석]** — `review.status == approved`일 때만 렌더, 미승인 시 "의미 주석 없음(또는 미승인) — 구조 데이터로 직접 해석하십시오" 한 줄.

### 4.3 layout/{시트}.html (스프레드시트)

- `<table data-sheet="1200">` 단일 테이블. 병합은 `colspan`/`rowspan`, 병합 자식 칸의 `<td>`는 생성하지 않는다(스팬 점유 맵).
- 모든 `<td>`에 `data-cell="B4"` 각인. **공통 값 계약(⑧)**: 레이아웃 HTML의 모든 원장 대응 요소는 자기 주소를 data-속성 값으로 각인하며, 그 값은 원장의 주소 문자열과 **문자 단위로 일치**한다. 속성명은 형식 고유(xlsx `data-cell`, docx `data-anchor`)이며 각 패키지 SKILL.md 사용법 절에 명시된다.
- 수식 셀: `data-formula` 속성에 원문. 표시 텍스트는 계산값이 있으면 값, 없으면 `[수식: ='1100'!B4]`.
- 스타일 최소주의: 굵게 `class="b"`, 테두리 `class="bd"`, 기본값 아닌 배경색만 `style="background:#RRGGBB"`. 그 외 서식은 버린다.
- `--max-rows` 적용, 절단 시 `<tr>` 중략 마커. **절단은 layout HTML에만 적용하며 원장(cells.jsonl)은 자르지 않는다**(v1.2 명문화 — M2 확정). 원장은 근거 추적의 최종 데이터라 항상 full extraction을 유지하고, 총 행이 `max_rows + 5`를 넘을 때만(중간이 실제로 생략될 때) 첫 `max_rows`행 + 말미 5행을 렌더하고 중간은 생략 `<tr>`(생략 행수 사실만 표기)로 표시한다. 절단 사실은 `diagnostics.truncations`에 기록한다(§4.6). 절단이 병합 범위를 걸치면 anchor의 `rowspan`을 실제 렌더되는 행 수로 clamp해 HTML이 깨지지 않게 한다.

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
  "truncations": [ { "sheet": "1200", "kept_head": 5000, "kept_tail": 5, "total_rows": 8123, "target": "layout" } ],
  "format_limitations": null
}
```

권고 문장("확인 필요" 등) 금지 — 사실만. docx 항목은 §12.6.

`truncations` 항목 형태(v1.2 명문화 — M2 확정): 각 원소는 `{ "sheet": 시트명, "kept_head": 상한 N, "kept_tail": 5, "total_rows": 원래 총 행수, "target": "layout" }`. layout HTML이 절단된 시트마다 한 원소가 생기며, 절단이 없으면 빈 배열이다. `target`은 지금은 `"layout"` 하나뿐이다(원장은 절단하지 않으므로). 스키마(`diagnostics.schema.json`)는 이 형태를 `additionalProperties: false`로 엄격 검증한다.

`full_dump_present`(v1.3 명문화 — `--full-names` 배선): `--full-names`로 전량 덤프(`data/defined_names_full.json`, §4.0)를 방출했으면 `true`, 아니면 `false`다. **파일 존재와 이 플래그는 반드시 일치**하며, 어긋나면(한쪽만) verify가 실패시킨다(§8.1). `samples`(상한 20)는 요약이고, 전건은 전량 덤프에만 있다.

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
| 추출 캐시(결정론 계층) | `sha256(file) + converter_version + conversion_params` | 파일 변경, 변환기 버전 업, 또는 옵션(`max_rows`·`full_names`) 변경 |
| 주석 캐시(해석 계층) | `sha256(file) + annotator_version + model + prompt_sha` | 파일·어노테이터·모델·프롬프트 변경 |

- `converted/_index.json` 항목: `{원본명, sha256, 패키지경로, converter_version, conversion_params, annotation_key, review_status, 최종생성시각}`. `conversion_params`(`max_rows`·`full_names`)는 캐시 키의 일부이며(v1.4 — 옵션이 산출을 좌우), probe가 현재 옵션과 대조해 다르면 `params_changed` miss로 재생성한다.
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

- **V1 스키마 검증**: `schemas/*.schema.json`으로 meta/references/diagnostics/semantics 검증. `--full-names` 패키지의 `defined_names_full.json`은 있을 때만 조건부로 검증(§8.1 full_names 일관성 검사).
- **V2 evidence 실재성**: semantics의 모든 evidence가 (a) 형식 유효 (b) 실존 대상 (c) 범위 내인지 검증. 주소 문법은 **형식별 파서 플러그인**으로 — 스프레드시트 `시트!셀|범위`(재계산 used range 기준), docx `p{n} | t{k}!r{r}c{c}(/중첩)`(실존 인덱스 기준). **approve 전 필수 통과.**
- **V3 재현성**: 동일 입력 2회 변환 → 결정론 계층 동일(meta.json은 `generated_at` 제외 정규화 비교). 재현의 '입력'은 (원본 파일 + 변환 파라미터)이므로, 재변환은 CLI 기본값이 아니라 **패키지의 `meta.conversion_params`를 읽어 그 값으로 수행한다**(v1.2 — M2 확정). 이로써 `--max-rows`를 비기본값으로 만든 패키지도 truncations가 일치해 V3가 참으로 통과한다. `--full-names`도 같은 성질이라 `conversion_params.full_names`를 읽어 재변환하며, 전량 덤프가 있으면 그것도 결정론 대조 대상에 포함한다(v1.3).
- **full_names 일관성 검사(v1.3 — `--full-names` 배선)**: `data/defined_names_full.json`의 **존재**와 `diagnostics.defined_names.full_dump_present`가 일치하는지 본다. 어긋나면(파일은 없는데 플래그 true, 또는 그 반대) verify 실패. 파일이 있으면 `defined_names_full.schema.json`으로 스키마까지 검증한다. 이 파일은 `--full-names` 시에만 존재하므로 V1 필수 파일 목록에는 넣지 않는다(조건부).

**verify 구현 기준 (v1.1 명문화 — M1 확정):**
- **스키마 3종은 실제 방출 결과에 맞춘 엄격 스키마**(`additionalProperties: false`, JSON Schema draft-07): `schemas/{meta,references,diagnostics}.schema.json`. 특히 xlsx/xls 차이를 반영한다 — xls는 `external_links.count=null`(관찰 불가≠0), `references.observability.workbook="unavailable_xls"` + `note` 문자열, `diagnostics.format_limitations` 문자열, `hidden.sheets`에 숨김 시트가 존재하는 케이스가 모두 통과해야 한다. `semantics.schema.json`은 M3 1단계에서 작성됐다(v1.5) — 있을 때만 조건부 검증.
- **M1 verify 범위**: V1(위 3종 스키마) + **필수 파일 존재**(meta.json·data/cells.jsonl·references.json·diagnostics.json + **M2 산출물 SKILL.md·layout/\*.html**[v1.4 — layout은 디렉터리+html 1개 이상]) + **cells.jsonl 각 줄 JSON 파싱 sanity**(jsonl은 스키마 대상 아님) + V3(아래). `semantics.json`이 있으면 `semantics.schema.json`으로 V1 검증하고 **V2(실재성)까지 수행**한다(v1.5 — M3 1단계). 없으면 V1:semantics·V2 검사 자체를 건너뛴다.
- **V3의 원본 처리**: 패키지에는 원본 바이트가 없으므로 재현성 비교는 `verify <패키지> --source <원본>`으로 원본을 줄 때만 수행한다(임시 폴더로 재변환 후 결정론 계층 대조, meta는 `generated_at` 제외). **재변환의 `--max-rows`·`--full-names`는 패키지의 `meta.conversion_params`(`max_rows`·`full_names`)를 읽어 쓴다**(v1.2 max_rows·v1.3 full_names — 없으면 각각 기본 5,000·false로 방어). 대조 대상은 결정론 3종(cells.jsonl·references.json·diagnostics.json) + **M2 산출물 SKILL.md(고정 경로 바이트 비교)·layout/\*.html(파일 목록·내용)**(v1.4)이며, 패키지에 `defined_names_full.json`이 있으면(=full_names) 그것도 포함한다. **단 SKILL.md는 V3 대조에서 빼고 별도 `SKILL` 자기일관성 검사로 담보한다**(v1.7 — SKILL.md는 승인 시 해석 계층에서 파생되므로 fresh convert와 다름; `build_skill_md_from_package` 재생성 결과와 바이트 일치하는지 원본 없이 검증해 훼손을 잡는다). meta 비교는 `generated_at`·`annotation`(해석 계층 상태)을 정규화 제외한다. **`--source`가 없다는 이유만으로 실패시키지 않는다** — V1이 통과하면 verify 통과이되 리포트에 `V3 skipped(원본 필요)`를 명시한다. `--source`가 주어졌는데 재현성 비교가 실패하면(또는 원본 sha가 패키지와 불일치하면) verify 실패.
- **판정은 exit code가 권위**: 통과 0 / 실패 비영. 검사 리포트는 stdout, 실패 사유는 stderr. `annotate`는 M3 2단계에서 구현됐고(v1.6), `review`는 M3 3단계까지 스텁(exit 2).

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