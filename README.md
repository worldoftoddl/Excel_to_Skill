# excel-to-skill

회계감사조서 Excel(`.xlsx`·`.xls`)을 빠르게 이해하고 질의할 수 있도록 workbook 사실,
감사·회계기준 문맥, agent-ready brief로 준비하고 근거 기반 브리핑·질의응답까지 수행하는
CLI 도구다. 일반 spreadsheet 구조 조회도 기존 명령으로 유지한다.

셀 주소는 제품의 최종 목적이 아니라 **설명과 결론을 원문으로 되짚는 provenance 계층**이다.
산출 패키지는 workbook-only 사실, 별도 RAG 기준서 문맥, 둘을 구분 인용하는 brief를
분리해 에이전트가 무엇이 조서에 적힌 내용이고 무엇이 외부 권위 문맥인지 혼동하지 않게 한다.

## 설치

```bash
uv sync                    # 결정론 계층(convert·verify)
uv sync --extra annotate   # 해석 계층(annotate)까지 — anthropic 포함
uv sync --extra prepare    # 감사조서 prepare — anthropic + FastMCP HTTP client
uv sync --extra graph      # LangGraph 영속 audit-chat + LangChain Anthropic
uv sync --extra inspection # 범위 profile·중복·이상치 분석용 pandas
uv sync --extra web --extra graph  # FastAPI/Uvicorn 웹 service + audit-chat runtime
uv sync --extra graph --extra prepare  # audit-chat 동적 기준서 research·research-first planning까지
```

## 명령

```bash
# 결정론 변환(해석 계층 없이) — 패키지 폴더 생성
excel-to-skill convert <파일.xlsx> [--out ./converted] [--max-rows 5000] [--full-names]
excel-to-skill convert <디렉터리> --all

# 패키지 계약 검증 — V1 스키마 / V2 evidence 실재성 / V3 재현성(--source)
excel-to-skill verify <패키지> [--source <원본>]

# 해석 계층(semantics.json draft) 생성 — 기존 패키지에 주석 추가
excel-to-skill annotate <패키지> [--model <모델명>] [--force]
excel-to-skill annotate <converted_root> --all   # 전 패키지 일괄 주석(집계·실패 격리)

# 해석 계층 검토 — 승인(승인판 SKILL.md 재생성) / 반려(사유 필수)
excel-to-skill review <패키지> --approve
excel-to-skill review <패키지> --reject --note "<반려 사유>"

# LLM 호출 전 전체·시트별 region 수와 예상 호출량 확인(외부 호출 없음)
excel-to-skill audit-scopes <패키지>

# 감사조서 준비 — 기본은 workbook 전체를 한 번에 분석
excel-to-skill prepare <패키지> [--scope workbook] [--mcp-config .mcp.json] [--force]

# 필요한 시트만 각각 독립 분석하거나, 내용이 있는 전 시트를 각각 분석
excel-to-skill prepare <패키지> --sheet C --sheet P
excel-to-skill prepare <패키지> --all-sheets

# 감사 준비본 사람 검토 — facts와 brief를 함께 승인/반려
excel-to-skill audit-review <패키지> --approve
excel-to-skill audit-review <패키지> --reject --note "<반려 사유>"
excel-to-skill audit-review <패키지> --sheet C --approve

# commit된 시트 brief만으로 계정별 종합 브리핑(ledger·standards_context 직접 재전송 없음)
excel-to-skill audit-aggregate <패키지> --sheet C --sheet D [--json]
excel-to-skill audit-aggregate <패키지> --all-committed-sheets [--json]
excel-to-skill audit-aggregate <패키지> --all-committed-sheets --plan

# 소비(Agent) — 원본 JSON 통째 로드 금지, 개요→시트→셀 단계 조회
excel-to-skill overview <패키지> [--sheet <시트>]         # 셀 원문 없이 구조·상태 요약
excel-to-skill inspect  <패키지> --sheet <시트> [--range A1:B10 | --cell A1]
excel-to-skill search   <패키지> --query <문자열> [--sheet <시트>]
excel-to-skill refs     <패키지> --cell <시트!A1>

# 감사조서 준비본 소비(audit-rag-v0)
excel-to-skill brief       <패키지> [--sheet C] [--limit 100]
excel-to-skill audit-search <패키지> --query <문자열> [--kind <종류>] [--sheet C]
excel-to-skill audit-get    <패키지> --id <fact/statement/citation ID> [--sheet C]
excel-to-skill assertion-procedures <패키지> [--query <문자열>] [--sheet C]
excel-to-skill trace        <패키지> --id <ID> [--sheet C] [--limit 100]

# 브리핑 에이전트 — 질문 생략 시 전체 브리핑, --json이면 구조화 근거 포함
excel-to-skill audit-agent <패키지> [--sheet C] [--question <질문>] [--json]

# 영속 대화 — 첫 호출이 출력한 thread ID로 후속 질문 재개
excel-to-skill audit-chat <패키지> --question "핵심 위험은?" [--sheet C] [--json]
excel-to-skill audit-chat <패키지> --thread <thread-id> --question "그 절차의 결과는?"

# committed 문맥이 부족할 때만 MCP 기반 동적 기준서 조사 허용
excel-to-skill audit-chat <패키지> --question "외부조회 관련 감사기준은?" --standards-research [--mcp-config .mcp.json]

# 관찰된 하나의 위험·주장에 대해 3~5개 미검토 감사 test 후보와 조합안 생성 허용
excel-to-skill audit-chat <패키지> --question "이 매출채권 실재성 위험에 어떤 test가 가능한가?" --procedure-planning

# 관련 기준 근거가 committed 문맥에 부족하면 같은 turn의 동적 조사부터 허용
excel-to-skill audit-chat <패키지> --question "이 위험에 가능한 test들을 비교해줘" --standards-research --procedure-planning [--mcp-config .mcp.json]

# 전체 workbook을 다시 보내지 않고 한 시트·한 범위의 결정론 검사를 필요할 때만 허용
excel-to-skill audit-chat <패키지> --question "C시트 J열의 반복 주장과 수식 참조를 확인해줘" --workbook-inspection

# 게시된 다중 시트 aggregate를 대화 root로 사용(--sheet와 동시 사용 불가)
excel-to-skill audit-chat <패키지> --aggregate-id <selection-sha256> --question "계정별 핵심 위험은?"
excel-to-skill audit-chat <패키지> --aggregate-id <selection-sha256> --thread <thread-id> --question "매출채권 근거를 추적해줘"
```

소비 명령은 결정론(키 불요)이며 결과 JSON을 stdout으로 낸다. 출력은 하드 상한(`--limit`)과
`returned`·`total`·`truncated`로 예산이 걸리고, `overview`는 승인된 해석만 노출한다.
`assertion-procedures`의 상한은 상위 목록뿐 아니라 각 pair의 관계·결과·trace 목록에도
적용되며, 각 목록의 `returned_*`·`total_*`·`*_truncated` 필드로 잘린 범위를 밝힌다.
`audit-search`는 의미 임베딩 검색이 아니라 대소문자를 무시한 텍스트 부분일치 검색이다.

`convert`의 stdout은 패키지 경로만, `annotate`의 stdout은 산출 `semantics.json` 경로만,
`review`의 stdout은 재생성된 `SKILL.md` 경로만 출력한다(진행·경고·오류는 stderr, 판정은
exit code). `annotate --force`는 주석 캐시를 무시하고 재주석한다.

## 감사조서 준비 계층(audit-rag-v0)

기존 `semantics.json`은 M3 비교 기준선으로 유지한다. 새 감사조서 경로는 서로 다른
출처가 섞이지 않도록 세 산출물로 분리한다.

- `data/audit_facts.json`: workbook에 실제로 문서화된 감사 사실과 실제 셀 원장 digest
- `data/standards_context.json`: 사실에서 도출한 query로 조회한 감사·회계기준 문맥
- `data/audit_brief.json`: workbook fact ID와 기준서 citation ID를 분리 인용하는 준비본

### 큰 workbook의 분석 범위

`convert`는 선택과 무관하게 먼저 workbook 전체를 기계적으로 파싱한다. 따라서 시트별
LLM 분석을 선택해도 `cells.jsonl`, 수식 참조, 진단정보와 시트 목록을 물리적으로 쪼개거나
잃지 않는다. 선택은 그 다음 의미 분석 단계에만 적용된다.

- 무옵션 `prepare` 또는 `--scope workbook`: 기존처럼 모든 시트를 하나의 audit bundle로 분석
- 반복 `--sheet`: 지정한 각 시트를 별도 bundle로 분석. 시트 간 fact ID namespace도 분리
- `--all-sheets`: ledger cell이 있는 모든 시트를 각각 분석. 한 시트 실패가 이미 커밋된 다른
  시트 결과를 되돌리지 않음
- `audit-scopes`: 시트별 dimensions·cell·region·직접 수식 dependency·예상 LLM 호출량과
  준비/검토 상태를 외부 호출 없이 표시

시트 scope의 모델 입력에는 선택한 시트 region만 들어간다. 수식 자체에 다른 시트 참조가
있으면 그 참조 표현은 보이지만, 참조 대상 시트의 셀 내용까지 자동으로 합치지는 않는다.
필요하면 dependency 정보를 보고 그 시트를 별도 `--sheet`로 준비한다.

기존 workbook bundle은 고정 `data/audit_*.json`과 `meta.audit_preparation`을 유지한다. 시트
bundle은 원 시트명을 경로에 쓰지 않고
`data/audit_scopes/sheets/<sheet-name-sha256>/` 아래에 저장하며, 세 artifact 게시 뒤
`commit.json`을 마지막에 기록한다. 커밋이 없거나 digest가 맞지 않는 scope는 모든 reader와
agent가 거부하고 workbook 결과로 fallback하지 않는다. 시트 prepare/review는 root
`meta.json`·`SKILL.md`와 다른 시트 scope를 변경하지 않는다. 시트별 조회·검토·agent에는
항상 같은 `--sheet`를 지정한다.

### 계정별 종합 aggregator

`audit-aggregate`는 workbook 전체를 다시 분석하지 않는다. 먼저 선택된 모든 시트의
`commit.json`과 세 audit artifact를 현재 workbook/cells digest까지 검증한 뒤, 각 시트의
workpaper 제목·검토/준비 상태·개수 요약과 bounded brief 후보만 compact dossier로 만든다.
모델 입력에는 `cells.jsonl`, source cell/value/formula, 전체 `audit_facts`,
`standards_context` 원문 필드가 직접 들어가지 않는다. 단, 이미 생성된 brief 문장 자체가
셀 값이나 기준서 원문을 요약·인용한 경우 그 문장은 후보 텍스트로 포함될 수 있으므로
prepared artifact도 민감자료로 취급한다.

시트별 로컬 `statement:*` ID가 서로 같을 수 있으므로 모델에는
`(sheet scope id, record kind, local id)`를 해시한 opaque `record:<sha256>`만 보낸다. 모델은
관찰한 record를 선택·정렬할 뿐 새 브리핑 문장을 쓰지 않으며, 코드는 선택 결과를 각
commit된 brief에서 다시 materialize한다. 모델 호출 전·후와 게시 잠금 안에서 source
commit identity를 반복 검증한다. `rejected` 또는 `not_ready` 시트가 하나라도 선택되면
모델 호출 전에 중단하고, `draft`·`partial`은 시트별 상태와 aggregate limitation에 그대로
남긴다. aggregate 자체는 입력이 모두 승인됐어도 새 `draft`다.

선택은 `--sheet` 반복 또는 `--all-committed-sheets` 중 하나를 반드시 명시한다. 수식
dependency는 다른 시트 내용의 관찰·실질 근거를 뜻하지 않으므로 자동 포함하지 않는다.
`--plan`은 외부 호출 없이 선택 범위, 600KB model payload 상한, 예상 호출 수와 cache 상태를
JSON으로 보여준다. 실제 결과는 선택 집합별
`data/audit_aggregates/<selection-sha256>/account_brief.json`에 저장되고 같은 폴더의
`commit.json`이 마지막에 게시된다. 같은 model·prompt·source commit이면 재실행은 모델을
호출하지 않고 cache를 사용한다.

compact dossier는 시트당 highlight/attention 후보 상한을 둔다. source brief record가 이
상한을 넘으면 `candidate_source_record_count`·`candidate_record_count`·
`omitted_candidate_record_count`를 함께 기록하고, `candidate_selection_complete=false`,
aggregate readiness `partial`, `candidate_truncated` limitation으로 숨김없이 표시한다.
high-severity attention 후보는 모델 선택과 무관하게 코드가 계정 섹션에 보존한다.

source scope나 commit 집합이 바뀌면 기존 aggregate consumer는 stale 결과를 거부한다.
`--all-committed-sheets`는 한 개의 안정된 selection 경로를 갱신하며, 명시 선택으로 남은
과거 조합은 `verify`에서 손상으로 오인하지 않고 `stale cache`로 보고·무시한다. 같은 선택을
다시 실행하면 현재 source manifest로 원자 갱신된다.

게시된 aggregate는 `audit-chat --aggregate-id <selection-sha256>`의 대화 root로 사용할 수
있다. ID는 위 산출 경로의 디렉터리명이며 `audit-aggregate --plan` JSON에서도 먼저 확인할 수
있다. `audit-chat`은 aggregate를 암묵적으로 만들거나 갱신하지 않고, 정확한 `account_brief.json`
과 `commit.json`, 선택된 모든 source sheet commit을 다시 검증한다. 따라서 같은 안정 ID의
`--all-committed-sheets` aggregate가 재게시됐거나 source scope가 바뀌면 기존 thread를 새
자료에 자동으로 맞추지 않고 새 thread를 요구한다.

현재 v1의 `accounts[]`는 **시트 scope 1개를 계정 섹션 1개로 roll-up**한다. 첫 `account`
fact 설명이 있으면 label로 쓰고, 없으면 workpaper title과 시트명 순으로 대체한다. 같은
계정이 여러 시트에 나뉜 경우를 하나의 account로 자동 병합하거나, 한 시트의 여러 account를
분할하는 단계는 아직 아니다.

`audit-aggregate`도 외부 모델 호출이며, `.env`에 LangSmith 키가 활성화돼 있으면 compact
dossier의 brief 문장이 trace로 복제될 수 있다. 민감 실행에서 추적을 끄려면
`LANGCHAIN_API_KEY= LANGSMITH_API_KEY= excel-to-skill audit-aggregate ...`처럼 두 변수를
빈 값으로 덮어쓴다.

조서에 기준서 번호가 명시되면 query의 `standard_nos`로 구조화해 MCP filter에 그대로
전달한다. 현재 citation 유형은 감사기준·회계기준만 구분하므로 `GUIDE` 실무지침은 잘못된
유형으로 기록하지 않고 명시적으로 조회 제한으로 남긴다.

추출은 시트 첫 N개 셀을 자르는 대신 모든 `cells.jsonl` 레코드를 결정론적 region으로
나눠 처리한다. workbook 주소는 used range 포함 여부만 보지 않고 실제 원장 레코드에
연결되는지와 내용 SHA-256까지 검증한다. brief의 draft 내용은 숨기지 않으며
`unreviewed: true`로 명시해 탐색에 사용하고, `trace`로 원문과 기준서 문맥을 확인한다.
가변 brief 본문은 `SKILL.md`에 복제하지 않는다. SKILL은 안정적인 bootstrap만 제공하고,
에이전트는 commit marker와 artifact digest를 검증하는 `excel-to-skill brief`로 내용을
불러온다.

`assertion-procedures`는 텍스트 유사도로 대응 관계를 추정하지 않는다. 준비본에
`procedure --tests--> assertion` 방향으로 명시된 관계만 주장·절차 쌍으로 반환하고,
그 절차에서 `produces`로 직접 연결된 result/finding만 함께 보여준다. 관계가 없는 주장과
절차는 각각 미연결 목록으로 남겨 누락을 숨기지 않는다. 산출물에 `inferred`로 기록된
관계도 숨기지 않되 pair의 `mapping_status`와 상태별 개수로 `documented` 관계와 구분한다.
relation trace는 relation 레코드가 직접 인용한 `relation_direct_*` source/cell과 양 endpoint
fact의 `endpoint_*` source/cell을 분리한다. 두 범위를 합친 `sources`·`cells`도 호환용으로
남지만, 관계 자체의 직접 근거를 볼 때는 `relation_direct_*`를 사용한다.

현재 brief 계약은 `audit_brief.v2`다. v1 준비본은 최신 계약으로 오인하지 않고 소비 단계에서
거부되며, `prepare`를 다시 실행하면 검증된 facts·standards cache를 재사용해 brief만 v2로
갱신할 수 있다.

### 브리핑 에이전트

`audit-agent`는 준비된 세 artifact에 새 사실을 쓰지 않는 일회성 read-only 에이전트다.
항상 commit marker와 artifact digest 검증을 먼저 통과한 뒤 `brief`와
`assertion-procedures`를 읽고, 필요할 때만 `audit-search`·`audit-get`·`trace`를 제한적으로
호출한다. 실행 시에는 `ANTHROPIC_API_KEY`가 필요하지만 이미 저장된 기준서 citation만
사용하므로 MCP를 다시 호출하지 않는다.

모델은 최종 문장을 직접 쓰지 않고 관찰된 `statement`·`fact`·`relation`·
`standard_citation` ID만 선택한다. 코드가 선택된 원본 record의 문장·상태·신뢰도를 그대로
가져오고, workbook 셀과 검증 기준서 CID를 `trace`로 보강한다. 자유 텍스트나 셀 값에 우연히
등장한 ID는 선택 권한으로 승격하지 않는다. ID 기반 `audit-get`·`trace`도 앞선 typed
결과에서 관찰된 ID만 허용한다. facts와 brief의 검토상태, 새 답변의 검토상태를 각각
분리하며, 둘 중 하나가 rejected이면 모델을 호출하지 않는다. 사람 검토 후 `audit-review`로
facts와 brief를 함께 승인/반려할 수 있고, 새 답변은 원본 bundle이 approved여도 항상
`unreviewed`다.

도구 결과가 잘리거나 trace가 불완전하면 `coverage.complete=false`와 limitation을 표시한다.
각 turn의 직렬화된 관찰 payload는 600KB로 제한되고 동일 도구 요청은 반복할 수 없다.
`rejected` 또는
`not_ready` 입력은 외부 모델을 호출하지 않고 결정론적으로 답변을 보류한다. `--json` 출력은
`audit_agent_response.v2` 스키마로 재검증되며, ID namespace를 고정하는 workbook 또는
sheet scope identity가 필수로 포함된다.

기준서가 선택된 claim은 collection이 붙은 검증 CID와 최대 400자의 원문 발췌를 함께
표시한다. brief 문장이 `KSA`/`KIFRS` 번호를 직접 쓸 때는 같은 문장의 citation이 그
기준서에 속해야 한다. 그렇지 않으면 문장을 그럴듯하게 고치지 않고 통째로 제외하며,
제외한 statement ID를 readiness 사유에 남긴다.

`audit-agent`는 brief statement, fact 설명, relation과 기준서 요약을 모델 제공자에게
전송한다. 모델이 `trace`를 선택하면 해당 범위의 원문 셀 값·수식도 다음 turn에 포함될 수
있으며, `--json` 출력에도 선택된 raw cell이 담긴다. LangSmith 키가 활성화되어 있으면 같은
호출이 외부 trace에 기록될 수 있다. 민감 조서는 승인된 처리환경에서만 실행하고, 추적을
끄려면 `LANGCHAIN_API_KEY= LANGSMITH_API_KEY= excel-to-skill audit-agent ...`처럼 두 변수를
빈 값으로 명시해 `.env` 자동 로드를 덮어쓴다.

### 영속 대화 graph

`audit-chat`은 기존 `audit-agent`의 commit gate, typed ID 권한, 600KB 관찰 상한, 중복 도구
차단, 최종 셀·CID hydration을 그대로 재사용하는 별도 LangGraph workflow다. 컴파일된 graph는
`bind_turn → bootstrap → decide → (execute_tool ↔ decide | execute_research ↔ decide |
execute_plan ↔ decide | execute_inspection ↔ decide | finalize) → commit_turn`으로
조건 분기한다. `--thread`를 생략하면 새 ID를 출력하고, 같은 ID를 다음 호출에 주면 프로세스가
종료된 뒤에도 SQLite checkpoint에서 대화를 재개한다.
같은 thread의 동시 turn은 hash-only lock으로 직렬화하고, 서로 다른 thread는 독립적으로
진행한다.

대화 thread는 최초 turn의 정확한 workbook/sheet bundle 또는 게시된 aggregate context에
묶인다. 단일 bundle은 facts·standards·brief key를, aggregate mode는 aggregate ID뿐 아니라
artifact key·commit/input digest·source manifest와 각 source sheet binding까지 고정한다.
그 뒤 `prepare`·`audit-review`·`audit-aggregate` 재게시로 어느 입력이든 바뀌면 자동으로 새
자료에 말을 맞추지 않고 모델 호출 전에 실패한다. 새 snapshot에는 새 thread를 시작해야 한다.
rejected·not_ready source도 모델 client 생성 전에 결정론적으로 보류하거나 거부한다.

aggregate mode에서 `account_brief.json`은 대화의 compact root이자 routing index일 뿐, 원
workbook·기준서 근거를 대체하지 않는다. bootstrap은 portfolio/account의 materialized
`record:<sha256>`만 노출하고, 필요한 경우 `aggregate_get`이 그 record와 같은 exact source
sheet의 scope-qualified `source:<sha256>`를 연다. 이후 `source_search`·
`assertion_procedures`·`source_get`·`trace`는 그 시트의 committed facts·standards·brief만
조회한다. 서로 다른 시트에 같은 로컬 `statement:*` ID가 있어도 opaque ref가 scope를 포함해
구분하며, 최종 셀·CID 근거도 선택 record가 속한 정확한 source sheet에서 다시 hydrate한다.
limitation·readiness record에는 존재하지 않는 셀 근거를 만들어 붙이지 않는다.

aggregate가 `partial`이거나 일부 commit 시트만 선택했거나, candidate가 잘렸거나, 아직
prepare되지 않은 시트가 있으면 그 상태를 main-agent의 trust·coverage·notice에 그대로
전파한다. 이때 선택된 account record를 workbook 전체의 완전한 결론처럼 표현하지 않으며,
최종 coverage는 discovery와 exact-source trace가 모두 완전할 때만 완료가 된다. aggregate와
새 답변은 source가 모두 승인됐더라도 각각 `draft`·`unreviewed`다.

checkpoint에는 질문·답변·셀·기준서 본문이나 모델 객체를 넣지 않는다. bundle identity,
단계·turn 번호, typed ID와 content-addressed reference만 저장한다. 실제 질문, bounded tool
observation, 모델의 ID 선택안, hydrated 답변은 패키지의 ignored private 경로
`.audit_runtime/conversations/`에 canonical JSON으로 저장되고 digest·thread ownership을 매번
검증한다. 디렉터리와 파일은 가능한 플랫폼에서 각각 `0700`·`0600`으로 만든다. 이 경로는
prepared bundle의 일부도, `verify`의 감사 근거도 아니며 원문을 포함할 수 있으므로 민감자료로
취급한다.

후속 turn에는 최근 3개 turn의 질문·검증 답변을 bounded focus로 제공한다. 과거 답변이 실제로
선택했던 ID를 현재 bundle에서 다시 typed record로 조회해 모델에 명시적으로 재노출한 경우에만
이번 turn의 `audit-get`·`trace`와 최종 선택 권한이 생긴다. 과거 질문·답변 자유문장이나 셀 안에
ID처럼 보이는 문자열은 권한이 아니다. 최대 100 turn 뒤에는 새 thread가 필요하다.

production 모델 경계는 `ChatAnthropic.with_structured_output(...)`이고 graph extra를 지연
import하므로 core 명령과 기존 `audit-agent`에는 새 의존성이 강제되지 않는다. 각 모델 요청의
provider-reported input/output/total token 수를 request event로 보존하고 turn 합계를 출력한다.
stub처럼 usage metadata를 주지 않는 client는 0으로 표시한다. 기본 `audit-chat`은 committed
기준서 citation만 사용하고 MCP를 호출하지 않는다. `--standards-research`를 명시하면 capability만
열리며, main-agent가 committed 문맥만으로 권위 기준 질문에 답하기 어렵다고 판단해
`standards_research`를 선택한 때에만 MCP 연결과 격리된 child graph를 지연 실행한다. 한 turn에
research request는 최대 1회, 검색 후보는 최대 5건, child 선택은 최대 3건이며 child 모델 호출도
`--max-steps`와 usage 합계에 포함된다.

애플리케이션은 현재 bundle의 collection에 검색을 고정한다. aggregate mode에서는 bootstrap에
노출된 정확한 source `scope_id`도 요구한다. child는 research query와 typed 후보만 보고 opaque
candidate ref를 선택하며, 애플리케이션이 선택 CID를 `standards_get_paragraph(context=0)`로
다시 조회해 검색 원문·metadata·collection을 대조한다. 결과는 현재 turn의 별도
`ephemeral`·`unreviewed` 보조 문맥이고 prepared bundle이나 조서 수행 사실로 승격되지 않으며,
시행일 적합성도 자동 검증하지 않는다. child graph는 부모 checkpointer를 상속하지 않고,
`research_ref`는 다음 turn의 focus나 ID 권한으로 승계되지 않는다.

#### 감사 test 후보 계획

`--procedure-planning`은 기본적으로 닫혀 있는 작성형 capability다. 이를 명시하고 main-agent가
현재 질문을 하나의 관찰된 위험과 하나의 경영진 주장에 고정할 수 있을 때에만 격리된
`procedure_planning.py` child graph가 실행된다. 결과는 정답 하나가 아니라 서로 다른 3~5개
후보다. 정확히 하나의 `primary`, 하나 이상의 `alternative`, 하나 이상의 `complementary`를
포함하고, 각 후보에 적용 조건·배제 조건, 확보할 증거, 수행 단계, 장점, 한계, 선행조건과
미해결 질문을 붙인다. 둘 이상의 후보를 묶은 권장 조합과 trade-off도 별도로 제시한다.

모든 후보와 조합은 `proposed / unreviewed / not_evidenced`이며 비완전 목록이다. 이는 조서에
문서화되거나 수행된 절차, 필수 절차 또는 승인된 감사계획이 아니다. 표본 수·금액 기준·선정
간격은 근거 없이 생성하지 않고 `TBD`로 유지한다. 계획 결과는 `procedure_plan` 보조 응답으로만
나오며 `audit_facts`, `standards_context`, `audit_brief`, aggregate 또는 기존
`tests`·`addresses`·`produces` 관계에 합쳐지지 않는다.

계획 근거는 workbook 근거와 standards 근거를 별도 ref 목록으로 보존한다. 기준서 근거는
prepared bundle에서 관찰한 검증 citation이거나 같은 turn의 `ephemeral` research 결과여야 한다.
적절한 기준 근거가 아직 보이지 않을 때 `--standards-research`와 `--procedure-planning`을 함께
열면 main-agent가 research를 먼저 수행한 뒤 그 결과를 계획에 사용할 수 있다. 그래도 안전한
근거가 없으면 자유 지식으로 채우지 않고 `no_plan`으로 끝난다. 동적 research를 실제로 선택한
경우에만 MCP URL·token이 필요하며 planning 자체는 MCP에 직접 연결하지 않는다.

단일 workbook/sheet 대화에서는 현재 turn에 typed 결과로 관찰한 fact·relation·citation ID만
계획 근거가 된다. aggregate 대화에서는 `source:<sha256>` ref가 모두 하나의 exact source
`scope_id`에 속해야 하며 여러 sheet를 섞을 수 없다. 같은 turn의 `research_ref`도 그 scope와
collection에 정확히 일치해야 한다. planning child는 부모 checkpointer를 상속하지 않고,
checkpoint에는 실행 상태와 content-addressed private-object ref만 남는다. raw 후보·기준서 본문과
선택 `plan_ref`의 원본은 `.audit_runtime/conversations/`에 invocation·thread 범위로 보관되며,
plan은 다음 turn의 focus나 새 ID 권한으로 승계되지 않는다.

5개 후보의 상세 구조화 응답은 일반 질의보다 출력이 크므로 CLI는 `--procedure-planning`을
켠 turn에 한해 Anthropic 출력 상한을 16,384 tokens로 예약한다(일반 `audit-chat`은 8,192).
이는 실제 사용량을 미리 소비한다는 뜻이 아니며, 반환 JSON의 `usage.requests`와 집계 필드에서
research·planning child를 포함해 provider가 보고한 실제 token 사용량을 확인할 수 있다.

#### Workbook 추가 검사

`--workbook-inspection`은 기본적으로 닫힌 read-only capability다. 모델이 committed brief와
bounded reader만으로 답하기 어렵다고 판단한 경우에 한해, 전체 원장이나 원본 파일을 모델에
다시 보내지 않고 정확히 한 시트·한 A1 범위를 결정론적으로 검사한다. 한 turn의 검사 시도는
성공·실패를 합해 최대 2회이며 aggregate 대화에서는 두 요청 모두 하나의 exact source sheet에 고정된다.

지원 operation은 다음과 같다.

- `inspect_range`: package `cells.jsonl`에서 최대 200개 셀을 다시 읽음
- `inspect_formula_dependencies`: `references.json`에서 선행·후행 참조를 최대 100건 조회
- `profile_table`: pandas로 열별 null·distinct·numeric·최소·최대·평균을 계산
- `find_duplicates`: 선택 열 조합의 중복 그룹을 최대 50건 계산
- `find_outliers`: 한 숫자 열의 IQR 1.5 방식 이상치를 최대 50건 계산

기본 source는 commit gate를 통과한 package ledger다. 원본 재조회는 같은 sheet/range의 ledger
검사가 먼저 성공했고, 호스트가 `BundleSnapshot.workbook_source_provider`에 opaque asset reader를
연결했으며, 그 bytes가 `meta.source.sha256`과 일치할 때만 가능하다. CLI는 경로를 agent tool에
노출하지 않으므로 현재 `--workbook-inspection`만 켠 경우 ledger 분석이 기본이고 raw source는
연결되지 않는다. raw XLSX는 압축 archive 상한과 XML 안전 parser를 거친 뒤 read-only로 열며,
요청 범위에 수식이 있을 때만 cached-value view를 두 번째로 연다.

결과는 `inspection:<sha256>` ref와 source/input/result digest를 가진
`computed / unreviewed / not_documented / turn_scoped` 보조 응답이다. 조서 fact, 수행된 감사절차,
감사결론이 아니며 prepared bundle·aggregate·다음 turn의 ID 권한으로 승격되지 않는다.

### 웹 service/API adapter

`audit.service`는 웹 요청이 로컬 경로를 직접 지정하지 못하도록 opaque `bundle_id`를
서버 소유 `BundleSnapshot`으로 해석한다. public thread ID는 tenant·subject별 내부 runtime ID로
변환되어 SQLite checkpoint와 private object store가 사용자 사이에서 섞이지 않는다. 같은
`Idempotency-Key`는 repository가 실행 전에 원자적으로 claim하며, 완료 전 재요청은
`TURN_IN_PROGRESS`, 다른 command 재사용은 `IDEMPOTENCY_CONFLICT`로 닫힌다.

`audit.web.create_fastapi_app(...)`은 호스트가 인증된 `ServicePrincipal` resolver와 위 service를
주입할 때 다음 동기식 turn API를 만든다.

```text
POST /v1/audit/conversation-turns
GET  /v1/audit/conversation-turns/{request_id}
```

POST body는 `bundle_id`, `question`, 선택적인 `thread_id`·`sheet`·`aggregate_id`와 세 opt-in
capability만 받으며 package path, model, runtime root, provider는 거부한다. 모든 POST에는 정확히
하나의 `Idempotency-Key`가 필요하다. FastAPI는 bounded 전용 executor에서 동기 graph turn을
수행하고, Pydantic/JSON 오류도 고정 400 오류 봉투로 반환한다.

내장 `InMemoryConversationArtifactRepository`와 `InMemoryTurnLock`은 테스트·단일 프로세스
prototype용이다. 다중 worker 운영은 `ConversationArtifactRepository`의 atomic
`claim/publish/abort`, principal-scoped receipt/thread binding과 `TurnLock`을 DB·분산 lock으로
구현해야 한다. runtime 시작 뒤 실패한 claim은 중복 turn을 막기 위해 자동 재실행하지 않고
pending으로 남으므로 운영 repository에는 별도 reconciliation 정책도 필요하다.

### 웹 제품 기본 경로

웹 UI는 OneDrive나 SharePoint를 전제로 하지 않는다. 채택한 기본 제품 흐름은 다음과 같다.

```text
XLSX 웹 업로드 → server-owned S3/MinIO-style object storage
→ prepare·sheet/account aggregate → bundle-bound 브리핑·대화
→ 편집안 제안·사용자 승인 → 수정된 XLSX 복사본 제공
```

이 경로에서는 업로드 원본과 수정본을 서버 snapshot으로 관리하면 된다. Microsoft 365 사용자가
현재 열어 둔 Excel Web/공동편집 파일에 변경을 직접 반영하려는 경우에만 아래 Office.js와
OneDrive/SharePoint provider 연동을 선택 기능으로 사용한다.

### Raw workbook upload API

`audit.workbook_asset_service`와 durable `audit.workbook_asset_sqlite` catalog는 웹 업로드를
prepared audit bundle과 분리한다. 최초 업로드는 principal-scoped `workbook_id`와 immutable
`raw_snapshot_id`만 만들며, 후속 convert/prepare commit이 끝나기 전에는 `bundle_id`를 발급하지
않는다. 실제 XLSX bytes는 content-addressed store에 있고 public 응답에는 object ref나 서버 경로가
포함되지 않는다.

`audit.workbook_asset_web.create_workbook_asset_fastapi_app(...)`은 다음 경계를 제공한다.

```text
POST /v1/audit/workbooks
GET  /v1/audit/workbooks/{workbook_id}/raw-snapshots/{raw_snapshot_id}
GET  /v1/audit/workbooks/{workbook_id}/raw-snapshots/{raw_snapshot_id}/download
```

POST는 multipart가 아닌 XLSX raw body와 정확히 하나의 `Idempotency-Key`를 받는다. 인증과
Content-Type/Content-Length 검사를 body보다 먼저 수행하고, 유한한 수신 deadline 안에서 최대
64MiB를 private spool로 읽은 뒤
ZIP framing, 압축 상한, CRC, 표준 XLSX content type, OOXML relationship을 검증한다. 정상 bytes만
immutable store에 기록하며 workbook, snapshot, current head, completed replay receipt는 한 SQLite
transaction에서 게시된다. 동일 command는 같은 snapshot을 재생하고 같은 key의 다른 workbook은
충돌한다. 검증된 immutable object가 catalog commit보다 먼저 만들어져 충돌·장애 뒤 orphan 후보가
남을 수 있지만 workbook/head로 노출되지는 않는다. Status/download와 completed replay는 exact
principal scope와 catalog row를 다시 확인한다. Download와 completed replay는 immutable object의
digest readback까지 다시 확인한다.

이 API는 raw source 보관 경계일 뿐 convert, prepare, aggregate 또는 LLM을 실행하지 않는다. 다음
processing slice가 raw snapshot을 deterministic loader에 연결하고 scope 선택 결과를 받은 뒤,
commit gate를 통과한 package만 기존 conversation service의 `BundleSnapshot`으로 게시한다.

### 승인형 workbook 편집 계약

대화 API가 workbook을 직접 수정하지 않도록 편집 경계도 별도 모듈로 분리한다.
`audit.workbook_edit`은 Excel을 열지 않고 content-addressed 문서를 만들고 검증하며,
`audit.workbook_edit_service`가 다음 상태 전이를 관리한다.

```text
proposed → previewed → approved → claimed → apply_started
                                            ├─ session_verified → snapshot published
                                            ├─ verification_failed
                                            └─ indeterminate
                    claimed ├─ stale_precondition
                            └─ aborted_before_apply
```

V1 proposal은 한 sheet의 single cell 최대 100개만 대상으로 하며 `set_value`, `set_formula`,
`set_number_format`, `clear_contents`를 지원한다. Office executor가 대상 셀의 authored value/formula,
계산값·타입, number format, merged/spill/protected/table 상태를 먼저 읽어 보내면 backend가 exact
before/after/diff를 만든다. 같은 sheet의 정적 A1 참조와 허용 함수만 쓰는 bounded formula만
허용하고, 거대한 참조 범위나 승인 범위 밖으로 spill할 수 있는 formula는 거부한다.

승인은 exact preview SHA-256과 만료시각에 묶이고 한 번만 소비된다. 실행 claim은 stable한 live
workbook 단위 monotonic fence와 challenge를 반환한다. Add-in은 쓰기 직전 before를 다시 읽어
다르면 아무것도 쓰지 않고 `stale_precondition`을 보고하고, 일치할 때만 write-start를 기록한 뒤
immutable manifest를 적용한다. 적용 뒤 재계산과 재조회 결과가 expected authored state에 맞아야
`session_verified`가 된다. write-start 뒤에는 짧은 server-issued execution deadline 안에서 결과를
보고해야 한다. 결과가 불명확한 `indeterminate` 또는 기대 상태와 다른 `verification_failed`는 같은
workbook의 후속 실행을 호스트가 조정할 때까지 격리한다.

`audit.workbook_edit_web.create_workbook_edit_fastapi_app(...)`은 다음 endpoint를 제공한다.

```text
POST /v1/audit/workbook-edit-workflows
POST /v1/audit/workbook-edit-workflows/{workflow}/previews
POST /v1/audit/workbook-edit-workflows/{workflow}/previews/{preview}/approve
POST /v1/audit/workbook-edit-workflows/{workflow}/previews/{preview}/reject
POST /v1/audit/workbook-edit-workflows/{workflow}/executions/claim
POST /v1/audit/workbook-edit-workflows/{workflow}/executions/{execution}/started
POST /v1/audit/workbook-edit-workflows/{workflow}/executions/{execution}/verify
POST /v1/audit/workbook-edit-workflows/{workflow}/executions/{execution}/abort
GET  /v1/audit/workbook-edit-workflows/{workflow}
GET  /v1/audit/workbook-edit-host-sessions/{host_session}/bootstrap
POST /v1/audit/workbook-edit-workflows/{workflow}/executions/{execution}/snapshot-publication
GET  /v1/audit/workbook-edit-workflows/{workflow}/executions/{execution}/snapshot-publication
```

모든 mutation은 정확히 하나의 `Idempotency-Key`가 필요하다. 경로·provider·JavaScript·credential은
요청 계약에 없고, host가 등록한 Office session만 bundle/snapshot/workbook/revision/worksheet에
연결된다. 운영 host는 coauthor session 사이에서 같고 독립 복사본 사이에서는 다른
`workbook_instance_id`를 등록해야 한다. session/host-session registry는 아직 in-memory reference다.
`SQLiteWorkbookEditRepository`는 한 공유 DB 안에서 workflow, bounded command/publication claim,
workbook fence/lease, source-head CAS와 재시작 replay를 보존한다. 여러 서버·스토리지 노드에 걸친
배포는 운영 DB/distributed lock과 `verification_failed`/`indeterminate`, 만료 claim·orphan asset
조정이 별도로 필요하다.

여기서 `session_verified`는 host가 인증한 executor의 bounded readback이 승인한 authored state와
일치한다는 뜻이다. backend가 Excel 계산 엔진을 독립 실행했다는 뜻이 아니며, dependent cell 전체나
conditional formatting 표시까지 보증하지 않는다. `persistence_policy=required`에서는 이 상태가
terminal이 아니다. workbook-level lock을 유지한 채 서버가 저장된 XLSX를 다시 취득하고 승인 manifest의
authored cell/formula와 number format을 재확인한 뒤 immutable asset 저장과 source-head CAS를 완료해야
한다. 이 publication도 새 raw-workbook snapshot일 뿐 prepared audit bundle 생성은 별도 단계다.

실제 실행기는 [`office-addin/`](office-addin/)에 있다. ExcelApi 1.13을 확인한 뒤 exact preview와
승인을 거쳐 claim/fence/challenge를 받고, write-start 왕복 뒤 셀을 다시 읽어 immutable manifest만
적용한다. formula edit은 대상 worksheet를 재계산하고, after-state witness가 backend artifact 상한을
넘으면 `actual_after`를 버린 `indeterminate` witness로 축약해 terminal quarantine를 요청한다.
개발 확인은 다음과 같이 수행한다.

```bash
cd office-addin
npm ci
npm test
npm run build
npm run validate:manifest
npm run dev
```

아래 내용은 선택적인 Microsoft 365 직접 편집 경로다. 현재 manifest와 자유 입력 API URL은
localhost 개발 harness용이다. production task pane은 같은
HTTPS origin이 주입한 `hostSessionId` 하나만 받고, bounded bootstrap 응답을 exact workflow와 대조한
뒤 모든 요청에 host-session header를 붙인다. `required` policy에서는 save 후 server-owned reacquirer,
content-addressed asset store, repository source-head CAS를 거치며 POST 응답이 유실되면 execution별 GET으로
publication만 복구한다. 저장소에는 local immutable asset store와 SQLite reference repository가 있지만,
실제 identity provider, durable host-session/session registry, OneDrive/SharePoint reacquirer, 운영 object
storage, 다중 worker 조정과 새 prepared bundle publication은 선택적 host 제품 통합 범위다. 자세한
실행 및 sideload 경계는 [`office-addin/README.md`](office-addin/README.md)를 따른다.

prepare의 Python 오케스트레이션 진입점은
`excel_to_skill.audit.prepare.prepare_package(...)`이다. 모델 client와
`StandardsRetriever`를 주입하므로 단위 테스트는 무네트워크로 실행된다. CLI는
`auditpaper-standards`의 Streamable HTTP MCP를 사용한다. `.mcp.json` 형식은 다음과 같다.

```json
{
  "mcpServers": {
    "auditpaper-standards": {
      "type": "http",
      "url": "https://<서버>/mcp",
      "headers": {"Authorization": "Bearer ${MCP_AUTH_TOKEN}"}
    }
  }
}
```

토큰은 현재 작업 디렉터리의 ignored `.env`에 `MCP_AUTH_TOKEN=...`으로 두거나 이미
export된 환경변수로 주입하고 저장소에 커밋하지 않는다. `prepare`와 `annotate`는 `.env`를
자동 로드하고 `audit-agent`·`audit-chat`도 모델 키를 같은 방식으로 읽되 기존 export 값을
덮어쓰지 않는다. URL은
`--mcp-url` 또는 `AUDITPAPER_MCP_URL`로도 줄 수 있다. URL/config를 생략하면 원격 MCP
안내서의 고정 HF Space(`https://toddl-auditpaper-mcp.hf.space/mcp`)를 사용한다.
prepare와 opt-in `audit-chat --standards-research`는 같은 MCP 연결 규칙을 사용한다. 동적
research의 URL·토큰은 main-agent가 실제 도구를 선택할 때 지연 해석한다. prepare는 서버의
`collection`을 cache 판본으로 고정하고, 검색으로 채택한 모든 CID를
`standards_get_paragraph` 직조회 또는 같은 `collection + cid`의 검증 cache로 확정한다.
서버가 구조화 시행일을 제공하지 않으므로 적용일 적합성은
자동 단정하지 않고 brief limitation으로 남긴다. 검증한 전문은
`<패키지 상위>/.auditpaper_standards_cache/`에 `collection + cid` 기준으로 원문 그대로
원자 저장하며, collection이 바뀌면 자동으로 다른 cache namespace를 사용한다.
이미 게시된 facts와 standards context가 현재 모델·조회정책·artifact key에 맞고 brief만
갱신하면 되는 경우, `prepare`는 MCP 연결이나 토큰 없이 두 상류 단계를 검증·재사용한다.
조회정책·collection·상류 artifact가 달라졌거나 `--force`를 사용하면 정상적으로 MCP
연결을 요구한다.

## 어노테이터(해석 계층)

- **모델**: 기본값은 **`claude-sonnet-4-5`**. `annotate --model <이름>`으로 교체한다.
- **키**: `ANTHROPIC_API_KEY` 환경변수가 필요하다. 없으면 `annotate`·`prepare`·
  `audit-agent`·`audit-chat`·`audit-aggregate`는 명확히 실패하며, `convert`·`verify`와 결정론 소비 명령은 영향받지 않는다
  (anthropic import는 지연된다).
- **Structured output**: 응답 스키마를 도구로 강제(`tool_choice`)해 스키마-유효 JSON을
  구조적으로 방출받는다(텍스트 파싱 실패 제거). 그 위에 스키마 재검증 + V2 실재성 + 1회
  재시도가 걸린다.
- **LangSmith 트래킹(선택)**: `LANGCHAIN_API_KEY`(또는 `LANGSMITH_API_KEY`)가 있으면 각
  호출을 자동 트레이스한다(`LANGCHAIN_TRACING_V2`·`LANGCHAIN_PROJECT`로 제어). 없으면
  무트래킹으로 정상 동작. `langsmith`도 어노테이터 모듈 안에서만 지연 import(P1 경계).
- **큰 시트 입력 예산**: 시트 단위 프롬프트의 layout HTML이 크면 **행 경계로 발췌**(앞+뒤
  보존, 가운데 생략 표시)해 모델 컨텍스트를 넘지 않게 한다. 그래도 초과하면 예산을 줄여
  1회 재시도하고, 최종 초과 시 그 시트만 제외한다(나머지 시트·워크북은 계속). 입력 정책이
  바뀌면 `ANNOTATOR_VERSION`이 올라 기존 주석 캐시가 무효화된다.
- 생성 직후 상태는 `draft`다. 승인·승인판 SKILL.md 재생성(`review`)은 `review --approve`.
  승인 전 `verify`로 V2(evidence 실재성) 통과를 확인하길 권장한다.
